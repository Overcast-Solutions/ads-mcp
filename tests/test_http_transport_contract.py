"""F091: local, read-only, stateless streamable HTTP for the installed console.

Every test drives the installed ``ads-mcp`` console. The child loads a
sitecustomize that substitutes only the Google Ads client factory (harness
transport) and wraps socket.bind: it records requested addresses, maps any
127.0.0.0/8 loopback alias onto 127.0.0.1 (hosts often lack aliases), can
rewrite a fixed port to an ephemeral one, and can simulate bind faults.
Outbound connections from the server are refused. Library IPv6 capability
probes bind ("::1", 0) without listening; serving is observed through listen().

Contract pinned here: the bound URL line and the warning line are consecutive
stderr lines; all pre-SDK 400 responses share one fixed body.
"""
import http.client
import json
import re
import signal
import socket
import subprocess
import sys
import threading
import tomllib
from pathlib import Path

import pytest

import harness as h

ROOT = Path(__file__).resolve().parents[1]
SERVING = re.compile(r"^ads-mcp: serving MCP over HTTP at http://(?P<host>\S+):(?P<port>\d+)/mcp \(read-only\)$")
MIB = 1024 * 1024
PROTOCOL = "2025-06-18"
DEFAULT_ARGS = ("--transport", "http", "--port", "0")
MARK = re.compile(rb"ORACLE_MARK_[A-Z]+")

INJECTION = r'''
import errno, json, os, socket, sys, threading
sys.path.insert(0, os.environ["ORACLE_TESTS"])
_lock = threading.Lock()
def mark(event, **fields):
    with _lock, open(os.environ["ORACLE_MARKER"], "a") as stream:
        stream.write(json.dumps({"event": event, **fields}) + "\n")
def _audit(event, args):
    if event == "socket.connect":
        mark("outbound connect")
        raise OSError("HTTP transport oracle forbids outbound connections")
    if event == "socket.getaddrinfo":
        mark("getaddrinfo", host=str(args[0]))
sys.addaudithook(_audit)
_bind = socket.socket.bind
def _oracle_bind(self, address):
    shown = list(address[:2]) if isinstance(address, tuple) else str(address)
    mark("bind", family=int(self.family), address=shown)
    fault = os.environ.get("ORACLE_BIND_FAULT")
    if fault == "in-use":
        raise OSError(errno.EADDRINUSE, os.strerror(errno.EADDRINUSE))
    if fault == "denied":
        raise PermissionError(errno.EACCES, os.strerror(errno.EACCES))
    if isinstance(address, tuple) and self.family == socket.AF_INET:
        host, port = address[0], address[1]
        if isinstance(host, str) and host.startswith("127."):
            host = "127.0.0.1"
        if os.environ.get("ORACLE_BIND_ANY_PORT") == "1":
            port = 0
        address = (host, port, *address[2:])
    return _bind(self, address)
socket.socket.bind = _oracle_bind
_listen = socket.socket.listen
def _oracle_listen(self, *args):
    result = _listen(self, *args)
    name = self.getsockname()
    mark("listen", family=int(self.family), address=list(name[:2]) if isinstance(name, tuple) else str(name))
    return result
socket.socket.listen = _oracle_listen
import harness as h
from google.ads.googleads.client import GoogleAdsClient
from google.auth.exceptions import RefreshError
control = os.environ["ORACLE_CONTROL"]
class _Transport(h.FakeGoogleAdsClient):
    def _do_search(self, service, method, args, kwargs):
        rows = super()._do_search(service, method, args, kwargs)
        call = self.searches[-1]
        mark("search", customer_id=call.customer_id, query=call.query)
        mode = json.loads(open(control).read() or "{}")
        if mode.get("fault") == "revoked":
            raise RefreshError("invalid_grant " + h.FAKE_REFRESH_TOKEN)
        return rows
_transport = h.stub_standard_account(_Transport())
GoogleAdsClient.load_from_dict = lambda *args, **kwargs: _transport
mark("loaded")
'''


def oracle_env(root, read_only="true", **overlay):
    root = Path(root)
    injection = root / "injection"
    injection.mkdir(parents=True, exist_ok=True)
    (injection / "sitecustomize.py").write_text(INJECTION)
    marker, control = root / "http-events.jsonl", root / "http-control.json"
    control.write_text("{}")
    env = h.google_ads_env(root)
    env.update({"PYTHONPATH": str(injection), "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1",
                "ORACLE_TESTS": str(ROOT / "tests"), "ORACLE_MARKER": str(marker), "ORACLE_CONTROL": str(control),
                "ADS_MCP_AUDIT_LOG": str(h.audit_file(root)), "ADS_MCP_RETRY_BASE_SECONDS": "0.001",
                "HOME": str(root)})
    if read_only is not None:
        env["ADS_MCP_READ_ONLY"] = read_only
    env.update({key: value for key, value in overlay.items() if value is not None})
    for key in [key for key, value in overlay.items() if value is None]:
        env.pop(key, None)
    return env, marker, control


PROBE = ["::1", 0]


def server_binds(marker):
    """Requested bind addresses, excluding the urllib3 IPv6 capability probe."""
    return [item for item in events(marker, "bind") if item["address"] != PROBE]


def events(marker, kind=None):
    if not Path(marker).exists():
        return []
    items = [json.loads(line) for line in Path(marker).read_text().splitlines() if line.strip()]
    return [item for item in items if kind is None or item["event"] == kind]


class HTTPServer:
    """The installed console serving HTTP, with stderr drained continuously."""

    def __init__(self, root, args=DEFAULT_ARGS, **env_overlay):
        self.env, self.marker, self.control = oracle_env(root, **env_overlay)
        self.process = subprocess.Popen([str(h.console_script()), *args], env=h.scrubbed_env(self.env),
                                        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                        text=True, bufsize=1)
        self.lines, self.out = [], []
        self.ready = threading.Event()
        self.readers = [threading.Thread(target=self._drain, args=(self.process.stderr, self.lines), daemon=True),
                        threading.Thread(target=self._drain, args=(self.process.stdout, self.out), daemon=True)]
        for reader in self.readers:
            reader.start()

    def _drain(self, stream, sink):
        for line in stream:
            sink.append(line.rstrip("\n"))
            if SERVING.match(line.rstrip("\n")):
                self.ready.set()
        self.ready.set()

    def wait_ready(self, seconds=30):
        self.ready.wait(seconds)
        match = next((SERVING.match(line) for line in self.lines if SERVING.match(line)), None)
        assert match, f"no serving line (exit={self.process.poll()}): {self.lines[-5:]}"
        self.host, self.port = match.group("host"), int(match.group("port"))
        return self

    def finish(self, sig=signal.SIGTERM, timeout=15):
        if self.process.poll() is None:
            self.process.send_signal(sig)
        try:
            code = self.process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.process.kill()
            code = self.process.wait()
            raise AssertionError("HTTP server did not stop on signal")
        finally:
            for reader in self.readers:
                reader.join(timeout=5)
            for stream in (self.process.stdout, self.process.stderr):
                stream.close()
        return code

    def stderr(self):
        return "\n".join(self.lines)

    def request(self, body, *, host=None, origin=None, headers=None, chunks=None, method="POST", path="/mcp"):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        sent = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                "Host": host if host is not None else f"127.0.0.1:{self.port}", "MCP-Protocol-Version": PROTOCOL}
        if origin is not None:
            sent["Origin"] = origin
        sent.update(headers or {})
        try:
            try:
                if chunks is not None:
                    connection.request(method, path, body=iter(chunks), headers=sent, encode_chunked=True)
                else:
                    connection.request(method, path, body=body, headers=sent)
            except (BrokenPipeError, ConnectionResetError):
                pass  # an early refusal may close before the whole body is sent
            response = connection.getresponse()
            return response.status, {k.lower(): v for k, v in response.getheaders()}, response.read()
        finally:
            connection.close()

    def rpc(self, method, params=None, *, ident=1, **kwargs):
        frame = {"jsonrpc": "2.0", "method": method}
        if ident is not None:
            frame["id"] = ident
        if params is not None:
            frame["params"] = params
        status, headers, body = self.request(json.dumps(frame).encode(), **kwargs)
        return status, headers, decode(headers, body)


def decode(headers, body):
    if not body:
        return None
    if headers.get("content-type", "").startswith("text/event-stream"):
        messages = [json.loads(line[5:].strip()) for line in body.decode().splitlines()
                    if line.startswith("data:") and line[5:].strip()]
        return messages[-1] if messages else None
    return json.loads(body)


def initialize(server):
    status, _, message = server.rpc("initialize", {"protocolVersion": PROTOCOL, "capabilities": {},
                                                    "clientInfo": {"name": "http-oracle", "version": "0"}})
    assert status == 200 and "serverInfo" in message["result"], (status, message)
    status, _, _ = server.rpc("notifications/initialized", ident=None)
    assert status == 202, status
    return message


def tool_payload(message):
    result = message["result"]
    assert not result.get("isError"), result
    value = result.get("structuredContent")
    if value is None:
        value = json.loads("".join(part.get("text", "") for part in result["content"]))
    return value.get("result", value) if set(value) == {"result"} else value


def ipv6_loopback_available():
    try:
        with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as probe:
            probe.bind(("::1", 0))
        return True
    except OSError:
        return False


@pytest.fixture
def started():
    servers = []

    def start(root, args=None, **env):
        server = HTTPServer(root, tuple(args) if args else DEFAULT_ARGS, **env)
        servers.append(server)
        return server

    yield start
    for server in servers:
        if server.process.poll() is None:
            server.process.kill()
            server.process.wait()
        for stream in (server.process.stdout, server.process.stderr):
            if not stream.closed:
                stream.close()


def run_cli(root, args, timeout=30, **overlay):
    env, marker, _ = oracle_env(root, **overlay)
    proc = subprocess.run([str(h.console_script()), *args], env=h.scrubbed_env(env), stdin=subprocess.DEVNULL,
                          capture_output=True, text=True, timeout=timeout)
    return proc, marker


def assert_usage_error(proc, marker, option):
    output = proc.stdout + proc.stderr
    assert proc.returncode == 2, (proc.returncode, output)
    assert option in proc.stderr, f"the refusal must name {option}: {proc.stderr!r}"
    assert "unrecognized arguments" not in proc.stderr, f"{option} is not a recognized option: {proc.stderr!r}"
    assert "Traceback" not in output and not SERVING.search(output)
    assert not server_binds(marker) and not events(marker, "listen"), "nothing may be bound for a refused invocation"


# ---------------------------------------------------------------------------
# Serving, protocol and parity with stdio.


def test_installed_http_serves_initialize_tools_list_and_reads_read_only(tmp_path, started):
    server = started(tmp_path).wait_ready()
    lines = [line for line in server.lines if line.strip()]
    index = next(i for i, line in enumerate(lines) if SERVING.match(line))
    assert server.host in ("127.0.0.1", "localhost", "[::1]") and 0 < server.port < 65536
    assert lines[index] == f"ads-mcp: serving MCP over HTTP at http://{server.host}:{server.port}/mcp (read-only)"
    warning = lines[index + 1].lower() if index + 1 < len(lines) else ""
    assert "local process" in warning and "read" in warning and "account" in warning, lines
    initialize(server)
    status, _, listing = server.rpc("tools/list", ident=2)
    assert status == 200
    names = {tool["name"] for tool in listing["result"]["tools"]}
    assert "health_check" in names and "confirm_and_apply" not in names and "pause_entity" not in names
    status, _, reply = server.rpc("tools/call", {"name": "get_account_info", "arguments": {}}, ident=3)
    assert status == 200 and tool_payload(reply)["customer_id"] == h.CUSTOMER_ID, reply
    status, _, reply = server.rpc("tools/call", {"name": "run_gaql", "arguments": {
        "query": "SELECT customer.id FROM customer", "customer_id": h.OTHER_CUSTOMER_ID}}, ident="string-id")
    assert status == 200 and reply["id"] == "string-id" and "rows" in tool_payload(reply)
    assert {item["customer_id"] for item in events(server.marker, "search")} == {h.CUSTOMER_ID, h.OTHER_CUSTOMER_ID}
    assert not events(server.marker, "outbound connect")
    assert server.finish() == 0
    text = server.stderr()
    assert "Traceback" not in text and "POST /mcp" not in text and "HTTP/1.1" not in text
    h.assert_no_secrets(text + "\n".join(server.out))


def test_http_tool_catalog_and_schemas_match_stdio_exactly(tmp_path, started):
    server = started(tmp_path / "http").wait_ready()
    initialize(server)
    _, _, listing = server.rpc("tools/list", ident=2)
    http_tools = {tool["name"]: tool["inputSchema"] for tool in listing["result"]["tools"]}
    assert server.finish() == 0
    env, _, _ = oracle_env(tmp_path / "stdio")
    proc, messages = h.stdio_tools_list(env)
    stdio_tools = {tool["name"]: tool["inputSchema"] for tool in
                   next(m for m in messages if m.get("id") == 2)["result"]["tools"]}
    assert http_tools == stdio_tools


def test_http_audit_behavior_matches_stdio(tmp_path, started):
    def strip(record):
        return {key: value for key, value in record.items() if key not in ("ts", "timestamp", "time")}

    server = started(tmp_path / "http").wait_ready()
    server.control.write_text(json.dumps({"fault": "revoked"}))
    initialize(server)
    _, _, health = server.rpc("tools/call", {"name": "health_check", "arguments": {}}, ident=2)
    assert tool_payload(health)["status"] == "AUTH_DEAD"
    assert server.finish() == 0
    http_records = [strip(r) for r in h.read_audit_records(tmp_path / "http")]
    env, _, control = oracle_env(tmp_path / "stdio")
    control.write_text(json.dumps({"fault": "revoked"}))
    frames = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": PROTOCOL, "capabilities": {}, "clientInfo": {"name": "stdio-oracle", "version": "0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "health_check", "arguments": {}}}]
    h._run_console_holding_pipe("ads-mcp", env_overlay=env, input_text="".join(json.dumps(f) + "\n" for f in frames))
    stdio_records = [strip(r) for r in h.read_audit_records(tmp_path / "stdio")]
    assert http_records == stdio_records and any(r.get("event") == "auth_failure" for r in http_records)
    h.assert_no_secrets(h.audit_file(tmp_path / "http").read_text())


# ---------------------------------------------------------------------------
# Read-only and CLI validation.


@pytest.mark.parametrize("value", ["false", "0", "no", "off", "FALSE"])
def test_http_refuses_unless_effectively_read_only_and_binds_nothing(tmp_path, value):
    proc, marker = run_cli(tmp_path, ["--transport", "http", "--port", "0"], read_only=value,
                           ADS_MCP_MAX_DAILY_BUDGET="100", ADS_MCP_MAX_BID_INCREASE_PCT="10", ADS_MCP_MAX_FIRST_BID="5")
    output = proc.stdout + proc.stderr
    assert proc.returncode == 2, (proc.returncode, output)
    assert "read-only" in proc.stderr.lower() or "ADS_MCP_READ_ONLY" in proc.stderr, proc.stderr
    assert "unrecognized arguments" not in proc.stderr
    assert not SERVING.search(output) and "Traceback" not in output
    assert not server_binds(marker) and not events(marker, "listen"), "a refused HTTP start must not bind"
    lines = [line for line in proc.stderr.splitlines() if line.strip()]
    assert len(lines) == 1, lines


@pytest.mark.parametrize("value", [None, "true", "1", "banana", ""])
def test_http_serves_whenever_the_effective_configuration_is_read_only(tmp_path, started, value):
    server = started(tmp_path, read_only=value).wait_ready()
    initialize(server)
    assert server.finish() == 0


@pytest.mark.parametrize("args,option", [
    (["--transport", "sse"], "--transport"), (["--transport", "streamable-http"], "--transport"),
    (["--transport", "websocket", "--port", "0"], "--transport"), (["--transport", ""], "--transport"),
    (["--transport", "http2"], "--transport"),
    (["--host", "127.0.0.1"], "--host"), (["--port", "0"], "--port"), (["--host", "localhost", "--port", "0"], "--host"),
])
def test_unknown_transports_and_options_without_http_are_usage_errors(tmp_path, args, option):
    proc, marker = run_cli(tmp_path, args)
    assert_usage_error(proc, marker, option)


HOST_REFUSALS = ["0.0.0.0", "::", "::ffff:127.0.0.1", "127.1", "0177.0.0.1", "127.000.000.001", "127.0.0.01",
                 "[::1]", "", "localhost.", "127.0.0.1.", "127.0.0.256", "128.0.0.1", "10.0.0.1", "192.168.1.10",
                 "localhost:8080", " 127.0.0.1", "127.0.0.1 ", "::1%lo0", "fe80::1", "0:0:0:0:0:0:0:1", "example.com",
                 "localhost.localdomain", "١٢٧.0.0.1", "127.0.0.1/8", "2130706433", "0x7f.0.0.1"]


@pytest.mark.parametrize("host", HOST_REFUSALS, ids=lambda value: repr(value))
def test_hosts_other_than_canonical_literal_loopback_are_refused(tmp_path, host):
    proc, marker = run_cli(tmp_path, ["--transport", "http", "--host", host, "--port", "0"])
    assert_usage_error(proc, marker, "--host")
    assert "loopback" in proc.stderr.lower(), "the refusal must name the loopback rule"


@pytest.mark.parametrize("host,shown", [("127.0.0.1", {"127.0.0.1"}), ("127.0.0.2", {"127.0.0.2"}),
                                        ("127.255.255.254", {"127.255.255.254"}), ("127.10.20.30", {"127.10.20.30"}),
                                        ("localhost", {"localhost", "127.0.0.1"}), ("LOCALHOST", {"localhost", "LOCALHOST", "127.0.0.1"}),
                                        ("LocalHost", {"localhost", "LocalHost", "127.0.0.1"})])
def test_canonical_ipv4_loopback_and_localhost_are_accepted_without_dns(tmp_path, started, host, shown):
    server = started(tmp_path, ["--transport", "http", "--host", host, "--port", "0"]).wait_ready()
    assert server.host in shown, server.host
    binds = server_binds(server.marker)
    listening = events(server.marker, "listen")
    assert binds and listening and all(item["family"] == int(socket.AF_INET) for item in binds + listening)
    requested = {item["address"][0] for item in binds}
    assert requested == ({"127.0.0.1"} if host.lower() == "localhost" else {host}), requested
    assert not [item for item in events(server.marker, "getaddrinfo") if "localhost" in item["host"].lower()]
    initialize(server)
    assert server.finish() == 0


def test_ipv6_loopback_is_accepted_and_shown_bracketed(tmp_path, started):
    server = started(tmp_path, ["--transport", "http", "--host", "::1", "--port", "0"])
    if ipv6_loopback_available():
        server.wait_ready()
        assert server.host == "[::1]"
        listening = events(server.marker, "listen")
        assert listening and {item["address"][0] for item in listening} == {"::1"}
        assert all(item["family"] == int(socket.AF_INET6) for item in listening)
        connection = http.client.HTTPConnection("::1", server.port, timeout=20)
        frame = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": PROTOCOL, "capabilities": {}, "clientInfo": {"name": "v6", "version": "0"}}}
        connection.request("POST", "/mcp", body=json.dumps(frame), headers={
            "Content-Type": "application/json", "Accept": "application/json, text/event-stream",
            "Host": f"[::1]:{server.port}"})
        response = connection.getresponse()
        assert response.status == 200, response.status
        response.read()
        connection.close()
        assert server.finish() == 0
    else:
        code = server.process.wait(timeout=30)
        text = "\n".join(server.lines)
        assert code == 2 and "Traceback" not in text and len([l for l in server.lines if l.strip()]) == 1, text
        assert "unrecognized arguments" not in text and not SERVING.search(text)


PORT_REFUSALS = ["-1", "+1", "65536", "99999999999999999999", "08080", "00", " 1", "1 ", "1.0", "0x10",
                 "١٢", "", "abc", "1e3", "８０８０"]


@pytest.mark.parametrize("port", PORT_REFUSALS, ids=lambda value: repr(value))
def test_ports_must_be_plain_ascii_digits_in_range(tmp_path, port):
    proc, marker = run_cli(tmp_path, ["--transport", "http", "--port", port])
    assert_usage_error(proc, marker, "--port")
    assert "65535" in proc.stderr, "the refusal must state the port range"


@pytest.mark.parametrize("port", ["1", "8080", "65535"])
def test_valid_fixed_ports_are_accepted(tmp_path, started, port):
    server = started(tmp_path, ["--transport", "http", "--port", port], ORACLE_BIND_ANY_PORT="1").wait_ready()
    ports = [item["address"][1] for item in server_binds(server.marker)]
    assert ports and set(ports) == {int(port)}, ports
    initialize(server)
    assert server.finish() == 0


@pytest.mark.parametrize("fault", ["in-use", "denied"])
def test_bind_failures_are_one_line_exit_2(tmp_path, fault):
    proc, marker = run_cli(tmp_path, ["--transport", "http", "--port", "0"], ORACLE_BIND_FAULT=fault)
    lines = [line for line in proc.stderr.splitlines() if line.strip()]
    assert proc.returncode == 2, (proc.returncode, proc.stderr)
    assert len(lines) == 1 and lines[0].startswith("ads-mcp:"), lines
    assert "Traceback" not in proc.stderr + proc.stdout and not SERVING.search(proc.stderr)
    assert server_binds(marker) and not events(marker, "listen"), "the failure must come from the bind attempt"


@pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGTERM], ids=["SIGINT", "SIGTERM"])
def test_sigint_and_sigterm_stop_cleanly_with_exit_0(tmp_path, started, sig):
    server = started(tmp_path).wait_ready()
    initialize(server)
    assert server.finish(sig) == 0
    assert "Traceback" not in server.stderr()


# ---------------------------------------------------------------------------
# Pre-SDK request guards.


def serving_server(started, tmp_path):
    """Started and initialized inside the test body, so a missing transport fails the test itself."""
    server = started(tmp_path).wait_ready()
    initialize(server)
    return server


def _no_echo(server, status, headers, body, supplied=b""):
    assert status != 500, body
    for token in MARK.findall(supplied):
        assert token not in body and token.decode() not in json.dumps(headers)
        assert token.decode() not in server.stderr()
    assert b"Traceback" not in body


def _tool_calls(server):
    return len(events(server.marker, "search"))


@pytest.mark.parametrize("host", ["evil.example:{port}", "127.0.0.1", "127.0.0.1:1", "localhost", "[::1]",
                                  "127.0.0.2:{port}", "0.0.0.0:{port}", "ORACLE_MARK_HOST:{port}", ""])
def test_wrong_host_header_is_421_before_the_sdk(started, tmp_path, host):
    serving = serving_server(started, tmp_path)
    before = _tool_calls(serving)
    frame = {"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {"name": "get_account_info", "arguments": {}}}
    supplied = json.dumps(frame).encode()
    status, headers, body = serving.request(supplied, host=host.format(port=serving.port))
    assert status == 421, (status, body)
    _no_echo(serving, status, headers, body, host.encode())
    assert _tool_calls(serving) == before


@pytest.mark.parametrize("host", ["127.0.0.1:{port}", "localhost:{port}", "[::1]:{port}"])
def test_each_allowed_host_header_is_accepted(started, tmp_path, host):
    serving = serving_server(started, tmp_path)
    status, _, message = serving.rpc("tools/list", ident=4, host=host.format(port=serving.port))
    assert status == 200 and message["result"]["tools"], (status, message)


@pytest.mark.parametrize("origin", ["http://evil.example", "https://127.0.0.1:{port}", "null", "http://127.0.0.1:1",
                                    "http://127.0.0.1", "http://localhost.:{port}", "file://", "http://ORACLE_MARK_ORIGIN:{port}"])
def test_wrong_origin_is_403_and_missing_origin_is_allowed(started, tmp_path, origin):
    serving = serving_server(started, tmp_path)
    status, _, message = serving.rpc("tools/list", ident=5)
    assert status == 200, "a request without Origin is allowed"
    for allowed in ("http://127.0.0.1:{port}", "http://localhost:{port}", "http://[::1]:{port}"):
        ok, _, _ = serving.rpc("tools/list", ident=6, origin=allowed.format(port=serving.port))
        assert ok == 200, allowed
    supplied = origin.format(port=serving.port)
    status, headers, body = serving.request(json.dumps({"jsonrpc": "2.0", "id": 7, "method": "tools/list"}).encode(),
                                            origin=supplied)
    assert status == 403, (status, body)
    _no_echo(serving, status, headers, body, supplied.encode())


def _padded(size):
    frame = json.dumps({"jsonrpc": "2.0", "id": 8, "method": "tools/list"}).encode()
    return frame + b" " * (size - len(frame))


def test_bodies_over_one_mib_are_413_including_chunked(started, tmp_path):
    serving = serving_server(started, tmp_path)
    status, _, body = serving.request(_padded(MIB))
    assert status == 200, "a body of exactly 1 MiB is within the limit"
    status, _, body = serving.request(_padded(MIB + 1))
    assert status == 413, status
    chunk = b" " * (64 * 1024)
    frame = json.dumps({"jsonrpc": "2.0", "id": 8, "method": "tools/list"}).encode()
    status, _, body = serving.request(None, chunks=[frame] + [chunk] * 17)
    assert status == 413, status
    status, _, body = serving.request(None, chunks=[frame, b" " * 10])
    assert status == 200, "small chunked bodies are processed"


INVALID_BODIES = {
    "not-json": b"{ORACLE_MARK_BODY not json",
    "truncated": b'{"jsonrpc": "2.0", "id": 1, "method": "tools/list"',
    "invalid-utf8": b'{"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"x": "\xff\xfe"}}',
    "lone-surrogate": b'{"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {"x": "\\ud800ORACLE_MARK_SURROGATE"}}',
    "batch": b'[{"jsonrpc": "2.0", "id": 1, "method": "tools/list"}]',
    "empty-batch": b"[]",
    "scalar": b'"ORACLE_MARK_SCALAR"',
    "id-object": b'{"jsonrpc": "2.0", "id": {"ORACLE_MARK_ID": 1}, "method": "tools/list"}',
    "id-array": b'{"jsonrpc": "2.0", "id": [1], "method": "tools/list"}',
    "id-null": b'{"jsonrpc": "2.0", "id": null, "method": "tools/list"}',
    "id-true": b'{"jsonrpc": "2.0", "id": true, "method": "tools/list"}',
    "id-false": b'{"jsonrpc": "2.0", "id": false, "method": "tools/list"}',
    "id-fraction": b'{"jsonrpc": "2.0", "id": 1.5, "method": "tools/list"}',
    "id-float-integral": b'{"jsonrpc": "2.0", "id": 1.0, "method": "tools/list"}',
    "empty": b"",
}


def test_malformed_requests_get_400_with_one_fixed_body(started, tmp_path):
    serving = serving_server(started, tmp_path)
    bodies = {}
    before = _tool_calls(serving)
    for name, supplied in INVALID_BODIES.items():
        status, headers, body = serving.request(supplied)
        assert status == 400, (name, status, body)
        _no_echo(serving, status, headers, body, supplied)
        bodies[name] = body
    assert len(set(bodies.values())) == 1, {name: body[:80] for name, body in bodies.items()}
    assert _tool_calls(serving) == before
    status, _, message = serving.rpc("tools/list", ident=0)
    assert status == 200 and message["id"] == 0, "valid integer ids keep exact correlation"
    status, _, message = serving.rpc("tools/list", ident="")
    assert status == 200 and message["id"] == "", "valid string ids keep exact correlation"
    status, _, _ = serving.rpc("notifications/initialized", ident=None)
    assert status == 202, "a request without id is a notification"
    assert serving.finish() == 0
    text = serving.stderr()
    assert "Traceback" not in text and "ORACLE_MARK" not in text
    assert "POST /mcp" not in text and "HTTP/1.1" not in text, "access logging must be off"


def test_stateless_requests_need_no_session(started, tmp_path):
    serving = serving_server(started, tmp_path)
    for ident in (11, 12, 13):
        status, headers, message = serving.rpc("tools/call", {"name": "health_check", "arguments": {}}, ident=ident)
        assert status == 200 and message["id"] == ident and tool_payload(message)["status"] == "OK", message
        assert "mcp-session-id" not in headers, "stateless serving issues no session"


# ---------------------------------------------------------------------------
# Declared dependencies and archives.


def test_starlette_and_uvicorn_are_declared_with_minimum_floors():
    from offline_contract import load_script

    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    declared = {re.split(r"[<>=!~ ;\[]", item, maxsplit=1)[0].lower(): item.replace(" ", "")
                for item in project["dependencies"]}
    assert "starlette" in declared and ">=1.7.0" in declared["starlette"], project["dependencies"]
    assert "uvicorn" in declared and ">=0.54.0" in declared["uvicorn"], project["dependencies"]
    minimum = [line.strip().lower() for line in (ROOT / "requirements-min.txt").read_text().splitlines()]
    assert "starlette==1.7.0" in minimum and "uvicorn==0.54.0" in minimum
    help_text = subprocess.run([str(h.console_script()), "--help"], env=h.scrubbed_env(), capture_output=True,
                               text=True, timeout=30).stdout
    assert "--transport" in help_text and "--host" in help_text and "--port" in help_text
    checker = load_script("check_release_archives")
    assert "tests/test_http_transport_contract.py" in checker.REQUIRED_SOURCE
    assert sys.version_info >= (3, 12)
