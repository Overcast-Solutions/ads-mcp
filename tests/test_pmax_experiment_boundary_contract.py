"""Installed protocol, audit privacy and uncertain experiment receipt contracts.

The child process runs the installed console. Only the provider factory and
service transport are substituted; SDK messages, parsing, tools and storage run
normally. All provider data and filesystem markers are synthetic.
"""

from datetime import datetime, timedelta
import base64
import json
import queue
from zoneinfo import ZoneInfo

import pytest
from google.protobuf import wrappers_pb2

import harness as h
import test_auth_cause_contract as plumbing
from pmax_experiment_oracle import CREATE, OPERATION_READ, rn
from test_pmax_experiment_workflow_contract import EXPECTED_ALL, EXPECTED_READS


WIRE_MARKER = "synthetic-wire-value-do-not-disclose"
AUDIT_MARKER = "synthetic-private-audit-directory"
STATUS_MARKER = "synthetic-status-message-do-not-disclose"
DETAIL_MARKER = "synthetic-status-detail-do-not-disclose"
UNICODE_HANDLE = "opaque/operations/café-\U0001f680"
PAUSE = {"entity_type": "campaign", "entity_id": "703"}
DETAIL_BASE64 = base64.b64encode(wrappers_pb2.StringValue(
    value=DETAIL_MARKER).SerializeToString()).decode("ascii")


INJECTION = r'''
import atexit
import json
import logging
import os
from pathlib import Path
import sys
import threading
sys.path.insert(0, os.environ["BOUNDARY_TESTS"])
import harness as h
from pmax_experiment_oracle import ExperimentClient, raw_operation, rn
from google.ads.googleads.client import GoogleAdsClient
from google.protobuf import wrappers_pb2
from google.protobuf.json_format import MessageToDict
from google.rpc import status_pb2

marker = Path(os.environ["BOUNDARY_MARKER"])
control = Path(os.environ["BOUNDARY_CONTROL"])
lock = threading.Lock()
def mark(event, **values):
    with lock, marker.open("a") as stream:
        stream.write(json.dumps({"event": event, **values}) + "\n")
mark("loaded", integer_limit=sys.get_int_max_str_digits())
def offline(event, args):
    if event == "socket.bind" and args[1] == ("::1", 0):
        raise OSError("offline IPv6 capability probe")
    if event in ("socket.connect", "socket.bind", "socket.getaddrinfo", "socket.sendto"):
        mark("network attempted")
        raise OSError("offline provider contract")
sys.addaudithook(offline)

def fault_storage():
    path = Path(os.environ["ADS_MCP_AUDIT_LOG"])
    path.rename(path.with_name("preserved-post-submit.jsonl"))
    path.mkdir()

class Transport(ExperimentClient):
    def _do_search(self, service, method, args, kwargs):
        if json.loads(control.read_text()).get("warning"):
            logging.getLogger("synthetic.boundary").warning("ordinary provider diagnostic remains visible")
        mark("search", customer_id=str(h._req_field(args, kwargs, "customer_id")),
             query=str(h._req_field(args, kwargs, "query")))
        return super()._do_search(service, method, args, kwargs)

    def _do_mutation(self, service, method, args, kwargs):
        request = kwargs.get("request", args[0] if args else None)
        assert request is not None and hasattr(request, "_pb")
        if service == "GoogleAdsService":
            assert kwargs.get("retry", "unset") is None
        mode = json.loads(control.read_text())
        mark("mutation", service=service, method=method,
             proto=request._pb.DESCRIPTOR.full_name,
             request=MessageToDict(request._pb, preserving_proto_field_name=True),
             validate_only=bool(request.validate_only))
        self.after_action = fault_storage if mode.get("post_audit") else None
        self.lose_response = bool(mode.get("lost"))
        if service == "CampaignService":
            assert method == "mutate_campaigns"
            response = h.get_ads_type("MutateCampaignsResponse")
            for operation in request.operations:
                response.results.append({"resource_name": operation.update.resource_name})
            if self.after_action:
                self.after_action()
        else:
            response = super()._do_mutation(service, method, args, kwargs)
            if not request.validate_only:
                if "status" in mode:
                    status = status_pb2.Status(code=mode["status"],
                        message="synthetic-status-message-do-not-disclose")
                    status.details.add().Pack(wrappers_pb2.StringValue(
                        value="synthetic-status-detail-do-not-disclose"))
                    response._pb.partial_failure_error.CopyFrom(status)
                if mode.get("foreign_identity"):
                    response.mutate_operation_responses[0].experiment_result.resource_name = rn(
                        "experiments", 901, h.OTHER_CUSTOMER_ID)
        mark("receipt", proto=response._pb.DESCRIPTOR.full_name,
             value=MessageToDict(response._pb, preserving_proto_field_name=True))
        return response

    def action(self, method, args, kwargs):
        mark("action", method=method)
        return super().action(method, args, kwargs)

transport = Transport()
transport.data[h.CUSTOMER_ID]["experiment"][0]["experiment"]["long_running_operation"] = "opaque/operations/café-\U0001f680"
transport.operation = raw_operation(name="opaque/operations/café-\U0001f680")
def factory(*args, **kwargs):
    mark("client factory")
    return transport
GoogleAdsClient.load_from_dict = factory
def finished():
    import ads_mcp.server
    mark("finished", module_file=ads_mcp.server.__file__,
         integer_limit=sys.get_int_max_str_digits(), polls=transport.polls)
atexit.register(finished)
'''


class RecordedConsole(plumbing.InstalledServer):
    """Retain the wire transcript even when the behavior assertion is red."""

    def write_wire(self, wire):
        with (self.root / "sent-wire.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(wire)
        self.process.stdin.write(wire)
        self.process.stdin.flush()

    def send(self, method, params):
        self.next_id += 1
        self.write_wire(json.dumps({"jsonrpc": "2.0", "id": self.next_id,
                                   "method": method, "params": params}) + "\n")
        return self.next_id

    def notify(self, method):
        self.write_wire(json.dumps({"jsonrpc": "2.0", "method": method}) + "\n")

    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            (self.root / "received-wire.jsonl").write_text("".join(self.public))
            (self.root / "stderr.txt").write_text("".join(self.stderr))


@pytest.fixture
def installed(tmp_path, monkeypatch):
    monkeypatch.setattr(plumbing, "INJECTION", INJECTION)
    # Do not inherit source overrides or credentials from the invoking shell.
    monkeypatch.delenv("PYTHONPATH", raising=False)
    monkeypatch.delenv("PYTHONHOME", raising=False)
    root = tmp_path / "console"
    audit = root / AUDIT_MARKER / "audit.jsonl"
    audit.parent.mkdir(parents=True)
    with RecordedConsole(root, env={
        "ADS_MCP_AUDIT_LOG": str(audit), "ADS_MCP_REQUIRE_DRY_RUN": "true",
    }) as server:
        server.audit_path = audit
        yield server


def creation_args(name="Synthetic URL boundary trial"):
    today = datetime.now(ZoneInfo("America/Denver")).date()
    return {"campaign_id": "703", "name": name, "date_start": today.isoformat(),
            "date_end": (today + timedelta(days=30)).isoformat()}


def stage(server, tool=CREATE):
    args = creation_args() if tool == CREATE else PAUSE
    return h.expect_ok(server.call(tool, args))["plan"]


def confirmation(server, plan, *, dry_run=False):
    return server.call("confirm_and_apply", {"plan_id": plan["id"], "dry_run": dry_run})


def live_mutations(server):
    return [item for item in server.events("mutation") if not item["validate_only"]]


def private_output(server, *payloads, extra=()):
    text = "".join(server.public + server.stderr) + json.dumps(payloads, ensure_ascii=True)
    h.assert_no_secrets(text)
    forbidden = (WIRE_MARKER, STATUS_MARKER, DETAIL_MARKER, DETAIL_BASE64, AUDIT_MARKER,
                 str(server.root), str(server.audit_path), "Traceback", "IsADirectoryError",
                 "NotADirectoryError", "PermissionError", "OSError", "[Errno", "Too many levels",
                 "input_value=", "input_type=", "ValidationError:",
                 "lone leading surrogate", "lone trailing surrogate", *extra)
    assert not [value for value in forbidden if value in text], "private content escaped the boundary"


def next_response(server, timeout=3):
    try:
        return server.inbox.get(timeout=timeout)
    except queue.Empty:
        return None


def refusal(item, identity, *, malformed=False):
    assert item is not None, "installed console silently dropped the malformed request"
    assert item.get("jsonrpc") == "2.0" and len(json.dumps(item)) < 4000
    if "error" in item:
        assert item["id"] in (None, identity)
        assert item["error"]["code"] in (-32700, -32600, -32602)
        if item["id"] is None:
            assert item["error"]["code"] in (-32700, -32600)
        assert item["error"].get("message")
    else:
        assert not malformed, "malformed JSON must produce a protocol refusal"
        assert item["id"] == identity, "tool refusal lost the original request identity"
        result = item["result"]
        if result.get("isError"):
            assert result.get("content")
        else:
            assert h.error_of(plumbing.InstalledServer.payload(item))["code"] != "INTERNAL"


@pytest.mark.parametrize("surface", ["experiment-account", "experiment-name", "operation-handle", "existing-read"])
@pytest.mark.parametrize("surrogate", ["\ud800", "\udfff"], ids=["high", "low"])
def test_installed_lone_surrogates_refuse_without_provider_access_and_keep_connection(installed, surface, surrogate):
    server = installed
    hostile = WIRE_MARKER + "/synthetic/private/location/" + surrogate
    if surface == "experiment-name":
        name, args = CREATE, creation_args(hostile)
    elif surface == "operation-handle":
        name, args = OPERATION_READ, {"experiment_id": "301", "operation_name": hostile}
    else:
        name = "get_account_info" if surface == "existing-read" else "list_pmax_url_experiments"
        args = {"customer_id": hostile}
    identity = server.send("tools/call", {"name": name, "arguments": args})
    result = next_response(server)
    # Keep stdin open until both requests have been observed, including red runs.
    followup = server.receive(server.send("tools/list", {}))
    assert {tool["name"] for tool in followup["result"]["tools"]} == EXPECTED_ALL
    assert not server.events("search")
    assert not server.events("mutation") and not server.events("action")
    private_output(server, result)
    refusal(result, identity)


def test_installed_syntactically_invalid_json_returns_safe_protocol_error(installed):
    server = installed
    server.write_wire('{"jsonrpc":"2.0","id":72,"private":"' + WIRE_MARKER + '",}\n')
    result = next_response(server)
    assert "tools" in server.receive(server.send("tools/list", {}))["result"]
    assert not server.events("search") and not server.events("mutation") and not server.events("action")
    private_output(server, result)
    refusal(result, 72, malformed=True)


@pytest.mark.parametrize("value", [True, {WIRE_MARKER: "synthetic invalid typed input"}])
def test_installed_original_typed_input_refuses_before_reads(installed, value):
    server = installed
    identity = server.send("tools/call", {"name": CREATE, "arguments": {**creation_args(), "name": value}})
    refusal(server.receive(identity), identity)
    assert not server.events("search") and not server.events("mutation") and not server.events("action")
    assert "tools" in server.receive(server.send("tools/list", {}))["result"]
    private_output(server)


@pytest.mark.parametrize("ascii_encoding", [False, True], ids=["utf8", "escaped-surrogate-pair"])
def test_installed_valid_unicode_notifications_and_initialization_remain_usable(installed, ascii_encoding):
    server = installed
    server.write_wire(json.dumps({"jsonrpc": "2.0", "method": "notifications/cancelled",
        "params": {"requestId": "unused", "reason": "ordinary cancellation"}}) + "\n")
    assert "tools" in server.receive(server.send("tools/list", {}))["result"]
    assert next_response(server, 0.1) is None, "valid notifications must not receive a response"
    name = "Café landing \U0001f680"
    server.next_id += 1
    identity = server.next_id
    request = {"jsonrpc": "2.0", "id": identity, "method": "tools/call",
               "params": {"name": CREATE, "arguments": creation_args(name)}}
    wire = json.dumps(request, ensure_ascii=ascii_encoding)
    if ascii_encoding:
        assert "\\ud83d\\ude80" in wire
    server.write_wire(wire + "\n")
    plan = h.expect_ok(server.payload(server.receive(identity)))["plan"]
    assert plan["operations"][0]["name"] == name
    sent = server.events("mutation")
    assert len(sent) == 1 and sent[0]["validate_only"]
    assert sent[0]["request"]["mutate_operations"][0]["experiment_operation"]["create"]["name"] == name
    result = h.expect_ok(server.call(OPERATION_READ, {"experiment_id": "301", "operation_name": UNICODE_HANDLE}))
    assert result["operation_name"] == UNICODE_HANDLE and result["state"] == "pending"
    assert not live_mutations(server)
    private_output(server)


def storage_fault(path, kind):
    saved = path.with_name("preserved-before-fault.jsonl")
    if path.exists():
        path.rename(saved)
    previous = saved.read_bytes() if saved.exists() else None
    victim = path.with_name("synthetic-unrelated-file")
    victim.write_bytes(b"synthetic unrelated data\n")
    if kind == "directory":
        path.mkdir()
    else:
        path.symlink_to(victim)

    def restore():
        assert victim.read_bytes() == b"synthetic unrelated data\n"
        if previous is not None:
            assert saved.read_bytes() == previous
        path.rmdir() if kind == "directory" else path.unlink()
        if previous is not None:
            saved.rename(path)
    return restore


@pytest.mark.parametrize("tool", [CREATE, "pause_entity"])
@pytest.mark.parametrize("moment", ["stage", "preview", "pre-apply"])
@pytest.mark.parametrize("kind", ["directory", "symlink"])
def test_installed_critical_audit_fault_is_private_and_prevents_execution(installed, tool, moment, kind):
    server = installed
    plan = stage(server, tool) if moment != "stage" else None
    if moment == "pre-apply":
        assert h.expect_ok(confirmation(server, plan, dry_run=True))["applied"] is False
    restore = storage_fault(server.audit_path, kind)
    if moment == "stage":
        result = server.call(tool, creation_args() if tool == CREATE else PAUSE)
    else:
        result = confirmation(server, plan, dry_run=moment == "preview")
    assert not live_mutations(server), "a pre-execution audit refusal must prevent provider changes"
    h.expect_ok(server.call("list_pmax_url_experiments"))
    restore()
    if moment == "pre-apply":
        assert h.error_of(confirmation(server, plan))["code"] == "PLAN_CONSUMED"
    elif moment == "preview":
        # A refused preview cannot authorize a later apply after storage recovery.
        assert h.error_of(confirmation(server, plan))["code"] == "DRY_RUN_REQUIRED"
        assert h.expect_ok(confirmation(server, plan, dry_run=True))["applied"] is False
    assert not live_mutations(server)
    error = h.error_of(result)
    assert error["code"] == "AUDIT_WRITE_FAILED" and len(error["message"]) < 800
    assert "audit" in error["message"].lower()
    private_output(server, result)
    if moment == "preview":
        assert h.expect_ok(confirmation(server, plan))["applied"] is True
        assert len(live_mutations(server)) == 1
        assert h.error_of(confirmation(server, plan))["code"] == "PLAN_CONSUMED"


@pytest.mark.parametrize("tool", [CREATE, "pause_entity"])
def test_installed_healthy_audit_keeps_preview_and_single_use_apply(installed, tool):
    server = installed
    plan = stage(server, tool)
    assert h.expect_ok(confirmation(server, plan, dry_run=True))["applied"] is False
    assert h.expect_ok(confirmation(server, plan))["applied"] is True
    assert h.error_of(confirmation(server, plan))["code"] == "PLAN_CONSUMED"
    records = [json.loads(line) for line in server.audit_path.read_text().splitlines()]
    assert {"plan_created", "dry_run", "apply_started", "applied"} <= {r["event"] for r in records}
    assert len(live_mutations(server)) == 1
    assert server.audit_path.stat().st_mode & 0o777 == 0o600
    private_output(server)


def test_installed_post_submit_audit_failure_retains_identity_and_generic_warning(installed):
    server = installed
    plan = stage(server)
    confirmation(server, plan, dry_run=True)
    server.mode(post_audit=True)
    result = h.expect_ok(confirmation(server, plan))
    assert result["submitted"] is True and result["applied"] is False
    assert result["experiment_id"] == "901" and result["resource_name"] == rn("experiments", 901)
    warning = result["audit_warning"].lower()
    assert "submitted" in warning and "reconcile" in warning and "audit" in warning
    assert h.error_of(confirmation(server, plan))["code"] == "PLAN_CONSUMED"
    assert len(live_mutations(server)) == 1
    private_output(server, result)


@pytest.mark.parametrize("status", [1, 13, 16])
def test_installed_nonzero_status_with_complete_receipts_is_uncertain_not_verified(installed, status):
    server = installed
    plan = stage(server)
    confirmation(server, plan, dry_run=True)
    server.mode(status=status)
    result = h.expect_ok(confirmation(server, plan))
    assert h.error_of(confirmation(server, plan))["code"] == "PLAN_CONSUMED"
    assert len(live_mutations(server)) == 1
    receipt = server.events("receipt")[-1]
    assert receipt["proto"] == "google.ads.googleads.v25.services.MutateGoogleAdsResponse"
    assert len(receipt["value"]["mutate_operation_responses"]) == 4
    assert receipt["value"]["partial_failure_error"]["code"] == status
    assert receipt["value"]["partial_failure_error"]["message"] == STATUS_MARKER
    assert receipt["value"]["partial_failure_error"]["details"]
    observed = h.expect_ok(server.call("get_pmax_url_experiment", {"experiment_id": "901"}))
    assert observed["experiment"]["name"] == creation_args()["name"]
    assert observed["experiment"]["resource_name"] == rn("experiments", 901)
    assert result["submitted"] is True and result["experiment_id"] == "901"
    assert result["resource_name"] == rn("experiments", 901)
    private_output(server, result)
    assert result["applied"] is False, "contradictory nonzero receipt was announced as applied"
    assert result["verification"] in ("unknown", "failed")
    assert result.get("observation_error") and "inspect" in result["recovery"].lower()
    assert "do not" in result["recovery"].lower()
    records = [json.loads(line) for line in server.audit_path.read_text().splitlines()]
    assert not [r for r in records if r.get("plan_id") == plan["id"] and r["event"] == "applied"]


@pytest.mark.parametrize("status", [None, 0], ids=["absent", "explicit-code-zero"])
def test_installed_normal_receipt_and_code_zero_status_verify_creation(installed, status):
    server = installed
    plan = stage(server)
    confirmation(server, plan, dry_run=True)
    server.mode(**({} if status is None else {"status": status}))
    result = h.expect_ok(confirmation(server, plan))
    assert result["submitted"] is True and result["applied"] is True and result["verification"] == "verified"
    assert result["experiment_id"] == "901" and result["resource_name"] == rn("experiments", 901)
    assert len(live_mutations(server)) == 1
    assert h.error_of(confirmation(server, plan))["code"] == "PLAN_CONSUMED"
    receipt = server.events("receipt")[-1]["value"]
    if status is None:
        assert "partial_failure_error" not in receipt
    else:
        assert receipt["partial_failure_error"].get("code", 0) == 0
        assert receipt["partial_failure_error"]["message"] == STATUS_MARKER
        assert receipt["partial_failure_error"]["details"]
    private_output(server, result)


@pytest.mark.parametrize("fault", ["foreign_identity", "lost"])
def test_installed_invalid_identity_and_lost_receipt_still_consume_without_retry(installed, fault):
    server = installed
    plan = stage(server)
    confirmation(server, plan, dry_run=True)
    server.mode(**{fault: True})
    result = h.expect_ok(confirmation(server, plan))
    assert result["submitted"] is True and result["applied"] is False
    assert result["verification"] == "unknown" and result.get("observation_error")
    assert result.get("resource_name") != rn("experiments", 901, h.OTHER_CUSTOMER_ID)
    assert "inspect" in result["recovery"].lower()
    assert h.error_of(confirmation(server, plan))["code"] == "PLAN_CONSUMED"
    assert len(live_mutations(server)) == 1
    private_output(server, result)


def test_read_only_inventory_remains_exactly_34_reads(tmp_path, monkeypatch):
    monkeypatch.setattr(plumbing, "INJECTION", INJECTION)
    monkeypatch.delenv("PYTHONPATH", raising=False)
    monkeypatch.delenv("PYTHONHOME", raising=False)
    with RecordedConsole(tmp_path / "readonly", env={"ADS_MCP_READ_ONLY": "true"}) as server:
        tools = server.receive(server.send("tools/list", {}))["result"]["tools"]
        assert len(tools) == 34 and {tool["name"] for tool in tools} == EXPECTED_READS
        assert not server.events("client factory") and not server.events("mutation")


def test_unrelated_default_warning_diagnostics_are_not_silenced(installed):
    server = installed
    server.mode(warning=True)
    h.expect_ok(server.call("list_pmax_url_experiments"))
    assert "ordinary provider diagnostic remains visible" in "".join(server.stderr)
    private_output(server)
