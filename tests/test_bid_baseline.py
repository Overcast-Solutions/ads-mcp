"""F020 — Bids are manageable without a pre-existing baseline."""

import pytest
from google.api_core import exceptions as core_exceptions

import harness
from ads_mcp.guardrails import PlanStore

CUR = {"customer": {"id": 9876543210, "currency_code": "USD"}}


def _no_bid_account(kw_bid=0, ag_bid=0):
    c = harness.stub_standard_account(harness.FakeGoogleAdsClient())
    c.stub("ad_group_criterion", [{
        "ad_group_criterion": {"criterion_id": 401, "status": "ENABLED",
                               "keyword": {"text": "kw", "match_type": "EXACT"},
                               "cpc_bid_micros": kw_bid},
        "ad_group": {"id": 201}, "campaign": {"id": 222}, **CUR}])
    c.stub("ad_group", [{
        "ad_group": {"id": 201, "name": "AG", "status": "ENABLED",
                     "cpc_bid_micros": ag_bid}, "campaign": {"id": 222}, **CUR}])
    return c


def test_first_bid_allowed_under_the_absolute_ceiling(tmp_path):
    client = _no_bid_account()
    server = harness.build_rw_server(tmp_path, client=client,
                                     env={"ADS_MCP_MAX_FIRST_BID": "5"})
    payload = harness.expect_ok(harness.call(
        server, "update_keyword_bid",
        {"ad_group_id": "201", "criterion_id": "401", "current_bid": 0, "new_bid": 3.0}))
    assert payload["plan"]["id"]


def test_first_bid_above_the_ceiling_refused_with_it_named(tmp_path):
    client = _no_bid_account()
    server = harness.build_rw_server(tmp_path, client=client,
                                     env={"ADS_MCP_MAX_FIRST_BID": "5"})
    err = harness.expect_error(
        server, "update_keyword_bid",
        {"ad_group_id": "201", "criterion_id": "401", "current_bid": 0, "new_bid": 9.0},
        code="FIRST_BID_CAP_EXCEEDED")
    assert "ADS_MCP_MAX_FIRST_BID" in err["message"] and "5" in err["message"]


def test_unset_ceiling_refuses_and_names_the_variable(tmp_path):
    client = _no_bid_account()
    env = harness.rw_env(tmp_path)
    del env["ADS_MCP_MAX_FIRST_BID"]
    server = harness.build_server(tmp_path, client=client, env=env)
    err = harness.expect_error(
        server, "update_keyword_bid",
        {"ad_group_id": "201", "criterion_id": "401", "current_bid": 0, "new_bid": 1.0},
        code="GUARDRAIL_CAP_UNSET")
    assert "ADS_MCP_MAX_FIRST_BID" in err["message"]


def test_a_failed_lookup_is_never_read_as_no_baseline(tmp_path):
    """The defect class that was re-opened twice: caller-supplied ids decide
    which lookup runs, so a failed lookup must not silently downgrade to the
    (typically far higher) absolute ceiling."""
    client = _no_bid_account()
    client.stub_error(core_exceptions.ServiceUnavailable("read failed"))
    server = harness.build_rw_server(
        tmp_path, client=client,
        env={"ADS_MCP_MAX_FIRST_BID": "5", "ADS_MCP_RETRY_BASE_SECONDS": "0.01"})
    harness.expect_error(
        server, "update_keyword_bid",
        {"ad_group_id": "201", "criterion_id": "401", "current_bid": 0, "new_bid": 4.0},
        code="BID_BASELINE_UNVERIFIED")
    assert client.live_mutations() == []


def test_keyword_inherits_the_ad_group_bid_rather_than_counting_as_first(tmp_path):
    client = _no_bid_account(kw_bid=0, ag_bid=1000000)  # $1.00 inherited
    server = harness.build_rw_server(
        tmp_path, client=client,
        env={"ADS_MCP_MAX_FIRST_BID": "50", "ADS_MCP_MAX_BID_INCREASE_PCT": "50"})
    harness.expect_error(
        server, "update_keyword_bid",
        {"ad_group_id": "201", "criterion_id": "401", "current_bid": 1.0, "new_bid": 2.0},
        code="BID_CAP_EXCEEDED")


@pytest.mark.parametrize("kw,ag", [(1200000, 0), (0, 1000000)])
def test_decreases_never_refused_when_any_bid_is_known(tmp_path, kw, ag):
    client = _no_bid_account(kw_bid=kw, ag_bid=ag)
    server = harness.build_rw_server(tmp_path, client=client)
    payload = harness.call(
        server, "update_keyword_bid",
        {"ad_group_id": "201", "criterion_id": "401", "current_bid": 5.0, "new_bid": 0.10})
    assert "error" not in payload, f"a bid cut was refused: {payload.get('error')}"


def test_no_caller_value_selects_the_ceiling(tmp_path):
    """A huge claimed current_bid must not turn a first-bid into a
    percentage-capped increase, nor raise either ceiling."""
    client = _no_bid_account()
    server = harness.build_rw_server(tmp_path, client=client,
                                     env={"ADS_MCP_MAX_FIRST_BID": "5"})
    for claimed in (100000.0, 1e6, -5.0):
        harness.expect_error(
            server, "update_keyword_bid",
            {"ad_group_id": "201", "criterion_id": "401",
             "current_bid": claimed, "new_bid": 99999.0},
            code="FIRST_BID_CAP_EXCEEDED")


def test_creation_paths_use_the_first_bid_ceiling(tmp_path, account_client):
    server = harness.build_rw_server(tmp_path, client=account_client,
                                     env={"ADS_MCP_MAX_FIRST_BID": "5"})
    ok = harness.call(server, "create_ad_group",
                      {"campaign_id": "222", "ad_group_name": "AG", "cpc_bid_micros": 3000000})
    assert "error" not in ok, f"an in-ceiling first bid on creation was refused: {ok.get('error')}"
    harness.expect_error(server, "create_ad_group",
                         {"campaign_id": "222", "ad_group_name": "AG2",
                          "cpc_bid_micros": 9000000},
                         code="FIRST_BID_CAP_EXCEEDED")


def test_first_bid_refusals_are_audited(tmp_path):
    client = _no_bid_account()
    server = harness.build_rw_server(tmp_path, client=client,
                                     env={"ADS_MCP_MAX_FIRST_BID": "5"})
    harness.expect_error(
        server, "update_keyword_bid",
        {"ad_group_id": "201", "criterion_id": "401", "current_bid": 0, "new_bid": 9.0},
        code="FIRST_BID_CAP_EXCEEDED")
    events = [r for r in harness.read_audit_records(tmp_path) if r["event"] == "refused"]
    assert events, "a guardrail refusal left no audit record (F012)"
    assert events[-1]["outcome"] == "FIRST_BID_CAP_EXCEEDED"


# Resume amendment (2026-09-12): the account is the only bid authority.

def _stage(server, tool, args):
    plan = harness.expect_ok(harness.call(server, tool, args))["plan"]
    harness.expect_ok(harness.call(
        server, "confirm_and_apply", {"plan_id": plan["id"], "dry_run": True}))
    return plan["id"]


def _apply(server, client, pid, micros, *, create=False):
    applied = harness.expect_ok(harness.call(
        server, "confirm_and_apply", {"plan_id": pid, "dry_run": False}))
    assert applied["applied"] is True
    bids = [getattr(op.create if create else op.update, "cpc_bid_micros", None)
            for call in client.live_mutations()
            for op in getattr(call.request, "operations", [])]
    assert micros in bids, f"the approved {micros} micro bid never reached the API: {bids}"


def _refused(tmp_path, server, client, tool, args, code):
    err = harness.expect_error(server, tool, args, code=code)
    assert client.live_mutations() == []
    records = harness.read_audit_records(tmp_path)
    assert any(r["event"] == "refused" and r.get("outcome") == code for r in records), (
        f"{code} refusal was not audited")
    return err


@pytest.mark.parametrize("claimed", [1.0, 50.0, 0.0, -5.0], ids=["lower", "higher", "zero", "negative"])
@pytest.mark.parametrize("new_bid", [1.6, 1.8], ids=["within", "at-ceiling"])
def test_account_ceiling_is_caller_independent_through_apply(tmp_path, claimed, new_bid):
    client = _no_bid_account(kw_bid=1200000, ag_bid=1000000)
    server = harness.build_rw_server(tmp_path, client=client,
                                     env={"ADS_MCP_MAX_BID_INCREASE_PCT": "50"})
    pid = _stage(server, "update_keyword_bid", {
        "ad_group_id": "201", "criterion_id": "401",
        "current_bid": claimed, "new_bid": new_bid})
    assert client.live_mutations() == []
    _apply(server, client, pid, round(new_bid * 1000000))


@pytest.mark.parametrize("claimed", [1.0, 50.0, 0.0, -5.0], ids=["lower", "higher", "zero", "negative"])
@pytest.mark.parametrize("at_apply", [False, True], ids=["staging", "application"])
def test_caller_cannot_raise_account_ceiling_at_either_gate(tmp_path, claimed, at_apply):
    client = _no_bid_account(kw_bid=1200000, ag_bid=1000000)
    store = PlanStore(clock=harness.FakeClock())
    args = {"ad_group_id": "201", "criterion_id": "401",
            "current_bid": claimed, "new_bid": 1.9}
    if at_apply:
        loose = harness.build_rw_server(tmp_path, client=client, plan_store=store,
                                        env={"ADS_MCP_MAX_BID_INCREASE_PCT": "100"})
        pid = _stage(loose, "update_keyword_bid", args)
        tool, args = "confirm_and_apply", {"plan_id": pid, "dry_run": False}
    else:
        tool = "update_keyword_bid"
    tight = harness.build_rw_server(tmp_path, client=client, plan_store=store,
                                    env={"ADS_MCP_MAX_BID_INCREASE_PCT": "50"})
    err = _refused(tmp_path, tight, client, tool, args, "BID_CAP_EXCEEDED")
    assert "ADS_MCP_MAX_BID_INCREASE_PCT" in err["message"] and "50" in err["message"]


@pytest.mark.parametrize("kw,ag", [(1200000, 1000000), (0, 1200000)])
def test_real_decrease_applies_with_percentage_cap_unset(tmp_path, kw, ag):
    client = _no_bid_account(kw_bid=kw, ag_bid=ag)
    server = harness.build_rw_server(tmp_path, client=client,
                                     env={"ADS_MCP_MAX_BID_INCREASE_PCT": None})
    pid = _stage(server, "update_keyword_bid", {
        "ad_group_id": "201", "criterion_id": "401", "current_bid": 0.1, "new_bid": 1.1})
    _apply(server, client, pid, 1100000)


def _existing_args(kind, micros=1600000):
    if kind == "ad-group":
        return "update_ad_group", {"ad_group_id": "201", "cpc_bid_micros": micros}
    return "update_keyword_bid", {"ad_group_id": "201", "criterion_id": "401",
                                  "current_bid": 1.2, "new_bid": micros / 1000000}


class _ResourceReadFailure(harness.FakeGoogleAdsClient):
    failing_resource = None

    def _do_search(self, service, method, args, kwargs):
        rows = super()._do_search(service, method, args, kwargs)
        match = harness._FROM_RE.search(self.searches[-1].query)
        if match and match.group(1) == self.failing_resource:
            raise core_exceptions.ServiceUnavailable("synthetic account lookup unavailable")
        return rows


def _existing_client(kind):
    client = _no_bid_account(kw_bid=0 if kind == "inherited" else 1200000, ag_bid=1200000)
    failing = _ResourceReadFailure()
    harness.stub_standard_account(failing)
    for resource, rows in client._responses.items():
        failing.stub_rows(resource, rows)
    return failing


def _alter_baseline(client, kind, fault):
    resource = "ad_group_criterion" if kind == "keyword" else "ad_group"
    if fault == "failed":
        client.failing_resource = resource
    elif fault == "empty":
        client.stub(resource, [])
    else:
        rows = client._responses[resource]
        row = rows[0]
        entity = row.ad_group_criterion if kind == "keyword" else row.ad_group
        if fault == "changed":
            entity.cpc_bid_micros = 500000
        elif kind == "keyword":
            # Same criterion id in a DIFFERENT ad group is not our keyword.
            row.ad_group.id = 999
        else:
            entity.id = 999


@pytest.mark.parametrize("kind", ["keyword", "inherited", "ad-group"])
@pytest.mark.parametrize("fault", ["failed", "empty", "mismatched"])
def test_existing_resource_must_be_verified_before_selecting_any_cap(tmp_path, kind, fault):
    client = _existing_client(kind)
    _alter_baseline(client, kind, fault)
    server = harness.build_rw_server(tmp_path, client=client,
                                     env={"ADS_MCP_RETRY_BASE_SECONDS": "0.001"})
    tool, args = _existing_args(kind)
    _refused(tmp_path, server, client, tool, args, "BID_BASELINE_UNVERIFIED")


@pytest.mark.parametrize("kind", ["keyword", "inherited", "ad-group"])
@pytest.mark.parametrize("fault", ["changed", "failed", "empty", "mismatched"])
def test_application_rereads_existing_account_baseline(tmp_path, kind, fault):
    client = _existing_client(kind)
    server = harness.build_rw_server(tmp_path, client=client,
                                     env={"ADS_MCP_MAX_BID_INCREASE_PCT": "50",
                                          "ADS_MCP_RETRY_BASE_SECONDS": "0.001"})
    tool, args = _existing_args(kind)
    pid = _stage(server, tool, args)
    searches_before = len(client.searches)
    _alter_baseline(client, kind, fault)
    code = "BID_CAP_EXCEEDED" if fault == "changed" else "BID_BASELINE_UNVERIFIED"
    _refused(tmp_path, server, client, "confirm_and_apply",
             {"plan_id": pid, "dry_run": False}, code)
    assert len(client.searches) > searches_before, "apply reused the preview's account snapshot"


@pytest.mark.parametrize("kind", ["keyword", "ad-group"])
def test_existing_entity_with_verified_zero_bid_can_set_first_bid_and_apply(tmp_path, kind):
    client = _no_bid_account()
    server = harness.build_rw_server(tmp_path, client=client,
                                     env={"ADS_MCP_MAX_FIRST_BID": "5",
                                          "ADS_MCP_MAX_BID_INCREASE_PCT": None})
    tool, args = _existing_args(kind, 3000000)
    pid = _stage(server, tool, args)
    _apply(server, client, pid, 3000000)


def _creation_args(kind, micros):
    keyword = {"text": "synthetic keyword", "match_type": "EXACT", "cpc_bid_micros": micros}
    if kind == "ad-group":
        return "create_ad_group", {"campaign_id": "222", "ad_group_name": "New AG",
                                   "cpc_bid_micros": micros}
    if kind == "keyword":
        return "draft_keywords", {"ad_group_id": "201", "keywords": [keyword]}
    return "draft_campaign", {"campaign_name": "New campaign", "daily_budget": 10.0, "contains_eu_political_advertising": False,
                               "bidding_strategy": "MANUAL_CPC", "geo_target_ids": [],
                               "language_ids": [], "ad_group_name": "New AG",
                               "keywords": [keyword]}


@pytest.mark.parametrize("kind", ["ad-group", "keyword", "campaign-keyword"])
@pytest.mark.parametrize("micros", [3000000, 9000000])
def test_creation_bid_uses_absolute_cap_and_reaches_transport(tmp_path, kind, micros):
    client = _no_bid_account()
    # A CREATE proves the new entity has no prior bid. Existing keyword
    # parent reads still succeed; there is no pre-existing child to fetch.
    client.stub("ad_group_criterion", [])
    server = harness.build_rw_server(tmp_path, client=client,
                                     env={"ADS_MCP_MAX_FIRST_BID": "5",
                                          "ADS_MCP_MAX_BID_INCREASE_PCT": None})
    tool, args = _creation_args(kind, micros)
    if micros > 5000000:
        _refused(tmp_path, server, client, tool, args, "FIRST_BID_CAP_EXCEEDED")
    else:
        pid = _stage(server, tool, args)
        _apply(server, client, pid, micros, create=True)


@pytest.mark.parametrize("micros", [1600000, 1900000])
def test_created_keyword_inherits_existing_parent_cap(tmp_path, micros):
    client = _no_bid_account(ag_bid=1200000)
    client.stub("ad_group_criterion", [])
    server = harness.build_rw_server(tmp_path, client=client,
                                     env={"ADS_MCP_MAX_FIRST_BID": "50",
                                          "ADS_MCP_MAX_BID_INCREASE_PCT": "50"})
    tool, args = _creation_args("keyword", micros)
    if micros > 1800000:
        _refused(tmp_path, server, client, tool, args, "BID_CAP_EXCEEDED")
    else:
        pid = _stage(server, tool, args)
        _apply(server, client, pid, micros, create=True)


def test_created_keyword_parent_is_verified_again_at_apply(tmp_path):
    client = _no_bid_account(ag_bid=1200000)
    server = harness.build_rw_server(tmp_path, client=client,
                                     env={"ADS_MCP_MAX_BID_INCREASE_PCT": "50"})
    tool, args = _creation_args("keyword", 1600000)
    pid = _stage(server, tool, args)
    client.stub("ad_group", [])
    _refused(tmp_path, server, client, "confirm_and_apply",
             {"plan_id": pid, "dry_run": False}, "BID_BASELINE_UNVERIFIED")


@pytest.mark.parametrize("kind", ["keyword", "ad-group"])
def test_first_bid_preview_does_not_override_a_new_account_baseline(tmp_path, kind):
    client = _no_bid_account()
    server = harness.build_rw_server(tmp_path, client=client,
                                     env={"ADS_MCP_MAX_FIRST_BID": "5",
                                          "ADS_MCP_MAX_BID_INCREASE_PCT": "50"})
    tool, args = _existing_args(kind, 3000000)
    pid = _stage(server, tool, args)
    resource = "ad_group_criterion" if kind == "keyword" else "ad_group"
    row = client._responses[resource][0]
    entity = row.ad_group_criterion if kind == "keyword" else row.ad_group
    entity.cpc_bid_micros = 1000000
    _refused(tmp_path, server, client, "confirm_and_apply",
             {"plan_id": pid, "dry_run": False}, "BID_CAP_EXCEEDED")


@pytest.mark.parametrize("kind", ["ad-group", "keyword", "campaign-keyword"])
def test_creation_first_bid_cap_is_rechecked_with_apply_configuration(tmp_path, kind):
    client = _no_bid_account()
    store = PlanStore(clock=harness.FakeClock())
    loose = harness.build_rw_server(tmp_path, client=client, plan_store=store,
                                    env={"ADS_MCP_MAX_FIRST_BID": "5"})
    tool, args = _creation_args(kind, 3000000)
    pid = _stage(loose, tool, args)
    tight = harness.build_rw_server(tmp_path, client=client, plan_store=store,
                                    env={"ADS_MCP_MAX_FIRST_BID": "2"})
    err = _refused(tmp_path, tight, client, "confirm_and_apply",
                   {"plan_id": pid, "dry_run": False}, "FIRST_BID_CAP_EXCEEDED")
    assert "ADS_MCP_MAX_FIRST_BID" in err["message"] and "2" in err["message"]
