"""F027: future forecast windows are bounded in the requested account zone."""
from datetime import datetime

import pytest

import harness as h


def server_at(tmp_path, client, instant="2026-08-02T00:30:00+00:00", zone="America/Los_Angeles"):
    client.stub("customer", [{"customer": {"id": int(h.OTHER_CUSTOMER_ID), "currency_code": "USD", "time_zone": zone}}])
    return h.build_server(tmp_path, client=client, clock=lambda: datetime.fromisoformat(instant).timestamp())


def forecast(server, start=None, end=None):
    args = {"keywords": ["widgets"], "customer_id": h.OTHER_CUSTOMER_ID}
    if start is not None:
        args["date_range_start"] = start
    if end is not None:
        args["date_range_end"] = end
    return h.call(server, "get_keyword_forecasts", args)


@pytest.mark.parametrize("start,end", [
    ("2026-08-03", "9999-12-31"), ("2020-01-01", "2020-01-30"),
    ("2026-08-03", "2027-08-03"),
    ("2026-08-04", "2026-08-03"), ("2026-8-03", "2026-08-04"),
    ("2026-08-03", "2026-8-04"), ("2026-08-03T00:00:00", "2026-08-04"),
    ("2026-02-30", "2026-08-04"), ("2026-08-03", None),
    (None, "2026-08-04"), ("", ""), ("2026-08-03", ""),
])
def test_globally_impossible_or_non_strict_windows_refuse_before_api(tmp_path, account_client, start, end):
    server = server_at(tmp_path, account_client)
    assert h.error_of(forecast(server, start, end))["code"] == "INVALID_WINDOW"
    assert not account_client.searches and not account_client.planner_calls()
    assert not account_client.mutations


@pytest.mark.parametrize("zone,start,end,valid", [
    ("America/Los_Angeles", "2026-08-02", "2026-08-31", True),
    ("Pacific/Kiritimati", "2026-08-02", "2026-08-31", False),
    ("America/Los_Angeles", "2026-08-03", "2027-08-01", True),
    ("America/Los_Angeles", "2026-08-03", "2027-08-02", False),
    ("Pacific/Kiritimati", "2026-08-03", "2027-08-02", True),
])
def test_account_local_start_and_inclusive_anniversary(tmp_path, account_client, zone, start, end, valid):
    server = server_at(tmp_path, account_client, zone=zone)
    payload = forecast(server, start, end)
    if valid:
        assert h.expect_ok(payload)["window"] == {"start": start, "end": end}
        assert len(account_client.planner_calls()) == 1
        method, _, kwargs = account_client.planner_calls()[0]
        assert method == "generate_keyword_forecast_metrics"
        request = kwargs["request"]
        assert request.customer_id == h.OTHER_CUSTOMER_ID
        assert request.forecast_period.start_date == start and request.forecast_period.end_date == end
    else:
        assert h.error_of(payload)["code"] == "INVALID_WINDOW"
        assert not account_client.planner_calls()
    # These dates are ambiguous across timezones: requested account is read.
    assert account_client.searches
    assert all(s.customer_id == h.OTHER_CUSTOMER_ID for s in account_client.searches)
    assert any("customer.time_zone" in s.query for s in account_client.searches)
    assert not account_client.mutations


@pytest.mark.parametrize("zone,start,end", [
    ("America/Los_Angeles", "2026-08-02", "2026-08-31"),
    ("Pacific/Kiritimati", "2026-08-03", "2026-09-01"),
])
def test_default_is_next_thirty_complete_account_days(tmp_path, account_client, zone, start, end):
    server = server_at(tmp_path, account_client, zone=zone)
    result = h.expect_ok(forecast(server))
    assert result["window"] == {"start": start, "end": end}
    request = account_client.planner_calls()[0][2]["request"]
    assert request.forecast_period.start_date == start and request.forecast_period.end_date == end
    assert not account_client.mutations


@pytest.mark.parametrize("end,valid", [("2029-02-28", True), ("2029-03-01", False)])
def test_leap_anniversary_clamps_to_february_28(tmp_path, account_client, end, valid):
    server = server_at(tmp_path, account_client, instant="2028-02-29T12:00:00+00:00", zone="UTC")
    result = forecast(server, "2028-03-01", end)
    if valid:
        assert h.expect_ok(result)["window"]["end"] == "2029-02-28"
        assert len(account_client.planner_calls()) == 1
    else:
        assert h.error_of(result)["code"] == "INVALID_WINDOW"
        assert not account_client.planner_calls()


@pytest.mark.parametrize("zone", ["", "not/a-timezone"])
def test_unknown_account_timezone_fails_closed(tmp_path, account_client, zone):
    server = server_at(tmp_path, account_client, zone=zone)
    error = h.error_of(forecast(server))
    assert error["code"] != "INTERNAL" and error["code"]
    assert "time" in error["message"].lower() and "zone" in error["message"].lower()
    assert not account_client.planner_calls() and not account_client.mutations


def test_missing_account_row_cannot_supply_timezone(tmp_path, account_client):
    server = server_at(tmp_path, account_client)
    account_client.stub("customer", [])
    assert h.error_of(forecast(server))["code"] != "INTERNAL"
    assert not account_client.planner_calls() and not account_client.mutations
