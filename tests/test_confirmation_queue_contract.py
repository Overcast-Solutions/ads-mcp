"""F050: inspection stays available through the installed stdio boundary.

Only the synthetic SDK transport is held. Protocol pings witness stdio readiness;
no worker counts, stack shapes, scheduler patches or capacity changes are used.
The existing F047 driver is neutral process/protocol plumbing, not an auth gate.
"""
from contextlib import contextmanager
import json
from pathlib import Path
import queue
import socket
import subprocess
import time

import pytest

import harness as h
import test_auth_cause_contract as plumbing
from tool_catalog import MUTATION_TOOLS


INJECTION = r'''
import sys
def early_network_guard(event, args):
    if event in ("socket.connect", "socket.bind", "socket.getaddrinfo", "socket.sendto"):
        raise OSError("offline queue contract forbids network")
sys.addaudithook(early_network_guard)
''' + plumbing.INJECTION + r'''
import time
from offline_contract import project, selected
from google.api_core.exceptions import ServiceUnavailable

hold = control.parent / "hold"
release = control.parent / "release"

class QueueTransport(Transport):
    def _do_search(self, service, method, args, kwargs):
        rows = super()._do_search(service, method, args, kwargs)
        query = str(h._req_field(args, kwargs, "query"))
        if h._FROM_RE.search(query).group(1) == "campaign":
            mark("budget read", micros=rows[0].campaign_budget.amount_micros)
        return [project(row, selected(query)) for row in rows]

    def _do_mutation(self, service, method, args, kwargs):
        request = kwargs.get("request") or args[0]
        assert hasattr(request, "_pb"), "mutation must be a genuine SDK request"
        mark("transport entered", service=service,
             request=MessageToDict(request._pb, preserving_proto_field_name=True))
        if hold.exists():
            mark("held")
            deadline = time.monotonic() + 45
            while not release.exists():
                if time.monotonic() >= deadline:
                    mark("hold watchdog")
                    raise AssertionError("parent did not release synthetic transport")
                time.sleep(0.01)
            mark("released")
        result = super()._do_mutation(service, method, args, kwargs)
        if service == "CampaignBudgetService":
            amount = request.operations[0].update.amount_micros
            self._responses["campaign"][0].campaign_budget.amount_micros = amount
            mark("budget committed", micros=amount)
        if json.loads(control.read_text()).get("lost_write"):
            raise ServiceUnavailable("synthetic lost write response")
        return result

transport = h.stub_standard_account(QueueTransport())
transport._responses["campaign"][0].campaign_budget.amount_micros = 200_000_000
'''


@pytest.fixture(autouse=True)
def offline_parent(monkeypatch, tmp_path):
    def deny(*args, **kwargs):
        raise AssertionError("F050 parent forbids network")
    for name in ("connect", "connect_ex", "bind", "sendto"):
        monkeypatch.setattr(socket.socket, name, deny)
    monkeypatch.setattr(socket, "getaddrinfo", deny)
    monkeypatch.setattr(plumbing, "INJECTION", INJECTION)
    # The console belongs to the Python running pytest, including clean installs.
    monkeypatch.setattr(h, "scrubbed_env", lambda overlay=None: {
        "PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "TMPDIR": str(tmp_path),
        "PYTHONNOUSERSITE": "1", **(overlay or {})})


class QueueServer(plumbing.InstalledServer):
    def __init__(self, *args, **kwargs):
        self.answers = {}
        try:
            super().__init__(*args, **kwargs)
        except BaseException:
            if hasattr(self, "process"):
                self.close()
            raise

    def observations(self):
        if not self.marker.exists():
            return []
        # A writer may currently be appending the last line. Never treat that
        # incomplete observation as an application or fixture failure.
        text = self.marker.read_text()
        return [json.loads(line) for line in text.splitlines(keepends=True)
                if line.endswith("\n")]

    def mode(self, **settings):
        pending = self.control.with_suffix(".pending")
        pending.write_text(json.dumps(settings))
        pending.replace(self.control)

    def collect(self, identities, seconds):
        deadline = time.monotonic() + seconds
        while not set(identities) <= self.answers.keys():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                item = self.inbox.get(timeout=min(0.05, remaining))
            except queue.Empty:
                if self.process.poll() is not None:
                    break
                continue
            if "id" in item:
                assert item["id"] not in self.answers, "duplicate JSON-RPC response"
                self.answers[item["id"]] = item
        return set(identities) <= self.answers.keys()

    def receive(self, identity):
        assert self.collect([identity], 15), "installed MCP response deadline exceeded"
        return self.answers[identity]

    def tool(self, name, **arguments):
        return self.send("tools/call", {"name": name, "arguments": arguments})

    def wait_held(self):
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and self.process.poll() is None:
            if self.events("held"):
                return True
            time.sleep(0.01)
        return False

    def close(self):
        # Even a failed handshake or assertion must release the real tool call.
        (self.root / "release").touch()
        if self.process.stdin and not self.process.stdin.closed:
            self.process.stdin.close()
        clean = True
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            clean = False
            self.process.kill()
            self.process.wait(timeout=5)
        for reader in getattr(self, "readers", []):
            reader.join(timeout=3)
        for stream in (self.process.stdout, self.process.stderr):
            if stream:
                stream.close()
        return clean

    def __exit__(self, *args):
        assert self.close(), "installed MCP failed to close after release and stdin EOF"
        assert self.process.returncode == 0, "".join(self.stderr)
        assert not self.events("network attempted") and not self.events("hold watchdog")
        public = "".join(self.public + self.stderr)
        assert "Traceback" not in public
        h.assert_no_secrets(public)
        assert self.events("finished")


@contextmanager
def held_calls(server):
    """Always release and drain outstanding RPCs before a behavioral assertion."""
    pending = []
    (server.root / "hold").touch()
    try:
        yield pending
    finally:
        (server.root / "release").touch()
        drained = server.collect(pending, 15)
        if not drained:
            server.close()
        assert drained, "confirmation responses did not drain after transport release"


def approved(server, tool="pause_entity", **arguments):
    plan = h.expect_ok(server.call(tool, arguments or {
        "entity_type": "campaign", "entity_id": "111"}))["plan"]
    result = h.expect_ok(server.call("confirm_and_apply", {"plan_id": plan["id"]}))
    assert result == {"applied": False, "plan": plan}
    return plan


@pytest.mark.parametrize("competitors", [2, 45], ids=["small-queue", "45-queued"])
def test_preview_and_health_finish_while_confirmations_wait(tmp_path, competitors):
    with QueueServer(tmp_path, env={"ADS_MCP_REQUIRE_DRY_RUN": "true"}) as server:
        plan = approved(server)
        with held_calls(server) as pending:
            first = server.tool("confirm_and_apply", plan_id=plan["id"], dry_run=False)
            pending.append(first)
            assert server.wait_held(), "apply did not reach held synthetic SDK transport"
            ready = server.send("ping", {})
            pending.append(ready)
            assert server.collect([ready], 5), "stdio was not responsive before confirmations queued"
            duplicates = [server.tool("confirm_and_apply", plan_id=plan["id"], dry_run=False)
                          for _ in range(competitors)]
            pending.extend(duplicates)
            # Flush the complete workload over real stdin. A later protocol ping
            # can itself stall when stdio I/O needs the exhausted worker pool;
            # its response is drained, never required as a fixture precondition.
            barrier = server.send("ping", {})
            pending.append(barrier)
            preview = server.tool("confirm_and_apply", plan_id=plan["id"], dry_run=True)
            health = server.tool("health_check")
            pending.extend([preview, health])
            available = server.collect([preview, health], 5)
            during_hold = set(server.answers)
            still_held = not server.events("released") and not server.events("mutation")
        # All response and safety checks run after orderly release/drain, even red.
        results = {i: server.payload(server.answers[i]) for i in [first, *duplicates, preview, health]}
        assert results[first]["applied"] is True
        assert all(h.error_of(results[i])["code"] == "PLAN_CONSUMED" for i in duplicates)
        assert len(server.events("transport entered")) == len(server.events("mutation")) == 1
        assert results[preview] == {"applied": False, "plan": plan}
        assert results[health]["status"] == "OK"
        records = server.audit()
        assert len([r for r in records if r["event"] == "applied"]) == 1
        consumed = [r for r in records if r["event"] == "refused" and r["outcome"] == "PLAN_CONSUMED"]
        assert len(consumed) == competitors
        assert all(r["plan_id"] == plan["id"] and r["customer_id"] == h.CUSTOMER_ID
                   and r["tool"] == "confirm_and_apply" for r in consumed)
        assert not ({first, *duplicates} & during_hold), "queued applies must wait, not return busy"
        assert still_held, "synthetic transport did not remain held during observation"
        assert available, (
            f"preview and health did not both respond within 5 seconds while {competitors} "
            "confirmations waited; transport released, all responses drained, one apply "
            "succeeded and every duplicate was consumed")


def test_distinct_queued_plan_rechecks_the_previous_committed_budget(tmp_path):
    with QueueServer(tmp_path, env={"ADS_MCP_REQUIRE_DRY_RUN": "true"}) as server:
        first_plan = approved(server, "update_campaign", campaign_id="111", daily_budget=150)
        second_plan = approved(server, "update_campaign", campaign_id="111", daily_budget=175)
        with held_calls(server) as pending:
            first = server.tool("confirm_and_apply", plan_id=first_plan["id"], dry_run=False)
            pending.append(first)
            assert server.wait_held()
            second = server.tool("confirm_and_apply", plan_id=second_plan["id"], dry_run=False)
            barrier = server.send("ping", {})
            pending.extend([second, barrier])
            assert server.collect([barrier], 5)
            second_returned_early = server.collect([second], 0.25)
        assert not second_returned_early, "distinct confirmations must queue behind active execution"
        assert server.payload(server.answers[first])["applied"] is True
        assert h.error_of(server.payload(server.answers[second]))["code"] == "BUDGET_CAP_EXCEEDED"
        assert [r["micros"] for r in server.events("budget committed")] == [150_000_000]
        timeline = server.observations()
        committed = next(i for i, r in enumerate(timeline) if r["event"] == "budget committed")
        assert any(r["event"] == "budget read" and r["micros"] == 150_000_000
                   for r in timeline[committed + 1:]), "queued plan did not read committed state"
        refused = [r for r in server.audit() if r["event"] == "refused"]
        assert refused[-1]["plan_id"] == second_plan["id"]
        assert refused[-1]["customer_id"] == h.CUSTOMER_ID
        later = approved(server, "update_campaign", campaign_id="111", daily_budget=125)
        assert server.call("confirm_and_apply", {"plan_id": later["id"], "dry_run": False})["applied"]
        assert [r["micros"] for r in server.events("budget committed")] == [150_000_000, 125_000_000]


def test_approval_irreversibility_audit_refusal_and_lost_write_controls(tmp_path):
    with QueueServer(tmp_path, env={"ADS_MCP_REQUIRE_DRY_RUN": "true"}) as server:
        plan = server.call("remove_entity", {"entity_type": "campaign", "entity_id": "111"})["plan"]
        args = {"plan_id": plan["id"], "dry_run": False}
        assert h.error_of(server.call("confirm_and_apply", args))["code"] == "DRY_RUN_REQUIRED"
        server.call("confirm_and_apply", {"plan_id": plan["id"]})
        assert h.error_of(server.call("confirm_and_apply", args))["code"] == "IRREVERSIBLE_CONFIRMATION_REQUIRED"
        assert not server.events("transport entered")
        assert server.call("confirm_and_apply", {**args, "confirm_irreversible": True})["applied"]
        blocked = approved(server)
        audit = h.audit_file(tmp_path)
        audit.rename(tmp_path / "saved-audit.jsonl")
        audit.mkdir()
        before = len(server.events("transport entered"))
        result = server.call("confirm_and_apply", {"plan_id": blocked["id"], "dry_run": False})
        assert h.error_of(result)["code"] == "AUDIT_WRITE_FAILED"
        assert len(server.events("transport entered")) == before
        audit.rmdir()
        later = approved(server)
        server.mode(lost_write=True)
        result = server.call("confirm_and_apply", {"plan_id": later["id"], "dry_run": False})
        assert h.error_of(result)["code"] == "MUTATION_TRANSPORT_FAILED"
        assert len(server.events("transport entered")) == before + 1, "writes must never retry"
        assert h.error_of(server.call("confirm_and_apply", {"plan_id": later["id"], "dry_run": False}))["code"] == "PLAN_CONSUMED"
        server.mode()
        recovery = approved(server)
        assert server.call("confirm_and_apply", {"plan_id": recovery["id"], "dry_run": False})["applied"]


def test_independent_process_and_read_only_registration_stay_available(tmp_path):
    with QueueServer(tmp_path / "writer") as writer, QueueServer(
        tmp_path / "reader", env={"ADS_MCP_READ_ONLY": "true"}) as reader:
        listed = reader.receive(reader.send("tools/list", {}))["result"]["tools"]
        names = {tool["name"] for tool in listed}
        assert not names & (MUTATION_TOOLS | {"confirm_and_apply"})
        plan = approved(writer)
        with held_calls(writer) as pending:
            pending.append(writer.tool("confirm_and_apply", plan_id=plan["id"], dry_run=False))
            assert writer.wait_held()
            result = reader.call("health_check")
            assert result["status"] == "OK" and result["guardrails"]["read_only"] is True
            assert not writer.events("released")
