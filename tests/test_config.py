"""F001 — Server boots from env config and fails fast, loudly, on bad config."""

import json

import pytest

import harness
from ads_mcp.config import ConfigError, load_config, normalize_customer_id
from tool_catalog import MUTATION_TOOLS, APPLY_TOOL

REQUIRED_VARS = [
    "GOOGLE_ADS_CUSTOMER_ID",
    "GOOGLE_ADS_CREDENTIALS_PATH",
    "GOOGLE_ADS_TOKEN_PATH",
]


def test_pure_google_ads_env_boots_read_only(tmp_path, account_client):
    """Standard Google Ads variables without ADS_MCP_* must serve read-only."""
    server = harness.build_server(tmp_path, client=account_client)
    names = harness.tool_names(server)
    assert names, "server registered no tools"
    assert not (names & MUTATION_TOOLS), (
        "mutation tools registered without explicit ADS_MCP_READ_ONLY=false"
    )
    assert APPLY_TOOL not in names


def test_boot_on_stdio_serves_tools_list(tmp_path):
    """The installed `ads-mcp` binary must speak MCP over real stdio."""
    proc, messages = harness.stdio_tools_list(harness.google_ads_env(tmp_path))
    assert proc.returncode == 0, (
        f"ads-mcp exited {proc.returncode}\nstderr: {proc.stderr[-2000:]}"
    )
    by_id = {m.get("id"): m for m in messages if isinstance(m, dict)}
    assert 1 in by_id and "result" in by_id[1], f"no initialize result: {messages}"
    assert 2 in by_id and "result" in by_id[2], f"no tools/list result: {messages}"
    tools = by_id[2]["result"].get("tools", [])
    assert tools, "tools/list returned an empty catalog"
    for tool in tools:
        assert tool.get("description"), f"tool {tool.get('name')} has empty description"


@pytest.mark.parametrize("var", REQUIRED_VARS)
def test_missing_required_env_fails_fast_named(tmp_path, var):
    env = harness.google_ads_env(tmp_path)
    del env[var]
    proc = harness.run_console("ads-mcp", env_overlay=env, timeout=30)
    out = proc.stdout + proc.stderr
    assert proc.returncode != 0, "server must exit nonzero on missing required env"
    assert var in out, f"startup error must name {var}; got: {out[-800:]}"
    assert "e.g." in out or "example" in out.lower(), (
        f"startup error must include an example value for {var}; got: {out[-800:]}"
    )
    assert "Traceback" not in out, f"raw traceback leaked to the operator: {out[-800:]}"


@pytest.mark.parametrize("var", REQUIRED_VARS)
def test_load_config_missing_var_raises_named(tmp_path, var):
    env = harness.google_ads_env(tmp_path)
    del env[var]
    with pytest.raises(ConfigError) as exc:
        load_config(env)
    assert var in str(exc.value)


@pytest.mark.parametrize("bad_id", ["garbage!!", "12-34", "abc", "123456789012", ""])
def test_malformed_customer_id_rejected_with_value_quoted(tmp_path, bad_id):
    env = harness.google_ads_env(tmp_path)
    env["GOOGLE_ADS_CUSTOMER_ID"] = bad_id
    with pytest.raises(ConfigError) as exc:
        load_config(env)
    if bad_id:
        assert bad_id in str(exc.value), (
            f"rejection must quote the offending value {bad_id!r}: {exc.value}"
        )


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("123-456-7890", "1234567890"),
        ("1234567890", "1234567890"),
        (" 987-654-3210 ", "9876543210"),
    ],
)
def test_customer_id_normalization(raw, expected):
    assert normalize_customer_id(raw) == expected


@pytest.mark.parametrize("garbage", ["12ab34cd56", "123", "12345678901", "1-2-3"])
def test_customer_id_garbage_rejected(garbage):
    with pytest.raises(ConfigError) as exc:
        normalize_customer_id(garbage)
    assert garbage.strip() in str(exc.value)


def test_dashed_env_ids_normalized_in_health_config(tmp_path, account_client):
    """Dashed account identifiers must normalize before use."""
    server = harness.build_server(tmp_path, client=account_client)
    payload = harness.expect_ok(harness.call(server, "health_check"))
    assert payload["config"]["customer_id"] == harness.CUSTOMER_ID
    assert payload["config"]["login_customer_id"] == harness.LOGIN_CUSTOMER_ID


def test_login_customer_id_is_optional(tmp_path, account_client):
    env = harness.google_ads_env(tmp_path)
    del env["GOOGLE_ADS_LOGIN_CUSTOMER_ID"]
    cfg = load_config(env)
    import ads_mcp.server as server_mod

    server = server_mod.create_server(
        cfg, client=account_client, clock=harness.FakeClock()
    )
    payload = harness.expect_ok(harness.call(server, "health_check"))
    assert payload["config"]["login_customer_id"] is None


def test_guardrail_vars_optional_with_safe_defaults(tmp_path, account_client):
    """No ADS_MCP_* set: read-only on, dry-run required — visible in health."""
    server = harness.build_server(tmp_path, client=account_client)
    payload = harness.expect_ok(harness.call(server, "health_check"))
    guardrails = payload["guardrails"]
    assert guardrails["read_only"] is True
    assert guardrails["require_dry_run"] is True


def test_every_registered_tool_has_description(tmp_path, account_client):
    server = harness.build_server(tmp_path, client=account_client)
    for name, tool in harness.tool_map(server).items():
        assert tool.description and tool.description.strip(), (
            f"tool {name} has an empty description"
        )
