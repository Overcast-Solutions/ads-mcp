"""F010 — Guardrail core: plan flow (preview → confirm_and_apply)."""

import pytest

import harness
from ads_mcp.guardrails import PlanStore

UPDATE_ARGS = {"campaign_id": "111", "daily_budget": 45.0}


def _plan(server, tool="update_campaign", args=None):
    payload = harness.expect_ok(harness.call(server, tool, args or dict(UPDATE_ARGS)))
    plan = payload["plan"]
    assert isinstance(plan.get("id"), str) and plan["id"], f"plan without id: {plan}"
    return plan


def test_mutation_returns_plan_and_applies_nothing(tmp_path, account_client):
    server = harness.build_rw_server(tmp_path, client=account_client)
    plan = _plan(server)
    assert isinstance(plan["summary"], str) and plan["summary"].strip()
    assert isinstance(plan["operations"], list) and plan["operations"]
    harness.parse_iso_utc(plan["expires_at"])
    assert account_client.live_mutations() == [], (
        "plan creation touched a live mutate call"
    )


def test_plan_ids_are_unique(tmp_path, account_client):
    server = harness.build_rw_server(tmp_path, client=account_client)
    ids = {_plan(server)["id"] for _ in range(5)}
    assert len(ids) == 5, f"plan ids collide: {ids}"


def test_dry_run_defaults_true_and_does_not_consume(tmp_path, account_client):
    server = harness.build_rw_server(tmp_path, client=account_client)
    plan = _plan(server)
    preview = harness.expect_ok(
        harness.call(server, "confirm_and_apply", {"plan_id": plan["id"]})
    )
    assert preview["applied"] is False, "confirm_and_apply must default to dry_run"
    assert account_client.live_mutations() == []
    # The preview must NOT consume the plan.
    applied = harness.expect_ok(
        harness.call(
            server, "confirm_and_apply", {"plan_id": plan["id"], "dry_run": False}
        )
    )
    assert applied["applied"] is True
    assert len(account_client.live_mutations()) == 1


def test_plans_are_single_use(tmp_path, account_client):
    server = harness.build_rw_server(tmp_path, client=account_client)
    plan = _plan(server)
    harness.expect_ok(
        harness.call(
            server, "confirm_and_apply", {"plan_id": plan["id"], "dry_run": False}
        )
    )
    harness.expect_error(
        server,
        "confirm_and_apply",
        {"plan_id": plan["id"], "dry_run": False},
        code="PLAN_CONSUMED",
    )
    assert len(account_client.live_mutations()) == 1, "a consumed plan applied twice"


@pytest.mark.parametrize("require_env", [None, "true"])
def test_require_dry_run_is_sequence_enforced(tmp_path, account_client, require_env):
    """Unset defaults to required (safe default); 'true' is explicit. The
    requirement is satisfied only by actually running the preview."""
    env = {}
    if require_env is None:
        env["ADS_MCP_REQUIRE_DRY_RUN"] = None  # build_config drops None values
    else:
        env["ADS_MCP_REQUIRE_DRY_RUN"] = require_env
    server = harness.build_rw_server(tmp_path, client=account_client, env=env)
    plan = _plan(server)
    harness.expect_error(
        server,
        "confirm_and_apply",
        {"plan_id": plan["id"], "dry_run": False},
        code="DRY_RUN_REQUIRED",
    )
    assert account_client.live_mutations() == []
    preview = harness.expect_ok(
        harness.call(server, "confirm_and_apply", {"plan_id": plan["id"], "dry_run": True})
    )
    assert preview["applied"] is False
    applied = harness.expect_ok(
        harness.call(server, "confirm_and_apply", {"plan_id": plan["id"], "dry_run": False})
    )
    assert applied["applied"] is True
    assert len(account_client.live_mutations()) == 1


def test_no_tool_parameter_bypasses_require_dry_run(tmp_path, account_client):
    """No bypass-shaped parameter may allow application without preview."""
    server = harness.build_rw_server(
        tmp_path, client=account_client, env={"ADS_MCP_REQUIRE_DRY_RUN": "true"}
    )
    for bypass in (
        {"bypass_require_dry_run": True},
        {"require_dry_run": False},
        {"force": True},
        {"confirmed_twice": True},
    ):
        plan = _plan(server)
        result = harness.call_result(
            server,
            "confirm_and_apply",
            {"plan_id": plan["id"], "dry_run": False, **bypass},
        )
        if not result.is_error:
            payload = harness.payload_of(result)
            assert payload.get("applied") is not True, (
                f"bypass parameter {bypass} caused an un-previewed apply"
            )
            harness.error_of(payload)
        assert account_client.live_mutations() == [], (
            f"bypass parameter {bypass} reached a live mutate call"
        )


def test_plans_expire_default_ttl_15m(tmp_path, account_client):
    clock = harness.FakeClock()
    server = harness.build_rw_server(tmp_path, client=account_client, clock=clock)
    plan = _plan(server)
    created = clock()
    expires = harness.parse_iso_utc(plan["expires_at"]).timestamp()
    assert expires == pytest.approx(created + 900, abs=2), (
        f"default TTL must be 15 minutes: expires_at={plan['expires_at']}"
    )
    clock.advance(800)
    preview = harness.expect_ok(
        harness.call(server, "confirm_and_apply", {"plan_id": plan["id"], "dry_run": True})
    )
    assert preview["applied"] is False
    clock.advance(200)  # now 1000s > 900s
    harness.expect_error(
        server,
        "confirm_and_apply",
        {"plan_id": plan["id"], "dry_run": False},
        code="PLAN_EXPIRED",
    )
    assert account_client.live_mutations() == []


def test_plan_ttl_configurable_via_env(tmp_path, account_client):
    clock = harness.FakeClock()
    server = harness.build_rw_server(
        tmp_path,
        client=account_client,
        clock=clock,
        env={"ADS_MCP_PLAN_TTL_SECONDS": "60"},
    )
    plan = _plan(server)
    clock.advance(61)
    harness.expect_error(
        server,
        "confirm_and_apply",
        {"plan_id": plan["id"], "dry_run": False},
        code="PLAN_EXPIRED",
    )


def test_unknown_plan_id(tmp_path, account_client):
    server = harness.build_rw_server(tmp_path, client=account_client)
    harness.expect_error(
        server,
        "confirm_and_apply",
        {"plan_id": "plan-that-never-existed", "dry_run": False},
        code="PLAN_NOT_FOUND",
    )


def test_plan_cannot_cross_customers(tmp_path, account_client):
    """A plan created for one customer id must not apply to another."""
    store = PlanStore(clock=harness.FakeClock())
    server_a = harness.build_rw_server(
        tmp_path, client=account_client, plan_store=store
    )
    plan = _plan(server_a)

    other = harness.stub_standard_account(harness.FakeGoogleAdsClient())
    server_b = harness.build_rw_server(
        tmp_path,
        client=other,
        plan_store=store,
        env={"GOOGLE_ADS_CUSTOMER_ID": harness.OTHER_CUSTOMER_ID},
    )
    harness.expect_error(
        server_b,
        "confirm_and_apply",
        {"plan_id": plan["id"], "dry_run": False},
        code="PLAN_CUSTOMER_MISMATCH",
    )
    assert other.live_mutations() == []


def test_dashed_and_undashed_customer_ids_are_one_customer(tmp_path, account_client):
    """Write-path/read-path id symmetry: dashes never split a customer."""
    store = PlanStore(clock=harness.FakeClock())
    server_a = harness.build_rw_server(
        tmp_path, client=account_client, plan_store=store
    )
    plan_payload = harness.expect_ok(
        harness.call(
            server_a,
            "update_campaign",
            {**UPDATE_ARGS, "customer_id": harness.CUSTOMER_ID_DASHED},
        )
    )
    applied = harness.expect_ok(
        harness.call(
            server_a,
            "confirm_and_apply",
            {"plan_id": plan_payload["plan"]["id"], "dry_run": False},
        )
    )
    assert applied["applied"] is True
