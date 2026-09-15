"""Numeric SDK account discovery must produce reusable public selectors.

The installed console and generated SDK search/pager run normally. Only the
credential-backed client construction and external RPC transport are synthetic.
These neutral ten-digit fixtures describe supported local identities, not the
account numbers a provider issues. No token-helper behavior is required here.
"""
import json
import queue
import re
import socket
import time

import pytest

import harness as h
import test_auth_cause_contract as stdio


FIRST = "0012345678"
SECOND = "0098765432"
ORDINARY = "2468013579"
MANAGER = "0001112223"
ACCOUNTS = {
    FIRST: {"name": "Example North", "currency_code": "EUR", "manager": False,
            "level": 1, "status": "ENABLED"},
    SECOND: {"name": "Example South", "currency_code": "CAD", "manager": False,
             "level": 2, "status": "CANCELED"},
    ORDINARY: {"name": "Example Group", "currency_code": "NZD", "manager": True,
               "level": 1, "status": "ENABLED"},
}

INJECTION = r'''
import atexit
import json
import os
from pathlib import Path
import re
import sys
import threading

marker = Path(os.environ["BOUNDARY_MARKER"])
control = Path(os.environ["BOUNDARY_CONTROL"])
lock = threading.Lock()
def mark(event, **fields):
    with lock, marker.open("a") as stream:
        stream.write(json.dumps({"event": event, **fields}) + "\n")
mark("loaded", integer_limit=sys.get_int_max_str_digits())
def deny(event, args):
    if event == "socket.bind" and args[1] == ("::1", 0):
        # Deny urllib3's import-time capability probe as well as external I/O.
        raise OSError("offline IPv6 probe")
    if event in ("socket.connect", "socket.bind", "socket.getaddrinfo",
                 "socket.gethostbyname", "socket.gethostbyaddr", "socket.sendto"):
        mark("network attempted", operation=event)
        raise OSError("offline account contract forbids network")
sys.addaudithook(deny)

sys.path.insert(0, os.environ["BOUNDARY_TESTS"])
from offline_contract import project, selected
from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.v25.services.services.google_ads_service import GoogleAdsServiceClient
from google.ads.googleads.v25.services.services.google_ads_service.transports.base import GoogleAdsServiceTransport
from google.ads.googleads.v25.services.types.google_ads_service import (
    GoogleAdsRow, SearchGoogleAdsRequest, SearchGoogleAdsResponse)
from google.auth.credentials import AnonymousCredentials
from google.protobuf.json_format import MessageToDict

accounts = json.loads(os.environ["ROUNDTRIP_ACCOUNTS"])
manager = os.environ["ROUNDTRIP_MANAGER"]

class SearchTransport(GoogleAdsServiceTransport):
    def __init__(self, owner):
        super().__init__(credentials=AnonymousCredentials())
        self.owner = owner
        self._wrapped_methods[self.search] = self.search

    def search(self, request, **kwargs):
        assert isinstance(request, SearchGoogleAdsRequest)
        mode = json.loads(control.read_text())
        resource = re.search(r"\bFROM\s+(\w+)", request.query, re.I).group(1)
        rows = []
        if resource == "customer_client" and not mode.get("empty"):
            # An accidental query of the configured child sees only itself;
            # a query of the manager can discover its siblings too.
            identities = list(accounts) if request.customer_id == manager else (
                [request.customer_id] if request.customer_id in accounts else [])
            if mode.get("ordinary_only"):
                identities = [cid for cid in identities if cid == "2468013579"]
            for cid in identities:
                info = accounts[cid]
                rows.append(GoogleAdsRow(customer_client={
                    "id": int(cid), "descriptive_name": info["name"],
                    "manager": info["manager"], "level": info["level"],
                    "status": info["status"],
                }))
        elif resource == "customer" and request.customer_id in accounts:
            info = accounts[request.customer_id]
            rows = [GoogleAdsRow(customer={
                "id": int(request.customer_id), "descriptive_name": info["name"],
                "currency_code": info["currency_code"], "time_zone": "Etc/UTC",
                "manager": info["manager"], "auto_tagging_enabled": True,
            })]
        fields = selected(request.query)
        limit = re.search(r"\bLIMIT\s+(\d+)", request.query, re.I)
        if limit:
            rows = rows[:int(limit.group(1))]
        response = SearchGoogleAdsResponse()
        response.results.extend(project(row, fields) for row in rows)
        numeric_ids = []
        if resource == "customer_client":
            for row in response.results:
                assert isinstance(row, GoogleAdsRow)
                assert type(row.customer_client.id) is int
                numeric_ids.append(row.customer_client.id)
        mark("search", request=MessageToDict(request._pb, preserving_proto_field_name=True),
             login_customer_id=self.owner.login_customer_id,
             fields=fields, numeric_ids=numeric_ids)
        return response

class UnusedService:
    def __getattr__(self, name):
        def refused(*args, **kwargs):
            mark("unexpected RPC", method=name)
            raise AssertionError("account reads and previews must not execute another RPC")
        return refused

def factory(config, *args, **kwargs):
    mark("client factory", login_customer_id=config.get("login_customer_id"))
    client = GoogleAdsClient(credentials=AnonymousCredentials(),
        developer_token=config["developer_token"],
        login_customer_id=config.get("login_customer_id"),
        use_proto_plus=True, version="v25")
    service = GoogleAdsServiceClient(transport=SearchTransport(client))
    client.get_service = lambda name, **kw: service if name == "GoogleAdsService" else UnusedService()
    return client
GoogleAdsClient.load_from_dict = factory

def finished():
    import ads_mcp.server
    mark("finished", module_file=ads_mcp.server.__file__,
         integer_limit=sys.get_int_max_str_digits())
atexit.register(finished)
'''


@pytest.fixture(autouse=True)
def offline_parent(monkeypatch, tmp_path):
    def deny(*args, **kwargs):
        raise AssertionError("account round-trip parent forbids network")
    for name in ("connect", "connect_ex", "bind", "sendto"):
        monkeypatch.setattr(socket.socket, name, deny)
    for name in ("getaddrinfo", "gethostbyname", "gethostbyname_ex", "gethostbyaddr"):
        monkeypatch.setattr(socket, name, deny)
    monkeypatch.setattr(stdio, "INJECTION", INJECTION)
    monkeypatch.setattr(h, "scrubbed_env", lambda overlay=None: {
        "PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "TMPDIR": str(tmp_path),
        "PYTHONNOUSERSITE": "1", **(overlay or {}),
    })


class AccountServer(stdio.InstalledServer):
    def __init__(self, root, *, customer=FIRST, login=MANAGER, writable=False):
        try:
            super().__init__(root, customer=customer, env={
                "GOOGLE_ADS_LOGIN_CUSTOMER_ID": login,
                "ADS_MCP_READ_ONLY": "false" if writable else "true",
                "ADS_MCP_REQUIRE_DRY_RUN": "true",
                "ROUNDTRIP_ACCOUNTS": json.dumps(ACCOUNTS),
                "ROUNDTRIP_MANAGER": MANAGER,
            })
        except BaseException:
            self.cleanup()
            raise

    def cleanup(self):
        process = getattr(self, "process", None)
        if process is None:
            return
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        for reader in getattr(self, "readers", []):
            reader.join(timeout=3)
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream and not stream.closed:
                stream.close()

    def receive(self, identity):
        deadline = time.monotonic() + 20
        while True:
            try:
                item = self.inbox.get(timeout=max(0, deadline - time.monotonic()))
            except queue.Empty:
                raise AssertionError("installed account MCP response deadline exceeded") from None
            if "id" in item:
                assert item["id"] == identity, item
                return item

    def __exit__(self, *args):
        try:
            super().__exit__(*args)
            assert not self.events("unexpected RPC"), "read or preview attempted another RPC"
        finally:
            self.cleanup()


def account_info(result, identity):
    info = ACCOUNTS[identity]
    assert result == {
        "customer_id": identity, "name": info["name"],
        "currency_code": info["currency_code"], "time_zone": "Etc/UTC",
        "manager": info["manager"], "auto_tagging_enabled": True, "test_account": False,
    }


def discovered_metadata(row, identity):
    info = ACCOUNTS[identity]
    assert {k: v for k, v in row.items() if k != "customer_id"} == {
        k: info[k] for k in ("name", "manager", "level", "status")
    }


def test_two_numeric_leading_zero_children_round_trip_unchanged(tmp_path):
    with AccountServer(tmp_path, customer="٠٠١٢٣٤٥٦٧٨", login="٠٠٠١١١٢٢٢٣") as server:
        listed = server.call("list_accounts")
        assert listed["customer_id"] == MANAGER
        by_name = {row["name"]: row for row in listed["accounts"]}
        assert len(listed["accounts"]) == len(by_name) == len(ACCOUNTS)
        discovery = server.events("search")
        assert len(discovery) == 1
        assert discovery[0]["request"]["customer_id"] == MANAGER
        assert discovery[0]["login_customer_id"] == MANAGER
        assert discovery[0]["numeric_ids"] == [int(cid) for cid in ACCOUNTS]
        assert "customer_client.id" in discovery[0]["fields"]
        # Drive every returned selector before asserting width. A formatting
        # failure must retain evidence of both failed consumer calls.
        roundtrips = []
        for identity, info in ACCOUNTS.items():
            row = by_name[info["name"]]
            discovered_metadata(row, identity)
            before = len(server.events("search"))
            result = server.call("get_account_info", {"customer_id": row["customer_id"]})
            roundtrips.append((identity, row["customer_id"], result,
                               server.events("search")[before:]))
        problems = []
        for identity, returned, result, requests in roundtrips:
            if returned != identity or not re.fullmatch(r"[0-9]{10}", returned):
                problems.append(f"{identity} discovered as {returned!r}")
            if "error" in result:
                problems.append(f"unchanged {returned!r} refused: {result['error']['code']}")
            else:
                account_info(result, identity)
                assert len(requests) == 1
                assert requests[0]["request"]["customer_id"] == identity
                assert requests[0]["login_customer_id"] == MANAGER
        assert not problems, "; ".join(problems)


def test_explicit_supported_selectors_keep_distinct_account_metadata(tmp_path):
    with AccountServer(tmp_path, customer=" 001-234-5678 ", login=" 000-111-2223 ") as server:
        for args, identity in [({}, FIRST), ({"customer_id": " 009-876-5432 "}, SECOND),
                               ({"customer_id": "００１２３４５６７８"}, FIRST)]:
            account_info(server.call("get_account_info", args), identity)
        requests = server.events("search")
        assert [r["request"]["customer_id"] for r in requests] == [FIRST, SECOND, FIRST]
        assert all(r["login_customer_id"] == MANAGER for r in requests)


@pytest.mark.parametrize("login", [MANAGER, ""], ids=["manager-root", "customer-root"])
def test_ordinary_ten_digit_discovery_and_empty_results(tmp_path, login):
    with AccountServer(tmp_path, customer=ORDINARY, login=login) as server:
        server.mode(ordinary_only=True)
        result = server.call("list_accounts")
        root = login or ORDINARY
        assert result["customer_id"] == root and len(result["accounts"]) == 1
        row = result["accounts"][0]
        assert row["customer_id"] == ORDINARY
        discovered_metadata(row, ORDINARY)
        account_info(server.call("get_account_info", {"customer_id": row["customer_id"]}), ORDINARY)
        server.mode(empty=True)
        assert server.call("list_accounts") == {"customer_id": root, "accounts": []}
        requests = server.events("search")
        assert [r["request"]["customer_id"] for r in requests] == [root, ORDINARY, root]
        assert all(r["login_customer_id"] == (login or None) for r in requests)


def test_short_discovery_spellings_and_malformed_user_ids_still_refuse_locally(tmp_path):
    with AccountServer(tmp_path) as server:
        for value in ("12345678", "98765432", "001234567²", ""):
            result = server.call("get_account_info", {"customer_id": value})
            error = h.error_of(result)
            assert error["code"] == "INVALID_CUSTOMER_ID"
            assert "10 digits" in error["message"] and "123-456-7890" in error["message"]
        assert not server.events("search") and not server.events("client factory")


def test_same_account_preview_and_foreign_account_refusal_stay_isolated(tmp_path):
    with AccountServer(tmp_path, writable=True) as server:
        args = {"entity_type": "campaign", "entity_id": "111"}
        foreign = server.call("pause_entity", {**args, "customer_id": SECOND})
        assert h.error_of(foreign)["code"] == "PLAN_CUSTOMER_MISMATCH"
        plan = server.call("pause_entity", {**args, "customer_id": "００１２３４５６７８"})["plan"]
        assert plan["operations"][0]["resource"] == f"customers/{FIRST}/campaigns/111"
        preview = server.call("confirm_and_apply", {"plan_id": plan["id"], "customer_id": "001-234-5678"})
        assert preview == {"applied": False, "plan": plan}
        refusal = server.call("confirm_and_apply", {
            "plan_id": plan["id"], "customer_id": SECOND, "dry_run": False,
        })
        assert h.error_of(refusal)["code"] == "PLAN_CUSTOMER_MISMATCH"
        assert not server.events("search") and not server.events("unexpected RPC")
