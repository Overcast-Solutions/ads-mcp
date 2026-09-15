"""F063: installed stdio inspection and cancellation under retained-read load.

The synthetic provider alone holds snapshot construction. A one-shot cohort
barrier at MCP call admission lets all 45 requests enter before worker pressure
can starve stdin itself. It changes no worker limits, locks or tool behavior;
every admitted call then delegates to the original framework dispatch. Arrival
receipts, not sleeps or successful pipe writes, delimit the health measurement.
"""
from collections import Counter
from datetime import timedelta
import hashlib
import json
import socket
import time

import pytest

import harness as h
import test_auth_cause_contract as plumbing
from test_confirmation_queue_contract import QueueServer


QUERY = "SELECT campaign.id, campaign.name FROM campaign"
INJECTION = plumbing.INJECTION + r'''
import anyio
import hashlib
import time
from collections import Counter
from mcp.server.mcpserver import MCPServer
from offline_contract import project, selected
from test_stable_read_continuation_contract import row_for

original_call = MCPServer.call_tool
cohort_count = 0
cohort_gate = None
async def observe_call(self, name, arguments, *args, **kwargs):
    global cohort_count, cohort_gate
    retained = name in ("run_gaql", "get_campaign_performance", "get_ad_performance")
    if retained and (control.parent / "cohort").exists() and cohort_count < 45:
        if cohort_gate is None:
            cohort_gate = anyio.Event()
        cohort_count += 1
        member = cohort_count
        if cohort_count == 45:
            cohort_gate.set()
        await cohort_gate.wait()
        mark("cohort dispatch", member=member)
    mark("dispatch", tool=name, arguments=arguments)
    try:
        return await original_call(self, name, arguments, *args, **kwargs)
    finally:
        mark("dispatch finished", tool=name, arguments=arguments)
MCPServer.call_tool = observe_call

class ReadTransport(h.FakeGoogleAdsClient):
    def __init__(self):
        super().__init__()
        self.generations = Counter()
    def _do_search(self, service, method, args, kwargs):
        cid = str(h._req_field(args, kwargs, "customer_id"))
        query = str(h._req_field(args, kwargs, "query"))
        resource = h._FROM_RE.search(query).group(1)
        mark("search", customer_id=cid, query=query, resource=resource)
        if resource == "customer":
            return [project(row_for(resource, cid, 1), selected(query))]
        self.generations[(cid, query)] += 1
        generation = self.generations[(cid, query)]
        scope = hashlib.sha256(query.encode()).hexdigest()[:10]
        def rows():
            mark("build entered", customer_id=cid, query=query)
            try:
                if "HOLD_SYNTHETIC" in query:
                    mark("held")
                    deadline = time.monotonic() + 40
                    while not (control.parent / "release").exists():
                        if time.monotonic() >= deadline:
                            mark("hold watchdog")
                            raise AssertionError("synthetic provider was not released")
                        time.sleep(0.01)
                    mark("released")
                # A repeated provider query changes its order AND generation.
                # A stable continuation must use the original retained rows.
                for offset in range(3):
                    index = 1 + (offset + generation - 1) % 3
                    row = row_for(resource, cid, index)
                    row.campaign.name = f"{cid}:{scope}:{generation}:{index}"
                    yield project(row, selected(query))
            finally:
                mark("build finished", customer_id=cid, query=query,
                     label_prefix=f"{cid}:{scope}:{generation}:")
        return rows()
    def _do_mutation(self, *args, **kwargs):
        mark("forbidden mutation")
        raise AssertionError("read queue oracle must never mutate")

transport = ReadTransport()
# Existing server clock injection controls absolute token expiry without sleep.
import ads_mcp.server as assembly
original_server = assembly.create_server
def create_server(config, **kwargs):
    def clock():
        return h.FIXED_NOW.timestamp() + json.loads(control.read_text()).get("advance", 0)
    return original_server(config, **{**kwargs, "clock": clock})
assembly.create_server = create_server
'''


@pytest.fixture(autouse=True)
def offline(monkeypatch, tmp_path):
    def deny(*args, **kwargs):
        raise AssertionError("synthetic read queue contract forbids networking")
    for name in ("connect", "connect_ex", "bind", "sendto"):
        monkeypatch.setattr(socket.socket, name, deny)
    monkeypatch.setattr(socket, "getaddrinfo", deny)
    monkeypatch.setattr(plumbing, "INJECTION", INJECTION)
    monkeypatch.setattr(h, "scrubbed_env", lambda overlay=None: {
        "PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "TMPDIR": str(tmp_path),
        "PYTHONNOUSERSITE": "1", **(overlay or {})})


class ReadServer(QueueServer):
    def __init__(self, root):
        super().__init__(root, env={"ADS_MCP_READ_ONLY": "true", "ADS_MCP_ROW_LIMIT": "2"})

    def wait_event(self, event, count=1, seconds=8):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline and self.process.poll() is None:
            if len(self.events(event)) >= count:
                return True
            time.sleep(0.01)
        return False

    def cancel(self, identity):
        self.process.stdin.write(json.dumps({"jsonrpc": "2.0", "method": "notifications/cancelled",
            "params": {"requestId": identity, "reason": "synthetic queued cancellation"}}) + "\n")
        self.process.stdin.flush()


def names(result, tool):
    if tool == "run_gaql":
        return [row["campaign"]["name"] for row in result["rows"]]
    key = "campaigns" if tool == "get_campaign_performance" else "ads"
    field = "name" if tool == "get_campaign_performance" else "campaign_name"
    return [row[field] for row in result[key]]


def calls(mixed):
    for i in range(45):
        cid = h.OTHER_CUSTOMER_ID if mixed and i % 2 else h.CUSTOMER_ID
        if not mixed or i % 3 == 0:
            yield "run_gaql", {"customer_id": cid, "query": QUERY}
        else:
            tool = "get_campaign_performance" if i % 3 == 1 else "get_ad_performance"
            yield tool, {"customer_id": cid, "last_n_days": 7 + i, "campaign_id": None}


@pytest.mark.parametrize("mixed", [False, True], ids=["repeated-gaql", "mixed-tools-accounts"])
def test_health_completes_after_45_reads_reach_dispatch_while_provider_is_held(tmp_path, mixed):
    with ReadServer(tmp_path) as server:
        assert server.call("health_check")["status"] == "OK"
        held = server.tool("run_gaql", query=QUERY + " WHERE campaign.name != 'HOLD_SYNTHETIC'")
        pending = [held]
        try:
            assert server.wait_held()
            assert server.call("health_check")["status"] == "OK"
            (server.root / "cohort").touch()
            requests = list(calls(mixed))
            queued = [server.tool(tool, **args) for tool, args in requests]
            pending.extend(queued)
            assert server.wait_event("cohort dispatch", 45), "45 calls did not reach real framework dispatch"
            assert len(server.events("build entered")) == 1, "waiting reads bypassed serialized snapshot ownership"
            before_health = sum(row["resource"] == "customer" for row in server.events("search"))
            health = server.tool("health_check")
            pending.append(health)
            available = server.collect([health], 3)
            fresh_during_hold = sum(row["resource"] == "customer" for row in server.events("search")) > before_health
            assert not server.events("released")
        finally:
            (server.root / "release").touch()
            assert server.collect(pending, 15), "reads did not drain after provider release"
        assert server.payload(server.answers[health])["status"] == "OK"
        for identity, (tool, args) in zip(queued, requests):
            result = h.expect_ok(server.payload(server.answers[identity]))
            assert result["customer_id"] == args["customer_id"]
            labels = names(result, tool)
            assert len(labels) == 2 and all(label.startswith(args["customer_id"] + ":") for label in labels)
            assert result.get("next_page_token")
            if tool == "run_gaql":
                expected_query = QUERY
            else:
                end = h.FIXED_NOW.date() - timedelta(days=1)
                start = end - timedelta(days=args["last_n_days"] - 1)
                assert result["window"] == {"start": start.isoformat(), "end": end.isoformat()}
                resource = "campaign" if tool == "get_campaign_performance" else "ad_group_ad"
                searches = [row for row in server.events("search")
                            if row["customer_id"] == args["customer_id"] and row["resource"] == resource
                            and start.isoformat() in row["query"] and end.isoformat() in row["query"]]
                assert len(searches) == 1, "queued report lost or duplicated its requested date/account scope"
                expected_query = searches[0]["query"]
            scope = hashlib.sha256(expected_query.encode()).hexdigest()[:10]
            assert all(label.split(":")[1] == scope for label in labels), "rows came from a different query"
        expected = Counter((args["customer_id"], args["query"]) for tool, args in requests if tool == "run_gaql")
        actual = Counter((row["customer_id"], row["query"]) for row in server.events("search") if row["query"] == QUERY)
        assert actual == expected, "queued account/query calls were dropped, duplicated or reused"
        active = 0
        for row in server.observations():
            if row["event"] == "build entered":
                active += 1
                assert active == 1, "provider snapshot construction overlapped"
            elif row["event"] == "build finished":
                active -= 1
        assert active == 0 and not server.events("forbidden mutation")
        # Do not demand that 45 snapshots survive the existing 16-snapshot cap.
        # Six latest provider builds fit within that bound. Observation of the
        # provider generation disambiguates repeated identical RPC arguments.
        prefixes = [row["label_prefix"] for row in server.events("build finished")[-6:]]
        matches = [(identity, tool, args) for identity, (tool, args) in zip(queued, requests)
                   if any(names(server.payload(server.answers[identity]), tool)[0].startswith(prefix)
                          for prefix in prefixes)]
        assert len(matches) == 6
        for identity, tool, args in matches:
            head = server.payload(server.answers[identity])
            before = len(server.events("search"))
            tail = h.expect_ok(server.call(tool, {**args, "page_token": head["next_page_token"]}))
            assert len(server.events("search")) == before
            labels = names(head, tool) + names(tail, tool)
            assert len(labels) == len(set(labels)) == 3
            assert len({label.rsplit(":", 1)[0] for label in labels}) == 1
            assert not tail.get("next_page_token")
        assert available and fresh_during_hold, "fresh authenticated health stalled behind 45 arrived retained reads"


def test_cancelled_queued_read_never_dispatches_provider_and_queue_recovers(tmp_path):
    with ReadServer(tmp_path) as server:
        held = server.tool("run_gaql", query=QUERY + " WHERE campaign.name != 'HOLD_SYNTHETIC'")
        pending = [held]
        cancelled_query = QUERY + " WHERE campaign.name != 'CANCELLED_SYNTHETIC'"
        try:
            assert server.wait_held()
            cancelled = server.tool("run_gaql", query=cancelled_query, customer_id=h.OTHER_CUSTOMER_ID)
            assert server.wait_event("dispatch", 2), "cancelled call never reached framework dispatch"
            assert any(row["arguments"].get("query") == cancelled_query for row in server.events("dispatch"))
            assert not any(row["query"] == cancelled_query for row in server.events("search"))
            server.cancel(cancelled)
            # Dispatcher processes cancellation notifications in receive order.
            ping = server.send("ping", {})
            assert server.collect([ping], 5), "cancellation notification was not consumed before release"
            survivor = server.tool("run_gaql", query=QUERY, customer_id=h.CUSTOMER_ID)
            pending.append(survivor)
            assert server.wait_event("dispatch", 3)
            assert not server.events("released")
        finally:
            (server.root / "release").touch()
            assert server.collect(pending, 15), "cancellation left the read queue stuck"
        assert server.payload(server.answers[survivor])["customer_id"] == h.CUSTOMER_ID
        assert server.call("health_check")["status"] == "OK"
        later = server.call("run_gaql", {"query": QUERY, "customer_id": h.OTHER_CUSTOMER_ID})
        assert later["customer_id"] == h.OTHER_CUSTOMER_ID
        assert not any(row["query"] == cancelled_query for row in server.events("search")), (
            "a cancelled waiter performed provider work after the held read was released")


def test_two_account_continuations_stay_scoped_and_expire_before_provider_work(tmp_path):
    with ReadServer(tmp_path) as server:
        heads = {cid: server.call("run_gaql", {"query": QUERY, "customer_id": cid})
                 for cid in (h.CUSTOMER_ID, h.OTHER_CUSTOMER_ID)}
        token = heads[h.CUSTOMER_ID]["next_page_token"]
        for args in [
            {"query": QUERY, "page_token": "malformed"},
            {"query": QUERY, "page_token": token, "customer_id": h.OTHER_CUSTOMER_ID},
            {"query": QUERY + " WHERE campaign.id > 0", "page_token": token},
        ]:
            before = len(server.events("search"))
            assert h.error_of(server.call("run_gaql", args))["code"] == "INVALID_PAGE_TOKEN"
            assert len(server.events("search")) == before
        for cid, head in heads.items():
            before = len(server.events("search"))
            tail = server.call("run_gaql", {"query": QUERY, "customer_id": cid, "page_token": head["next_page_token"]})
            assert len(server.events("search")) == before
            labels = names(head, "run_gaql") + names(tail, "run_gaql")
            assert len(set(labels)) == 3 and all(label.startswith(cid + ":") for label in labels)
        server.mode(advance=300)
        for cid, head in heads.items():
            before = len(server.events("search"))
            result = server.call("run_gaql", {"query": QUERY, "customer_id": cid, "page_token": head["next_page_token"]})
            assert h.error_of(result)["code"] == "INVALID_PAGE_TOKEN"
            assert len(server.events("search")) == before
        assert server.call("health_check")["status"] == "OK"
