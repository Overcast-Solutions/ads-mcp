"""F004 — Truthful authenticated health probe."""

import json

from google.api_core import exceptions as core_exceptions
from google.auth.exceptions import RefreshError

import harness


def test_health_ok_only_via_real_authenticated_read(tmp_path, account_client):
    server = harness.build_server(tmp_path, client=account_client)
    payload = harness.expect_ok(harness.call(server, "health_check"))
    assert payload["status"] == "OK"
    probes = [s for s in account_client.searches if "customer.id" in s.query]
    assert probes, (
        "health_check must execute a real GAQL read selecting customer.id; "
        f"queries seen: {account_client.queries()}"
    )


def test_health_auth_dead_on_revoked_credentials(tmp_path, account_client):
    account_client.stub_error(
        RefreshError("invalid_grant: Token has been expired or revoked.")
    )
    server = harness.build_server(tmp_path, client=account_client)
    payload = harness.call(server, "health_check")
    assert payload["status"] == "AUTH_DEAD", (
        f"health said {payload.get('status')!r} with dead credentials"
    )
    assert "AUTH_TOKEN_REVOKED" in json.dumps(payload["credentials"]), (
        f"credentials section must carry the AUTH_TOKEN_REVOKED detail: {payload['credentials']}"
    )


def test_health_never_ok_from_config_presence_alone(tmp_path, account_client):
    """Valid files with an unavailable API must not produce healthy status."""
    account_client.stub_error(
        core_exceptions.ServiceUnavailable("gRPC UNAVAILABLE: hard down")
    )
    server = harness.build_server(
        tmp_path, client=account_client, env={"ADS_MCP_RETRY_BASE_SECONDS": "0.01"}
    )
    payload = harness.call(server, "health_check")
    assert payload["status"] != "OK"


def test_health_has_three_named_sections(tmp_path, account_client):
    server = harness.build_server(tmp_path, client=account_client)
    payload = harness.expect_ok(harness.call(server, "health_check"))
    for section in ("config", "credentials", "guardrails"):
        assert isinstance(payload.get(section), dict), f"missing section {section!r}"
    guardrails = payload["guardrails"]
    for key in ("read_only", "require_dry_run", "max_daily_budget", "audit_log"):
        assert key in guardrails, f"guardrails section missing {key!r}"
    assert payload["config"]["customer_id"] == harness.CUSTOMER_ID


def test_health_failure_classes_distinguished(tmp_path):
    statuses = {}

    client = harness.stub_standard_account(harness.FakeGoogleAdsClient())
    client.stub_error(RefreshError("invalid_grant: revoked"))
    server = harness.build_server(tmp_path, client=client)
    statuses["auth"] = harness.call(server, "health_check")["status"]

    client = harness.stub_standard_account(harness.FakeGoogleAdsClient())
    client.stub_error(core_exceptions.ServiceUnavailable("gRPC UNAVAILABLE"))
    server = harness.build_server(
        tmp_path, client=client, env={"ADS_MCP_RETRY_BASE_SECONDS": "0.01"}
    )
    statuses["transport"] = harness.call(server, "health_check")["status"]

    server = harness.build_server(
        tmp_path,
        client=None,
        env={"GOOGLE_ADS_CREDENTIALS_PATH": str(tmp_path / "nope.json")},
    )
    statuses["config"] = harness.call(server, "health_check")["status"]

    assert statuses["auth"] == "AUTH_DEAD"
    assert statuses["transport"] == "TRANSPORT_FAILED"
    assert statuses["config"] == "CONFIG_INVALID"
    assert len(set(statuses.values())) == 3, f"failure classes collapsed: {statuses}"
