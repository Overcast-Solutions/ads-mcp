"""F038: inclusive D-29..D retention and honest hard-ceiling pagination."""
from datetime import datetime, timedelta
import json
import re
from zoneinfo import ZoneInfo

import pytest

import harness as h
from offline_contract import ProjectedClient, ROOT


def server_at(tmp_path, now, zone="America/Los_Angeles", row_limit=1000):
    client = ProjectedClient()
    client.stub("customer", [{"customer": {"id": int(h.OTHER_CUSTOMER_ID), "time_zone": zone}}])
    clock = h.FakeClock(datetime.fromisoformat(now).timestamp())
    server = h.build_server(tmp_path, client=client, clock=clock, env={"ADS_MCP_ROW_LIMIT": str(row_limit)})
    return server, client


@pytest.mark.parametrize("now,zone", [
    ("2026-08-02T00:30:00+00:00", "America/Los_Angeles"),
    ("2026-08-02T23:30:00+00:00", "Pacific/Kiritimati"),
    ("2026-08-02T12:00:00+00:00", "Etc/UTC"),
])
@pytest.mark.parametrize("offset,accepted", [(-30, False), (-29, True), (0, True), (1, False)])
def test_retention_boundaries_use_verified_requested_account_date(tmp_path, now, zone, offset, accepted):
    server, client = server_at(tmp_path, now, zone)
    today = datetime.fromisoformat(now).astimezone(ZoneInfo(zone)).date()
    day = today + timedelta(days=offset)
    payload = h.call(server, "get_change_history", {"customer_id": h.OTHER_CUSTOMER_ID,
         "date_range_start": str(day), "date_range_end": str(day)})
    history = [s for s in client.searches if "FROM change_event" in s.query]
    if accepted:
        assert h.expect_ok(payload)["window"] == {"start": str(day), "end": str(day)}
        assert len(history) == 1
    else:
        error = h.error_of(payload)
        assert error["code"] == "CHANGE_HISTORY_RANGE_EXCEEDED"
        assert "30" in error["message"] and any(word in error["message"].lower() for word in ("past", "recent", "retention"))
        assert not history
    assert {s.customer_id for s in client.searches} <= {h.OTHER_CUSTOMER_ID}
    # Even accepted dates must be validated against a verified zone.
    if accepted:
        assert any("customer.time_zone" in s.query for s in client.searches)


def test_full_inclusive_thirty_day_window_is_valid(tmp_path):
    server, client = server_at(tmp_path, "2026-08-02T12:00:00+00:00", "Etc/UTC")
    payload = h.expect_ok(h.call(server, "get_change_history", {"date_range_start": "2026-07-04", "date_range_end": "2026-08-02"}))
    assert payload["changes"] == []
    assert "change_event.change_date_time < '2026-08-03 00:00:00'" in client.searches[-1].query


@pytest.mark.parametrize("start,end,code", [
    ("2026-7-4", "2026-07-05", "INVALID_WINDOW"),
    ("2026-02-30", "2026-03-01", "INVALID_WINDOW"),
    ("2026-08-02", "2026-07-31", "INVALID_WINDOW"),
    ("1900-01-01", "1900-01-02", "CHANGE_HISTORY_RANGE_EXCEEDED"),
    ("2099-01-01", "2099-01-02", "CHANGE_HISTORY_RANGE_EXCEEDED"),
])
def test_malformed_or_globally_impossible_history_dates_never_read_account(tmp_path, start, end, code):
    server, client = server_at(tmp_path, "2026-08-02T12:00:00+00:00")
    error = h.error_of(h.call(server, "get_change_history", {"date_range_start": start, "date_range_end": end}))
    assert error["code"] == code
    assert not client.searches


@pytest.mark.parametrize("zone", ["", "Not/A_Timezone", None])
def test_unavailable_zone_never_substitutes_utc(tmp_path, zone):
    server, client = server_at(tmp_path, "2026-08-02T00:30:00+00:00", zone or "")
    if zone is None:
        client.stub("customer", [])
    error = h.error_of(h.call(server, "get_change_history", {"date_range_start": "2026-07-31", "date_range_end": "2026-08-01"}))
    assert error["code"] == "ACCOUNT_TIME_ZONE_UNAVAILABLE"
    assert not any("FROM change_event" in s.query for s in client.searches)


def events(count):
    return [{"change_event": {"change_date_time": "2026-08-01 12:00:00", "user_email": f"synthetic-{i}@example.org",
              "client_type": "GOOGLE_ADS_WEB_CLIENT", "change_resource_type": "CAMPAIGN", "resource_change_operation": "UPDATE",
              "campaign": f"customers/{h.OTHER_CUSTOMER_ID}/campaigns/111", "changed_fields": "name",
              "old_resource": {"campaign": {"name": "Old"}}, "new_resource": {"campaign": {"name": "New"}}}}
            for i in range(count)]


@pytest.mark.parametrize("count", [999, 1000, 1001])
def test_hard_limit_has_honest_signal_through_every_local_page(tmp_path, count):
    server, client = server_at(tmp_path, "2026-08-02T12:00:00+00:00", row_limit=333)
    client.stub("change_event", events(count))
    args = {"customer_id": h.OTHER_CUSTOMER_ID, "date_range_start": "2026-08-01", "date_range_end": "2026-08-01"}
    seen, tokens = [], set()
    for _ in range(5):
        payload = h.expect_ok(h.call(server, "get_change_history", args))
        if count >= 1000:
            assert payload.get("possibly_truncated") is True, "hitting LIMIT 1000 silently claims complete results"
            text = json.dumps({k: v for k, v in payload.items() if k != "changes"}).lower()
            assert "narrow" in text and ("window" in text or "date" in text)
        else:
            assert set(payload) <= {"customer_id", "window", "changes", "next_page_token"}
        seen += [e["actor"] for e in payload["changes"]]
        token = payload.get("next_page_token")
        if not token:
            break
        assert token not in tokens
        tokens.add(token)
        args["page_token"] = token
    assert seen == [f"synthetic-{i}@example.org" for i in range(min(count, 1000))]
    assert not payload.get("next_page_token"), "local offsets must terminate at the hard provider query cap"
    assert all(re.search(r"\bLIMIT\s+1000\b", s.query, re.I) for s in client.searches if "FROM change_event" in s.query)


def test_history_docs_explain_unrecoverable_same_timestamp_ceiling():
    text = (ROOT / "README.md").read_text().lower()
    assert "run_gaql" in text and ("10000" in text or "10,000" in text)
    assert "timestamp" in text and ("1000" in text or "1,000" in text)
    assert "calendar" in text and ("29" in text or "thirty" in text or "30" in text)
