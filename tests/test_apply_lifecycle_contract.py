"""F030: the whole apply interval, observed through concurrent MCP calls."""
import asyncio
import threading

import pytest
from google.api_core.exceptions import ServiceUnavailable

import harness as h
from ads_mcp.audit import AuditLog
from ads_mcp.errors import ToolError
from ads_mcp.guardrails import MAX_RETAINED_PLANS, PlanStore
from offline_contract import apply, campaign, stage


class StatefulClient(h.FakeGoogleAdsClient):
    def __init__(self, kind, hold_step):
        super().__init__()
        h.stub_standard_account(self)
        campaign(self)
        self.kind, self.hold_step = kind, hold_step
        self.value = 200_000_000 if kind == "budget" else 2_000_000
        self.entered, self.release, self.revalidated = (threading.Event() for _ in range(3))
        self.first_written = threading.Event()
        self.armed = False
        self.steps, self.baselines = [], []

    def _do_search(self, service, method, args, kwargs):
        query = str(h._req_field(args, kwargs, "query"))
        if "FROM campaign " in query:
            self._responses["campaign"][0].campaign_budget.amount_micros = self.value
        elif "FROM ad_group_criterion " in query:
            self._responses["ad_group_criterion"][0].ad_group_criterion.cpc_bid_micros = self.value
        if self.armed and ((self.kind == "budget" and "FROM campaign " in query)
                           or (self.kind == "bid" and "FROM ad_group_criterion " in query)):
            self.baselines.append(self.value)
            if self.entered.is_set():
                self.revalidated.set()
        return super()._do_search(service, method, args, kwargs)

    def _do_mutation(self, service, method, args, kwargs):
        req = kwargs["request"]
        op = req.operations[0].update
        step = "name" if service == "CampaignService" else self.kind
        target = op.amount_micros if service == "CampaignBudgetService" else op.cpc_bid_micros if service == "AdGroupCriterionService" else None
        second_target = 175_000_000 if self.kind == "budget" else 1_750_000
        if target == second_target and not self.first_written.wait(5):
            raise RuntimeError("bounded synthetic first mutation did not finish")
        if self.armed and step == self.hold_step and not self.entered.is_set():
            self.entered.set()
            if not self.release.wait(5):
                raise RuntimeError("bounded synthetic hold was not released")
        if service == "CampaignBudgetService":
            self.value = op.amount_micros
        elif service == "AdGroupCriterionService":
            self.value = op.cpc_bid_micros
        if target == (150_000_000 if self.kind == "budget" else 1_500_000):
            self.first_written.set()
        self.steps.append((step, self.value))
        return super()._do_mutation(service, method, args, kwargs)


@pytest.mark.parametrize("kind,hold_step", [("budget", "budget"), ("budget", "name"), ("bid", "bid")])
def test_distinct_plans_serialize_rechecks_through_last_step(tmp_path, kind, hold_step):
    client = StatefulClient(kind, hold_step)
    server = h.build_rw_server(tmp_path, client=client,
                               env={"ADS_MCP_MAX_BID_INCREASE_PCT": "0"})
    if kind == "budget":
        tool = "update_campaign"
        first_args = {"campaign_id": "111", "daily_budget": 150, "name": "first completed"}
        second_args = {"campaign_id": "111", "daily_budget": 175}
    else:
        tool = "update_keyword_bid"
        first_args = {"ad_group_id": "201", "criterion_id": "401", "current_bid": 2, "new_bid": 1.5}
        second_args = {**first_args, "new_bid": 1.75}
    first, second = stage(server, tool, first_args), stage(server, tool, second_args)
    client.armed = True

    async def run(connection):
        tasks = []
        try:
            tasks.append(asyncio.create_task(connection.call_tool("confirm_and_apply", {"plan_id": first["id"], "dry_run": False})))
            assert await asyncio.to_thread(client.entered.wait, 3), "first execution never reached transport"
            tasks.append(asyncio.create_task(connection.call_tool("confirm_and_apply", {"plan_id": second["id"], "dry_run": False})))
            leaked = await asyncio.to_thread(client.revalidated.wait, .25)
            # A preview must remain available while a different plan applies.
            preview = await asyncio.wait_for(connection.call_tool("confirm_and_apply", {"plan_id": second["id"], "dry_run": True}), 2)
            assert h.payload_of(preview)["applied"] is False
        finally:
            client.release.set()  # also releases when correct serialization blocks validation
            results = await asyncio.wait_for(asyncio.gather(*tasks), 5)
        return leaked, [h.payload_of(r) for r in results]

    leaked, results = h.session_run(server, run)
    assert not leaked, "a distinct plan validated inside another plan's execution interval"
    assert h.expect_ok(results[0])["applied"] is True
    assert h.error_of(results[1])["code"] == ("BUDGET_CAP_EXCEEDED" if kind == "budget" else "BID_CAP_EXCEEDED")
    expected = 150_000_000 if kind == "budget" else 1_500_000
    assert client.value == expected and client.baselines[-1] == expected
    assert len(client.mutations) == (2 if kind == "budget" else 1)


def test_application_does_not_serialize_independent_server_contexts(tmp_path):
    client = StatefulClient("budget", "budget")
    first = h.build_rw_server(tmp_path, client=client)
    plan = stage(first, "update_campaign", {"campaign_id": "111", "daily_budget": 150})
    other_path = tmp_path / "other"
    other_path.mkdir()
    other_client = h.stub_standard_account(h.FakeGoogleAdsClient())
    other = h.build_rw_server(other_path, client=other_client)
    second = stage(other, "pause_entity", {"entity_type": "campaign", "entity_id": "111"})
    client.armed = True
    async def run(connection):
        task = asyncio.create_task(connection.call_tool("confirm_and_apply", {"plan_id": plan["id"], "dry_run": False}))
        try:
            assert await asyncio.to_thread(client.entered.wait, 3)
            result = await asyncio.wait_for(asyncio.to_thread(apply, other, second), 2)
            assert h.expect_ok(result)["applied"] is True
        finally:
            client.release.set()
            await asyncio.wait_for(task, 5)
    h.session_run(first, run)


@pytest.mark.parametrize("failure", ["validation", "provider", "pre_audit", "terminal_audit", "success"])
def test_interval_released_after_every_exit(tmp_path, account_client, monkeypatch, failure):
    store = PlanStore(clock=h.FakeClock())
    server = h.build_rw_server(tmp_path, client=account_client, plan_store=store)
    first = stage(server, "pause_entity", {"entity_type": "campaign", "entity_id": "111"})
    second = stage(server, "pause_entity", {"entity_type": "campaign", "entity_id": "222"})
    if failure == "validation":
        def reject(ctx):
            raise ToolError("SYNTHETIC_RECHECK_REFUSED", "synthetic state changed")
        store.get(first["id"]).rechecks.append(reject)
    elif failure == "provider":
        def fail(*args, **kwargs):
            raise ServiceUnavailable("synthetic mutation failure")
        monkeypatch.setattr(account_client, "_do_mutation", fail)
    original = AuditLog.write
    def audit(self, record, **kwargs):
        if record.get("plan_id") == first["id"]:
            if failure == "pre_audit" and record["event"] == "apply_started":
                raise ToolError("AUDIT_WRITE_FAILED", "synthetic audit unavailable")
            if failure == "terminal_audit" and record["event"] == "applied":
                return False
        return original(self, record, **kwargs)
    monkeypatch.setattr(AuditLog, "write", audit)
    result = apply(server, first)
    if failure in ("validation", "provider", "pre_audit"):
        h.error_of(result)
    else:
        assert h.expect_ok(result)["applied"] is True
    if failure == "terminal_audit":
        assert result.get("audit_warning")
    if failure == "provider":
        monkeypatch.setattr(account_client, "_do_mutation", h.FakeGoogleAdsClient._do_mutation.__get__(account_client))
    async def run(connection):
        return h.payload_of(await asyncio.wait_for(connection.call_tool("confirm_and_apply", {"plan_id": second["id"], "dry_run": False}), 3))
    assert h.expect_ok(h.session_run(server, run))["applied"] is True


@pytest.mark.parametrize("cleanup", ["expiry", "capacity"])
@pytest.mark.parametrize("partial", [False, True])
def test_consumed_recovery_after_cleanup_never_denies_prior_effects(tmp_path, account_client, cleanup, partial):
    clock = h.FakeClock()
    store = PlanStore(ttl_seconds=30, clock=clock)
    server = h.build_rw_server(tmp_path, client=account_client, plan_store=store, clock=clock)
    plan = stage(server, "pause_entity", {"entity_type": "campaign", "entity_id": "111"})
    if partial:
        original = store.get(plan["id"]).execute
        def execute(ctx):
            original(ctx)
            raise ServiceUnavailable("synthetic later step failed")
        store.get(plan["id"]).execute = execute
    result = apply(server, plan)
    assert len(account_client.mutations) == 1
    assert ("error" in result) is partial
    if cleanup == "expiry":
        clock.advance(31)
    for _ in range(1 if cleanup == "expiry" else MAX_RETAINED_PLANS + 2):
        store.create(tool="synthetic", customer_id=h.CUSTOMER_ID, summary="bounded", operations=[], execute=lambda ctx: None)
    error = h.error_of(apply(server, plan))
    message = error["message"].lower()
    assert "nothing was applied" not in message and "unapplied plans" not in message
    assert "check" in message and any(word in message for word in ("audit", "account", "prior", "previous"))
    assert len(account_client.mutations) == 1
    for value in vars(store).values():
        if isinstance(value, (dict, set, list)):
            assert len(value) <= MAX_RETAINED_PLANS + 1


def test_unknown_history_does_not_offer_blind_restage(tmp_path, account_client):
    server = h.build_rw_server(tmp_path, client=account_client)
    error = h.error_of(apply(server, {"id": "unknown-synthetic-plan"}))
    assert "check" in error["message"].lower()
    assert any(word in error["message"].lower() for word in ("audit", "account", "prior"))
    assert not account_client.mutations
