"""F006 — Curated reporting reads."""

import pytest

import harness

WINDOW_ARGS = {"date_range_start": "2026-07-01", "date_range_end": "2026-07-31"}

WINDOWED_TOOLS = {
    "get_campaign_performance": "campaigns",
    "get_ad_performance": "ads",
    "get_keyword_performance": "keywords",
    "get_search_terms": "search_terms",
    "get_geo_performance": "locations",
}


def _server(tmp_path, client, env=None, clock=None):
    return harness.build_server(tmp_path, client=client, env=env, clock=clock)


def _stub_geo_and_terms(client):
    cur = {"customer": {"id": 9876543210, "currency_code": "USD"}}
    client.stub(
        "search_term_view",
        [
            {
                "search_term_view": {"search_term": "buy acme"},
                "ad_group": {"id": 201},
                "campaign": {"id": 222},
                "metrics": {"impressions": 90, "clicks": 7, "cost_micros": 5200000, "conversions": 1.0},
                **cur,
            }
        ],
    )
    client.stub(
        "geographic_view",
        [
            {
                "geographic_view": {"country_criterion_id": 2840, "location_type": "LOCATION_OF_PRESENCE"},
                "campaign": {"id": 111},
                "metrics": {"impressions": 800, "clicks": 25, "cost_micros": 30000000, "conversions": 1.5},
                **cur,
            }
        ],
    )
    client.stub(
        "keyword_view",
        [
            {
                "ad_group_criterion": {
                    "criterion_id": 401,
                    "status": "ENABLED",
                    "keyword": {"text": "acme widgets", "match_type": "EXACT"},
                    "cpc_bid_micros": 1200000,
                },
                "ad_group": {"id": 201},
                "campaign": {"id": 222},
                "metrics": {"impressions": 250, "clicks": 10, "cost_micros": 7000000, "conversions": 0.0},
                **cur,
            }
        ],
    )
    return client


@pytest.mark.parametrize("tool,key", sorted(WINDOWED_TOOLS.items()))
def test_windowed_reports_echo_window_and_type_metrics(tmp_path, account_client, tool, key):
    _stub_geo_and_terms(account_client)
    server = _server(tmp_path, account_client)
    payload = harness.expect_ok(harness.call(server, tool, dict(WINDOW_ARGS)))
    assert payload["window"] == {"start": "2026-07-01", "end": "2026-07-31"}
    rows = payload[key]
    assert rows, f"{tool} returned no rows from the recorded fixture"
    for row in rows:
        assert isinstance(row["impressions"], int)
        assert isinstance(row["clicks"], int)
        assert isinstance(row["conversions"], float)
        harness.assert_money(row["cost"])


def test_last_n_days_window_uses_injected_clock(tmp_path, account_client):
    clock = harness.FakeClock()  # 2026-08-02T12:00:00Z
    server = _server(tmp_path, account_client, clock=clock)
    payload = harness.expect_ok(
        harness.call(server, "get_campaign_performance", {"last_n_days": 7})
    )
    assert payload["window"] == {"start": "2026-07-26", "end": "2026-08-01"}, (
        "last_n_days must be the N complete days ending yesterday (clock-derived)"
    )
    query = account_client.searches[-1].query
    assert "2026-07-26" in query and "2026-08-01" in query, (
        f"the GAQL window must match the echoed window: {query}"
    )


def test_micros_converted_to_decimal_with_currency(tmp_path, account_client):
    server = _server(tmp_path, account_client)
    payload = harness.expect_ok(
        harness.call(server, "get_campaign_performance", dict(WINDOW_ARGS))
    )
    row = next(r for r in payload["campaigns"] if r["campaign_id"] == "111")
    assert row["cost"] == "52.40 USD"
    assert row["average_cpc"] == "1.31 USD"
    assert row["daily_budget"] == "50.00 USD"


def test_enabled_only_filters_server_side(tmp_path, account_client):
    server = _server(tmp_path, account_client)
    payload = harness.expect_ok(
        harness.call(
            server, "get_campaign_performance", {**WINDOW_ARGS, "enabled_only": True}
        )
    )
    statuses = {row["status"] for row in payload["campaigns"]}
    assert statuses == {"ENABLED"}, f"enabled_only leaked: {statuses}"
    query = account_client.searches[-1].query.upper()
    assert "WHERE" in query and "ENABLED" in query, (
        "enabled_only must filter in the GAQL query (server-side), not client-side"
    )


def test_status_trio_exposed_together(tmp_path, account_client):
    server = _server(tmp_path, account_client)
    payload = harness.expect_ok(
        harness.call(server, "get_campaign_performance", dict(WINDOW_ARGS))
    )
    for row in payload["campaigns"]:
        assert "serving_status" in row
        assert "primary_status" in row
        assert isinstance(row["primary_status_reasons"], list)
    paused = next(r for r in payload["campaigns"] if r["campaign_id"] == "222")
    assert paused["primary_status_reasons"] == ["CAMPAIGN_PAUSED"]


def test_budget_and_bidding_strategy_in_one_call(tmp_path, account_client):
    server = _server(tmp_path, account_client)
    payload = harness.expect_ok(
        harness.call(server, "get_campaign_performance", dict(WINDOW_ARGS))
    )
    pmax = next(r for r in payload["campaigns"] if r["campaign_id"] == "111")
    assert pmax["daily_budget"] == "50.00 USD"
    assert pmax["bidding_strategy"]["type"] == "MAXIMIZE_CONVERSION_VALUE"
    assert pmax["bidding_strategy"]["target_roas"] == 3.5
    tcpa = next(r for r in payload["campaigns"] if r["campaign_id"] == "333")
    assert tcpa["bidding_strategy"]["target_cpa"] == "8.00 USD"


def test_read_parity_tools_return_shaped_rows(tmp_path, account_client):
    account_client.stub(
        "campaign_asset",
        [
            {
                "campaign_asset": {"field_type": "SITELINK", "status": "ENABLED"},
                "asset": {"id": 777001, "type_": "SITELINK", "sitelink_asset": {"link_text": "Spring Sale"}},
                "campaign": {"id": 222},
            }
        ],
    )
    server = _server(tmp_path, account_client)
    for tool, key, args in [
        ("list_accounts", "accounts", {}),
        ("get_conversion_actions", "conversion_actions", {}),
        ("get_negative_keywords", "negative_keywords", {}),
        ("list_extensions", "extensions", {}),
        ("search_geo_targets", "results", {"query": "United States"}),
    ]:
        payload = harness.expect_ok(harness.call(server, tool, args))
        assert payload.get(key), f"{tool} returned no {key} from the recorded fixture"


def test_row_heavy_reads_enforce_default_row_limit(tmp_path, account_client):
    cur = {"customer": {"id": 9876543210, "currency_code": "USD"}}
    account_client.stub(
        "search_term_view",
        [
            {
                "search_term_view": {"search_term": f"query {i}"},
                "ad_group": {"id": 201},
                "campaign": {"id": 222},
                "metrics": {"impressions": i, "clicks": 1, "cost_micros": 1000000, "conversions": 0.0},
                **cur,
            }
            for i in range(1500)
        ],
    )
    server = _server(tmp_path, account_client)  # no ADS_MCP_ROW_LIMIT set
    payload = harness.expect_ok(
        harness.call(server, "get_search_terms", dict(WINDOW_ARGS))
    )
    assert len(payload["search_terms"]) <= 1000, (
        "an unbounded 1500-row dump escaped the default row limit"
    )
    assert payload.get("next_page_token"), "truncation must be explicit, with a token"


def test_row_limit_env_and_token_walk(tmp_path, account_client):
    cur = {"customer": {"id": 9876543210, "currency_code": "USD"}}
    account_client.stub(
        "search_term_view",
        [
            {
                "search_term_view": {"search_term": f"query {i}"},
                "ad_group": {"id": 201},
                "campaign": {"id": 222},
                "metrics": {"impressions": i, "clicks": 1, "cost_micros": 1000000, "conversions": 0.0},
                **cur,
            }
            for i in range(25)
        ],
    )
    server = _server(tmp_path, account_client, env={"ADS_MCP_ROW_LIMIT": "10"})
    seen = []
    args = dict(WINDOW_ARGS)
    for _ in range(5):
        payload = harness.expect_ok(harness.call(server, "get_search_terms", args))
        seen.extend(r["search_term"] for r in payload["search_terms"])
        token = payload.get("next_page_token")
        if not token:
            break
        args = {**WINDOW_ARGS, "page_token": token}
    assert len(seen) == 25 and len(set(seen)) == 25, (
        f"token walk lost or duplicated rows: {len(seen)} seen"
    )


def test_empty_results_echo_window_not_error(tmp_path, account_client):
    account_client.stub("search_term_view", [])
    server = _server(tmp_path, account_client)
    payload = harness.expect_ok(
        harness.call(server, "get_search_terms", dict(WINDOW_ARGS))
    )
    assert payload["search_terms"] == []
    assert payload["window"] == {"start": "2026-07-01", "end": "2026-07-31"}
