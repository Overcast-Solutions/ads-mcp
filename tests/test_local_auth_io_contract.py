"""F052: installed OAuth helper, genuine HTTP parsing and real FIFO operations.

Only external consent, readiness/listener and exchange transports are synthetic.
The real BaseServer timeout/dispatch and BaseHTTPRequestHandler parser run. FIFO
opens are observed, never made nonblocking by the fixture; a reader is introduced
only as a control or to release a defective writer during orderly cleanup.
Runtime response bytes remain in tmp_path for the separate builder visual gate.
"""
import base64
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import socket
import stat
import subprocess
import time
import urllib.parse

import pytest

import harness as h


TOKEN = "synthetic-local-refresh-token"
SECRET = "synthetic-local-client-secret"
PRIVATE = "synthetic-local-input-do-not-reflect"
CODE = "synthetic+code/with=encoding"
OLD = b'{"refresh_token":"synthetic-existing-token"}\n'

INJECTION = r'''
import atexit
import builtins
import http.server
import io
import json
import os
from pathlib import Path
import socketserver
import stat
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

settings = json.loads(os.environ["LOCAL_SETTINGS"])
marker = Path(os.environ["LOCAL_MARKER"])
output = Path(os.environ["LOCAL_OUTPUT"])
def mark(event, **fields):
    with marker.open("a") as stream:
        stream.write(json.dumps({"event": event, **fields}) + "\n")
mark("loaded")
def audit(event, args):
    if event == "socket.bind" and args[1] == ("::1", 0):
        # urllib3 probes local IPv6 capability at import. Deny the bind too,
        # without misclassifying the blocked capability probe as provider I/O.
        raise OSError("offline IPv6 capability probe")
    if event in ("socket.connect", "socket.bind", "socket.getaddrinfo", "socket.sendto"):
        mark("network attempted", operation=event)
        raise OSError("offline helper contract forbids network")
    if event == "open" and isinstance(args[0], (str, bytes)):
        path = Path(os.fsdecode(args[0])).absolute()
        if path.is_relative_to(output.parent) and isinstance(args[2], int):
            if args[2] & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC):
                mark("output open", destination=path == output)
                if settings.get("permission_failure"):
                    mark("permission refused")
                    raise PermissionError(os.environ["LOCAL_PRIVATE"])
sys.addaudithook(audit)
os.umask(0o022)

issued = None
def browser(url):
    global issued
    issued = url
    mark("browser", url=url)
    if settings["browser"] == "exception":
        raise webbrowser.Error(os.environ["LOCAL_PRIVATE"])
    return settings["browser"] != "false"
webbrowser.open = browser

class RawConnection:
    def __init__(self, raw):
        self.raw = raw
        self.output = bytearray()
    def makefile(self, *args, **kwargs):
        return io.BytesIO(self.raw)
    def sendall(self, data):
        self.output.extend(data)

class ListeningTransport:
    def gettimeout(self):
        # A bounded synthetic listener timeout; BaseServer itself chooses the
        # minimum, waits for readiness and takes its real no-request timeout path.
        return 0.05

class Readiness:
    def __enter__(self):
        return self
    def __exit__(self, *args):
        pass
    def register(self, *args):
        pass
    def select(self, timeout):
        if settings["case"] == "no-request":
            started = time.monotonic()
            time.sleep(timeout)
            mark("no request", elapsed=time.monotonic() - started, timeout=timeout)
            return []
        return [(None, None)]
socketserver._ServerSelector = Readiness

class Loopback(http.server.HTTPServer):
    def __init__(self, address, handler):
        # BaseServer initialization needs no socket and retains its genuine
        # handle_request -> process_request -> finish_request dispatch.
        socketserver.BaseServer.__init__(self, address, handler)
        self.socket = ListeningTransport()
        self.server_name, self.server_port = address
        self.accepted = 0
        mark("listener")
    def get_request(self):
        self.accepted += 1
        assert self.accepted == 1, "first callback must terminate the attempt"
        params = urllib.parse.parse_qs(urllib.parse.urlsplit(issued).query)
        state = params["state"][0]
        private = os.environ["LOCAL_PRIVATE"]
        path = "/?" + urllib.parse.urlencode({"code": os.environ["LOCAL_CODE"], "state": state})
        kind = settings["case"]
        method, version = settings["method"], "HTTP/1.1"
        if kind == "invalid":
            path += "&state=" + private
        elif kind == "denied":
            path = "/?" + urllib.parse.urlencode({"state": state, "error": "access_denied", "error_description": private})
        elif kind == "parser":
            version = "HTTP/" + private
        tail = ("Host: localhost\r\nX-Synthetic: " + private + "\r\nConnection: close\r\n\r\n" + private)
        if kind == "blank":
            raw = ("\r\n" + tail).encode("ascii")
        elif kind == "whitespace":
            raw = (" \t \r\n" + tail).encode("ascii")
        elif kind == "eof":
            raw = b""
        else:
            raw = (method + " " + path + " " + version + "\r\n" + tail).encode("ascii")
        mark("accepted", bytes=len(raw), first_line=raw.split(b"\n", 1)[0].decode("ascii"), state=state)
        self.request = RawConnection(raw)
        return self.request, ("127.0.0.1", 43210)
    def shutdown_request(self, request):
        mark("response", raw=bytes(request.output).decode("utf-8", errors="replace"))
    def handle_timeout(self):
        mark("listener timed out", accepted=self.accepted)
    def server_close(self):
        mark("closed")
http.server.HTTPServer = Loopback

def exchange(request, *args, **kwargs):
    mark("exchange", body=request.data.decode("ascii"), timeout=kwargs.get("timeout"))
    if settings["exchange"] == "timeout":
        raise TimeoutError(os.environ["LOCAL_PRIVATE"])
    if settings["exchange"] == "http":
        raise urllib.error.HTTPError("https://example.invalid/token", 400,
            os.environ["LOCAL_PRIVATE"], {}, io.BytesIO(os.environ["LOCAL_PRIVATE"].encode()))
    return io.BytesIO(json.dumps({"refresh_token": os.environ["LOCAL_TOKEN"]}).encode())
urllib.request.urlopen = exchange

real_os_open = os.open
def observed_os_open(path, flags, *args, **kwargs):
    observed = os.fspath(path) == str(output)
    if observed:
        mark("os open entered")
    fd = real_os_open(path, flags, *args, **kwargs)
    if observed:
        mark("os open returned")
    return fd
os.open = observed_os_open

write_states = {}
def writing(fd, writer, data):
    info = os.fstat(fd)
    written = writer(data)
    if written:
        key = (info.st_dev, info.st_ino)
        state = write_states.setdefault(key, {"data": bytearray(), "pending": []})
        state["data"].extend(bytes(data)[:written])
        state["pending"].append({"mode": stat.S_IMODE(info.st_mode),
            "regular": stat.S_ISREG(info.st_mode)})
        if os.environ["LOCAL_TOKEN"].encode() in state["data"]:
            for fields in state["pending"]:
                mark("token write", **fields)
            state["pending"].clear()
    return written

def instrument(stream):
    # Observe the OS descriptor write, after text buffering. Tightening mode
    # while text is still buffered is valid; splitting writes is also valid.
    buffer = getattr(stream, "buffer", stream)
    raw = getattr(buffer, "raw", buffer)
    if not getattr(raw, "_local_observed", False):
        original = raw.write
        raw.write = lambda data: writing(raw.fileno(), original, data)
        raw._local_observed = True
    return stream

def observing(opener):
    def opened(*args, **kwargs):
        stream = opener(*args, **kwargs)
        # Observe all writable streams, including a privately created replacement.
        return instrument(stream) if stream.writable() else stream
    return opened
builtins.open = observing(builtins.open)
io.open = observing(io.open)
os.fdopen = observing(os.fdopen)
real_write = os.write
def observed_write(fd, data):
    return writing(fd, lambda value: real_write(fd, value), data)
os.write = observed_write

def finished():
    import ads_mcp.auth
    mark("finished", module_file=ads_mcp.auth.__file__)
atexit.register(finished)
'''


@pytest.fixture(autouse=True)
def offline_parent(monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError("F052 parent forbids network")
    for name in ("connect", "connect_ex", "bind", "sendto"):
        monkeypatch.setattr(socket.socket, name, deny)
    monkeypatch.setattr(socket, "getaddrinfo", deny)


def observations(marker):
    if not marker.exists():
        return []
    return [json.loads(line) for line in marker.read_text().splitlines(keepends=True)
            if line.endswith("\n")]


def fingerprint(path):
    info = path.lstat()
    return (stat.S_IFMT(info.st_mode), stat.S_IMODE(info.st_mode),
            path.read_bytes() if stat.S_ISREG(info.st_mode) else None)


@dataclass
class Attempt:
    result: subprocess.CompletedProcess
    rows: list
    output: Path
    before: tuple | None
    prompt: bool
    pipe_bytes: bytes

    def events(self, name):
        return [r for r in self.rows if r["event"] == name]

    @property
    def body(self):
        responses = self.events("response")
        assert len(responses) == 1
        raw = responses[0]["raw"]
        return raw.partition("\r\n\r\n")[2] if "\r\n\r\n" in raw else raw


def launch(tmp_path, *, case="valid", method="GET", destination="existing",
           browser="success", exchange="success", permission_failure=False):
    tmp_path.mkdir(parents=True, exist_ok=True)
    injection = tmp_path / "injection"
    injection.mkdir()
    (injection / "sitecustomize.py").write_text(INJECTION)
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    output = output_dir / "token.json"
    if destination == "existing":
        output.write_bytes(OLD)
        output.chmod(0o640)
    elif destination.startswith("fifo"):
        os.mkfifo(output, 0o640)
    elif destination == "directory":
        output.mkdir()
    elif destination == "missing-parent":
        output = output_dir / "absent" / "token.json"
    before = fingerprint(output) if output.exists() else None
    credentials = tmp_path / "client.json"
    credentials.write_text(json.dumps({"installed": {"client_id": "synthetic-local.apps.example.org", "client_secret": SECRET}}))
    marker = tmp_path / "events.jsonl"
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "TMPDIR": str(tmp_path),
        "PYTHONPATH": str(injection), "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1",
        "LOCAL_SETTINGS": json.dumps({"case": case, "method": method, "browser": browser,
            "exchange": exchange, "permission_failure": permission_failure}),
        "LOCAL_MARKER": str(marker), "LOCAL_OUTPUT": str(output),
        "LOCAL_PRIVATE": PRIVATE, "LOCAL_CODE": CODE, "LOCAL_TOKEN": TOKEN}
    command = [str(h.console_script("ads-mcp-generate-token")), "--credentials", str(credentials), "--token-path", str(output)]
    reader = None
    process = None
    prompt = True
    pipe_bytes = b""
    try:
        if destination == "fifo-reader":
            reader = os.open(output, os.O_RDONLY | os.O_NONBLOCK)
        process = subprocess.Popen(command, cwd=tmp_path, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if destination == "fifo-no-reader":
            # A repair may refuse before opening. Otherwise observe arrival at
            # the real open (audit hook and optional OS-call observer), then start
            # the refusal deadline; slow process startup does not count as a bug.
            deadline = time.monotonic() + 10
            while process.poll() is None and time.monotonic() < deadline:
                rows = observations(marker)
                if any(r["event"] in ("os open entered", "output open") for r in rows):
                    break
                time.sleep(0.01)
            ready = process.poll() is not None or any(
                r["event"] in ("os open entered", "output open") for r in observations(marker))
            if ready:
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    prompt = False
            else:
                prompt = False
            # Reader creation follows the entire no-reader observation. It is
            # cleanup/causal control, never a way to turn the missing refusal green.
            reader = os.open(output, os.O_RDONLY | os.O_NONBLOCK)
        try:
            stdout, stderr = process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            prompt = False
            process.kill()
            stdout, stderr = process.communicate(timeout=5)
        if reader is not None:
            try:
                pipe_bytes = os.read(reader, 65536)
            except BlockingIOError:
                pipe_bytes = b""
        result = subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
    finally:
        if process is not None and process.poll() is None:
            process.kill()
            process.communicate(timeout=5)
        if reader is not None:
            os.close(reader)
    rows = observations(marker)
    assert rows and rows[0]["event"] == "loaded", "installed helper injection did not initialize"
    assert not any(r["event"] == "network attempted" for r in rows)
    attempt = Attempt(result, rows, output, before, prompt, pipe_bytes)
    # Finished is intentionally checked after timeout assertions in callers:
    # cleanup killing a stuck process is behavioral failure, not setup error.
    return attempt


def clean_output(attempt):
    stdout = attempt.result.stdout
    for row in attempt.events("browser"):
        stdout = stdout.replace(row["url"], "<issued-url>")
    public = (stdout + attempt.result.stderr +
              "".join(r["raw"] for r in attempt.events("response")))
    values = [TOKEN, SECRET, PRIVATE, CODE]
    values += [r["state"] for r in attempt.events("accepted")]
    for row in attempt.events("exchange"):
        values += urllib.parse.parse_qs(row["body"])["code_verifier"]
    for value in values:
        assert value not in public and urllib.parse.quote(value, safe="") not in public
    assert "Traceback" not in public


def failure(attempt, code, *, exchanged=False):
    assert attempt.prompt, "installed helper did not refuse/finish before bounded deadline; subprocess cleaned up"
    assert attempt.events("finished")
    # A destination preflight may correctly refuse before opening a listener.
    # If the helper did create one, every terminal outcome must close it.
    if attempt.events("listener"):
        assert attempt.events("closed")
    assert attempt.result.returncode != 0
    lines = [line for line in attempt.result.stderr.splitlines() if line.strip()]
    assert len(lines) == 1 and len(lines[0]) <= 600
    assert lines[0].startswith(code + ":"), lines[0]
    assert any(word in lines[0].lower() for word in ("retry", "rerun", "try again"))
    if not exchanged:
        assert not attempt.events("exchange") and not attempt.events("output open")
    if attempt.before is not None:
        if exchanged and attempt.before[0] == stat.S_IFREG:
            # A failed storage attempt may already have tightened permissions;
            # preservation of the previous token does not require undoing that.
            assert attempt.output.read_bytes() == attempt.before[2]
        else:
            assert fingerprint(attempt.output) == attempt.before
    assert not attempt.events("token write")
    clean_output(attempt)


@pytest.mark.parametrize("case", ["blank", "whitespace", "eof"])
def test_incomplete_received_connection_is_invalid_not_elapsed_timeout(tmp_path, case):
    attempt = launch(tmp_path / "received", case=case)
    assert len(attempt.events("accepted")) == 1
    assert not attempt.events("no request") and not attempt.events("listener timed out")
    assert attempt.events("closed") and not attempt.events("exchange")
    assert fingerprint(attempt.output) == attempt.before
    clean_output(attempt)
    # Check both intended outcomes after the process has exited, so the red
    # report names actual parser/console behavior rather than a timeout exception.
    problems = []
    if not attempt.result.stderr.startswith("AUTH_CALLBACK_INVALID:"):
        problems.append("received connection returned " + attempt.result.stderr.strip())
    if case != "eof":
        control = launch(tmp_path / "invalid-control", case="invalid")
        failure(control, "AUTH_CALLBACK_INVALID")
        if attempt.body != control.body or "invalid" not in attempt.body.lower():
            problems.append("received blank/whitespace line has no static invalid recovery page")
    assert not problems, "; ".join(problems)
    failure(attempt, "AUTH_CALLBACK_INVALID")


@pytest.mark.parametrize("case,method,code", [
    ("no-request", "GET", "AUTH_CALLBACK_TIMEOUT"),
    ("denied", "GET", "AUTH_CONSENT_DENIED"),
    ("invalid", "GET", "AUTH_CALLBACK_INVALID"),
    ("parser", "GET", "AUTH_CALLBACK_INVALID"),
    ("valid", "POST", "AUTH_CALLBACK_INVALID"),
    ("valid", "HEAD", "AUTH_CALLBACK_INVALID"),
])
def test_supported_callback_failure_and_true_no_request_controls(tmp_path, case, method, code):
    attempt = launch(tmp_path, case=case, method=method)
    failure(attempt, code)
    if case == "no-request":
        assert not attempt.events("accepted") and not attempt.events("response")
        assert attempt.events("listener timed out") == [{"event": "listener timed out", "accepted": 0}]
        assert attempt.events("no request")[0]["elapsed"] >= attempt.events("no request")[0]["timeout"] > 0
    elif method == "HEAD":
        assert attempt.body == "", "HEAD keeps no-body semantics without redemption"
    else:
        assert ("denied" if case == "denied" else "invalid") in attempt.body.lower()
        assert "terminal" in attempt.body.lower()


@pytest.mark.parametrize("destination,browser", [("new", "false"), ("existing", "exception")])
def test_regular_file_success_is_private_and_retains_state_pkce_and_manual_fallback(tmp_path, destination, browser):
    attempt = launch(tmp_path, destination=destination, browser=browser)
    assert attempt.prompt and attempt.result.returncode == 0, attempt.result.stderr
    assert json.loads(attempt.output.read_text()) == {"refresh_token": TOKEN}
    assert stat.S_IMODE(attempt.output.stat().st_mode) == 0o600
    assert attempt.events("token write")
    assert all(r["regular"] and r["mode"] & 0o077 == 0 for r in attempt.events("token write"))
    assert attempt.events("finished") and attempt.events("closed")
    assert len(attempt.events("exchange")) == 1
    consent = urllib.parse.parse_qs(urllib.parse.urlsplit(attempt.events("browser")[0]["url"]).query)
    exchange = urllib.parse.parse_qs(attempt.events("exchange")[0]["body"])
    verifier = exchange["code_verifier"][0]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).decode().rstrip("=")
    assert consent["code_challenge"] == [challenge] and consent["code_challenge_method"] == ["S256"]
    assert attempt.events("accepted")[0]["state"] == consent["state"][0]
    assert exchange["code"] == [CODE] and exchange["client_secret"] == [SECRET]
    assert 0 < attempt.events("exchange")[0]["timeout"] <= 30
    assert "manually" in attempt.result.stdout.lower()
    assert "received" in attempt.body.lower() and "terminal" in attempt.body.lower()
    clean_output(attempt)


@pytest.mark.parametrize("destination", ["fifo-no-reader", "fifo-reader"])
def test_real_fifo_refuses_without_reader_dependency_bytes_or_metadata_changes(tmp_path, destination):
    attempt = launch(tmp_path, destination=destination)
    assert fingerprint(attempt.output) == attempt.before, "FIFO type or permissions changed"
    assert stat.S_ISFIFO(attempt.output.stat().st_mode)
    assert attempt.pipe_bytes == b"" and not attempt.events("token write")
    clean_output(attempt)
    assert attempt.prompt, (
        "FIFO destination did not promptly refuse while no reader existed; real open "
        "was observed, then a reader released it and the subprocess was drained/closed")
    failure(attempt, "AUTH_TOKEN_WRITE_FAILED", exchanged=True)


@pytest.mark.parametrize("destination", ["directory", "missing-parent"])
def test_existing_output_path_error_controls(tmp_path, destination):
    attempt = launch(tmp_path, destination=destination)
    failure(attempt, "AUTH_TOKEN_WRITE_FAILED", exchanged=True)
    assert not attempt.output.is_file()


def test_prewrite_permission_refusal_preserves_old_regular_file(tmp_path):
    attempt = launch(tmp_path, permission_failure=True)
    assert attempt.events("permission refused")
    failure(attempt, "AUTH_TOKEN_WRITE_FAILED", exchanged=True)


@pytest.mark.parametrize("exchange,code", [("timeout", "AUTH_EXCHANGE_UNAVAILABLE"), ("http", "AUTH_EXCHANGE_REJECTED")])
def test_exchange_failures_preserve_output_and_pending_page(tmp_path, exchange, code):
    attempt = launch(tmp_path, exchange=exchange)
    failure(attempt, code, exchanged=True)
    assert len(attempt.events("exchange")) == 1 and not attempt.events("output open")
    assert "received" in attempt.body.lower() and "terminal" in attempt.body.lower()
