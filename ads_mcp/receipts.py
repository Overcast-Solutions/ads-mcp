"""Validate mutation identities before retaining them or using dependencies."""

import re
from dataclasses import dataclass, field

from ads_mcp.errors import ToolError


# Resource collections supported by the write surface. Composite names retain
# every component; a child ID alone cannot identify these resources.
_COLLECTIONS = {
    "Campaign": "campaigns", "CampaignBudget": "campaignBudgets",
    "AdGroup": "adGroups", "Ad": "ads", "Asset": "assets",
    "CustomAudience": "customAudiences", "ConversionAction": "conversionActions",
    "BiddingStrategy": "biddingStrategies", "AssetGroup": "assetGroups",
    "SharedSet": "sharedSets", "Experiment": "experiments",
    "CampaignCriterion": "campaignCriteria", "AdGroupCriterion": "adGroupCriteria",
    "AdGroupAd": "adGroupAds", "CampaignAsset": "campaignAssets",
    "AssetGroupAsset": "assetGroupAssets", "AssetGroupSignal": "assetGroupSignals",
    "AssetGroupListingGroupFilter": "assetGroupListingGroupFilters",
    "SharedCriterion": "sharedCriteria", "CampaignSharedSet": "campaignSharedSets",
    "ExperimentArm": "experimentArms",
}
_PARENTS = {
    "CampaignCriterion": ("campaign",), "AdGroupCriterion": ("ad_group",),
    "AdGroupAd": ("ad_group",), "AssetGroupSignal": ("asset_group",),
    "AssetGroupListingGroupFilter": ("asset_group",),
    "SharedCriterion": ("shared_set",), "ExperimentArm": ("experiment",),
    "CampaignSharedSet": ("campaign", "shared_set"),
    "CampaignAsset": ("campaign", "asset"),
    "AssetGroupAsset": ("asset_group", "asset"),
}
_ASSET_LINKS = {"CampaignAsset", "AssetGroupAsset"}


def _invalid():
    raise ToolError(
        "MUTATION_RECEIPT_INVALID",
        "The write response could not be reconciled with its operations. "
        "Changes may have landed; inspect the account before staging another "
        "plan. This write is not retried.",
    )


def _parts(name, customer, kind):
    collection = _COLLECTIONS.get(kind)
    if not isinstance(name, str) or collection is None:
        _invalid()
    prefix = f"customers/{customer}/{collection}/"
    if not name.startswith(prefix) or len(name) > 256:
        _invalid()
    parts = name[len(prefix):].split("~")
    count = 3 if kind in _ASSET_LINKS else (2 if kind in _PARENTS else 1)
    if len(parts) != count:
        _invalid()
    numbers = parts[:-1] if kind in _ASSET_LINKS else parts
    if any(not re.fullmatch(r"[1-9][0-9]{0,18}", value)
           or int(value) > 2**63 - 1 for value in numbers):
        _invalid()
    if kind in _ASSET_LINKS and not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", parts[-1]):
        _invalid()
    return parts


def validate(request, response):
    """Return a whole step's receipts only after every result is consistent.

    Temporary request names establish correlations, never output identities.
    A malformed batch supplies no trusted receipts, even if some rows look valid.
    """
    try:
        return _validate(request, response)
    except ToolError:
        raise
    except Exception:
        # SDK shape errors are equally uncertain; never expose their payload.
        _invalid()


def _validate(request, response):
    if getattr(response, "partial_failure_error", None) is not None:
        if response.partial_failure_error.code:
            _invalid()
    heterogeneous = "mutate_operations" in request._pb.DESCRIPTOR.fields_by_name
    operations = request.mutate_operations if heterogeneous else request.operations
    results = response.mutate_operation_responses if heterogeneous else response.results
    if not operations or len(operations) != len(results):
        _invalid()
    rows, temporary, seen = [], {}, set()
    for wrapped, result in zip(operations, results):
        operation = wrapped
        if heterogeneous:
            selected = wrapped._pb.WhichOneof("operation")
            if not selected or result._pb.WhichOneof("response") != selected.removesuffix("_operation") + "_result":
                _invalid()
            operation = getattr(wrapped, selected)
            result = getattr(result, result._pb.WhichOneof("response"))
        action = operation._pb.WhichOneof("operation")
        if action not in {"create", "update", "remove"}:
            _invalid()
        kind = operation._pb.DESCRIPTOR.name.removesuffix("Operation")
        name = result.resource_name
        components = _parts(name, request.customer_id, kind)
        if name in seen:
            _invalid()
        seen.add(name)
        entity = getattr(operation, action) if action != "remove" else None
        original = operation.remove if action == "remove" else entity.resource_name
        if action in {"update", "remove"} and name != original:
            _invalid()
        if action == "create" and original:
            if "-" in original.rsplit("/", 1)[-1]:
                if original in temporary:
                    _invalid()
                temporary[original] = name
            elif original != name:
                _invalid()
        rows.append((action, kind, entity, name, components))

    receipts = {"created": [], "updated": []}
    for action, kind, entity, name, components in rows:
        if action == "create":
            for index, parent_field in enumerate(_PARENTS.get(kind, ())):
                parent = getattr(entity, parent_field)
                resolved = temporary.get(parent, parent)
                if not resolved or resolved.rsplit("/", 1)[-1] != components[index]:
                    _invalid()
            if kind in _ASSET_LINKS and entity.field_type.name != components[-1]:
                _invalid()
            receipts["created"].append(name)
        elif action == "update":
            receipts["updated"].append(name)
    return receipts


@dataclass
class ApplyReceipts:
    """One confirmation's state, owned by its worker and cleared on exit."""

    created: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    complete: bool = True

    def accept(self, step):
        if (set(step["created"]) & set(self.created + self.updated)
                or set(step["updated"]) & set(self.created)):
            self.complete = False
            _invalid()
        for category in ("created", "updated"):
            retained = getattr(self, category)
            retained.extend(name for name in step[category] if name not in retained)

    def payload(self):
        result = {"created": list(self.created), "updated": list(self.updated),
                  "receipts_complete": self.complete}
        if not self.complete:
            result["partial_changes_possible"] = True
        return result
