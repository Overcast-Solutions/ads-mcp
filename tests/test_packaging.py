"""F015 — Packaging and docs: clone to first tool call without reading source."""

import importlib.metadata
import re
import subprocess
import sys
from pathlib import Path

import harness
from tool_catalog import ALL_WRITE_MODE_TOOLS

REPO = Path(__file__).resolve().parent.parent

ENV_VARS_DOCUMENTED = [
    "GOOGLE_ADS_DEVELOPER_TOKEN",
    "GOOGLE_ADS_CUSTOMER_ID",
    "GOOGLE_ADS_LOGIN_CUSTOMER_ID",
    "GOOGLE_ADS_CREDENTIALS_PATH",
    "GOOGLE_ADS_TOKEN_PATH",
    "ADS_MCP_READ_ONLY",
    "ADS_MCP_REQUIRE_DRY_RUN",
    "ADS_MCP_MAX_DAILY_BUDGET",
    "ADS_MCP_MAX_BID_INCREASE_PCT",
    "ADS_MCP_MAX_FIRST_BID",
    "ADS_MCP_AUDIT_LOG",
    "ADS_MCP_PLAN_TTL_SECONDS",
    "ADS_MCP_ROW_LIMIT",
    "ADS_MCP_RETRY_BASE_SECONDS",
]


def test_console_entry_point_help(tmp_path):
    proc = harness.run_console("ads-mcp", args=["--help"], timeout=30)
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, f"ads-mcp --help exited {proc.returncode}: {out[-500:]}"
    assert "ads-mcp" in out
    assert "Traceback" not in out


def test_console_version_matches_package(tmp_path):
    proc = harness.run_console("ads-mcp", args=["--version"], timeout=30)
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, f"ads-mcp --version exited {proc.returncode}: {out[-500:]}"
    assert importlib.metadata.version("ads-mcp") in out


def test_generate_token_helper_ships_and_is_documented(tmp_path):
    proc = harness.run_console("ads-mcp-generate-token", args=["--help"], timeout=30)
    out = proc.stdout + proc.stderr
    assert proc.returncode == 0, (
        f"ads-mcp-generate-token --help exited {proc.returncode}: {out[-500:]}"
    )
    assert re.search(r"refresh[ _-]?token|oauth", out, re.I), (
        f"the helper must explain what it produces: {out[:400]}"
    )
    readme = (REPO / "README.md").read_text()
    assert "ads-mcp-generate-token" in readme, "README must document the token helper"


def test_readme_is_a_complete_setup_guide():
    readme = (REPO / "README.md").read_text()
    for var in ENV_VARS_DOCUMENTED:
        assert var in readme, f"README env-var table is missing {var}"
    assert "mcpServers" in readme or "claude mcp add" in readme, (
        "README must include a Claude Code registration snippet"
    )
    assert re.search(r"oauth", readme, re.I), "README must cover OAuth setup"
    assert re.search(r"read[- ]only", readme, re.I) and re.search(
        r"rollout|cutover|parallel", readme, re.I
    ), "README must give read-only rollout guidance"


def test_docs_tools_md_generated_from_registry_and_current():
    doc_path = REPO / "docs" / "tools.md"
    assert doc_path.exists(), "docs/tools.md is missing"
    doc = doc_path.read_text()
    for name in sorted(ALL_WRITE_MODE_TOOLS):
        assert name in doc, f"docs/tools.md is missing tool {name}"
    generator = REPO / "scripts" / "gen_tools_md.py"
    assert generator.exists(), "scripts/gen_tools_md.py (the doc generator) is missing"
    proc = subprocess.run(
        [sys.executable, str(generator), "--stdout"],
        capture_output=True,
        text=True,
        timeout=60,
        cwd=REPO,
    )
    assert proc.returncode == 0, f"generator failed: {proc.stderr[-500:]}"
    assert proc.stdout.strip() == doc.strip(), (
        "docs/tools.md has drifted from the registry — regenerate it"
    )


def test_license_and_changelog_present():
    license_path = REPO / "LICENSE"
    assert license_path.exists(), "LICENSE file is missing (MIT per decisions/0002)"
    text = license_path.read_text()
    assert "MIT" in text and "Permission is hereby granted" in text
    changelog = REPO / "CHANGELOG.md"
    assert changelog.exists(), "CHANGELOG.md is missing"
    assert changelog.read_text().strip(), "CHANGELOG.md is empty"
