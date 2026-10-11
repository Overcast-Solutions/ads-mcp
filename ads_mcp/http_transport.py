"""Local, read-only, stateless MCP streamable HTTP for the installed console.

``ads-mcp --transport http`` serves the same read tools, schemas and audit
behavior as stdio over an HTTP endpoint bound to literal loopback only. The
endpoint is unauthenticated by design, so it is refused unless the effective
configuration is read-only, and every guard below runs *before* the MCP SDK
parses anything:

* ``Host`` must be one of the canonical loopback authorities carrying the bound
  port, compared case-insensitively (``421`` otherwise); ``Origin``, when
  present, must be the ``http`` form of one of them (``403``; a missing
  ``Origin`` is allowed).
* only ``POST`` is served at the endpoint path: a stateless server has no
  event stream to open and no session to delete, so other methods get ``405``
  with ``Allow: POST``.
* a body that stalls for ``BODY_IDLE_TIMEOUT_SECONDS`` or is still incomplete
  after ``BODY_TOTAL_TIMEOUT_SECONDS`` gets ``408`` and the connection closes.
* request bodies over 1 MiB are refused (``413``), including chunked bodies
  that only exceed the limit while streaming. The rest of the body is drained
  first — bounded by ``MAX_DRAIN_BYTES`` and by the body timeouts — so the
  caller finishes sending and reads the refusal instead of a reset.
* a POST body that is not a single JSON-RPC envelope the SDK can safely parse
  is refused with one fixed body (``400``), and any ``400`` the SDK itself
  produces is replaced by that same body.

No refusal echoes the request back to the caller or to the log. Protocol- and
dispatch-level failures are reduced to fixed messages as well, which makes
this transport deliberately less talkative than stdio: an HTTP peer is any
local process, so it learns nothing about what it sent. Tool results and the
tools' own error payloads are untouched.
"""

from __future__ import annotations

import asyncio
import json
import re
import socket
import sys
import time
from collections import deque

#: Canonical loopback host specifications accepted by ``--host``.
LOOPBACK_RULE = (
    "canonical literal loopback only: 127.x.y.z (four decimal octets without "
    "leading zeros), ::1 (no brackets or zone id), or localhost"
)
PORT_RULE = "a decimal port from 0 to 65535 without sign, whitespace or leading zeros"

MAX_BODY_BYTES = 1024 * 1024
#: How much of an oversize body is drained before the connection is closed, so
#: an ordinary client reads the 413 rather than losing the connection mid-send.
MAX_DRAIN_BYTES = 8 * 1024 * 1024
#: A body that goes quiet for this long is abandoned. Not user-configurable:
#: the endpoint is local, so these are protection against a stuck peer holding
#: a connection, not a tuning knob.
BODY_IDLE_TIMEOUT_SECONDS = 10
#: A body that is not complete within this long is abandoned, however slowly
#: its pieces keep arriving.
BODY_TOTAL_TIMEOUT_SECONDS = 30

#: Every pre-SDK envelope refusal shares this body, so a malformed request
#: learns nothing about which check rejected it.
INVALID_REQUEST_BODY = json.dumps(
    {
        "jsonrpc": "2.0",
        "id": None,
        "error": {"code": -32600, "message": "Invalid JSON-RPC request"},
    },
    separators=(",", ":"),
).encode()
BODY_TOO_LARGE_BODY = b"Request body too large"
BODY_TIMEOUT_BODY = b"Request body timed out"
METHOD_NOT_ALLOWED_BODY = b"Method Not Allowed"

#: Fixed replacements for the JSON-RPC errors the SDK raises. The SDK attaches
#: the offending method (or the parser's diagnostics) as ``data``; over HTTP the
#: code alone is the answer.
_METHOD_NOT_FOUND = -32601
_INVALID_REQUEST = -32600
_INVALID_PARAMS = -32602
_INTERNAL_ERROR = -32603
_PARSE_ERROR = -32700
FIXED_ERROR_MESSAGES = {
    _PARSE_ERROR: "Parse error",
    _INVALID_REQUEST: "Invalid JSON-RPC request",
    _METHOD_NOT_FOUND: "Method not found",
    _INVALID_PARAMS: "Invalid request parameters",
    _INTERNAL_ERROR: "Internal error",
}
DEFAULT_FIXED_ERROR_MESSAGE = "Request refused"
UNKNOWN_TOOL_MESSAGE = (
    "Unknown tool; call tools/list for the tools this server offers"
)

_OCTET = re.compile(r"0|[1-9][0-9]{0,2}")
_PORT = re.compile(r"0|[1-9][0-9]{0,4}")


class OptionError(ValueError):
    """An invalid ``--host``/``--port`` value; the message names the rule."""


class HostSpec:
    """A validated loopback endpoint: how to bind it and how to show it."""

    def __init__(self, family: int, address: str, display: str):
        self.family = family
        self.address = address
        self.display = display


def resolve_host(value: str) -> HostSpec:
    """Validate ``--host`` as canonical literal loopback.

    ``localhost`` (in any case) binds 127.0.0.1 directly: the name is never
    resolved, so a poisoned resolver cannot move the endpoint off loopback. It
    is also *shown* as 127.0.0.1, because that is the authority the endpoint
    answers for without depending on how the caller's resolver maps the name.
    Every other accepted host is shown as the lowercase literal that was bound.
    """
    if value.lower() == "localhost":
        return HostSpec(socket.AF_INET, "127.0.0.1", "127.0.0.1")
    if value == "::1":
        return HostSpec(socket.AF_INET6, "::1", "[::1]")
    parts = value.split(".")
    if len(parts) == 4 and all(_OCTET.fullmatch(part) for part in parts):
        octets = [int(part) for part in parts]
        if octets[0] == 127 and all(octet <= 255 for octet in octets):
            return HostSpec(socket.AF_INET, value, value.lower())
    raise OptionError(f"--host {value!r} is not accepted: {LOOPBACK_RULE}")


def resolve_port(value: str) -> int:
    """Validate ``--port`` as plain ASCII digits in the TCP port range."""
    if not _PORT.fullmatch(value) or int(value) > 65535:
        raise OptionError(f"--port {value!r} is not accepted: {PORT_RULE}")
    return int(value)


def allowed_authorities(port: int, literal: str | None = None) -> tuple:
    """The only ``Host`` authorities this endpoint answers for.

    The three canonical loopback authorities always answer, and so does the
    validated ``--host`` literal with the bound port: the URL the operator was
    given has to work as printed, and a client that was handed
    ``http://127.0.0.2:PORT/mcp`` sends ``Host: 127.0.0.2:PORT``. The literal
    comes from the validated option, never from the socket, so a rewritten or
    aliased bind cannot widen this set.
    """
    authorities = [f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"]
    if literal:
        extra = f"{literal.lower()}:{port}"
        if extra not in authorities:
            authorities.append(extra)
    return tuple(authorities)


class _IntToken:
    """An integer token kept as text, so nothing converts untrusted digits."""

    __slots__ = ("token",)

    def __init__(self, token: str):
        self.token = token


def _id_token_in_range(token: str) -> bool:
    """True when an integer id can be carried and echoed back exactly.

    Python refuses to render an integer wider than its decimal conversion
    limit, so an id beyond it cannot be correlated in a reply at all. That
    limit is the boundary: every id the stdio matrix accepts is far inside it.
    """
    limit = sys.get_int_max_str_digits()
    if limit <= 0:
        return True
    return len(token.removeprefix("-")) <= limit


def _has_lone_surrogate(value) -> bool:
    """True when any string in the envelope is not encodable as UTF-8."""
    if isinstance(value, str):
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            return True
        return False
    if isinstance(value, dict):
        return any(
            _has_lone_surrogate(key) or _has_lone_surrogate(item)
            for key, item in value.items()
        )
    if isinstance(value, list):
        return any(_has_lone_surrogate(item) for item in value)
    return False


def envelope_is_valid(body: bytes) -> bool:
    """True when the body is one JSON-RPC envelope the SDK may safely parse.

    A batch, a scalar, invalid UTF-8, a lone surrogate, a ``jsonrpc`` member
    that is not ``"2.0"``, a non-string ``method``, non-object ``params``, an
    id that is not a string or integer (including ``null``, booleans and any
    float) or an integer id too wide to echo back is refused here, so the SDK's
    schema never reports the caller's own tokens. An absent id is a
    notification and is accepted.
    """
    if not body:
        return False
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        return False
    try:
        # Token types are all that matter. Replacing the number hooks keeps
        # Python's decimal conversion limit off untrusted input, exactly as the
        # stdio preflight does; integer tokens stay text so their width can be
        # measured without converting them.
        envelope = json.loads(text, parse_int=_IntToken, parse_float=lambda _: 0.0)
    except (ValueError, RecursionError):
        return False
    if _has_lone_surrogate(envelope):
        return False
    if not isinstance(envelope, dict):
        return False
    if envelope.get("jsonrpc") != "2.0":
        return False
    if "method" in envelope:
        if type(envelope["method"]) is not str:
            return False
        params = envelope.get("params")
        if params is not None and not isinstance(params, dict):
            return False
    elif "result" not in envelope and "error" not in envelope:
        return False
    if "id" in envelope:
        ident = envelope["id"]
        if isinstance(ident, _IntToken):
            if not _id_token_in_range(ident.token):
                return False
        elif type(ident) is not str:
            return False
    return True


class LocalRequestGuard:
    """ASGI guard in front of the SDK app: authority, method, size, envelope.

    The body is buffered once and replayed to the wrapped application, so the
    SDK still sees a normal ASGI request.
    """

    def __init__(
        self,
        app,
        *,
        port: int,
        literal: str | None = None,
        path: str = "/mcp",
        max_body: int = MAX_BODY_BYTES,
    ):
        self.app = app
        self.hosts = frozenset(allowed_authorities(port, literal))
        self.origins = frozenset(f"http://{item}" for item in self.hosts)
        self.path = path
        self.max_body = max_body

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        host = origin = None
        for name, raw in scope["headers"]:
            if name == b"host":
                host = raw.decode("latin-1")
            elif name == b"origin":
                origin = raw.decode("latin-1")
        # Authorities are case-insensitive, so the printed URL keeps working
        # whichever case a client sends it back in.
        if host is None or host.lower() not in self.hosts:
            await _refuse(send, 421, b"Invalid Host header")
            return
        if origin is not None and origin.lower() not in self.origins:
            await _refuse(send, 403, b"Invalid Origin header")
            return
        if scope["path"] == self.path and scope["method"] != "POST":
            # Stateless serving answers every message on its POST: there is no
            # standalone stream to GET and no session to DELETE.
            await _refuse(
                send,
                405,
                METHOD_NOT_ALLOWED_BODY,
                extra=((b"allow", b"POST"),),
            )
            return

        outcome, body, complete, pending = await self._read_body(receive)
        if outcome == "timeout":
            await _refuse(send, 408, BODY_TIMEOUT_BODY, close=True)
            return
        if outcome == "oversize":
            await _refuse(send, 413, BODY_TOO_LARGE_BODY, close=True)
            return

        if scope["method"] == "POST" and not envelope_is_valid(body):
            await _refuse(send, 400, INVALID_REQUEST_BODY, "application/json")
            return

        pending.appendleft(
            {"type": "http.request", "body": body, "more_body": not complete}
        )

        async def replay():
            if pending:
                return pending.popleft()
            return await receive()

        await self.app(_normalized(scope, host, origin), replay, _fixed_refusals(send))

    async def _read_body(self, receive):
        """Buffer the body under both timeouts, draining what is oversize.

        Returns ``(outcome, body, complete, pending)``: ``outcome`` is
        ``"ok"``, ``"oversize"`` or ``"timeout"``, and ``pending`` carries any
        non-body message (a disconnect) that must still reach the SDK.
        """
        body = bytearray()
        pending = deque()
        received = 0
        oversize = False
        complete = False
        # Read the limits per request so an operator-visible module constant
        # stays the single source of truth even when it is patched at runtime.
        deadline = time.monotonic() + BODY_TOTAL_TIMEOUT_SECONDS
        while True:
            wait = min(BODY_IDLE_TIMEOUT_SECONDS, deadline - time.monotonic())
            if wait <= 0:
                return "timeout", b"", False, pending
            try:
                async with asyncio.timeout(wait):
                    message = await receive()
            except TimeoutError:
                return "timeout", b"", False, pending
            if message["type"] != "http.request":
                pending.append(message)
                break
            chunk = message.get("body", b"")
            received += len(chunk)
            if received > self.max_body:
                # Keep reading, but stop keeping: the body is already refused
                # and only the caller's completion still matters.
                oversize = True
                body.clear()
            else:
                body.extend(chunk)
            if not message.get("more_body", False):
                complete = True
                break
            if oversize and received >= MAX_DRAIN_BYTES:
                break
        if oversize:
            return "oversize", b"", complete, pending
        return "ok", bytes(body), complete, pending


def _normalized(scope, host, origin):
    """The scope with its authorities lowercased for the SDK's own check.

    The guard has already validated both headers case-insensitively; the SDK
    compares them literally, so it is handed the canonical spelling rather than
    a second, stricter rule the printed URL could fail.
    """
    if (host is None or host == host.lower()) and (
            origin is None or origin == origin.lower()):
        return scope
    headers = []
    for name, raw in scope["headers"]:
        if name in (b"host", b"origin"):
            raw = raw.decode("latin-1").lower().encode("latin-1")
        headers.append((name, raw))
    return {**scope, "headers": headers}


def _fixed_refusals(send):
    """Replace any ``400`` the SDK produces with the guard's fixed body.

    The SDK's JSON-RPC schema reports what it rejected, including the caller's
    own tokens. The guard refuses the same envelopes first; this keeps the
    answer identical if the two ever disagree.
    """
    replacing = False

    async def wrapped(message):
        nonlocal replacing
        if message["type"] == "http.response.start":
            if message["status"] == 400:
                replacing = True
                await _start(send, 400, INVALID_REQUEST_BODY, "application/json")
                await send(
                    {"type": "http.response.body", "body": INVALID_REQUEST_BODY}
                )
                return
        elif replacing and message["type"] == "http.response.body":
            return
        await send(message)

    return wrapped


async def _start(send, status, body, media_type, extra=()):
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", f"{media_type}; charset=utf-8".encode()),
                (b"content-length", str(len(body)).encode()),
                *extra,
            ],
        }
    )


async def _refuse(send, status, body, media_type="text/plain", *, close=False, extra=()):
    """Answer with a fixed body that never restates the caller's input."""
    headers = list(extra)
    if close:
        # The caller may still be mid-send; say the connection is finished so
        # it reads this answer instead of discovering a closed socket.
        headers.append((b"connection", b"close"))
    await _start(send, status, body, media_type, headers)
    await send({"type": "http.response.body", "body": body})


class ProtocolErrorSanitizer:
    """Reduce protocol- and dispatch-level failures to fixed messages.

    Installed on the HTTP server only. The SDK answers an unknown method with
    the method it was sent, an unknown tool with its name, and a rejected
    argument set with the offending key; over HTTP the peer is any local
    process, so each becomes a fixed message instead. Successful results —
    including the tools' own structured error payloads, which never raise
    across the MCP boundary — pass through untouched.
    """

    def __init__(self, declared_parameters: dict):
        self._declared = declared_parameters

    async def __call__(self, ctx, call_next):
        from mcp.shared.exceptions import MCPError
        from pydantic import ValidationError

        if ctx.request_id is None:
            # A notification has no reply to sanitize.
            return await call_next(ctx)
        try:
            result = await call_next(ctx)
        except MCPError as exc:
            # The code is the answer; the SDK's ``data`` is the caller's own
            # method name or the parser's reading of its tokens.
            raise MCPError(
                code=exc.error.code, message=_fixed_message(exc.error.code)
            ) from None
        except ValidationError:
            raise MCPError(
                code=_INVALID_PARAMS, message=_fixed_message(_INVALID_PARAMS)
            ) from None
        except Exception:
            # Anything the dispatcher did not classify would otherwise reach
            # the peer as its own text. Cancellation is a BaseException and
            # still propagates.
            raise MCPError(
                code=_INTERNAL_ERROR, message=_fixed_message(_INTERNAL_ERROR)
            ) from None
        if ctx.method == "tools/call" and isinstance(result, dict):
            if result.get("isError") or result.get("is_error"):
                sanitized = dict(result)
                sanitized["content"] = [
                    {"type": "text", "text": self._dispatch_message(ctx)}
                ]
                sanitized.pop("structuredContent", None)
                return sanitized
        return result

    def _dispatch_message(self, ctx):
        """The fixed reply for a call the dispatcher refused.

        Only two things reach here: a tool this server does not register, and
        an argument set its schema rejected. Everything the tools themselves
        classify is returned as an ordinary result by their own guard.
        """
        params = ctx.params if isinstance(ctx.params, dict) else {}
        name = params.get("name")
        declared = self._declared.get(name) if isinstance(name, str) else None
        if declared is None:
            return UNKNOWN_TOOL_MESSAGE
        if not declared:
            return f"Invalid arguments for {name}; this tool accepts no arguments"
        return (
            f"Invalid arguments for {name}; this tool accepts "
            f"{', '.join(declared)}"
        )


def _fixed_message(code: int) -> str:
    return FIXED_ERROR_MESSAGES.get(code, DEFAULT_FIXED_ERROR_MESSAGE)


def declared_parameters(server) -> dict:
    """Each registered tool's schema-declared parameter names, in order."""
    declared = {}
    for tool in server._tool_manager.list_tools():
        properties = (tool.parameters or {}).get("properties") or {}
        declared[tool.name] = tuple(properties)
    return declared


def build_app(server, port: int, literal: str | None = None):
    """The guarded, stateless streamable HTTP application for ``server``."""
    from mcp.server.transport_security import TransportSecuritySettings

    authorities = allowed_authorities(port, literal)
    installed = server._lowlevel_server.middleware
    if not any(isinstance(item, ProtocolErrorSanitizer) for item in installed):
        # Innermost, so it sees the dispatcher's own failures before any
        # observing middleware records them.
        installed.append(ProtocolErrorSanitizer(declared_parameters(server)))
    app = server.streamable_http_app(
        streamable_http_path="/mcp",
        stateless_http=True,
        max_request_body_size=MAX_BODY_BYTES,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True,
            allowed_hosts=list(authorities),
            allowed_origins=[f"http://{item}" for item in authorities],
        ),
    )
    return LocalRequestGuard(app, port=port, literal=literal)


def bind(spec: HostSpec, port: int) -> socket.socket:
    """Bind and listen before anything is announced, so the URL is live."""
    listener = socket.socket(spec.family, socket.SOCK_STREAM)
    try:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind((spec.address, port))
        listener.listen(socket.SOMAXCONN)
    except BaseException:
        listener.close()
        raise
    return listener


def serving_lines(spec: HostSpec, port: int) -> tuple:
    return (
        f"ads-mcp: serving MCP over HTTP at http://{spec.display}:{port}/mcp "
        "(read-only)",
        "ads-mcp: warning: this endpoint is unauthenticated — any local "
        "process can read the configured accounts through it",
    )


def _absorb_signal(signum, frame):
    """Absorb the signal uvicorn re-raises after its graceful shutdown.

    ``uvicorn.Server.capture_signals`` restores the previous handler and then
    re-raises the signal it handled, which would end an already clean shutdown
    with a signal status instead of exit 0.
    """


def _silence_library_logging():
    """Leave stderr to the two startup lines.

    The MCP SDK installs a root handler at INFO on construction and uvicorn
    reports every connection-level oddity, so a session of ordinary refusals
    would bury the operator's two lines in session, dispatch and shutdown
    chatter — including a cancellation traceback when a client is cut off
    mid-body. The operator's interface here is the printed URL, not a request
    log, so the libraries' handlers are removed and their records dropped.
    """
    import logging

    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    root.addHandler(logging.NullHandler())
    root.setLevel(logging.CRITICAL + 1)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access", "mcp", "asyncio"):
        quiet = logging.getLogger(name)
        quiet.handlers = [logging.NullHandler()]
        quiet.setLevel(logging.CRITICAL + 1)
        quiet.propagate = False
    logging.raiseExceptions = False


#: How long uvicorn may wait for in-flight requests on shutdown. A client that
#: is holding a connection open mid-body must not keep the operator's terminal
#: hostage, so the wait is short enough to land well inside a five-second
#: expectation for SIGINT/SIGTERM.
SHUTDOWN_GRACE_SECONDS = 2


def serve(server, listener: socket.socket, literal: str | None = None) -> int:
    """Serve until SIGINT/SIGTERM, with the libraries' logging left off."""
    import signal

    import uvicorn

    port = listener.getsockname()[1]
    config = uvicorn.Config(
        build_app(server, port, literal),
        loop="asyncio",
        log_config=None,
        log_level="critical",
        access_log=False,
        # A local endpoint has no reason to advertise its server version.
        server_header=False,
        timeout_graceful_shutdown=SHUTDOWN_GRACE_SECONDS,
    )
    # After uvicorn's own Config has had its say about log levels.
    _silence_library_logging()
    restore = {
        sig: signal.signal(sig, _absorb_signal)
        for sig in (signal.SIGINT, signal.SIGTERM)
    }
    try:
        uvicorn.Server(config).run(sockets=[listener])
    finally:
        for sig, handler in restore.items():
            signal.signal(sig, handler)
    return 0
