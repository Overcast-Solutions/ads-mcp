"""F064: authenticated health agrees with the secure audit writer.

The existing MCP/stdio drivers and synthetic SDK fixtures are read-only reuse.
Only standard file boundaries and the existing audit writer are observed; no
readiness helper, replacement response, or new implementation seam is assumed.
"""
from contextlib import contextmanager
from contextvars import ContextVar
import builtins
import io
import json
import os
from pathlib import Path
import socket
import stat
from types import SimpleNamespace

import grpc
import pytest
from google.api_core.exceptions import ServiceUnavailable

import harness as h
from pmax_oracle import assert_catalog
import test_auth_cause_contract as plumbing
from ads_mcp.audit import AuditLog
from test_confirmation_queue_contract import QueueServer
from test_private_audit_destination_contract import observe_file_io
from tool_catalog import ALL_WRITE_MODE_TOOLS, READ_TOOLS


INJECTION = r'''
import sys
def offline(event, args):
    if event in ("socket.connect", "socket.bind", "socket.getaddrinfo", "socket.sendto"):
        raise OSError("synthetic health contract forbids networking")
sys.addaudithook(offline)
''' + plumbing.INJECTION + r'''
import grpc
def deny_channel(*args, **kwargs):
    mark("network attempted")
    raise AssertionError("synthetic health contract forbids provider channels")
grpc.secure_channel = grpc.insecure_channel = deny_channel
from google.api_core.exceptions import ServiceUnavailable
original_search = Transport._do_search
def search_with_one_retry(self, *args, **kwargs):
    rows = original_search(self, *args, **kwargs)
    if json.loads(control.read_text()).get("retry_once") and not getattr(self, "retried", False):
        self.retried = True
        raise ServiceUnavailable("synthetic temporary read failure")
    return rows
Transport._do_search = search_with_one_retry
audit_path = os.path.abspath(os.environ["ADS_MCP_AUDIT_LOG"])
def observe_unsafe_open(event, args):
    if event != "open" or isinstance(args[0], int) or not os.path.islink(audit_path):
        return
    opened = os.path.abspath(args[0])
    target = os.path.abspath(os.path.join(os.path.dirname(audit_path), os.readlink(audit_path)))
    if opened == target or (opened == audit_path and not args[2] & os.O_NOFOLLOW):
        mark("unsafe audit open")
sys.addaudithook(observe_unsafe_open)
'''
PAUSE = {"entity_type": "campaign", "entity_id": "111"}
PRIOR = b'{"event":"prior-synthetic-record"}\n'


@pytest.fixture(autouse=True)
def offline_runtime(monkeypatch, tmp_path):
    def deny(*args, **kwargs):
        raise AssertionError("synthetic health contract forbids networking")
    for name in ("connect", "connect_ex", "bind", "sendto"):
        monkeypatch.setattr(socket.socket, name, deny)
    monkeypatch.setattr(socket, "getaddrinfo", deny)
    monkeypatch.setattr(grpc, "secure_channel", deny)
    monkeypatch.setattr(grpc, "insecure_channel", deny)
    monkeypatch.setattr(plumbing, "INJECTION", INJECTION)
    monkeypatch.setattr(h, "scrubbed_env", lambda overlay=None: {
        "PATH": os.environ.get("PATH", os.defpath), "HOME": str(tmp_path),
        "TMPDIR": str(tmp_path), "PYTHONNOUSERSITE": "1", **(overlay or {})})


@contextmanager
def connected(root, boundary, read_only=False):
    env = {"ADS_MCP_READ_ONLY": str(read_only).lower(),
           "ADS_MCP_RETRY_BASE_SECONDS": "0.001"}
    if boundary == "installed-stdio":
        with QueueServer(root, env=env) as server:
            yield SimpleNamespace(
                call=server.call,
                catalog=lambda: {t["name"] for t in server.receive(
                    server.send("tools/list", {}))["result"]["tools"]},
                searches=lambda: server.events("search"),
                mutations=lambda: server.events("mutation"),
                unsafe_opens=lambda: server.events("unsafe audit open"),
                retry=lambda: server.mode(retry_once=True))
    else:
        provider = h.stub_standard_account(h.FakeGoogleAdsClient())
        server = h.build_rw_server(root, client=provider, env=env)
        yield SimpleNamespace(
            call=lambda name, args=None: h.call(server, name, args),
            catalog=lambda: h.tool_names(server),
            searches=lambda: [vars(row) for row in provider.searches],
            mutations=lambda: provider.mutations,
            retry=lambda: provider.stub_error(
                ServiceUnavailable("synthetic temporary read failure"), times=1))


def file_state(path):
    return path.read_bytes(), stat.S_IMODE(path.stat().st_mode)


def configuration(root):
    return {name: file_state(root / name)
            for name in ("oauth_client.json", "oauth_refresh.json")}


def health_shape(payload, root, read_only, status):
    # Preserve the exact public payload, including legacy presence metadata.
    assert payload == {
        "status": status,
        "config": {"customer_id": h.CUSTOMER_ID,
                   "login_customer_id": h.LOGIN_CUSTOMER_ID,
                   "developer_token": "present",
                   "credentials_path": str(root / "oauth_client.json"),
                   "token_path": str(root / "oauth_refresh.json")},
        "credentials": {"state": "OK"},
        "guardrails": {"read_only": read_only, "require_dry_run": False,
                       "max_daily_budget": 100, "max_bid_increase_pct": 100,
                       "max_first_bid": 10, "plan_ttl_seconds": 900,
                       "row_limit": 1000, "audit_log": str(h.audit_file(root))},
    }


@pytest.mark.parametrize("boundary", ["framework", "installed-stdio"])
@pytest.mark.parametrize("read_only", [True, False], ids=["readonly", "write-mode"])
@pytest.mark.parametrize("kind", ["writable-target", "dangling"])
def test_symlink_health_matches_plan_and_apply_refusal(tmp_path, boundary, read_only, kind):
    path, target = h.audit_file(tmp_path), tmp_path / "target"
    if kind == "writable-target":
        target.write_bytes(b"synthetic unrelated user data\n")
        target.chmod(0o666)
    target_before = file_state(target) if target.exists() else None
    with connected(tmp_path, boundary, read_only) as server:
        config_before = configuration(tmp_path)
        assert_catalog(server.catalog(), read_only=read_only)
        health_shape(server.call("health_check"), tmp_path, read_only, "OK")
        plan = None
        if not read_only:
            plan = h.expect_ok(server.call("pause_entity", PAUSE))["plan"]
            assert plan["operations"][0]["resource"] == f"customers/{h.CUSTOMER_ID}/campaigns/111"
        if path.exists():
            path.rename(tmp_path / "saved-audit.jsonl")
        saved_path = tmp_path / "saved-audit.jsonl"
        saved = file_state(saved_path) if saved_path.exists() else None
        path.symlink_to(target)
        link_before = path.lstat().st_mode, os.readlink(path)
        before = len(server.searches())
        result = server.call("health_check")
        probes = server.searches()[before:]
        assert probes and all(row["customer_id"] == h.CUSTOMER_ID for row in probes)
        assert any("customer.id" in row["query"] for row in probes)
        if plan:
            for tool, args in [("pause_entity", PAUSE), ("confirm_and_apply", {
                "plan_id": plan["id"], "dry_run": False})]:
                error = h.error_of(server.call(tool, args))
                assert error["code"] == "AUDIT_WRITE_FAILED"
                assert "audit" in error["message"].lower()
                h.assert_no_secrets(json.dumps(error))
        if saved is not None:
            assert file_state(saved_path) == saved
        assert not server.mutations()
        assert (path.lstat().st_mode, os.readlink(path)) == link_before
        assert configuration(tmp_path) == config_before
        if target_before is None:
            assert not target.exists(), "health must not create a dangling link's target"
        else:
            assert file_state(target) == target_before
        # Assert after writer and preservation controls so a red documents the
        # actual disagreement, rather than short-circuiting the safety checks.
        assert result["status"] == "AUDIT_UNWRITABLE", (
            f"{kind}: authenticated health reported {result['status']} for a refused audit symlink")
        health_shape(result, tmp_path, read_only, "AUDIT_UNWRITABLE")
        if boundary == "installed-stdio":
            assert not server.unsafe_opens(), "readiness followed the unsafe audit destination"


@pytest.mark.parametrize("read_only", [True, False], ids=["readonly", "write-mode"])
@pytest.mark.parametrize("mode", [None, 0o600, 0o644], ids=["new", "private", "normalizable"])
def test_installed_valid_neighbors_keep_observations_and_private_writes(tmp_path, read_only, mode):
    path = h.audit_file(tmp_path)
    if mode is not None:
        path.write_bytes(PRIOR)
        path.chmod(mode)
    with connected(tmp_path, "installed-stdio", read_only) as server:
        before = configuration(tmp_path)
        assert_catalog(server.catalog(), read_only=read_only)
        server.retry()
        health_shape(server.call("health_check"), tmp_path, read_only, "OK")
        rows = [json.loads(line) for line in path.read_bytes().splitlines()]
        if mode is not None:
            assert path.read_bytes().startswith(PRIOR)
            rows = rows[1:]
        assert len(rows) == 1 and rows[0]["event"] == "retry"
        assert rows[0]["tool"] == "health_check" and rows[0]["customer_id"] == h.CUSTOMER_ID
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        if not read_only:
            plan = h.expect_ok(server.call("pause_entity", PAUSE))["plan"]
            assert server.call("confirm_and_apply", {"plan_id": plan["id"]}) == {
                "applied": False, "plan": plan}
            assert server.call("confirm_and_apply", {
                "plan_id": plan["id"], "dry_run": False})["applied"] is True
            assert len(server.mutations()) == 1
        assert configuration(tmp_path) == before
        h.assert_no_secrets(path.read_text())


@pytest.mark.parametrize("kind", ["directory", "fifo"])
def test_installed_nonregular_health_refuses_without_blocking(tmp_path, kind):
    path = h.audit_file(tmp_path)
    path.mkdir() if kind == "directory" else os.mkfifo(path, 0o600)
    before = path.lstat().st_mode
    with connected(tmp_path, "installed-stdio") as server:
        health_shape(server.call("health_check"), tmp_path, False, "AUDIT_UNWRITABLE")
        assert h.error_of(server.call("pause_entity", PAUSE))["code"] == "AUDIT_WRITE_FAILED"
        assert not server.mutations()
        assert path.lstat().st_mode == before


@pytest.mark.parametrize("existing", [False, True], ids=["new", "existing"])
def test_readiness_adds_no_trial_file_or_probe_bytes_but_keeps_read_audit(tmp_path, monkeypatch, existing):
    path = h.audit_file(tmp_path)
    if existing:
        path.write_bytes(PRIOR)
    with connected(tmp_path, "framework") as server:
        config_before = configuration(tmp_path)
        _, writes = observe_file_io(monkeypatch, path)
        ordinary = ContextVar("ordinary_audit_write", default=False)
        original_write = AuditLog.write
        normal_writes, creations = [], []

        def audit_write(self, *args, **kwargs):
            token = ordinary.set(True)
            start = len(writes)
            try:
                return original_write(self, *args, **kwargs)
            finally:
                normal_writes.extend(writes[start:])
                ordinary.reset(token)

        def observe_creation(value, creating):
            if ordinary.get() or isinstance(value, int) or not creating:
                return
            candidate = Path(value).absolute()
            if tmp_path in candidate.parents and not os.path.lexists(candidate):
                creations.append(candidate)

        real_io, real_os = io.open, os.open

        def opening(value, mode="r", *args, **kwargs):
            observe_creation(value, any(c in mode for c in "wax"))
            return real_io(value, mode, *args, **kwargs)

        def opening_fd(value, flags, *args, **kwargs):
            observe_creation(value, flags & os.O_CREAT)
            return real_os(value, flags, *args, **kwargs)

        monkeypatch.setattr(AuditLog, "write", audit_write)
        monkeypatch.setattr(builtins, "open", opening)
        monkeypatch.setattr(io, "open", opening)
        monkeypatch.setattr(os, "open", opening_fd)
        server.retry()
        result = server.call("health_check")
        assert not creations, "readiness created a trial file outside normal audit observation"
        assert writes == normal_writes and writes, "readiness added probe writes or lost ordinary audit"
        content = path.read_bytes()
        assert content.startswith(PRIOR if existing else b"")
        rows = [json.loads(line) for line in content.splitlines()][int(existing):]
        assert len(rows) == 1 and rows[0]["event"] == "retry"
        assert rows[0]["tool"] == "health_check" and rows[0]["customer_id"] == h.CUSTOMER_ID
        assert configuration(tmp_path) == config_before
        health_shape(result, tmp_path, False, "OK")
