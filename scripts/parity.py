#!/usr/bin/env python3
"""Check ads-mcp reads against this project's expected-output fixtures.

Offline (default): replays every golden contract fixture through a real
server instance over a recorded-fixture transport and reports MATCH/DRIFT
per read tool. Needs no credentials and no network — safe anywhere.

Live (--live): runs the same read surface against the real Google Ads API
using GOOGLE_ADS_* credentials from the environment. Reads only; still, it
spends API quota and requires real credentials — operator-run, consumed at
the deployment review as a live read smoke check. This does not compare
another implementation or prove behavioral equivalence.
"""

from __future__ import annotations

import argparse
from contextlib import nullcontext
import json
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tests"))

SEARCH_URL_READS = frozenset({"get_responsive_search_ad_urls", "get_keyword_urls"})
SHARED_READS = frozenset({"list_shared_negative_keyword_lists", "get_shared_negative_keyword_list"})
DEMO_READS = frozenset({"get_demographic_targeting"})


def fixture_paths(fixtures_dir: Path | None) -> list[Path]:
    """Select an explicit inventory or all 30 authored read fixtures."""
    directories = ([fixtures_dir] if fixtures_dir is not None else
                   [REPO / "tests/fixtures/contract", REPO / "tests/fixtures/pmax"])
    if any(not directory.is_dir() for directory in directories):
        raise ValueError("fixture directory does not exist or is not a directory")
    paths = [path for directory in directories for path in directory.glob("*.json")]
    if fixtures_dir is None:
        paths.extend(REPO / "tests/fixtures/search_urls" / (name + ".json")
                     for name in sorted(SEARCH_URL_READS))
        paths.extend(REPO / "tests/fixtures/shared_targeting" / (name + ".json")
                     for name in sorted(SHARED_READS | DEMO_READS))
    return sorted(paths)


def validate_fixture_inventory(fixtures_dir: Path | None) -> None:
    """Accept complete baseline or complete merged inventories before server setup."""
    from tool_catalog import READ_TOOLS
    from pmax_oracle import PMAX_READS

    paths = fixture_paths(fixtures_dir)
    if not paths:
        raise ValueError("fixture directory contains no JSON fixtures")
    seen = set()
    for path in paths:
        try:
            fixture = json.loads(path.read_text())
        except (OSError, ValueError):
            raise ValueError("fixture inventory contains unreadable or invalid JSON") from None
        tool = fixture.get("tool") if isinstance(fixture, dict) else None
        if not isinstance(tool, str) or tool not in READ_TOOLS | PMAX_READS | SEARCH_URL_READS | SHARED_READS | DEMO_READS:
            raise ValueError("fixture inventory contains an unknown or missing tool identity")
        if tool in seen:
            raise ValueError(f"fixture inventory contains duplicate tool: {tool}")
        seen.add(tool)
    required = READ_TOOLS
    if fixtures_dir is None or seen & (PMAX_READS | SEARCH_URL_READS | SHARED_READS | DEMO_READS):
        required = required | PMAX_READS
    if fixtures_dir is None or seen & (SEARCH_URL_READS | SHARED_READS | DEMO_READS):
        required = required | SEARCH_URL_READS
    if fixtures_dir is None or seen & (SHARED_READS | DEMO_READS):
        required = required | SHARED_READS
    if fixtures_dir is None or seen & DEMO_READS:
        required = required | DEMO_READS
    missing = required - seen
    if missing:
        raise ValueError("fixture inventory is missing tools: " + ", ".join(sorted(missing)))


def offline_report(fixtures_dir: Path | None, out) -> int:
    import harness  # tests/harness.py — the recorded-fixture transport

    failures = 0
    with tempfile.TemporaryDirectory() as tmp:
        for path in fixture_paths(fixtures_dir):
            fixture = harness.load_contract_fixture(path)
            client = harness.FakeGoogleAdsClient()
            for resource, rows in fixture["gaql"].items():
                client.stub(resource, rows)
            for method, response in fixture.get("planner", {}).items():
                client.stub_planner(method, harness.planner_response(response))
            env = {"ADS_MCP_AUDIT_LOG": str(harness.audit_file(tmp))}
            env.update(fixture.get("env", {}))
            server = harness.build_server(
                tmp, client=client, env=env, clock=harness.FakeClock()
            )
            payload = harness.call(server, fixture["tool"], fixture["args"])
            base_env = harness.google_ads_env(tmp)
            golden = harness.substitute_placeholders(
                fixture["golden"],
                {
                    "CREDENTIALS_PATH": base_env["GOOGLE_ADS_CREDENTIALS_PATH"],
                    "TOKEN_PATH": base_env["GOOGLE_ADS_TOKEN_PATH"],
                    "AUDIT_LOG": env["ADS_MCP_AUDIT_LOG"],
                },
            )
            status = "MATCH" if payload == golden else "DRIFT"
            if status == "DRIFT":
                failures += 1
            print(f"{fixture['tool']:32s} {status}", file=out)
            if status == "DRIFT":
                print(f"  got:    {json.dumps(payload)[:400]}", file=out)
                print(f"  golden: {json.dumps(golden)[:400]}", file=out)
    print(
        f"\n{'ALL MATCH' if failures == 0 else f'{failures} DRIFTED'} "
        f"across {len(fixture_paths(fixtures_dir))} read-tool fixtures",
        file=out,
    )
    return 1 if failures else 0


def live_report(fixtures_dir: Path | None, out) -> int:
    import os

    from ads_mcp.config import load_config
    from ads_mcp.server import create_server

    import harness

    config = load_config(os.environ)
    server = create_server(config)
    print(f"live read-surface sweep against customer {config.customer_id}", file=out)
    failures = 0
    for path in fixture_paths(fixtures_dir):
        fixture = harness.load_contract_fixture(path)
        try:
            payload = harness.call(server, fixture["tool"], fixture["args"])
            status = "ERROR" if "error" in payload else "OK"
        except Exception as exc:  # noqa: BLE001 — report, don't crash the sweep
            payload = {"error": {"code": "EXCEPTION", "message": str(exc)}}
            status = "ERROR"
        if status == "ERROR":
            failures += 1
        print(f"{fixture['tool']:32s} {status}", file=out)
        if status == "ERROR":
            print(f"  {json.dumps(payload.get('error'))[:400]}", file=out)
    return 1 if failures else 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="parity.py",
        description="ads-mcp own-fixture read report (offline fixture "
        "replay by default).",
    )
    inventory = parser.add_mutually_exclusive_group()
    inventory.add_argument(
        "--fixtures",
        help="directory containing the complete 21-read baseline, 25-read PMax inventory "
        "or the 27-read Search URL, 29-read shared-list or 30-read targeting inventory; default combines all project read fixtures",
    )
    inventory.add_argument(
        "--all-fixtures", action="store_true",
        help="combine all 30 contract, PMax, Search URL and targeting fixtures (the default)",
    )
    parser.add_argument(
        "--report", default="-", help="report path, or - for stdout"
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help="run against the REAL Google Ads API instead of fixtures; "
        "requires real GOOGLE_ADS_* credentials in the environment and "
        "spends API quota (reads only). Operator-run.",
    )
    args = parser.parse_args(argv)
    fixtures_dir = Path(args.fixtures) if args.fixtures is not None else None
    try:
        validate_fixture_inventory(fixtures_dir)
    except (OSError, ValueError) as exc:
        print(f"parity.py: fixture preflight failed: {exc}", file=sys.stderr)
        return 1
    try:
        output = nullcontext(sys.stdout) if args.report == "-" else open(args.report, "w")
        with output as out:
            if args.live:
                result = live_report(fixtures_dir, out)
            else:
                result = offline_report(fixtures_dir, out)
            out.flush()
        return result
    except OSError:
        print(
            "parity.py: PARITY_REPORT_WRITE_FAILED: unable to save the report; "
            "choose a writable report path and check its parent directory "
            "and permissions.",
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
