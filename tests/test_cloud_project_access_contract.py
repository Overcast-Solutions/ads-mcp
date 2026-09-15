"""F060: Cloud-project access through genuine SDK metadata and MCP startup.

No Ads/OAuth network. Synthetic OAuth credentials replace only the SDK OAuth
refresh factory. Real client construction, generated Search/SearchStream,
Google interceptors and grpc.intercept_channel execute up to the channel edge.
"""
import json
import socket
from datetime import datetime, timedelta, timezone

import grpc
import pytest
from google.ads.googleads import oauth2
from google.ads.googleads.errors import GoogleAdsException
from google.ads.googleads.v25.services.services.google_ads_service.transports.grpc import GoogleAdsServiceGrpcTransport
from google.oauth2.credentials import Credentials
from google.protobuf.json_format import ParseDict

import harness as h
from ads_mcp import auth
from ads_mcp.config import ConfigError, load_config


@pytest.mark.parametrize("token", [None, "", "   ", h.FAKE_DEVELOPER_TOKEN])
def test_optional_legacy_token_allows_valid_config_and_installed_startup(tmp_path, token):
    env = h.google_ads_env(tmp_path)
    if token is None:
        env.pop("GOOGLE_ADS_DEVELOPER_TOKEN")
    else:
        env["GOOGLE_ADS_DEVELOPER_TOKEN"] = token
    try:
        cfg = load_config(env)
    except ConfigError as exc:
        pytest.fail(f"otherwise valid Cloud-project configuration still requires a developer token: {exc}")
    assert cfg.read_only and cfg.require_dry_run
    assert cfg.customer_id == h.CUSTOMER_ID and cfg.login_customer_id == h.LOGIN_CUSTOMER_ID
    # Startup is lazy: listing tools requires no OAuth refresh or provider read.
    (tmp_path / "sitecustomize.py").write_text('''
import socket
from google.ads.googleads import oauth2
def blocked(*a, **kw):
    raise AssertionError("ORACLE_NETWORK_FORBIDDEN")
socket.socket.connect = blocked
socket.socket.connect_ex = blocked
socket.create_connection = blocked
oauth2.get_installed_app_credentials = blocked
''')
    env.update(PYTHONPATH=str(tmp_path), PYTHONDONTWRITEBYTECODE="1")
    proc, messages = h.stdio_tools_list(env)
    assert proc.returncode == 0 and "Traceback" not in proc.stdout + proc.stderr
    tools = next(m["result"]["tools"] for m in messages if m.get("id") == 2)
    assert {t["name"] for t in tools} == __import__("tool_catalog").READ_TOOLS
    h.assert_no_secrets(proc.stdout + proc.stderr)


class CapturedAtChannel(BaseException):
    """Stop after all real request/metadata adapters, before any network I/O."""


class OfflineChannel(grpc.Channel):
    def __init__(self):
        self.calls = []

    def unary_unary(self, method, *args, **kwargs):
        return self._callable(method, "unary")

    def unary_stream(self, method, *args, **kwargs):
        return self._callable(method, "stream")

    def _callable(self, method, kind):
        owner = self

        class Invocation:
            def __call__(self, request, timeout=None, metadata=None, **kwargs):
                owner.calls.append((kind, method, request, tuple(metadata or ())))
                raise CapturedAtChannel()

            with_call = __call__
            future = __call__

        return Invocation()

    def stream_unary(self, *args, **kwargs):
        raise AssertionError("unexpected streaming request")

    stream_stream = stream_unary

    def subscribe(self, *args, **kwargs):
        pass

    def unsubscribe(self, *args, **kwargs):
        pass

    def close(self):
        pass


@pytest.mark.parametrize("method,kind", [("search", "unary"), ("search_stream", "stream")])
@pytest.mark.parametrize("token", [None, "", "   ", h.FAKE_DEVELOPER_TOKEN])
def test_actual_sdk_metadata_omits_legacy_token_for_unary_and_streaming(tmp_path, monkeypatch, method, kind, token):
    def blocked(*args, **kwargs):
        raise AssertionError("real network is forbidden")

    monkeypatch.setattr(socket.socket, "connect", blocked)
    monkeypatch.setattr(socket.socket, "connect_ex", blocked)
    monkeypatch.setattr(socket, "create_connection", blocked)
    credentials = Credentials(token="synthetic-access-token", refresh_token=h.FAKE_REFRESH_TOKEN,
        token_uri="https://oauth2.googleapis.com/token", client_id="oracle.apps.googleusercontent.com",
        client_secret=h.FAKE_CLIENT_SECRET,
        expiry=datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(hours=1))
    monkeypatch.setattr(oauth2, "get_installed_app_credentials", lambda *a, **kw: credentials)
    channel = OfflineChannel()
    constructed = []

    def create_channel(*args, **kwargs):
        assert kwargs["credentials"] is credentials
        constructed.append(True)
        return channel

    monkeypatch.setattr(GoogleAdsServiceGrpcTransport, "create_channel", create_channel)
    env = h.google_ads_env(tmp_path)
    if token is None:
        env.pop("GOOGLE_ADS_DEVELOPER_TOKEN")
    else:
        env["GOOGLE_ADS_DEVELOPER_TOKEN"] = token
    client = auth.build_client(load_config(env))
    service = client.get_service("GoogleAdsService", version="v25")
    with pytest.raises(CapturedAtChannel):
        result = getattr(service, method)(customer_id=h.OTHER_CUSTOMER_ID,
            query="SELECT customer.id FROM customer", retry=None, timeout=1)
        list(result)
    assert constructed and len(channel.calls) == 1
    actual_kind, path, request, metadata = channel.calls[0]
    assert actual_kind == kind and ("SearchStream" if kind == "stream" else "Search") in str(path)
    assert request.customer_id == h.OTHER_CUSTOMER_ID
    assert request.query == "SELECT customer.id FROM customer"
    assert dict(metadata)["login-customer-id"] == h.LOGIN_CUSTOMER_ID
    assert not any(key.lower() == "developer-token" for key, value in metadata), (
        "real SDK metadata still transmits a developer-token header"
    )
    assert h.FAKE_DEVELOPER_TOKEN not in str(metadata)
    assert getattr(client, "use_cloud_org_for_api_access", False) is True


def authorization_failure(name):
    failure = h.get_ads_type("GoogleAdsFailure")
    ParseDict({"errors": [{"error_code": {"authorization_error": name},
        "message": "untrusted project diagnostic " + h.FAKE_REFRESH_TOKEN}]}, failure._pb)
    return GoogleAdsException(None, None, failure, "synthetic-request")


@pytest.mark.parametrize("tool,args", [
    ("run_gaql", {"query": "SELECT customer.id FROM customer"}),
    ("get_account_info", {}),
    ("discover_keywords", {"seed_keywords": ["synthetic"]}),
])
def test_explicit_v25_project_approval_error_has_specific_safe_guidance(tmp_path, account_client, tool, args):
    failure = authorization_failure("CLOUD_PROJECT_NOT_APPROVED_FOR_PRODUCTION")
    account_client.stub_error(failure)
    if tool == "discover_keywords":
        from types import SimpleNamespace
        def deny(*args, **kwargs):
            account_client.other_calls.append("denied")
            raise failure
        account_client._services["KeywordPlanIdeaService"] = SimpleNamespace(generate_keyword_ideas=deny)
    server = h.build_server(tmp_path, client=account_client)
    error = h.error_of(h.call(server, tool, args))
    assert error["code"] == "ADS_CLOUD_PROJECT_NOT_APPROVED"
    text = error["message"].lower()
    assert "cloud" in text and "console" in text and "project" in text
    assert "developer token" not in text and "developer-token" not in text
    assert "untrusted project diagnostic" not in text
    h.assert_no_secrets(json.dumps(error))
    assert len(account_client.searches) + len(account_client.other_calls) == 1


@pytest.mark.parametrize("authorization,expected", [
    ("ACTION_NOT_PERMITTED", "GAQL_ERROR"),
    ("USER_PERMISSION_DENIED", "ACCOUNT_NOT_ACCESSIBLE"),
    ("CUSTOMER_NOT_ENABLED", "ACCOUNT_NOT_ACCESSIBLE"),
])
def test_unrelated_authorization_is_not_mislabeled_project_approval(tmp_path, account_client, authorization, expected):
    account_client.stub_error(authorization_failure(authorization))
    server = h.build_server(tmp_path, client=account_client)
    error = h.error_of(h.call(server, "run_gaql", {"query": "SELECT customer.id FROM customer", "customer_id": h.OTHER_CUSTOMER_ID}))
    assert error["code"] == expected
    assert "ADS_CLOUD_PROJECT_NOT_APPROVED" not in json.dumps(error)
    h.assert_no_secrets(json.dumps(error))
    assert len(account_client.searches) == 1


def test_project_refusal_on_mutation_is_single_attempt_and_consumes_plan(tmp_path, account_client):
    from types import SimpleNamespace
    calls = []
    def deny(request):
        calls.append(request)
        raise authorization_failure("CLOUD_PROJECT_NOT_APPROVED_FOR_PRODUCTION")
    account_client._services["CampaignService"] = SimpleNamespace(mutate_campaigns=deny)
    server = h.build_rw_server(tmp_path, client=account_client)
    plan = h.expect_ok(h.call(server, "pause_entity", {"entity_type": "campaign", "entity_id": "111"}))["plan"]
    error = h.error_of(h.call(server, "confirm_and_apply", {"plan_id": plan["id"], "dry_run": False}))
    assert error["code"] == "ADS_CLOUD_PROJECT_NOT_APPROVED"
    assert len(calls) == 1
    h.expect_error(server, "confirm_and_apply", {"plan_id": plan["id"], "dry_run": False}, code="PLAN_CONSUMED")
    assert len(calls) == 1
    h.assert_no_secrets(h.audit_file(tmp_path).read_text())
