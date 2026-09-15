"""F043: installed OAuth recovery and real file confidentiality boundaries.

Only consent/exchange are synthetic. File wrappers delegate real opens/writes,
record descriptor modes at writes, and optionally raise filesystem errors. No
chmod, temporary-file or replacement algorithm is required by these tests.
"""
import json
import re
import subprocess

import pytest

import harness as h


TOKEN = "synthetic-recovery-refresh-token-DO-NOT-DISCLOSE"
SECRET = "synthetic-recovery-client-secret-DO-NOT-DISCLOSE"
CODE = "synthetic-recovery-authorization-code-DO-NOT-DISCLOSE"
DETAIL = "synthetic-recovery-provider-detail-DO-NOT-DISCLOSE"
OLD = b'{"refresh_token": "synthetic-preexisting-user-token"}'

INJECTION = r'''
import builtins
import http.server
import io
import json
import os
from pathlib import Path
import stat
import sys
import urllib.error
import urllib.request
import urllib.parse
import webbrowser

original_open = builtins.open
marker = os.environ["ORACLE_MARKER"]
def mark(event, **fields):
    with original_open(marker, "a") as stream:
        stream.write(json.dumps({"event": event, **fields}) + "\n")
mark("loaded")
os.umask(0o022)

def deny_network(event, args):
    if event == "socket.bind" and args[1] == ("::1", 0):
        raise OSError("offline IPv6 capability probe")
    if event in ("socket.connect", "socket.bind", "socket.getaddrinfo"):
        mark("network attempted")
        raise OSError("offline oracle denies network")
sys.addaudithook(deny_network)

scenario = os.environ["ORACLE_SCENARIO"]
issued_state = ""
def browser(url):
    global issued_state
    issued_state = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).get("state", [""])[0]
    mark("browser")
    return True
class Loopback:
    def __init__(self, address, handler):
        mark("loopback", port=address[1])
        if scenario == "bind":
            raise OSError(48, os.environ["ORACLE_DETAIL"])
        self.handler = handler
    def handle_request(self):
        mark("callback")
        handler = self.handler.__new__(self.handler)
        handler.path = "/?" + urllib.parse.urlencode({"code": os.environ["ORACLE_CODE"], "state": issued_state})
        handler.wfile = io.BytesIO()
        handler.send_response = lambda *a: None
        handler.send_header = lambda *a: None
        handler.end_headers = lambda: None
        handler.do_GET()
    def server_close(self):
        pass
def exchange(request, *args, **kwargs):
    mark("exchange")
    if scenario == "http":
        raise urllib.error.HTTPError(request.full_url, 400,
            os.environ["ORACLE_DETAIL"], {}, io.BytesIO(os.environ["ORACLE_BODY"].encode()))
    if scenario == "transport":
        raise urllib.error.URLError(os.environ["ORACLE_DETAIL"])
    return io.BytesIO(os.environ["ORACLE_BODY"].encode())
webbrowser.open = browser
http.server.HTTPServer = Loopback
urllib.request.urlopen = exchange

directory = Path(os.environ["ORACLE_OUTPUT"]).parent
failure = os.environ.get("ORACLE_FAILURE", "")
tracked_fds = set()
write_states = {}
token_inodes = set()
def in_output(path):
    try:
        return Path(os.fspath(path)).absolute().is_relative_to(directory)
    except TypeError:
        return False
def fail_permission():
    mark("permission denied")
    raise PermissionError(os.environ["ORACLE_DETAIL"])
def observed_write(fd, writer, value):
    info = os.fstat(fd)
    written = writer(value)
    if written and stat.S_ISREG(info.st_mode):
        inode = (info.st_dev, info.st_ino)
        state = write_states.setdefault(inode, {"bytes": bytearray(), "pending": []})
        state["bytes"].extend(bytes(value)[:written])
        state["pending"].append({"mode": stat.S_IMODE(info.st_mode), "size": written})
        if os.environ["ORACLE_TOKEN"].encode() in state["bytes"]:
            token_inodes.add(inode)
            for fields in state["pending"]:
                mark("write", **fields)
            state["pending"].clear()
    return written
def instrument(stream):
    # Observe the raw descriptor write rather than TextIOWrapper.write: a
    # caller may legally tighten permissions while text is still buffered.
    buffer = getattr(stream, "buffer", stream)
    raw = getattr(buffer, "raw", buffer)
    if not getattr(raw, "_oracle_observed", False):
        original = raw.write
        raw.write = lambda value: observed_write(raw.fileno(), original, value)
        raw._oracle_observed = True
    return stream
def wrap_open(opener):
    def opened(file, *args, **kwargs):
        mode = args[0] if args else kwargs.get("mode", "r")
        writing = isinstance(mode, str) and any(c in mode for c in "wax+")
        tracked = in_output(file) or (isinstance(file, int) and file in tracked_fds)
        if tracked and writing and failure == "prewrite":
            fail_permission()
        stream = opener(file, *args, **kwargs)
        if writing:
            if tracked:
                tracked_fds.add(stream.fileno())
            return instrument(stream)
        return stream
    return opened
builtins.open = wrap_open(builtins.open)
io.open = wrap_open(io.open)
os.fdopen = wrap_open(os.fdopen)
original_os_open, original_write = os.open, os.write
def opened_fd(path, flags, mode=0o777, *, dir_fd=None):
    tracked = in_output(path) or (dir_fd in tracked_fds if dir_fd is not None else False)
    writing = bool(flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC))
    if tracked and writing and failure == "prewrite":
        fail_permission()
    fd = original_os_open(path, flags, mode, dir_fd=dir_fd)
    if tracked:
        tracked_fds.add(fd)
    return fd
def write_fd(fd, value):
    return observed_write(fd, lambda data: original_write(fd, data), value)
os.open, os.write = opened_fd, write_fd
original_chmod, original_fchmod = os.chmod, os.fchmod
def has_token(path):
    try:
        info = os.fstat(path) if isinstance(path, int) else os.stat(path)
        return (info.st_dev, info.st_ino) in token_inodes
    except OSError:
        return False
def chmod(path, mode, *args, **kwargs):
    if (in_output(path) or has_token(path)) and failure in ("chmod", "prewrite"):
        fail_permission()
    return original_chmod(path, mode, *args, **kwargs)
def fchmod(fd, mode):
    if (fd in tracked_fds or has_token(fd)) and failure in ("chmod", "prewrite"):
        fail_permission()
    return original_fchmod(fd, mode)
os.chmod, os.fchmod = chmod, fchmod
def wrap_move(move):
    def moved(source, destination, *args, **kwargs):
        if in_output(destination) and failure == "prewrite":
            fail_permission()
        return move(source, destination, *args, **kwargs)
    return moved
os.rename, os.replace = wrap_move(os.rename), wrap_move(os.replace)
'''


def launch(tmp_path, *, scenario="success", body=None, failure="", existing=False,
           port=None):
    injection = tmp_path / "injection"
    injection.mkdir()
    (injection / "sitecustomize.py").write_text(INJECTION)
    directory = tmp_path / "token-output"
    directory.mkdir(mode=0o755)
    output = directory / "token.json"
    if existing:
        output.write_bytes(OLD)
        output.chmod(0o644)
    credential = tmp_path / "synthetic-client.json"
    credential.write_text(json.dumps({"installed": {
        "client_id": "synthetic-client.apps.example.org", "client_secret": SECRET}}))
    marker = tmp_path / "observations.jsonl"
    env = h.scrubbed_env({"PYTHONPATH": str(injection), "PYTHONDONTWRITEBYTECODE": "1",
        "ORACLE_MARKER": str(marker), "ORACLE_OUTPUT": str(output),
        "ORACLE_SCENARIO": scenario, "ORACLE_FAILURE": failure,
        "ORACLE_BODY": json.dumps({"refresh_token": TOKEN}) if body is None else body,
        "ORACLE_CODE": CODE, "ORACLE_DETAIL": DETAIL, "ORACLE_TOKEN": TOKEN})
    args = [str(h.console_script("ads-mcp-generate-token")), "--credentials", str(credential),
            "--token-path", str(output)]
    if port is not None:
        args.extend(["--port", str(port)])
    result = subprocess.run(args, cwd=tmp_path, env=env, capture_output=True, text=True, timeout=40)
    observations = [json.loads(line) for line in marker.read_text().splitlines()]
    assert observations[0]["event"] == "loaded"
    assert not any(r["event"] == "network attempted" for r in observations)
    return result, output, observations


def diagnostics(result):
    text = result.stdout + result.stderr
    assert "Traceback" not in text, result.stderr
    for planted in (TOKEN, SECRET, CODE, DETAIL):
        assert planted not in text


def clean_failure(result, *, guidance):
    assert result.returncode != 0, "the installed helper reported success for invalid input"
    diagnostics(result)
    lines = [line for line in result.stderr.splitlines() if line.strip()]
    assert len(lines) == 1 and len(lines[0]) <= 600, result.stderr
    match = re.match(r"(?:[a-zA-Z0-9_.-]+: )?([A-Z][A-Z0-9_]+):", lines[0])
    assert match, result.stderr
    assert any(word in lines[0].lower() for word in guidance), result.stderr
    return match[1]


def private_writes(observations):
    writes = [r for r in observations if r["event"] == "write"]
    assert writes, "observe the actual file write, not only the final chmod"
    assert all(r["mode"] & 0o077 == 0 for r in writes), (
        f"new token bytes were written with group/other read access: {writes}")


@pytest.mark.parametrize("existing", [False, True])
def test_token_bytes_are_private_throughout_real_new_or_existing_output(tmp_path, existing):
    result, output, observations = launch(tmp_path, existing=existing)
    assert result.returncode == 0, result.stderr
    private_writes(observations)
    assert json.loads(output.read_text()) == {"refresh_token": TOKEN}
    assert output.stat().st_mode & 0o777 == 0o600
    assert all(any(r["event"] == event for r in observations) for event in ("browser", "callback", "exchange"))
    assert "authorize" in result.stdout.lower() and "written" in result.stdout.lower()
    diagnostics(result)


@pytest.mark.parametrize("existing", [False, True])
def test_failed_permission_tightening_never_exposes_new_token_bytes(tmp_path, existing):
    result, output, observations = launch(tmp_path, failure="chmod", existing=existing)
    writes = [r for r in observations if r["event"] == "write"]
    assert all(r["mode"] & 0o077 == 0 for r in writes), observations
    if result.returncode == 0:
        # Direct private creation is valid even when every chmod would fail.
        private_writes(observations)
        assert json.loads(output.read_text()) == {"refresh_token": TOKEN}
        assert output.stat().st_mode & 0o777 == 0o600
        diagnostics(result)
    else:
        assert clean_failure(result, guidance=("permission", "path", "writable")) == "AUTH_TOKEN_WRITE_FAILED"
    for path in output.parent.rglob("*"):
        if path.is_file() and TOKEN.encode() in path.read_bytes():
            assert path.stat().st_mode & 0o077 == 0


@pytest.mark.parametrize("existing", [False, True])
def test_prewrite_permission_failure_preserves_user_owned_content(tmp_path, existing):
    result, output, observations = launch(tmp_path, failure="prewrite", existing=existing)
    assert clean_failure(result, guidance=("permission", "path", "writable")) == "AUTH_TOKEN_WRITE_FAILED"
    assert any(r["event"] == "permission denied" for r in observations)
    assert all(r["mode"] & 0o077 == 0 for r in observations if r["event"] == "write")
    assert output.read_bytes() == OLD if existing else not output.exists()
    assert all(path.stat().st_mode & 0o077 == 0 for path in output.parent.rglob("*")
               if path.is_file() and TOKEN.encode() in path.read_bytes())


def test_unavailable_loopback_port_has_clean_actionable_recovery(tmp_path):
    result, output, observations = launch(tmp_path, scenario="bind")
    clean_failure(result, guidance=("port",))
    assert any(r["event"] == "loopback" for r in observations)
    assert not any(r["event"] == "exchange" for r in observations)
    assert not output.exists()


@pytest.mark.parametrize("port", [-1, 0, 65536, "not-a-port", "1.5"])
def test_invalid_configured_ports_refuse_before_consent_or_exchange(tmp_path, port):
    result, output, observations = launch(tmp_path, port=port)
    clean_failure(result, guidance=("port",))
    assert not any(r["event"] in ("loopback", "callback", "exchange") for r in observations)
    assert not output.exists()


@pytest.mark.parametrize("scenario,guidance", [("http", ("authoriz", "consent")),
                                               ("transport", ("retry", "network", "connection"))])
def test_exchange_failures_have_sanitized_actionable_recovery(tmp_path, scenario, guidance):
    result, output, observations = launch(tmp_path, scenario=scenario,
        body=json.dumps({"error": "invalid_grant", "error_description": DETAIL, "access_token": TOKEN}))
    clean_failure(result, guidance=guidance)
    assert any(r["event"] == "exchange" for r in observations)
    assert not output.exists()


def test_http_rejection_and_transport_failure_have_distinct_named_outcomes(tmp_path):
    codes = []
    for scenario, guidance in (("http", ("authoriz", "consent")), ("transport", ("retry", "network", "connection"))):
        case = tmp_path / scenario
        case.mkdir()
        result, output, observations = launch(case, scenario=scenario,
            body=json.dumps({"error": "invalid_grant", "error_description": DETAIL, "access_token": TOKEN}))
        codes.append(clean_failure(result, guidance=guidance))
        assert any(r["event"] == "exchange" for r in observations)
        assert not output.exists()
    assert codes[0] != codes[1], "rejected authorization and failed transport need distinct named outcomes"


@pytest.mark.parametrize("body", [
    "{" + DETAIL, "[]", "null", json.dumps(DETAIL), "7", "true", "{}",
    json.dumps({DETAIL: TOKEN}), *[json.dumps({"refresh_token": value}) for value in
        (None, "", " \t\n", False, True, 0, 42, [], [TOKEN], {"token": TOKEN})],
])
def test_malformed_exchange_responses_never_create_a_token_file(tmp_path, body):
    result, output, observations = launch(tmp_path, body=body)
    clean_failure(result, guidance=("response", "exchange", "refresh", "token", "authoriz"))
    assert any(r["event"] == "exchange" for r in observations)
    assert not output.exists() and not list(output.parent.iterdir())


@pytest.mark.parametrize("port", [1, 65535])
def test_valid_port_boundaries_and_nonempty_token_preserve_exact_value(tmp_path, port):
    supplied = "  " + TOKEN + "  "
    result, output, observations = launch(tmp_path, port=port,
        body=json.dumps({"refresh_token": supplied, "access_token": DETAIL}))
    assert result.returncode == 0, result.stderr
    assert json.loads(output.read_text()) == {"refresh_token": supplied}
    assert output.stat().st_mode & 0o777 == 0o600
    assert any(r["event"] == "loopback" and r["port"] == port for r in observations)
    diagnostics(result)
