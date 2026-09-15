"""F017 — Multi-account reads under an MCC login."""

import pytest

import harness
from tool_catalog import MUTATION_TOOLS, READ_TOOLS

OTHER = harness.OTHER_CUSTOMER_ID
WINDOW = {"date_range_start": "2026-07-01", "date_range_end": "2026-07-31"}

# Reads that query account data. health_check and list_accounts are about the
# configured login itself and take no customer_id override.
ACCOUNT_SCOPED_READS = sorted(READ_TOOLS - {"health_check", "list_accounts"})

ARGS = {
    "run_gaql": {"query": "SELECT campaign.id FROM campaign"},
    "get_shopping_performance": {"campaign_id": "111", **WINDOW},
    "get_listing_groups": {"campaign_id": "111"},
    "get_product_status": {"campaign_id": "111"},
    "get_change_history": {"date_range_start": "2026-07-28", "date_range_end": "2026-08-01"},
    "search_geo_targets": {"query": "United States"},
    "get_policy_issues": {"mode": "summary"},
    "discover_keywords": {"seed_keywords": ["harness"]},
    "get_keyword_forecasts": {"keywords": ["harness"]},
}


def _args(tool):
    return dict(ARGS.get(tool, WINDOW if "performance" in tool or "terms" in tool else {}))


@pytest.mark.parametrize("tool", ACCOUNT_SCOPED_READS)
def test_every_account_read_accepts_customer_id(tmp_path, account_client, tool):
    server = harness.build_server(tmp_path, client=account_client)
    payload = harness.call(server, tool, {**_args(tool), "customer_id": OTHER})
    assert "error" not in payload, f"{tool} refused an explicit customer_id: {payload.get('error')}"
    assert payload.get("customer_id") == OTHER, (
        f"{tool} must echo the account it actually queried, got {payload.get('customer_id')!r}"
    )


@pytest.mark.parametrize("tool", ["get_campaign_performance", "get_search_terms", "get_policy_issues"])
def test_search_is_issued_for_the_named_account(tmp_path, account_client, tool):
    server = harness.build_server(tmp_path, client=account_client)
    harness.call(server, tool, {**_args(tool), "customer_id": harness.OTHER_CUSTOMER_ID})
    assert account_client.searches, f"{tool} made no search"
    assert all(s.customer_id == OTHER for s in account_client.searches), (
        f"{tool} queried {[s.customer_id for s in account_client.searches]}, expected {OTHER}"
    )


def test_dashed_and_undashed_are_the_same_account(tmp_path, account_client):
    server = harness.build_server(tmp_path, client=account_client)
    a = harness.call(server, "get_campaign_performance", {**WINDOW, "customer_id": "555-666-7777"})
    b = harness.call(server, "get_campaign_performance", {**WINDOW, "customer_id": "5556667777"})
    assert a == b and a["customer_id"] == OTHER


def test_omitting_customer_id_uses_the_configured_account(tmp_path, account_client):
    server = harness.build_server(tmp_path, client=account_client)
    payload = harness.expect_ok(harness.call(server, "get_campaign_performance", dict(WINDOW)))
    assert payload["customer_id"] == harness.CUSTOMER_ID
    assert account_client.searches[-1].customer_id == harness.CUSTOMER_ID


@pytest.mark.parametrize("bad", ["garbage", "12-34", "123456789012", ""])
def test_malformed_customer_id_refused_before_any_api_call(tmp_path, account_client, bad):
    server = harness.build_server(tmp_path, client=account_client)
    payload = harness.call(server, "get_campaign_performance", {**WINDOW, "customer_id": bad})
    err = harness.error_of(payload)
    if bad:
        assert bad in err["message"]
    assert not account_client.searches, "a malformed id reached the API"


def test_cross_account_read_sends_the_login_customer_header(tmp_path, account_client):
    server = harness.build_server(tmp_path, client=account_client)
    harness.call(server, "get_campaign_performance", {**WINDOW, "customer_id": OTHER})
    assert account_client.login_customer_id == harness.LOGIN_CUSTOMER_ID, (
        "cross-account reads need the MCC login-customer header; without it the "
        "API answers USER_PERMISSION_DENIED"
    )


def test_unreachable_account_is_named_not_raw(tmp_path, account_client):
    from google.api_core import exceptions as core_exceptions

    account_client.stub_error(core_exceptions.PermissionDenied("USER_PERMISSION_DENIED"))
    server = harness.build_server(tmp_path, client=account_client)
    err = harness.expect_error(
        server, "get_campaign_performance", {**WINDOW, "customer_id": OTHER},
        code="ACCOUNT_NOT_ACCESSIBLE",
    )
    assert OTHER in err["message"], f"the inaccessible account must be named: {err['message']}"


def test_writes_still_refuse_a_foreign_customer_id(tmp_path, account_client):
    server = harness.build_rw_server(tmp_path, client=account_client)
    for tool in ("update_campaign", "pause_entity", "remove_entity"):
        args = {"update_campaign": {"campaign_id": "111", "daily_budget": 20.0},
                "pause_entity": {"entity_type": "campaign", "entity_id": "111"},
                "remove_entity": {"entity_type": "ad", "entity_id": "201~901"}}[tool]
        harness.expect_error(server, tool, {**args, "customer_id": OTHER},
                             code="PLAN_CUSTOMER_MISMATCH")
    assert account_client.live_mutations() == []


SCOPED = ["get_campaign_performance", "get_ad_performance", "get_keyword_performance",
          "get_search_terms", "get_geo_performance"]


@pytest.mark.parametrize("tool", SCOPED)
def test_campaign_scoping_filters_server_side(tmp_path, account_client, tool):
    server = harness.build_server(tmp_path, client=account_client)
    payload = harness.expect_ok(harness.call(server, tool, {**WINDOW, "campaign_id": "222"}))
    assert payload.get("campaign_id") == "222", "the filter must be echoed"
    query = account_client.searches[-1].query
    assert "222" in query, f"campaign_id must filter in GAQL, not client-side: {query}"


@pytest.mark.parametrize("tool", SCOPED)
def test_unknown_campaign_returns_empty_not_whole_account(tmp_path, account_client, tool):
    server = harness.build_server(tmp_path, client=account_client)
    payload = harness.expect_ok(harness.call(server, tool, {**WINDOW, "campaign_id": "999999"}))
    rows = next(v for k, v in payload.items() if isinstance(v, list))
    assert rows == [], (
        f"{tool} returned {len(rows)} rows for an unmatched campaign — a silent "
        "whole-account answer is the failure mode this closes"
    )
    assert payload.get("campaign_id") == "999999"
