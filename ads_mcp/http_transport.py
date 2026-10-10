"""Local, read-only, stateless MCP streamable HTTP for the installed console.

``ads-mcp --transport http`` serves the same read tools, schemas and audit
behavior as stdio over an HTTP endpoint bound to literal loopback only. The
endpoint is unauthenticated by design, so it is refused unless the effective
configuration is read-only, and every guard below runs *before* the MCP SDK
parses anything:

* ``Host`` must be one of the three canonical loopback authorities carrying the
  bound port (``421`` otherwise); ``Origin``, when present, must be the ``http``
  form of one of them (``403``; a missing ``Origin`` is allowed).
* request bodies over 1 MiB are refused (``413``), including chunked bodies
  that only exceed the limit while streaming.
* a POST body that is not a single JSON-RPC object with an absent or
  ``str``/``int`` id is refused with one fixed body (``400``).

No refusal echoes the request back to the caller or to the log.
"""

from __future__ import annotations

import json
import re
import socket
from collections import deque

#: Canonical loopback host specifications accepted by ``--host``.
LOOPBACK_RULE = (
    "canonical literal loopback only: 127.x.y.z (four decimal octets without "
    "leading zeros), ::1 (no brackets or zone id), or localhost"
)
PORT_RULE = "a decimal port from 0 to 65535 without sign, whitespace or leading zeros"

MAX_BODY_BYTES = 1024 * 1024
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
    resolved, so a poisoned resolver cannot move the endpoint off loopback.
    """
    if value.lower() == "localhost":
        return HostSpec(socket.AF_INET, "127.0.0.1", value)
    if value == "::1":
        return HostSpec(socket.AF_INET6, "::1", "[::1]")
    parts = value.split(".")
    if len(parts) == 4 and all(_OCTET.fullmatch(part) for part in parts):
        octets = [int(part) for part in parts]
        if octets[0] == 127 and all(octet <= 255 for octet in octets):
            return HostSpec(socket.AF_INET, value, value)
    raise OptionError(f"--host {value!r} is not accepted: {LOOPBACK_RULE}")


def resolve_port(value: str) -> int:
    """Validate ``--port`` as plain ASCII digits in the TCP port range."""
    if not _PORT.fullmatch(value) or int(value) > 65535:
        raise OptionError(f"--port {value!r} is not accepted: {PORT_RULE}")
    return int(value)


def allowed_authorities(port: int) -> tuple:
    """The only ``Host`` authorities this endpoint answers for."""
    return (f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}")


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

    A batch, a scalar, invalid UTF-8, a lone surrogate or an id that is not a
    string or integer (including ``null``, booleans and any float) is refused
    here. An absent id is a notification and is accepted.
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
        # stdio preflight does.
        envelope = json.loads(
            text, parse_int=lambda _: 0, parse_float=lambda _: 0.0
        )
    except (ValueError, RecursionError):
        return False
    if _has_lone_surrogate(envelope):
        return False
    if not isinstance(envelope, dict):
        return False
    if "id" in envelope and type(envelope["id"]) not in (str, int):
        return False
    return True


class LocalRequestGuard:
    """ASGI guard in front of the SDK app: authority, size and envelope.

    The body is buffered once and replayed to the wrapped application, so the
    SDK still sees a normal ASGI request.
    """

    def __init__(self, app, *, port: int, max_body: int = MAX_BODY_BYTES):
        self.app = app
        self.hosts = frozenset(allowed_authorities(port))
        self.origins = frozenset(f"http://{item}" for item in self.hosts)
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
        if host not in self.hosts:
            await _refuse(scope, receive, send, 421, b"Invalid Host header")
            return
        if origin is not None and origin not in self.origins:
            await _refuse(scope, receive, send, 403, b"Invalid Origin header")
            return
        for name, raw in scope["headers"]:
            if name == b"content-length" and raw.isdigit():
                if int(raw) > self.max_body:
                    await _refuse(scope, receive, send, 413, b"Request body too large")
                    return
                break

        body = bytearray()
        pending = deque()
        complete = False
        while True:
            message = await receive()
            if message["type"] != "http.request":
                pending.append(message)
                break
            body.extend(message.get("body", b""))
            if len(body) > self.max_body:
                await _refuse(scope, receive, send, 413, b"Request body too large")
                return
            if not message.get("more_body", False):
                complete = True
                break

        if scope["method"] == "POST" and not envelope_is_valid(bytes(body)):
            await _refuse(
                scope, receive, send, 400, INVALID_REQUEST_BODY, "application/json"
            )
            return

        pending.appendleft(
            {"type": "http.request", "body": bytes(body), "more_body": not complete}
        )

        async def replay():
            if pending:
                return pending.popleft()
            return await receive()

        await self.app(scope, replay, send)


async def _refuse(scope, receive, send, status, body, media_type="text/plain"):
    """Answer with a fixed body that never restates the caller's input."""
    await send(
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", f"{media_type}; charset=utf-8".encode()),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


def build_app(server, port: int):
    """The guarded, stateless streamable HTTP application for ``server``."""
    from mcp.server.transport_security import TransportSecuritySettings

    authorities = allowed_authorities(port)
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
    return LocalRequestGuard(app, port=port)


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


def serve(server, listener: socket.socket) -> int:
    """Serve until SIGINT/SIGTERM, with uvicorn's own logging left off."""
    import logging
    import signal

    import uvicorn

    # Access logging would echo every request line into the operator's
    # terminal; the startup/shutdown chatter is replaced by the two lines
    # above. Genuine warnings and errors still reach stderr.
    logging.getLogger("uvicorn").setLevel(logging.WARNING)
    port = listener.getsockname()[1]
    config = uvicorn.Config(
        build_app(server, port),
        loop="asyncio",
        log_config=None,
        access_log=False,
        # A local endpoint has no reason to advertise its server version.
        server_header=False,
        timeout_graceful_shutdown=10,
    )
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
