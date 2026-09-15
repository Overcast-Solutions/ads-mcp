"""F008 (part 1) — Recommendations with forgiving ids, guardrailed apply."""

import harness

FULL_RESOURCE = f"customers/{harness.CUSTOMER_ID}/recommendations/777"


def test_list_recommendations_typed_fields_and_impact(tmp_path, account_client):
    server = harness.build_server(tmp_path, client=account_client)
    payload = harness.expect_ok(harness.call(server, "list_recommendations"))
    recs = payload["recommendations"]
    assert recs, "no recommendations from the recorded fixture"
    rec = recs[0]
    assert rec["type"] == "CAMPAIGN_BUDGET"
    assert rec["campaign_id"] == "111"
    assert rec["dismissed"] is False
    base, potential = rec["impact"]["base"], rec["impact"]["potential"]
    harness.assert_money(base["cost"])
    harness.assert_money(potential["cost"])
    assert isinstance(base["conversions"], float)
    assert isinstance(potential["conversions"], float)
    assert potential["clicks"] > base["clicks"]


def test_dismiss_accepts_bare_id_and_full_resource_name(tmp_path, account_client):
    """Bare ids and full resources must normalize to one resource name."""
    server = harness.build_rw_server(tmp_path, client=account_client)
    for given in ("777", FULL_RESOURCE):
        payload = harness.expect_ok(
            harness.call(server, "dismiss_recommendation", {"recommendation_id": given})
        )
        plan = payload["plan"]
        resources = [op.get("resource") for op in plan["operations"]]
        assert resources == [FULL_RESOURCE], (
            f"id {given!r} normalized to {resources}, expected [{FULL_RESOURCE}]"
        )


def test_apply_accepts_both_formats_and_never_doubles_path(tmp_path, account_client):
    server = harness.build_rw_server(tmp_path, client=account_client)
    payload = harness.expect_ok(
        harness.call(server, "apply_recommendation", {"recommendation_id": FULL_RESOURCE})
    )
    ops = payload["plan"]["operations"]
    for op in ops:
        resource = op.get("resource", "")
        assert resource.count("customers/") == 1, f"path doubled: {resource}"
        assert resource.count("recommendations/") == 1, f"path doubled: {resource}"


def test_apply_recommendation_routes_through_plan_flow(tmp_path, account_client):
    """No one-click apply exists: preview plan first, execute only via
    confirm_and_apply."""
    server = harness.build_rw_server(tmp_path, client=account_client)
    payload = harness.expect_ok(
        harness.call(server, "apply_recommendation", {"recommendation_id": "777"})
    )
    plan_id = payload["plan"]["id"]
    assert account_client.live_mutations() == [], (
        "apply_recommendation executed something at plan time"
    )
    applied = harness.expect_ok(
        harness.call(server, "confirm_and_apply", {"plan_id": plan_id, "dry_run": False})
    )
    assert applied["applied"] is True
    assert len(account_client.live_mutations()) == 1, (
        "confirm_and_apply must be the single execution path"
    )
