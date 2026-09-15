"""F025: a previewed reduction cannot reuse a stale spend exemption."""
import json

import pytest

import harness as h


def stage(server, amount=150, **changes):
    result = h.expect_ok(h.call(server, "update_campaign", {
        "campaign_id": "111", "daily_budget": amount, **changes}))
    plan = result["plan"]
    h.expect_ok(h.call(server, "confirm_and_apply", {"plan_id": plan["id"], "dry_run": True}))
    return plan


def apply(server, plan):
    return h.call(server, "confirm_and_apply", {"plan_id": plan["id"], "dry_run": False})


def assert_refusal(payload, code, client, tmp_path):
    assert h.error_of(payload)["code"] == code
    assert not client.mutations
    audit = h.read_audit_records(tmp_path)
    assert any(r["event"] == "refused" and r["outcome"] == code for r in audit)
    assert not any(r["event"] == "applied" for r in audit)
    h.assert_no_secrets(json.dumps(audit))


@pytest.mark.parametrize("changes", [{}, {"target_roas": 2.0, "geo_target_ids": ["2840"]}])
def test_stale_decrease_becomes_above_cap_increase(tmp_path, account_client, changes):
    row = account_client._responses["campaign"][0]
    row.campaign_budget.amount_micros = 200_000_000
    server = h.build_rw_server(tmp_path, client=account_client)
    plan = stage(server, **changes)
    row.campaign_budget.amount_micros = 50_000_000
    before = len(account_client.searches)
    result = apply(server, plan)
    assert_refusal(result, "BUDGET_CAP_EXCEEDED", account_client, tmp_path)
    assert len(account_client.searches) > before
    query = " ".join(s.query for s in account_client.searches[before:])
    for field in ("campaign.campaign_budget", "campaign.resource_name", "campaign_budget.resource_name", "campaign_budget.amount_micros"):
        assert field in query
    assert "100" in result["error"]["message"]


@pytest.mark.parametrize("fault", ["empty", "failure", "campaign_id", "campaign_resource", "budget_id", "budget_resource", "missing_link", "different_link"])
def test_apply_reverifies_the_exact_campaign_budget_identity(tmp_path, account_client, fault):
    row = account_client._responses["campaign"][0]
    row.campaign_budget.amount_micros = 200_000_000
    server = h.build_rw_server(tmp_path, client=account_client)
    plan = stage(server)
    if fault == "empty":
        account_client._responses["campaign"] = []
    elif fault == "failure":
        account_client.stub_error(RuntimeError("synthetic budget read failed"))
    elif fault == "campaign_id":
        row.campaign.id = 999
    elif fault == "campaign_resource":
        row.campaign.resource_name = f"customers/{h.OTHER_CUSTOMER_ID}/campaigns/111"
    elif fault == "budget_id":
        row.campaign_budget.id = 999
    elif fault == "budget_resource":
        row.campaign_budget.resource_name = f"customers/{h.OTHER_CUSTOMER_ID}/campaignBudgets/311"
    elif fault == "missing_link":
        row.campaign.campaign_budget = ""
    else:
        row.campaign.campaign_budget = f"customers/{h.CUSTOMER_ID}/campaignBudgets/999"
    assert_refusal(apply(server, plan), "BUDGET_BASELINE_UNVERIFIED", account_client, tmp_path)


@pytest.mark.parametrize("staged,current,new", [(200, 180, 150), (50, 60, 100), (50, 50, 80), (200, 150, 150)])
def test_fresh_decreases_and_in_cap_increases_apply(tmp_path, account_client, staged, current, new):
    row = account_client._responses["campaign"][0]
    row.campaign_budget.amount_micros = staged * 1_000_000
    server = h.build_rw_server(tmp_path, client=account_client)
    plan = stage(server, new)
    row.campaign_budget.amount_micros = current * 1_000_000
    before = len(account_client.searches)
    assert h.expect_ok(apply(server, plan))["applied"] is True
    assert len(account_client.searches) > before
    call = next(m for m in account_client.mutations if m.method == "mutate_campaign_budgets")
    update = call.request.operations[0].update
    assert update.resource_name == f"customers/{h.CUSTOMER_ID}/campaignBudgets/311"
    assert update.amount_micros == new * 1_000_000


def test_unset_budget_cap_still_refuses_a_verified_reduction(tmp_path, account_client):
    server = h.build_rw_server(tmp_path, client=account_client, env={"ADS_MCP_MAX_DAILY_BUDGET": None})
    result = h.call(server, "update_campaign", {"campaign_id": "111", "daily_budget": 30})
    assert_refusal(result, "GUARDRAIL_CAP_UNSET", account_client, tmp_path)


@pytest.mark.parametrize("amount", [1e-8, 0.0000009])
def test_positive_budget_must_not_truncate_to_zero_micros(tmp_path, account_client, amount):
    server = h.build_rw_server(tmp_path, client=account_client)
    result = h.call(server, "update_campaign", {"campaign_id": "111", "daily_budget": amount})
    assert_refusal(result, "INVALID_BUDGET", account_client, tmp_path)


def test_existing_minimum_budget_is_preserved_on_wire(tmp_path, account_client):
    server = h.build_rw_server(tmp_path, client=account_client)
    assert h.expect_ok(apply(server, stage(server, 0.01)))["applied"]
    assert account_client.mutations[0].request.operations[0].update.amount_micros == 10000
