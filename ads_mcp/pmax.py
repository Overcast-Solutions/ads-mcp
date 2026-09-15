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
        "approval_status": signal.approval_status.name if signal.approval_status else None,
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
    scope = audience.scope.name
    if ((scope == "CUSTOMER" and group is not None)
            or (scope == "ASSET_GROUP" and group is None)):
        _signal_unverified("Audience scope and asset-group binding disagree")
    return {
        "audience_id": identity, "resource_name": audience.resource_name,
        "name": audience.name if audience._pb.HasField("name") else None,
        "status": audience.status.name, "scope": scope, "asset_group_id": group,
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
