"""F012 — Append-only audit log."""

import json
import os
import stat

from google.api_core import exceptions as core_exceptions
from google.auth.exceptions import RefreshError

import harness

EXPECTED_EVENTS = {"plan_created", "dry_run", "applied", "refused", "retry", "auth_failure"}


def _full_lifecycle(tmp_path, account_client):
    """Drive plan_created + dry_run + applied + refused + retry + auth_failure
    against one audit file, then return its parsed records."""
    server = harness.build_rw_server(
        tmp_path, client=account_client, env={"ADS_MCP_RETRY_BASE_SECONDS": "0.01"}
    )
    # plan_created -> dry_run -> applied
    plan = harness.expect_ok(
        harness.call(server, "update_campaign", {"campaign_id": "111", "daily_budget": 80.0})
    )["plan"]
    harness.expect_ok(
        harness.call(server, "confirm_and_apply", {"plan_id": plan["id"], "dry_run": True})
    )
    harness.expect_ok(
        harness.call(server, "confirm_and_apply", {"plan_id": plan["id"], "dry_run": False})
    )
    # refused (over cap)
    harness.expect_error(
        server,
        "update_campaign",
        {"campaign_id": "111", "daily_budget": 150.0},
        code="BUDGET_CAP_EXCEEDED",
    )
    # retry (one transient blip, then success)
    account_client.stub_error(
        core_exceptions.ServiceUnavailable("gRPC UNAVAILABLE: blip"), times=1
    )
    harness.expect_ok(
        harness.call(server, "run_gaql", {"query": "SELECT campaign.id FROM campaign"})
    )
    # auth_failure
    account_client.stub_error(
        RefreshError(f"invalid_grant: revoked refresh_token={harness.FAKE_REFRESH_TOKEN}")
    )
    harness.expect_error(
        server,
        "run_gaql",
        {"query": "SELECT campaign.id FROM campaign"},
        code="AUTH_TOKEN_REVOKED",
    )
    return plan, harness.read_audit_records(tmp_path)


def test_every_lifecycle_event_lands_as_one_jsonl_record(tmp_path, account_client):
    plan, records = _full_lifecycle(tmp_path, account_client)
    events = {r.get("event") for r in records}
    missing = EXPECTED_EVENTS - events
    assert not missing, f"audit log is missing events {missing}; got {sorted(events)}"
    for record in records:
        ts = harness.parse_iso_utc(record["ts"])
        assert ts is not None
        assert record.get("tool"), f"record without tool name: {record}"
        assert record.get("customer_id") == harness.CUSTOMER_ID, record
        assert "outcome" in record, f"record without outcome: {record}"
    plan_events = [r for r in records if r["event"] in ("plan_created", "dry_run", "applied")]
    assert plan_events, "no plan lifecycle records"
    for record in plan_events:
        assert record.get("plan_id") == plan["id"], (
            f"plan lifecycle record missing plan id: {record}"
        )


def test_applied_record_carries_operation_summary(tmp_path, account_client):
    _plan, records = _full_lifecycle(tmp_path, account_client)
    applied = [r for r in records if r["event"] == "applied"]
    assert applied, "no applied record"
    text = json.dumps(applied[0])
    assert "daily_budget" in text, f"applied record must name the changed field: {text}"
    assert "80" in text, f"applied record must carry the new value: {text}"
    assert "50" in text, f"applied record must carry the old value when known: {text}"


def test_audit_records_never_contain_secrets(tmp_path, account_client):
    _plan, _records = _full_lifecycle(tmp_path, account_client)
    harness.assert_no_secrets(harness.audit_file(tmp_path).read_text())


def test_audit_write_failure_fails_mutation_closed_reads_continue(tmp_path, account_client):
    """A change that cannot be audited is not applied."""
    blocked = tmp_path / "blocked-audit.jsonl"
    blocked.write_text("")
    os.chmod(blocked, 0)  # unwritable
    try:
        server = harness.build_rw_server(
            tmp_path, client=account_client, env={"ADS_MCP_AUDIT_LOG": str(blocked)}
        )
        result = harness.call_result(
            server, "update_campaign", {"campaign_id": "111", "daily_budget": 80.0}
        )
        blocked_apply = False
        if result.is_error:
            blocked_apply = True
        else:
            payload = harness.payload_of(result)
            if "error" in payload:
                assert payload["error"]["code"] == "AUDIT_WRITE_FAILED", payload["error"]
                blocked_apply = True
            else:
                # Plan creation may be tolerated; the APPLY must fail closed.
                harness.expect_error(
                    server,
                    "confirm_and_apply",
                    {"plan_id": payload["plan"]["id"], "dry_run": False},
                    code="AUDIT_WRITE_FAILED",
                )
                blocked_apply = True
        assert blocked_apply
        assert account_client.live_mutations() == [], (
            "a mutation was applied without an audit record"
        )
        # Reads continue.
        rows = harness.expect_ok(
            harness.call(server, "run_gaql", {"query": "SELECT campaign.id FROM campaign"})
        )
        assert rows["rows"], "reads must keep working while audit is unwritable"
    finally:
        os.chmod(blocked, stat.S_IRUSR | stat.S_IWUSR)
