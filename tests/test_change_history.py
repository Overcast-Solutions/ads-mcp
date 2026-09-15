"""F008 (part 2) — change-history attribution."""

import harness


def _stub_change_events(client):
    client.stub(
        "change_event",
        [
            {
                "change_event": {
                    "change_date_time": "2026-07-30 14:02:11",
                    "user_email": "ops@example.org",
                    "client_type": "GOOGLE_ADS_WEB_CLIENT",
                    "change_resource_type": "CAMPAIGN",
                    "resource_change_operation": "UPDATE",
                    "changed_fields": "maximizeConversionValue.targetRoas",
                    "old_resource": {
                        "campaign": {"maximize_conversion_value": {"target_roas": 3.5}}
                    },
                    "new_resource": {
                        "campaign": {"maximize_conversion_value": {"target_roas": 2.8}}
                    },
                    "campaign": f"customers/{harness.CUSTOMER_ID}/campaigns/111",
                }
            }
        ],
    )
    return client


def test_change_history_returns_actor_and_old_new_values(tmp_path, account_client):
    _stub_change_events(account_client)
    server = harness.build_server(tmp_path, client=account_client)
    payload = harness.expect_ok(
        harness.call(
            server,
            "get_change_history",
            {"date_range_start": "2026-07-28", "date_range_end": "2026-08-01"},
        )
    )
    changes = payload["changes"]
    assert changes, "no change events from the recorded fixture"
    event = changes[0]
    assert event["actor"] == "ops@example.org"
    assert event["client_type"] == "GOOGLE_ADS_WEB_CLIENT"
    assert event["resource_type"] == "CAMPAIGN"
    assert event["operation"] == "UPDATE"
    assert event["timestamp"] == "2026-07-30 14:02:11"
    field_changes = event["changes"]
    key = "campaign.maximize_conversion_value.target_roas"
    assert key in field_changes, f"changed field missing: {field_changes}"
    assert field_changes[key]["old"] == 3.5
    assert field_changes[key]["new"] == 2.8


def test_change_history_query_is_bounded_and_filterable(tmp_path, account_client):
    _stub_change_events(account_client)
    server = harness.build_server(tmp_path, client=account_client)
    harness.expect_ok(
        harness.call(
            server,
            "get_change_history",
            {
                "date_range_start": "2026-07-28",
                "date_range_end": "2026-08-01",
                "resource_type": "CAMPAIGN",
            },
        )
    )
    query = account_client.searches[-1].query
    assert "LIMIT" in query.upper(), (
        "change_event queries must carry an explicit LIMIT (Google requires it)"
    )
    assert "CAMPAIGN" in query, f"resource-type filter not applied server-side: {query}"


def test_range_beyond_google_constraint_is_named_error(tmp_path, account_client):
    _stub_change_events(account_client)
    server = harness.build_server(tmp_path, client=account_client)
    err = harness.expect_error(
        server,
        "get_change_history",
        {"date_range_start": "2026-05-01", "date_range_end": "2026-08-01"},
        code="CHANGE_HISTORY_RANGE_EXCEEDED",
    )
    assert "30" in err["message"], (
        f"the 30-day constraint must be named: {err['message']}"
    )
    assert not account_client.searches, "over-range request must not reach the API"
