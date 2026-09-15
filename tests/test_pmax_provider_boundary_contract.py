"""PMax provider boundaries through real MCP dispatch and genuine v25 messages.

The adjacent factual fixture comes from official reporting metadata, independent
of product query constants and protobuf descriptors. SDK presence alone cannot
prove GAQL usability or provider-required CREATE fields. All transport is local.
"""
from copy import deepcopy
import json
from pathlib import Path
import re

import pytest

import harness as h
from pmax_oracle import PMAX_ARGS, apply, checked_apply, preview, rn, setup, stage


FACTS = json.loads(Path(__file__).with_name("fixtures").joinpath(
    "pmax_provider_fields_v25.json").read_text())["resources"]
READS = [
    ("get_asset_groups", {"campaign_id": "701"}),
    ("get_asset_group_signals", {"asset_group_id": "801"}),
    ("list_audiences", {}),
    ("get_pmax_url_settings", {"campaign_id": "701"}),
]
WRITES = [*[(name, deepcopy(args)) for name, args in PMAX_ARGS.items()],
          ("pause_entity", {"entity_type": "asset_group", "entity_id": "801"}),
          ("enable_entity", {"entity_type": "asset_group", "entity_id": "801"})]
FIELD = r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+"


def field_uses(query):
    """Inspect every field token in each clause, excluding quoted literals.

    This deliberately fails on unrecognized SELECT/ORDER expressions instead
    of silently omitting them. WHERE operators need not be enumerated to see
    their field operands. The checker does not prescribe whitespace or order.
    """
    clean = re.sub(r"'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\"", "''", query)
    parts = re.fullmatch(
        r"\s*SELECT\s+(?P<select>.+?)\s+FROM\s+(?P<resource>\w+)"
        r"(?:\s+WHERE\s+(?P<where>.+?))?"
        r"(?:\s+ORDER\s+BY\s+(?P<order>.+?))?"
        r"(?:\s+LIMIT\s+\d+)?\s*", clean, re.I | re.S)
    assert parts, f"Unparsed emitted GAQL: {query}"
    uses = []
    for value in parts["select"].split(","):
        value = value.strip()
        assert re.fullmatch(FIELD, value), f"Unparsed SELECT field: {value}"
        uses.append((value, 0))
    if parts["where"]:
        uses.extend((value, 1) for value in re.findall(FIELD, parts["where"]))
    if parts["order"]:
        for value in parts["order"].split(","):
            match = re.fullmatch(rf"\s*({FIELD})(?:\s+(?:ASC|DESC))?\s*", value, re.I)
            assert match, f"Unparsed ORDER BY field: {value}"
            uses.append((match[1], 2))
    return parts["resource"], uses


def assert_provider_queries(searches, record_property=None):
    assert searches, "This boundary must exercise a fresh provider read"
    failures, use_count = [], 0
    for search in searches:
        resource, uses = field_uses(search.query)
        assert resource in FACTS, f"Missing independent metadata for FROM {resource}"
        allowed = {resource, *FACTS[resource]["attributed_resources"]}
        for field, usage in uses:
            use_count += 1
            owner = field.split(".")[0]
            flags = FACTS.get(owner, {}).get("fields", {}).get(field)
            if owner not in allowed or flags is None or not flags[usage]:
                failures.append({"field": field, "usage": ("SELECT", "WHERE", "ORDER BY")[usage],
                                 "from": resource, "query": search.query})
    if record_property:
        record_property("queries", len(searches))
        record_property("field_uses", use_count)
    assert not failures, "Official v25 metadata rejects emitted field uses: " + json.dumps(failures)


@pytest.mark.parametrize("tool,args", READS, ids=[x[0] for x in READS])
@pytest.mark.parametrize("customer", [h.CUSTOMER_ID, h.OTHER_CUSTOMER_ID])
def test_read_queries_use_official_selectable_filterable_sortable_fields(
        tmp_path, tool, args, customer, record_property):
    server, provider = setup(tmp_path, [tool], read_only=True)
    h.expect_ok(h.call(server, tool, {**args, "customer_id": customer}))
    assert not provider.mutations
    assert all(call.customer_id == customer for call in provider.searches)
    assert_provider_queries(provider.searches, record_property)


@pytest.mark.parametrize("tool,args", WRITES, ids=[x[0] for x in WRITES])
@pytest.mark.parametrize("boundary", ["stage", "apply"])
def test_stage_and_apply_queries_use_independent_official_field_facts(
        tmp_path, tool, args, boundary, record_property):
    server, provider = setup(tmp_path, [tool])
    if tool == "enable_entity":
        provider.data[h.CUSTOMER_ID]["asset_group"][0]["asset_group"]["status"] = "PAUSED"
    plan = stage(server, tool, args)
    if boundary == "apply":
        preview(server, plan)
        provider.searches.clear()
        h.expect_ok(apply(server, plan))
        assert len(provider.live_mutations()) == 1
    else:
        assert not provider.mutations
    assert_provider_queries(provider.searches, record_property)


@pytest.mark.parametrize("query,bad_field", [
    ("SELECT campaign_criterion.webpage FROM campaign_criterion", "campaign_criterion.webpage"),
    ("SELECT campaign_criterion.webpage.conditions FROM campaign_criterion "
     "WHERE campaign_criterion.webpage.conditions IS NOT NULL", "campaign_criterion.webpage.conditions"),
    ("SELECT asset_group.primary_status FROM asset_group ORDER BY asset_group.primary_status DESC",
     "asset_group.primary_status"),
])
def test_field_oracle_rejects_nonexistent_nonfilterable_and_nonsortable_fields(query, bad_field):
    from types import SimpleNamespace
    with pytest.raises(AssertionError, match=re.escape(bad_field)):
        assert_provider_queries([SimpleNamespace(query=query)])


def test_field_oracle_accepts_documented_leaves_and_ignores_field_like_literals():
    from types import SimpleNamespace
    query = ("SELECT campaign_criterion.criterion_id, campaign_criterion.webpage.conditions, "
             "campaign_criterion.webpage.criterion_name FROM campaign_criterion "
             "WHERE campaign_criterion.webpage.criterion_name LIKE 'asset_group.no_such_field%' "
             "ORDER BY campaign_criterion.criterion_id DESC LIMIT 2")
    assert_provider_queries([SimpleNamespace(query=query)])


def named_values(value, key):
    if isinstance(value, dict):
        for name, child in value.items():
            if name == key:
                yield child
            else:
                yield from named_values(child, key)
    elif isinstance(value, list):
        for child in value:
            yield from named_values(child, key)


def reviewed_name(plan):
    names = list(named_values(plan["operations"], "criterion_name"))
    assert len(names) == 1, "The reviewed create operation must expose its provider criterion_name"
    assert isinstance(names[0], str) and names[0].strip(), "Reviewed criterion_name must be nonblank"
    return names[0]


@pytest.mark.parametrize("mode,operator,url", [
    ("EXACT", "EQUALS", "https://example.invalid/catalog/summer?edition=one"),
    ("CONTAINS", "CONTAINS", "/catalog/summer/"),
])
def test_exclusion_create_transmits_required_name_and_reviewed_condition(tmp_path, mode, operator, url):
    # https://developers.google.com/google-ads/api/reference/rpc/v25/WebpageInfo#criterion_name
    # Google requires this field on CREATE; protobuf accepts its absence.
    server, provider = setup(tmp_path, ["add_pmax_url_exclusion"])
    plan = stage(server, "add_pmax_url_exclusion", {"campaign_id": "701", "url": url, "match_type": mode})
    request_call = checked_apply(server, provider, plan)
    assert request_call.service == "CampaignCriterionService"
    assert request_call.method == "mutate_campaign_criteria"
    request = request_call.request
    assert request._pb.DESCRIPTOR.full_name == "google.ads.googleads.v25.services.MutateCampaignCriteriaRequest"
    assert request.customer_id == h.CUSTOMER_ID and not request.partial_failure
    assert len(request.operations) == 1
    operation = request.operations[0]
    assert operation._pb.WhichOneof("operation") == "create"
    assert operation.create.campaign == rn("campaigns", "701") and operation.create.negative
    webpage = operation.create.webpage
    assert webpage.criterion_name.strip(), "Provider-required WebpageInfo.criterion_name is missing"
    assert webpage.criterion_name == reviewed_name(plan), "The applied name must be the name reviewed"
    assert url not in webpage.criterion_name, "Generated identifiers must not embed raw input"
    assert len(webpage.conditions) == 1
    condition = webpage.conditions[0]
    expected = {"operand": "URL", "operator": operator, "argument": url}
    actual = {"operand": condition.operand.name, "operator": condition.operator.name, "argument": condition.argument}
    assert actual == expected
    assert [expected] in list(named_values(plan["operations"], "conditions"))


@pytest.mark.parametrize("mode", ["EXACT", "CONTAINS"])
def test_exclusion_names_are_predictable_compact_and_require_no_new_argument(tmp_path, mode):
    server, provider = setup(tmp_path, ["add_pmax_url_exclusion"])
    properties = h.tool_map(server)["add_pmax_url_exclusion"].input_schema["properties"]
    assert set(properties) == {"campaign_id", "url", "match_type", "customer_id"}
    prefix = "https://example.invalid/" if mode == "EXACT" else "/"
    urls = [prefix + "synthetic-section/" * count for count in (32, 64, 96)]
    names = []
    for url in urls:
        args = {"campaign_id": "701", "url": url, "match_type": mode}
        first = reviewed_name(stage(server, "add_pmax_url_exclusion", args))
        equivalent = {**args, "campaign_id": " 000701 "}
        if mode == "EXACT":
            equivalent.pop("match_type")
        second = reviewed_name(stage(server, "add_pmax_url_exclusion", equivalent))
        assert first == second, "Equivalent normalized inputs must have a predictable name"
        assert url not in first
        assert first.isprintable() and first == first.strip()
        names.append(first)
    # These inputs remain below 2K. Compare compact output to realistic input
    # growth without inventing a Google name limit or a hash/prefix/algorithm.
    assert max(map(len, names)) < min(map(len, urls)), "Names must remain compact as URL input grows"
    assert not provider.mutations


@pytest.mark.parametrize("tool", ["add_pmax_url_exclusion", "remove_pmax_url_exclusions"])
@pytest.mark.parametrize("change", ["criterion_name", "operand", "operator", "argument", "extra_condition"])
def test_exclusion_rechecks_preserve_name_and_every_condition_component(tmp_path, tool, change):
    server, provider = setup(tmp_path, [tool, "get_pmax_url_settings"])
    webpage = provider.data[h.CUSTOMER_ID]["campaign_criterion"][1]["campaign_criterion"]["webpage"]
    expected = deepcopy(webpage["conditions"])
    read = h.expect_ok(h.call(server, "get_pmax_url_settings", {"campaign_id": "701"}))
    row = next(row for row in read["exclusions"] if row["criterion_id"] == "602")
    assert row["conditions"] == expected
    plan = stage(server, tool)
    if tool == "remove_pmax_url_exclusions":
        assert expected in list(named_values(plan["operations"], "conditions"))
    preview(server, plan)
    if change == "criterion_name":
        webpage[change] = "Updated synthetic rule"
    elif change == "extra_condition":
        webpage["conditions"].append({"operand": "CUSTOM_LABEL", "operator": "EQUALS", "argument": "seasonal"})
    else:
        webpage["conditions"][1][change] = {"operand": "PAGE_CONTENT", "operator": "CONTAINS",
                                          "argument": "Revised archive"}[change]
    assert h.error_of(apply(server, plan))["code"] == "STALE_PLAN"
    assert not provider.mutations


# Each field is a separate genuine protobuf boundary. Values are injected into
# provider data, after a valid preview for apply, never into caller arguments.
ENUM_CASES = [
    ("asset_group_signal", 0, "approval_status", "get_asset_group_signals", {"asset_group_id": "801"},
     "add_asset_group_search_themes", {"asset_group_id": "801", "themes": ["Seasonal equipment"]}, "LIMITED", True, True),
    ("audience", 1, "scope", "list_audiences", {}, "add_asset_group_audience_signal",
     {"asset_group_id": "801", "audience_id": "902"}, "ASSET_GROUP", True, False),
    ("audience", 1, "status", "list_audiences", {}, "add_asset_group_audience_signal",
     {"asset_group_id": "801", "audience_id": "902"}, "ENABLED", True, False),
    ("asset_group", 0, "primary_status", "get_asset_groups", {"campaign_id": "701"},
     "pause_entity", {"entity_type": "asset_group", "entity_id": "801"}, "NOT_ELIGIBLE", True, True),
    ("asset_group", 0, "status", "get_asset_groups", {"campaign_id": "701"},
     "pause_entity", {"entity_type": "asset_group", "entity_id": "801"}, "ENABLED", False, False),
    ("campaign", 0, "status", "get_asset_groups", {"campaign_id": "701"},
     "pause_entity", {"entity_type": "asset_group", "entity_id": "801"}, "PAUSED", False, False),
    ("campaign", 0, "advertising_channel_type", "get_asset_groups", {"campaign_id": "701"},
     "pause_entity", {"entity_type": "asset_group", "entity_id": "801"}, "PERFORMANCE_MAX", False, False),
]
ENUM_IDS = [f"{case[0]}.{case[2]}" for case in ENUM_CASES]
STATE_CODES = {"PMAX_STATE_UNVERIFIED", "PMAX_SIGNAL_STATE_UNVERIFIED",
               "PMAX_URL_STATE_UNVERIFIED", "PMAX_PRODUCT_STATE_UNVERIFIED"}


def set_enum(provider, case, value):
    resource, index, field = case[:3]
    row = provider.data[h.CUSTOMER_ID][resource][index]
    if value is None:
        row[resource].pop(field, None)
    else:
        row[resource][field] = value
    genuine = h.make_row(row)
    assert genuine._pb.DESCRIPTOR.full_name == "google.ads.googleads.v25.services.GoogleAdsRow"
    if isinstance(value, int):
        assert int(getattr(getattr(genuine, resource), field)) == value


def assert_state_refusal(payload, provider, *, stale=False):
    error = h.error_of(payload)
    assert error["code"] in ({"STALE_PLAN"} if stale else STATE_CODES), payload
    assert isinstance(error["message"], str) and error["message"].strip()
    assert not any(word in json.dumps(payload) for word in ("AttributeError", "Traceback", "'int' object", "INTERNAL"))
    assert "plan" not in payload
    assert not provider.mutations


@pytest.mark.parametrize("case", ENUM_CASES, ids=ENUM_IDS)
@pytest.mark.parametrize("value", [999, 2147483647])
@pytest.mark.parametrize("boundary", ["read", "stage", "apply"])
def test_unknown_numeric_provider_enums_have_named_read_stage_and_stale_apply_refusals(
        tmp_path, case, value, boundary):
    _, _, _, reader, read_args, writer, write_args, *_ = case
    server, provider = setup(tmp_path, [reader, writer])
    if boundary == "apply":
        plan = stage(server, writer, write_args)
        preview(server, plan)
    set_enum(provider, case, value)
    before = len(provider.searches)
    if boundary == "read":
        result = h.call(server, reader, read_args)
    elif boundary == "stage":
        result = h.call(server, writer, write_args)
    else:
        result = apply(server, plan)
    assert len(provider.searches) > before, "The refusal must examine fresh provider state"
    assert_state_refusal(result, provider, stale=boundary == "apply")


@pytest.mark.parametrize("case", ENUM_CASES, ids=ENUM_IDS)
def test_known_provider_enums_still_allow_read_staging_and_application(tmp_path, case):
    _, _, _, reader, read_args, writer, write_args, valid, *_ = case
    server, provider = setup(tmp_path, [reader, writer])
    set_enum(provider, case, valid)
    h.expect_ok(h.call(server, reader, read_args))
    plan = stage(server, writer, write_args)
    checked_apply(server, provider, plan)


@pytest.mark.parametrize("case", ENUM_CASES, ids=ENUM_IDS)
@pytest.mark.parametrize("value", [None, "UNSPECIFIED"], ids=["absent", "unspecified"])
def test_absent_and_unspecified_provider_enums_preserve_existing_contracts(tmp_path, case, value):
    resource, _, field, reader, read_args, writer, write_args, _, readable, writable = case
    server, provider = setup(tmp_path, [reader, writer])
    if not writable:
        previously_valid = stage(server, writer, write_args)
        preview(server, previously_valid)
    set_enum(provider, case, value)
    result = h.call(server, reader, read_args)
    if readable:
        payload = h.expect_ok(result)
        collection = {"asset_group_signal": "signals", "audience": "audiences", "asset_group": "asset_groups"}[resource]
        index = 1 if resource == "audience" else 0
        expected = None if resource == "asset_group_signal" else "UNSPECIFIED"
        assert payload[collection][index][field] == expected
    else:
        assert_state_refusal(result, provider)
    if writable:
        plan = stage(server, writer, write_args)
        checked_apply(server, provider, plan)
    else:
        assert_state_refusal(h.call(server, writer, write_args), provider)
        assert_state_refusal(apply(server, previously_valid), provider, stale=True)
