"""F051: keyword classification and canonical account identity on installed MCP.

The generated SDK search method constructs the real request and pager. Only its
RPC transport is synthetic; predicates are evaluated on full genuine rows before
SELECT projection. The two accounts are keyed by exact ASCII identities, so a
malformed dispatch cannot accidentally obtain another account's fixture data.
"""
import json
from pathlib import Path
import queue
import socket
import subprocess

import pytest

import harness as h
import test_auth_cause_contract as plumbing
from ads_mcp.config import normalize_customer_id


ACCOUNT = "0012345678"
OTHER = "0098765432"
LOGIN = "0001112223"
DECIMAL_ACCOUNT = "٠٠١٢٣٤٥٦٧٨"
DECIMAL_OTHER = "００９８７６５４３２"
DECIMAL_LOGIN = "٠٠٠١١١٢٢٢٣"

INJECTION = r'''
import atexit
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import threading

def deny(event, args):
    if event in ("socket.connect", "socket.bind", "socket.getaddrinfo", "socket.sendto"):
        raise OSError("offline identity contract forbids network")
sys.addaudithook(deny)
sys.path.insert(0, os.environ["BOUNDARY_TESTS"])
import harness as h
from offline_contract import project, selected
from google.ads.googleads.client import GoogleAdsClient
from google.ads.googleads.v25.services.services.google_ads_service import GoogleAdsServiceClient
from google.ads.googleads.v25.services.services.google_ads_service.transports.base import GoogleAdsServiceTransport
from google.auth.credentials import AnonymousCredentials
from google.api_core.exceptions import ServiceUnavailable
from google.protobuf.json_format import MessageToDict

marker = Path(os.environ["BOUNDARY_MARKER"])
control = Path(os.environ["BOUNDARY_CONTROL"])
lock = threading.Lock()
def mark(event, **fields):
    with lock, marker.open("a") as stream:
        stream.write(json.dumps({"event": event, **fields}) + "\n")
mark("loaded", integer_limit=sys.get_int_max_str_digits())

def criterion(identity, negative, kind, **fields):
    return h.make_row({"campaign": {"id": 111}, "campaign_criterion": {
        "criterion_id": identity, "negative": negative, "type_": kind, **fields}})

accounts = {
    "0012345678": {"name": "Synthetic first account", "currency": "EUR", "criteria": [
        criterion(701, True, "KEYWORD", keyword={"text": "Synthetic Exact Term", "match_type": "EXACT"}),
        criterion(702, True, "LOCATION", location={"geo_target_constant": "geoTargetConstants/2840"}),
        criterion(703, True, "IP_BLOCK", ip_block={"ip_address": "192.0.2.10"}),
        criterion(704, False, "KEYWORD", keyword={"text": "positive term", "match_type": "BROAD"})]},
    "0098765432": {"name": "Synthetic second account", "currency": "CAD", "criteria": [
        criterion(801, True, "KEYWORD", keyword={"text": "Other Phrase Term", "match_type": "PHRASE"}),
        criterion(802, False, "KEYWORD", keyword={"text": "other positive term", "match_type": "EXACT"})]},
}

def filter_criteria(rows, query):
    # The bounded fixture's GAQL predicates use SQL-compatible scalar operators.
    # SQLite evaluates AND/OR/equality/IN/NOT on FULL criterion data, independently
    # of product classification and before unselected oneof fields disappear.
    where = re.search(r"\bWHERE\s+(.*?)(?:\bORDER\s+BY\b|\bLIMIT\b|$)", query, re.I | re.S)
    if not where:
        return rows
    names = {
        "campaign_criterion.criterion_id": "criterion_id",
        "campaign_criterion.negative": "negative",
        "campaign_criterion.type": "kind",
        "campaign_criterion.keyword.text": "keyword_text",
        "campaign_criterion.keyword.match_type": "match_type",
        "campaign.id": "campaign_id",
    }
    expression = where.group(1)
    for public, column in names.items():
        expression = re.sub(r"\b" + re.escape(public) + r"\b", column, expression, flags=re.I)
    with sqlite3.connect(":memory:") as db:
        db.execute("CREATE TABLE criteria (position, criterion_id, negative, kind, keyword_text, match_type, campaign_id)")
        db.executemany("INSERT INTO criteria VALUES (?, ?, ?, ?, ?, ?, ?)", [
            (i, row.campaign_criterion.criterion_id, row.campaign_criterion.negative,
             row.campaign_criterion.type_.name, row.campaign_criterion.keyword.text,
             row.campaign_criterion.keyword.match_type.name, row.campaign.id)
            for i, row in enumerate(rows)])
        indices = [item[0] for item in db.execute("SELECT position FROM criteria WHERE " + expression)]
    return [rows[i] for i in indices]

class SearchTransport(GoogleAdsServiceTransport):
    def __init__(self, owner):
        super().__init__(credentials=AnonymousCredentials())
        self.owner = owner
        self.used_faults = set()
        self._wrapped_methods[self.search] = self.search

    def search(self, request, **kwargs):
        assert type(request).__name__ == "SearchGoogleAdsRequest"
        mode = json.loads(control.read_text())
        mark("search", request=MessageToDict(request._pb, preserving_proto_field_name=True),
             login_customer_id=self.owner.login_customer_id)
        if mode.get("retry") and mode["retry"] not in self.used_faults:
            self.used_faults.add(mode["retry"])
            raise ServiceUnavailable("synthetic temporary read interruption")
        resource = h._FROM_RE.search(request.query).group(1)
        account = accounts.get(request.customer_id)
        rows = []
        if resource == "customer" and account:
            rows = [h.make_row({"customer": {"id": int(request.customer_id),
                "descriptive_name": account["name"], "currency_code": account["currency"],
                "time_zone": "Etc/UTC"}})]
        elif resource == "campaign_criterion" and account and not mode.get("empty"):
            rows = filter_criteria(account["criteria"], request.query)
        elif resource == "customer_client" and request.customer_id == "0001112223":
            rows = [h.make_row({"customer_client": {"id": int(cid),
                "descriptive_name": info["name"], "manager": False, "level": 1,
                "status": "ENABLED"}}) for cid, info in accounts.items()]
        limit = re.search(r"\bLIMIT\s+(\d+)", request.query, re.I)
        if limit:
            rows = rows[:int(limit.group(1))]
        response = h.get_ads_type("SearchGoogleAdsResponse")
        response.results.extend(project(row, selected(request.query)) for row in rows)
        return response

class Client(h.FakeGoogleAdsClient):
    def __init__(self):
        super().__init__()
        self._services["GoogleAdsService"] = GoogleAdsServiceClient(transport=SearchTransport(self))

    def _do_mutation(self, service, method, args, kwargs):
        request = kwargs.get("request") or args[0]
        assert hasattr(request, "_pb")
        mark("mutation", request_type=type(request).__name__,
             request=MessageToDict(request._pb, preserving_proto_field_name=True))
        return super()._do_mutation(service, method, args, kwargs)

def factory(config, *args, **kwargs):
    mark("client factory", login_customer_id=config.get("login_customer_id"))
    client = Client()
    client.login_customer_id = config.get("login_customer_id")
    return client
GoogleAdsClient.load_from_dict = factory

def finished():
    import ads_mcp.server
    mark("finished", module_file=ads_mcp.server.__file__, integer_limit=sys.get_int_max_str_digits())
atexit.register(finished)
'''


@pytest.fixture(autouse=True)
def offline_parent(monkeypatch, tmp_path):
    def deny(*args, **kwargs):
        raise AssertionError("F051 parent forbids network")
    for name in ("connect", "connect_ex", "bind", "sendto"):
        monkeypatch.setattr(socket.socket, name, deny)
    monkeypatch.setattr(socket, "getaddrinfo", deny)
    monkeypatch.setattr(plumbing, "INJECTION", INJECTION)
    monkeypatch.setattr(h, "scrubbed_env", lambda overlay=None: {
        "PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "TMPDIR": str(tmp_path),
        "PYTHONNOUSERSITE": "1", **(overlay or {})})


class IdentityServer(plumbing.InstalledServer):
    def __init__(self, root, *, customer=ACCOUNT, login=LOGIN):
        try:
            super().__init__(root, customer=customer, env={"GOOGLE_ADS_LOGIN_CUSTOMER_ID": login})
        except BaseException:
            if hasattr(self, "process") and self.process.poll() is None:
                self.process.kill()
                self.process.wait(timeout=5)
                for reader in self.readers:
                    reader.join(timeout=3)
            raise

    def receive(self, identity):
        try:
            return super().receive(identity)
        except queue.Empty:
            raise AssertionError("installed identity MCP response deadline exceeded") from None

    def audit(self):
        path = h.audit_file(self.root)
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def error_guidance(error, value):
    assert error["code"] == "INVALID_CUSTOMER_ID"
    message = error["message"]
    assert repr(value.strip()) in message
    assert "10 digits" in message and "123-456-7890" in message
    assert "Traceback" not in message


@pytest.mark.parametrize("raw", [ACCOUNT, " 001-234-5678 ", DECIMAL_ACCOUNT, " ００١-٢٣٤-５６７８ "],
                         ids=["ascii", "dashed", "arabic-decimal", "mixed-decimal"])
def test_ten_decimal_customer_digits_have_one_ascii_identity(raw):
    assert normalize_customer_id(raw) == ACCOUNT


@pytest.mark.parametrize("field", ["GOOGLE_ADS_CUSTOMER_ID", "GOOGLE_ADS_LOGIN_CUSTOMER_ID"])
def test_config_fields_reject_nondecimal_digit_like_values_before_client_use(tmp_path, field):
    env = h.google_ads_env(tmp_path)
    env[field] = "²" * 10
    injection = tmp_path / "injection"
    injection.mkdir()
    (injection / "sitecustomize.py").write_text(INJECTION)
    marker = tmp_path / "events.jsonl"
    control = tmp_path / "control.json"
    control.write_text("{}")
    env.update({"PYTHONPATH": str(injection), "PYTHONDONTWRITEBYTECODE": "1",
        "BOUNDARY_TESTS": str(Path(__file__).parent), "BOUNDARY_MARKER": str(marker),
        "BOUNDARY_CONTROL": str(control)})
    # EOF gives an erroneously accepted configuration a clean exit too. The
    # distinction is the actual console's nonzero, quoted configuration refusal,
    # not a hang waiting for MCP initialization from a deliberately invalid boot.
    try:
        result = subprocess.run([str(h.console_script())], cwd=tmp_path,
            env=h.scrubbed_env(env), input="", capture_output=True, text=True, timeout=15)
    except subprocess.TimeoutExpired:
        raise AssertionError("installed console did not finish invalid configuration; child killed and drained") from None
    rows = [json.loads(line) for line in marker.read_text().splitlines()]
    assert rows[-1]["event"] == "finished"
    assert not any(r["event"] in ("client factory", "search", "mutation") for r in rows)
    assert "Traceback" not in result.stdout + result.stderr
    assert result.returncode != 0, f"installed console accepted nondecimal digits in {field}"
    assert "configuration error" in result.stderr
    assert repr("²" * 10) in result.stderr and "10 digits" in result.stderr
    assert "123-456-7890" in result.stderr


def test_negative_report_excludes_genuine_location_ip_and_positive_keyword(tmp_path):
    with IdentityServer(tmp_path) as server:
        result = server.call("get_negative_keywords")
        assert not server.events("mutation")
        assert server.events("search")[-1]["request"]["customer_id"] == ACCOUNT
        assert result == {"customer_id": ACCOUNT, "negative_keywords": [{
            "campaign_id": "111", "criterion_id": "701", "keyword": "Synthetic Exact Term", "match_type": "EXACT"}]}


def test_requested_account_and_empty_negative_keyword_controls(tmp_path):
    with IdentityServer(tmp_path) as server:
        args = {"customer_id": " 009-876-5432 "}
        result = server.call("get_negative_keywords", args)
        assert result == {"customer_id": OTHER, "negative_keywords": [{
            "campaign_id": "111", "criterion_id": "801", "keyword": "Other Phrase Term", "match_type": "PHRASE"}]}
        assert server.events("search")[-1]["request"]["customer_id"] == OTHER
        server.mode(empty=True)
        assert server.call("get_negative_keywords", args) == {"customer_id": OTHER, "negative_keywords": []}
        assert not server.events("mutation")
        assert all(r["login_customer_id"] == LOGIN for r in server.events("search"))


@pytest.mark.parametrize("customer,login", [(" 001-234-5678 ", " 000-111-2223 "),
                                           (DECIMAL_ACCOUNT, DECIMAL_LOGIN)],
                         ids=["ascii-config-control", "decimal-config"])
def test_configured_identity_reaches_health_login_and_default_reads(tmp_path, customer, login):
    with IdentityServer(tmp_path, customer=customer, login=login) as server:
        health = server.call("health_check")
        info = server.call("get_account_info")
        accounts = server.call("list_accounts")
        events = server.events("search")
        assert health["config"]["customer_id"] == ACCOUNT
        assert health["config"]["login_customer_id"] == LOGIN
        assert health["status"] == "OK"
        assert info["customer_id"] == ACCOUNT and info["name"] == "Synthetic first account"
        assert accounts["customer_id"] == LOGIN and len(accounts["accounts"]) == 2
        assert [r["request"]["customer_id"] for r in events] == [ACCOUNT, ACCOUNT, LOGIN]
        assert all(r["login_customer_id"] == LOGIN for r in events)
        assert server.events("client factory")[0]["login_customer_id"] == LOGIN
        assert not server.events("mutation")


@pytest.mark.parametrize("value", ["²" * 10, "abcdefghij", "123456789", "12345678901"],
                         ids=["superscript", "letters", "nine-digits", "eleven-digits"])
def test_invalid_read_override_refuses_locally_with_existing_quoted_guidance(tmp_path, value):
    with IdentityServer(tmp_path) as server:
        result = server.call("get_account_info", {"customer_id": value})
        # Empty rows for unknown wire identities do not invent account access.
        assert not server.events("search"), "malformed customer reached genuine SDK search request"
        assert not server.events("client factory") and not server.events("mutation")
        error_guidance(h.error_of(result), value)


@pytest.mark.parametrize("override", [" 009-876-5432 ", DECIMAL_OTHER], ids=["ascii-control", "decimal-override"])
def test_read_override_returned_identity_and_retry_audit_agree(tmp_path, override):
    with IdentityServer(tmp_path) as server:
        server.mode(retry="one synthetic interruption")
        result = server.call("get_account_info", {"customer_id": override})
        requests = server.events("search")
        assert [r["request"]["customer_id"] for r in requests] == [OTHER, OTHER]
        assert all(r["login_customer_id"] == LOGIN for r in requests)
        assert result["customer_id"] == OTHER and result["name"] == "Synthetic second account"
        assert result["currency_code"] == "CAD"
        retries = [r for r in server.audit() if r["event"] == "retry"]
        assert len(retries) == 1 and retries[0]["customer_id"] == OTHER
        assert retries[0]["tool"] == "get_account_info" and not retries[0].get("plan_id")
        default = server.call("get_account_info")
        assert default["customer_id"] == ACCOUNT and default["currency_code"] == "EUR"
        assert not server.events("mutation")


@pytest.mark.parametrize("same,foreign", [(ACCOUNT, OTHER), (DECIMAL_ACCOUNT, DECIMAL_OTHER)],
                         ids=["ascii-control", "decimal-comparison"])
def test_same_account_mutation_and_foreign_account_refusal_keep_canonical_audit(tmp_path, same, foreign):
    with IdentityServer(tmp_path) as server:
        args = {"entity_type": "campaign", "entity_id": "111"}
        refused = server.call("pause_entity", {**args, "customer_id": foreign})
        assert h.error_of(refused)["code"] == "PLAN_CUSTOMER_MISMATCH"
        assert not server.events("search") and not server.events("mutation")
        plan = h.expect_ok(server.call("pause_entity", {**args, "customer_id": same}))["plan"]
        assert plan["operations"][0]["resource"] == f"customers/{ACCOUNT}/campaigns/111"
        preview = server.call("confirm_and_apply", {"plan_id": plan["id"], "customer_id": same})
        assert preview == {"applied": False, "plan": plan}
        foreign_apply = server.call("confirm_and_apply", {"plan_id": plan["id"], "customer_id": foreign, "dry_run": False})
        assert h.error_of(foreign_apply)["code"] == "PLAN_CUSTOMER_MISMATCH"
        assert not server.events("mutation")
        assert server.call("confirm_and_apply", {"plan_id": plan["id"], "customer_id": same, "dry_run": False})["applied"]
        writes = server.events("mutation")
        assert len(writes) == 1 and writes[0]["request_type"] == "MutateCampaignsRequest"
        assert writes[0]["request"]["customer_id"] == ACCOUNT
        assert writes[0]["request"]["operations"][0]["update"]["resource_name"] == f"customers/{ACCOUNT}/campaigns/111"
        records = server.audit()
        assert {r["customer_id"] for r in records} == {ACCOUNT}
        apply_refusals = [r for r in records if r["event"] == "refused" and r["tool"] == "confirm_and_apply"]
        assert len(apply_refusals) == 1 and apply_refusals[0]["plan_id"] == plan["id"]
        assert h.error_of(server.call("confirm_and_apply", {"plan_id": plan["id"], "dry_run": False}))["code"] == "PLAN_CONSUMED"
