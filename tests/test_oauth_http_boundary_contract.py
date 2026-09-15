"""F048: real installed helper, raw BaseHTTPRequestHandler request parsing.

Only browser/listener/exchange transports are replaced. No do_GET shortcut or
replacement parser/error emitter is used. Actual response bytes are returned
for the separate driver-owned five-state visual gate; pytest needs no browser.
"""
import base64
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
import stat
import subprocess
import urllib.parse

import pytest

import harness as h
from test_oauth_consent_contract import fingerprint


PRIVATE = "synthetic-http-private-DO-NOT-REFLECT"
TOKEN = "synthetic-http-refresh-DO-NOT-DISCLOSE"
SECRET = "synthetic-http-secret-DO-NOT-DISCLOSE"
OLD = b'{"refresh_token":"synthetic-existing-http-token"}\n'
INJECTION = r'''
import atexit
import http.server
import io
import json
import os
from pathlib import Path
import socket
import sys
import urllib.parse
import urllib.request
import webbrowser

settings = json.loads(os.environ["HTTP_SETTINGS"])
marker = Path(os.environ["HTTP_MARKER"])
output = Path(os.environ["HTTP_OUTPUT"])
def mark(event, **fields):
    with marker.open("a") as stream:
        stream.write(json.dumps({"event": event, **fields}) + "\n")
mark("loaded")
def audit(event, args):
    if event == "socket.bind" and args[1] == ("::1", 0):
        raise OSError("offline IPv6 capability probe")
    if event in ("socket.connect", "socket.bind", "socket.getaddrinfo"):
        mark("network attempted")
        raise OSError("offline raw HTTP contract forbids sockets")
    if event == "open" and isinstance(args[0], (str, bytes)):
        path = Path(os.fsdecode(args[0])).absolute()
        if path.is_relative_to(output.parent) and isinstance(args[2], int):
            if args[2] & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC):
                mark("output write attempted")
sys.addaudithook(audit)
os.umask(0o022)
issued = None
def browser(url):
    global issued
    issued = url
    mark("browser", url=url)
    if settings["browser"] == "exception":
        raise OSError(os.environ["HTTP_PRIVATE"])
    return settings["browser"] != "false"
webbrowser.open = browser

class RawRequest:
    def __init__(self, raw):
        self.raw = raw
        self.output = bytearray()
    def makefile(self, *args, **kwargs):
        return io.BytesIO(self.raw)
    def sendall(self, data):
        self.output.extend(data)

class Loopback:
    def __init__(self, address, handler):
        self.handler = handler
        self.server_name, self.server_port = address
        self.calls = 0
        self.closed = False
        mark("listener")
    def __enter__(self):
        return self
    def __exit__(self, *args):
        self.server_close()
    def server_close(self):
        self.closed = True
        mark("closed")
    def handle_request(self):
        self.calls += 1
        assert self.calls == 1, "a rejected first request must finish the attempt"
        mark("listen", timeout=self.timeout, closed=self.closed)
        if settings["case"] == "no-request":
            mark("no request")
            return
        state = urllib.parse.parse_qs(urllib.parse.urlsplit(issued).query)["state"][0]
        private = os.environ["HTTP_PRIVATE"]
        path = "/?code=" + urllib.parse.quote(settings["code"], safe="") + "&state=" + state
        method, version = settings["method"], "HTTP/1.1"
        headers = "Host: localhost\r\nX-Synthetic: " + private + "\r\nConnection: close\r\n"
        kind = settings["case"]
        if kind == "space-target":
            path += " " + private
        elif kind == "bad-version":
            version = "HTTP/" + private
        elif kind == "future-version":
            version = "HTTP/2.0"
        elif kind == "long-request":
            path += "&padding=" + private + "x" * 65536
        elif kind == "long-header":
            headers += "X-Long: " + private + "x" * 65536 + "\r\n"
        elif kind == "many-headers":
            headers += "".join("X-Extra-" + str(i) + ": " + private + "\r\n" for i in range(101))
        elif kind == "invalid":
            path += "&state=" + private
        elif kind == "denied":
            path = "/?state=" + state + "&error=access_denied&error_description=" + private
        raw = (method + " " + path + " " + version + "\r\n" + headers +
               "Content-Length: " + str(len(private)) + "\r\n\r\n" + private).encode("ascii")
        request = RawRequest(raw)
        mark("raw request", bytes=len(raw), method=method, state=state)
        # Genuine constructor -> handle -> handle_one_request -> parse_request.
        # Observe entry to prove malformed cases never rely on calling do_GET.
        calls = []
        def profile(frame, event, arg):
            if event == "call" and frame.f_code.co_name in ("parse_request", "do_GET"):
                calls.append(frame.f_code.co_name)
        sys.setprofile(profile)
        try:
            self.handler(request, ("127.0.0.1", 43210), self)
        finally:
            sys.setprofile(None)
            mark("response", raw=bytes(request.output).decode("utf-8", errors="replace"),
                 calls=calls, output_exists=output.exists())

http.server.HTTPServer = Loopback
def exchange(request, *args, **kwargs):
    mark("exchange", body=request.data.decode("ascii"), timeout=kwargs.get("timeout"))
    if settings["exchange"] == "timeout":
        raise TimeoutError(os.environ["HTTP_PRIVATE"])
    return io.BytesIO(json.dumps({"refresh_token": os.environ["HTTP_TOKEN"]}).encode())
urllib.request.urlopen = exchange
def finish():
    mark("finished", module_file=sys.modules["ads_mcp.auth"].__file__)
atexit.register(finish)
'''


@dataclass
class Attempt:
    result: subprocess.CompletedProcess
    rows: list
    output: Path
    before: dict
    code: str

    def events(self, name):
        return [row for row in self.rows if row["event"] == name]

    @property
    def response(self):
        rows = self.events("response")
        assert len(rows) == 1
        return rows[0]["raw"]

    @property
    def body(self):
        # HTTP/0.9 errors may have body only; retaining it catches disclosure.
        return self.response.partition("\r\n\r\n")[2] if "\r\n\r\n" in self.response else self.response


def launch(tmp_path, *, case="valid", method="GET", existing=True,
           browser="success", exchange="success", code=PRIVATE):
    tmp_path.mkdir(parents=True, exist_ok=True)
    injection = tmp_path / "injection"
    injection.mkdir()
    (injection / "sitecustomize.py").write_text(INJECTION)
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    output = output_dir / "token.json"
    if existing:
        output.write_bytes(OLD)
        output.chmod(0o640)
    before = fingerprint(output_dir)
    credentials = tmp_path / "client.json"
    credentials.write_text(json.dumps({"installed": {"client_id": "synthetic-http.apps.example.org", "client_secret": SECRET}}))
    marker = tmp_path / "events.jsonl"
    env = h.scrubbed_env({"PYTHONPATH": str(injection), "PYTHONDONTWRITEBYTECODE": "1",
        "HTTP_SETTINGS": json.dumps({"case": case, "method": method, "browser": browser, "exchange": exchange, "code": code}),
        "HTTP_MARKER": str(marker), "HTTP_OUTPUT": str(output), "HTTP_PRIVATE": PRIVATE, "HTTP_TOKEN": TOKEN})
    result = subprocess.run([str(h.console_script("ads-mcp-generate-token")),
        "--credentials", str(credentials), "--token-path", str(output)],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=20)
    rows = [json.loads(line) for line in marker.read_text().splitlines()]
    assert rows[0]["event"] == "loaded" and rows[-1]["event"] == "finished"
    assert not any(row["event"] == "network attempted" for row in rows)
    assert rows[-1]["module_file"]
    return Attempt(result, rows, output, before, code)


def no_disclosure(attempt):
    # The issued consent URL intentionally contains public state and is printed
    # before any callback. Exclude precisely that URL; additional reflection of
    # state anywhere in HTTP bytes or terminal diagnostics remains forbidden.
    issued = attempt.events("browser")[0]["url"]
    public = (attempt.result.stdout.replace(issued, "<issued-consent-url>") +
              attempt.result.stderr + "".join(r["raw"] for r in attempt.events("response")))
    values = [PRIVATE, TOKEN, SECRET, attempt.code]
    values += [r["state"] for r in attempt.events("raw request")]
    for exchange in attempt.events("exchange"):
        values += urllib.parse.parse_qs(exchange["body"]).get("code_verifier", [])
    for value in values:
        assert value not in public and urllib.parse.quote(value, safe="") not in public, "raw HTTP/terminal reflection"
    assert "Traceback" not in public


def rejected(attempt, *, timeout=False):
    assert attempt.result.returncode != 0
    assert not attempt.events("exchange") and not attempt.events("output write attempted")
    assert fingerprint(attempt.output.parent) == attempt.before
    assert attempt.events("closed")
    assert len(attempt.events("listen")) == 1 and not attempt.events("listen")[0]["closed"]
    lines = [line for line in attempt.result.stderr.splitlines() if line.strip()]
    assert len(lines) == 1 and len(lines[0]) <= 600
    expected = "AUTH_CALLBACK_TIMEOUT" if timeout else "AUTH_CALLBACK_INVALID"
    assert lines[0].startswith(expected + ":"), lines[0]
    assert any(word in lines[0].lower() for word in ("retry", "rerun", "try again"))
    if not timeout:
        assert "timeout" not in lines[0].lower(), "a received rejection is not a no-request timeout"


PARSER_CASES = ["space-target", "bad-version", "future-version", "long-request", "long-header", "many-headers"]
METHODS = ["POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE", "CONNECT", PRIVATE]


@pytest.mark.parametrize("case", PARSER_CASES)
def test_raw_parser_failures_never_reflect_and_end_as_invalid(tmp_path, case):
    attempt = launch(tmp_path, case=case)
    assert "do_GET" not in attempt.events("response")[0]["calls"]
    if case != "long-request":
        assert "parse_request" in attempt.events("response")[0]["calls"]
    no_disclosure(attempt)
    rejected(attempt)
    # The stdlib may use HTTP/0.9 body-only framing when no valid version was
    # parsed. The contract does not require a status/protocol rewrite.
    if attempt.response.startswith("HTTP/"):
        assert re.match(r"HTTP/1\.[01] [45]\d\d [^\r\n]+\r\n", attempt.response)
    assert "invalid" in attempt.body.lower() and "terminal" in attempt.body.lower()
    assert len(attempt.response) <= 4096


@pytest.mark.parametrize("method", METHODS)
def test_unsupported_methods_are_static_invalid_callbacks(tmp_path, method):
    attempt = launch(tmp_path, method=method)
    assert "parse_request" in attempt.events("response")[0]["calls"]
    assert "do_GET" not in attempt.events("response")[0]["calls"]
    no_disclosure(attempt)
    rejected(attempt)
    assert re.match(r"HTTP/1\.[01] [45]\d\d [^\r\n]+\r\n", attempt.response)
    assert "invalid" in attempt.body.lower() and "terminal" in attempt.body.lower()
    assert len(attempt.response) <= 4096


def test_head_keeps_no_body_semantics_and_cannot_redeem(tmp_path):
    attempt = launch(tmp_path, method="HEAD")
    assert attempt.body == "", "HEAD must not emit a response body or redeem consent"
    no_disclosure(attempt)
    rejected(attempt)


def test_all_outer_errors_retain_the_existing_static_invalid_page(tmp_path):
    ordinary = launch(tmp_path / "ordinary", case="invalid")
    rejected(ordinary)
    for label, settings in [("parser", {"case": "space-target"}), ("method", {"method": "POST"})]:
        attempt = launch(tmp_path / label, **settings)
        assert attempt.body == ordinary.body, "outer errors must retain the bound static invalid-page appearance"
        no_disclosure(attempt)


def test_positive_control_no_request_is_the_only_timeout(tmp_path):
    attempt = launch(tmp_path, case="no-request")
    rejected(attempt, timeout=True)
    assert not attempt.events("response") and attempt.events("no request")
    assert attempt.events("listen")[0]["timeout"] > 0
    no_disclosure(attempt)


@pytest.mark.parametrize("browser", ["success", "false", "exception"])
def test_positive_control_raw_get_retains_pkce_encoded_code_and_private_storage(tmp_path, browser):
    code = "synthetic+code/with=encoding"
    attempt = launch(tmp_path, browser=browser, code=code, existing=False)
    assert attempt.result.returncode == 0, attempt.result.stderr
    assert json.loads(attempt.output.read_text()) == {"refresh_token": TOKEN}
    assert stat.S_IMODE(attempt.output.stat().st_mode) == 0o600
    assert attempt.events("closed") and len(attempt.events("exchange")) == 1
    calls = attempt.events("response")[0]["calls"]
    assert "parse_request" in calls and calls.count("do_GET") == 1
    assert "received" in attempt.body.lower() and "terminal" in attempt.body.lower()
    consent = urllib.parse.parse_qs(urllib.parse.urlsplit(attempt.events("browser")[0]["url"]).query)
    exchange = urllib.parse.parse_qs(attempt.events("exchange")[0]["body"])
    verifier = exchange["code_verifier"][0]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).decode().rstrip("=")
    assert consent["code_challenge"] == [challenge] and consent["code_challenge_method"] == ["S256"]
    assert exchange["code"] == [code] and exchange["client_secret"] == [SECRET]
    assert 0 < attempt.events("exchange")[0]["timeout"] <= 30
    assert not attempt.events("response")[0]["output_exists"]
    no_disclosure(attempt)


def test_positive_control_raw_denial_remains_distinct(tmp_path):
    attempt = launch(tmp_path, case="denied")
    assert attempt.result.returncode != 0 and "AUTH_CONSENT_DENIED:" in attempt.result.stderr
    assert "denied" in attempt.body.lower() and "terminal" in attempt.body.lower()
    assert not attempt.events("exchange") and fingerprint(attempt.output.parent) == attempt.before
    assert attempt.events("closed")
    no_disclosure(attempt)


def test_positive_control_exchange_timeout_preserves_output_and_pending_page(tmp_path):
    attempt = launch(tmp_path, exchange="timeout")
    assert attempt.result.returncode != 0 and "AUTH_EXCHANGE_UNAVAILABLE:" in attempt.result.stderr
    assert "received" in attempt.body.lower() and "terminal" in attempt.body.lower()
    assert len(attempt.events("exchange")) == 1 and not attempt.events("output write attempted")
    assert fingerprint(attempt.output.parent) == attempt.before and attempt.events("closed")
    no_disclosure(attempt)
