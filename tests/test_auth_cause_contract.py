"""F047: installed stdio authentication causes at safety-read boundaries.

Only the SDK client factory/transport is synthetic. The installed console,
configuration, MCP dispatch, error classification, plan store and audit execute
normally. The neutral InstalledServer driver is also used by F049; it does not
assert or require F047 behavior. Positive controls are intentionally green.
"""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from pathlib import Path
import queue
import subprocess
import threading

import pytest

import harness as h
from tool_catalog import MUTATION_ARGS


DETAIL = "synthetic-safety-provider-detail-DO-NOT-DISCLOSE"
INJECTION = r'''
import atexit
from dataclasses import asdict
import json
import os
from pathlib import Path
import sys
import threading
sys.path.insert(0, os.environ["BOUNDARY_TESTS"])
import harness as h
from google.ads.googleads.client import GoogleAdsClient
from google.auth.exceptions import GoogleAuthError, RefreshError
from google.api_core.exceptions import InvalidArgument
from google.protobuf.json_format import MessageToDict
from ads_mcp.errors import AuthConfigError

marker = Path(os.environ["BOUNDARY_MARKER"])
control = Path(os.environ["BOUNDARY_CONTROL"])
lock = threading.Lock()
barrier = threading.Barrier(2)
def mark(event, **fields):
    with lock, marker.open("a") as stream:
        stream.write(json.dumps({"event": event, **fields}) + "\n")
mark("loaded", integer_limit=sys.get_int_max_str_digits())
def deny(event, args):
    if event == "socket.bind" and args[1] == ("::1", 0):
        raise OSError("offline IPv6 probe")
    if event in ("socket.connect", "socket.bind", "socket.getaddrinfo"):
        mark("network attempted", operation=event)
        raise OSError("offline contract forbids network")
sys.addaudithook(deny)

class Transport(h.FakeGoogleAdsClient):
    def _do_search(self, service, method, args, kwargs):
        rows = super()._do_search(service, method, args, kwargs)
        # Derive observation from this invocation, never a shared list tail:
        # two concurrent account reads must not race in the fixture itself.
        call = h.SearchCall(service, method,
            str(h._req_field(args, kwargs, "customer_id")),
            str(h._req_field(args, kwargs, "query")),
            str(h._req_field(args, kwargs, "page_token")),
            int(h._req_field(args, kwargs, "page_size", 0) or 0))
        mode = json.loads(control.read_text())
        mark("search", **asdict(call))
        resource = h._FROM_RE.search(call.query).group(1)
        fault = mode.get("fault")
        if fault and resource in mode.get("resources", [resource]):
            if mode.get("overlap"):
                barrier.wait(timeout=10)
            mark("fault", kind=fault, customer_id=call.customer_id)
            private = os.environ["BOUNDARY_DETAIL"] + " " + h.FAKE_REFRESH_TOKEN
            if fault == "revoked":
                raise RefreshError("invalid_grant " + private)
            if fault == "auth":
                raise GoogleAuthError(private)
            if fault == "auth-config":
                raise AuthConfigError("AUTH_CONFIG_INCOMPLETE", "credential files require non-blank strings for: refresh_token")
            if fault == "non-auth":
                raise InvalidArgument(private)
            if fault == "missing":
                return []
            if fault == "mismatch":
                if resource == "asset":
                    rows[0].asset.resource_name = "customers/1234567890/assets/801"
                elif resource == "campaign":
                    rows[0].campaign.resource_name = "customers/1234567890/campaigns/111"
                else:
                    rows[0].ad_group.id = 999
        if resource == "customer" and "accounts" in mode:
            account = mode["accounts"][call.customer_id]
            return [h.make_row({"customer": {"id": int(call.customer_id), **account}})]
        if resource == "recommendation" and "recommendations" in mode:
            return [h.make_row({"recommendation": {
                "resource_name": "customers/" + call.customer_id + "/recommendations/" + identity,
                "type_": "CALLOUT_ASSET"}}) for identity in mode["recommendations"]]
        return rows

    def _do_mutation(self, service, method, args, kwargs):
        result = super()._do_mutation(service, method, args, kwargs)
        item = self.mutations[-1]
        assert item.request is not None and hasattr(item.request, "_pb")
        mark("mutation", service=service, method=method,
             request=MessageToDict(item.request._pb, preserving_proto_field_name=True),
             validate_only=item.validate_only)
        return result

class ForecastService:
    def __init__(self, owner):
        self.owner = owner
    def generate_keyword_forecast_metrics(self, request):
        assert type(request).__name__ == "GenerateKeywordForecastMetricsRequest"
        mark("forecast", request=MessageToDict(request._pb, preserving_proto_field_name=True),
             login_customer_id=self.owner.login_customer_id)
        mode = json.loads(control.read_text())
        result = h.get_ads_type("GenerateKeywordForecastMetricsResponse")
        if mode.get("metrics") is not None:
            result.campaign_forecast_metrics = mode["metrics"]
        return result

transport = h.stub_standard_account(Transport())
# Isolated server processes can be configured for either synthetic customer.
customer = os.environ["GOOGLE_ADS_CUSTOMER_ID"].replace("-", "")
if customer != h.CUSTOMER_ID:
    for resource, rows in transport._responses.items():
        transport.stub(resource, [json.loads(json.dumps(MessageToDict(
            row._pb, preserving_proto_field_name=True)).replace(h.CUSTOMER_ID, customer))
            for row in rows])
transport._services["KeywordPlanIdeaService"] = ForecastService(transport)
def factory(*args, **kwargs):
    mark("client factory")
    return transport
GoogleAdsClient.load_from_dict = factory
def finished():
    import ads_mcp.server
    mark("finished", module_file=ads_mcp.server.__file__,
         integer_limit=sys.get_int_max_str_digits())
atexit.register(finished)
'''


class InstalledServer:
    """A real installed ads-mcp process with synchronous JSON-RPC observation."""
    def __init__(self, root, *, customer=h.CUSTOMER_ID, env=None):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.control = self.root / "transport-control.json"
        self.control.write_text("{}")
        self.marker = self.root / "transport-events.jsonl"
        injection = self.root / "injection"
        injection.mkdir()
        (injection / "sitecustomize.py").write_text(INJECTION)
        child_env = h.google_ads_env(self.root)
        child_env.update(h.rw_env(self.root))
        child_env.update({"GOOGLE_ADS_CUSTOMER_ID": customer,
            "ADS_MCP_RETRY_BASE_SECONDS": "0.001", "PYTHONPATH": str(injection),
            "PYTHONDONTWRITEBYTECODE": "1", "BOUNDARY_TESTS": str(Path(__file__).parent),
            "BOUNDARY_CONTROL": str(self.control), "BOUNDARY_MARKER": str(self.marker),
            "BOUNDARY_DETAIL": DETAIL})
        child_env.update(env or {})
        self.process = subprocess.Popen([str(h.console_script())], cwd=self.root,
            env=h.scrubbed_env(child_env), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, bufsize=1)
        self.inbox = queue.Queue()
        self.public = []
        self.stderr = []
        self.next_id = 0
        def read_stdout():
            for line in self.process.stdout:
                self.public.append(line)
                self.inbox.put(json.loads(line))
        def read_stderr():
            self.stderr.extend(self.process.stderr)
        self.readers = [threading.Thread(target=read_stdout, daemon=True),
                        threading.Thread(target=read_stderr, daemon=True)]
        for reader in self.readers:
            reader.start()
        identity = self.send("initialize", {"protocolVersion": "2025-03-26",
            "capabilities": {}, "clientInfo": {"name": "offline-boundary-contract", "version": "1"}})
        assert "serverInfo" in self.receive(identity)["result"]
        self.notify("notifications/initialized")

    def notify(self, method):
        self.process.stdin.write(json.dumps({"jsonrpc": "2.0", "method": method}) + "\n")
        self.process.stdin.flush()

    def send(self, method, params):
        self.next_id += 1
        self.process.stdin.write(json.dumps({"jsonrpc": "2.0", "id": self.next_id,
            "method": method, "params": params}) + "\n")
        self.process.stdin.flush()
        return self.next_id

    def receive(self, identity):
        while True:
            item = self.inbox.get(timeout=25)
            if "id" in item:
                assert item["id"] == identity, item
                return item

    @staticmethod
    def payload(item):
        assert "error" not in item, item
        result = item["result"]
        assert not result.get("isError"), result
        value = result.get("structuredContent")
        if value is None:
            value = json.loads("".join(part.get("text", "") for part in result["content"]))
        if set(value) == {"result"}:
            value = value["result"]
        return value

    def call(self, name, args=None):
        return self.payload(self.receive(self.send("tools/call", {"name": name, "arguments": args or {}})))

    def concurrent(self, calls):
        identities = [self.send("tools/call", {"name": name, "arguments": args}) for name, args in calls]
        answers = {}
        while len(answers) < len(identities):
            item = self.inbox.get(timeout=25)
            if "id" in item:
                answers[item["id"]] = self.payload(item)
        return [answers[identity] for identity in identities]

    def mode(self, **settings):
        self.control.write_text(json.dumps(settings))

    def events(self, kind):
        return [row for row in self.observations() if row["event"] == kind]

    def observations(self):
        return [json.loads(line) for line in self.marker.read_text().splitlines()]

    def audit(self):
        return h.read_audit_records(self.root)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.process.stdin.close()
        try:
            self.process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=5)
            raise AssertionError("installed MCP did not close after stdin EOF")
        for reader in self.readers:
            reader.join(timeout=3)
        rows = self.observations()
        assert rows[0]["event"] == "loaded"
        assert rows[-1]["event"] == "finished" and rows[-1]["module_file"]
        assert rows[0]["integer_limit"] == rows[-1]["integer_limit"], "do not change the global Python integer limit"
        assert not self.events("network attempted")
        assert self.process.returncode == 0, "".join(self.stderr)
        text = "".join(self.public + self.stderr)
        assert "Traceback" not in text
        h.assert_no_secrets(text)


SURFACES = {
    "bid": ("update_keyword_bid", {"ad_group_id": "201", "criterion_id": "401", "current_bid": 1.2, "new_bid": 1.25}, "ad_group_criterion", "BID_BASELINE_UNVERIFIED"),
    "budget": ("update_campaign", {"campaign_id": "111", "daily_budget": 50}, "campaign", "BUDGET_BASELINE_UNVERIFIED"),
    "strategy": ("update_campaign", {"campaign_id": "111", "target_roas": 2.5}, "campaign", "CAMPAIGN_STRATEGY_UNVERIFIED"),
    "image": ("create_pmax_campaign", MUTATION_ARGS["create_pmax_campaign"], "asset", "IMAGE_ASSET_UNVERIFIED"),
}


def staged(server, name):
    tool, args, _, _ = SURFACES[name]
    return h.expect_ok(server.call(tool, deepcopy(args)))["plan"]


def apply(server, plan, **kwargs):
    return server.call("confirm_and_apply", {"plan_id": plan["id"], "dry_run": False, **kwargs})


def clean_auth(payload, code):
    error = h.error_of(payload)
    assert error["code"] == code, error
    assert len(error["message"]) <= 600
    assert DETAIL not in json.dumps(payload)
    h.assert_no_secrets(json.dumps(payload))
    if code == "AUTH_TOKEN_REVOKED":
        assert "ads-mcp-generate-token" in error["message"] and "restart" in error["message"].lower()
    return error


@pytest.mark.parametrize("surface", SURFACES)
@pytest.mark.parametrize("phase", ["stage", "apply"])
@pytest.mark.parametrize("fault,code", [("revoked", "AUTH_TOKEN_REVOKED"), ("auth", "AUTH_FAILED"), ("auth-config", "AUTH_CONFIG_INCOMPLETE")])
def test_classified_auth_survives_every_safety_read(tmp_path, surface, phase, fault, code):
    with InstalledServer(tmp_path) as server:
        tool, args, resource, _ = SURFACES[surface]
        plan = staged(server, surface) if phase == "apply" else None
        before = len(server.events("search"))
        server.mode(fault=fault, resources=[resource])
        result = apply(server, plan) if plan else server.call(tool, deepcopy(args))
        assert len(server.events("search")) - before == 1, "authentication errors must not retry"
        assert not server.events("mutation")
        clean_auth(result, code)
        records = server.audit()
        refusals = [r for r in records if r["event"] == "refused"]
        assert len(refusals) == 1
        current_tool = "confirm_and_apply" if plan else tool
        assert refusals[0]["tool"] == current_tool and refusals[0]["customer_id"] == h.CUSTOMER_ID
        if plan:
            assert refusals[0]["plan_id"] == plan["id"]
        if fault == "revoked":
            events = [r for r in records if r["event"] == "auth_failure"]
            assert len(events) == 1 and events[0]["tool"] == current_tool
            assert events[0]["customer_id"] == h.CUSTOMER_ID
        assert DETAIL not in json.dumps(records)
        h.assert_no_secrets(json.dumps(records))


@pytest.mark.parametrize("surface", SURFACES)
def test_failed_auth_apply_remains_recoverable_then_single_use(tmp_path, surface):
    with InstalledServer(tmp_path) as server:
        plan = staged(server, surface)
        server.mode(fault="revoked")
        refusal = apply(server, plan)
        assert not server.events("mutation")
        server.mode()
        preview = h.expect_ok(server.call("confirm_and_apply", {"plan_id": plan["id"], "dry_run": True}))
        assert preview["plan"]["id"] == plan["id"]
        assert h.expect_ok(apply(server, plan))["applied"]
        writes = len(server.events("mutation"))
        assert writes >= 1
        assert h.error_of(apply(server, plan))["code"] == "PLAN_CONSUMED"
        assert len(server.events("mutation")) == writes
        clean_auth(refusal, "AUTH_TOKEN_REVOKED")


@pytest.mark.parametrize("surface", SURFACES)
@pytest.mark.parametrize("fault", ["missing", "mismatch", "non-auth"])
@pytest.mark.parametrize("phase", ["stage", "apply"])
def test_positive_control_unverified_non_auth_evidence_keeps_refusal(tmp_path, surface, fault, phase):
    with InstalledServer(tmp_path) as server:
        tool, args, resource, code = SURFACES[surface]
        plan = staged(server, surface) if phase == "apply" else None
        server.mode(fault=fault, resources=[resource])
        result = apply(server, plan) if plan else server.call(tool, deepcopy(args))
        assert h.error_of(result)["code"] == code
        assert not server.events("mutation") and not [r for r in server.audit() if r["event"] == "auth_failure"]


@pytest.mark.parametrize("surface", SURFACES)
def test_positive_control_verified_safety_reads_still_preview_and_apply_once(tmp_path, surface):
    with InstalledServer(tmp_path, env={"ADS_MCP_REQUIRE_DRY_RUN": "true"}) as server:
        plan = staged(server, surface)
        assert not server.events("mutation")
        assert h.error_of(apply(server, plan))["code"] == "DRY_RUN_REQUIRED"
        preview = h.expect_ok(server.call("confirm_and_apply", {"plan_id": plan["id"], "dry_run": True}))
        assert preview["plan"]["operations"] == plan["operations"]
        assert h.expect_ok(apply(server, plan))["applied"]
        count = len(server.events("mutation"))
        assert count >= 1
        assert h.error_of(apply(server, plan))["code"] == "PLAN_CONSUMED"
        assert len(server.events("mutation")) == count


def test_sequential_and_concurrent_calls_do_not_reuse_customer_tool_or_plan(tmp_path):
    with InstalledServer(tmp_path) as server:
        plan = staged(server, "bid")
        server.mode(fault="revoked")
        failed_apply = apply(server, plan)
        # The same context serves a later read for another account.
        failed_read = server.call("run_gaql", {"query": "SELECT customer.id FROM customer", "customer_id": h.OTHER_CUSTOMER_ID})
        before = len(server.audit())
        server.mode(fault="revoked", overlap=True)
        results = server.concurrent([
            ("update_campaign", {"campaign_id": "111", "daily_budget": 50}),
            ("run_gaql", {"query": "SELECT customer.id FROM customer", "customer_id": h.OTHER_CUSTOMER_ID})])
        for result in [failed_apply, failed_read, *results]:
            clean_auth(result, "AUTH_TOKEN_REVOKED")
        all_auth = [r for r in server.audit() if r["event"] == "auth_failure"]
        assert len(all_auth) == 4
        current = [r for r in server.audit()[before:] if r["event"] == "auth_failure"]
        assert {(r["tool"], r["customer_id"]) for r in current} == {
            ("update_campaign", h.CUSTOMER_ID), ("run_gaql", h.OTHER_CUSTOMER_ID)}
        for record in all_auth[1:]:
            assert not record.get("plan_id"), "later calls inherited a previous apply plan"
        assert not server.events("mutation")


def test_concurrent_independent_account_contexts_keep_auth_attribution(tmp_path):
    def check(customer):
        with InstalledServer(tmp_path / customer, customer=customer) as server:
            plan = staged(server, "budget")
            server.mode(fault="revoked")
            result = apply(server, plan)
            records = server.audit()
            clean_auth(result, "AUTH_TOKEN_REVOKED")
            assert {r["customer_id"] for r in records} == {customer}
            assert len([r for r in records if r["event"] == "auth_failure"]) == 1
            assert [r for r in records if r["event"] == "refused"][0]["plan_id"] == plan["id"]
            assert not server.events("mutation")
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(check, [h.CUSTOMER_ID, h.OTHER_CUSTOMER_ID]))


@pytest.mark.parametrize("tool", ["run_gaql", "health_check"])
def test_positive_control_read_and_health_keep_revoked_recovery(tmp_path, tool):
    with InstalledServer(tmp_path) as server:
        server.mode(fault="revoked")
        args = {"query": "SELECT customer.id FROM customer", "customer_id": h.OTHER_CUSTOMER_ID} if tool == "run_gaql" else {}
        payload = server.call(tool, args)
        error_payload = payload if tool == "run_gaql" else {"error": payload["credentials"]}
        clean_auth(error_payload, "AUTH_TOKEN_REVOKED")
        rows = [r for r in server.audit() if r["event"] == "auth_failure"]
        assert len(rows) == 1 and rows[0]["tool"] == tool
        assert rows[0]["customer_id"] == (h.OTHER_CUSTOMER_ID if tool == "run_gaql" else h.CUSTOMER_ID)


def test_auth_observation_failure_is_best_effort_but_mutation_audit_stays_closed(tmp_path):
    with InstalledServer(tmp_path) as server:
        plan = staged(server, "budget")
        audit_path = h.audit_file(tmp_path)
        audit_path.rename(tmp_path / "prior-audit.jsonl")
        audit_path.mkdir()
        server.mode(fault="revoked")
        read = server.call("run_gaql", {"query": "SELECT customer.id FROM customer"})
        clean_auth(read, "AUTH_TOKEN_REVOKED")
        # Unavailable observation cannot conceal the known safety-read cause.
        result = server.call("update_campaign", {"campaign_id": "111", "daily_budget": 50})
        clean_auth(result, "AUTH_TOKEN_REVOKED")
        server.mode()
        assert h.error_of(apply(server, plan))["code"] == "AUDIT_WRITE_FAILED"
        assert not server.events("mutation")


@pytest.mark.parametrize("tool,args,code", [
    ("update_campaign", {"campaign_id": "111", "daily_budget": 101}, "BUDGET_CAP_EXCEEDED"),
    ("update_keyword_bid", {"ad_group_id": "201", "criterion_id": "401", "current_bid": 1.2, "new_bid": 3}, "BID_CAP_EXCEEDED"),
])
def test_positive_control_caps_still_refuse_verified_increases(tmp_path, tool, args, code):
    with InstalledServer(tmp_path) as server:
        assert h.error_of(server.call(tool, args))["code"] == code
        assert not server.events("mutation")
