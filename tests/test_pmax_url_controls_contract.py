"""F069: current automation settings and exact negative WEBPAGE conditions."""
from copy import deepcopy
import json

import pytest

import harness as h
from pmax_oracle import (BAD_IDS, EXPANSION, PMAX_ARGS, SAFETY_CASES, checked_apply, changed,
    criterion_row, golden, read_bound, read_walk, rejected, rn, safeguard, setup, stage, stale)

TOOLS = ["get_pmax_url_settings", "set_pmax_final_url_expansion", "add_pmax_url_exclusion", "remove_pmax_url_exclusions"]
WRITES = TOOLS[1:]


def test_url_settings_have_an_independently_authored_payload(tmp_path):
    golden(tmp_path, "get_pmax_url_settings")


@pytest.mark.parametrize("customer", [h.CUSTOMER_ID, h.OTHER_CUSTOMER_ID])
@pytest.mark.parametrize("campaign,ids", [("701", ["601", "602"]), ("702", ["604"])])
def test_settings_and_negative_rules_scope_two_accounts_and_campaigns(tmp_path, customer, campaign, ids):
    server, provider = setup(tmp_path, TOOLS[:1], read_only=True)
    result = h.expect_ok(h.call(server, TOOLS[0], {"campaign_id": campaign, "customer_id": customer}))
    assert result["customer_id"] == customer and result["campaign_id"] == campaign
    assert result["final_url_expansion"] == {"status": "OPTED_IN", "explicit": True}
    assert [row["criterion_id"] for row in result["exclusions"]] == ids
    for row in result["exclusions"]:
        assert row["resource_name"] == rn("campaignCriteria", f"{campaign}~{row['criterion_id']}", customer)
        assert row["campaign_id"] == campaign and row["conditions"]
    if campaign == "701":
        assert result["exclusions"][1]["conditions"] == [
            {"operand": "URL", "operator": "CONTAINS", "argument": "/archive/"},
            {"operand": "PAGE_TITLE", "operator": "EQUALS", "argument": "Archived"}]
    assert all(s.customer_id == customer for s in provider.searches)
    assert not provider.mutations


@pytest.mark.parametrize("settings,expected", [([], {"status": "UNSPECIFIED", "explicit": False}),
    ([{"asset_automation_type": "TEXT_ASSET_AUTOMATION", "asset_automation_status": "OPTED_OUT"}], {"status": "UNSPECIFIED", "explicit": False}),
    ([{"asset_automation_type": EXPANSION, "asset_automation_status": "UNSPECIFIED"}], {"status": "UNSPECIFIED", "explicit": True}),
    ([{"asset_automation_type": EXPANSION, "asset_automation_status": "OPTED_OUT"}], {"status": "OPTED_OUT", "explicit": True})])
def test_absence_default_and_explicit_status_are_distinct(tmp_path, settings, expected):
    server, provider = setup(tmp_path, TOOLS[:1], read_only=True)
    provider.data[h.CUSTOMER_ID]["campaign"][0]["campaign"]["asset_automation_settings"] = settings
    result = h.expect_ok(h.call(server, TOOLS[0], {"campaign_id": "701"}))
    assert result["final_url_expansion"] == expected
    assert result["automation_settings"] == settings


@pytest.mark.parametrize("case", ["walk", "account", "filter", "tool", "tamper", "expiry"])
def test_url_exclusion_continuations_are_stable_and_bound(tmp_path, case):
    read_walk(tmp_path, TOOLS[0], {"campaign_id": "701"}, "exclusions", "campaign_criterion",
        lambda cid: [criterion_row(610 + i, customer=cid) for i in range(3)], case=case)


def test_url_read_is_bounded_with_one_lookahead(tmp_path):
    read_bound(tmp_path, TOOLS[0], {"campaign_id": "701"}, "exclusions", "campaign_criterion",
        lambda cid: [criterion_row(10000 + i, customer=cid) for i in range(10002)])


@pytest.mark.parametrize("enabled", [True, False])
@pytest.mark.parametrize("absent", [True, False])
def test_expansion_updates_current_setting_and_preserves_every_other_entry(tmp_path, enabled, absent):
    server, provider = setup(tmp_path, TOOLS)
    initial = provider.data[h.CUSTOMER_ID]["campaign"][0]["campaign"]["asset_automation_settings"]
    if absent:
        initial[:] = [entry for entry in initial if entry["asset_automation_type"] != EXPANSION]
    untouched = [deepcopy(entry) for entry in initial if entry["asset_automation_type"] != EXPANSION]
    plan = stage(server, TOOLS[1], {"campaign_id": "701", "enabled": enabled})
    text = json.dumps(plan).lower()
    assert "text" in text and any(word in text for word in ("destination", "landing"))
    assert "independent" in text or "customization" in text or "customisation" in text
    assert "url_expansion_opt_out" not in text
    call = checked_apply(server, provider, plan)
    assert call.service == "CampaignService" and call.method == "mutate_campaigns"
    request = call.request
    assert request._pb.DESCRIPTOR.name == "MutateCampaignsRequest" and request.customer_id == h.CUSTOMER_ID
    assert len(request.operations) == 1 and not request.partial_failure
    op = request.operations[0]
    assert op._pb.WhichOneof("operation") == "update" and op.update.resource_name == rn("campaigns", "701")
    assert list(op.update_mask.paths) == ["asset_automation_settings"]
    assert {f.name for f, _ in op.update._pb.ListFields()} == {"resource_name", "asset_automation_settings"}
    actual = [{"asset_automation_type": a.asset_automation_type.name, "asset_automation_status": a.asset_automation_status.name} for a in op.update.asset_automation_settings]
    assert [e for e in actual if e["asset_automation_type"] != EXPANSION] == untouched
    assert [e for e in actual if e["asset_automation_type"] == EXPANSION] == [
        {"asset_automation_type": EXPANSION, "asset_automation_status": "OPTED_IN" if enabled else "OPTED_OUT"}]


@pytest.mark.parametrize("enabled", [None, 0, 1, "true", "false", [], {}, 1.5])
def test_expansion_boolean_is_strict_before_reads(tmp_path, enabled):
    server, provider = setup(tmp_path, TOOLS)
    rejected(server, provider, TOOLS[1], {"campaign_id": "701", "enabled": enabled}, local=True)


@pytest.mark.parametrize("issue", ["unrelated_conflict", "conflict", "unknown_type", "unknown_status"])
def test_unsafe_automation_state_cannot_be_rewritten(tmp_path, issue):
    server, provider = setup(tmp_path, TOOLS)
    settings = provider.data[h.CUSTOMER_ID]["campaign"][0]["campaign"]["asset_automation_settings"]
    if issue in ("unrelated_conflict", "conflict"):
        settings.append({"asset_automation_type": "TEXT_ASSET_AUTOMATION" if issue == "unrelated_conflict" else EXPANSION,
            "asset_automation_status": "OPTED_IN" if issue == "unrelated_conflict" else "OPTED_OUT"})
    else:
        settings.append({"asset_automation_type": "UNKNOWN" if issue == "unknown_type" else "GENERATE_LANDING_PAGE_TEXT",
            "asset_automation_status": "UNKNOWN" if issue == "unknown_status" else "OPTED_IN"})
    rejected(server, provider, TOOLS[1], PMAX_ARGS[TOOLS[1]])


@pytest.mark.parametrize("url,mode,operator", [("https://example.invalid/landing?q=One", "EXACT", "EQUALS"),
    ("http://example.invalid/landing", "EXACT", "EQUALS"), ("/Private/", "CONTAINS", "CONTAINS")])
def test_exclusion_is_exactly_one_url_condition(tmp_path, url, mode, operator):
    server, provider = setup(tmp_path, TOOLS)
    plan = stage(server, TOOLS[2], {"campaign_id": "701", "url": url, "match_type": mode})
    assert url in json.dumps(plan) and mode in json.dumps(plan)
    call = checked_apply(server, provider, plan)
    assert call.service == "CampaignCriterionService" and call.method == "mutate_campaign_criteria"
    req = call.request
    assert req._pb.DESCRIPTOR.name == "MutateCampaignCriteriaRequest" and req.customer_id == h.CUSTOMER_ID and not req.partial_failure
    assert len(req.operations) == 1 and req.operations[0]._pb.WhichOneof("operation") == "create"
    criterion = req.operations[0].create
    assert criterion.campaign == rn("campaigns", "701") and criterion.negative is True
    assert criterion._pb.WhichOneof("criterion") == "webpage" and len(criterion.webpage.conditions) == 1
    condition = criterion.webpage.conditions[0]
    assert condition.operand.name == "URL" and condition.operator.name == operator and condition.argument == url


@pytest.mark.parametrize("url,mode", [("", "EXACT"), (" ", "CONTAINS"), ("example.invalid/x", "EXACT"),
    ("ftp://example.invalid/x", "EXACT"), ("https://user:pass@example.invalid/x", "EXACT"),
    ("https://example.invalid/a b", "EXACT"), ("a b", "CONTAINS"), ("a\nb", "CONTAINS"),
    ("a\x00b", "CONTAINS"), ("/private", "REGEX"), (True, "EXACT"), ({"url": "x"}, "EXACT")])
def test_exclusion_input_rejects_empty_broad_or_malformed_rules_locally(tmp_path, url, mode):
    server, provider = setup(tmp_path, TOOLS)
    rejected(server, provider, TOOLS[2], {"campaign_id": "701", "url": url, "match_type": mode}, local=True)


def test_exclusion_default_is_exact_and_existing_duplicates_refuse(tmp_path):
    server, provider = setup(tmp_path, TOOLS)
    plan = stage(server, TOOLS[2], {"campaign_id": "701", "url": "https://example.invalid/new"})
    call = checked_apply(server, provider, plan)
    assert call.request.operations[0].create.webpage.conditions[0].operator.name == "EQUALS"
    provider.mutations.clear()
    rejected(server, provider, TOOLS[2], {"campaign_id": "701", "url": "https://example.invalid/exclude/601"})


def test_removal_names_only_exact_existing_negative_webpage_criteria(tmp_path):
    server, provider = setup(tmp_path, TOOLS)
    plan = stage(server, TOOLS[3])
    assert plan["irreversible"] is True
    for value in ("/archive/", "Archived", "https://example.invalid/exclude/601"):
        assert value in json.dumps(plan), "preview must preserve complete removed conditions"
    call = checked_apply(server, provider, plan)
    assert call.service == "CampaignCriterionService" and call.request.customer_id == h.CUSTOMER_ID
    assert [op.remove for op in call.request.operations] == [rn("campaignCriteria", "701~601"), rn("campaignCriteria", "701~602")]
    assert all(op._pb.WhichOneof("operation") == "remove" for op in call.request.operations)


@pytest.mark.parametrize("issue", ["positive", "keyword", "foreign", "other_campaign", "duplicate", "missing", "incomplete", "over_limit"])
@pytest.mark.parametrize("tool", [TOOLS[2], TOOLS[3]])
def test_exclusion_source_requires_complete_verified_rules(tmp_path, tool, issue):
    server, provider = setup(tmp_path, TOOLS)
    if issue == "positive":
        provider.data[h.CUSTOMER_ID]["campaign_criterion"][0]["campaign_criterion"]["negative"] = False
    elif issue == "keyword":
        criterion = provider.data[h.CUSTOMER_ID]["campaign_criterion"][0]["campaign_criterion"]
        criterion.pop("webpage")
        criterion.update(type_="KEYWORD", keyword={"text": "unrelated", "match_type": "EXACT"})
    elif issue == "foreign":
        provider.corrupt["campaign_criterion"] = lambda rows: [changed(r, "campaign_criterion.resource_name", rn("campaignCriteria", "701~601", h.OTHER_CUSTOMER_ID)) for r in rows]
    elif issue == "other_campaign":
        provider.corrupt["campaign_criterion"] = lambda rows: [changed(r, "campaign_criterion.campaign", rn("campaigns", "702")) for r in rows]
    elif issue == "duplicate":
        provider.corrupt["campaign_criterion"] = lambda rows: rows + rows
    elif issue == "missing":
        provider.data[h.CUSTOMER_ID]["campaign_criterion"] = []
    elif issue == "incomplete":
        provider.fail_after["campaign_criterion"] = 1
    else:
        provider.data[h.CUSTOMER_ID]["campaign_criterion"] = [criterion_row(10000 + i) for i in range(10002)]
    # Unrelated/absent criteria are valid neighbors for a create; removal must
    # prove its requested IDs are existing negative WEBPAGE criteria.
    if tool == TOOLS[2] and issue in ("positive", "keyword", "missing"):
        checked_apply(server, provider, stage(server, tool))
    else:
        rejected(server, provider, tool, PMAX_ARGS[tool])
    assert provider.pulls["campaign_criterion"] <= 10001


@pytest.mark.parametrize("tool,field", [(TOOLS[0], "campaign_id"), (TOOLS[1], "campaign_id"), (TOOLS[3], "criterion_ids")])
@pytest.mark.parametrize("bad", BAD_IDS)
def test_url_workflow_identifiers_are_local(tmp_path, tool, field, bad):
    server, provider = setup(tmp_path, TOOLS)
    args = {"campaign_id": "701"} if tool == TOOLS[0] else PMAX_ARGS[tool]
    rejected(server, provider, tool, {**args, field: [bad] if field == "criterion_ids" else bad}, local=True)


@pytest.mark.parametrize("ids", [[], ["601", "601"], ["601", "0601"], ["604"], ["999"]])
def test_exclusion_removals_require_unique_local_child_ids(tmp_path, ids):
    server, provider = setup(tmp_path, TOOLS)
    rejected(server, provider, TOOLS[3], {"campaign_id": "701", "criterion_ids": ids}, local=not ids or len(ids) > 1)


@pytest.mark.parametrize("tool,change", [(tool, change) for tool in WRITES for change in ("status", "automation", "exclusion")
    if not ((tool == TOOLS[1] and change == "exclusion") or (tool != TOOLS[1] and change == "automation"))])
def test_url_plan_rechecks_relevant_state(tmp_path, tool, change):
    server, provider = setup(tmp_path, TOOLS)
    plan = stage(server, tool)
    if change == "status":
        provider.data[h.CUSTOMER_ID]["campaign"][0]["campaign"]["status"] = "PAUSED"
    elif change == "automation":
        provider.data[h.CUSTOMER_ID]["campaign"][0]["campaign"]["asset_automation_settings"][0]["asset_automation_status"] = "OPTED_IN"
    else:
        provider.data[h.CUSTOMER_ID]["campaign_criterion"][0]["campaign_criterion"]["webpage"]["conditions"][0]["argument"] = "https://example.invalid/changed"
    stale(server, provider, plan)


@pytest.mark.parametrize("tool", WRITES)
@pytest.mark.parametrize("case", SAFETY_CASES)
def test_each_url_operation_keeps_application_safeguards(tmp_path, monkeypatch, tool, case):
    safeguard(tmp_path, monkeypatch, tool, case)


def test_url_exclusion_removal_requires_irreversible_acknowledgement(tmp_path, monkeypatch):
    safeguard(tmp_path, monkeypatch, TOOLS[3], "acknowledgement")


@pytest.mark.parametrize("tool", WRITES)
def test_url_writes_do_not_register_readonly(tmp_path, tool):
    server, provider = setup(tmp_path, TOOLS[:1], read_only=True)
    assert tool not in h.tool_names(server)
    result = h.call_result(server, tool, PMAX_ARGS[tool])
    assert result.is_error and "unknown tool" in h.result_text(result).lower()


def test_verified_empty_url_exclusions_and_unrelated_criteria_are_truthful(tmp_path):
    server, provider = setup(tmp_path, TOOLS[:1], read_only=True)
    provider.data[h.CUSTOMER_ID]["campaign_criterion"] = []
    assert h.expect_ok(h.call(server, TOOLS[0], {"campaign_id": "701"}))["exclusions"] == []
    rejected(server, provider, TOOLS[0], {"campaign_id": "703"})
    rejected(server, provider, TOOLS[0], {"campaign_id": "999"})


@pytest.mark.parametrize("issue", ["positive", "keyword", "foreign", "other_campaign"])
def test_url_reads_never_present_unrelated_criteria_as_exclusions(tmp_path, issue):
    server, provider = setup(tmp_path, TOOLS[:1], read_only=True)
    row = criterion_row()
    criterion = row["campaign_criterion"]
    if issue == "positive":
        criterion["negative"] = False
    elif issue == "keyword":
        criterion.pop("webpage")
        criterion.update(type_="KEYWORD", keyword={"text": "unrelated", "match_type": "EXACT"})
    elif issue == "foreign":
        criterion["resource_name"] = rn("campaignCriteria", "701~601", h.OTHER_CUSTOMER_ID)
    else:
        criterion["campaign"] = rn("campaigns", "702")
    provider.corrupt["campaign_criterion"] = lambda rows: [h.make_row(row)]
    payload = h.call(server, TOOLS[0], {"campaign_id": "701"})
    if "error" in payload:
        assert h.error_of(payload)["code"] != "INTERNAL"
    else:
        assert payload["exclusions"] == []
