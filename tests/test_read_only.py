"""F014 — Read-only mode: the rollout switch."""

import pytest

import harness
from pmax_oracle import assert_catalog
from tool_catalog import ALL_WRITE_MODE_TOOLS, APPLY_TOOL, MUTATION_TOOLS, READ_TOOLS

WINDOW_ARGS = {"date_range_start": "2026-07-01", "date_range_end": "2026-07-31"}


@pytest.mark.parametrize(
    "value",
    [None, "", "true", "TRUE", "1", "yes", "banana"],
    ids=["absent", "empty", "true", "TRUE", "1", "yes", "junk"],
)
def test_read_only_unless_explicitly_false(tmp_path, account_client, value):
    """Fail-safe default: only an explicit false enables mutations."""
    env = {} if value is None else {"ADS_MCP_READ_ONLY": value}
    server = harness.build_server(tmp_path, client=account_client, env=env)
    names = harness.tool_names(server)
    assert_catalog(names, read_only=True)


@pytest.mark.parametrize("value", ["false", "False", "FALSE"])
def test_explicit_false_registers_the_full_catalog(tmp_path, account_client, value):
    server = harness.build_server(
        tmp_path,
        client=account_client,
        env={
            "ADS_MCP_READ_ONLY": value,
            "ADS_MCP_AUDIT_LOG": str(harness.audit_file(tmp_path)),
        },
    )
    assert_catalog(harness.tool_names(server))


def test_mutation_tools_are_unregistered_not_refused(tmp_path, account_client):
    server = harness.build_server(tmp_path, client=account_client)
    for tool in sorted(MUTATION_TOOLS | {APPLY_TOOL}):
        result = harness.call_result(server, tool, {})
        assert result.is_error, f"{tool} answered in read-only mode"
        text = harness.result_text(result)
        assert "unknown tool" in text.lower(), (
            f"{tool} must be UNREGISTERED (unknown tool), not merely refused: {text[:200]}"
        )


def test_reads_behave_identically_in_read_only(tmp_path, account_client):
    ro = harness.build_server(tmp_path, client=account_client)
    ro_payload = harness.expect_ok(
        harness.call(ro, "get_campaign_performance", dict(WINDOW_ARGS))
    )
    rw_client = harness.stub_standard_account(harness.FakeGoogleAdsClient())
    rw = harness.build_rw_server(tmp_path, client=rw_client)
    rw_payload = harness.expect_ok(
        harness.call(rw, "get_campaign_performance", dict(WINDOW_ARGS))
    )
    assert ro_payload == rw_payload, "read results differ between modes"


def test_health_reports_read_only_state(tmp_path, account_client):
    ro = harness.build_server(tmp_path, client=account_client)
    assert harness.call(ro, "health_check")["guardrails"]["read_only"] is True
    rw_client = harness.stub_standard_account(harness.FakeGoogleAdsClient())
    rw = harness.build_rw_server(tmp_path, client=rw_client)
    assert harness.call(rw, "health_check")["guardrails"]["read_only"] is False
