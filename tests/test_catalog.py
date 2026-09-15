"""F013 (exhaustive) — the registry walk: every mutation tool returns a plan,
zero direct-apply paths exist, and the registry matches what registers."""

import harness
from pmax_oracle import assert_catalog
from ads_mcp.tools.registry import all_tool_specs
from tool_catalog import (
    ALL_WRITE_MODE_TOOLS,
    APPLY_TOOL,
    IRREVERSIBLE_TOOLS,
    MUTATION_ARGS,
    MUTATION_TOOLS,
    READ_TOOLS,
)


def test_registry_specs_match_locked_catalog_exactly():
    specs = all_tool_specs()
    names = [s.name for s in specs]
    assert len(names) == len(set(names)), "duplicate tool names in registry"
    by_kind = {}
    for spec in specs:
        assert spec.description and spec.description.strip(), (
            f"{spec.name}: empty description"
        )
        by_kind.setdefault(spec.kind, set()).add(spec.name)
    assert_catalog(by_kind.get("read", set()), kind="read")
    assert_catalog(by_kind.get("mutation", set()), kind="mutation")
    assert by_kind.get("apply", set()) == {APPLY_TOOL}
    assert set(by_kind) == {"read", "mutation", "apply"}, f"unknown kinds: {set(by_kind)}"


def test_write_mode_registration_matches_registry(tmp_path, account_client):
    server = harness.build_rw_server(tmp_path, client=account_client)
    assert_catalog(harness.tool_names(server))


def test_every_mutation_tool_returns_plan_and_never_applies(tmp_path, account_client):
    """The exhaustive walk: all 30 mutation tools, one by one."""
    server = harness.build_rw_server(tmp_path, client=account_client)
    plan_ids = set()
    for tool in sorted(MUTATION_TOOLS):
        payload = harness.call(server, tool, dict(MUTATION_ARGS[tool]))
        assert "error" not in payload, (
            f"{tool} refused its minimal valid args: {payload.get('error')}"
        )
        plan = payload.get("plan")
        assert isinstance(plan, dict), f"{tool} did not return a plan payload: {payload}"
        assert isinstance(plan.get("id"), str) and plan["id"], f"{tool}: plan without id"
        assert isinstance(plan.get("summary"), str) and plan["summary"].strip(), (
            f"{tool}: plan without human-readable summary"
        )
        assert isinstance(plan.get("operations"), list) and plan["operations"], (
            f"{tool}: plan without structured operations"
        )
        harness.parse_iso_utc(plan["expires_at"])
        assert plan["id"] not in plan_ids, f"{tool}: plan id collides"
        plan_ids.add(plan["id"])
        if tool in IRREVERSIBLE_TOOLS:
            assert plan.get("irreversible") is True, f"{tool}: removal not flagged"
    assert account_client.live_mutations() == [], (
        "a mutation tool reached a live mutate call without confirm_and_apply: "
        f"{[ (m.service, m.method) for m in account_client.live_mutations() ]}"
    )


def test_confirm_and_apply_is_the_only_live_path(tmp_path, account_client):
    server = harness.build_rw_server(tmp_path, client=account_client)
    for tool in sorted(MUTATION_TOOLS):
        harness.call(server, tool, dict(MUTATION_ARGS[tool]))
    assert account_client.live_mutations() == []
    plan = harness.expect_ok(
        harness.call(server, "update_campaign", {"campaign_id": "111", "daily_budget": 45.0})
    )["plan"]
    applied = harness.expect_ok(
        harness.call(server, "confirm_and_apply", {"plan_id": plan["id"], "dry_run": False})
    )
    assert applied["applied"] is True
    assert len(account_client.live_mutations()) == 1, (
        "exactly one live mutation may exist after exactly one confirmed apply"
    )
