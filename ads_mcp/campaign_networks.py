"""Explicit Search network settings and guarded partial campaign updates."""

from __future__ import annotations

from itertools import islice

from google.protobuf.field_mask_pb2 import FieldMask

from ads_mcp import executors
from ads_mcp.errors import ToolError, classify_exception
from ads_mcp.search_urls import numeric_id

DEFAULTS = {
    "target_google_search": True,
    "target_search_network": False,
    "target_partner_search_network": False,
    "target_content_network": False,
}
FIELDS = tuple(DEFAULTS)
STATE_FIELDS = (
    "id", "resource_name", "status", "advertising_channel_type",
    "advertising_channel_sub_type",
)


def supplied_settings(**values):
    supplied = {field: value for field, value in values.items() if value is not None}
    if any(field not in FIELDS or type(value) is not bool
           for field, value in supplied.items()):
        raise ToolError("INVALID_ARGUMENT", "Network settings require booleans or null")
    return supplied


def validate_relation(settings):
    if settings["target_search_network"] and not settings["target_google_search"]:
        raise ToolError("INVALID_NETWORK_SETTINGS", "Search Partners requires Google Search")


def creation_settings(channel, **values):
    supplied = supplied_settings(**values)
    if channel != "SEARCH":
        if supplied:
            raise ToolError("INVALID_NETWORK_SETTINGS", "Network options require a Search campaign")
        return None
    settings = {**DEFAULTS, **supplied}
    validate_relation(settings)
    return settings


def observed_settings(campaign):
    """Keep optional provider field presence, including explicit false."""
    settings = campaign.network_settings._pb
    return {field: getattr(settings, field) if settings.HasField(field) else None
            for field in FIELDS}


def _unverified():
    raise ToolError("CAMPAIGN_NETWORK_STATE_UNVERIFIED",
                    "Could not verify complete standard Search campaign network state")


def _state(ctx, customer_id, campaign_id):
    try:
        fields = ["campaign." + field for field in STATE_FIELDS]
        fields += ["campaign.network_settings." + field for field in FIELDS]
        query = (f"SELECT {', '.join(fields)} FROM campaign "
                 f"WHERE campaign.id = {campaign_id} LIMIT 2")
        rows = ctx.retry_account_read(
            lambda: list(islice(ctx.search_iter(query, customer_id), 2)), customer_id,
        )
        if len(rows) != 1:
            _unverified()
        campaign = rows[0].campaign
        raw = campaign._pb
        enums = {}
        for field in ("status", "advertising_channel_type", "advertising_channel_sub_type"):
            descriptor = raw.DESCRIPTOR.fields_by_name[field].enum_type
            value = descriptor.values_by_number.get(getattr(raw, field))
            if value is None:
                _unverified()
            enums[field] = value.name
        identity = f"customers/{customer_id}/campaigns/{campaign_id}"
        settings = observed_settings(campaign)
        if (str(campaign.id) != campaign_id or campaign.resource_name != identity
                or enums["status"] not in {"ENABLED", "PAUSED"}
                or enums["advertising_channel_type"] != "SEARCH"
                or enums["advertising_channel_sub_type"] != "UNSPECIFIED"
                or any(value is None for value in settings.values())):
            _unverified()
        return {"customer_id": customer_id, "campaign_id": campaign_id,
                "resource_name": identity, **enums, "network_settings": settings}
    except Exception as exc:
        error = classify_exception(exc, scrub=ctx.scrub)
        if error.code.startswith("AUTH_") or error.code == "ACCOUNT_NOT_ACCESSIBLE":
            raise error from None
        _unverified()


def network_plan(ctx, *, campaign_id, **values):
    campaign_id = numeric_id(campaign_id, "campaign_id")
    supplied = supplied_settings(**values)
    if not supplied:
        raise ToolError("MISSING_ARGUMENT", "Supply at least one network boolean")
    customer_id = ctx.config.customer_id
    state = _state(ctx, customer_id, campaign_id)
    merged = {**state["network_settings"], **supplied}
    validate_relation(merged)
    mask = ["network_settings." + field for field in FIELDS if field in supplied]

    def recheck(current):
        try:
            fresh = _state(current, customer_id, campaign_id)
        except ToolError:
            raise ToolError("STALE_PLAN", "Campaign network state is no longer verified; "
                            "inspect and stage a fresh plan") from None
        if fresh != state:
            raise ToolError("STALE_PLAN", "Campaign network state changed; "
                            "inspect and stage a fresh plan")

    def execute(current):
        client = current.client()
        operation = client.get_type("CampaignOperation")
        operation.update.resource_name = state["resource_name"]
        for field, value in supplied.items():
            setattr(operation.update.network_settings, field, value)
        operation.update_mask = FieldMask(paths=mask)
        return executors._send(current, client, "CampaignService", "mutate_campaigns",
                               "MutateCampaignsRequest", [operation])

    return {
        "tool": "set_campaign_networks",
        "summary": f"Update Search networks for campaign {campaign_id}. "
                   "Enabling networks may change serving and spend. Restricted partner "
                   "targeting requires provider/account eligibility; provider validation applies.",
        "operations": [{"type": "update", "resource_name": state["resource_name"],
                        "before": {field: state["network_settings"][field] for field in supplied},
                        "after": supplied, "update_mask": mask, "preserved_state": state}],
        "execute": execute, "rechecks": [recheck],
    }
