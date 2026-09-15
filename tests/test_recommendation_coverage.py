"""F021 — Recommendation spend coverage without blanket refusal."""

import pytest

import harness

CUR = {"customer": {"id": 9876543210, "currency_code": "USD"}}
RES = f"customers/{harness.CUSTOMER_ID}/recommendations/42"


def _rec(rec_type, detail):
    return {"recommendation": {"resource_name": RES, "type_": rec_type,
                               "dismissed": False,
                               "campaign": f"customers/{harness.CUSTOMER_ID}/campaigns/111",
                               **detail}, **CUR}


def _server(tmp_path, row, cap="100"):
    client = harness.stub_standard_account(harness.FakeGoogleAdsClient())
    client.stub("recommendation", [row])
    return client, harness.build_rw_server(
        tmp_path, client=client, env={"ADS_MCP_MAX_DAILY_BUDGET": cap})


def test_nested_options_budget_is_read(tmp_path):
    row = _rec("TARGET_CPA_OPT_IN", {"target_cpa_opt_in_recommendation": {
        "recommended_target_cpa_micros": 20000000,
        "options": [{"required_campaign_budget_amount_micros": 65000000}]}})
    _c, server = _server(tmp_path, row)
    payload = harness.call(server, "apply_recommendation", {"recommendation_id": "42"})
    assert "error" not in payload, (
        f"a $65/day requirement under a $100 cap must yield a plan: {payload.get('error')}"
    )
    assert payload["plan"]["id"]


def test_the_maximum_option_budget_is_the_figure_compared(tmp_path):
    """Budget caps compare daily budgets, not unrelated spend values."""
    row = _rec("TARGET_CPA_OPT_IN", {"target_cpa_opt_in_recommendation": {
        "recommended_target_cpa_micros": 20000000,
        "options": [{"required_campaign_budget_amount_micros": 65000000},
                    {"required_campaign_budget_amount_micros": 5000000000}]}})
    client, server = _server(tmp_path, row)
    err = harness.expect_error(server, "apply_recommendation",
                               {"recommendation_id": "42"}, code="BUDGET_CAP_EXCEEDED")
    assert "100" in err["message"]
    assert client.live_mutations() == []


def test_a_cpa_target_is_never_treated_as_a_daily_budget(tmp_path):
    row = _rec("TARGET_CPA_OPT_IN", {"target_cpa_opt_in_recommendation": {
        "recommended_target_cpa_micros": 150000000}})  # $150 CPA, no budget named
    _c, server = _server(tmp_path, row)
    err = harness.error_of(harness.call(server, "apply_recommendation",
                                        {"recommendation_id": "42"}))
    assert err["code"] != "BUDGET_CAP_EXCEEDED", (
        "a $150 cost-per-acquisition target is not a $150 daily budget"
    )
    assert err["code"] == "SPEND_IMPACT_UNBOUNDED"


def test_over_cap_budget_refused_with_the_cap_named(tmp_path):
    row = _rec("CAMPAIGN_BUDGET", {"campaign_budget_recommendation": {
        "recommended_budget_amount_micros": 5000000000}})
    _c, server = _server(tmp_path, row)
    err = harness.expect_error(server, "apply_recommendation",
                               {"recommendation_id": "42"}, code="BUDGET_CAP_EXCEEDED")
    assert "ADS_MCP_MAX_DAILY_BUDGET" in err["message"]


def test_under_cap_yields_a_plan_not_an_apply(tmp_path):
    row = _rec("CAMPAIGN_BUDGET", {"campaign_budget_recommendation": {
        "recommended_budget_amount_micros": 65000000}})
    client, server = _server(tmp_path, row)
    payload = harness.expect_ok(
        harness.call(server, "apply_recommendation", {"recommendation_id": "42"}))
    assert payload["plan"]["id"]
    assert client.live_mutations() == [], "nothing executes without confirm_and_apply"


def test_options_budget_rechecked_at_apply(tmp_path):
    from ads_mcp.guardrails import PlanStore

    row = _rec("TARGET_CPA_OPT_IN", {"target_cpa_opt_in_recommendation": {
        "options": [{"required_campaign_budget_amount_micros": 65000000}]}})
    client = harness.stub_standard_account(harness.FakeGoogleAdsClient())
    client.stub("recommendation", [row])
    store = PlanStore(clock=harness.FakeClock())
    loose = harness.build_rw_server(tmp_path, client=client, plan_store=store,
                                    env={"ADS_MCP_MAX_DAILY_BUDGET": "1000"})
    plan = harness.expect_ok(
        harness.call(loose, "apply_recommendation", {"recommendation_id": "42"}))["plan"]
    harness.call(loose, "confirm_and_apply", {"plan_id": plan["id"], "dry_run": True})
    tight = harness.build_rw_server(tmp_path, client=client, plan_store=store,
                                    env={"ADS_MCP_MAX_DAILY_BUDGET": "50"})
    harness.expect_error(tight, "confirm_and_apply",
                         {"plan_id": plan["id"], "dry_run": False},
                         code="BUDGET_CAP_EXCEEDED")


@pytest.mark.parametrize("rec_type", [
    "MAXIMIZE_CONVERSION_VALUE_OPT_IN", "KEYWORD", "SHOPPING_ADD_PRODUCTS_TO_CAMPAIGN",
    "USE_BROAD_MATCH_KEYWORD", "UPGRADE_SMART_SHOPPING_CAMPAIGN_TO_PERFORMANCE_MAX",
])
def test_budgetless_spend_types_are_refused_by_name(tmp_path, rec_type):
    _c, server = _server(tmp_path, _rec(rec_type, {}))
    err = harness.expect_error(server, "apply_recommendation",
                               {"recommendation_id": "42"}, code="SPEND_IMPACT_UNBOUNDED")
    assert rec_type in err["message"], f"the refusal must name the type: {err['message']}"


def test_spend_neutral_types_still_apply(tmp_path):
    _c, server = _server(tmp_path, _rec("CALLOUT_ASSET", {}))
    payload = harness.call(server, "apply_recommendation", {"recommendation_id": "42"})
    assert "error" not in payload, (
        f"a callout-asset recommendation is harmless: {payload.get('error')}"
    )


def test_allowlist_names_all_exist_in_the_pinned_enum():
    from ads_mcp.tools import mutations

    names = getattr(mutations, "SPEND_NEUTRAL_TYPES", None)
    assert names, "the spend-neutral allowlist must be inspectable"
    from google.ads.googleads.v25.enums.types.recommendation_type import RecommendationTypeEnum

    real = {e.name for e in RecommendationTypeEnum.RecommendationType}
    assert set(names) <= real, f"allowlist names absent from the API: {sorted(set(names)-real)}"


def test_recommendation_refusals_are_audited(tmp_path):
    _c, server = _server(tmp_path, _rec("KEYWORD", {}))
    harness.expect_error(server, "apply_recommendation",
                         {"recommendation_id": "42"}, code="SPEND_IMPACT_UNBOUNDED")
    events = [r for r in harness.read_audit_records(tmp_path) if r["event"] == "refused"]
    assert events and events[-1]["outcome"] == "SPEND_IMPACT_UNBOUNDED"
