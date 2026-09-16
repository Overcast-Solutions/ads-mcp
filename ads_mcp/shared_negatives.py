"""Complete shared negative-keyword state and atomic, reviewed changes."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from itertools import islice

from ads_mcp import executors
from ads_mcp.errors import ToolError, classify_exception
from ads_mcp.search_urls import numeric_id, resource


LIST_LIMIT = 100
MEMBER_LIMIT = 5000
LINK_LIMIT = 1000
STATE_BYTES = 16 * 1024 * 1024
MATCHES = {"BROAD", "PHRASE", "EXACT"}
SET_FIELDS = "id, resource_name, name, type, status, member_count, reference_count"
MEMBER_FIELDS = "criterion_id, resource_name, shared_set, type, negative, keyword.text, keyword.match_type"
LINK_FIELDS = "resource_name, campaign, shared_set, status"
CAMPAIGN_FIELDS = "id, resource_name, name, status, advertising_channel_type, advertising_channel_sub_type"


def _invalid(message):
    raise ToolError("INVALID_SHARED_NEGATIVE_ARGUMENT", message)


def _unverified():
    raise ToolError(
        "SHARED_NEGATIVE_STATE_UNVERIFIED",
        "Could not verify complete shared negative list and campaign state. "
        "Inspect the account and supported list before staging a fresh plan.",
    )


def customer(ctx, value, *, write=False):
    if value is not None and (
        not isinstance(value, str)
        or re.fullmatch(r"(?:[0-9]{10}|[0-9]{3}-[0-9]{3}-[0-9]{4})", value) is None
    ):
        _invalid("customer_id requires a ten-digit account string or null")
    cid = ctx.resolve_customer(value)
    if write and cid != ctx.config.customer_id:
        raise ToolError("CUSTOMER_MISMATCH", "Mutations require the configured customer account")
    return cid


def _text(value):
    if (not isinstance(value, str) or not value or value != value.strip()
            or any(unicodedata.category(char) in {"Cc", "Cs"} for char in value)):
        _invalid("Text must be nonblank without edge whitespace, controls or surrogates")
    return value


def _comparison(value):
    return unicodedata.normalize("NFC", value).casefold()


def name_value(value):
    _text(value)
    if unicodedata.normalize("NFC", value) != value or len(value.encode("utf-8")) > 255:
        _invalid("List names require original NFC text of 1 to 255 UTF-8 bytes")
    return value


def _keyword(value):
    if not isinstance(value, dict) or set(value) != {"text", "match_type"}:
        _invalid("Each keyword requires exactly text and match_type")
    text = _text(value["text"])
    if len(text) > 80 or len(text.split()) > 10:
        _invalid("Keyword text has local limits of 80 codepoints and 10 words")
    match = value["match_type"]
    if not isinstance(match, str) or match not in MATCHES:
        _invalid("match_type requires BROAD, PHRASE or EXACT")
    return {"text": text, "match_type": match}


def _keyword_key(value):
    return _comparison(value["text"]), value["match_type"]


def _batch(values):
    if not isinstance(values, list) or not 1 <= len(values) <= 100:
        _invalid("A batch requires 1 to 100 entries")


def keywords_value(values):
    _batch(values)
    result = [_keyword(value) for value in values]
    if len({_keyword_key(value) for value in result}) != len(result):
        _invalid("Duplicate keywords are not allowed")
    return result


def ids_value(values):
    _batch(values)
    result = [numeric_id(value, "batch ID") for value in values]
    if len(set(result)) != len(result):
        _invalid("Duplicate IDs are not allowed")
    return result


def _enum(message, field):
    raw = message._pb
    descriptor = raw.DESCRIPTOR.fields_by_name[field].enum_type
    value = descriptor.values_by_number.get(getattr(raw, field))
    if value is None or value.name == "UNKNOWN":
        _unverified()
    return value.name


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _bounded(value):
    if len(_json(value).encode("utf-8")) > STATE_BYTES:
        _unverified()
    return value


def _scan(ctx, cid, kind, fields, where, limit, project):
    selected = ", ".join(kind + "." + field for field in fields.split(", "))
    query = f"SELECT {selected} FROM {kind} WHERE {where} LIMIT {limit + 1}"

    def read():
        result, identities, size = [], set(), 2
        for row in islice(ctx.search_iter(query, cid), limit + 1):
            if len(result) == limit:
                _unverified()
            item = project(getattr(row, kind))
            identity = item["resource_name"]
            if identity in identities:
                _unverified()
            identities.add(identity)
            size += len(_json(item).encode("utf-8")) + 1
            if size > STATE_BYTES:
                _unverified()
            result.append(item)
        return sorted(result, key=lambda item: item["resource_name"])

    return ctx.retry_account_read(read, cid)


def _set(message, cid):
    ident = numeric_id(str(message.id), "provider list ID")
    status, kind = _enum(message, "status"), _enum(message, "type_")
    _text(message.name)
    if (message.resource_name != resource(cid, "sharedSets", ident)
            or kind != "NEGATIVE_KEYWORDS" or status != "ENABLED"
            or not 0 <= message.member_count <= MEMBER_LIMIT
            or not 0 <= message.reference_count <= LINK_LIMIT):
        _unverified()
    return {"shared_set_id": ident, "resource_name": message.resource_name,
            "name": message.name, "type": kind, "status": status,
            "member_count": message.member_count, "reference_count": message.reference_count}


def _catalog(ctx, cid):
    return {"customer_id": cid, "lists": _scan(
        ctx, cid, "shared_set", SET_FIELDS,
        "shared_set.type = 'NEGATIVE_KEYWORDS' AND shared_set.status != 'REMOVED'",
        LIST_LIMIT, lambda message: _set(message, cid),
    )}


def _campaigns(ctx, cid, ids):
    if not ids:
        return []

    def project(message):
        ident = numeric_id(str(message.id), "provider campaign ID")
        status = _enum(message, "status")
        channel = _enum(message, "advertising_channel_type")
        subtype = _enum(message, "advertising_channel_sub_type")
        if (ident not in ids or message.resource_name != resource(cid, "campaigns", ident)
                or status not in {"ENABLED", "PAUSED"} or channel not in {"SEARCH", "SHOPPING"}
                or subtype != "UNSPECIFIED"):
            _unverified()
        _text(message.name)
        return {"campaign_id": ident, "resource_name": message.resource_name,
                "name": message.name, "status": status, "advertising_channel_type": channel,
                "advertising_channel_sub_type": subtype}

    result = _scan(ctx, cid, "campaign", CAMPAIGN_FIELDS,
                   "campaign.id IN (" + ", ".join(sorted(ids)) + ")", len(ids), project)
    if len(result) != len(ids):
        _unverified()
    return result


def _detail(ctx, cid, ident):
    lists = _scan(ctx, cid, "shared_set", SET_FIELDS, f"shared_set.id = {ident}", 1,
                  lambda message: _set(message, cid))
    if len(lists) != 1 or lists[0]["shared_set_id"] != ident:
        _unverified()
    shared = lists[0]
    identity = shared["resource_name"]

    def project_member(message):
        child = numeric_id(str(message.criterion_id), "provider criterion ID")
        kind = _enum(message, "type_")
        if (message.shared_set != identity
                or message.resource_name != resource(cid, "sharedCriteria", ident + "~" + child)
                or kind != "KEYWORD" or message._pb.WhichOneof("criterion") != "keyword"):
            _unverified()
        keyword = _keyword({"text": message.keyword.text,
                            "match_type": _enum(message.keyword, "match_type")})
        return {"criterion_id": child, "resource_name": message.resource_name,
                "shared_set": identity, "type": kind, "negative": message.negative, **keyword}

    members = _scan(ctx, cid, "shared_criterion", MEMBER_FIELDS,
                    f"shared_criterion.shared_set = '{identity}'", MEMBER_LIMIT, project_member)
    if len({_keyword_key(item) for item in members}) != len(members):
        _unverified()

    def project_link(message):
        prefix = resource(cid, "campaigns", "")
        if not message.campaign.startswith(prefix):
            _unverified()
        campaign_id = numeric_id(message.campaign[len(prefix):], "provider campaign ID")
        status = _enum(message, "status")
        if (message.shared_set != identity or status != "ENABLED"
                or message.resource_name != resource(cid, "campaignSharedSets", campaign_id + "~" + ident)):
            _unverified()
        return {"campaign_id": campaign_id, "resource_name": message.resource_name,
                "status": status}

    links = _scan(ctx, cid, "campaign_shared_set", LINK_FIELDS,
                  f"campaign_shared_set.shared_set = '{identity}' "
                  "AND campaign_shared_set.status != 'REMOVED'", LINK_LIMIT, project_link)
    if len(members) != shared["member_count"] or len(links) != shared["reference_count"]:
        _unverified()
    by_id = {link["campaign_id"]: link for link in links}
    campaigns = [{**item, "campaign_shared_set": by_id[item["campaign_id"]]["resource_name"],
                  "link_status": by_id[item["campaign_id"]]["status"]}
                 for item in _campaigns(ctx, cid, set(by_id))]
    return _bounded({"customer_id": cid, "shared_set": shared,
                     "keywords": members, "campaigns": campaigns})


def _verified(ctx, call):
    try:
        return _bounded(call())
    except Exception as exc:
        error = classify_exception(exc, scrub=ctx.scrub)
        if error.code.startswith("AUTH_") or error.code == "ACCOUNT_NOT_ACCESSIBLE":
            raise error from None
        _unverified()


def inspect(ctx, *, shared_set_id=None, customer_id=None):
    cid = customer(ctx, customer_id)
    if shared_set_id is None:
        return _verified(ctx, lambda: _catalog(ctx, cid))
    ident = numeric_id(shared_set_id, "shared_set_id")
    return _verified(ctx, lambda: _detail(ctx, cid, ident))


def plan(ctx, *, tool, customer_id=None, shared_set_id=None, name=None,
         keywords=None, criterion_ids=None, campaign_ids=None):
    cid = customer(ctx, customer_id, write=True)
    create = tool == "create_shared_negative_keyword_list"
    add = tool == "add_shared_negative_keywords"
    remove = tool == "remove_shared_negative_keywords"
    attach = tool == "attach_shared_negative_keyword_list"
    detach = tool == "detach_shared_negative_keyword_list"
    if create:
        name = name_value(name)
    else:
        shared_set_id = numeric_id(shared_set_id, "shared_set_id")
        if add:
            keywords = keywords_value(keywords)
        elif remove:
            criterion_ids = ids_value(criterion_ids)
        else:
            campaign_ids = ids_value(campaign_ids)

    def read(current):
        if create:
            return _catalog(current, cid)
        state = _detail(current, cid, shared_set_id)
        if attach or detach:
            state["selected_campaigns"] = _campaigns(current, cid, set(campaign_ids))
        return _bounded(state)

    state = _verified(ctx, lambda: read(ctx))
    if create:
        if any(_comparison(item["name"]) == _comparison(name) for item in state["lists"]):
            _invalid("An active negative keyword list already uses this name")
        if len(state["lists"]) >= LIST_LIMIT:
            _invalid("The resulting list catalog exceeds the local limit of 100")
        changes = [{"type": "create", "name": name, "shared_set_type": "NEGATIVE_KEYWORDS"}]
        service, method, request_type, operation_type = (
            "SharedSetService", "mutate_shared_sets", "MutateSharedSetsRequest", "SharedSetOperation")
    else:
        identity = state["shared_set"]["resource_name"]
        if add:
            existing = {_keyword_key(item) for item in state["keywords"]}
            if any(_keyword_key(item) in existing for item in keywords):
                _invalid("A requested keyword is already in the list")
            if len(state["keywords"]) + len(keywords) > MEMBER_LIMIT:
                _invalid("The resulting membership exceeds the local limit of 5000")
            changes = [{"type": "create", "shared_set": identity, "keyword": item} for item in keywords]
        elif remove:
            members = {item["criterion_id"]: item for item in state["keywords"]}
            if not set(criterion_ids) <= members.keys():
                _invalid("Each removal must identify an existing member of this list")
            changes = [{"type": "remove", "resource_name": members[ident]["resource_name"]}
                       for ident in criterion_ids]
        else:
            linked = {item["campaign_id"] for item in state["campaigns"]}
            selected = set(campaign_ids)
            if (attach and linked.intersection(selected)) or (detach and not selected <= linked):
                _invalid("Each requested association must change the current list links")
            if attach and len(linked) + len(selected) > LINK_LIMIT:
                _invalid("The resulting associations exceed the local limit of 1000")
            changes = ([{"type": "create", "campaign": resource(cid, "campaigns", ident),
                         "shared_set": identity} for ident in campaign_ids] if attach else
                       [{"type": "remove", "resource_name": resource(
                           cid, "campaignSharedSets", ident + "~" + shared_set_id)}
                        for ident in campaign_ids])
        service, method, request_type, operation_type = (
            ("SharedCriterionService", "mutate_shared_criteria", "MutateSharedCriteriaRequest",
             "SharedCriterionOperation") if add or remove else
            ("CampaignSharedSetService", "mutate_campaign_shared_sets", "MutateCampaignSharedSetsRequest",
             "CampaignSharedSetOperation"))

    def recheck(current):
        try:
            fresh = _verified(current, lambda: read(current))
        except ToolError:
            raise ToolError("STALE_PLAN", "Shared list state is no longer verifiable; stage a fresh plan") from None
        if fresh != state:
            raise ToolError("STALE_PLAN", "Shared list or affected campaign state changed; stage a fresh plan")

    def execute(current):
        client = current.client()
        operations = []
        for change in changes:
            operation = client.get_type(operation_type)
            if change["type"] == "remove":
                operation.remove = change["resource_name"]
            elif create:
                operation.create.name = name
                operation.create.type_ = "NEGATIVE_KEYWORDS"
            elif add:
                operation.create.shared_set = change["shared_set"]
                operation.create.keyword.text = change["keyword"]["text"]
                operation.create.keyword.match_type = change["keyword"]["match_type"]
            else:
                operation.create.campaign = change["campaign"]
                operation.create.shared_set = change["shared_set"]
            operations.append(operation)
        return executors._send(current, client, service, method, request_type, operations)

    return {
        "tool": tool,
        "summary": "Change a shared negative keyword list in one atomic service request. "
                   "Membership and association changes can change serving across the shown campaigns. "
                   "Provider validation still applies; a recheck does not prevent external races.",
        "operations": [{"changes": changes, "before": state,
                        "state_fingerprint": hashlib.sha256(_json(state).encode("utf-8")).hexdigest()}],
        "irreversible": remove or detach, "rechecks": [recheck], "execute": execute,
    }
