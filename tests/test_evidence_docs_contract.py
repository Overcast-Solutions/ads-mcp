"""F036: preflight evidence honesty and generated/operator descriptions.

The approved rendered-image inspection is builder evidence, not an ambient
renderer/browser requirement for pytest. This module checks source artifact
syntax and actual tools/list; the driver must separately inspect the render.
"""
import contextlib
import io
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys

import pytest

import harness as h
from offline_contract import ROOT, load_script, stage
from tool_catalog import MUTATION_ARGS


@pytest.mark.parametrize("live", [False, True])
@pytest.mark.parametrize("inventory", ["missing_dir", "empty", "missing_tool", "duplicate", "unknown"])
def test_invalid_parity_inventory_refuses_before_configuration_or_server(tmp_path, monkeypatch, live, inventory):
    fixtures = tmp_path / "fixtures"
    if inventory != "missing_dir":
        fixtures.mkdir()
    if inventory not in ("missing_dir", "empty"):
        for path in h.contract_fixture_files():
            shutil.copy(path, fixtures / path.name)
        if inventory == "missing_tool":
            (fixtures / "health_check.json").unlink()
        elif inventory == "duplicate":
            shutil.copy(fixtures / "health_check.json", fixtures / "duplicate-health.json")
        else:
            data = json.loads((fixtures / "health_check.json").read_text())
            data["tool"] = "unknown_synthetic_tool"
            (fixtures / "unknown.json").write_text(json.dumps(data))
    touched = []
    def forbidden(*args, **kwargs):
        touched.append(True)
        raise AssertionError("parity touched configuration/server before fixture inventory validation")
    monkeypatch.setattr(h, "build_server", forbidden)
    monkeypatch.setattr(h, "build_config", forbidden)
    monkeypatch.setattr(h.config_mod, "load_config", forbidden)
    monkeypatch.setattr(h.server_mod, "create_server", forbidden)
    script = load_script("parity")
    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = script.main(["--fixtures", str(fixtures)] + (["--live"] if live else []))
    except SystemExit as exc:
        code = exc.code
    except Exception as exc:
        pytest.fail(f"parity preflight raised {type(exc).__name__} instead of concise unsuccessful status")
    assert code not in (None, 0) and not touched
    message = out.getvalue() + err.getvalue()
    assert "fixture" in message.lower() and "ALL MATCH" not in message and "Traceback" not in message
    assert len(message) < 4000


@pytest.mark.parametrize("live", [False, True])
def test_missing_fixture_cli_exits_unsuccessfully_without_traceback(tmp_path, live):
    result = subprocess.run([sys.executable, str(ROOT / "scripts/parity.py"), "--fixtures", str(tmp_path / "absent")]
                 + (["--live"] if live else []), cwd=tmp_path, env=h.scrubbed_env(), text=True, capture_output=True, timeout=10)
    assert result.returncode != 0
    text = result.stdout + result.stderr
    assert "fixture" in text.lower() and "Traceback" not in text and "GOOGLE_ADS_DEVELOPER_TOKEN" not in text


def test_generated_parameter_tables_have_exactly_four_columns_and_union_text():
    rendered = load_script("gen_tools_md").render()
    assert rendered == (ROOT / "docs/tools.md").read_text()
    rows = [line for line in rendered.splitlines() if line.startswith("|")]
    assert rows
    for line in rows:
        assert len(re.split(r"(?<!\\)\|", line)) == 6, f"parameter row does not have four cells: {line}"
    section = rendered.split("### `add_negative_keywords`", 1)[1].split("### ", 1)[0]
    assert re.search(r"array(?:<|&lt;)string\s*(?:\\\||&#124;|&vert;)\s*object(?:>|&gt;)", section)


def test_read_only_docs_match_accepted_spelling_policy(tmp_path):
    text = (ROOT / "docs/configuration.md").read_text().lower()
    assert "only the literal" not in text
    assert all(re.search(r"\b" + word + r"\b", text) for word in ("false", "0", "no", "off"))
    assert "case-insensitive" in text or "case insensitive" in text
    assert "unknown" in text and "empty" in text
    for value in ("false", "0", "no", "off", "FALSE", "OFF"):
        assert h.build_config(tmp_path, {"ADS_MCP_READ_ONLY": value, "ADS_MCP_AUDIT_LOG": str(tmp_path / "audit.jsonl")}).read_only is False
    for value in ("", "banana"):
        assert h.build_config(tmp_path, {"ADS_MCP_READ_ONLY": value}).read_only is True


def test_catalog_enable_and_schedule_describe_actual_effects(tmp_path, account_client):
    server = h.build_rw_server(tmp_path, client=account_client)
    catalog = h.tool_map(server)
    enable = catalog["enable_entity"].description.lower()
    assert "spend-neutral" not in enable and "spend" in enable and "budget" in enable
    schedule = catalog["set_campaign_schedule"].description.lower()
    plan = stage(server, "set_campaign_schedule", MUTATION_ARGS["set_campaign_schedule"])
    for text in (schedule, json.dumps(plan).lower()):
        assert "add" in text and "replace" in text
        assert any(term in text for term in ("does not", "doesn't", "without", "not replace"))
