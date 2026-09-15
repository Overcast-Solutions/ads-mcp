"""F038: evaluate real emitted predicates over genuine fractional SDK events."""
from datetime import datetime, timedelta
import operator
import re

import pytest

import harness as h
from offline_contract import ProjectedClient


def predicate_rows(rows, call):
    """A provider stand-in evaluates the query, never the requested test dates."""
    predicates = re.findall(r"change_event\.change_date_time\s*(>=|<=|>|<|=)\s*'([^']+)'", call.query)
    assert len(predicates) == 2, "history must emit both explicit timestamp boundaries"
    compare = {">=": operator.ge, "<=": operator.le, ">": operator.gt, "<": operator.lt, "=": operator.eq}
    return [row for row in rows if all(compare[op](datetime.fromisoformat(row.change_event.change_date_time),
        datetime.fromisoformat(bound)) for op, bound in predicates)]


@pytest.mark.parametrize("start,end,now,zone", [
    ("2026-09-11", "2026-09-11", "2026-09-12T12:00:00+00:00", "Etc/UTC"),
    ("2026-08-30", "2026-08-31", "2026-09-01T12:00:00+00:00", "Pacific/Kiritimati"),
    ("2026-12-30", "2026-12-31", "2027-01-01T12:00:00+00:00", "America/Los_Angeles"),
    ("2026-03-08", "2026-03-08", "2026-03-09T12:00:00+00:00", "America/New_York"),
    ("2026-11-01", "2026-11-01", "2026-11-02T12:00:00+00:00", "America/New_York"),
])
def test_whole_final_day_survives_calendar_rollover_and_dst(tmp_path, start, end, now, zone):
    lower = datetime.fromisoformat(start)
    upper = datetime.fromisoformat(end) + timedelta(days=1)
    stamps = [lower - timedelta(microseconds=1), lower,
        upper - timedelta(seconds=1), upper - timedelta(microseconds=999999),
        upper - timedelta(microseconds=1), upper]
    client = ProjectedClient()
    client.stub("customer", [{"customer": {"id": int(h.OTHER_CUSTOMER_ID), "time_zone": zone}}])
    client.stub("change_event", [{"change_event": {
        "change_date_time": stamp.isoformat(sep=" ", timespec="microseconds"),
        "user_email": f"event-{index}@example.org", "client_type": "GOOGLE_ADS_WEB_CLIENT",
        "change_resource_type": "CAMPAIGN", "resource_change_operation": "UPDATE"}}
        for index, stamp in enumerate(stamps)])
    client.filters["change_event"] = predicate_rows
    server = h.build_server(tmp_path, client=client,
        clock=h.FakeClock(datetime.fromisoformat(now).timestamp()), env={"ADS_MCP_ROW_LIMIT": "2"})
    args = {"customer_id": h.OTHER_CUSTOMER_ID, "date_range_start": start, "date_range_end": end}
    seen, tokens = [], set()
    for _ in range(4):
        payload = h.expect_ok(h.call(server, "get_change_history", args))
        assert payload["window"] == {"start": start, "end": end}
        assert not payload.get("possibly_truncated")
        seen += [row["actor"] for row in payload["changes"]]
        token = payload.get("next_page_token")
        if not token:
            break
        assert token not in tokens
        tokens.add(token)
        args["page_token"] = token
    assert seen == [f"event-{index}@example.org" for index in (1, 2, 3, 4)]
    assert not payload.get("next_page_token")
    history = [s.query for s in client.searches if "FROM change_event" in s.query]
    for query in history:
        assert re.search(r"change_event\.change_date_time\s*>=\s*'" + start + r" 00:00:00'", query)
        assert re.search(r"change_event\.change_date_time\s*<\s*'" + upper.strftime("%Y-%m-%d") + r" 00:00:00'", query)
        assert re.search(r"\bLIMIT\s+1000\b", query)
    assert {s.customer_id for s in client.searches} == {h.OTHER_CUSTOMER_ID}
    assert not client.mutations
