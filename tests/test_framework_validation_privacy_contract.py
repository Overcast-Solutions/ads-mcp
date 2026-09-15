"""F061: secrets stay out of genuine MCP validation results and logging.

Only synthetic OAuth files and the SDK factory are supplied. The same cases
use in-memory framework dispatch and the invoking runtime's installed console.
"""
import json
import logging
import socket

import pytest

import harness as h
import test_auth_cause_contract as plumbing
from test_confirmation_queue_contract import QueueServer
from test_plan_input_fidelity_contract import ObservedPlanStore


SECRETS = (h.FAKE_CLIENT_SECRET, h.FAKE_REFRESH_TOKEN, h.FAKE_DEVELOPER_TOKEN)
INJECTION = plumbing.INJECTION + r'''
from ads_mcp.guardrails import PlanStore
original_create = PlanStore.create
def observe_create(self, **kwargs):
    result = original_create(self, **kwargs)
    mark("plan staged", identity=result.id)
    return result
PlanStore.create = observe_create
'''


class PrivacyServer(QueueServer):
    def __exit__(self, *args):
        # Inspect privacy in the test after joining stderr, including on reds.
        assert self.close(), "installed server did not close after stdin EOF"
        assert self.process.returncode == 0
        assert self.events("finished") and not self.events("network attempted")


@pytest.fixture(autouse=True)
def offline(monkeypatch, tmp_path):
    def deny(*args, **kwargs):
        raise AssertionError("synthetic validation contract forbids networking")
    for name in ("connect", "connect_ex", "bind", "sendto"):
        monkeypatch.setattr(socket.socket, name, deny)
    monkeypatch.setattr(socket, "getaddrinfo", deny)
    monkeypatch.setattr(plumbing, "INJECTION", INJECTION)
    monkeypatch.setattr(h, "scrubbed_env", lambda overlay=None: {
        "PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "TMPDIR": str(tmp_path),
        "PYTHONNOUSERSITE": "1", **(overlay or {})})


CASES = [
    ("client-key", "health_check", {SECRETS[0]: 1}, None),
    ("refresh-key", "run_gaql", {"query": "SELECT customer.id FROM customer", SECRETS[1]: 1}, None),
    ("legacy-key", "pause_entity", {"entity_type": "campaign", "entity_id": "111", SECRETS[2]: 1}, None),
    ("embedded-long-key", "health_check", {"x" * 10000 + SECRETS[1] + "y" * 10000: 1}, None),
    ("hidden-unknown-value", "health_check", {"unsupported_option": SECRETS[0]}, "unsupported_option"),
    ("hidden-wrong-type", "run_gaql", {"query": {"value": SECRETS[1]}}, "query"),
    ("hidden-wrong-value", "run_gaql", {"query": "SELECT customer.id FROM customer", "page_size": SECRETS[2]}, "page_size"),
]


def refused(wire, ordinary_field):
    result = wire["result"]
    text = json.dumps(result)
    if not result.get("isError"):
        error = h.error_of(plumbing.InstalledServer.payload(wire))
        assert error["code"] != "INTERNAL"
    if ordinary_field:
        assert ordinary_field in text, "secret-free argument names must remain actionable"
    assert "Traceback" not in text


@pytest.mark.parametrize("boundary", ["framework", "installed-stdio"])
@pytest.mark.parametrize("label,tool,args,ordinary_field", CASES, ids=[case[0] for case in CASES])
def test_validation_never_reflects_configured_secrets(tmp_path, caplog, boundary, label, tool, args, ordinary_field):
    caplog.set_level(logging.WARNING)
    if boundary == "framework":
        provider = h.stub_standard_account(h.FakeGoogleAdsClient())
        plans = ObservedPlanStore()
        server = h.build_rw_server(tmp_path, client=provider, plan_store=plans)
        result = h.call_result(server, tool, args)
        wire = {"result": result.model_dump(mode="json", by_alias=True)}
        assert not plans.created and not provider.searches
        assert not provider.planner_calls() and not provider.mutations
        logs = caplog.text
        audit = h.read_audit_records(tmp_path) if h.audit_file(tmp_path).exists() else []
    else:
        with PrivacyServer(tmp_path) as server:
            wire = server.receive(server.tool(tool, **args))
            assert not server.events("plan staged") and not server.events("search")
            assert not server.events("forecast") and not server.events("mutation")
            audit = server.audit() if h.audit_file(tmp_path).exists() else []
        logs = "".join(server.stderr)
    refused(wire, ordinary_field)
    assert not [row for row in audit if row["event"] == "plan_created"]
    channels = {"result": json.dumps(wire), "logging/stderr": logs, "audit": json.dumps(audit)}
    leaked = [name for name, text in channels.items() if any(secret in text for secret in SECRETS)]
    assert not leaked, f"configured synthetic credential reflected in {', '.join(leaked)} ({label})"


@pytest.mark.parametrize("read_only", [True, False], ids=["readonly", "write-mode"])
def test_closed_schemas_keep_ordinary_diagnostics_and_successful_neighbors(tmp_path, read_only):
    with PrivacyServer(tmp_path, env={"ADS_MCP_READ_ONLY": str(read_only).lower()}) as server:
        tools = server.receive(server.send("tools/list", {}))["result"]["tools"]
        catalog = {tool["name"]: tool for tool in tools}
        assert ("pause_entity" in catalog) is (not read_only)
        assert ("confirm_and_apply" in catalog) is (not read_only)
        assert all(tool["inputSchema"]["additionalProperties"] is False for tool in tools)
        for name, arguments, field in [
            ("health_check", {"ordinary_unsupported_field": 1}, "ordinary_unsupported_field"),
            ("run_gaql", {"query": ["wrong type"]}, "query"),
        ]:
            before = len(server.events("search"))
            wire = server.receive(server.tool(name, **arguments))
            refused(wire, field)
            assert wire["result"].get("isError"), "wrong types must still reach genuine framework refusal"
            assert len(server.events("search")) == before
        assert server.call("health_check")["status"] == "OK"
        server.mode(accounts={cid: {"currency_code": "USD", "time_zone": "Etc/UTC"}
                              for cid in (h.CUSTOMER_ID, h.OTHER_CUSTOMER_ID)})
        for cid in (h.CUSTOMER_ID, h.OTHER_CUSTOMER_ID):
            result = server.call("run_gaql", {"query": "SELECT customer.id FROM customer", "customer_id": cid})
            assert result["customer_id"] == cid
            assert int(result["rows"][0]["customer"]["id"]) == int(cid)
            report = h.expect_ok(server.call("get_campaign_performance", {
                "customer_id": cid, "last_n_days": 7, "campaign_id": None}))
            assert report["customer_id"] == cid and "campaigns" in report and "window" in report
        h.expect_ok(server.call("get_keyword_forecasts", {"keyword_texts": ["synthetic"]}))
        if not read_only:
            before = len(server.events("search"))
            foreign = server.call("pause_entity", {"entity_type": "campaign", "entity_id": "111",
                                                    "customer_id": h.OTHER_CUSTOMER_ID})
            assert h.error_of(foreign)["code"] == "PLAN_CUSTOMER_MISMATCH"
            assert len(server.events("search")) == before and not server.events("plan staged")
            plan = server.call("pause_entity", {"entity_type": "campaign", "entity_id": "111"})["plan"]
            assert server.call("confirm_and_apply", {"plan_id": plan["id"]})["applied"] is False
            assert not server.events("mutation")
        server.mode(fault="non-auth", resources=["campaign"])
        error = h.error_of(server.call("run_gaql", {"query": "SELECT campaign.id FROM campaign"}))
        assert plumbing.DETAIL in error["message"], "ordinary provider detail must remain useful"
        assert SECRETS[1] not in error["message"]
    h.assert_no_secrets("".join(server.public + server.stderr))
