"""Server assembly and console entry.

``create_server`` wires config, auth, transport, tool registry, guardrails,
and audit into an ``mcp.server.mcpserver.MCPServer`` on stdio. ``client``,
``plan_store``, and ``clock`` are injection seams used by the locked oracle.
"""

from __future__ import annotations

import importlib.metadata
import json
import os
import sys
import time
from contextlib import asynccontextmanager

import anyio
from anyio.abc import ObjectReceiveStream
from mcp.server import stdio as sdk_stdio
from mcp.server.mcpserver import MCPServer
from mcp.server.stdio import stdio_server
from mcp.shared.message import SessionMessage
from mcp_types import ErrorData, JSONRPCError

from ads_mcp import auth
from ads_mcp.config import Config, ConfigError, load_config
from ads_mcp.context import ServerContext
from ads_mcp.tools import registry


class _RequestIdInput:
    """Inspect original ID types before the SDK can choose a notification."""

    def __init__(self, incoming):
        self.incoming = incoming

    async def __aiter__(self):
        async for line in self.incoming:
            try:
                # Only token types matter here. Avoid converting integer tokens
                # or imposing Python's decimal conversion limit on SDK input.
                envelope = json.loads(line, parse_int=lambda _: 0,
                                      parse_float=lambda _: 0.0)
            except (ValueError, RecursionError):
                # The SDK parser and refusal stream handle malformed JSON.
                yield line
                continue
            if (isinstance(envelope, dict) and "method" in envelope
                    and "id" in envelope
                    and type(envelope["id"]) not in (str, int)):
                # An invalid envelope reaches the existing generic refusal path.
                # Never forward the untrusted ID or turn it into a notification.
                yield "{}\n"
            else:
                yield line


@asynccontextmanager
async def _request_id_input():
    # The SDK has no raw-envelope hook. Reuse its input claim and non-owning
    # wrapper so this preflight keeps descriptor diversion and restoration.
    # stdio_server still owns its normal output claim and serialized writer.
    buffer, release = sdk_stdio._claim_fd(
        0, sys.stdin, "rb", sdk_stdio._open_stdin_diversion,
    )
    try:
        incoming = anyio.wrap_file(sdk_stdio._UnownedTextWrapper(
            buffer, encoding="utf-8", errors="replace",
        ))
        yield _RequestIdInput(incoming)
    finally:
        if release is not None:
            release()


class _ProtocolReadStream(ObjectReceiveStream):
    """Refuse malformed envelopes before session dispatch can log their input."""

    def __init__(self, incoming, outgoing):
        self.incoming = incoming
        self.outgoing = outgoing

    @property
    def last_context(self):
        return getattr(self.incoming, "last_context", None)

    async def receive(self):
        while True:
            item = await self.incoming.receive()
            if not isinstance(item, Exception):
                return item
            # The parser did not establish an envelope or trustworthy ID.
            # Keep the original input and exception entirely off the wire.
            await self.outgoing.send(SessionMessage(JSONRPCError(
                jsonrpc="2.0", id=None,
                error=ErrorData(code=-32600, message="Invalid JSON-RPC request"),
            )))

    async def aclose(self):
        await self.incoming.aclose()


class _StdioServer(MCPServer):
    async def run_stdio_async(self):
        # Retain the SDK transport's stream ownership and serialized output.
        async with _request_id_input() as stdin, stdio_server(stdin=stdin) as streams:
            incoming, outgoing = streams
            await self._lowlevel_server.run(
                _ProtocolReadStream(incoming, outgoing), outgoing,
                self._lowlevel_server.create_initialization_options(),
            )


def _package_version():
    try:
        return importlib.metadata.version("ads-mcp")
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def create_server(config: Config, *, client=None, plan_store=None, clock=None):
    server = _StdioServer(
        name="ads-mcp",
        version=_package_version(),
        description=(
            "Google Ads MCP server built for autonomous agents: full tool "
            "surface behind server-side spend guardrails."
        ),
    )
    resolved_clock = clock if clock is not None else time.time
    if plan_store is None and not config.read_only:
        from ads_mcp.guardrails import PlanStore

        plan_store = PlanStore(
            ttl_seconds=config.plan_ttl_seconds, clock=resolved_clock
        )
    audit = None
    if config.audit_log:
        from ads_mcp.audit import AuditLog

        audit = AuditLog(config.audit_log, clock=resolved_clock)
    ctx = ServerContext(
        config=config,
        injected_client=client,
        plan_store=plan_store,
        clock=resolved_clock,
        secrets=auth.collect_secrets(config),
        audit=audit,
    )
    registry.register_tools(server, ctx)
    return server


def main(argv=None):
    import argparse

    parser = argparse.ArgumentParser(
        prog="ads-mcp",
        description=(
            "ads-mcp: a Google Ads MCP server built for autonomous agents — "
            "full tool surface behind server-side spend guardrails. Serves "
            "MCP over stdio; configure via GOOGLE_ADS_* and ADS_MCP_* "
            "environment variables (see README)."
        ),
    )
    parser.add_argument("--version", action="version",
                        version=f"ads-mcp {_package_version()}")
    parser.parse_args(argv)

    try:
        config = load_config(os.environ)
    except ConfigError as exc:
        print(f"ads-mcp: configuration error: {exc}", file=sys.stderr)
        return 2
    try:
        server = create_server(config)
        server.run()
    except KeyboardInterrupt:
        return 0
    except ConfigError as exc:
        print(f"ads-mcp: configuration error: {exc}", file=sys.stderr)
        return 2
    return 0
