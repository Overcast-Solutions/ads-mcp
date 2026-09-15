"""Offline installed-archive smoke check. Run with python -I outside source."""

import asyncio
import importlib.metadata
import json
from pathlib import Path
import socket
import subprocess
import sys

import ads_mcp
import mcp
from ads_mcp.config import Config
from ads_mcp.server import create_server


def no_network(*args, **kwargs):
    raise RuntimeError("Installed catalog checks must not contact a provider")


async def catalog(read_only):
    config = Config(
        developer_token="", customer_id="0000000000", login_customer_id=None,
        credentials_path="", token_path="", read_only=read_only,
        require_dry_run=True, max_daily_budget=None, max_bid_increase_pct=None,
        max_first_bid=None, audit_log=None, retry_base_seconds=1.0,
        plan_ttl_seconds=900, row_limit=1000,
    )
    async with mcp.Client(create_server(config, client=object())) as client:
        result = await client.list_tools()
    names = sorted(tool.name for tool in result.tools)
    assert len(names) == (22 if read_only else 53)
    assert ("confirm_and_apply" in names) is (not read_only)
    return names


def main():
    module = Path(ads_mcp.__file__).resolve()
    assert Path(sys.prefix).resolve() in module.parents, "repository import fallback"
    distribution = importlib.metadata.distribution("ads-mcp")
    assert distribution.metadata["License-Expression"] == "Apache-2.0"
    for name, args in (("ads-mcp", ["--version"]),
                       ("ads-mcp", ["--help"]),
                       ("ads-mcp-generate-token", ["--help"]),
                       ("ads-mcp-export-source", ["--help"])):
        subprocess.run([str(Path(sys.executable).with_name(name)), *args], check=True)
    socket.create_connection = no_network
    socket.socket.connect = no_network
    read_names = asyncio.run(catalog(True))
    write_names = asyncio.run(catalog(False))
    print(json.dumps({"version": distribution.version,
                      "installed_module": str(module),
                      "read_catalog": read_names, "write_catalog": write_names}))


if __name__ == "__main__":
    main()
