"""F018 — Keyword Planner: discovery and forecasts, persisting nothing."""

import pytest

import harness
from tool_catalog import READ_TOOLS

IDEAS = {"results": [
    {"text": "baseball training harness",
     "keyword_idea_metrics": {"avg_monthly_searches": 2400, "competition": "MEDIUM"}},
    {"text": "pitching velocity trainer",
     "keyword_idea_metrics": {"avg_monthly_searches": 880, "competition": "LOW"}},
]}
FORECAST = {"campaign_forecast_metrics": {
    "clicks": 480.0,
    "cost_micros": 624000000, "average_cpc_micros": 1300000}}


def _server(tmp_path, client, env=None):
    client.stub_planner("generate_keyword_ideas", harness.planner_response(IDEAS))
    client.stub_planner("generate_keyword_forecast_metrics", harness.planner_response(FORECAST))
    return harness.build_server(tmp_path, client=client, env=env)


def test_both_tools_are_read_tools(tmp_path, account_client):
    server = _server(tmp_path, account_client)
    names = harness.tool_names(server)
    assert {"discover_keywords", "get_keyword_forecasts"} <= names
    assert {"discover_keywords", "get_keyword_forecasts"} <= READ_TOOLS


def test_discover_keywords_returns_ideas(tmp_path, account_client):
    server = _server(tmp_path, account_client)
    payload = harness.expect_ok(
        harness.call(server, "discover_keywords", {"seed_keywords": ["baseball harness"]})
    )
    ideas = payload["ideas"]
    assert [i["keyword"] for i in ideas] == [
        "baseball training harness", "pitching velocity trainer"]
    assert ideas[0]["avg_monthly_searches"] == 2400
    assert ideas[0]["competition"] == "MEDIUM"


def test_discover_accepts_a_page_url(tmp_path, account_client):
    server = _server(tmp_path, account_client)
    harness.expect_ok(
        harness.call(server, "discover_keywords", {"page_url": "https://example.com/harness"})
    )
    assert account_client.planner_calls(), "no KeywordPlanIdeaService call was made"


def test_forecasts_return_typed_metrics_with_money(tmp_path, account_client):
    server = _server(tmp_path, account_client)
    payload = harness.expect_ok(harness.call(
        server, "get_keyword_forecasts",
        {"keywords": ["baseball harness"],
         "date_range_start": "2026-09-01", "date_range_end": "2026-09-30"}))
    f = payload["forecast"]
    assert f["impressions"] is None
    assert isinstance(f["clicks"], (int, float)) and f["clicks"] == 480.0
    harness.assert_money(f["cost"])
    harness.assert_money(f["average_cpc"])
    assert payload["window"] == {"start": "2026-09-01", "end": "2026-09-30"}


def test_neither_tool_persists_anything(tmp_path, account_client):
    """The KeywordPlanService route creates a real KeywordPlan in the account.
    Read-only mode gates registration, not what a read tool does internally —
    so this is what keeps 'zero spend risk by construction' true."""
    server = _server(tmp_path, account_client)
    harness.call(server, "discover_keywords", {"seed_keywords": ["x"]})
    harness.call(server, "get_keyword_forecasts", {"keywords": ["x"]})
    assert account_client.mutations == [], (
        f"a keyword-planner tool issued mutate calls: "
        f"{[(m.service, m.method) for m in account_client.mutations]}"
    )
    methods = {m for m, _a, _k in account_client.planner_calls()}
    assert methods <= {"generate_keyword_ideas", "generate_keyword_forecast_metrics"}, (
        f"only the non-persisting idea-service methods may be used, saw {methods}"
    )


def test_neither_tool_produces_a_plan(tmp_path, account_client):
    server = _server(tmp_path, account_client)
    for tool, args in (("discover_keywords", {"seed_keywords": ["x"]}),
                       ("get_keyword_forecasts", {"keywords": ["x"]})):
        assert "plan" not in harness.call(server, tool, args)


def test_ideas_are_bounded_with_a_page_token(tmp_path, account_client):
    big = {"results": [
        {"text": f"kw {i}", "keyword_idea_metrics": {
            "avg_monthly_searches": i, "competition": "LOW"}} for i in range(25)]}
    account_client.stub_planner("generate_keyword_ideas", harness.planner_response(big))
    server = harness.build_server(tmp_path, client=account_client,
                                  env={"ADS_MCP_ROW_LIMIT": "10"})
    page1 = harness.expect_ok(
        harness.call(server, "discover_keywords", {"seed_keywords": ["x"]}))
    assert len(page1["ideas"]) == 10
    assert page1.get("next_page_token")


@pytest.mark.parametrize("tool,args,code", [
    ("discover_keywords", {}, "MISSING_ARGUMENT"),
    ("discover_keywords", {"seed_keywords": []}, "MISSING_ARGUMENT"),
    ("get_keyword_forecasts", {"keywords": []}, "MISSING_ARGUMENT"),
    ("get_keyword_forecasts",
     {"keywords": ["x"], "date_range_start": "2026-09-30",
      "date_range_end": "2026-09-01"}, "INVALID_WINDOW"),
])
def test_adversarial_input_named_before_any_api_call(tmp_path, account_client, tool, args, code):
    server = _server(tmp_path, account_client)
    account_client._planner_calls.clear()
    harness.expect_error(server, tool, args, code=code)
    assert not account_client.planner_calls(), "invalid input reached the API"


def test_customer_id_is_honoured(tmp_path, account_client):
    server = _server(tmp_path, account_client)
    payload = harness.expect_ok(harness.call(
        server, "discover_keywords",
        {"seed_keywords": ["x"], "customer_id": harness.OTHER_CUSTOMER_ID}))
    assert payload["customer_id"] == harness.OTHER_CUSTOMER_ID
