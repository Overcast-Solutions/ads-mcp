"""F016 — Contract fixtures and the parity script.

Every read tool has a golden-fixture contract test: recorded Ads API rows
in, EXACT tool payload out. The fixture set is the compatibility contract
for refactors — a shape change without a deliberate fixture update fails
here (and re-locking fixtures means returning to /spec)."""

import subprocess
import sys
from pathlib import Path

import pytest

import harness
from pmax_oracle import PMAX_FIXTURES, PMAX_READS, assert_catalog
from ads_mcp.tools.registry import all_tool_specs
from tool_catalog import READ_TOOLS

REPO = Path(__file__).resolve().parent.parent

FIXTURE_FILES = harness.contract_fixture_files()
assert FIXTURE_FILES, "tests/fixtures/contract is empty — the oracle is broken"


@pytest.mark.parametrize("path", FIXTURE_FILES, ids=lambda p: p.stem)
def test_read_tool_matches_golden_payload_exactly(tmp_path, path):
    fixture = harness.load_contract_fixture(path)
    client = harness.FakeGoogleAdsClient()
    for resource, rows in fixture["gaql"].items():
        client.stub(resource, rows)
    for method, response in (fixture.get("planner") or {}).items():
        client.stub_planner(method, harness.planner_response(response))
    env = {"ADS_MCP_AUDIT_LOG": str(harness.audit_file(tmp_path))}
    env.update(fixture.get("env", {}))
    server = harness.build_server(
        tmp_path, client=client, env=env, clock=harness.FakeClock()
    )
    payload = harness.call(server, fixture["tool"], fixture["args"])
    base_env = harness.google_ads_env(tmp_path)
    golden = harness.substitute_placeholders(
        fixture["golden"],
        {
            "CREDENTIALS_PATH": base_env["GOOGLE_ADS_CREDENTIALS_PATH"],
            "TOKEN_PATH": base_env["GOOGLE_ADS_TOKEN_PATH"],
            "AUDIT_LOG": env["ADS_MCP_AUDIT_LOG"],
        },
    )
    assert payload == golden, (
        f"{fixture['tool']} drifted from its golden contract fixture.\n"
        f"got:    {payload}\n"
        f"golden: {golden}"
    )


def test_every_registry_read_tool_has_a_contract_fixture():
    """Drift guard in both directions: a read tool without a fixture, or a
    fixture for a tool the registry no longer exposes, fails."""
    fixture_tools = {harness.load_contract_fixture(p)["tool"] for p in FIXTURE_FILES}
    registry_reads = {s.name for s in all_tool_specs() if s.kind == "read"}
    assert_catalog(registry_reads, read_only=True)
    approved_fixtures = {harness.load_contract_fixture(p)["tool"] for p in PMAX_FIXTURES.glob("*.json")}
    assert approved_fixtures == PMAX_READS
    search_url_fixtures = {
        path.name: harness.load_contract_fixture(path)["tool"]
        for path in (REPO / "tests" / "fixtures" / "search_urls").glob("*.json")
    }
    assert search_url_fixtures == {
        "get_responsive_search_ad_urls.json": "get_responsive_search_ad_urls",
        "get_keyword_urls.json": "get_keyword_urls",
    }
    assert registry_reads <= fixture_tools | approved_fixtures | set(search_url_fixtures.values())
    assert fixture_tools == READ_TOOLS, (
        f"registry reads: {sorted(registry_reads)}\n"
        f"fixtures:       {sorted(fixture_tools)}"
    )


def test_parity_script_runs_offline_against_fixtures():
    """scripts/parity.py must exercise the full read surface with NO live
    credentials and emit a side-by-side report."""
    script = REPO / "scripts" / "parity.py"
    assert script.exists(), "scripts/parity.py is missing"
    proc = subprocess.run(
        [sys.executable, str(script), "--fixtures", str(harness.CONTRACT_DIR), "--report", "-"],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=REPO,
        env=harness.scrubbed_env(),  # no GOOGLE_ADS_* / ADS_MCP_* at all
    )
    assert proc.returncode == 0, f"parity.py failed offline: {proc.stderr[-800:]}"
    for tool in sorted(READ_TOOLS):
        assert tool in proc.stdout, f"parity report is missing read tool {tool}"


def test_parity_script_documents_live_mode():
    script = REPO / "scripts" / "parity.py"
    assert script.exists(), "scripts/parity.py is missing"
    proc = subprocess.run(
        [sys.executable, str(script), "--help"],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=REPO,
    )
    assert proc.returncode == 0
    assert "--live" in proc.stdout, "parity.py must document its --live mode"
    assert "credential" in proc.stdout.lower(), (
        "the --live help text must warn that it needs real credentials"
    )
