"""F041: strict calendar spelling at all six public report wrappers."""
import pytest

import harness as h


REPORTS = ["get_campaign_performance", "get_ad_performance", "get_keyword_performance",
    "get_search_terms", "get_geo_performance", "get_shopping_performance"]


def arguments(tool, window):
    return {**window, "customer_id": h.OTHER_CUSTOMER_ID,
        **({"campaign_id": "111"} if tool == "get_shopping_performance" else {})}


@pytest.mark.parametrize("tool", REPORTS)
@pytest.mark.parametrize("start,end", [
    ("2026-9-10", "2026-9-2"), ("2026-9-2", "2026-9-10"),
    ("2026-09-01", "2026-9-02"), ("2026-9-01", "2026-09-02"),
    ("2026-09-1", "2026-09-02"), ("2026/09/01", "2026-09-02"),
    ("20260901", "2026-09-02"), ("2026-09-01T00:00:00", "2026-09-02"),
    (" 2026-09-01", "2026-09-02"), ("2026-02-29", "2026-03-01"),
    ("2026-02-30", "2026-03-01"), ("2026-09-10", "2026-09-02"),
])
def test_noncanonical_invalid_and_reversed_windows_refuse_before_account(tmp_path, account_client, tool, start, end):
    server = h.build_server(tmp_path, client=account_client)
    h.expect_error(server, tool, arguments(tool, {"date_range_start": start, "date_range_end": end}), code="INVALID_WINDOW")
    assert not account_client.searches and not account_client.mutations


@pytest.mark.parametrize("tool", REPORTS)
@pytest.mark.parametrize("start,end", [("2026-09-01", "2026-09-01"),
    ("2026-09-02", "2026-09-10"), ("2024-02-28", "2024-02-29"),
    ("2024-02-29", "2024-03-01"), ("2025-12-31", "2026-01-01")])
def test_canonical_valid_windows_match_payload_and_query(tmp_path, account_client, tool, start, end):
    server = h.build_server(tmp_path, client=account_client)
    payload = h.expect_ok(h.call(server, tool, arguments(tool, {"date_range_start": start, "date_range_end": end})))
    assert payload["window"] == {"start": start, "end": end}
    assert f"segments.date BETWEEN '{start}' AND '{end}'" in account_client.searches[-1].query
    assert {s.customer_id for s in account_client.searches} == {h.OTHER_CUSTOMER_ID}


@pytest.mark.parametrize("tool", REPORTS)
@pytest.mark.parametrize("window", [{}, {"date_range_start": "2026-09-01"},
    {"date_range_end": "2026-09-01"}, {"last_n_days": 0}, {"last_n_days": -1}, {"last_n_days": 3651}])
def test_existing_missing_pair_and_relative_range_errors_remain(tmp_path, account_client, tool, window):
    server = h.build_server(tmp_path, client=account_client)
    h.expect_error(server, tool, arguments(tool, window), code="INVALID_WINDOW")
    assert not account_client.searches


@pytest.mark.parametrize("tool", REPORTS)
@pytest.mark.parametrize("explicit", [False, True])
def test_relative_days_keep_existing_precedence_and_complete_day_window(tmp_path, account_client, tool, explicit):
    # Existing resolve_window gives last_n_days precedence when both forms
    # are supplied. This repair does not invent a conflicting-input policy.
    window = {"last_n_days": 2}
    if explicit:
        window.update(date_range_start="2026-01-01", date_range_end="2026-01-02")
    server = h.build_server(tmp_path, client=account_client, clock=h.FakeClock())
    payload = h.expect_ok(h.call(server, tool, arguments(tool, window)))
    assert payload["window"] == {"start": "2026-07-31", "end": "2026-08-01"}
    assert "segments.date BETWEEN '2026-07-31' AND '2026-08-01'" in account_client.searches[-1].query
