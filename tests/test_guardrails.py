"""F011 — Spend guardrails that cannot be talked around."""

import pytest

import harness
from ads_mcp.guardrails import PlanStore
from tool_catalog import MUTATION_ARGS

PMAX_OVER = {**MUTATION_ARGS["create_pmax_campaign"], "daily_budget": 150.0}
DRAFT_OVER = {**MUTATION_ARGS["draft_campaign"], "daily_budget": 150.0}

BUDGET_RAISING_PATHS = [
    ("update_campaign", {"campaign_id": "111", "daily_budget": 150.0}),
    ("draft_campaign", DRAFT_OVER),
    ("create_pmax_campaign", PMAX_OVER),
]


def test_budget_cap_refused_at_plan_creation_every_path(tmp_path, account_client):
    server = harness.build_rw_server(tmp_path, client=account_client)  # cap 100
    for tool, args in BUDGET_RAISING_PATHS:
        err = harness.expect_error(server, tool, args, code="BUDGET_CAP_EXCEEDED")
        assert "ADS_MCP_MAX_DAILY_BUDGET" in err["message"], (
            f"{tool}: the cap must be named by its env var: {err['message']}"
        )
        assert "100" in err["message"], f"{tool}: cap value missing: {err['message']}"
    assert account_client.live_mutations() == []


def test_budget_cap_via_apply_recommendation(tmp_path, account_client):
    """A budget recommendation above the cap must be refused too."""
    account_client.stub(
        "recommendation",
        [
            {
                "recommendation": {
                    "resource_name": f"customers/{harness.CUSTOMER_ID}/recommendations/888",
                    "type": "CAMPAIGN_BUDGET",
                    "dismissed": False,
                    "campaign": f"customers/{harness.CUSTOMER_ID}/campaigns/111",
                    "campaign_budget_recommendation": {
                        "current_budget_amount_micros": 50000000,
                        "recommended_budget_amount_micros": 150000000,
                    },
                },
                "customer": {"id": 9876543210, "currency_code": "USD"},
            }
        ],
    )
    server = harness.build_rw_server(tmp_path, client=account_client)  # cap 100
    err = harness.expect_error(
        server,
        "apply_recommendation",
        {"recommendation_id": "888"},
        code="BUDGET_CAP_EXCEEDED",
    )
    assert "ADS_MCP_MAX_DAILY_BUDGET" in err["message"]
    assert account_client.live_mutations() == []


def test_budget_cap_rechecked_at_apply(tmp_path, account_client):
    """A plan admitted under a loose cap must re-check against the cap that
    is live at apply time."""
    store = PlanStore(clock=harness.FakeClock())
    loose = harness.build_rw_server(
        tmp_path,
        client=account_client,
        plan_store=store,
        env={"ADS_MCP_MAX_DAILY_BUDGET": "1000"},
    )
    payload = harness.expect_ok(
        harness.call(loose, "update_campaign", {"campaign_id": "111", "daily_budget": 150.0})
    )
    plan_id = payload["plan"]["id"]

    tight = harness.build_rw_server(
        tmp_path,
        client=account_client,
        plan_store=store,
        env={"ADS_MCP_MAX_DAILY_BUDGET": "100"},
    )
    err = harness.expect_error(
        tight,
        "confirm_and_apply",
        {"plan_id": plan_id, "dry_run": False},
        code="BUDGET_CAP_EXCEEDED",
    )
    assert "100" in err["message"]
    assert account_client.live_mutations() == []


def test_bid_increase_cap_named_and_enforced(tmp_path, account_client):
    server = harness.build_rw_server(
        tmp_path, client=account_client, env={"ADS_MCP_MAX_BID_INCREASE_PCT": "50"}
    )
    err = harness.expect_error(
        server,
        "update_keyword_bid",
        {"ad_group_id": "201", "criterion_id": "401", "current_bid": 1.0, "new_bid": 1.9},
        code="BID_CAP_EXCEEDED",
    )
    assert "50" in err["message"], f"configured limit not named: {err['message']}"
    within = harness.expect_ok(
        harness.call(
            server,
            "update_keyword_bid",
            {"ad_group_id": "201", "criterion_id": "401", "current_bid": 1.0, "new_bid": 1.4},
        )
    )
    assert within["plan"]["id"]


def test_ad_group_bid_increase_checked_against_current(tmp_path, account_client):
    """The current bid comes from the account (fixture ad_group 201 =
    1_000_000 micros), not from a caller-supplied figure alone."""
    server = harness.build_rw_server(
        tmp_path, client=account_client, env={"ADS_MCP_MAX_BID_INCREASE_PCT": "50"}
    )
    harness.expect_error(
        server,
        "update_ad_group",
        {"ad_group_id": "201", "cpc_bid_micros": 1600000},
        code="BID_CAP_EXCEEDED",
    )
    ok = harness.expect_ok(
        harness.call(
            server, "update_ad_group", {"ad_group_id": "201", "cpc_bid_micros": 1400000}
        )
    )
    assert ok["plan"]["id"]


def test_no_tool_parameter_can_raise_or_disable_caps(tmp_path, account_client):
    server = harness.build_rw_server(tmp_path, client=account_client)  # cap 100
    for extra in (
        {"max_daily_budget": 100000},
        {"override_budget_cap": True},
        {"bypass_guardrails": True},
    ):
        result = harness.call_result(
            server, "update_campaign", {"campaign_id": "111", "daily_budget": 150.0, **extra}
        )
        if not result.is_error:
            payload = harness.payload_of(result)
            assert "plan" not in payload, (
                f"parameter {extra} smuggled an over-cap budget into a plan"
            )
            harness.error_of(payload)
    assert account_client.live_mutations() == []


def test_unset_cap_refuses_spend_raising_operations(tmp_path, account_client):
    """Safe default: mutations enabled but no cap configured means no
    budget/bid raises at all — a fresh write-enabled install cannot spend."""
    env = harness.rw_env(tmp_path)
    del env["ADS_MCP_MAX_DAILY_BUDGET"]
    del env["ADS_MCP_MAX_BID_INCREASE_PCT"]
    server = harness.build_server(tmp_path, client=account_client, env=env)
    err = harness.expect_error(
        server,
        "update_campaign",
        {"campaign_id": "111", "daily_budget": 5.0},
        code="GUARDRAIL_CAP_UNSET",
    )
    assert "ADS_MCP_MAX_DAILY_BUDGET" in err["message"]
    err = harness.expect_error(
        server,
        "update_keyword_bid",
        {"ad_group_id": "201", "criterion_id": "401", "current_bid": 1.0, "new_bid": 1.3},
        code="GUARDRAIL_CAP_UNSET",
    )
    assert "ADS_MCP_MAX_BID_INCREASE_PCT" in err["message"]


def test_decreases_and_spend_neutral_unaffected(tmp_path, account_client):
    server = harness.build_rw_server(tmp_path, client=account_client)  # caps 100/100%
    # Decrease from 50.00 fixture budget: fine.
    dec = harness.expect_ok(
        harness.call(server, "update_campaign", {"campaign_id": "111", "daily_budget": 30.0})
    )
    assert dec["plan"]["id"]
    # Exactly at the cap: allowed ("above" is refused, equality is not).
    at_cap = harness.expect_ok(
        harness.call(server, "update_campaign", {"campaign_id": "111", "daily_budget": 100.0})
    )
    assert at_cap["plan"]["id"]
    # Bid decrease: fine regardless of percentage cap.
    bid_dec = harness.expect_ok(
        harness.call(
            server,
            "update_keyword_bid",
            {"ad_group_id": "201", "criterion_id": "401", "current_bid": 1.2, "new_bid": 0.6},
        )
    )
    assert bid_dec["plan"]["id"]
    # Spend-neutral mutation: fine.
    neutral = harness.expect_ok(
        harness.call(server, "pause_entity", {"entity_type": "campaign", "entity_id": "111"})
    )
    assert neutral["plan"]["id"]


@pytest.mark.parametrize("bad_budget", [-5.0, 0.0])
def test_nonpositive_budgets_rejected_named(tmp_path, account_client, bad_budget):
    server = harness.build_rw_server(tmp_path, client=account_client)
    harness.expect_error(
        server,
        "update_campaign",
        {"campaign_id": "111", "daily_budget": bad_budget},
        code="INVALID_BUDGET",
    )


@pytest.mark.parametrize("bad_bid", [-1.0, 0.0])
def test_nonpositive_bids_rejected_named(tmp_path, account_client, bad_bid):
    server = harness.build_rw_server(tmp_path, client=account_client)
    harness.expect_error(
        server,
        "update_keyword_bid",
        {"ad_group_id": "201", "criterion_id": "401", "current_bid": 1.0, "new_bid": bad_bid},
        code="INVALID_BID",
    )
