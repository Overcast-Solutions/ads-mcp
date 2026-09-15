"""F058: private, nonblocking, append-only audit destinations.

All paths contain synthetic data in pytest tmp directories. Standard OS/open
boundaries observe the real destination, including replacement and pre-write
failures. No audit implementation helper or new production seam is assumed.
"""
from concurrent.futures import ThreadPoolExecutor
import builtins
import errno
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys

import pytest
from google.api_core import exceptions as core_exceptions

import harness as h
from ads_mcp.audit import AuditLog
from ads_mcp.errors import ToolError


RECORD = {"event": "plan_created", "tool": "synthetic", "customer_id": h.CUSTOMER_ID,
          "plan_id": "synthetic-plan", "outcome": "OK"}


def observe_file_io(monkeypatch, destination, *, permission_failure=False, write_failure=False, replace_with=None):
    """Capture the actual file at its first write, not its mode after return."""
    opened, writes = [], []
    real_open, real_io_open, real_write = os.open, io.open, os.write
    real_chmod, real_fchmod = os.chmod, os.fchmod
    replaced = False

    def targets(value):
        try:
            return not isinstance(value, int) and Path(value) == destination
        except TypeError:
            return False

    def swap(value):
        nonlocal replaced
        if replace_with is not None and targets(value) and not replaced:
            replaced = True
            destination.unlink()
            destination.symlink_to(replace_with)

    def opening(path, *args, **kwargs):
        swap(path)
        fd = real_open(path, *args, **kwargs)
        if targets(path):
            opened.append(fd)
        return fd

    def before_write(fd):
        if fd in opened:
            mode = stat.S_IMODE(os.fstat(fd).st_mode)
            writes.append(mode)
            if write_failure:
                raise OSError(errno.ENOSPC, "synthetic private write detail")

    class ObservedFile:
        def __init__(self, file):
            self.file = file
        def __getattr__(self, name):
            return getattr(self.file, name)
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return self.file.__exit__(*args)
        def write(self, data):
            before_write(self.file.fileno())
            return self.file.write(data)
        def writelines(self, lines):
            for line in lines:
                self.write(line)

    def io_open(path, *args, **kwargs):
        swap(path)
        file = real_io_open(path, *args, **kwargs)
        if targets(path) or (isinstance(path, int) and path in opened):
            if file.fileno() not in opened:
                opened.append(file.fileno())
            return ObservedFile(file)
        return file

    def writing(fd, data):
        before_write(fd)
        return real_write(fd, data)

    def chmod(path, mode, *args, **kwargs):
        if permission_failure and targets(path):
            raise PermissionError("synthetic permission tightening failure")
        return real_chmod(path, mode, *args, **kwargs)

    def fchmod(fd, mode):
        if permission_failure and fd in opened:
            raise PermissionError("synthetic permission tightening failure")
        return real_fchmod(fd, mode)

    monkeypatch.setattr(os, "open", opening)
    monkeypatch.setattr(io, "open", io_open)
    monkeypatch.setattr(builtins, "open", io_open)
    monkeypatch.setattr(os, "write", writing)
    monkeypatch.setattr(os, "chmod", chmod)
    monkeypatch.setattr(os, "fchmod", fchmod)
    return opened, writes


def closed(fds):
    assert fds, "probe did not observe an acquired output descriptor"
    for fd in set(fds):
        with pytest.raises(OSError) as exc:
            os.fstat(fd)
        assert exc.value.errno == errno.EBADF


@pytest.mark.parametrize("existing_mode", [None, 0o644, 0o666, 0o600])
def test_private_from_first_byte_and_preserves_prior_jsonl(tmp_path, monkeypatch, existing_mode):
    path = tmp_path / "audit.jsonl"
    old = json.dumps({"event": "old-synthetic"}) + "\n"
    if existing_mode is not None:
        path.write_text(old)
        path.chmod(existing_mode)
    opened, writes = observe_file_io(monkeypatch, path)
    mask = os.umask(0)
    try:
        assert AuditLog(path).write(RECORD, critical=True) is True
    finally:
        os.umask(mask)
    assert writes and all(mode & 0o077 == 0 for mode in writes), "audit bytes were written while readable by group/others"
    assert stat.S_IMODE(path.stat().st_mode) & 0o077 == 0
    content = path.read_text()
    if existing_mode is not None:
        assert content.startswith(old)
    records = list(map(json.loads, content.splitlines()))
    assert records[-1]["plan_id"] == RECORD["plan_id"]
    closed(opened)


@pytest.mark.parametrize("critical", [False, True])
def test_failed_permission_tightening_preserves_all_existing_bytes(tmp_path, monkeypatch, critical):
    path = tmp_path / "audit.jsonl"
    old = b'{"event":"prior synthetic record"}\n'
    path.write_bytes(old)
    path.chmod(0o644)
    opened, writes = observe_file_io(monkeypatch, path, permission_failure=True)
    if critical:
        with pytest.raises(ToolError, match="audit") as exc:
            AuditLog(path).write(RECORD, critical=True)
        assert exc.value.code == "AUDIT_WRITE_FAILED"
    else:
        assert AuditLog(path).write(RECORD) is False
    assert path.read_bytes() == old and not writes
    closed(opened)


@pytest.mark.parametrize("critical", [False, True])
def test_write_failure_closes_descriptors_and_has_named_critical_error(tmp_path, monkeypatch, critical):
    path = tmp_path / "audit.jsonl"
    path.write_text('{"event":"prior"}\n')
    path.chmod(0o600)
    opened, writes = observe_file_io(monkeypatch, path, write_failure=True)
    if critical:
        with pytest.raises(ToolError) as exc:
            AuditLog(path).write(RECORD, critical=True)
        assert exc.value.code == "AUDIT_WRITE_FAILED"
        assert "synthetic private write detail" not in str(exc.value)
    else:
        assert AuditLog(path).write(RECORD) is False
    assert writes and path.read_text() == '{"event":"prior"}\n'
    closed(opened)


@pytest.mark.parametrize("destination", ["symlink", "dangling", "directory", "fifo", "fifo_reader"])
def test_unsafe_destinations_refuse_promptly_without_content_or_mode_changes(tmp_path, destination):
    path, victim = tmp_path / "audit", tmp_path / "victim"
    victim.write_bytes(b"prior synthetic private bytes")
    victim.chmod(0o644)
    reader = None
    if destination == "symlink":
        path.symlink_to(victim)
    elif destination == "dangling":
        path.symlink_to(tmp_path / "absent")
    elif destination == "directory":
        path.mkdir()
    else:
        os.mkfifo(path, 0o644)
        if destination == "fifo_reader":
            reader = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    original_mode = path.lstat().st_mode
    script = '''
import json, sys
from ads_mcp.audit import AuditLog
from ads_mcp.errors import ToolError
try:
    result = AuditLog(sys.argv[1]).write({"event": "synthetic"}, critical=True)
except ToolError as exc:
    print(json.dumps({"code": exc.code, "message": exc.message}))
else:
    print(json.dumps({"accepted": result}))
'''
    try:
        try:
            proc = subprocess.run([sys.executable, "-c", script, str(path)], capture_output=True, text=True, timeout=3)
        except subprocess.TimeoutExpired:
            pytest.fail("audit destination blocked on a special file instead of promptly refusing")
        assert proc.returncode == 0 and "Traceback" not in proc.stderr
        assert json.loads(proc.stdout).get("code") == "AUDIT_WRITE_FAILED", "unsafe audit destination accepted"
        assert victim.read_bytes() == b"prior synthetic private bytes" and stat.S_IMODE(victim.stat().st_mode) == 0o644
        assert path.lstat().st_mode == original_mode
        assert not (tmp_path / "absent").exists()
        if reader is not None:
            try:
                data = os.read(reader, 4096)
            except BlockingIOError:
                data = b""
            assert data == b"", "audit bytes reached the FIFO reader"
    finally:
        if reader is not None:
            os.close(reader)


def test_replacement_between_validation_and_open_cannot_follow_symlink(tmp_path, monkeypatch):
    path, victim = tmp_path / "audit", tmp_path / "victim"
    path.write_text('{"event":"old"}\n')
    victim.write_text("synthetic unrelated content")
    victim.chmod(0o644)
    opened, writes = observe_file_io(monkeypatch, path, replace_with=victim)
    with pytest.raises(ToolError) as exc:
        AuditLog(path).write(RECORD, critical=True)
    assert exc.value.code == "AUDIT_WRITE_FAILED"
    assert victim.read_text() == "synthetic unrelated content"
    assert stat.S_IMODE(victim.stat().st_mode) == 0o644
    assert not writes
    if opened:
        closed(opened)


def test_multiple_audit_instances_append_complete_concurrent_records(tmp_path):
    path = tmp_path / "audit.jsonl"
    def append(index):
        return AuditLog(path).write({**RECORD, "sequence": index, "padding": "x" * 12000}, critical=True)
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert all(pool.map(append, range(64)))
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert len(rows) == 64 and {r["sequence"] for r in rows} == set(range(64))
    assert all(r["padding"] == "x" * 12000 for r in rows)
    assert stat.S_IMODE(path.stat().st_mode) & 0o077 == 0


@pytest.mark.parametrize("phase", ["plan", "apply"])
def test_secure_audit_refusal_closes_mutation_but_reads_continue(tmp_path, account_client, phase):
    path = tmp_path / "critical-audit"
    victim = tmp_path / "victim"
    victim.write_bytes(b"previous unrelated content")
    server = h.build_rw_server(tmp_path, client=account_client, env={"ADS_MCP_AUDIT_LOG": str(path), "ADS_MCP_RETRY_BASE_SECONDS": "0.001"})
    args = {"entity_type": "campaign", "entity_id": "111"}
    if phase == "apply":
        plan = h.expect_ok(h.call(server, "pause_entity", args))["plan"]
        path.unlink()
    path.symlink_to(victim)
    if phase == "plan":
        error = h.error_of(h.call(server, "pause_entity", args))
    else:
        error = h.error_of(h.call(server, "confirm_and_apply", {"plan_id": plan["id"], "dry_run": False}))
    assert error["code"] == "AUDIT_WRITE_FAILED"
    assert not account_client.mutations and victim.read_bytes() == b"previous unrelated content"
    account_client.stub_error(core_exceptions.ServiceUnavailable("synthetic retry"), times=1)
    read = h.expect_ok(h.call(server, "run_gaql", {"query": "SELECT customer.id FROM customer"}))
    assert read["rows"] and victim.read_bytes() == b"previous unrelated content"
