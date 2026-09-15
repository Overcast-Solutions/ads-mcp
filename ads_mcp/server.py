"""Server assembly and console entry.

``create_server`` wires config, auth, transport, tool registry, guardrails,
and audit into an ``mcp.server.mcpserver.MCPServer`` on stdio. ``client``,
``plan_store``, and ``clock`` are injection seams used by the locked oracle.
"""

from __future__ import annotations

import importlib.metadata
import os
import sys
import time

from mcp.server.mcpserver import MCPServer

from ads_mcp import auth
from ads_mcp.config import Config, ConfigError, load_config
from ads_mcp.context import ServerContext
from ads_mcp.tools import registry


def _package_version():
    try:
        return importlib.metadata.version("ads-mcp")
    except importlib.metadata.PackageNotFoundError:
        return "unknown"


def create_server(config: Config, *, client=None, plan_store=None, clock=None):
    server = MCPServer(
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
