"""F034: public error envelopes, synthetic OAuth shapes and call attribution."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import queue
import subprocess
import sys
import time

import pytest
from google.api_core.exceptions import ServiceUnavailable
from google.auth.exceptions import RefreshError

import harness as h
from ads_mcp import auth, server as server_mod
from ads_mcp.errors import AuthConfigError
from offline_contract import apply, audited, refusal, stage

WINDOW_TOOLS = ["get_campaign_performance", "get_ad_performance", "get_keyword_performance",
               "get_search_terms", "get_geo_performance", "get_shopping_performance"]
BAD_WINDOWS = [{}, {"last_n_days": 0}, {"last_n_days": -1},
               {"date_range_start": "2026-02-30", "date_range_end": "2026-03-01"},
               {"date_range_start": "2026-08-01", "date_range_end": "2026-07-01"}]


@pytest.mark.parametrize("tool", WINDOW_TOOLS)
@pytest.mark.parametrize("window", BAD_WINDOWS)
def test_date_domain_failures_are_structured_before_account_calls(tmp_path, account_client, tool, window):
    server = h.build_server(tmp_path, client=account_client)
    args = {**window, **({"campaign_id": "111"} if tool == "get_shopping_performance" else {})}
    result = h.call_result(server, tool, args)
    assert not result.is_error, "domain validation escaped the registered structured error boundary"
    err = h.error_of(h.payload_of(result))
    assert err["code"] == "INVALID_WINDOW"
    assert not account_client.searches


@pytest.mark.parametrize("tool", WINDOW_TOOLS)
def test_framework_wrong_window_types_stay_framework_errors(tmp_path, account_client, tool):
    server = h.build_server(tmp_path, client=account_client)
    args = {"last_n_days": "not-an-integer", **({"campaign_id": "111"} if tool == "get_shopping_performance" else {})}
    assert h.call_result(server, tool, args).is_error
    assert not account_client.searches


BAD_OAUTH = [
    ([], {"refresh_token": h.FAKE_REFRESH_TOKEN}),
    ({"installed": "wrong shape"}, {"refresh_token": h.FAKE_REFRESH_TOKEN}),
    ({"web": 123}, {"refresh_token": h.FAKE_REFRESH_TOKEN}),
    ({"installed": {"client_id": " ", "client_secret": h.FAKE_CLIENT_SECRET}}, {"refresh_token": h.FAKE_REFRESH_TOKEN}),
    ({"installed": {"client_id": 1234, "client_secret": h.FAKE_CLIENT_SECRET}}, {"refresh_token": h.FAKE_REFRESH_TOKEN}),
    ({"client_id": "synthetic-client", "client_secret": [h.FAKE_CLIENT_SECRET]}, {"refresh_token": h.FAKE_REFRESH_TOKEN}),
    ({"client_id": "synthetic-client", "client_secret": h.FAKE_CLIENT_SECRET}, []),
    ({"client_id": "synthetic-client", "client_secret": h.FAKE_CLIENT_SECRET}, {"refresh_token": "\u2003"}),
    ({"client_id": "synthetic-client", "client_secret": h.FAKE_CLIENT_SECRET}, {"refresh_token": {"planted": h.FAKE_REFRESH_TOKEN}}),
    ({}, {}),
]


def oauth_config(tmp_path, credentials, token):
    cfg = h.build_config(tmp_path)
    Path(cfg.credentials_path).write_text(json.dumps(credentials))
    Path(cfg.token_path).write_text(json.dumps(token))
    return cfg


@pytest.mark.parametrize("credentials,token", BAD_OAUTH)
def test_normal_oauth_loader_rejects_shapes_with_named_error(tmp_path, credentials, token):
    cfg = oauth_config(tmp_path, credentials, token)
    try:
        auth.load_oauth_material(cfg)
    except AuthConfigError as exc:
        assert exc.code.startswith("AUTH_CONFIG_")
        h.assert_no_secrets(exc.message)
    except Exception as exc:
        pytest.fail(f"OAuth shape escaped as {type(exc).__name__}, expected AUTH_CONFIG_*")
    else:
        pytest.fail("malformed OAuth shape was accepted as usable credentials")


@pytest.mark.parametrize("credentials,token", BAD_OAUTH)
def test_normal_server_health_reports_config_invalid_without_network(tmp_path, credentials, token, monkeypatch):
    import socket
    from google.ads.googleads.client import GoogleAdsClient
    network = []
    def denied(*args, **kwargs):
        network.append(True)
        raise RuntimeError("OFFLINE_ORACLE_NETWORK_FORBIDDEN")
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(socket, "create_connection", denied)
    # gRPC's C transport bypasses Python sockets. Stop at the public client
    # factory too; invalid material must be rejected before SDK construction.
    monkeypatch.setattr(GoogleAdsClient, "load_from_dict", denied)
    cfg = oauth_config(tmp_path, credentials, token)
    try:
        server = server_mod.create_server(cfg, clock=h.FakeClock())
    except AuthConfigError as exc:
        assert exc.code.startswith("AUTH_CONFIG_")
        return
    except Exception as exc:
        pytest.fail(f"server startup escaped with {type(exc).__name__} for malformed credential shape")
    payload = h.call(server, "health_check")
    assert payload.get("status") == "CONFIG_INVALID", payload
    assert payload["credentials"]["code"].startswith("AUTH_CONFIG_")
    h.assert_no_secrets(json.dumps(payload))
    assert not network


def test_best_effort_secrets_never_accept_nonscalar_scrub_values(tmp_path):
    cfg = oauth_config(tmp_path, {"installed": {"client_secret": [h.FAKE_CLIENT_SECRET]}}, {"refresh_token": {"x": h.FAKE_REFRESH_TOKEN}})
    try:
        secrets = auth.collect_secrets(cfg)
        result = secrets.scrub("ordinary operator message")
    except Exception as exc:
        pytest.fail(f"best-effort secret collection/scrubbing crashed: {type(exc).__name__}")
    assert result == "ordinary operator message"


@pytest.mark.parametrize("wrapper", [None, "installed", "web"])
@pytest.mark.parametrize("fallback", [False, True])
def test_valid_oauth_objects_and_token_fallback_remain_supported(tmp_path, wrapper, fallback):
    values = {"client_id": "synthetic-client", "client_secret": h.FAKE_CLIENT_SECRET}
    credentials = {} if fallback else values
    if wrapper:
        credentials = {wrapper: credentials}
    token = {"refresh_token": h.FAKE_REFRESH_TOKEN, **(values if fallback else {})}
    cfg = oauth_config(tmp_path, credentials, token)
    assert auth.load_oauth_material(cfg) == {**values, "refresh_token": h.FAKE_REFRESH_TOKEN}


@pytest.mark.parametrize("entry", ["console", "module"])
@pytest.mark.parametrize("bad_file", ["credentials", "token"])
def test_installed_startup_never_prints_traceback_for_nonobject_json(tmp_path, entry, bad_file):
    env = h.google_ads_env(tmp_path)
    path = env["GOOGLE_ADS_CREDENTIALS_PATH" if bad_file == "credentials" else "GOOGLE_ADS_TOKEN_PATH"]
    Path(path).write_text("[]")
    env["PYTHONPATH"] = str(Path(__file__).parent / "fixtures/offline_startup_guard")
    command = [str(h.console_script())] if entry == "console" else [sys.executable, "-m", "ads_mcp"]
    frames = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "oracle", "version": "0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "health_check", "arguments": {}}},
    ]
    proc = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, cwd=tmp_path, env=h.scrubbed_env(env))
    lines, received = [], queue.Queue()
    def receive():
        try:
            for line in proc.stdout:
                lines.append(line)
                try:
                    received.put(json.loads(line))
                except json.JSONDecodeError:
                    continue
        finally:
            received.put(None)  # EOF: an early concise startup failure is allowed.
    def await_response(identity):
        deadline = time.monotonic() + 5
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                pytest.fail(f"installed startup did not respond to MCP id {identity}")
            try:
                message = received.get(timeout=remaining)
            except queue.Empty:
                pytest.fail(f"installed startup did not respond to MCP id {identity}")
            if message is None:
                return False
            if message.get("id") == identity:
                return True
    def send(messages):
        proc.stdin.write("".join(json.dumps(frame) + "\n" for frame in messages))
        proc.stdin.flush()
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(receive)
            try:
                try:
                    send(frames[:1])
                    if await_response(1):
                        send(frames[1:])
                        await_response(2)
                except BrokenPipeError:
                    pass
                # Keep the pipe open until the health response or confirmed
                # process EOF, rather than cancelling an in-flight SDK call.
                proc.stdin.close()
                proc.wait(timeout=5)
                future.result(timeout=3)
            finally:
                if proc.poll() is None:
                    proc.terminate()
                    proc.wait(timeout=3)
        stderr = proc.stderr.read()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=3)
    text = "".join(lines) + stderr
    assert "Traceback" not in text and "AttributeError" not in text
    assert "OFFLINE_ORACLE_NETWORK_FORBIDDEN" not in text
    h.assert_no_secrets(text)
    assert "AUTH_CONFIG_" in text
    assert proc.returncode != 0 or "CONFIG_INVALID" in text


@pytest.mark.parametrize("kind", ["dry_run", "irreversible", "expired", "unknown", "recheck"])
def test_every_apply_refusal_audit_has_supplied_plan_id(tmp_path, account_client, kind):
    clock = h.FakeClock()
    server = h.build_rw_server(tmp_path, client=account_client, clock=clock,
             env={"ADS_MCP_REQUIRE_DRY_RUN": "true" if kind == "dry_run" else "false"})
    args = {"campaign_id": "111", "daily_budget": 80} if kind == "recheck" else {"entity_type": "campaign", "entity_id": "111"}
    tool = "update_campaign" if kind == "recheck" else "remove_entity" if kind == "irreversible" else "pause_entity"
    plan = stage(server, tool, args)
    if kind == "expired":
        clock.advance(901)
    elif kind == "unknown":
        plan = {"id": "unknown-synthetic-refusal"}
    elif kind == "recheck":
        account_client.stub("campaign", [])
    error = refusal(apply(server, plan, confirm_irreversible=False), account_client)
    audited(tmp_path, error, plan_id=plan["id"], tool="confirm_and_apply", customer_id=h.CUSTOMER_ID)


def test_concurrent_refusals_and_later_reads_do_not_reuse_plan_attribution(tmp_path, account_client):
    server = h.build_rw_server(tmp_path, client=account_client, env={"ADS_MCP_REQUIRE_DRY_RUN": "true", "ADS_MCP_RETRY_BASE_SECONDS": "0.001"})
    plans = [stage(server, "pause_entity", {"entity_type": "campaign", "entity_id": str(cid)}) for cid in (111, 222)]
    async def run(connection):
        results = await asyncio.gather(*(connection.call_tool("confirm_and_apply", {"plan_id": p["id"], "dry_run": False}) for p in plans))
        return [h.payload_of(r) for r in results]
    for p, result in zip(plans, h.session_run(server, run)):
        audited(tmp_path, h.error_of(result), plan_id=p["id"])
    account_client.stub_error(ServiceUnavailable("synthetic retry"), times=1)
    h.expect_ok(h.call(server, "get_account_info", {"customer_id": h.OTHER_CUSTOMER_ID}))
    retry = next(r for r in h.read_audit_records(tmp_path) if r["event"] == "retry")
    assert not retry.get("plan_id") and retry["customer_id"] == h.OTHER_CUSTOMER_ID


def test_failed_authenticated_health_probe_emits_one_sanitized_event(tmp_path, account_client):
    account_client.stub_error(RefreshError(h.FAKE_REFRESH_TOKEN))
    server = h.build_rw_server(tmp_path, client=account_client)
    payload = h.call(server, "health_check")
    assert payload["status"] == "AUTH_DEAD" and payload["credentials"]["code"] == "AUTH_TOKEN_REVOKED"
    records = [r for r in h.read_audit_records(tmp_path) if r["event"] == "auth_failure"]
    assert len(records) == 1 and records[0]["tool"] == "health_check"
    assert records[0]["customer_id"] == h.CUSTOMER_ID
    h.assert_no_secrets(json.dumps(records) + json.dumps(payload))


@pytest.mark.parametrize("tool,args", [("run_gaql", {"query": "SELECT customer.id FROM customer"}), ("health_check", {})])
def test_retry_audit_uses_registered_name(tmp_path, account_client, tool, args):
    account_client.stub_error(ServiceUnavailable("synthetic transient"), times=1)
    server = h.build_rw_server(tmp_path, client=account_client, env={"ADS_MCP_RETRY_BASE_SECONDS": "0.001"})
    h.expect_ok(h.call(server, tool, args))
    records = [r for r in h.read_audit_records(tmp_path) if r["event"] == "retry"]
    assert len(records) == 1 and records[0]["tool"] == tool
