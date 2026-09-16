"""Inspect existing Search destinations and stage precise URL updates."""

from __future__ import annotations

import hashlib
import json
import re
from itertools import islice
from urllib.parse import urlsplit

from google.protobuf.field_mask_pb2 import FieldMask

from ads_mcp import executors
from ads_mcp.continuation import bounded_rows
from ads_mcp.errors import ToolError, classify_exception


CAMPAIGN_FIELDS = (
    "campaign.id, campaign.resource_name, campaign.status, "
    "campaign.advertising_channel_type"
)
GROUP_FIELDS = (
    "ad_group.id, ad_group.resource_name, ad_group.campaign, "
    "ad_group.status, ad_group.type"
)
AD_FIELDS = ", ".join(
    "ad_group_ad." + field for field in (
        "resource_name", "ad_group", "status", "ad.id", "ad.resource_name", "ad.type",
        "ad.final_urls", "ad.final_mobile_urls", "ad.tracking_url_template",
        "ad.final_url_suffix", "ad.url_custom_parameters",
        "ad.responsive_search_ad.headlines", "ad.responsive_search_ad.descriptions",
        "ad.responsive_search_ad.path1", "ad.responsive_search_ad.path2",
    )
)
KEYWORD_FIELDS = ", ".join(
    "ad_group_criterion." + field for field in (
        "resource_name", "ad_group", "criterion_id", "status", "type", "negative",
        "keyword.text", "keyword.match_type", "final_urls", "final_mobile_urls",
        "tracking_url_template", "final_url_suffix", "url_custom_parameters",
    )
)
LIVE_STATUSES = frozenset({"ENABLED", "PAUSED"})
URL_FIELDS = ("final_urls", "final_mobile_urls")


def numeric_id(value, name):
    """Require an exact positive decimal string within signed int64."""
    if (not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]{0,18}", value)
            or int(value) > 2**63 - 1):
        raise ToolError("INVALID_ID", f"{name} requires a canonical positive numeric string")
    return value


def resource(customer_id, kind, identity):
    return f"customers/{customer_id}/{kind}/{identity}"


def _unverified():
    raise ToolError(
        "SEARCH_URL_STATE_UNVERIFIED",
        "Could not verify complete Search destination, creative and parent state. "
        "Inspect the requested resource and account before trying again.",
    )


def _enum(message, field):
    """Resolve raw enum numbers without triggering proto-plus warnings."""
    raw = message._pb
    descriptor = raw.DESCRIPTOR.fields_by_name[field].enum_type
    value = descriptor.values_by_number.get(getattr(raw, field))
    if value is None or value.name == "UNKNOWN":
        _unverified()
    return value.name


def _resource_id(value, customer_id, kind):
    prefix = resource(customer_id, kind, "")
    if not isinstance(value, str) or not value.startswith(prefix):
        _unverified()
    return numeric_id(value[len(prefix):], "provider resource ID")


def url_list(values, name):
    """Apply local bounds without changing valid URL spelling or order."""
    if not isinstance(values, list) or len(values) > 10:
        raise ToolError("INVALID_URL", f"{name} requires a list of at most 10 unique URLs")
    for value in values:
        valid = False
        if isinstance(value, str) and 0 < len(value) <= 2048:
            try:
                parsed = urlsplit(value)
                valid = (
                    parsed.scheme.lower() in {"http", "https"} and bool(parsed.hostname)
                    and "@" not in parsed.netloc
                    and not any(char.isspace() or ord(char) < 32 or 127 <= ord(char) <= 159
                                for char in value)
                )
                parsed.port
                if parsed.netloc.endswith(":"):
                    valid = False
            except ValueError:
                valid = False
        if not valid:
            raise ToolError(
                "INVALID_URL", f"{name} requires HTTP(S) URLs with a host and valid port, "
                "without user information, whitespace or controls; each URL has a local "
                "limit of 2048 Unicode codepoints",
            )
    if len(set(values)) != len(values):
        raise ToolError("INVALID_URL", f"{name} must not contain duplicate URLs")
    return list(values)


def _unique(ctx, query, customer_id):
    rows = ctx.retry_account_read(
        lambda: list(islice(ctx.search_iter(query, customer_id), 2)), customer_id,
    )
    if len(rows) != 1:
        _unverified()
    return rows[0]


def _parents(ctx, customer_id, ad_group_id):
    group = _unique(ctx, f"SELECT {GROUP_FIELDS} FROM ad_group "
                    f"WHERE ad_group.id = {ad_group_id} LIMIT 2", customer_id).ad_group
    campaign_id = _resource_id(group.campaign, customer_id, "campaigns")
    group_status, group_type = _enum(group, "status"), _enum(group, "type_")
    if (str(group.id) != ad_group_id
            or group.resource_name != resource(customer_id, "adGroups", ad_group_id)
            or group_status not in LIVE_STATUSES or group_type != "SEARCH_STANDARD"):
        _unverified()
    campaign = _unique(ctx, f"SELECT {CAMPAIGN_FIELDS} FROM campaign "
                       f"WHERE campaign.id = {campaign_id} LIMIT 2", customer_id).campaign
    campaign_status = _enum(campaign, "status")
    channel = _enum(campaign, "advertising_channel_type")
    if (str(campaign.id) != campaign_id
            or campaign.resource_name != resource(customer_id, "campaigns", campaign_id)
            or campaign_status not in LIVE_STATUSES or channel != "SEARCH"):
        _unverified()
    return {
        "campaign": {"campaign_id": campaign_id, "resource_name": campaign.resource_name,
                     "status": campaign_status, "advertising_channel_type": channel},
        "ad_group": {"ad_group_id": ad_group_id, "resource_name": group.resource_name,
                     "campaign_id": campaign_id, "status": group_status, "type": group_type},
    }


def _url_state(ad):
    parameters, keys = [], set()
    for parameter in ad.url_custom_parameters:
        if not parameter.key or parameter.key in keys:
            _unverified()
        keys.add(parameter.key)
        parameters.append({"key": parameter.key, "value": parameter.value})
    return {
        **{field: url_list(list(getattr(ad, field)), field) for field in URL_FIELDS},
        "tracking_url_template": ad.tracking_url_template,
        "final_url_suffix": ad.final_url_suffix, "url_custom_parameters": parameters,
    }


def _creative(ad):
    creative = ad.responsive_search_ad
    result = {"path1": creative.path1, "path2": creative.path2}
    for field in ("headlines", "descriptions"):
        result[field] = []
        for asset in getattr(creative, field):
            if not asset.text.strip():
                _unverified()
            result[field].append({"text": asset.text, "pinned_field": _enum(asset, "pinned_field")})
        if not result[field]:
            _unverified()
    return result


def _ad_state(ctx, customer_id, ad_group_id, ad_id):
    try:
        association = _unique(
            ctx, f"SELECT {AD_FIELDS} FROM ad_group_ad "
            f"WHERE ad_group_ad.ad.id = {ad_id} AND ad_group_ad.status != 'REMOVED' LIMIT 2",
            customer_id,
        ).ad_group_ad
        ad = association.ad
        status, ad_type = _enum(association, "status"), _enum(ad, "type_")
        if (association.resource_name != resource(customer_id, "adGroupAds", ad_group_id + "~" + ad_id)
                or association.ad_group != resource(customer_id, "adGroups", ad_group_id)
                or ad.resource_name != resource(customer_id, "ads", ad_id)
                or str(ad.id) != ad_id or status not in LIVE_STATUSES
                or ad_type != "RESPONSIVE_SEARCH_AD"):
            _unverified()
        urls = _url_state(ad)
        if not urls["final_urls"]:
            _unverified()
        state = {
            "customer_id": customer_id, **_parents(ctx, customer_id, ad_group_id),
            "ad": {"ad_id": ad_id, "resource_name": ad.resource_name,
                   "association_resource_name": association.resource_name,
                   "status": status, "type": ad_type, **urls,
                   "responsive_search_ad": _creative(ad)},
        }
        _, _, truncated = bounded_rows([state])
        if truncated:
            _unverified()
        return state
    except Exception as exc:
        error = classify_exception(exc, scrub=ctx.scrub)
        if error.code.startswith("AUTH_") or error.code == "ACCOUNT_NOT_ACCESSIBLE":
            raise error from None
        _unverified()


def get_responsive_search_ad_urls(ctx, *, ad_group_id, ad_id, customer_id=None):
    ad_group_id = numeric_id(ad_group_id, "ad_group_id")
    ad_id = numeric_id(ad_id, "ad_id")
    customer_id = ctx.resolve_customer(customer_id)
    try:
        return _ad_state(ctx, customer_id, ad_group_id, ad_id)
    except ToolError as exc:
        if exc.code == "SEARCH_URL_STATE_UNVERIFIED":
            ctx.observe_audit({
                "event": "refused", "tool": "get_responsive_search_ad_urls",
                "customer_id": customer_id, "outcome": exc.code, "message": exc.message,
            })
        raise


def _keyword_state(ctx, customer_id, ad_group_id, criterion_id):
    try:
        identity = resource(customer_id, "adGroupCriteria", ad_group_id + "~" + criterion_id)
        criterion = _unique(
            ctx, f"SELECT {KEYWORD_FIELDS} FROM ad_group_criterion "
            f"WHERE ad_group_criterion.resource_name = '{identity}' LIMIT 2", customer_id,
        ).ad_group_criterion
        status, kind = _enum(criterion, "status"), _enum(criterion, "type_")
        match_type = _enum(criterion.keyword, "match_type")
        if (criterion.resource_name != identity or str(criterion.criterion_id) != criterion_id
                or criterion.ad_group != resource(customer_id, "adGroups", ad_group_id)
                or status not in LIVE_STATUSES or kind != "KEYWORD" or criterion.negative
                or match_type not in {"EXACT", "PHRASE", "BROAD"}
                or not criterion.keyword.text.strip()):
            _unverified()
        state = {
            "customer_id": customer_id, **_parents(ctx, customer_id, ad_group_id),
            "keyword": {
                "criterion_id": criterion_id, "resource_name": identity,
                "status": status, "type": kind, "negative": False,
                "text": criterion.keyword.text, "match_type": match_type,
                **_url_state(criterion),
            },
            "destination_note": "Empty keyword final URLs use the ad destination. "
                                "Direct settings do not resolve the served URL or inherited tracking.",
        }
        _, _, truncated = bounded_rows([state])
        if truncated:
            _unverified()
        return state
    except Exception as exc:
        error = classify_exception(exc, scrub=ctx.scrub)
        if error.code.startswith("AUTH_") or error.code == "ACCOUNT_NOT_ACCESSIBLE":
            raise error from None
        _unverified()


def get_keyword_urls(ctx, *, ad_group_id, criterion_id, customer_id=None):
    ad_group_id = numeric_id(ad_group_id, "ad_group_id")
    criterion_id = numeric_id(criterion_id, "criterion_id")
    customer_id = ctx.resolve_customer(customer_id)
    try:
        return _keyword_state(ctx, customer_id, ad_group_id, criterion_id)
    except ToolError as exc:
        if exc.code == "SEARCH_URL_STATE_UNVERIFIED":
            ctx.observe_audit({
                "event": "refused", "tool": "get_keyword_urls",
                "customer_id": customer_id, "outcome": exc.code, "message": exc.message,
            })
        raise


def keyword_url_plan(ctx, *, ad_group_id, criterion_id, final_urls=None, final_mobile_urls=None):
    ad_group_id = numeric_id(ad_group_id, "ad_group_id")
    criterion_id = numeric_id(criterion_id, "criterion_id")
    supplied = {key: url_list(value, key) for key, value in
                (("final_urls", final_urls), ("final_mobile_urls", final_mobile_urls))
                if value is not None}
    if not supplied:
        raise ToolError("INVALID_URL", "Supply at least one destination URL list")
    if supplied.get("final_urls") == [] and supplied.get("final_mobile_urls"):
        raise ToolError("KEYWORD_URL_DEPENDENCY", "Clear mobile URLs when clearing final URLs")
    customer_id = ctx.config.customer_id
    state = _keyword_state(ctx, customer_id, ad_group_id, criterion_id)
    keyword = state["keyword"]
    before = {field: keyword[field] for field in URL_FIELDS}
    after = {**before, **supplied}
    if not after["final_urls"]:
        if after["final_mobile_urls"]:
            raise ToolError("KEYWORD_URL_DEPENDENCY", "Mobile URLs require final URLs; "
                            "supply final URLs or clear mobile URLs")
        if supplied.get("final_urls") == [] and (
                keyword["tracking_url_template"] or keyword["url_custom_parameters"]):
            raise ToolError("KEYWORD_URL_DEPENDENCY", "Cannot clear final URLs while a tracking "
                            "template or custom parameters remain; remove those settings "
                            "separately before clearing the destination")
    mask = [field for field in URL_FIELDS if field in supplied and before[field] != after[field]]
    if not mask:
        raise ToolError("NO_CHANGES", "The resulting URLs are unchanged; no plan was created")
    fingerprint = hashlib.sha256(json.dumps(state, sort_keys=True, ensure_ascii=False,
                                           separators=(",", ":")).encode("utf-8")).hexdigest()

    def recheck(current):
        try:
            fresh = _keyword_state(current, customer_id, ad_group_id, criterion_id)
        except ToolError:
            raise ToolError("STALE_PLAN", "Keyword destination state is no longer verified; "
                            "inspect and stage a fresh plan") from None
        if fresh != state:
            raise ToolError("STALE_PLAN", "Keyword destination or parent state changed; "
                            "inspect and stage a fresh plan")

    def execute(current):
        client = current.client()
        operation = client.get_type("AdGroupCriterionOperation")
        operation.update.resource_name = keyword["resource_name"]
        for field in mask:
            getattr(operation.update, field).extend(after[field])
        operation.update_mask = FieldMask(paths=mask)
        return executors._send(current, client, "AdGroupCriterionService", "mutate_ad_group_criteria",
                               "MutateAdGroupCriteriaRequest", [operation])

    return {
        "tool": "update_keyword_urls",
        "summary": f"Update destination overrides on keyword {criterion_id}. "
                   "Keyword text, match type, bids, status and tracking are preserved. "
                   "Provider validation and policy review still apply; delivery may change.",
        "operations": [{"type": "update", "resource_name": keyword["resource_name"],
                        "before": before, "after": after, "update_mask": mask,
                        "preserved_state": state, "state_fingerprint": fingerprint}],
        "execute": execute, "rechecks": [recheck],
    }


def ad_url_plan(ctx, *, ad_group_id, ad_id, final_urls=None, final_mobile_urls=None):
    ad_group_id = numeric_id(ad_group_id, "ad_group_id")
    ad_id = numeric_id(ad_id, "ad_id")
    supplied = {key: url_list(value, key) for key, value in
                (("final_urls", final_urls), ("final_mobile_urls", final_mobile_urls))
                if value is not None}
    if not supplied or supplied.get("final_urls") == []:
        raise ToolError("INVALID_URL", "Supply at least one URL list; an ad must retain final URLs")
    customer_id = ctx.config.customer_id
    state = _ad_state(ctx, customer_id, ad_group_id, ad_id)
    before = {field: state["ad"][field] for field in URL_FIELDS}
    after = {**before, **supplied}
    mask = [field for field in URL_FIELDS if field in supplied and before[field] != after[field]]
    if not mask:
        raise ToolError("NO_CHANGES", "The resulting URLs are unchanged; no plan was created")
    fingerprint = hashlib.sha256(json.dumps(state, sort_keys=True, ensure_ascii=False,
                                           separators=(",", ":")).encode("utf-8")).hexdigest()

    def recheck(current):
        try:
            fresh = _ad_state(current, customer_id, ad_group_id, ad_id)
        except ToolError:
            raise ToolError("STALE_PLAN", "Search destination state is no longer verified; "
                            "inspect and stage a fresh plan") from None
        if fresh != state:
            raise ToolError("STALE_PLAN", "Search destination or parent state changed; "
                            "inspect and stage a fresh plan")

    def execute(current):
        client = current.client()
        operation = client.get_type("AdOperation")
        operation.update.resource_name = state["ad"]["resource_name"]
        for field in mask:
            getattr(operation.update, field).extend(after[field])
        operation.update_mask = FieldMask(paths=mask)
        return executors._send(current, client, "AdService", "mutate_ads",
                               "MutateAdsRequest", [operation])

    return {
        "tool": "update_responsive_search_ad_urls",
        "summary": f"Update destinations on responsive search ad {ad_id}. "
                   "Creative, tracking and status are preserved. Provider validation and "
                   "policy review still apply; delivery may change.",
        "operations": [{"type": "update", "resource_name": state["ad"]["resource_name"],
                        "before": before, "after": after, "update_mask": mask,
                        "preserved_state": state, "state_fingerprint": fingerprint}],
        "execute": execute, "rechecks": [recheck],
    }
