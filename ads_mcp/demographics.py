"""Inspect explicit demographics and stage conservative ad-group changes."""

from __future__ import annotations

import hashlib
import json
from itertools import islice

from google.protobuf.field_mask_pb2 import FieldMask

from ads_mcp import executors
from ads_mcp.errors import ToolError, classify_exception
from ads_mcp.search_urls import numeric_id, resource
from ads_mcp.shared_negatives import customer


CATEGORIES = {
    "AGE_RANGE": ["AGE_RANGE_18_24", "AGE_RANGE_25_34", "AGE_RANGE_35_44",
                  "AGE_RANGE_45_54", "AGE_RANGE_55_64", "AGE_RANGE_65_UP",
                  "AGE_RANGE_UNDETERMINED"],
    "GENDER": ["MALE", "FEMALE", "UNDETERMINED"],
    "INCOME_RANGE": ["INCOME_RANGE_0_50", "INCOME_RANGE_50_60", "INCOME_RANGE_60_70",
                     "INCOME_RANGE_70_80", "INCOME_RANGE_80_90", "INCOME_RANGE_90_UP",
                     "INCOME_RANGE_UNDETERMINED"],
    "PARENTAL_STATUS": ["PARENT", "NOT_A_PARENT", "UNDETERMINED"],
}
NUMERIC_FIELDS = ("bid_modifier", "cpc_bid_micros", "cpm_bid_micros",
                  "cpv_bid_micros", "percent_cpc_bid_micros")
STRING_FIELDS = ("final_url_suffix", "tracking_url_template")
LIST_FIELDS = ("final_urls", "final_mobile_urls", "labels")
CUSTOM_FIELDS = (*NUMERIC_FIELDS, *STRING_FIELDS, *LIST_FIELDS, "url_custom_parameters")
CRITERION_FIELDS = ("criterion_id", "resource_name", "type", "status", "negative",
                    *(dimension.lower() + ".type" for dimension in CATEGORIES))
STATE_BYTES = 16 * 1024 * 1024


def _invalid(message):
    raise ToolError("INVALID_DEMOGRAPHIC_ARGUMENT", message)


def _unverified():
    raise ToolError("DEMOGRAPHIC_STATE_UNVERIFIED",
                    "Could not verify complete supported demographic and parent state. "
                    "Inspect the account, campaign and ad group before staging a fresh plan.")


def _enum(message, field):
    raw = message._pb
    value = raw.DESCRIPTOR.fields_by_name[field].enum_type.values_by_number.get(
        getattr(raw, field))
    if value is None or value.name == "UNKNOWN":
        _unverified()
    return value.name


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _scan(ctx, cid, kind, fields, where, limit, project):
    selected = ", ".join(kind + "." + field for field in fields)
    query = f"SELECT {selected} FROM {kind} WHERE {where} LIMIT {limit + 1}"

    def read():
        result, identities, size = [], set(), 2
        for row in islice(ctx.search_iter(query, cid), limit + 1):
            if len(result) == limit:
                _unverified()
            item = project(getattr(row, kind))
            if item["resource_name"] in identities:
                _unverified()
            identities.add(item["resource_name"])
            size += len(_json(item).encode("utf-8")) + 1
            if size > STATE_BYTES:
                _unverified()
            result.append(item)
        return sorted(result, key=lambda item: item["resource_name"])

    return ctx.retry_account_read(read, cid)


def _parents(ctx, cid, ident):
    def group(message):
        prefix = resource(cid, "campaigns", "")
        if not message.campaign.startswith(prefix):
            _unverified()
        campaign_id = numeric_id(message.campaign[len(prefix):], "provider campaign ID")
        status, kind = _enum(message, "status"), _enum(message, "type_")
        if (str(message.id) != ident or message.resource_name != resource(cid, "adGroups", ident)
                or status not in {"ENABLED", "PAUSED"}
                or kind not in {"SEARCH_STANDARD", "DISPLAY_STANDARD"}):
            _unverified()
        restrictions = []
        for restriction in message.targeting_setting.target_restrictions:
            dimension = _enum(restriction, "targeting_dimension")
            if dimension == "UNSPECIFIED" or any(
                    item["targeting_dimension"] == dimension for item in restrictions):
                _unverified()
            restrictions.append({"targeting_dimension": dimension, "bid_only": restriction.bid_only})
        return {"ad_group_id": ident, "resource_name": message.resource_name,
                "name": message.name, "campaign_id": campaign_id, "status": status, "type": kind,
                "optimized_targeting_enabled": message.optimized_targeting_enabled,
                "target_restrictions": sorted(restrictions, key=lambda item: item["targeting_dimension"])}

    groups = _scan(ctx, cid, "ad_group", (
        "id", "resource_name", "name", "campaign", "status", "type",
        "optimized_targeting_enabled", "targeting_setting.target_restrictions"),
        f"ad_group.id = {ident}", 1, group)
    if len(groups) != 1:
        _unverified()
    parent = groups[0]["campaign_id"]

    def campaign(message):
        status = _enum(message, "status")
        channel = _enum(message, "advertising_channel_type")
        subtype = _enum(message, "advertising_channel_sub_type")
        if (str(message.id) != parent or message.resource_name != resource(cid, "campaigns", parent)
                or status not in {"ENABLED", "PAUSED"} or channel not in {"SEARCH", "DISPLAY"}
                or subtype != "UNSPECIFIED" or groups[0]["type"] != channel + "_STANDARD"):
            _unverified()
        return {"campaign_id": parent, "resource_name": message.resource_name, "name": message.name,
                "status": status, "advertising_channel_type": channel,
                "advertising_channel_sub_type": subtype}

    campaigns = _scan(ctx, cid, "campaign", (
        "id", "resource_name", "name", "status", "advertising_channel_type",
        "advertising_channel_sub_type"), f"campaign.id = {parent}", 1, campaign)
    if len(campaigns) != 1:
        _unverified()
    return {"campaign": campaigns[0], "ad_group": groups[0]}


def _criteria(ctx, cid, level, ident, supported):
    kind = level + "_criterion"
    path, owner = (("adGroupCriteria", "adGroups") if level == "ad_group"
                   else ("campaignCriteria", "campaigns"))
    parent = resource(cid, owner, ident)

    def project(message):
        child = numeric_id(str(message.criterion_id), "provider criterion ID")
        dimension, status = _enum(message, "type_"), _enum(message, "status")
        if (message.resource_name != resource(cid, path, ident + "~" + child)
                or getattr(message, level) != parent or dimension not in supported
                or status not in {"ENABLED", "PAUSED"}
                or message._pb.WhichOneof("criterion") != dimension.lower()):
            _unverified()
        value = _enum(getattr(message, dimension.lower()), "type_")
        if value not in CATEGORIES[dimension]:
            _unverified()
        result = {"criterion_id": child, "resource_name": message.resource_name,
                  "dimension": dimension, "value": value, "status": status,
                  "negative": message.negative}
        if level == "ad_group":
            result.update({field: getattr(message, field) if message._pb.HasField(field) else None
                           for field in NUMERIC_FIELDS})
            result.update({field: getattr(message, field) for field in STRING_FIELDS})
            result.update({field: sorted(getattr(message, field)) for field in LIST_FIELDS})
            parameters = [{"key": item.key, "value": item.value}
                          for item in message.url_custom_parameters]
            if (any(not item["key"] for item in parameters)
                    or len({item["key"] for item in parameters}) != len(parameters)):
                _unverified()
            result["url_custom_parameters"] = sorted(parameters, key=lambda item: item["key"])
            if any(not label.startswith(resource(cid, "labels", "")) for label in message.labels):
                _unverified()
            for label in message.labels:
                numeric_id(label.rsplit("/", 1)[-1], "provider label ID")
        return result

    fields = (*CRITERION_FIELDS, level, *(CUSTOM_FIELDS if level == "ad_group" else ()))
    dimensions = ", ".join("'" + item + "'" for item in CATEGORIES)
    rows = _scan(ctx, cid, kind, fields,
                 f"{kind}.{level} = '{parent}' AND {kind}.status != 'REMOVED' "
                 f"AND {kind}.type IN ({dimensions})", 100, project)
    if len({(item["dimension"], item["value"]) for item in rows}) != len(rows):
        _unverified()
    return rows


def _state(ctx, cid, ident):
    try:
        parents = _parents(ctx, cid, ident)
        supported = {key: list(values) for key, values in CATEGORIES.items()
                     if parents["campaign"]["advertising_channel_type"] == "DISPLAY"
                     or key != "PARENTAL_STATUS"}
        state = {"customer_id": cid, **parents,
                 "criteria": _criteria(ctx, cid, "ad_group", ident, supported),
                 "campaign_exclusions": _criteria(
                     ctx, cid, "campaign", parents["campaign"]["campaign_id"], supported),
                 "supported_values": supported,
                 "eligibility_note": "Absent or removed criteria are unconfigured defaults; "
                                     "explicit criteria do not establish effective eligibility."}
        if len(_json(state).encode("utf-8")) > STATE_BYTES:
            _unverified()
        return state
    except Exception as exc:
        error = classify_exception(exc, scrub=ctx.scrub)
        if error.code.startswith("AUTH_") or error.code == "ACCOUNT_NOT_ACCESSIBLE":
            raise error from None
        _unverified()


def inspect(ctx, *, ad_group_id, customer_id=None):
    ident = numeric_id(ad_group_id, "ad_group_id")
    cid = customer(ctx, customer_id)
    return _state(ctx, cid, ident)


def _check_exclusions(dimension, excluded):
    if not set(CATEGORIES[dimension][:-1]) - excluded:
        _invalid("The proposed exclusions must leave at least one known category in each "
                 "requested dimension. All-excluded and undetermined-only sets are unsupported.")


def _changes(values):
    if not isinstance(values, list) or not 1 <= len(values) <= 20:
        _invalid("changes requires 1 to 20 exact dimension/value/action records")
    keys = set()
    for item in values:
        if (not isinstance(item, dict) or set(item) != {"dimension", "value", "action"}
                or any(not isinstance(value, str) for value in item.values())):
            _invalid("Each change requires exactly dimension, value and action strings")
        dimension, value = item["dimension"], item["value"]
        if (dimension not in CATEGORIES or value not in CATEGORIES[dimension]
                or item["action"] not in {"INCLUDE", "EXCLUDE"}):
            _invalid("Use a supported demographic category and INCLUDE or EXCLUDE")
        key = dimension, value
        if key in keys:
            _invalid("Each dimension/value pair must appear once")
        keys.add(key)
    for dimension in {item["dimension"] for item in values}:
        _check_exclusions(dimension, {item["value"] for item in values
                                     if item["dimension"] == dimension and item["action"] == "EXCLUDE"})
    return [dict(item) for item in values]


def _propose(state, changes):
    active = {(item["dimension"], item["value"]): item for item in state["criteria"]}
    campaign_exclusions = {(item["dimension"], item["value"]) for item in state["campaign_exclusions"]
                           if item["negative"] and item["status"] == "ENABLED"}
    exclusions = {key for key, item in active.items() if item["negative"] and item["status"] == "ENABLED"}
    operations = []
    for change in changes:
        dimension, value = change["dimension"], change["value"]
        if dimension not in state["supported_values"]:
            _invalid("Parental status requires a supported Display ad group")
        key = dimension, value
        negative = change["action"] == "EXCLUDE"
        if not negative and key in campaign_exclusions:
            _invalid("An active campaign demographic exclusion blocks INCLUDE. "
                     "Inspect and resolve that campaign exclusion separately.")
        if negative:
            exclusions.add(key)
        else:
            exclusions.discard(key)
        before = active.get(key)
        after = {"ad_group": state["ad_group"]["resource_name"], "dimension": dimension,
                 "value": value, "negative": negative, "status": "ENABLED"}
        if before and before["negative"] == negative:
            if before["status"] == "ENABLED":
                continue
            operations.append({"type": "update", "resource_name": before["resource_name"],
                               "before": before, "after": {**before, "status": "ENABLED"}})
        else:
            if before:
                if (any(before[field] is not None for field in NUMERIC_FIELDS)
                        or any(before[field] for field in (*STRING_FIELDS, *LIST_FIELDS, "url_custom_parameters"))):
                    _invalid("Replacement would discard direct criterion customization. "
                             "Inspect and resolve the existing settings separately.")
                operations.append({"type": "remove", "resource_name": before["resource_name"],
                                   "before": before, "after": None})
            operations.append({"type": "create", "before": None,
                               "after": {**after, "resource_name": None},
                               "identity_note": "The provider assigns a new criterion identity at creation."})
    for dimension in {item["dimension"] for item in changes}:
        _check_exclusions(dimension, {value for dim, value in exclusions | campaign_exclusions
                                     if dim == dimension})
    if not operations:
        raise ToolError("NO_CHANGES", "The requested demographic state is unchanged")
    return sorted(operations, key=lambda operation: operation["type"] != "remove")


def plan(ctx, *, ad_group_id, changes, customer_id=None):
    ident = numeric_id(ad_group_id, "ad_group_id")
    cid = customer(ctx, customer_id, write=True)
    changes = _changes(changes)
    state = _state(ctx, cid, ident)
    operations = _propose(state, changes)

    def recheck(current):
        try:
            fresh = _state(current, cid, ident)
            proposed = _propose(fresh, changes)
        except ToolError:
            raise ToolError("STALE_PLAN", "Demographic state is no longer verified; stage a fresh plan") from None
        if fresh != state or proposed != operations:
            raise ToolError("STALE_PLAN", "Demographic or parent state changed; stage a fresh plan")

    def execute(current):
        client = current.client()
        request_operations = []
        for change in operations:
            operation = client.get_type("AdGroupCriterionOperation")
            if change["type"] == "remove":
                operation.remove = change["resource_name"]
            elif change["type"] == "update":
                operation.update.resource_name = change["resource_name"]
                operation.update.status = "ENABLED"
                operation.update_mask = FieldMask(paths=["status"])
            else:
                after = change["after"]
                operation.create.ad_group = after["ad_group"]
                operation.create.negative = after["negative"]
                operation.create.status = "ENABLED"
                getattr(operation.create, after["dimension"].lower()).type_ = after["value"]
            request_operations.append(operation)
        return executors._send(current, client, "AdGroupCriterionService", "mutate_ad_group_criteria",
                               "MutateAdGroupCriteriaRequest", request_operations)

    return {"tool": "update_demographic_targeting",
            "summary": "Change explicit ad-group demographics in one atomic service request. "
                       "Removing and recreating a criterion assigns a new identity. "
                       "Explicit criteria do not prove effective eligibility or reach. "
                       "Income geography, policy limits and provider validation still apply; "
                       "a recheck cannot prevent external races.",
            "operations": [{"changes": operations, "before": state,
                            "state_fingerprint": hashlib.sha256(_json(state).encode("utf-8")).hexdigest()}],
            "irreversible": any(item["type"] == "remove" for item in operations),
            "rechecks": [recheck], "execute": execute}
