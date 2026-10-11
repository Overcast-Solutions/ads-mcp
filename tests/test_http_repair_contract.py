"""F095 repair: HTTP echo, shutdown, printed URL, body timeouts and connection hygiene.

Reproduces the verify findings S2 (protocol and dispatch errors echoed the
caller's method, tool and argument names), N7 (out-of-range ids got the SDK's
parse-error body), S3 (a signal while a client was mid-body printed a
traceback and took about 10 s), N6 (per-request SDK and uvicorn lines on
stderr), S4 (the printed URL for ``--host LOCALHOST`` or ``127.0.0.2`` was
refused with 421), N8 (a stalled body held its connection forever), N9
(``GET /mcp`` held an event stream open on a stateless server), U1 (an
oversize body was answered with a reset instead of a readable 413) and the
N14 "applies" grammar nit.

Every test drives the installed ``ads-mcp`` console through the F091 driver
and sitecustomize (tests/test_http_transport_contract.py), which substitutes
only the Google Ads client factory and observes socket binds. Where a test
needs more, it appends to that same injection: real 127.x binds where the
platform has the alias, and the ``BODY_IDLE_TIMEOUT_SECONDS`` /
``BODY_TOTAL_TIMEOUT_SECONDS`` module constants of ``ads_mcp.http_transport``
patched after import (so the server must read them after import, not freeze
them into function defaults). Every wait is bounded.

Contract pinned here: "the fixed 400 body" is the guard's existing refusal
body, byte for byte, as returned for an empty batch.
"""
import http.client
import json
import re
import signal
import socket
import subprocess
import time

import pytest

import harness as h
import test_http_transport_contract as http_contract
from test_http_transport_contract import (  # noqa: F401 - ``started`` is a fixture
    MIB,
    PROTOCOL,
    SERVING,
    decode,
    events,
    initialize,
    run_cli,
    serving_server,
    started,
    tool_payload,
)

MARK = "ORACLE_MARK"

REAL_ALIAS_BIND = r'''
def _oracle_real_bind(self, address):
    shown = list(address[:2]) if isinstance(address, tuple) else str(address)
    mark("bind", family=int(self.family), address=shown)
    return _bind(self, address)
socket.socket.bind = _oracle_real_bind
'''

BODY_TIMEOUTS = r'''
import ads_mcp.http_transport as _oracle_http
mark("body timeouts", idle=getattr(_oracle_http, "BODY_IDLE_TIMEOUT_SECONDS", None),
     total=getattr(_oracle_http, "BODY_TOTAL_TIMEOUT_SECONDS", None))
if os.environ.get("ORACLE_BODY_IDLE"):
    _oracle_http.BODY_IDLE_TIMEOUT_SECONDS = float(os.environ["ORACLE_BODY_IDLE"])
if os.environ.get("ORACLE_BODY_TOTAL"):
    _oracle_http.BODY_TOTAL_TIMEOUT_SECONDS = float(os.environ["ORACLE_BODY_TOTAL"])
'''


def frame(method, params=None, ident=1):
    message = {"jsonrpc": "2.0", "id": ident, "method": method}
    if params is not None:
        message["params"] = params
    return json.dumps(message).encode()


def init_frame(ident=1):
    return frame("initialize", {"protocolVersion": PROTOCOL, "capabilities": {},
                                "clientInfo": {"name": "http-repair-oracle", "version": "0"}}, ident)


def head(method, port, *extra, host=None):
    lines = [f"{method} /mcp HTTP/1.1", f"Host: {host or f'127.0.0.1:{port}'}",
             "Content-Type: application/json", "Accept: application/json, text/event-stream",
             f"MCP-Protocol-Version: {PROTOCOL}", *extra]
    return ("\r\n".join(lines) + "\r\n\r\n").encode()


def connect(port, address="127.0.0.1", timeout=20):
    return socket.create_connection((address, port), timeout=timeout)


def read_to_end(sock, timeout=20):
    """All bytes until the server closes, plus how the read ended."""
    sock.settimeout(timeout)
    data = bytearray()
    try:
        while True:
            chunk = sock.recv(65536)
            if not chunk:
                return bytes(data), None
            data.extend(chunk)
    except OSError as exc:
        return bytes(data), type(exc).__name__


def parse_response(data):
    head_part, _, body = data.partition(b"\r\n\r\n")
    lines = head_part.decode("latin-1").split("\r\n")
    parts = lines[0].split(" ", 2)
    status = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
    headers = {}
    for line in lines[1:]:
        name, _, value = line.partition(":")
        headers[name.strip().lower()] = value.strip()
    return status, headers, body


def answer_or_close(sock, wait):
    """('408' | 'closed' | 'open' | 'answered <status>', seconds) within ``wait`` seconds."""
    sock.settimeout(wait)
    begun = time.monotonic()
    try:
        data = sock.recv(65536)
    except socket.timeout:
        return "open", time.monotonic() - begun
    except OSError:
        return "closed", time.monotonic() - begun
    elapsed = time.monotonic() - begun
    if not data:
        return "closed", elapsed
    status, _, _ = parse_response(data)
    return ("408" if status == 408 else f"answered {status}"), elapsed


def assert_no_echo(status, headers, body):
    assert status != 500, body[:300]
    assert MARK.encode() not in body, f"caller input echoed: {body[:300]!r}"
    assert MARK not in json.dumps(headers)
    assert b"Traceback" not in body


def is_error_reply(message):
    return "error" in message or (message.get("result") or {}).get("isError") is True


def alias_bindable(address):
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind((address, 0))
        return True
    except OSError:
        return False


def post_initialize(address, port, *, host=None, origin=None):
    """An initialize POST from a default client: Host comes from the address unless given."""
    connection = http.client.HTTPConnection(address, port, timeout=20)
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if host is not None:
        headers["Host"] = host
    if origin is not None:
        headers["Origin"] = origin
    try:
        connection.request("POST", "/mcp", body=init_frame(), headers=headers)
        response = connection.getresponse()
        reply = {k.lower(): v for k, v in response.getheaders()}
        body = response.read()
        return response.status, decode(reply, body) if response.status == 200 else body
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# S2 and N7: protocol and dispatch errors never echo caller input.


def test_unknown_methods_get_a_fixed_method_not_found_without_data(started, tmp_path):
    serving = serving_server(started, tmp_path)
    errors = []
    for method in (MARK + "_METHOD_A<script>", MARK + "_METHOD_B\u001b[31m"):
        status, headers, body = serving.request(frame(method, ident=41))
        assert_no_echo(status, headers, body)
        message = decode(headers, body)
        assert message["id"] == 41 and message["error"]["code"] == -32601, message
        assert "data" not in message["error"], message
        errors.append(message["error"])
    assert errors[0] == errors[1], "the method-not-found message is fixed"


def test_unknown_tools_get_one_fixed_reply_without_the_name(started, tmp_path):
    serving = serving_server(started, tmp_path)
    replies = []
    for name in (MARK + "_TOOL_A", MARK + "_TOOL_B\u001b[31m"):
        status, headers, body = serving.request(frame("tools/call", {"name": name, "arguments": {}}, ident=42))
        assert_no_echo(status, headers, body)
        message = decode(headers, body)
        assert message["id"] == 42 and is_error_reply(message), message
        replies.append(message)
    assert replies[0] == replies[1], "the unknown-tool message is fixed"


def test_schema_validation_failures_name_no_caller_key_or_value(started, tmp_path):
    serving = serving_server(started, tmp_path)
    replies = []
    for arguments in ({"name": "campaign", MARK + "_KEY_A": 1},
                      {"name": "campaign", MARK + "_KEY_B\u001b[2J": MARK + "_VALUE_B"}):
        status, headers, body = serving.request(frame("tools/call", {"name": "get_resource_metadata",
                                                                     "arguments": arguments}, ident=43))
        assert_no_echo(status, headers, body)
        message = decode(headers, body)
        assert message["id"] == 43 and is_error_reply(message), message
        replies.append(message)
    assert replies[0] == replies[1], "an unexpected argument key gets one fixed message"
    for arguments in ({"name": [MARK + "_VALUE_C"]}, {"name": "campaign", "page_size": MARK + "_VALUE_D"}):
        status, headers, body = serving.request(frame("tools/call", {"name": "get_resource_metadata",
                                                                     "arguments": arguments}, ident=44))
        assert_no_echo(status, headers, body)
        assert is_error_reply(decode(headers, body))
    # Tool-level errors are unchanged: the tool's own sanitized excerpt still reaches the caller.
    status, headers, body = serving.request(frame("tools/call", {"name": "get_resource_metadata",
                                                                 "arguments": {"name": "Campaign"}}, ident=45))
    over_http = tool_payload(decode(headers, body))
    (tmp_path / "local").mkdir()
    local = h.call(h.build_server(tmp_path / "local", client=h.FakeGoogleAdsClient()),
                   "get_resource_metadata", {"name": "Campaign"})
    assert over_http == local and "'Campaign'" in over_http["error"]["message"], over_http


def test_bodies_the_sdk_schema_rejects_get_the_fixed_400_body(started, tmp_path):
    serving = serving_server(started, tmp_path)
    status, _, fixed = serving.request(b"[]")
    assert status == 400 and fixed
    rejected = {
        "wrong-jsonrpc": json.dumps({"jsonrpc": "ECHO_ME", "id": 51, "method": "ping"}).encode(),
        "params-not-object": json.dumps({"jsonrpc": "2.0", "id": 52, "method": "ping", "params": "ECHO_P"}).encode(),
        "method-not-string": json.dumps({"jsonrpc": "2.0", "id": 53, "method": 7}).encode(),
        "id-above-range": b'{"jsonrpc": "2.0", "id": ' + b"9" * 5000 + b', "method": "ping"}',
        "id-below-range": b'{"jsonrpc": "2.0", "id": -' + b"9" * 5000 + b', "method": "ping"}',
    }
    for name, body in rejected.items():
        status, headers, reply = serving.request(body)
        assert (status, reply) == (400, fixed), (name, status, reply[:160])
        assert b"ECHO_" not in reply
    for ident in (2**127, -1, 0):
        status, _, message = serving.rpc("ping", ident=ident)
        assert status == 200 and message["id"] == ident, "ids the stdio matrix accepts keep exact correlation"


# ---------------------------------------------------------------------------
# N6 and S3: stderr and shutdown.


def test_http_stderr_holds_only_the_startup_lines_and_at_most_one_shutdown_line(started, tmp_path):
    serving = serving_server(started, tmp_path)
    port = serving.port
    serving.rpc("tools/list", ident=2)
    serving.rpc("tools/call", {"name": "get_account_info", "arguments": {}}, ident=3)
    serving.request(b"{" + MARK.encode() + b"_MALFORMED")
    try:
        serving.request(b'{"jsonrpc": "2.0", "id": 4, "method": "tools/list"}' + b" " * (MIB + 1))
    except OSError:
        pass  # this test reads stderr; the 413 itself is pinned below
    serving.rpc(MARK + "_METHOD", ident=5)
    serving.rpc("tools/call", {"name": MARK + "_TOOL", "arguments": {}}, ident=6)
    serving.rpc("tools/call", {"name": "get_resource_metadata",
                               "arguments": {"name": "campaign", MARK + "_KEY": 1}}, ident=7)
    with connect(port, timeout=5) as upgrade:
        upgrade.sendall(head("GET", port, "Upgrade: websocket", "Connection: Upgrade",
                             "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==", "Sec-WebSocket-Version: 13"))
        answer_or_close(upgrade, 3)
    with connect(port, timeout=5) as garbled:
        garbled.sendall(MARK.encode() + b"_LINE\r\n\r\n")
        answer_or_close(garbled, 3)
    with connect(port, timeout=10) as mid:
        mid.sendall(head("POST", port, "Content-Length: 100") + b'{"jsonrpc"')
        time.sleep(0.5)
        code = serving.finish(signal.SIGTERM, timeout=20)
    lines = [line for line in serving.lines if line.strip()]
    assert len(lines) >= 2 and SERVING.match(lines[0]) and "local process" in lines[1], lines[:3]
    assert len(lines) <= 3, f"stderr must hold the two startup lines and at most one shutdown line: {lines}"
    text = "\n".join(lines)
    assert MARK not in text and "Traceback" not in text, text
    assert code == 0


@pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGTERM], ids=["SIGINT", "SIGTERM"])
def test_a_signal_with_a_client_mid_body_exits_0_within_5_seconds_without_a_traceback(started, tmp_path, sig):
    serving = serving_server(started, tmp_path)
    with connect(serving.port, timeout=10) as mid:
        mid.sendall(head("POST", serving.port, "Content-Length: 100") + b'{"jsonrpc"')
        time.sleep(0.5)
        signalled = time.monotonic()
        serving.process.send_signal(sig)
        try:
            code = serving.process.wait(timeout=20)
        except subprocess.TimeoutExpired:
            pytest.fail(f"the server was still running 20 s after {sig.name}")
        elapsed = time.monotonic() - signalled
    serving.finish()
    text = serving.stderr()
    assert elapsed <= 5.0, f"exit took {elapsed:.1f} s after {sig.name} with a client mid-body"
    assert code == 0, (code, text)
    for forbidden in ("Traceback", "CancelledError", "Exception in ASGI application"):
        assert forbidden not in text, text


# ---------------------------------------------------------------------------
# S4: the printed URL works as printed; Host and Origin follow --host.


@pytest.mark.parametrize("host", ["localhost", "LOCALHOST", "LocalHost"])
def test_localhost_in_any_case_prints_a_url_a_default_client_can_use(started, tmp_path, host):
    server = started(tmp_path, ["--transport", "http", "--host", host, "--port", "0"]).wait_ready()
    printed = next(line for line in server.lines if SERVING.match(line))
    assert server.host == "127.0.0.1", f"--host {host} must print http://127.0.0.1:PORT/mcp: {printed!r}"
    status, message = post_initialize(server.host, server.port)
    assert status == 200 and "serverInfo" in message["result"], (status, message)
    assert server.finish() == 0


@pytest.mark.parametrize("authority", ["LOCALHOST:{port}", "LocalHost:{port}"])
def test_host_and_origin_compare_the_host_part_case_insensitively(started, tmp_path, authority):
    serving = serving_server(started, tmp_path)
    shown = authority.format(port=serving.port)
    status, headers, body = serving.request(frame("tools/list", ident=61), host=shown)
    assert status == 200, f"Host {shown} must be accepted: {status} {body[:80]!r}"
    assert decode(headers, body)["result"]["tools"]
    status, headers, body = serving.request(frame("tools/list", ident=62), origin="http://" + shown)
    assert status == 200, f"Origin http://{shown} must be accepted: {status} {body[:80]!r}"
    assert decode(headers, body)["result"]["tools"]
    assert serving.request(frame("tools/list", ident=63), host=f"LOCALHOST.:{serving.port}")[0] == 421
    assert serving.request(frame("tools/list", ident=64), origin=f"https://{shown}")[0] == 403


def test_a_non_default_loopback_literal_is_printed_and_accepted_in_host_and_origin(started, tmp_path, monkeypatch):
    real = alias_bindable("127.0.0.2")
    if real:
        monkeypatch.setattr(http_contract, "INJECTION", http_contract.INJECTION + REAL_ALIAS_BIND)
    server = started(tmp_path, ["--transport", "http", "--host", "127.0.0.2", "--port", "0"]).wait_ready()
    assert server.host == "127.0.0.2", server.lines[:2]
    authority = f"127.0.0.2:{server.port}"
    if real:
        # Where the alias exists the URL is used exactly as printed.
        status, message = post_initialize("127.0.0.2", server.port)
        listening = {tuple(item["address"]) for item in events(server.marker, "listen")}
        assert ("127.0.0.2", server.port) in listening, listening
    else:
        # Without the alias the F091 injection binds 127.0.0.1; the Host is still the printed one.
        status, message = post_initialize("127.0.0.1", server.port, host=authority)
    assert status == 200, f"Host {authority} must be accepted: {status} {message!r}"
    assert "serverInfo" in message["result"], message
    address = "127.0.0.2" if real else "127.0.0.1"
    status, message = post_initialize(address, server.port, host=authority, origin="http://" + authority)
    assert status == 200, (status, message)
    status, _ = post_initialize(address, server.port, host=f"127.0.0.3:{server.port}")
    assert status == 421, "only the validated --host literal joins the allowed hosts"
    for other in (f"127.0.0.1:{server.port}", f"localhost:{server.port}", f"[::1]:{server.port}"):
        assert post_initialize(address, server.port, host=other)[0] == 200, other
    assert server.finish() == 0


# ---------------------------------------------------------------------------
# N8, N9 and U1: body timeouts, GET/DELETE and the oversize drain.


def test_a_stalled_body_is_answered_408_or_closed_after_the_idle_timeout(started, tmp_path, monkeypatch):
    monkeypatch.setattr(http_contract, "INJECTION", http_contract.INJECTION + BODY_TIMEOUTS)
    serving = started(tmp_path, ORACLE_BODY_IDLE="1", ORACLE_BODY_TOTAL="20").wait_ready()
    initialize(serving)
    port = serving.port
    body = frame("tools/list", ident=71)
    with connect(port) as sock:
        sock.sendall(head("POST", port, f"Content-Length: {len(body)}", "Connection: close"))
        for start in range(0, len(body), 12):
            time.sleep(0.3)
            sock.sendall(body[start:start + 12])
        status, _, _ = parse_response(read_to_end(sock)[0])
    assert status == 200, "a body whose pieces arrive within the idle limit is served"
    for partial in (b"", b'{"jsonrpc"'):
        with connect(port) as sock:
            sock.sendall(head("POST", port, "Content-Length: 100") + partial)
            outcome, waited = answer_or_close(sock, 6)
        assert outcome in ("408", "closed"), (
            f"a body silent for the idle timeout (1 s) must be answered 408 or closed: {outcome} after {waited:.1f} s")
        assert waited <= 1 + 2.5, waited
    recorded = events(serving.marker, "body timeouts")
    assert recorded and recorded[0]["idle"] == 10 and recorded[0]["total"] == 30, (
        f"ads_mcp.http_transport must define BODY_IDLE_TIMEOUT_SECONDS = 10 and BODY_TOTAL_TIMEOUT_SECONDS = 30: "
        f"{recorded}")
    assert serving.finish() == 0


def test_a_trickling_body_is_answered_408_or_closed_after_the_total_timeout(started, tmp_path, monkeypatch):
    monkeypatch.setattr(http_contract, "INJECTION", http_contract.INJECTION + BODY_TIMEOUTS)
    serving = started(tmp_path, ORACLE_BODY_IDLE="2", ORACLE_BODY_TOTAL="3").wait_ready()
    initialize(serving)
    outcome = "open"
    with connect(serving.port) as sock:
        sock.sendall(head("POST", serving.port, "Content-Length: 1000") + b"{")
        begun = time.monotonic()
        while time.monotonic() - begun < 9:
            try:
                sock.sendall(b" ")
            except OSError:
                outcome = "closed"
                break
            outcome = answer_or_close(sock, 0.5)[0]
            if outcome != "open":
                break
        waited = time.monotonic() - begun
    assert outcome in ("408", "closed"), (
        f"a body still incomplete after the total timeout (3 s) must be answered 408 or closed: {outcome} "
        f"after {waited:.1f} s of trickling")
    assert waited <= 3 + 2.5, waited
    assert serving.finish() == 0


@pytest.mark.parametrize("method", ["GET", "DELETE"])
def test_get_and_delete_on_mcp_answer_405_with_allow_post_immediately(started, tmp_path, method):
    serving = serving_server(started, tmp_path)
    connection = http.client.HTTPConnection("127.0.0.1", serving.port, timeout=5)
    try:
        begun = time.monotonic()
        connection.request(method, "/mcp", headers={"Accept": "application/json, text/event-stream",
                                                    "MCP-Protocol-Version": PROTOCOL})
        response = connection.getresponse()
        status, allow = response.status, response.getheader("allow")
        if status == 405:
            response.read()
        elapsed = time.monotonic() - begun
    finally:
        connection.close()
    assert status == 405, f"{method} /mcp must be refused on a stateless server, got {status}"
    assert allow is not None and [item.strip() for item in allow.split(",")] == ["POST"], (
        f"405 must carry Allow: POST, got {allow!r}")
    assert elapsed < 5


def _chunked(body, size=64 * 1024):
    for start in range(0, len(body), size):
        piece = body[start:start + size]
        yield b"%x\r\n" % len(piece) + piece + b"\r\n"
    yield b"0\r\n\r\n"


def read_response(sock, timeout=20):
    """One HTTP response read by its Content-Length: (status, headers, failure)."""
    sock.settimeout(timeout)
    data = bytearray()
    try:
        while b"\r\n\r\n" not in data:
            chunk = sock.recv(65536)
            if not chunk:
                return None, {}, f"closed before a response: {bytes(data[:120])!r}"
            data.extend(chunk)
        status, headers, body = parse_response(bytes(data))
        length = int(headers.get("content-length", "0") or 0)
        while len(body) < length:
            chunk = sock.recv(65536)
            if not chunk:
                break
            body += chunk
    except OSError as exc:
        return None, {}, f"{type(exc).__name__} before the response was read: {bytes(data[:120])!r}"
    return status, headers, None


@pytest.mark.parametrize("framing,size,client_close", [
    ("content-length", 2 * MIB, False), ("content-length", 8 * MIB, True), ("chunked", 4 * MIB, False),
], ids=["length-2MiB", "length-8MiB-client-close", "chunked-4MiB"])
def test_oversize_bodies_are_drained_then_answered_413_with_connection_close(
        started, tmp_path, framing, size, client_close):
    serving = serving_server(started, tmp_path)
    port = serving.port
    start = frame("tools/list", ident=81)
    body = start + b" " * (size - len(start))
    extra = ["Connection: close"] if client_close else []
    with connect(port, timeout=30) as sock:
        if framing == "content-length":
            sock.sendall(head("POST", port, f"Content-Length: {size}", *extra))
            pieces = (body[offset:offset + 64 * 1024] for offset in range(0, size, 64 * 1024))
        else:
            sock.sendall(head("POST", port, "Transfer-Encoding: chunked", *extra))
            pieces = _chunked(body)
        try:
            for piece in pieces:
                sock.sendall(piece)
        except OSError as exc:
            pytest.fail(f"the server cut the connection while the client was still sending "
                        f"({type(exc).__name__}); it must drain up to 8 MiB before closing")
        status, headers, failure = read_response(sock)
        assert failure is None, failure
        assert status == 413, status
        assert headers.get("connection", "").lower() == "close", f"413 must carry Connection: close: {headers}"
        rest, ended = read_to_end(sock, timeout=5)
    assert ended is None and rest == b"", f"after the 413 the server closes cleanly: {ended} {rest[:80]!r}"
    assert serving.finish() == 0


# ---------------------------------------------------------------------------
# N14 and archives.


@pytest.mark.parametrize("option,value", [("--host", "127.0.0.1"), ("--port", "0")])
def test_option_errors_read_applies_for_one_option_and_apply_for_both(tmp_path, option, value):
    one, _ = run_cli(tmp_path / "one", [option, value])
    assert one.returncode == 2 and option in one.stderr, one.stderr
    assert re.search(r"\bapplies\b", one.stderr) and not re.search(r"\bapply\b", one.stderr), one.stderr
    both, _ = run_cli(tmp_path / "both", ["--host", "127.0.0.1", "--port", "0"])
    assert both.returncode == 2 and "--host" in both.stderr and "--port" in both.stderr, both.stderr
    assert re.search(r"\bapply\b", both.stderr) and not re.search(r"\bapplies\b", both.stderr), both.stderr
