"""Existing Search destination inspection and precise guarded URL updates."""
from copy import deepcopy
import json
import re

import pytest

import harness as h
from offline_contract import refusal
from search_url_oracle import (AFTER_FINAL, AFTER_MOBILE, BAD_IDS, BAD_LISTS, BEFORE_FINAL, BEFORE_MOBILE,
    FIXTURES, KINDS, SAFETY_CASES, TRACKING, args, apply, assert_plan, assert_provider_queries,
    checked_apply, corrupt_path, entity, golden, preview, rejected, rn, safeguard, set_path, setup, stage,
    stale, values_named)

ENTITY_INVALID = [('ad_group_ad', 'ad_group_ad.status', 'REMOVED'), ('ad_group_ad', 'ad_group_ad.status', 'UNKNOWN'), ('ad_group_ad', 'ad_group_ad.status', 777), ('ad_group_ad', 'ad_group_ad.ad.type_', 777), ('ad_group_ad', 'ad_group_ad.ad.type_', 'EXPANDED_TEXT_AD'), ('ad_group_ad', 'ad_group_ad.ad.responsive_search_ad.headlines.0.pinned_field', 777), ('ad_group_ad', 'ad_group_ad.ad.responsive_search_ad.descriptions.0.pinned_field', 777)]
DRIFT_FIELDS = [('campaign', 'campaign.status', 'PAUSED'), ('ad_group', 'ad_group.status', 'PAUSED'), ('ad_group_ad', 'ad_group_ad.status', 'PAUSED'), ('ad_group_ad', 'ad_group_ad.ad.final_urls', ['https://example.invalid/changed']), ('ad_group_ad', 'ad_group_ad.ad.final_mobile_urls', []), ('ad_group_ad', 'ad_group_ad.ad.tracking_url_template', 'https://track.example.invalid/new?u={lpurl}'), ('ad_group_ad', 'ad_group_ad.ad.final_url_suffix', 'edition=changed'), ('ad_group_ad', 'ad_group_ad.ad.url_custom_parameters.0.value', 'Changed'), ('ad_group_ad', 'ad_group_ad.ad.responsive_search_ad.headlines.0.text', 'Different trail equipment'), ('ad_group_ad', 'ad_group_ad.ad.responsive_search_ad.descriptions.0.text', 'Explore a different collection.'), ('ad_group_ad', 'ad_group_ad.ad.responsive_search_ad.headlines.0.pinned_field', 'HEADLINE_2'), ('ad_group_ad', 'ad_group_ad.ad.responsive_search_ad.descriptions.0.pinned_field', 'DESCRIPTION_2'), ('ad_group_ad', 'ad_group_ad.ad.responsive_search_ad.path1', 'changed'), ('ad_group_ad', 'ad_group_ad.ad.responsive_search_ad.path2', 'changed')]

KIND = 'ad'
SPEC = KINDS[KIND]
ENTITY_INVALID += [("ad_group_ad", "ad_group_ad.resource_name", rn("adGroupAds", "801~602"))]


def test_read_payload_matches_independently_authored_golden(tmp_path):
    golden(tmp_path, KIND)


def test_schema_is_exact_and_writes_are_absent_in_read_only_mode(tmp_path):
    server, provider = setup(tmp_path, KIND)
    tools = h.tool_map(server)
    for mode in ("read", "write"):
        schema = tools[SPEC[mode]].input_schema
        expected = {"ad_group_id", SPEC["id"], "customer_id"}
        if mode == "write":
            expected |= {"final_urls", "final_mobile_urls"}
        assert set(schema["properties"]) == expected
        assert set(schema.get("required", [])) == {"ad_group_id", SPEC["id"]}
    readonly, client = setup(tmp_path, KIND, read_only=True)
    assert SPEC["write"] not in h.tool_names(readonly)
    result = h.call_result(readonly, SPEC["write"], args(KIND, final_urls=AFTER_FINAL))
    assert result.is_error and "unknown tool" in h.result_text(result).lower()
    assert not client.searches and not client.mutations
    assert not provider.searches and not provider.mutations


@pytest.mark.parametrize("customer", [h.CUSTOMER_ID, h.OTHER_CUSTOMER_ID])
def test_complete_singular_inspection_scopes_two_accounts_and_siblings(tmp_path, customer):
    server, provider = setup(tmp_path, KIND, env={"ADS_MCP_ROW_LIMIT": "1"})
    readonly, ro_provider = setup(tmp_path, KIND, read_only=True, env={"ADS_MCP_ROW_LIMIT": "1"})
    result = h.expect_ok(h.call(server, SPEC["read"], args(KIND, customer_id=customer)))
    assert result == h.call(readonly, SPEC["read"], args(KIND, customer_id=customer))
    assert result["customer_id"] == customer
    record = result[SPEC["entity"]]
    assert record[SPEC["id"]] == "601"
    assert record["resource_name"] == rn(SPEC["path"], "601" if KIND == "ad" else "801~601", customer)
    assert record["final_urls"] == BEFORE_FINAL and record["final_mobile_urls"] == BEFORE_MOBILE
    assert result["campaign"]["campaign_id"] == "701" and result["ad_group"]["ad_group_id"] == "801"
    assert "next_page_token" not in result and not result.get("possibly_truncated") and not result.get("truncated")
    for client in (provider, ro_provider):
        assert all(call.customer_id == customer for call in client.searches)
        assert_provider_queries(client.searches)
        assert not client.mutations


@pytest.mark.parametrize("mode", ["read", "write"])
@pytest.mark.parametrize("field", ["ad_group_id", 'ad_id'])
@pytest.mark.parametrize("value", BAD_IDS, ids=[f"invalid-id-{i}" for i in range(len(BAD_IDS))])
def test_noncanonical_resource_identifiers_refuse_before_provider_reads(tmp_path, mode, field, value):
    server, provider = setup(tmp_path, KIND)
    parameters = args(KIND, **({"final_urls": AFTER_FINAL} if mode == "write" else {}))
    parameters[field] = value
    rejected(server, provider, SPEC[mode], parameters, local=True)


@pytest.mark.parametrize("field", ["final_urls", "final_mobile_urls"])
@pytest.mark.parametrize("value", BAD_LISTS, ids=[f"invalid-list-{i}" for i in range(len(BAD_LISTS))])
def test_invalid_url_arguments_refuse_locally_without_normalizing(tmp_path, field, value):
    server, provider = setup(tmp_path, KIND)
    rejected(server, provider, SPEC["write"], args(KIND, **{field: value}), local=True)


@pytest.mark.parametrize("mode", ["read", "write"])
@pytest.mark.parametrize("extra", [{"page_token": "opaque"}, {"unexpected": 1}, {"update_mask": ["status"]}, {"status": "PAUSED"}])
def test_singular_tools_reject_extra_arguments_before_reads(tmp_path, mode, extra):
    server, provider = setup(tmp_path, KIND)
    values = {"final_urls": AFTER_FINAL} if mode == "write" else {}
    rejected(server, provider, SPEC[mode], args(KIND, **values, **extra), local=True)


@pytest.mark.parametrize("values", [{}, {"final_urls": None}, {"final_mobile_urls": None},
    {"final_urls": None, "final_mobile_urls": None}])
def test_omitted_or_only_null_lists_do_not_create_a_plan(tmp_path, values):
    server, provider = setup(tmp_path, KIND)
    rejected(server, provider, SPEC["write"], args(KIND, **values), local=True)


@pytest.mark.parametrize("values", [{"final_urls": BEFORE_FINAL}, {"final_mobile_urls": BEFORE_MOBILE},
    {"final_urls": BEFORE_FINAL, "final_mobile_urls": BEFORE_MOBILE}])
def test_unchanged_results_refuse_without_staging_or_writing(tmp_path, values):
    server, provider = setup(tmp_path, KIND)
    rejected(server, provider, SPEC["write"], args(KIND, **values))
    assert_provider_queries(provider.searches)
    assert not any(record["event"] == "plan_created" for record in h.read_audit_records(tmp_path))


@pytest.mark.parametrize("values,mask", [
    ({"final_urls": AFTER_FINAL}, ["final_urls"]),
    ({"final_urls": AFTER_FINAL, "final_mobile_urls": None}, ["final_urls"]),
    ({"final_mobile_urls": AFTER_MOBILE}, ["final_mobile_urls"]),
    ({"final_urls": None, "final_mobile_urls": AFTER_MOBILE}, ["final_mobile_urls"]),
    ({"final_urls": BEFORE_FINAL, "final_mobile_urls": AFTER_MOBILE}, ["final_mobile_urls"]),
    ({"final_urls": AFTER_FINAL, "final_mobile_urls": BEFORE_MOBILE}, ["final_urls"]),
    ({"final_urls": AFTER_FINAL, "final_mobile_urls": AFTER_MOBILE}, ["final_urls", "final_mobile_urls"]),
    ({"final_mobile_urls": []}, ["final_mobile_urls"]),
])
def test_reviewed_replacements_use_exact_update_identity_mask_and_values(tmp_path, values, mask):
    server, provider = setup(tmp_path, KIND)
    before = {"final_urls": BEFORE_FINAL, "final_mobile_urls": BEFORE_MOBILE}
    after = {**before, **{key: value for key, value in values.items() if value is not None}}
    plan = stage(server, KIND, values)
    assert_plan(plan, KIND, before, after, mask)
    checked_apply(server, provider, KIND, plan, after, mask)
    inspected = h.expect_ok(h.call(server, SPEC["read"], args(KIND)))[SPEC["entity"]]
    assert all(inspected[key] == value for key, value in after.items())


@pytest.mark.parametrize("urls", [
    ["https://example.invalid/" + "é" * (2048 - len("https://example.invalid/"))],
    [f"https://example.invalid/{i}" for i in range(10)],
    ["https://example.invalid/Case", "https://example.invalid/case"],
    ["https://example.invalid:65535/path?x={keyword}#Part", "http://[2001:db8::1]:8080/"],
    ["https://example.invalid/second", "https://example.invalid/first"],
])
def test_valid_boundary_neighbors_preserve_codepoints_case_order_and_valuetrack(tmp_path, urls):
    server, provider = setup(tmp_path, KIND)
    plan = stage(server, KIND, {"final_urls": urls})
    checked_apply(server, provider, KIND, plan, {"final_urls": urls}, ["final_urls"])


@pytest.mark.parametrize("resource,path", [
    ("campaign", "campaign.status"), ("ad_group", "ad_group.status"), ('ad_group_ad', 'ad_group_ad.status'),
])
def test_paused_supported_resources_remain_editable_without_status_changes(tmp_path, resource, path):
    server, provider = setup(tmp_path, KIND)
    set_path(provider.data[h.CUSTOMER_ID][resource][0], path, "PAUSED")
    plan = stage(server, KIND)
    checked_apply(server, provider, KIND, plan, {"final_urls": AFTER_FINAL}, ["final_urls"])


PARENT_INVALID = [
    ("campaign", "campaign.status", "REMOVED"), ("campaign", "campaign.status", "UNKNOWN"),
    ("campaign", "campaign.status", 777), ("campaign", "campaign.advertising_channel_type", "DISPLAY"),
    ("campaign", "campaign.advertising_channel_type", "PERFORMANCE_MAX"),
    ("campaign", "campaign.advertising_channel_type", 777),
    ("ad_group", "ad_group.status", "REMOVED"), ("ad_group", "ad_group.status", "UNSPECIFIED"),
    ("ad_group", "ad_group.status", 777), ("ad_group", "ad_group.type_", "SEARCH_DYNAMIC_ADS"),
    ("ad_group", "ad_group.type_", 777),
]


@pytest.mark.parametrize("mode", ["read", "write"])
@pytest.mark.parametrize("resource,path,value", PARENT_INVALID + ENTITY_INVALID)
def test_unsupported_or_unknown_state_has_named_content_safe_refusal(tmp_path, mode, resource, path, value):
    server, provider = setup(tmp_path, KIND)
    set_path(provider.data[h.CUSTOMER_ID][resource][0], path, value)
    parameters = args(KIND, **({"final_urls": AFTER_FINAL} if mode == "write" else {}))
    rejected(server, provider, SPEC[mode], parameters)
    assert_provider_queries(provider.searches)
    assert not any(record["event"] == "plan_created" for record in h.read_audit_records(tmp_path))


@pytest.mark.parametrize("mode", ["read", "write"])
@pytest.mark.parametrize("fault", ["missing", "duplicate", "many", "incomplete", "foreign", "identity", "parent", "oversized", "bad_url", "duplicate_url", "duplicate_tracking", "malformed_tracking"])
def test_singular_state_must_be_complete_bounded_and_verifiable(tmp_path, mode, fault):
    server, provider = setup(tmp_path, KIND, env={"ADS_MCP_ROW_LIMIT": "1", "ADS_MCP_RETRY_BASE_SECONDS": "0.001"})
    resource = SPEC["resource"]
    prefix = "ad_group_ad.ad" if KIND == "ad" else "ad_group_criterion"
    if fault == "missing":
        provider.corrupt[resource] = lambda rows: []
    elif fault in ("duplicate", "many"):
        provider.corrupt[resource] = lambda rows: [deepcopy(rows[0]) for _ in range(2 if fault == "duplicate" else 100)]
    elif fault == "incomplete":
        provider.fail_after[resource] = 1
    elif fault == "foreign":
        corrupt_path(provider, resource, prefix + ".resource_name", rn(SPEC["path"], "601" if KIND == "ad" else "801~601", h.OTHER_CUSTOMER_ID))
    elif fault == "identity":
        corrupt_path(provider, resource, prefix + (".id" if KIND == "ad" else ".criterion_id"), 602)
    elif fault == "parent":
        corrupt_path(provider, resource, resource + ".ad_group", rn("adGroups", "802"))
    elif fault == "oversized":
        entity(provider, KIND)["final_url_suffix"] = "synthetic-private-provider-" + "x" * (16 * 1024 * 1024)
    elif fault == "bad_url":
        entity(provider, KIND)["final_urls"] = ["https://user@example.invalid/secret"]
    elif fault == "duplicate_url":
        entity(provider, KIND)["final_urls"] = BEFORE_FINAL * 2
    elif fault == "duplicate_tracking":
        entity(provider, KIND)["url_custom_parameters"] = [{"key": "edition", "value": "One"}, {"key": "edition", "value": "Two"}]
    else:
        entity(provider, KIND)["url_custom_parameters"] = [{"key": "", "value": "synthetic-private-provider"}]
    rejected(server, provider, SPEC[mode], args(KIND, **({"final_urls": AFTER_FINAL} if mode == "write" else {})))
    assert_provider_queries(provider.searches)
    if fault in ("duplicate", "many"):
        assert provider.pulls[resource] <= 2, "Singular verification consumed beyond one lookahead"
    for call in provider.searches:
        if re.search(r"FROM\s+" + resource + r"\b", call.query, re.I):
            assert re.search(r"LIMIT\s+2\b", call.query, re.I), "Singular verification must retain one lookahead"


@pytest.mark.parametrize("resource,path,value", PARENT_INVALID + ENTITY_INVALID + DRIFT_FIELDS)
def test_apply_rereads_every_relevant_state_field_and_refuses_drift(tmp_path, resource, path, value):
    server, provider = setup(tmp_path, KIND)
    plan = stage(server, KIND)
    set_path(provider.data[h.CUSTOMER_ID][resource][0], path, value)
    stale(server, provider, plan)


@pytest.mark.parametrize("fault", ["missing", "duplicate", "incomplete", "oversized", "foreign"])
def test_apply_unverifiable_state_is_stale_and_never_a_partial_write(tmp_path, fault):
    server, provider = setup(tmp_path, KIND, env={"ADS_MCP_RETRY_BASE_SECONDS": "0.001"})
    plan = stage(server, KIND)
    resource = SPEC["resource"]
    if fault == "missing":
        provider.corrupt[resource] = lambda rows: []
    elif fault == "duplicate":
        provider.corrupt[resource] = lambda rows: rows + rows
    elif fault == "incomplete":
        provider.fail_after[resource] = 1
    elif fault == "oversized":
        entity(provider, KIND)["final_url_suffix"] = "x" * (16 * 1024 * 1024)
    else:
        prefix = "ad_group_ad.ad" if KIND == "ad" else "ad_group_criterion"
        corrupt_path(provider, resource, prefix + ".resource_name", rn(SPEC["path"], "601" if KIND == "ad" else "801~601", h.OTHER_CUSTOMER_ID))
    stale(server, provider, plan)


@pytest.mark.parametrize("case", SAFETY_CASES)
def test_each_editor_preserves_confirmation_audit_and_delivery_safeguards(tmp_path, monkeypatch, case):
    safeguard(tmp_path, monkeypatch, KIND, case)


def test_responsive_search_ad_final_urls_cannot_be_cleared(tmp_path):
    server, provider = setup(tmp_path, KIND)
    rejected(server, provider, SPEC["write"], args(KIND, final_urls=[]), local=True)


@pytest.mark.parametrize("phase", ["inspect", "stage", "apply"])
def test_ad_verification_queries_all_nonremoved_associations_before_ad_service_update(tmp_path, phase):
    server, provider = setup(tmp_path, KIND)
    plan = stage(server, KIND) if phase == "apply" else None
    shared = deepcopy(provider.data[h.CUSTOMER_ID]["ad_group_ad"][0])
    shared["ad_group_ad"]["resource_name"] = rn("adGroupAds", "802~601")
    shared["ad_group_ad"]["ad_group"] = rn("adGroups", "802")
    provider.data[h.CUSTOMER_ID]["ad_group_ad"].append(shared)
    if plan:
        stale(server, provider, plan)
    else:
        rejected(server, provider, SPEC["read"] if phase == "inspect" else SPEC["write"],
                 args(KIND, **({} if phase == "inspect" else {"final_urls": AFTER_FINAL})))
    relevant = [call.query for call in provider.searches if "FROM ad_group_ad" in call.query]
    assert relevant
    for query in relevant:
        where = re.search(r"WHERE(.*?)(?:ORDER BY|LIMIT|$)", query, re.I | re.S)
        assert where
        assert not re.search(r"\bad_group(?:_ad)?\.(?:ad_group|id|resource_name)\s*(?:=|IN)", where[1], re.I)
    assert provider.pulls["ad_group_ad"] <= (3 if phase == "apply" else 2)


def test_removed_other_association_does_not_hide_the_supported_association(tmp_path):
    server, provider = setup(tmp_path, KIND)
    old = deepcopy(provider.data[h.CUSTOMER_ID]["ad_group_ad"][0])
    old["ad_group_ad"].update(resource_name=rn("adGroupAds", "802~601"), ad_group=rn("adGroups", "802"), status="REMOVED")
    provider.data[h.CUSTOMER_ID]["ad_group_ad"].append(old)
    plan = stage(server, KIND)
    checked_apply(server, provider, KIND, plan, {"final_urls": AFTER_FINAL}, ["final_urls"])


@pytest.mark.parametrize("boundary", ["inspect", "stage", "apply"])
@pytest.mark.parametrize("resource,path,value", [
    ("campaign", "campaign.id", 702),
    ("campaign", "campaign.resource_name", rn("campaigns", "701", h.OTHER_CUSTOMER_ID)),
    ("ad_group", "ad_group.id", 802),
    ("ad_group", "ad_group.resource_name", rn("adGroups", "801", h.OTHER_CUSTOMER_ID)),
])
def test_malformed_parent_identity_cannot_authorize_inspection_or_updates(tmp_path, boundary, resource, path, value):
    server, provider = setup(tmp_path, KIND)
    plan = stage(server, KIND) if boundary == "apply" else None
    corrupt_path(provider, resource, path, value)
    if plan:
        stale(server, provider, plan)
    else:
        parameters = args(KIND, **({"final_urls": AFTER_FINAL} if boundary == "stage" else {}))
        rejected(server, provider, SPEC["read"] if boundary == "inspect" else SPEC["write"], parameters)


@pytest.mark.parametrize("field", ["ad_group_id", SPEC["id"]])
def test_signed_64_bit_maximum_resource_identifier_is_a_valid_neighbor(tmp_path, field):
    server, provider = setup(tmp_path, KIND)
    maximum = "9223372036854775807"
    selected_group = maximum if field == "ad_group_id" else "801"
    selected_id = maximum if field == SPEC["id"] else "601"
    group = provider.data[h.CUSTOMER_ID]["ad_group"][0]["ad_group"]
    group.update(id=int(selected_group), resource_name=rn("adGroups", selected_group))
    row = provider.data[h.CUSTOMER_ID][SPEC["resource"]][0][SPEC["resource"]]
    row["ad_group"] = rn("adGroups", selected_group)
    current = entity(provider, KIND)
    if KIND == "ad":
        row["resource_name"] = rn("adGroupAds", selected_group + "~" + selected_id)
        current.update(id=int(selected_id), resource_name=rn("ads", selected_id))
    else:
        current.update(criterion_id=int(selected_id), resource_name=rn("adGroupCriteria", selected_group + "~" + selected_id))
    parameters = args(KIND, **{field: maximum})
    result = h.expect_ok(h.call(server, SPEC["read"], parameters))
    assert result[SPEC["entity"]][SPEC["id"]] == selected_id
    assert result["ad_group"]["ad_group_id"] == selected_group
    plan = h.expect_ok(h.call(server, SPEC["write"], {**parameters, "final_urls": AFTER_FINAL}))["plan"]
    preview(server, plan)
    assert h.expect_ok(apply(server, plan))["applied"] is True
    call = provider.live_mutations()[0]
    assert call.request.operations[0].update.resource_name == current["resource_name"]
    assert list(call.request.operations[0].update.final_urls) == AFTER_FINAL
    assert_provider_queries(provider.searches)


@pytest.mark.parametrize("field,value", [("final_urls", []), ("responsive_search_ad.headlines", []),
    ("responsive_search_ad.descriptions", []), ("responsive_search_ad.headlines.0.text", "")])
@pytest.mark.parametrize("boundary", ["inspect", "stage", "apply"])
def test_missing_required_creative_or_destination_state_is_unverifiable(tmp_path, field, value, boundary):
    server, provider = setup(tmp_path, KIND)
    plan = stage(server, KIND) if boundary == "apply" else None
    set_path(entity(provider, KIND), field, value)
    if plan:
        stale(server, provider, plan)
    else:
        rejected(server, provider, SPEC["read"] if boundary == "inspect" else SPEC["write"],
                 args(KIND, **({"final_urls": AFTER_FINAL} if boundary == "stage" else {})))



def test_apply_rechecks_parent_relationship_even_when_both_campaigns_are_supported(tmp_path):
    server, provider = setup(tmp_path, KIND)
    plan = stage(server, KIND)
    provider.data[h.CUSTOMER_ID]["ad_group"][0]["ad_group"]["campaign"] = rn("campaigns", "702")
    stale(server, provider, plan)


def test_other_supported_search_parent_is_not_confused_with_default_fixture_parent(tmp_path):
    server, provider = setup(tmp_path, KIND)
    provider.data[h.CUSTOMER_ID]["ad_group"][0]["ad_group"]["campaign"] = rn("campaigns", "702")
    result = h.expect_ok(h.call(server, SPEC["read"], args(KIND)))
    assert result["campaign"]["campaign_id"] == "702"
    plan = stage(server, KIND)
    checked_apply(server, provider, KIND, plan, {"final_urls": AFTER_FINAL}, ["final_urls"])



@pytest.mark.parametrize("resource", ["campaign", "ad_group"])
@pytest.mark.parametrize("fault", ["missing", "duplicate"])
@pytest.mark.parametrize("boundary", ["inspect", "stage", "apply"])
def test_parent_verification_refuses_incomplete_or_ambiguous_singular_results(tmp_path, resource, fault, boundary):
    server, provider = setup(tmp_path, KIND)
    plan = stage(server, KIND) if boundary == "apply" else None
    provider.corrupt[resource] = (lambda rows: []) if fault == "missing" else (lambda rows: rows + rows)
    if plan:
        stale(server, provider, plan)
    else:
        rejected(server, provider, SPEC["read"] if boundary == "inspect" else SPEC["write"],
                 args(KIND, **({"final_urls": AFTER_FINAL} if boundary == "stage" else {})))


@pytest.mark.parametrize("mode", ["read", "write"])
def test_mcp_argument_errors_do_not_repeat_configured_credential_values(tmp_path, mode):
    server, provider = setup(tmp_path, KIND)
    parameters = args(KIND, **({"final_urls": AFTER_FINAL} if mode == "write" else {}))
    parameters[h.FAKE_REFRESH_TOKEN] = {"nested": h.FAKE_CLIENT_SECRET}
    result = rejected(server, provider, SPEC[mode], parameters, local=True)
    h.assert_no_secrets(h.result_text(result))
