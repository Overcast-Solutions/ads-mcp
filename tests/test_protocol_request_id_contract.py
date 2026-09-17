"""Request identity contracts at the installed stdio boundary.

The offline fixture replaces only the Google Ads client factory and provider
services. Parsing, dispatch, plans, auditing and the console run unchanged.
"""

from contextlib import contextmanager
import json
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time

import pytest

import harness as h
from test_pmax_experiment_workflow_contract import EXPECTED_ALL, EXPECTED_READS


PRIVATE = "synthetic-request-id-private-value"
WARNING = "ordinary synthetic provider warning"
NOISE = "incidental synthetic provider stdout"
CHILD_NOISE = "incidental synthetic child stdout"
INIT = {"protocolVersion": "2025-03-26", "capabilities": {},
        "clientInfo": {"name": "request-identity-contract", "version": "1"}}
INVALID_IDS = [
    pytest.param({"private": PRIVATE}, id="object"),
    pytest.param([PRIVATE], id="array"),
    pytest.param(None, id="null"),
    pytest.param(True, id="true"),
    pytest.param(False, id="false"),
    pytest.param(1.5, id="fractional"),
    pytest.param(1.0, id="float-one"),
]
VALID_IDS = [0, -1, 9007199254740993, 2**127, "", "café-\U0001f680", "ordinary"]


PROVIDER = r'''
import atexit
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
import threading
sys.path.insert(0, os.environ["IDENTITY_TESTS"])
import harness as h
from google.ads.googleads.client import GoogleAdsClient
from google.protobuf.json_format import MessageToDict

marker = Path(os.environ["IDENTITY_MARKER"])
lock = threading.Lock()
def mark(event, **values):
    with lock, marker.open("a") as stream:
        stream.write(json.dumps({"event": event, **values}) + "\n")
mark("loaded", integer_limit=sys.get_int_max_str_digits())
def offline(event, args):
    if event == "socket.bind" and args[1] == ("::1", 0):
        raise OSError("offline IPv6 capability probe")
    if event in ("socket.connect", "socket.bind", "socket.getaddrinfo", "socket.sendto"):
        mark("network attempted", operation=event)
        raise OSError("offline provider contract")
sys.addaudithook(offline)

accounts = {
    h.CUSTOMER_ID: {"descriptive_name": "Synthetic North", "currency_code": "USD", "time_zone": "America/Denver"},
    h.OTHER_CUSTOMER_ID: {"descriptive_name": "Synthetic South", "currency_code": "EUR", "time_zone": "Europe/Paris"},
}
class Transport(h.FakeGoogleAdsClient):
    def get_service(self, name, version=None):
        mark("service", name=name)
        return super().get_service(name, version=version)

    def _do_search(self, service, method, args, kwargs):
        customer = str(h._req_field(args, kwargs, "customer_id"))
        query = str(h._req_field(args, kwargs, "query"))
        mark("search", customer_id=customer, query=query)
        assert customer in accounts
        assert h._FROM_RE.search(query).group(1) == "customer"
        row = h.make_row({"customer": {"id": int(customer),
            "resource_name": "customers/" + customer, **accounts[customer]}})
        assert row._pb.DESCRIPTOR.full_name.endswith("GoogleAdsRow")
        mark("row", proto=row._pb.DESCRIPTOR.full_name, customer_id=customer)
        if os.environ.get("IDENTITY_NOISE") == "1":
            print("incidental synthetic provider stdout", flush=True)
            subprocess.run([sys.executable, "-S", "-c",
                "print('incidental synthetic child stdout', flush=True)"], check=True)
            logging.getLogger("synthetic.provider").warning("ordinary synthetic provider warning")
        return [row]

    def _do_mutation(self, service, method, args, kwargs):
        request = kwargs.get("request", args[0] if args else None)
        assert service == "CampaignService" and method == "mutate_campaigns"
        assert request._pb.DESCRIPTOR.full_name.endswith("MutateCampaignsRequest")
        assert request.customer_id in accounts
        mark("mutation", proto=request._pb.DESCRIPTOR.full_name,
             request=MessageToDict(request._pb, preserving_proto_field_name=True),
             validate_only=bool(request.validate_only))
        response = h.get_ads_type("MutateCampaignsResponse")
        for operation in request.operations:
            response.results.append({"resource_name": operation.update.resource_name})
        mark("receipt", proto=response._pb.DESCRIPTOR.full_name)
        return response

transport = Transport()
def factory(*args, **kwargs):
    mark("factory")
    return transport
GoogleAdsClient.load_from_dict = factory
def finished():
    import ads_mcp.server
    mark("finished", module_file=ads_mcp.server.__file__, integer_limit=sys.get_int_max_str_digits())
atexit.register(finished)
'''


class Console:
    """Observe the real console without replacing its protocol implementation."""

    def __init__(self, root, *, writes=False, provider=False, customer=h.CUSTOMER_ID, noise=False):
        self.root = Path(root)
        self.root.mkdir(parents=True)
        self.marker = self.root / "provider-events.jsonl"
        self.audit = self.root / "audit.jsonl"
        self.writes = writes
        self.provider = provider
        env = h.scrubbed_env(h.google_ads_env(self.root))
        env.pop("PYTHONPATH", None)
        env.pop("PYTHONHOME", None)
        env.update(h.rw_env(self.root))
        env.update({"ADS_MCP_READ_ONLY": str(not writes).lower(),
                    "ADS_MCP_REQUIRE_DRY_RUN": "true",
                    "GOOGLE_ADS_CUSTOMER_ID": customer,
                    "PYTHONDONTWRITEBYTECODE": "1"})
        if provider:
            injection = self.root / "injection"
            injection.mkdir()
            (injection / "sitecustomize.py").write_text(PROVIDER)
            env.update({"PYTHONPATH": str(injection),
                        "IDENTITY_TESTS": str(Path(__file__).parent),
                        "IDENTITY_MARKER": str(self.marker),
                        "IDENTITY_NOISE": str(int(noise))})
        self.process = subprocess.Popen([str(h.console_script())], cwd=self.root,
            env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8", bufsize=1)
        self.inbox = queue.Queue()
        self.public, self.errors, self.sent = [], [], []
        self.sequence = 0

        def read_stdout():
            for line in self.process.stdout:
                self.public.append(line)
                try:
                    item = json.loads(line)
                    assert isinstance(item, dict) and item.get("jsonrpc") == "2.0"
                except (ValueError, AssertionError) as exc:
                    item = AssertionError("non-protocol stdout: " + repr(line))
                    item.__cause__ = exc
                self.inbox.put(item)

        def read_stderr():
            self.errors.extend(self.process.stderr)

        self.readers = [threading.Thread(target=read_stdout, daemon=True),
                        threading.Thread(target=read_stderr, daemon=True)]
        for reader in self.readers:
            reader.start()

    def wire(self, text):
        assert self.process.poll() is None and not self.process.stdin.closed
        self.sent.append(text)
        self.process.stdin.write(text)
        self.process.stdin.flush()

    def message(self, message):
        self.wire(json.dumps(message, ensure_ascii=True) + "\n")

    def request(self, method, params=None, *, identity=None):
        if identity is None:
            self.sequence += 1
            identity = "control-" + str(self.sequence)
        self.message({"jsonrpc": "2.0", "id": identity, "method": method,
                      "params": params or {}})
        item = self.take()
        assert type(item.get("id")) is type(identity) and item["id"] == identity, item
        assert "result" in item and "error" not in item, item
        return item["result"]

    def take(self, timeout=15):
        item = self.inbox.get(timeout=timeout)
        if isinstance(item, Exception):
            raise item
        return item

    def quiet(self, timeout=0.15):
        try:
            item = self.take(timeout)
        except queue.Empty:
            return
        raise AssertionError("unexpected additional protocol message: " + repr(item))

    def initialize(self, identity="initialization"):
        result = self.request("initialize", INIT, identity=identity)
        assert result["serverInfo"]["name"] == "ads-mcp"
        self.message({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def call(self, name, arguments=None):
        result = self.request("tools/call", {"name": name, "arguments": arguments or {}})
        assert not result.get("isError"), result
        payload = result.get("structuredContent")
        if payload is None:
            payload = json.loads("".join(part.get("text", "") for part in result["content"]))
        return payload["result"] if set(payload) == {"result"} else payload

    def events(self):
        return [json.loads(line) for line in self.marker.read_text().splitlines()] if self.marker.exists() else []

    def state(self):
        return (self.events(), self.audit.read_bytes() if self.audit.exists() else None)

    def malformed_then_recover(self, wire, *, before_initialize=False):
        """Retain missing/extra replies and verify recovery before asserting refusal."""
        self.wire(wire)
        self.sequence += 1
        identity = "recovery-" + str(self.sequence)
        method, params = ("initialize", INIT) if before_initialize else ("ping", {})
        self.message({"jsonrpc": "2.0", "id": identity, "method": method, "params": params})
        deadline = time.monotonic() + 15
        replies, recovered = [], None
        while recovered is None:
            item = self.take(max(0.01, deadline - time.monotonic()))
            if item.get("id") == identity:
                recovered = item
            else:
                replies.append(item)
        # The follow-up does not justify assuming an asynchronous refusal's order.
        deadline = time.monotonic() + (0.15 if replies else 1.0)
        while time.monotonic() < deadline:
            try:
                replies.append(self.take(max(0.01, deadline - time.monotonic())))
            except queue.Empty:
                break
        assert recovered.get("jsonrpc") == "2.0" and "result" in recovered and "error" not in recovered
        if before_initialize:
            assert "serverInfo" in recovered["result"]
            self.message({"jsonrpc": "2.0", "method": "notifications/initialized"})
        assert self.process.poll() is None and not self.process.stdin.closed
        return replies

    def finish(self):
        self.process.stdin.close()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)
            raise AssertionError("installed console did not terminate after stdin EOF")
        finally:
            for reader in self.readers:
                reader.join(timeout=3)
            for name, lines in [("sent-wire.jsonl", self.sent),
                                ("received-wire.jsonl", self.public), ("stderr.txt", self.errors)]:
                (self.root / name).write_text("".join(lines))
            self.process.stdout.close()
            self.process.stderr.close()
        assert not any(reader.is_alive() for reader in self.readers)
        assert self.process.returncode == 0, "".join(self.errors)
        if self.provider:
            events = self.events()
            assert events[0]["event"] == "loaded" and events[-1]["event"] == "finished"
            assert events[0]["integer_limit"] == events[-1]["integer_limit"]
            assert not any(event["event"] == "network attempted" for event in events)
        private_output(self)


@contextmanager
def installed(root, **kwargs):
    server = Console(root, **kwargs)
    try:
        yield server
    finally:
        server.finish()


def private_output(server):
    text = "".join(server.public + server.errors)
    h.assert_no_secrets(text)
    for value in (PRIVATE, str(server.root), "Traceback", "input_value=", "input_type=",
                  "ValidationError:", "lone leading surrogate", "lone trailing surrogate"):
        assert value not in text, "private request or exception detail reached public output"


def invalid_wire(identity, method, params):
    return json.dumps({"jsonrpc": "2.0", "id": identity, "method": method,
                       "params": params, "private": PRIVATE}) + "\n"


def refusal(replies):
    assert len(replies) == 1, "invalid request ID needs exactly one refusal; observed " + repr(replies)
    item = replies[0]
    assert item.get("jsonrpc") == "2.0" and item.get("id") is None and "result" not in item
    assert item["error"]["code"] == -32600
    assert isinstance(item["error"].get("message"), str) and 0 < len(item["error"]["message"]) <= 200
    assert "data" not in item["error"] or item["error"]["data"] is None
    assert len(json.dumps(item)) < 400


def catalog(server):
    names = {tool["name"] for tool in server.request("tools/list")["tools"]}
    assert names == (EXPECTED_ALL if server.writes else EXPECTED_READS)


@pytest.mark.parametrize("identity", INVALID_IDS)
@pytest.mark.parametrize("method", ["initialize", "ping", "tools/list"])
@pytest.mark.parametrize("writes", [False, True], ids=["read-only", "writes"])
def test_unmodified_console_refuses_invalid_ids_and_recovers(tmp_path, identity, method, writes):
    with installed(tmp_path / "console", writes=writes) as server:
        if method != "initialize":
            server.initialize()
        replies = server.malformed_then_recover(invalid_wire(identity, method, INIT if method == "initialize" else {}),
                                                before_initialize=method == "initialize")
        catalog(server)
        server.quiet()
        private_output(server)
        refusal(replies)


def staged(server):
    return h.expect_ok(server.call("pause_entity", {"entity_type": "campaign", "entity_id": "111"}))["plan"]


def confirm(server, plan, *, preview=False):
    return server.call("confirm_and_apply", {"plan_id": plan["id"], "dry_run": preview})


@pytest.mark.parametrize("identity", INVALID_IDS)
@pytest.mark.parametrize("surface", ["read", "stage", "preview", "apply"])
def test_invalid_tool_ids_have_no_provider_or_plan_effects(tmp_path, identity, surface):
    with installed(tmp_path / "console", writes=True, provider=True) as server:
        server.initialize()
        plan = staged(server) if surface in ("preview", "apply") else None
        if surface == "apply":
            assert h.expect_ok(confirm(server, plan, preview=True))["applied"] is False
        if surface == "read":
            name, args = "get_account_info", {"customer_id": h.OTHER_CUSTOMER_ID}
        elif surface == "stage":
            name, args = "pause_entity", {"entity_type": "campaign", "entity_id": "111"}
        else:
            name, args = "confirm_and_apply", {"plan_id": plan["id"], "dry_run": surface == "preview"}
        before = server.state()
        replies = server.malformed_then_recover(invalid_wire(identity, "tools/call", {"name": name, "arguments": args}))
        assert server.state() == before, "invalid identity reached provider or changed audit/plan state"
        # Green workflow controls execute before the deliberately red refusal assertion.
        for customer, label in [(h.CUSTOMER_ID, "Synthetic North"), (h.OTHER_CUSTOMER_ID, "Synthetic South")]:
            payload = h.expect_ok(server.call("get_account_info", {"customer_id": customer}))
            assert label in json.dumps(payload)
            assert ("Synthetic South" if customer == h.CUSTOMER_ID else "Synthetic North") not in json.dumps(payload)
        if surface != "read":
            plan = plan or staged(server)
            if surface == "preview":
                assert h.error_of(confirm(server, plan))["code"] == "DRY_RUN_REQUIRED"
            assert h.expect_ok(confirm(server, plan, preview=True))["applied"] is False
            assert h.expect_ok(confirm(server, plan))["applied"] is True
            assert h.error_of(confirm(server, plan))["code"] == "PLAN_CONSUMED"
            mutations = [event for event in server.events() if event["event"] == "mutation"]
            assert len(mutations) == 1 and mutations[0]["validate_only"] is False
            assert mutations[0]["request"]["customer_id"] == h.CUSTOMER_ID
            assert mutations[0]["request"]["operations"][0]["update"]["resource_name"] == f"customers/{h.CUSTOMER_ID}/campaigns/111"
        private_output(server)
        refusal(replies)


@pytest.mark.parametrize("writes", [False, True], ids=["read-only", "writes"])
def test_valid_ids_notifications_and_catalogs_remain_compatible(tmp_path, writes):
    with installed(tmp_path / "console", writes=writes) as server:
        server.initialize(identity=0)
        for identity in VALID_IDS:
            assert server.request("ping", identity=identity) == {}
            names = {tool["name"] for tool in server.request("tools/list", identity=identity)["tools"]}
            assert names == (EXPECTED_ALL if writes else EXPECTED_READS)
        server.message({"jsonrpc": "2.0", "method": "notifications/progress",
                        "params": {"progressToken": "synthetic", "progress": 1, "total": 2}})
        server.message({"jsonrpc": "2.0", "method": "notifications/initialized"})
        assert server.request("ping", identity="after-notifications") == {}
        server.quiet()


@pytest.mark.parametrize("writes", [False, True], ids=["read-only", "writes"])
def test_provider_controls_keep_accounts_stdout_and_warnings_separate(tmp_path, writes):
    with installed(tmp_path / "console", writes=writes, provider=True, noise=True) as server:
        server.initialize()
        for customer, label in [(h.CUSTOMER_ID, "Synthetic North"), (h.OTHER_CUSTOMER_ID, "Synthetic South")]:
            payload = h.expect_ok(server.call("get_account_info", {"customer_id": customer}))
            assert label in json.dumps(payload)
            assert ("Synthetic South" if customer == h.CUSTOMER_ID else "Synthetic North") not in json.dumps(payload)
        catalog(server)
        server.quiet()
        if writes:
            before = server.events()
            foreign = server.call("pause_entity", {"entity_type": "campaign", "entity_id": "111",
                                                   "customer_id": h.OTHER_CUSTOMER_ID})
            assert h.error_of(foreign)["code"] == "PLAN_CUSTOMER_MISMATCH"
            assert server.events() == before
            plan = staged(server)
            assert h.expect_ok(confirm(server, plan, preview=True))["applied"] is False
            assert h.expect_ok(confirm(server, plan))["applied"] is True
            assert h.error_of(confirm(server, plan))["code"] == "PLAN_CONSUMED"
        events = server.events()
        assert {event["customer_id"] for event in events if event["event"] == "search"} == {h.CUSTOMER_ID, h.OTHER_CUSTOMER_ID}
        assert {event["customer_id"] for event in events if event["event"] == "row"} == {h.CUSTOMER_ID, h.OTHER_CUSTOMER_ID}
    assert WARNING in "".join(server.errors)
    for value in (NOISE, CHILD_NOISE):
        assert value in "".join(server.errors) and value not in "".join(server.public)


@pytest.mark.parametrize("wire", [
    '{"jsonrpc":"2.0","id":19,"private":"' + PRIVATE + '",}\n',
    json.dumps({"jsonrpc": "2.0", "id": 19, "method": "tools/call", "params": {
        "name": "get_account_info", "arguments": {"customer_id": PRIVATE + "\ud800"}}}) + "\n",
    json.dumps({"jsonrpc": "2.0", "id": 19, "method": "tools/call", "params": {
        "name": "get_account_info", "arguments": {"customer_id": PRIVATE + "\udfff"}}}) + "\n",
], ids=["malformed-json", "high-surrogate", "low-surrogate"])
def test_existing_malformed_wire_refusal_and_recovery_survive(tmp_path, wire):
    with installed(tmp_path / "console", provider=True) as server:
        server.initialize()
        before = server.state()
        replies = server.malformed_then_recover(wire)
        assert server.state() == before
        catalog(server)
        private_output(server)
        refusal(replies)


@pytest.mark.parametrize("writes", [False, True], ids=["read-only", "writes"])
def test_sdk_response_envelopes_are_not_reclassified_as_requests(tmp_path, writes):
    with installed(tmp_path / "console", writes=writes) as server:
        server.initialize()
        # Responses have no method. A null error-response ID is distinct from
        # an explicitly null ID on a method-bearing request.
        for message in [
            {"jsonrpc": "2.0", "id": "completed", "result": {}},
            {"jsonrpc": "2.0", "id": 0, "result": {}},
            {"jsonrpc": "2.0", "id": None,
             "error": {"code": -32600, "message": "Invalid request"}},
        ]:
            server.message(message)
            assert server.request("ping") == {}
            server.quiet()
