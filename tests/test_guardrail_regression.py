"""F022: exhaustive application, batches, concurrency and guardrail rechecks.

These scenarios exercise every mutation through its executor and keep
staging, preview and application consistent under failure and concurrency.
"""

import importlib
import json
import sys
import threading
from pathlib import Path

import pytest

import harness
from tool_catalog import IRREVERSIBLE_TOOLS, MUTATION_ARGS, MUTATION_TOOLS, OPTIONAL_ARGS

CHECKS = [
    "check_every_tool_applies",
    "check_single_use_under_concurrency",
    "check_bid_cap_cannot_be_defined_by_caller",
    "check_bid_cap_with_no_baseline_anywhere",
    "check_customer_id_refused_on_every_write_tool",
    "check_partial_apply_is_recorded",
    "check_audit_honesty_on_executor_failure",
    "check_mutations_are_not_retried",
    "check_apply_time_recheck_runs",
    "check_ad_group_bid_applies",
    "check_recommendation_spend_gate",
    "check_composite_entity_ids",
    "check_decreases_are_never_blocked",
]


@pytest.mark.parametrize("check", CHECKS)
def test_absorbed_guardrail_check(check, capsys):
    mod = importlib.reload(importlib.import_module("guardrail_checks"))
    mod.FAILS.clear()
    getattr(mod, check)()
    assert not mod.FAILS, f"{check}: " + "; ".join(mod.FAILS)


# ---------------------------------------------------------------------------
# The blind spot itself: optional arguments, driven through plan AND apply.


@pytest.mark.parametrize("tool", sorted(OPTIONAL_ARGS))
def test_optional_arguments_survive_apply(tmp_path, account_client, tool):
    """Unchanged valid optional arguments must survive application rechecks."""
    server = harness.build_rw_server(tmp_path, client=account_client)
    staged = harness.call(server, tool, {**MUTATION_ARGS[tool], **OPTIONAL_ARGS[tool]})
    assert "error" not in staged, f"{tool} refused optional args at plan: {staged.get('error')}"
    pid = staged["plan"]["id"]
    harness.call(server, "confirm_and_apply", {"plan_id": pid, "dry_run": True})
    applied = harness.call(server, "confirm_and_apply",
                           {"plan_id": pid, "dry_run": False,
                            "confirm_irreversible": tool in IRREVERSIBLE_TOOLS})
    assert "error" not in applied, (
        f"{tool} staged and previewed with optional args, then REFUSED at apply: "
        f"{applied.get('error')}"
    )
    assert account_client.live_mutations(), f"{tool} applied but sent nothing"


# ---------------------------------------------------------------------------
# Irreversible operations need a separate acknowledgement.


@pytest.mark.parametrize("tool", sorted(IRREVERSIBLE_TOOLS))
def test_irreversible_plans_need_a_second_acknowledgement(tmp_path, account_client, tool):
    server = harness.build_rw_server(tmp_path, client=account_client)
    plan = harness.expect_ok(harness.call(server, tool, dict(MUTATION_ARGS[tool])))["plan"]
    assert plan["irreversible"] is True
    harness.call(server, "confirm_and_apply", {"plan_id": plan["id"], "dry_run": True})
    harness.expect_error(
        server, "confirm_and_apply", {"plan_id": plan["id"], "dry_run": False},
        code="IRREVERSIBLE_CONFIRMATION_REQUIRED")
    assert account_client.live_mutations() == [], (
        "an irreversible change applied without the second acknowledgement"
    )
    applied = harness.expect_ok(harness.call(
        server, "confirm_and_apply",
        {"plan_id": plan["id"], "dry_run": False, "confirm_irreversible": True}))
    assert applied["applied"] is True


def test_reversible_plans_are_unaffected_by_the_second_gate(tmp_path, account_client):
    server = harness.build_rw_server(tmp_path, client=account_client)
    plan = harness.expect_ok(harness.call(
        server, "update_campaign", {"campaign_id": "111", "daily_budget": 20.0}))["plan"]
    harness.call(server, "confirm_and_apply", {"plan_id": plan["id"], "dry_run": True})
    applied = harness.expect_ok(harness.call(
        server, "confirm_and_apply", {"plan_id": plan["id"], "dry_run": False}))
    assert applied["applied"] is True


def test_confirm_irreversible_is_not_a_dry_run_bypass(tmp_path, account_client):
    """Irreversible acknowledgement must not bypass the preview requirement."""
    server = harness.build_rw_server(
        tmp_path, client=account_client, env={"ADS_MCP_REQUIRE_DRY_RUN": "true"})
    plan = harness.expect_ok(harness.call(
        server, "remove_entity", {"entity_type": "ad", "entity_id": "201~901"}))["plan"]
    harness.expect_error(
        server, "confirm_and_apply",
        {"plan_id": plan["id"], "dry_run": False, "confirm_irreversible": True},
        code="DRY_RUN_REQUIRED")
    assert account_client.live_mutations() == []
