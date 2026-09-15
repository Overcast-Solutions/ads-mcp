"""F002 — Auth layer: structured credential errors, never a generic failure."""

import json
import logging
import re

from google.auth.exceptions import RefreshError

import harness


def _revoked_error():
    return RefreshError(
        "invalid_grant: Token has been expired or revoked. "
        f"refresh_token={harness.FAKE_REFRESH_TOKEN} "
        f"client_secret={harness.FAKE_CLIENT_SECRET}",
        {"error": "invalid_grant"},
    )


def test_invalid_grant_maps_to_auth_token_revoked(tmp_path, account_client):
    account_client.stub_error(_revoked_error())
    server = harness.build_server(tmp_path, client=account_client)
    err = harness.expect_error(
        server,
        "run_gaql",
        {"query": "SELECT campaign.id FROM campaign"},
        code="AUTH_TOKEN_REVOKED",
    )
    assert re.search(r"re-?auth|regenerat|generate-token|oauth", err["message"], re.I), (
        f"AUTH_TOKEN_REVOKED must name the re-auth step: {err['message']}"
    )


def test_missing_credential_files_are_auth_config_errors(tmp_path, account_client):
    """File problems are AUTH_CONFIG_*, distinct from AUTH_TOKEN_REVOKED.

    client=None forces the server to reach its real credential loading.
    """
    for var, filename in [
        ("GOOGLE_ADS_CREDENTIALS_PATH", "missing_client.json"),
        ("GOOGLE_ADS_TOKEN_PATH", "missing_refresh.json"),
    ]:
        env = {var: str(tmp_path / filename)}
        server = harness.build_server(tmp_path, client=None, env=env)
        err = harness.error_of(
            harness.call(server, "run_gaql", {"query": "SELECT campaign.id FROM campaign"})
        )
        assert err["code"].startswith("AUTH_CONFIG_"), (
            f"expected AUTH_CONFIG_* for a missing file, got {err['code']}"
        )
        assert err["code"] != "AUTH_TOKEN_REVOKED"


def test_auth_errors_never_retried(tmp_path, account_client):
    account_client.stub_error(_revoked_error())
    server = harness.build_server(
        tmp_path, client=account_client, env={"ADS_MCP_RETRY_BASE_SECONDS": "0.01"}
    )
    harness.expect_error(
        server,
        "run_gaql",
        {"query": "SELECT campaign.id FROM campaign"},
        code="AUTH_TOKEN_REVOKED",
    )
    assert len(account_client.searches) == 1, (
        f"auth failure was retried: {len(account_client.searches)} attempts"
    )


def test_no_secret_material_in_responses_or_logs(tmp_path, account_client, caplog):
    account_client.stub_error(_revoked_error())
    server = harness.build_server(tmp_path, client=account_client)
    with caplog.at_level(logging.DEBUG):
        for tool, args in [
            ("health_check", {}),
            ("run_gaql", {"query": "SELECT campaign.id FROM campaign"}),
        ]:
            result = harness.call_result(server, tool, args)
            harness.assert_no_secrets(harness.result_text(result))
    harness.assert_no_secrets("\n".join(r.getMessage() for r in caplog.records))


def test_no_secret_material_on_success_path(tmp_path, account_client):
    server = harness.build_server(tmp_path, client=account_client)
    result = harness.call_result(server, "health_check", {})
    text = harness.result_text(result)
    harness.assert_no_secrets(text)
    payload = harness.payload_of(result)
    assert payload["config"]["developer_token"] == "present", (
        "health must report token presence, never the token value"
    )
