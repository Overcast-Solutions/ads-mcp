"""Verified Performance Max reads and plan-bound request builders.

Resource identities and parent state are checked before staging and freshly
before application. The shared plan store owns preview, expiry, serialization
and single use; executors own audit-aware, non-retrying provider writes.
"""

from __future__ import annotations

from itertools import islice
import hashlib
import json
import re
import unicodedata
from urllib.parse import urlsplit

from google.protobuf import json_format
from google.protobuf.field_mask_pb2 import FieldMask

from ads_mcp import executors
from ads_mcp.continuation import bounded_rows, retained
from ads_mcp.errors import ToolError


CAMPAIGN_FIELDS = (
    "campaign.id, campaign.resource_name, campaign.status, "
    "campaign.advertising_channel_type"
)
GROUP_FIELDS = (
    "asset_group.id, asset_group.resource_name, asset_group.campaign, "
    "asset_group.name, asset_group.status, asset_group.primary_status, "
    "asset_group.final_urls, asset_group.final_mobile_urls"
)
LIVE_STATUSES = frozenset({"ENABLED", "PAUSED"})
SIGNAL_FIELDS = (
    "asset_group_signal.resource_name, asset_group_signal.asset_group, "
    "asset_group_signal.approval_status, asset_group_signal.disapproval_reasons, "
    "asset_group_signal.search_theme.text, asset_group_signal.audience.audience, "
    "asset_group_signal.local_services_id.service_id, "
    "asset_group_signal.vertical_ads_item_group_rule_list.shared_set"
)
AUDIENCE_FIELDS = (
    "audience.id, audience.resource_name, audience.name, audience.status, "
    "audience.scope, audience.asset_group"
)
SUPPORTED_SIGNALS = frozenset({"search_theme", "audience"})
# Local safety limits, not a guarantee of provider acceptance.
MAX_SEARCH_THEMES = 50
MAX_THEME_CODEPOINTS = 80
EXPANSION = "FINAL_URL_EXPANSION_TEXT_ASSET_AUTOMATION"
CRITERION_FIELDS = (
    "campaign_criterion.criterion_id, campaign_criterion.resource_name, "
    "campaign_criterion.campaign, campaign_criterion.status, "
    "campaign_criterion.negative, campaign_criterion.type, "
    "campaign_criterion.webpage.conditions, campaign_criterion.webpage.criterion_name"
)
LISTING_FIELDS = ", ".join(
    "asset_group_listing_group_filter." + field for field in (
        "id", "resource_name", "asset_group", "type", "listing_source",
        "parent_listing_group_filter", "case_value.product_item_id.value",
        "case_value.product_brand.value", "case_value.product_category.category_id",
        "case_value.product_category.level", "case_value.product_channel.channel",
        "case_value.product_condition.condition", "case_value.product_custom_attribute.index",
        "case_value.product_custom_attribute.value", "case_value.product_type.level",
        "case_value.product_type.value", "case_value.webpage.conditions",
        "case_value.retail_filter_bundle.shared_set",
    )
)
MAX_LISTING_NODES = 1000
MAX_ITEM_IDS = MAX_LISTING_NODES - 2


def _product_unverified(message="Listing tree is inconsistent or unverifiable"):
    raise ToolError(
        "PMAX_PRODUCT_STATE_UNVERIFIED", message + ". Read get_listing_groups and "
        "review the tree. Only empty, all-products unit, or flat Item-ID trees "
        "with one catch-all can be replaced; other shapes need separate management.",
    )


def _listing_tree(ctx, customer_id, asset_group_id):
    group_name = resource(customer_id, "assetGroups", asset_group_id)

    def nodes():
        rows = ctx.search_iter(
            f"SELECT {LISTING_FIELDS} FROM asset_group_listing_group_filter "
            f"WHERE asset_group_listing_group_filter.asset_group = '{group_name}' "
            "ORDER BY asset_group_listing_group_filter.id LIMIT 1001", customer_id,
        )
        for index, row in enumerate(islice(rows, MAX_LISTING_NODES + 1)):
            if index == MAX_LISTING_NODES:
                _product_unverified("Complete tree exceeds the local 1000-node ceiling")
            node = row.asset_group_listing_group_filter
            identity = numeric_id(node.id, "listing filter ID")
            if (node.resource_name != resource(customer_id, "assetGroupListingGroupFilters",
                                               asset_group_id + "~" + identity)
                    or node.asset_group != group_name
                    or getattr(node.listing_source, "name", None) != "SHOPPING"):
                _product_unverified()
            kind = getattr(node.type_, "name", None)
            if kind not in {"SUBDIVISION", "UNIT_INCLUDED", "UNIT_EXCLUDED"}:
                _product_unverified("Unknown listing node type")
            dimension = node.case_value._pb.WhichOneof("dimension")
            if node.parent_listing_group_filter:
                if dimension != "product_item_id":
                    _product_unverified("Only Item-ID child dimensions are supported")
                item = node.case_value.product_item_id
                if item._pb.HasField("value") and not item.value:
                    _product_unverified("An explicit empty Item ID is ambiguous")
            elif node._pb.HasField("case_value"):
                _product_unverified("Root must not carry a dimension")
            yield json_format.MessageToDict(node._pb, preserving_proto_field_name=True)

    encoded, _, truncated = ctx.retry_account_read(lambda: bounded_rows(nodes()), customer_id)
    if truncated:
        _product_unverified("Complete tree exceeds the local 16 MiB state limit")
    tree = sorted((json.loads(row) for row in encoded), key=lambda node: int(node["id"]))
    if not tree:
        return tree
    names = {node["resource_name"] for node in tree}
    roots = [node for node in tree if not node.get("parent_listing_group_filter")]
    if len(names) != len(tree) or len(roots) != 1:
        _product_unverified("Duplicate nodes or missing/multiple roots")
    root = roots[0]
    if root["type_"] != "SUBDIVISION":
        if len(tree) != 1:
            _product_unverified("All-products unit cannot have children")
        return tree
    catchalls, items = 0, set()
    for node in tree:
        if node is root:
            continue
        if (node.get("parent_listing_group_filter") != root["resource_name"]
                or node["type_"] not in {"UNIT_INCLUDED", "UNIT_EXCLUDED"}):
            _product_unverified("Nested, cyclic or disconnected trees are unsupported")
        item = node["case_value"]["product_item_id"].get("value")
        if item is None:
            catchalls += 1
        elif item in items:
            _product_unverified("Duplicate Item-ID siblings")
        else:
            items.add(item)
    if catchalls != 1:
        _product_unverified("A subdivision needs exactly one Item-ID catch-all")
    return tree


def _product_state(ctx, customer_id, asset_group_id):
    return {
        "group": group_state(ctx, asset_group_id, customer_id, shopping=True),
        "tree": _listing_tree(ctx, customer_id, asset_group_id),
    }


def product_selection_plan(ctx, *, asset_group_id, item_ids):
    """Replace a verified flat tree in one dedicated atomic provider request."""
    asset_group_id = numeric_id(asset_group_id, "asset_group_id")
    _nonempty_list(item_ids, "item_ids", MAX_ITEM_IDS)
    items = []
    for value in item_ids:
        if (not isinstance(value, str) or not value.strip() or len(value.strip()) > 128
                or any(unicodedata.category(char).startswith("C") for char in value)
                or any(char.isspace() for char in value.strip())):
            raise ToolError("INVALID_ARGUMENT", "Item IDs must be nonblank strings of at most "
                            "128 Unicode codepoints without controls or internal whitespace")
        items.append(value.strip())
    if len(set(items)) != len(items):
        raise ToolError("INVALID_ARGUMENT", "Item IDs must be distinct after trimming; case is preserved")
    customer_id = ctx.config.customer_id
    before = _product_state(ctx, customer_id, asset_group_id)
    fingerprint = _fingerprint(before)
    group_name = before["group"]["resource_name"]
    root_name = resource(customer_id, "assetGroupListingGroupFilters", asset_group_id + "~-1")
    after = [{"resource_name": root_name, "asset_group": group_name,
              "type_": "SUBDIVISION", "listing_source": "SHOPPING"}]
    for index, item in enumerate([*items, None], start=2):
        after.append({
            "resource_name": resource(customer_id, "assetGroupListingGroupFilters",
                                      f"{asset_group_id}~-{index}"),
            "asset_group": group_name, "listing_source": "SHOPPING",
            "type_": "UNIT_EXCLUDED" if item is None else "UNIT_INCLUDED",
            "parent_listing_group_filter": root_name,
            "case_value": {"product_item_id": {} if item is None else {"value": item}},
        })

    def recheck(current):
        try:
            fresh = _product_state(current, customer_id, asset_group_id)
        except ToolError as exc:
            if exc.code not in {"PMAX_STATE_UNVERIFIED", "PMAX_PRODUCT_STATE_UNVERIFIED", "INVALID_ID"}:
                raise
            raise ToolError("STALE_PLAN", "Group, campaign or listing tree is no longer verified; "
                            "stage a fresh product-selection plan") from None
        if _fingerprint(fresh) != fingerprint:
            raise ToolError("STALE_PLAN", "Group, campaign feed or listing tree changed; "
                            "stage a fresh product-selection plan")

    def execute(current):
        client = current.client()
        batch = []
        # Validated trees are at most one level deep. Remove every child first.
        ordered = sorted(before["tree"], key=lambda node: not node.get("parent_listing_group_filter"))
        for node in ordered:
            operation = client.get_type("AssetGroupListingGroupFilterOperation")
            operation.remove = node["resource_name"]
            batch.append(operation)
        for node in after:
            operation = client.get_type("AssetGroupListingGroupFilterOperation")
            operation.create = node
            batch.append(operation)
        # This v25 dedicated request has no partial_failure field. Google
        # validates the complete resulting tree atomically; _send never retries.
        return executors._send(
            current, client, "AssetGroupListingGroupFilterService",
            "mutate_asset_group_listing_group_filters",
            "MutateAssetGroupListingGroupFiltersRequest", batch,
        )

    return {
        "tool": "set_asset_group_product_selection", "irreversible": True,
        "summary": f"Irreversibly replace the complete product tree for asset group {asset_group_id} "
                   f"with {len(items)} included Item IDs and an excluded everything-else leaf. "
                   "Changes inventory eligibility and may affect delivery and spend under the "
                   "existing budget. Provider eligibility and policy checks still apply.",
        "operations": [{"type": "replace_listing_tree", "asset_group": group_name,
                        "before": before, "after": after, "state_fingerprint": fingerprint}],
        "execute": execute, "rechecks": [recheck],
    }


def _url_unverified(message="URL settings or exclusions are inconsistent or unverifiable"):
    raise ToolError("PMAX_URL_STATE_UNVERIFIED", message)


def _enum_name(value):
    # New numeric enum values are not safe to rewrite with this SDK.
    name = getattr(value, "name", None)
    if name is None or name == "UNKNOWN":
        _url_unverified("Unknown provider enum in URL state")
    return name


def _provider_enum_name(value, refuse):
    """Keep known enum behavior while refusing unfamiliar numeric values."""
    name = getattr(value, "name", None)
    if name is None:
        refuse()
    return name


def _automation_settings(campaign):
    settings, seen = [], set()
    for setting in campaign.asset_automation_settings:
        kind = _enum_name(setting.asset_automation_type)
        status = _enum_name(setting.asset_automation_status)
        if kind == "UNSPECIFIED" or kind in seen:
            _url_unverified("Unknown or duplicate automation type cannot be safely preserved")
        seen.add(kind)
        settings.append({"asset_automation_type": kind, "asset_automation_status": status})
    _, _, truncated = bounded_rows(settings)
    if truncated:
        _url_unverified("Automation settings exceed the local verification limit")
    return settings


def _expansion_status(settings):
    for setting in settings:
        if setting["asset_automation_type"] == EXPANSION:
            return {"status": setting["asset_automation_status"], "explicit": True}
    return {"status": "UNSPECIFIED", "explicit": False}


def _exclusions(ctx, customer_id, campaign_id, *, fingerprint=False):
    campaign_name = resource(customer_id, "campaigns", campaign_id)
    rows = ctx.search_iter(
        f"SELECT {CRITERION_FIELDS} FROM campaign_criterion "
        f"WHERE campaign_criterion.campaign = '{campaign_name}' "
        "AND campaign_criterion.negative = TRUE AND campaign_criterion.type = WEBPAGE "
        "AND campaign_criterion.status != REMOVED "
        "ORDER BY campaign_criterion.criterion_id LIMIT 10001", customer_id,
    )
    seen = set()
    for index, row in enumerate(islice(rows, 10001)):
        if fingerprint and index == 10000:
            _url_unverified("Complete exclusion state exceeds the local 10000-row limit")
        criterion = row.campaign_criterion
        identity = numeric_id(criterion.criterion_id, "criterion ID")
        if (criterion.resource_name != resource(customer_id, "campaignCriteria",
                                                campaign_id + "~" + identity)
                or criterion.campaign != campaign_name or identity in seen):
            _url_unverified()
        seen.add(identity)
        kind = _enum_name(criterion.type_)
        status = _enum_name(criterion.status)
        if kind == "UNSPECIFIED" or status == "UNSPECIFIED":
            _url_unverified()
        if not criterion.negative or kind != "WEBPAGE" or status == "REMOVED":
            continue
        if (status not in LIVE_STATUSES
                or criterion._pb.WhichOneof("criterion") != "webpage"):
            _url_unverified()
        conditions = []
        for condition in criterion.webpage.conditions:
            operand = _enum_name(condition.operand)
            operator = _enum_name(condition.operator)
            if (operand == "UNSPECIFIED" or operator == "UNSPECIFIED"
                    or not condition.argument.strip()):
                _url_unverified("Incomplete webpage exclusion condition")
            conditions.append({"operand": operand, "operator": operator,
                               "argument": condition.argument})
        if not conditions:
            _url_unverified("Empty webpage exclusion condition set")
        payload = {"criterion_id": identity, "resource_name": criterion.resource_name,
                   "campaign_id": campaign_id, "conditions": conditions}
        if fingerprint:
            payload["source"] = json_format.MessageToDict(
                criterion._pb, preserving_proto_field_name=True,
            )
        yield payload


def get_pmax_url_settings(ctx, *, campaign_id, customer_id=None, page_token=None):
    campaign_id = numeric_id(campaign_id, "campaign_id")
    return _pmax_url_settings(ctx, campaign_id=campaign_id,
                              customer_id=customer_id, page_token=page_token)


@retained("exclusions")
def _pmax_url_settings(ctx, *, campaign_id, customer_id=None, page_token=None):
    customer_id = ctx.resolve_customer(customer_id)
    state = campaign_state(ctx, campaign_id, customer_id, automation=True)
    settings = state["automation_settings"]
    return {"customer_id": customer_id, "campaign_id": campaign_id,
            "final_url_expansion": _expansion_status(settings),
            "automation_settings": settings,
            "exclusions": _exclusions(ctx, customer_id, campaign_id)}


def _url_state(ctx, customer_id, campaign_id, *, automation):
    campaign = campaign_state(ctx, campaign_id, customer_id, automation=automation)
    if automation:
        return {"campaign": campaign}
    encoded, _, truncated = ctx.retry_account_read(
        lambda: bounded_rows(_exclusions(ctx, customer_id, campaign_id, fingerprint=True)),
        customer_id,
    )
    if truncated:
        _url_unverified("Complete exclusion state exceeds the local 16 MiB limit")
    return {"campaign": campaign,
            "exclusions": sorted((json.loads(row) for row in encoded),
                                 key=lambda row: row["criterion_id"])}


def _url_argument(url, match_type):
    if (not isinstance(match_type, str) or match_type not in {"EXACT", "CONTAINS"}
            or not isinstance(url, str) or not url
            or any(char.isspace() or unicodedata.category(char).startswith("C")
                   for char in url)):
        raise ToolError("INVALID_ARGUMENT", "Supply a nonblank URL without whitespace "
                        "or controls and match_type EXACT or CONTAINS")
    if match_type == "EXACT":
        try:
            parsed = urlsplit(url)
            valid = (parsed.scheme in {"http", "https"} and parsed.hostname
                     and parsed.username is None and parsed.password is None)
            parsed.port
        except ValueError:
            valid = False
        if not valid:
            raise ToolError("INVALID_ARGUMENT", "EXACT requires an HTTP(S) URL without user info")
    return {"operand": "URL", "operator": "EQUALS" if match_type == "EXACT" else "CONTAINS",
            "argument": url}


def url_plan(ctx, *, tool, campaign_id, enabled=None, url=None,
             match_type="EXACT", criterion_ids=None):
    campaign_id = numeric_id(campaign_id, "campaign_id")
    automation = tool == "set_pmax_final_url_expansion"
    removing = tool == "remove_pmax_url_exclusions"
    if automation:
        if type(enabled) is not bool:
            raise ToolError("INVALID_ARGUMENT", "enabled must be a boolean")
    elif removing:
        _nonempty_list(criterion_ids, "criterion_ids", 10000)
        criterion_ids = [numeric_id(value, "criterion_ids") for value in criterion_ids]
        if len(set(criterion_ids)) != len(criterion_ids):
            raise ToolError("INVALID_ARGUMENT", "Duplicate criterion IDs are not allowed")
    elif tool == "add_pmax_url_exclusion":
        condition = _url_argument(url, match_type)
    else:
        raise ToolError("INVALID_ARGUMENT", "Unsupported URL operation")
    customer_id = ctx.config.customer_id
    before = _url_state(ctx, customer_id, campaign_id, automation=automation)
    campaign_name = before["campaign"]["resource_name"]
    if automation:
        old = before["campaign"]["automation_settings"]
        new = [{**entry, "asset_automation_status": "OPTED_IN" if enabled else "OPTED_OUT"}
               if entry["asset_automation_type"] == EXPANSION else dict(entry) for entry in old]
        if not any(entry["asset_automation_type"] == EXPANSION for entry in old):
            new.append({"asset_automation_type": EXPANSION,
                        "asset_automation_status": "OPTED_IN" if enabled else "OPTED_OUT"})
        operations = [{"type": "update", "resource_name": campaign_name,
                       "update_mask": ["asset_automation_settings"],
                       "changes": {"asset_automation_settings": {"old": old, "new": new}},
                       "previous_expansion": _expansion_status(old)}]
        summary = (f"Set final URL expansion to {'OPTED_IN' if enabled else 'OPTED_OUT'} "
                   f"for campaign {campaign_id}. Expansion permits different landing "
                   "destinations and generated text for those pages. Disabling expansion "
                   "does not disable independent text customization.")
    elif removing:
        by_id = {row["criterion_id"]: row for row in before["exclusions"]}
        operations = []
        for identity in criterion_ids:
            row = by_id.get(identity)
            if row is None or not any(c["operand"] == "URL" for c in row["conditions"]):
                _url_unverified("Removal requires an existing negative WEBPAGE URL criterion "
                                "under this campaign")
            operations.append({"type": "remove", **{
                key: value for key, value in row.items() if key != "source"
            }})
        summary = f"Irreversibly remove {len(operations)} URL exclusions from campaign {campaign_id}."
    else:
        if any(row["conditions"] == [condition] for row in before["exclusions"]):
            raise ToolError("INVALID_ARGUMENT", "This URL exclusion already exists")
        criterion_name = "pmax-url-" + _fingerprint({
            "campaign": campaign_name, "condition": condition,
        })[:24]
        operations = [{"type": "create", "campaign": campaign_name,
                       "match_type": match_type, "criterion_name": criterion_name,
                       "conditions": [condition]}]
        summary = f"Add one {match_type} URL exclusion to campaign {campaign_id}."
    if not automation:
        summary += (" Exclusions are not universal destination blocks: explicitly supplied "
                    "final URLs and applicable Merchant Center inventory can still serve.")
    fingerprint = _fingerprint(before)
    for operation in operations:
        operation["state_fingerprint"] = fingerprint

    def recheck(current):
        try:
            after = _url_state(current, customer_id, campaign_id, automation=automation)
        except ToolError as exc:
            if exc.code not in {"PMAX_STATE_UNVERIFIED", "PMAX_URL_STATE_UNVERIFIED", "INVALID_ID"}:
                raise
            raise ToolError("STALE_PLAN", "URL or campaign state is no longer verified; "
                            "stage a fresh plan") from None
        if _fingerprint(after) != fingerprint:
            raise ToolError("STALE_PLAN", "URL or campaign state changed; stage a fresh plan")

    def execute(current):
        client = current.client()
        if automation:
            operation = client.get_type("CampaignOperation")
            operation.update.resource_name = campaign_name
            operation.update.asset_automation_settings = new
            operation.update_mask = FieldMask(paths=["asset_automation_settings"])
            return executors._send(current, client, "CampaignService", "mutate_campaigns",
                                   "MutateCampaignsRequest", [operation])
        batch = []
        for change in operations:
            operation = client.get_type("CampaignCriterionOperation")
            if removing:
                operation.remove = change["resource_name"]
            else:
                operation.create.campaign = campaign_name
                operation.create.negative = True
                operation.create.webpage.criterion_name = change["criterion_name"]
                operation.create.webpage.conditions = change["conditions"]
            batch.append(operation)
        return executors._send(current, client, "CampaignCriterionService",
                               "mutate_campaign_criteria", "MutateCampaignCriteriaRequest", batch)

    return {"tool": tool, "summary": summary, "operations": operations,
            "execute": execute, "rechecks": [recheck], "irreversible": removing}


def _signal_unverified(message="Signal or audience state is inconsistent or unverifiable"):
    raise ToolError("PMAX_SIGNAL_STATE_UNVERIFIED", message)


def _signal_payload(signal, customer_id, asset_group_id):
    prefix = resource(customer_id, "assetGroupSignals", asset_group_id + "~")
    if (not signal.resource_name.startswith(prefix)
            or signal.asset_group != resource(customer_id, "assetGroups", asset_group_id)):
        _signal_unverified()
    child = signal.resource_name[len(prefix):]
    if numeric_id(child, "signal ID") != child:
        _signal_unverified()
    kind = signal._pb.WhichOneof("signal") or "unknown"
    audience = signal.audience.audience if kind == "audience" else None
    if audience is not None:
        _resource_id(audience, customer_id, "audiences")
    return {
        "asset_group_id": asset_group_id,
        "signal_id": asset_group_id + "~" + child,
        "resource_name": signal.resource_name,
        "kind": kind,
        "supported": kind in SUPPORTED_SIGNALS,
        "search_theme": signal.search_theme.text if kind == "search_theme" else None,
        "audience_resource_name": audience,
        # This enum has no protobuf presence. UNSPECIFIED represents unavailable
        # review status; repeated reasons have no presence either.
        "approval_status": (_provider_enum_name(signal.approval_status, _signal_unverified)
                            if signal.approval_status else None),
        "disapproval_reasons": list(signal.disapproval_reasons),
    }


def _signals(ctx, customer_id, asset_group_id, *, fingerprint=False):
    group_name = resource(customer_id, "assetGroups", asset_group_id)
    rows = ctx.search_iter(
        f"SELECT {SIGNAL_FIELDS} FROM asset_group_signal "
        f"WHERE asset_group_signal.asset_group = '{group_name}'",
        customer_id,
    )
    seen = set()
    for row in rows:
        signal = row.asset_group_signal
        payload = _signal_payload(signal, customer_id, asset_group_id)
        if payload["signal_id"] in seen:
            _signal_unverified("Duplicate signal identity in provider response")
        seen.add(payload["signal_id"])
        if fingerprint:
            # Keep unsupported payloads in the comparison too. Their contents
            # must not disappear just because this tool cannot mutate them.
            payload["source"] = json_format.MessageToDict(
                signal._pb, preserving_proto_field_name=True,
            )
        yield payload


def get_asset_group_signals(ctx, *, asset_group_id, customer_id=None, page_token=None):
    asset_group_id = numeric_id(asset_group_id, "asset_group_id")
    return _asset_group_signals(ctx, asset_group_id=asset_group_id,
                               customer_id=customer_id, page_token=page_token)


@retained("signals")
def _asset_group_signals(ctx, *, asset_group_id, customer_id=None, page_token=None):
    customer_id = ctx.resolve_customer(customer_id)
    group_state(ctx, asset_group_id, customer_id)
    return {"customer_id": customer_id, "asset_group_id": asset_group_id,
            "signals": _signals(ctx, customer_id, asset_group_id)}


def _audience_payload(audience, customer_id):
    identity = _resource_id(audience.resource_name, customer_id, "audiences")
    if str(audience.id) != identity:
        _signal_unverified()
    group = (_resource_id(audience.asset_group, customer_id, "assetGroups")
             if audience.asset_group else None)
    scope = _provider_enum_name(audience.scope, _signal_unverified)
    if ((scope == "CUSTOMER" and group is not None)
            or (scope == "ASSET_GROUP" and group is None)):
        _signal_unverified("Audience scope and asset-group binding disagree")
    return {
        "audience_id": identity, "resource_name": audience.resource_name,
        "name": audience.name if audience._pb.HasField("name") else None,
        "status": _provider_enum_name(audience.status, _signal_unverified),
        "scope": scope, "asset_group_id": group,
    }


@retained("audiences")
def list_audiences(ctx, *, customer_id=None, page_token=None):
    customer_id = ctx.resolve_customer(customer_id)
    rows = ctx.search_iter(
        f"SELECT {AUDIENCE_FIELDS} FROM audience ORDER BY audience.id", customer_id,
    )

    def audiences():
        seen = set()
        for row in rows:
            payload = _audience_payload(row.audience, customer_id)
            if payload["audience_id"] in seen:
                _signal_unverified("Duplicate audience identity in provider response")
            seen.add(payload["audience_id"])
            yield payload

    return {"customer_id": customer_id, "audiences": audiences()}


def _audience_state(ctx, customer_id, asset_group_id, audience_id):
    row = _unique(
        ctx, f"SELECT {AUDIENCE_FIELDS}, audience.dimensions, "
        "audience.exclusion_dimension FROM audience "
        f"WHERE audience.id = {audience_id} LIMIT 2", customer_id,
    )
    audience = row.audience
    payload = _audience_payload(audience, customer_id)
    if (payload["audience_id"] != audience_id or payload["status"] != "ENABLED"
            or payload["scope"] not in {"CUSTOMER", "ASSET_GROUP"}
            or (payload["scope"] == "ASSET_GROUP"
                and payload["asset_group_id"] != asset_group_id)):
        _signal_unverified("Audience must be enabled and scoped to this customer or asset group")
    # Bind composition without editing it, so a changed audience needs a new
    # preview. Use the same byte ceiling as other bounded relevant state.
    source = json_format.MessageToDict(audience._pb, preserving_proto_field_name=True)
    _, _, truncated = bounded_rows([source])
    if truncated:
        _signal_unverified("Audience state exceeds the local verification byte limit")
    return source


def _signal_state(ctx, customer_id, asset_group_id, audience_id=None):
    group = group_state(ctx, asset_group_id, customer_id)
    encoded, _, truncated = ctx.retry_account_read(
        lambda: bounded_rows(_signals(ctx, customer_id, asset_group_id, fingerprint=True)),
        customer_id,
    )
    if truncated:
        _signal_unverified("Complete signal state exceeds the local 10000-row or 16 MiB limit")
    signals = sorted((json.loads(row) for row in encoded), key=lambda row: row["signal_id"])
    audience = (_audience_state(ctx, customer_id, asset_group_id, audience_id)
                if audience_id is not None else None)
    return {"group": group, "signals": signals, "audience": audience}


def _fingerprint(state):
    return hashlib.sha256(json.dumps(state, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":")).encode("utf-8")).hexdigest()


def _nonempty_list(values, name, maximum):
    if not isinstance(values, list) or not 1 <= len(values) <= maximum:
        raise ToolError("INVALID_ARGUMENT",
                        f"{name} must be a nonempty list of at most {maximum} values")


def signal_plan(ctx, *, tool, asset_group_id, themes=None, audience_id=None, signal_ids=None):
    """Stage only locally valid, verified signal additions or supported removals."""
    asset_group_id = numeric_id(asset_group_id, "asset_group_id")
    if tool == "add_asset_group_search_themes":
        _nonempty_list(themes, "themes", MAX_SEARCH_THEMES)
        normalized = []
        for theme in themes:
            if (not isinstance(theme, str) or not theme.strip()
                    or len(theme.strip()) > MAX_THEME_CODEPOINTS
                    or any(unicodedata.category(char).startswith("C") for char in theme)):
                raise ToolError("INVALID_ARGUMENT", "Themes must be nonblank text of "
                                "at most 80 Unicode codepoints without control characters")
            normalized.append(theme.strip())
        themes = normalized
        if len(set(themes)) != len(themes):
            raise ToolError("INVALID_ARGUMENT", "Duplicate search themes are not allowed")
    elif tool == "add_asset_group_audience_signal":
        audience_id = numeric_id(audience_id, "audience_id")
    elif tool == "remove_asset_group_signals":
        _nonempty_list(signal_ids, "signal_ids", 10000)
        signal_ids = [numeric_id(value, "signal_ids") for value in signal_ids]
        if len(set(signal_ids)) != len(signal_ids):
            raise ToolError("INVALID_ARGUMENT", "Duplicate signal IDs are not allowed")
    else:
        raise ToolError("INVALID_ARGUMENT", "Unsupported signal operation")
    customer_id = ctx.config.customer_id
    before = _signal_state(ctx, customer_id, asset_group_id, audience_id)
    operations = []
    if themes is not None:
        existing = [row["search_theme"] for row in before["signals"]
                    if row["kind"] == "search_theme"]
        if (len(existing) + len(themes) > MAX_SEARCH_THEMES
                or set(themes).intersection(text.strip() for text in existing)):
            raise ToolError("INVALID_ARGUMENT", "Theme already exists or would exceed "
                            "the local ceiling of 50 resulting themes; Google may "
                            "impose additional limits")
        operations = [{"type": "create", "kind": "search_theme", "search_theme": text}
                      for text in themes]
    elif audience_id is not None:
        audience_name = resource(customer_id, "audiences", audience_id)
        if any(row["audience_resource_name"] == audience_name for row in before["signals"]):
            raise ToolError("INVALID_ARGUMENT", "This audience signal is already attached")
        operations = [{"type": "create", "kind": "audience",
                       "audience_resource_name": audience_name}]
    else:
        by_id = {row["signal_id"]: row for row in before["signals"]}
        for child in signal_ids:
            row = by_id.get(asset_group_id + "~" + child)
            if row is None or row["kind"] not in SUPPORTED_SIGNALS:
                _signal_unverified("Removal requires an existing search_theme or "
                                   "audience signal under this group; other kinds "
                                   "are unsupported")
            operations.append({"type": "remove", **{
                key: value for key, value in row.items() if key != "source"
            }})
    fingerprint = _fingerprint(before)
    for operation in operations:
        operation["asset_group"] = before["group"]["resource_name"]
        operation["state_fingerprint"] = fingerprint

    def recheck(current):
        try:
            after = _signal_state(current, customer_id, asset_group_id, audience_id)
        except ToolError as exc:
            if exc.code not in {"PMAX_STATE_UNVERIFIED", "PMAX_SIGNAL_STATE_UNVERIFIED",
                                "INVALID_ID"}:
                raise
            raise ToolError("STALE_PLAN", "Signal, group or audience state is no "
                            "longer verified; stage a fresh plan") from None
        if _fingerprint(after) != fingerprint:
            raise ToolError("STALE_PLAN", "Signal, group or audience state changed; "
                            "stage a fresh plan")

    def execute(current):
        client = current.client()
        batch = []
        for change in operations:
            operation = client.get_type("AssetGroupSignalOperation")
            if change["type"] == "remove":
                operation.remove = change["resource_name"]
            else:
                operation.create.asset_group = change["asset_group"]
                if change["kind"] == "search_theme":
                    operation.create.search_theme.text = change["search_theme"]
                else:
                    operation.create.audience.audience = change["audience_resource_name"]
            batch.append(operation)
        return executors._send(
            current, client, "AssetGroupSignalService", "mutate_asset_group_signals",
            "MutateAssetGroupSignalsRequest", batch,
        )

    removing = signal_ids is not None
    return {
        "tool": tool,
        "summary": f"{'Remove' if removing else 'Add'} {len(operations)} optimization signals "
                   f"for asset group {asset_group_id}. Signals guide optimization, "
                   "not hard targeting. "
                   "Provider eligibility and policy checks still apply."
                   + (" Removal is irreversible." if removing else ""),
        "operations": operations, "execute": execute, "rechecks": [recheck],
        "irreversible": removing,
    }


def numeric_id(value, name):
    """Normalize a positive, ASCII, signed-int64 resource identifier locally."""
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ToolError("INVALID_ID", f"{name} must be a positive numeric ID")
    text = str(value).strip()
    if (not re.fullmatch(r"[0-9]{1,19}", text)
            or not 0 < int(text) <= 2**63 - 1):
        raise ToolError("INVALID_ID", f"{name} must be a positive numeric ID")
    return str(int(text))


def resource(customer_id, kind, identity):
    return f"customers/{customer_id}/{kind}/{identity}"


def _unverified():
    raise ToolError(
        "PMAX_STATE_UNVERIFIED",
        "Could not verify a unique asset group and live Performance Max "
        "campaign in the requested account. Read the account and try again.",
    )


def _resource_id(value, customer_id, kind):
    prefix = resource(customer_id, kind, "")
    if not isinstance(value, str) or not value.startswith(prefix):
        _unverified()
    identity = value[len(prefix):]
    try:
        canonical = numeric_id(identity, "resource ID")
    except ToolError:
        _unverified()
    if identity != canonical:
        _unverified()
    return canonical


def _unique(ctx, query, customer_id):
    # Two rows prove non-uniqueness without retaining an unbounded response.
    rows = ctx.retry_account_read(
        lambda: list(islice(ctx.search_iter(query, customer_id), 2)), customer_id,
    )
    if len(rows) != 1:
        _unverified()
    return rows[0]


def campaign_state(ctx, campaign_id, customer_id, *, automation=False, shopping=False):
    fields = CAMPAIGN_FIELDS + (", campaign.asset_automation_settings" if automation else "")
    if shopping:
        fields += (", campaign.shopping_setting.merchant_id, campaign.shopping_setting.feed_label, "
                   "campaign.shopping_setting.enable_local")
    row = _unique(
        ctx, f"SELECT {fields} FROM campaign "
        f"WHERE campaign.id = {campaign_id} LIMIT 2", customer_id,
    )
    campaign = row.campaign
    status = _provider_enum_name(campaign.status, _unverified)
    channel = _provider_enum_name(campaign.advertising_channel_type, _unverified)
    if (str(campaign.id) != campaign_id
            or campaign.resource_name != resource(customer_id, "campaigns", campaign_id)
            or status not in LIVE_STATUSES
            or channel != "PERFORMANCE_MAX"):
        _unverified()
    state = {
        "campaign_id": campaign_id,
        "resource_name": campaign.resource_name,
        "status": status,
        "channel_type": channel,
    }
    if automation:
        state["automation_settings"] = _automation_settings(campaign)
    if shopping:
        if campaign.shopping_setting.merchant_id <= 0:
            _product_unverified("Campaign needs a nonzero Merchant Center feed link")
        state["shopping_setting"] = json_format.MessageToDict(
            campaign.shopping_setting._pb, preserving_proto_field_name=True,
        )
    return state


def _group_payload(group, customer_id, campaign_id, *, live=False):
    identity = _resource_id(group.resource_name, customer_id, "assetGroups")
    status = _provider_enum_name(group.status, _unverified)
    if (str(group.id) != identity
            or group.campaign != resource(customer_id, "campaigns", campaign_id)
            or status not in (LIVE_STATUSES if live else LIVE_STATUSES | {"REMOVED"})):
        _unverified()
    return {
        "asset_group_id": identity,
        "resource_name": group.resource_name,
        "campaign_id": campaign_id,
        "name": group.name,
        "status": status,
        "primary_status": _provider_enum_name(group.primary_status, _unverified),
        "final_urls": list(group.final_urls),
        "final_mobile_urls": list(group.final_mobile_urls),
    }


def group_state(ctx, asset_group_id, customer_id, *, shopping=False):
    row = _unique(
        ctx, f"SELECT {GROUP_FIELDS} FROM asset_group "
        f"WHERE asset_group.id = {asset_group_id} LIMIT 2", customer_id,
    )
    campaign_id = _resource_id(row.asset_group.campaign, customer_id, "campaigns")
    group = _group_payload(row.asset_group, customer_id, campaign_id, live=True)
    if group["asset_group_id"] != asset_group_id:
        _unverified()
    campaign = campaign_state(ctx, campaign_id, customer_id, shopping=shopping)
    # Status plans depend on identity, relationship and lifecycle state, not
    # independently editable names, URLs or serving diagnostics.
    return {
        "asset_group_id": asset_group_id,
        "resource_name": group["resource_name"],
        "status": group["status"],
        "campaign": campaign,
    }


def get_asset_groups(ctx, *, campaign_id, customer_id=None, page_token=None):
    # Normalize before constructing the continuation scope and before any read.
    campaign_id = numeric_id(campaign_id, "campaign_id")
    return _asset_groups(ctx, campaign_id=campaign_id, customer_id=customer_id,
                         page_token=page_token)


@retained("asset_groups")
def _asset_groups(ctx, *, campaign_id, customer_id=None, page_token=None):
    customer_id = ctx.resolve_customer(customer_id)
    campaign_state(ctx, campaign_id, customer_id)
    rows = ctx.search_iter(
        f"SELECT {GROUP_FIELDS} FROM asset_group "
        f"WHERE asset_group.campaign = '{resource(customer_id, 'campaigns', campaign_id)}' "
        "ORDER BY asset_group.id", customer_id,
    )

    def groups():
        seen = set()
        for row in rows:
            payload = _group_payload(row.asset_group, customer_id, campaign_id)
            identity = payload["asset_group_id"]
            if identity in seen:
                _unverified()
            seen.add(identity)
            yield payload

    return {"customer_id": customer_id, "campaign_id": campaign_id,
            "asset_groups": groups()}


def status_plan(ctx, *, tool, asset_group_id, status_name):
    """Return shared-plan arguments without staging or executing a write."""
    asset_group_id = numeric_id(asset_group_id, "entity_id")
    customer_id = ctx.config.customer_id
    before = group_state(ctx, asset_group_id, customer_id)

    def recheck(current):
        try:
            after = group_state(current, asset_group_id, customer_id)
        except ToolError as exc:
            if exc.code != "PMAX_STATE_UNVERIFIED":
                raise
            raise ToolError(
                "STALE_PLAN", "Asset-group or campaign state is no longer verified; "
                "read the current state and stage a fresh plan.",
            ) from None
        if after != before:
            raise ToolError(
                "STALE_PLAN", "Asset-group or campaign state changed; "
                "read the current state and stage a fresh plan.",
            )

    def execute(current):
        client = current.client()
        operation = client.get_type("AssetGroupOperation")
        operation.update.resource_name = before["resource_name"]
        operation.update.status = getattr(client.enums.AssetGroupStatusEnum, status_name)
        operation.update_mask = FieldMask(paths=["status"])
        return executors._send(
            current, client, "AssetGroupService", "mutate_asset_groups",
            "MutateAssetGroupsRequest", [operation],
        )

    impact = (
        "Enabling may resume delivery and spend under the existing campaign budget."
        if status_name == "ENABLED" else
        "Pausing stops this group's delivery; other groups may still spend under "
        "the existing campaign budget."
    )
    return {
        "tool": tool,
        "summary": f"Set asset group {asset_group_id} in campaign "
                   f"{before['campaign']['campaign_id']} from {before['status']} "
                   f"to {status_name}. {impact}",
        "operations": [{
            "type": tool,
            "resource": before["resource_name"],
            "campaign": before["campaign"],
            "update_mask": ["status"],
            "changes": {"status": {"old": before["status"], "new": status_name}},
        }],
        "execute": execute,
        "rechecks": [recheck],
    }
