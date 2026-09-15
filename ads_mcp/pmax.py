"""Verified Performance Max reads and plan-bound request builders.

Resource identities and parent state are checked before staging and freshly
before application. The shared plan store owns preview, expiry, serialization
and single use; executors own audit-aware, non-retrying provider writes.
"""

from __future__ import annotations

from itertools import islice
import re

from google.protobuf.field_mask_pb2 import FieldMask

from ads_mcp import executors
from ads_mcp.continuation import retained
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


def campaign_state(ctx, campaign_id, customer_id):
    row = _unique(
        ctx, f"SELECT {CAMPAIGN_FIELDS} FROM campaign "
        f"WHERE campaign.id = {campaign_id} LIMIT 2", customer_id,
    )
    campaign = row.campaign
    if (str(campaign.id) != campaign_id
            or campaign.resource_name != resource(customer_id, "campaigns", campaign_id)
            or campaign.status.name not in LIVE_STATUSES
            or campaign.advertising_channel_type.name != "PERFORMANCE_MAX"):
        _unverified()
    return {
        "campaign_id": campaign_id,
        "resource_name": campaign.resource_name,
        "status": campaign.status.name,
        "channel_type": campaign.advertising_channel_type.name,
    }


def _group_payload(group, customer_id, campaign_id, *, live=False):
    identity = _resource_id(group.resource_name, customer_id, "assetGroups")
    if (str(group.id) != identity
            or group.campaign != resource(customer_id, "campaigns", campaign_id)
            or group.status.name not in (LIVE_STATUSES if live else
                                         LIVE_STATUSES | {"REMOVED"})):
        _unverified()
    return {
        "asset_group_id": identity,
        "resource_name": group.resource_name,
        "campaign_id": campaign_id,
        "name": group.name,
        "status": group.status.name,
        "primary_status": group.primary_status.name,
        "final_urls": list(group.final_urls),
        "final_mobile_urls": list(group.final_mobile_urls),
    }


def group_state(ctx, asset_group_id, customer_id):
    row = _unique(
        ctx, f"SELECT {GROUP_FIELDS} FROM asset_group "
        f"WHERE asset_group.id = {asset_group_id} LIMIT 2", customer_id,
    )
    campaign_id = _resource_id(row.asset_group.campaign, customer_id, "campaigns")
    group = _group_payload(row.asset_group, customer_id, campaign_id, live=True)
    if group["asset_group_id"] != asset_group_id:
        _unverified()
    campaign = campaign_state(ctx, campaign_id, customer_id)
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
