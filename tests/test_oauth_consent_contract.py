"""F046: installed OAuth consent binding, truthful status and bounded recovery.

The installed console runs outside the checkout in an isolated subprocess.
Only OS entropy observation and browser/listener/exchange transports are
instrumented. The real helper builds both protocol requests, handles callback
input, writes each response body and performs real token-file operations.
All socket operations are denied; no provider, private credential or browser
dependency is used. Tests explicitly named positive_control are expected green
on the pre-fix helper; the remaining tests discriminate F046 behavior.

``launch`` returns the actual handler bodies in ``callback`` observations, so
a separate runtime producer can render those bytes for the required eyes-on
verification. Text checks here do not satisfy that independent visual gate.
"""

import base64
from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import re
import stat
import subprocess
import urllib.parse

import pytest

import harness as h


CLIENT = "synthetic-consent-client.apps.example.org"
SECRET = "synthetic-consent-client-secret-DO-NOT-DISCLOSE"
TOKEN = "synthetic-consent-refresh-token-DO-NOT-DISCLOSE"
CODE = "synthetic-consent-code-DO-NOT-DISCLOSE"
DETAIL = "synthetic-consent-provider-detail-DO-NOT-DISCLOSE"
OLD = b'{"refresh_token":"synthetic-previous-user-token"}\n'
VALID = "/?state={state}&code={code}"
DENIED = "/?state={state}&error=access_denied&error_description={detail}"


INJECTION = r'''
import atexit
import http.server
import io
import json
import os
from pathlib import Path
import random
import socket
import sys
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

marker = Path(os.environ["CONSENT_MARKER"])
output = Path(os.environ["CONSENT_OUTPUT"])
settings = json.loads(os.environ["CONSENT_SETTINGS"])
def mark(event, **fields):
    with marker.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps({"event": event, **fields}) + "\n")
mark("loaded")
os.umask(0o022)
socket.setdefaulttimeout(settings["socket_default"])

def audit(event, args):
    # An optional urllib3 import may probe IPv6 availability; refuse it too.
    if event == "socket.bind" and args[1] == ("::1", 0):
        raise OSError("offline IPv6 capability probe")
    if event in ("socket.connect", "socket.bind", "socket.getaddrinfo"):
        mark("network attempted", operation=event)
        raise OSError("offline consent oracle forbids sockets")
    if event == "open" and isinstance(args[0], (str, bytes)):
        path = Path(os.fsdecode(args[0])).absolute()
        flags = args[2]
        if path.is_relative_to(output.parent) and isinstance(flags, int):
            if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC):
                mark("output write attempted", path=str(path))
sys.addaudithook(audit)

# Observe real OS-backed draws, including secrets/SystemRandom, token_hex,
# token_urlsafe, direct urandom and (where available) getrandom. Do not replace
# entropy with fixed values or require a particular high-level secrets API.
def observe_entropy(source):
    def draw(*args, **kwargs):
        value = source(*args, **kwargs)
        frame = sys._getframe(1)
        while frame is not None:
            if frame.f_globals.get("__name__", "").startswith("ads_mcp"):
                mark("os entropy", size=len(value))
                break
            frame = frame.f_back
        return value
    return draw
os.urandom = observe_entropy(os.urandom)
random._urandom = observe_entropy(random._urandom)
if hasattr(os, "getrandom"):
    os.getrandom = observe_entropy(os.getrandom)

original_setdefaulttimeout = socket.setdefaulttimeout
def setdefaulttimeout(value):
    mark("global timeout changed", value=value)
    return original_setdefaulttimeout(value)
socket.setdefaulttimeout = setdefaulttimeout

def finished():
    module = sys.modules.get("ads_mcp.auth")
    mark("finished", module_file=getattr(module, "__file__", None),
         socket_default=socket.getdefaulttimeout())
atexit.register(finished)

issued_url = None
def browser(url):
    global issued_url
    issued_url = url
    mark("browser", url=url)
    if settings["browser"] == "webbrowser-error":
        raise webbrowser.Error(os.environ["CONSENT_DETAIL"])
    if settings["browser"] == "os-error":
        raise OSError(os.environ["CONSENT_DETAIL"])
    return settings["browser"] != "false"

class Loopback:
    def __init__(self, address, handler):
        self.handler = handler
        self.calls = 0
        self.closed = False
        mark("listener", address=list(address))
    def __enter__(self):
        return self
    def __exit__(self, *args):
        self.server_close()
    def server_close(self):
        self.closed = True
        mark("listener closed")
    def handle_request(self):
        self.calls += 1
        mark("listen", timeout=getattr(self, "timeout", None), closed=self.closed)
        if self.calls > 1:
            raise AssertionError("the first invalid callback must terminate the attempt")
        if settings["callback"] is None:
            mark("no callback")
            return
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(issued_url).query)
        # Pre-fix positive controls remain reachable without inventing an
        # issued state. Strict binding tests separately reject its absence.
        state = query.get("state", [""])[0]
        substitutions = {
            "state": urllib.parse.quote(state, safe=""),
            "code": urllib.parse.quote(os.environ["CONSENT_CODE"], safe=""),
            "detail": urllib.parse.quote(os.environ["CONSENT_DETAIL"], safe=""),
        }
        path = settings["callback"]
        for key, value in substitutions.items():
            path = path.replace("{" + key + "}", value)
        handler = self.handler.__new__(self.handler)
        handler.path = path
        handler.wfile = io.BytesIO()
        handler.client_address = ("127.0.0.1", 12345)
        handler.requestline = "GET " + path + " HTTP/1.1"
        handler.request_version = "HTTP/1.1"
        handler.command = "GET"
        handler.server = self
        handler.headers = {}
        response = {"status": None, "headers": []}
        def status(code, *args):
            response["status"] = code
            # BaseHTTPRequestHandler.send_response normally invokes this;
            # preserve the production logging seam in our response recorder.
            handler.log_request(code)
        handler.send_response = status
        handler.send_header = lambda *args: response["headers"].append(list(args))
        handler.end_headers = lambda: None
        mark("callback entered", path=path)
        try:
            handler.do_GET()
        finally:
            mark("callback", body=handler.wfile.getvalue().decode("utf-8", errors="replace"),
                 output_exists=output.exists(), **response)

def exchange(request, *args, **kwargs):
    explicit = kwargs.get("timeout", args[0] if args else "not supplied")
    mark("exchange", url=request.full_url, method=request.get_method(),
         data=request.data.decode("ascii"), timeout=explicit,
         socket_default=socket.getdefaulttimeout(), output_exists=output.exists())
    if settings["exchange"] == "timeout":
        raise TimeoutError(os.environ["CONSENT_DETAIL"])
    if settings["exchange"] == "http":
        raise urllib.error.HTTPError(request.full_url, 400,
            os.environ["CONSENT_DETAIL"], {}, io.BytesIO(b"synthetic rejection"))
    return io.BytesIO(json.dumps({"refresh_token": os.environ["CONSENT_TOKEN"],
                                 "access_token": os.environ["CONSENT_DETAIL"]}).encode())

webbrowser.open = browser
http.server.HTTPServer = Loopback
urllib.request.urlopen = exchange
'''


def fingerprint(directory):
    """Semantic file state, excluding unstable filesystem directory sizes."""
    return {
        str(path.relative_to(directory)): (
            stat.S_IFMT(path.lstat().st_mode), stat.S_IMODE(path.lstat().st_mode),
            path.read_bytes() if path.is_file() else None,
        )
        for path in [directory, *sorted(directory.rglob("*"))]
    }


@dataclass
class Attempt:
    result: subprocess.CompletedProcess
    output: Path
    observations: list
    before: dict
    code: str

    def events(self, name):
        return [row for row in self.observations if row["event"] == name]

    @property
    def consent(self):
        urls = self.events("browser")
        assert len(urls) == 1, "exactly one consent URL must be issued"
        return urllib.parse.parse_qs(urllib.parse.urlsplit(urls[0]["url"]).query,
                                    keep_blank_values=True, strict_parsing=True)

    @property
    def exchange(self):
        rows = self.events("exchange")
        assert len(rows) == 1, "a valid callback exchanges once, without automatic retry"
        return urllib.parse.parse_qs(rows[0]["data"], keep_blank_values=True,
                                    strict_parsing=True)


def launch(tmp_path, *, callback=VALID, browser="success", exchange="success",
           existing=False, code=CODE, socket_default=None):
    """Run the real installed console; bodies are available in returned events."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    injection = tmp_path / "injection"
    injection.mkdir()
    (injection / "sitecustomize.py").write_text(INJECTION)
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    output = output_dir / "refresh.json"
    if existing:
        output.write_bytes(OLD)
        output.chmod(0o640)
    before = fingerprint(output_dir)
    credentials = tmp_path / "synthetic-client.json"
    credentials.write_text(json.dumps({"installed": {
        "client_id": CLIENT, "client_secret": SECRET}}))
    marker = tmp_path / "observations.jsonl"
    settings = {"callback": callback, "browser": browser, "exchange": exchange,
                "socket_default": socket_default}
    env = h.scrubbed_env({
        "PYTHONPATH": str(injection), "PYTHONDONTWRITEBYTECODE": "1",
        "CONSENT_MARKER": str(marker), "CONSENT_OUTPUT": str(output),
        "CONSENT_SETTINGS": json.dumps(settings), "CONSENT_CODE": code,
        "CONSENT_SECRET": SECRET, "CONSENT_TOKEN": TOKEN, "CONSENT_DETAIL": DETAIL,
    })
    command = [str(h.console_script("ads-mcp-generate-token")),
               "--credentials", str(credentials), "--token-path", str(output)]
    result = subprocess.run(command, cwd=tmp_path, env=env, text=True,
                            capture_output=True, timeout=20)
    rows = [json.loads(line) for line in marker.read_text().splitlines()]
    assert rows[0]["event"] == "loaded", "installed helper must load the offline transport"
    assert not any(row["event"] == "network attempted" for row in rows)
    assert rows[-1]["event"] == "finished" and rows[-1]["module_file"], (
        "the installed entry point must execute the actual OAuth module")
    return Attempt(result, output, rows, before, code)


def diagnostics(attempt):
    public = attempt.result.stdout + attempt.result.stderr
    public += "\n".join(row["body"] for row in attempt.events("callback"))
    assert "Traceback" not in public, attempt.result.stderr
    private = [SECRET, TOKEN, attempt.code, DETAIL, OLD.decode().strip()]
    for row in attempt.events("exchange"):
        private.extend(urllib.parse.parse_qs(row["data"]).get("code_verifier", []))
    for value in private:
        if value:
            assert value not in public, "terminal or browser output leaked secret/query material"
            assert urllib.parse.quote(value, safe="") not in public, "encoded secret leaked"


def unsuccessful(attempt, *, meaning):
    assert attempt.result.returncode != 0, "invalid consent incorrectly completed token storage"
    diagnostics(attempt)
    lines = [line for line in attempt.result.stderr.splitlines() if line.strip()]
    assert len(lines) == 1 and len(lines[0]) <= 600, attempt.result.stderr
    match = re.match(r"(?:[a-zA-Z0-9_.-]+: )?([A-Z][A-Z0-9_]+):", lines[0])
    assert match, "callback failures need a named, concise unsuccessful outcome"
    assert any(word in lines[0].lower() for word in meaning), lines[0]
    assert any(word in lines[0].lower() for word in ("retry", "rerun", "try again", "run again")), (
        "unsuccessful consent needs explicit retry guidance")
    return match[1]


def unchanged_output(attempt):
    assert not attempt.events("output write attempted"), "refused callbacks must precede file writes"
    assert fingerprint(attempt.output.parent) == attempt.before, "failure changed existing output"


def closed_listener(attempt):
    assert len(attempt.events("listener")) == 1
    assert attempt.events("listener closed"), "terminal outcome leaked its loopback listener"
    listens = attempt.events("listen")
    assert len(listens) == 1, "one pending listener must handle only the first callback"
    assert listens[0]["closed"] is False, "manual URL cannot target an already closed listener"


def successful_storage(attempt):
    assert attempt.result.returncode == 0, attempt.result.stderr
    assert json.loads(attempt.output.read_text()) == {"refresh_token": TOKEN}
    assert stat.S_IMODE(attempt.output.stat().st_mode) == 0o600
    assert len(attempt.events("exchange")) == 1
    diagnostics(attempt)


def response_body(attempt):
    responses = attempt.events("callback")
    assert len(responses) == 1, "the actual callback handler must produce a response"
    response = responses[0]
    assert isinstance(response["status"], int) and 200 <= response["status"] < 500
    headers = {name.lower(): value for name, value in response["headers"]}
    assert headers.get("content-type", "").lower().startswith("text/html")
    body = response["body"]
    assert 1 <= len(body.encode()) <= 4096, "retain a small static callback message"
    assert not re.search(r"\bauthorized[.!<]|successfully\s+(?:authorized|saved|stored)", body, re.I), (
        "callback HTML must not claim completed authorization or storage")
    diagnostics(attempt)
    return body


def test_two_attempts_bind_actual_consent_and_exchange_with_independent_s256(tmp_path):
    bindings = []
    for number in range(2):
        attempt = launch(tmp_path / str(number))
        successful_storage(attempt)
        consent, exchange = attempt.consent, attempt.exchange
        assert "state" in consent and len(consent["state"]) == 1 and consent["state"][0], (
            "each installed-helper consent URL needs a nonempty issued state")
        assert "code_verifier" in exchange and len(exchange["code_verifier"]) == 1
        state, verifier = consent["state"][0], exchange["code_verifier"][0]
        assert re.fullmatch(r"[A-Za-z0-9._~-]{43,128}", verifier), "RFC7636 verifier grammar"
        assert state != verifier, "the private verifier must be independent of public state"
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).decode().rstrip("=")
        assert consent.get("code_challenge") == [challenge], "independently recompute S256"
        assert consent.get("code_challenge_method") == ["S256"]
        assert "code_verifier" not in consent and "code" not in consent and "client_secret" not in consent
        assert exchange["code"] == [CODE] and exchange["client_secret"] == [SECRET]
        assert exchange["client_id"] == consent["client_id"] == [CLIENT]
        assert exchange["redirect_uri"] == consent["redirect_uri"]
        assert exchange["grant_type"] == ["authorization_code"]
        assert consent["response_type"] == ["code"] and consent["access_type"] == ["offline"]
        assert consent["prompt"] == ["consent"]
        assert consent["scope"] == ["https://www.googleapis.com/auth/adwords"]
        assert attempt.events("exchange")[0]["url"] == "https://oauth2.googleapis.com/token"
        assert attempt.events("exchange")[0]["method"] == "POST"
        bindings.append((state, verifier, challenge))
    assert all(bindings[0][i] != bindings[1][i] for i in range(3)), "later consent must use fresh bindings"
    second = launch(tmp_path / "stale-state", callback="/?state=" + urllib.parse.quote(bindings[0][0], safe="") + "&code={code}", existing=True)
    unsuccessful(second, meaning=("invalid", "state", "callback", "correlat"))
    assert not second.events("exchange"), "a previous attempt's state cannot authorize this attempt"
    unchanged_output(second)
    closed_listener(second)


def test_bindings_obtain_at_least_512_bits_of_real_os_entropy_per_attempt(tmp_path):
    # OS bytes establish the source and minimum combined entropy budget;
    # the independent binding/freshness test checks separate public/private
    # values. Source review still verifies allocation of >=256 bits to each;
    # finite black-box samples cannot mathematically prove entropy allocation.
    for number in range(2):
        attempt = launch(tmp_path / str(number))
        draws = attempt.events("os entropy")
        assert sum(row["size"] for row in draws) >= 64, (
            "state and verifier together require at least 512 bits from OS-backed cryptographic entropy")


INVALID_CALLBACKS = [
    pytest.param("/?code={code}", id="missing-state"),
    pytest.param("/?state=not-issued-by-helper&code={code}", id="wrong-state"),
    pytest.param("/?state={state}&state={state}&code={code}", id="duplicate-same-state"),
    pytest.param("/?state={state}&state=foreign&code={code}", id="duplicate-different-state"),
    pytest.param("/?state={state}&%73tate={state}&code={code}", id="encoded-duplicate-state-key"),
    pytest.param("/?state=&code={code}", id="blank-state"),
    pytest.param("/?state&code={code}", id="valueless-state"),
    pytest.param("/?state=%20{state}&code={code}", id="space-state"),
    pytest.param("/?state={state}%00&code={code}", id="control-state"),
    pytest.param("/?state={state}%ZZ&code={code}", id="bad-percent-state"),
    pytest.param("/?state={state}%FF&code={code}", id="bad-utf8-state"),
    pytest.param("/?state={state}&code={code}&code={code}", id="duplicate-same-code"),
    pytest.param("/?state={state}&code={code}&code=foreign", id="duplicate-different-code"),
    pytest.param("/?state={state}&code={code}&%63ode=foreign", id="encoded-duplicate-code-key"),
    pytest.param("/?state={state}&code=", id="blank-code"),
    pytest.param("/?state={state}&code", id="valueless-code"),
    pytest.param("/?state={state}", id="missing-code-and-error"),
    pytest.param("/?state={state}&code={code}%20", id="space-code"),
    pytest.param("/?state={state}&code={code}+suffix", id="plus-space-code"),
    pytest.param("/?state={state}&code={code}%09", id="tab-code"),
    pytest.param("/?state={state}&code={code}%0A", id="newline-code"),
    pytest.param("/?state={state}&code={code}%00", id="nul-code"),
    pytest.param("/?state={state}&code={code}%7F", id="delete-control-code"),
    pytest.param("/?state={state}&code={code}%C2%85", id="unicode-control-code"),
    pytest.param("/?state={state}&code={code}%E2%80%83", id="unicode-space-code"),
    pytest.param("/?state={state}&code={code}%", id="truncated-percent"),
    pytest.param("/?state={state}&code={code}%2", id="short-percent"),
    pytest.param("/?state={state}&code={code}%GG", id="nonhex-percent"),
    pytest.param("/?state={state}&code={code}%FF", id="invalid-utf8"),
    pytest.param("/?state={state}&code={code}%C0%AF", id="overlong-utf8"),
    pytest.param("/?state={state}&code={code}%E2%82", id="truncated-utf8"),
    pytest.param("/?state={state}&code={code}%ED%A0%80", id="encoded-surrogate"),
    pytest.param("/?state={state}&code={code}\ud800", id="malformed-unicode"),
    pytest.param("/?state={state}&code={code}&error=access_denied", id="mixed-code-error"),
    pytest.param("/?state={state}&code={code}&error=", id="mixed-code-blank-error"),
    pytest.param("/?state={state}&error=", id="blank-error"),
    pytest.param("/?state={state}&error", id="valueless-error"),
    pytest.param("/?state={state}&error=access_denied&error=access_denied", id="duplicate-error"),
    pytest.param("/?state={state}&error=access_denied&%65rror=foreign", id="encoded-duplicate-error"),
    pytest.param("/?state=foreign&error=access_denied", id="uncorrelated-denial"),
    pytest.param("/?error=access_denied", id="denial-without-state"),
    pytest.param("/?state={state}&error=access_denied%GG", id="malformed-error"),
    pytest.param("/wrong?state={state}&code={code}", id="wrong-path"),
    pytest.param("/favicon.ico?state={state}&code={code}", id="favicon-path"),
    pytest.param("/%2F?state={state}&code={code}", id="encoded-wrong-path"),
]


@pytest.mark.parametrize("callback", INVALID_CALLBACKS)
@pytest.mark.parametrize("existing", [False, True], ids=["new-file", "existing-file"])
def test_invalid_first_callback_refuses_before_exchange_or_output(tmp_path, callback, existing):
    attempt = launch(tmp_path, callback=callback, existing=existing)
    unsuccessful(attempt, meaning=("invalid", "state", "callback", "malformed", "correlat"))
    assert not attempt.events("exchange"), "invalid input reached the actual token exchange"
    unchanged_output(attempt)
    closed_listener(attempt)
    body = response_body(attempt).lower()
    assert any(word in body for word in ("invalid", "rejected", "not valid", "could not")), body


@pytest.mark.parametrize("existing", [False, True])
def test_correlated_provider_denial_has_named_unsuccessful_outcome(tmp_path, existing):
    attempt = launch(tmp_path, callback=DENIED, existing=existing)
    unsuccessful(attempt, meaning=("denied", "denial", "declined", "cancel"))
    assert not attempt.events("exchange")
    unchanged_output(attempt)
    closed_listener(attempt)
    body = response_body(attempt).lower()
    assert any(word in body for word in ("denied", "declined", "cancel")), body


@pytest.mark.parametrize("existing", [False, True])
def test_no_callback_timeout_has_named_retry_outcome_and_closes_listener(tmp_path, existing):
    attempt = launch(tmp_path, callback=None, existing=existing)
    unsuccessful(attempt, meaning=("timeout", "timed out", "no callback", "no response"))
    assert not attempt.events("exchange") and not attempt.events("callback")
    timeout = attempt.events("listen")[0]["timeout"]
    assert isinstance(timeout, (float, int)) and math.isfinite(timeout) and timeout > 0
    unchanged_output(attempt)
    closed_listener(attempt)


@pytest.mark.parametrize("exchange", ["success", "timeout", "http"])
def test_valid_callback_page_reports_received_pending_before_exchange_result(tmp_path, exchange):
    attempt = launch(tmp_path, exchange=exchange)
    body = response_body(attempt).lower()
    assert "received" in body and "terminal" in body, (
        "valid callback must say consent was received and direct the user to the terminal")
    assert any(word in body for word in ("pending", "exchang", "sav", "finish", "complet"))
    assert attempt.observations.index(attempt.events("callback")[0]) < attempt.observations.index(attempt.events("exchange")[0])
    assert not attempt.events("callback")[0]["output_exists"], "response precedes token creation"
    closed_listener(attempt)


@pytest.mark.parametrize("kind", ["received", "denied", "invalid"])
def test_actual_browser_bodies_are_static_and_do_not_reflect_query_values(tmp_path, kind):
    bodies = []
    for number in range(2):
        injected = urllib.parse.quote(f"<script>synthetic-query-{number}</script>", safe="")
        if kind == "received":
            callback = VALID + "&ignored=" + injected
        elif kind == "denied":
            callback = "/?state={state}&error=" + injected
        else:
            callback = "/?state=" + injected + "&code={code}"
        attempt = launch(tmp_path / str(number), callback=callback, code=CODE + str(number))
        body = response_body(attempt)
        assert "synthetic-query-" not in body and "<script" not in body.lower()
        bodies.append(body)
    assert bodies[0] == bodies[1], "callback status must use static text, not escaped reflection"


@pytest.mark.parametrize("browser", ["false", "webbrowser-error", "os-error"])
def test_browser_failure_keeps_same_manual_consent_url_usable(tmp_path, browser):
    attempt = launch(tmp_path, browser=browser)
    successful_storage(attempt)
    text = (attempt.result.stdout + attempt.result.stderr).lower()
    assert "browser" in text and any(word in text for word in ("manual", "copy", "paste", "could not", "unable")), (
        "browser failure needs concise manual-opening guidance")
    url = attempt.events("browser")[0]["url"]
    printed = re.findall(r"https://accounts\.google\.com/[^\s]+", attempt.result.stdout)
    assert printed and set(printed) == {url}, "fallback must keep the same issued consent URL"
    consent = attempt.consent
    assert len(consent.get("state", [])) == 1 and consent["state"][0]
    assert len(attempt.events("callback entered")) == 1
    closed_listener(attempt)


@pytest.mark.parametrize("socket_default", [None, 7.25])
def test_exchange_sets_explicit_finite_timeout_without_global_socket_changes(tmp_path, socket_default):
    attempt = launch(tmp_path, socket_default=socket_default)
    successful_storage(attempt)
    exchange = attempt.events("exchange")[0]
    timeout = exchange["timeout"]
    assert isinstance(timeout, (int, float)) and not isinstance(timeout, bool), (
        "actual urllib exchange must receive an explicit numeric socket timeout")
    assert math.isfinite(timeout) and 0 < timeout <= 30
    assert exchange["socket_default"] == socket_default
    assert attempt.events("finished")[0]["socket_default"] == socket_default
    assert not attempt.events("global timeout changed")


@pytest.mark.parametrize("existing", [False, True])
def test_exchange_timeout_is_named_sanitized_unretried_and_releases_listener(tmp_path, existing):
    attempt = launch(tmp_path, exchange="timeout", existing=existing)
    assert unsuccessful(attempt, meaning=("network", "connection", "exchange")) == "AUTH_EXCHANGE_UNAVAILABLE"
    assert len(attempt.events("exchange")) == 1, "timeout must not trigger automatic exchange retry"
    unchanged_output(attempt)
    closed_listener(attempt)


@pytest.mark.parametrize("callback,code", [
    pytest.param(VALID, CODE, id="root-path"),
    pytest.param("?state={state}&code={code}", CODE, id="equivalent-empty-path"),
    pytest.param("/?%73tate={state}&%63ode={code}", CODE + "/A+B=é", id="valid-percent-encoding"),
])
def test_positive_control_valid_encoded_callback_preserves_exact_code_and_private_storage(tmp_path, callback, code):
    attempt = launch(tmp_path, callback=callback, code=code)
    successful_storage(attempt)
    assert attempt.exchange["code"] == [code]
    assert attempt.exchange["redirect_uri"] == attempt.consent["redirect_uri"]
    redirect = urllib.parse.urlsplit(attempt.consent["redirect_uri"][0])
    assert redirect.scheme == "http" and redirect.hostname in ("localhost", "127.0.0.1")
    assert redirect.port == 8085 and redirect.path in ("", "/")
    assert attempt.events("listener")[0]["address"] == ["127.0.0.1", 8085]


def test_positive_control_existing_named_timeout_recovery_preserves_previous_token(tmp_path):
    attempt = launch(tmp_path, exchange="timeout", existing=True)
    assert unsuccessful(attempt, meaning=("network", "connection", "exchange")) == "AUTH_EXCHANGE_UNAVAILABLE"
    assert len(attempt.events("exchange")) == 1
    unchanged_output(attempt)
