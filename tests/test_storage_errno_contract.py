"""F055: distinguish unavailable secure storage from path/permission failures.

Host-local syscall faults, actual installed console, genuine raw HTTP parsing,
and real files. No product functions are replaced. The F052 transport/driver
was read completely; only its neutral plumbing is loaded into a private module,
with an explicit adjacent-console lookup (no harness environment or assertions).
The review producers/results are provenance, never imported oracle assertions.
"""

import ast
import base64
import errno
import hashlib
import json
from pathlib import Path
import re
import socket
import stat
import sys
import types
import urllib.parse

import pytest


PLUMBING = Path(__file__).with_name("test_local_auth_io_contract.py")

# Runs before the borrowed transport imports and before the installed entry point.
# The import-time IPv6 capability probe is denied too; it is not provider I/O.
CHILD_GUARD = r'''
import os
import sys
_observer_fstat = os.fstat
_network_attempts = []
def errno_guard(event, args):
    if event in ("socket.connect", "socket.bind", "socket.sendto", "socket.sendmsg",
                 "socket.getaddrinfo", "socket.gethostbyname", "socket.gethostbyaddr",
                 "socket.getnameinfo"):
        if not (event == "socket.bind" and args[1] == ("::1", 0)):
            _network_attempts.append(event)
        raise OSError("F055 synthetic child denies network")
    if event in ("subprocess.Popen", "os.system", "os.posix_spawn", "os.fork", "os.forkpty"):
        raise OSError("F055 synthetic child denies further children")
sys.addaudithook(errno_guard)
'''


STORAGE_FAULTS = r'''
import errno
import hashlib

fault = FAULT_SETTINGS
if fault["private"]:
    # Fixture preparation, before fault installation or product execution.
    output.chmod(0o600)

tracked = {}
def output_path(path):
    return (isinstance(path, (str, bytes, os.PathLike))
            and Path(os.fsdecode(path)).absolute().is_relative_to(output.parent))

def remember(fd):
    info = _observer_fstat(fd)
    tracked[fd] = (info.st_dev, info.st_ino)

def output_fd(fd):
    if fd not in tracked:
        return False
    info = _observer_fstat(fd)
    return tracked[fd] == (info.st_dev, info.st_ino)

def fail(operation):
    if fault["kind"] == "notimplemented":
        exc = NotImplementedError(os.environ["LOCAL_PRIVATE"])
    else:
        exc = OSError(getattr(errno, fault["kind"]), os.environ["LOCAL_PRIVATE"])
    mark("storage fault", operation=operation, errno=getattr(exc, "errno", None),
         exception=type(exc).__name__)
    raise exc

previous_open = os.open
def storage_open(path, flags, *args, **kwargs):
    relevant = output_path(path)
    if relevant and fault["operation"] == "open":
        fail("open")
    fd = previous_open(path, flags, *args, **kwargs)
    if relevant:
        remember(fd)
    return fd
os.open = storage_open

# Cover streams that do not use os.open, including an alternative private
# replacement file. All inspection here uses the saved native fstat, never the
# injected product-facing function. No function-name or call-count targeting.
def track_streams(opener):
    def opened(*args, **kwargs):
        stream = opener(*args, **kwargs)
        if output_path(getattr(stream, "name", None)):
            remember(stream.fileno())
        return stream
    return opened
for owner, name in ((builtins, "open"), (io, "open"), (os, "fdopen")):
    setattr(owner, name, track_streams(getattr(owner, name)))

# Retain actual successful output-write chunks, including deleted replacement
# files and incomplete JSON values. Evidence writes themselves are out of scope.
previous_writing = writing
def writing(fd, writer, data):
    info = _observer_fstat(fd)
    relevant = output_fd(fd)
    def write_and_record(value):
        count = writer(value)
        if relevant and count:
            mark("output bytes", identity=[info.st_dev, info.st_ino],
                 data=bytes(value)[:count].hex())
        return count
    return previous_writing(fd, write_and_record, data)

if fault["operation"] in ("fstat", "fchmod", "ftruncate"):
    operation = fault["operation"]
    if fault["kind"] == "missing":
        delattr(os, operation)
    else:
        native_operation = getattr(os, operation)
        def storage_operation(fd, *args, **kwargs):
            if output_fd(fd):
                fail(operation)
            return native_operation(fd, *args, **kwargs)
        setattr(os, operation, storage_operation)

mark("fault configured", **fault)
def storage_finished():
    leaks = 0
    for fd, identity in tracked.items():
        try:
            info = _observer_fstat(fd)
        except OSError:
            continue
        leaks += (info.st_dev, info.st_ino) == identity
    mark("descriptor cleanup", leaked=leaks, acquired=len(tracked))
    mark("network guard", attempted=_network_attempts)
    import ads_mcp.auth
    path = Path(ads_mcp.auth.__file__)
    mark("source binding", path=str(path), sha256=hashlib.sha256(path.read_bytes()).hexdigest())
atexit.register(storage_finished)
'''


@pytest.fixture(autouse=True)
def contained_parent(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("TMPDIR", str(tmp_path))

    def deny(*args, **kwargs):
        raise AssertionError("F055 parent denies network")

    for name in ("connect", "connect_ex", "bind", "sendto", "sendmsg"):
        if hasattr(socket.socket, name):
            monkeypatch.setattr(socket.socket, name, deny)
    for name in ("getaddrinfo", "gethostbyname", "gethostbyname_ex", "gethostbyaddr", "getnameinfo"):
        monkeypatch.setattr(socket, name, deny)


def plumbing():
    """Select only fully inspected transport/driver code, isolated per attempt."""
    tree = ast.parse(PLUMBING.read_text())
    names = {"TOKEN", "SECRET", "PRIVATE", "CODE", "OLD", "INJECTION",
             "observations", "fingerprint", "Attempt", "launch"}
    nodes = []
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            if isinstance(node, ast.Import) and any(a.name == "harness" for a in node.names):
                continue
            nodes.append(node)
        elif isinstance(node, ast.Assign) and any(
                isinstance(t, ast.Name) and t.id in names for t in node.targets):
            nodes.append(node)
        elif isinstance(node, (ast.FunctionDef, ast.ClassDef)) and node.name in names:
            nodes.append(node)
    module = types.ModuleType("_f055_local_plumbing")
    # dataclasses resolves module annotations through sys.modules. This private
    # name does not replace/import/modify the existing test module or harness.
    sys.modules[module.__name__] = module
    module.h = types.SimpleNamespace(
        console_script=lambda name: Path(sys.executable).with_name(name))
    exec(compile(ast.Module(body=nodes, type_ignores=[]), str(PLUMBING), "exec"), module.__dict__)
    # This changes only the borrowed test observer, inside this child's string.
    # Missing/failing fstat must never disable marker writes or byte observers.
    assert module.INJECTION.count("info = os.fstat(fd)") == 1
    module.INJECTION = module.INJECTION.replace(
        "info = os.fstat(fd)", "info = _observer_fstat(fd)")
    return module


def launch(tmp_path, record_property, *, operation=None, kind=None,
           destination="existing", private=False):
    local = plumbing()
    # Public fixture with a different prefix from OLD, so a partial new JSON
    # token is distinguishable from copying the existing content to a backup.
    local.TOKEN = "f055-new-refresh-token-public-fixture"
    fault = {"operation": operation, "kind": kind, "private": private}
    local.INJECTION = (CHILD_GUARD + local.INJECTION
                       + STORAGE_FAULTS.replace("FAULT_SETTINGS", repr(fault)))
    attempt = local.launch(tmp_path / "helper", destination=destination)
    evidence = {
        "command": attempt.result.args,
        "exit": attempt.result.returncode,
        "diagnostic": attempt.result.stderr.strip(),
        "source": attempt.events("source binding"),
        "faults": attempt.events("storage fault"),
        "configured": attempt.events("fault configured"),
        "listeners": len(attempt.events("listener")),
        "closed": len(attempt.events("closed")),
        "exchanges": len(attempt.events("exchange")),
        "cleanup": attempt.events("descriptor cleanup"),
        "token_writes": attempt.events("token write"),
        "output_write_chunks": len(attempt.events("output bytes")),
        "network": attempt.events("network guard"),
        "old_preserved": (attempt.output.read_bytes() == local.OLD
                          if destination == "existing" else None),
    }
    record_property("installed_helper_observation", json.dumps(evidence, sort_keys=True))
    assert attempt.prompt, "installed helper exceeded bounded deadline; killed and drained"
    assert attempt.events("finished"), "installed entry path did not finish"
    assert "Error in sitecustomize" not in attempt.result.stderr, "fault plumbing failed"
    assert attempt.events("fault configured") == [{"event": "fault configured", **fault}]
    assert len(attempt.events("source binding")) == 1
    cleanup = attempt.events("descriptor cleanup")
    assert len(cleanup) == 1 and cleanup[0]["leaked"] == 0, "acquired output descriptor leaked"
    assert len(attempt.events("closed")) == len(attempt.events("listener")), "listener leaked"
    assert attempt.events("network guard") == [{"event": "network guard", "attempted": []}]
    assert not attempt.events("network attempted")
    assert_confidential(attempt, local)
    # A correct early preflight need not create a listener or exchange a code.
    if attempt.events("exchange"):
        assert_exchange(attempt, local)
    return attempt, local


def public_output(attempt):
    stdout = attempt.result.stdout
    for row in attempt.events("browser"):
        stdout = stdout.replace(row["url"], "<issued consent URL>")
    return stdout + attempt.result.stderr + "\n".join(
        row["raw"] for row in attempt.events("response"))


def assert_confidential(attempt, local):
    public = public_output(attempt)
    values = [local.TOKEN, local.SECRET, local.PRIVATE, local.CODE,
              json.loads(local.OLD)["refresh_token"]]
    for row in attempt.events("browser"):
        values += urllib.parse.parse_qs(urllib.parse.urlsplit(row["url"]).query).get("state", [])
    for row in attempt.events("exchange"):
        values += urllib.parse.parse_qs(row["body"]).get("code_verifier", [])
    for value in values:
        assert all(encoded not in public for encoded in (
            value, urllib.parse.quote(value, safe=""), urllib.parse.quote_plus(value))), (
                "private callback/credential/token material escaped")
    assert "Traceback" not in public and "Error in sitecustomize" not in public


def assert_exchange(attempt, local):
    assert len(attempt.events("exchange")) == len(attempt.events("browser")) == 1
    assert len(attempt.events("accepted")) == len(attempt.events("listener")) == 1
    consent = urllib.parse.parse_qs(urllib.parse.urlsplit(attempt.events("browser")[0]["url"]).query)
    exchange = urllib.parse.parse_qs(attempt.events("exchange")[0]["body"])
    verifier = exchange["code_verifier"][0]
    assert re.fullmatch(r"[A-Za-z0-9._~-]{43,128}", verifier)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).decode().rstrip("=")
    assert consent["code_challenge"] == [challenge] and consent["code_challenge_method"] == ["S256"]
    assert consent["state"] == [attempt.events("accepted")[0]["state"]]
    assert exchange["code"] == [local.CODE] and exchange["client_secret"] == [local.SECRET]
    assert exchange["client_id"] == consent["client_id"]
    assert exchange["redirect_uri"] == consent["redirect_uri"]
    assert exchange["grant_type"] == ["authorization_code"]
    assert 0 < attempt.events("exchange")[0]["timeout"] <= 30
    assert len(attempt.events("response")) == 1
    raw = attempt.events("response")[0]["raw"]
    assert raw.split("\r\n", 1)[0].split()[1] == "200"
    assert "received" in attempt.body.lower() and "terminal" in attempt.body.lower()


def assert_private_success(attempt, local):
    assert attempt.result.returncode == 0, attempt.result.stderr
    assert json.loads(attempt.output.read_text()) == {"refresh_token": local.TOKEN}
    info = attempt.output.stat()
    assert stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o600
    writes = attempt.events("token write")
    assert writes, "real file observer did not see token bytes"
    assert all(row["regular"] and row["mode"] == 0o600 for row in writes)
    # Success must complete consent, real parser, exchange, and real private write.
    assert_exchange(attempt, local)


CAPABILITY = re.compile(
    r"unsupported|not supported|not implemented|enosys|enotsup|eopnotsupp|"
    r"(?:capabilit\w*|operation|function|api|fchmod|fstat|ftruncate).{0,50}"
    r"(?:unavailable|not available|missing|absent)|"
    r"(?:unavailable|missing|absent).{0,50}(?:capabilit|operation|function|api)|"
    r"(?:secure|private).{0,30}storage.{0,30}(?:unavailable|not available|absent)|"
    r"(?:platform|filesystem|system).{0,50}(?:cannot|can't|does not|lack)|"
    r"(?:cannot|can't).{0,35}(?:secure|protect|guarantee.{0,15}priv)", re.I)
SECURE_SETUP = re.compile(
    r"secur|privat|protect|safe|permission|posix|nonblock|fchmod|fstat|ftruncate", re.I)
RECOVERY = re.compile(
    r"\b(?:use|using|choose|select|switch|run|rerun|retry|try|configure|install|check|see)\b", re.I)
SETUP = re.compile(
    r"platform|filesystem|file system|environment|system|posix|unix|"
    r"(?:new|different|another|private|secure|supported).{0,35}(?:file|path|storage|destination)|"
    r"(?:readme|documentation|storage requirements)", re.I)
PATH_PERMISSION = re.compile(
    r"path|director|permission|writ(?:able|e)|access|file|denied|eacces|eperm|enoent", re.I)


def assert_safe_failure(attempt, local, *, capability):
    assert attempt.result.returncode != 0
    # Check safety before the intended diagnostic red. Permission tightening
    # already performed on a later failed boundary need not be rolled back.
    if attempt.before and attempt.before[0] == stat.S_IFREG:
        assert attempt.output.is_file() and attempt.output.read_bytes() == local.OLD
    assert not attempt.events("token write"), "failed pre-write storage emitted token bytes"
    written = {}
    for row in attempt.events("output bytes"):
        written.setdefault(tuple(row["identity"]), bytearray()).extend(bytes.fromhex(row["data"]))
    for data in written.values():
        assert local.TOKEN.encode() not in data, "new token written then removed"
        for value in re.findall(rb'"refresh_token"\s*:\s*"([^"\\]*)', data):
            assert not (value and local.TOKEN.encode().startswith(value)), "partial new token written"
    for path in attempt.output.parent.rglob("*"):
        if path.is_file():
            assert local.TOKEN.encode() not in path.read_bytes(), "new token left in output or replacement"
    status = re.sub(r"<[^>]+>", "\n", public_output(attempt))
    assert not re.search(
        r"^\s*(?:(?:(?:the|your)\s+)?(?:refresh\s+)?token\s+"
        r"(?:(?:was|has been)\s+)?(?:written|saved)|successfully\s+(?:saved|written|authorized)|"
        r"authorization\s+(?:complete|successful))\b", status, re.I | re.M), "failed storage claimed success"
    lines = [line for line in attempt.result.stderr.splitlines() if line.strip()]
    assert len(lines) == 1 and len(lines[0]) <= 600, "expected one concise recovery diagnostic"
    assert lines[0].startswith("AUTH_TOKEN_WRITE_FAILED:"), lines[0]
    guidance = lines[0].partition(":")[2]
    assert RECOVERY.search(guidance), "storage diagnostic lacks a recovery action"
    if capability:
        assert (CAPABILITY.search(guidance) and SECURE_SETUP.search(guidance)
                and SETUP.search(guidance)), (
            "unavailable secure-storage operation was not explained as a capability limitation: " + lines[0])
    else:
        assert PATH_PERMISSION.search(guidance), "ordinary failure lacks path/permission recovery"
        assert not CAPABILITY.search(guidance), "ordinary path/permission failure mislabeled as unavailable capability"


UNSUPPORTED = ["ENOSYS", "ENOTSUP"]
if hasattr(errno, "EOPNOTSUPP") and errno.EOPNOTSUPP not in {errno.ENOSYS, errno.ENOTSUP}:
    UNSUPPORTED.append("EOPNOTSUPP")


def assert_fault_outcome(attempt, local, *, capability):
    if attempt.result.returncode == 0:
        # A different secure storage algorithm may avoid the faulted operation.
        # Do not require fchmod, fstat, truncation, call order, or replacement.
        assert not attempt.events("storage fault"), "unsupported pre-write operation claimed success"
        assert_private_success(attempt, local)
    else:
        assert_safe_failure(attempt, local, capability=capability)


@pytest.mark.parametrize("kind", UNSUPPORTED)
def test_existing_file_tightening_explains_unsupported_errno(tmp_path, record_property, kind):
    attempt, local = launch(tmp_path, record_property, operation="fchmod", kind=kind)
    assert_fault_outcome(attempt, local, capability=True)


# One representative unavailable error at each other actual pre-write boundary;
# do not multiply every errno by every operation and destination.
@pytest.mark.parametrize("operation,kind", [
    ("open", "ENOSYS"), ("fstat", "ENOTSUP"), ("ftruncate", "ENOSYS"),
], ids=["open-ENOSYS", "fstat-ENOTSUP", "ftruncate-ENOSYS"])
def test_other_prewrite_boundaries_explain_unavailable_capability(tmp_path, record_property, operation, kind):
    attempt, local = launch(tmp_path, record_property, operation=operation, kind=kind)
    assert_fault_outcome(attempt, local, capability=True)


@pytest.mark.parametrize("operation,kind", [
    ("open", "EACCES"), ("fstat", "EPERM"),
    ("fchmod", "EACCES"), ("ftruncate", "EPERM"),
], ids=["open-EACCES", "fstat-EPERM", "fchmod-EACCES", "ftruncate-EPERM"])
def test_ordinary_prewrite_errors_keep_path_permission_recovery(tmp_path, record_property, operation, kind):
    attempt, local = launch(tmp_path, record_property, operation=operation, kind=kind)
    assert_fault_outcome(attempt, local, capability=False)


def test_real_missing_parent_keeps_path_recovery(tmp_path, record_property):
    attempt, local = launch(tmp_path, record_property, destination="missing-parent")
    assert_safe_failure(attempt, local, capability=False)
    assert not attempt.output.exists()


@pytest.mark.parametrize("kind", ["missing", "notimplemented"])
def test_existing_unavailable_api_recovery_controls(tmp_path, record_property, kind):
    attempt, local = launch(tmp_path, record_property, operation="fchmod", kind=kind)
    assert_fault_outcome(attempt, local, capability=True)


@pytest.mark.parametrize("destination", ["new", "existing"])
def test_native_private_storage_completes_real_flow(tmp_path, record_property, destination):
    attempt, local = launch(tmp_path, record_property, destination=destination)
    assert_private_success(attempt, local)


@pytest.mark.parametrize("destination", ["new", "existing"])
def test_unavailable_fchmod_is_unnecessary_for_private_storage(tmp_path, record_property, destination):
    attempt, local = launch(tmp_path, record_property, operation="fchmod", kind="ENOTSUP",
                            destination=destination, private=destination == "existing")
    assert_private_success(attempt, local)
