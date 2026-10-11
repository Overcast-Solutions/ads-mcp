"""F093 repair: field-metadata deadline, request budget and lookup audit attribution.

Reproduces the verify findings S1 (retry backoff slept past the 10-second
deadline), N4 (SDK pager pages escaped the three-request budget and kept a
stale timeout), S9 and N5 (lookup retries audited as query retries, and with
an account), N1 (a resource without attribute fields reported as missing) and
the N14 enum-hint punctuation nit.

Provider substitution only: tests/harness.py's GoogleAdsFieldService stub
serves genuine v25 GoogleAdsField messages; its ``field_hook`` delays, fails
or caps pages exactly as a slow or paging field service would. The doubles
honor the timeout each request carries. Every test that can sleep runs the
tool call under its own bounded wait, and ``time.sleep`` is observed (and cut
off at a hard stop beyond the pass threshold) so a red run cannot hang.

Contract pinned here: the deadline clock starts no later than the first
field-service request of a call (or of a run_gaql hint lookup), so every
request carries at most ``first request + 10 s - now`` and the call returns by
``first request + 10.5 s``.
"""
import math
import threading
import time
from types import SimpleNamespace

import pytest
from google.api_core.exceptions import DeadlineExceeded, ServiceUnavailable

import harness as h
import resource_metadata_oracle as rm
from test_gaql_field_guidance_contract import OPERATOR, hints_of, no_hints, query_failure

DEADLINE = rm.DEADLINE_SECONDS
GRACE = 0.5
EPSILON = 0.15
HARD_STOP = 12.0
WATCHDOG = 40.0
MAX_RETRY_BASE = {"ADS_MCP_RETRY_BASE_SECONDS": "30"}
FAST_RETRY = {"ADS_MCP_RETRY_BASE_SECONDS": "0.001"}
UNRECOGNIZED = "Unrecognized field in the query: 'campaign.stauts'."
QUERY = "SELECT campaign.stauts FROM campaign"
_real_sleep = time.sleep


def server_for(tmp_path, client, env=None, *, read_only=True):
    tmp_path.mkdir(parents=True, exist_ok=True)
    merged = {**FAST_RETRY, **(env or {})}
    if read_only:
        merged["ADS_MCP_AUDIT_LOG"] = str(h.audit_file(tmp_path))
        return h.build_server(tmp_path, client=client, env=merged)
    return h.build_rw_server(tmp_path, client=client, env=merged)


def bounded(call, seconds=WATCHDOG):
    """Run ``call`` on a daemon thread; a call that never returns fails here."""
    box = {}

    def target():
        try:
            box["value"] = call()
        except BaseException as exc:  # noqa: BLE001 - re-raised in the test thread
            box["error"] = exc

    worker = threading.Thread(target=target, daemon=True)
    worker.start()
    worker.join(seconds)
    if worker.is_alive():
        pytest.fail(f"the tool call did not return within {seconds:g} s")
    if "error" in box:
        raise box["error"]
    return box["value"]


@pytest.fixture
def sleeps(monkeypatch):
    """Every time.sleep requested during the test, cut off at ``hard_stop``."""
    log = SimpleNamespace(calls=[], hard_stop=None)

    def observed(seconds):
        now = time.monotonic()
        log.calls.append((now, float(seconds)))
        limit = float(seconds) if log.hard_stop is None else min(float(seconds), log.hard_stop - now)
        _real_sleep(max(0.0, limit))

    monkeypatch.setattr(time, "sleep", observed)
    return log


def numeric(timeout):
    return isinstance(timeout, (int, float)) and not isinstance(timeout, bool)


def slow_then_transient(client, delay, seen):
    """A field service that honors its timeout, delays, then fails transiently."""

    def hook(call):
        seen.append((time.monotonic(), call.timeout))
        budget = call.timeout if numeric(call.timeout) else delay
        _real_sleep(max(0.0, min(delay, budget)))
        if budget < delay:
            raise DeadlineExceeded("synthetic field request ran out of time")
        raise ServiceUnavailable("synthetic slow field service")

    client.field_hook = hook


def capped_pages(client, cap, seen, delay=0.0):
    """A field service that returns at most ``cap`` fields per page and honors its timeout."""

    def hook(call):
        seen.append((time.monotonic(), call.timeout))
        call.page_size = min(call.page_size or cap, cap)
        if delay:
            budget = call.timeout if numeric(call.timeout) else delay
            _real_sleep(max(0.0, min(delay, budget)))
            if budget < delay:
                raise DeadlineExceeded("synthetic field page ran out of time")

    client.field_hook = hook


def like_pages(cap, top="campaign"):
    """Pages the prefix query for ``top`` needs at ``cap`` fields per page."""
    return math.ceil(len([name for name in rm.by_name() if name.startswith(top + ".")]) / cap)


def assert_within_deadline(seen, finished, sleeps=None):
    assert seen, "the field service was never asked"
    start = seen[0][0]
    elapsed = finished - start
    assert elapsed <= DEADLINE + GRACE, (
        f"the call returned {elapsed:.1f} s after its first field request; the deadline is "
        f"{DEADLINE:g} s + {GRACE:g} s including retry backoff and pages")
    if sleeps is not None:
        late = [(round(at - start, 2), round(seconds, 2)) for at, seconds in sleeps.calls
                if at + seconds > start + DEADLINE + GRACE]
        assert not late, f"retry backoff was scheduled past the deadline (offset, seconds): {late}"
    for at, timeout in seen:
        assert numeric(timeout), f"field request has no numeric timeout: {timeout!r}"
        assert timeout <= start + DEADLINE - at + EPSILON, (
            f"a request {at - start:.2f} s into the call carried {timeout:.2f} s; at most the "
            "remaining time may be given")


# ---------------------------------------------------------------------------
# S1: the deadline is a wall, retry backoff included.


def test_metadata_deadline_holds_across_retry_backoff_at_the_maximum_base(tmp_path, sleeps):
    client = rm.provider()
    seen = []
    slow_then_transient(client, 2.0, seen)
    server = server_for(tmp_path, client, MAX_RETRY_BASE)
    sleeps.hard_stop = time.monotonic() + HARD_STOP
    payload, finished = bounded(lambda: (rm.metadata(server, {"name": "campaign"}), time.monotonic()))
    assert_within_deadline(seen, finished, sleeps)
    error = h.error_of(payload)
    assert error["code"] == "TRANSPORT_FAILED", error
    assert "deadline" in error["message"].lower(), f"the deadline error must say so: {error['message']!r}"
    assert len(client.field_searches) <= rm.MAX_REQUESTS and not client.searches


def test_hint_lookup_deadline_holds_across_retry_backoff_and_keeps_the_original_error(tmp_path, sleeps):
    client = rm.provider().stub_error(query_failure("UNRECOGNIZED_FIELD", UNRECOGNIZED, "campaign.stauts"))
    seen = []
    slow_then_transient(client, 2.0, seen)
    server = server_for(tmp_path, client, MAX_RETRY_BASE)
    sleeps.hard_stop = time.monotonic() + HARD_STOP
    payload, finished = bounded(lambda: (h.call(server, "run_gaql", {"query": QUERY}), time.monotonic()))
    assert_within_deadline(seen, finished, sleeps)
    error = h.error_of(payload)
    assert error["message"] == UNRECOGNIZED and error.get("provider_error") == "UNRECOGNIZED_FIELD", error
    no_hints(error)
    assert [call.query for call in client.searches] == [QUERY], "the query is sent exactly once"


def test_slow_follow_on_pages_stop_at_the_deadline_never_a_late_or_partial_list(tmp_path):
    cap = 65
    assert like_pages(cap) == 2
    client = rm.provider()
    seen = []
    capped_pages(client, cap, seen, delay=4.0)
    server = server_for(tmp_path, client)
    payload, finished = bounded(lambda: (rm.metadata(server, {"name": "campaign"}), time.monotonic()))
    assert_within_deadline(seen, finished)
    error = h.error_of(payload)
    assert error["code"] == "TRANSPORT_FAILED" and "deadline" in error["message"].lower(), error
    assert "fields" not in payload


# ---------------------------------------------------------------------------
# N4: pager pages are requests: they share the deadline and the three-request cap.


def test_follow_on_pages_count_toward_three_requests_and_get_only_the_remaining_time(tmp_path):
    cap = 65
    assert like_pages(cap) == 2, "the resource row plus two prefix pages: three requests"
    client = rm.provider()
    seen = []
    capped_pages(client, cap, seen, delay=0.6)
    server = server_for(tmp_path, client)
    first, finished = bounded(lambda: (rm.metadata(server, {"name": "campaign"}), time.monotonic()))
    first = h.expect_ok(first)
    assert len(client.field_searches) == 3, (
        f"a family needing three requests is served with exactly three, saw {len(client.field_searches)}")
    assert_within_deadline(seen, finished)
    expected = rm.expected_fields("campaign")
    assert first["fields"] == expected[:100]
    second = h.expect_ok(rm.metadata(server, {"name": "campaign", "page_token": first["next_page_token"]}))
    assert first["fields"] + second["fields"] == expected, "the paged family must be complete"


@pytest.mark.parametrize("surface", ["metadata", "hints"])
def test_a_family_needing_a_fourth_request_fails_on_the_request_budget(tmp_path, surface):
    cap = 44
    assert like_pages(cap) == 3, "the resource row plus three prefix pages: four requests"
    client = rm.provider()
    if surface == "hints":
        client.stub_error(query_failure("UNRECOGNIZED_FIELD", UNRECOGNIZED, "campaign.stauts"))
    seen = []
    capped_pages(client, cap, seen)
    server = server_for(tmp_path, client)
    if surface == "metadata":
        payload = bounded(lambda: rm.metadata(server, {"name": "campaign"}))
        assert "error" in payload, (
            f"a family needing a fourth request was served with {len(client.field_searches)} requests")
        error = h.error_of(payload)
        assert error["code"] == "TRANSPORT_FAILED", error
        message = error["message"].lower()
        assert "request" in message and ("3" in message or "three" in message), (
            f"the error must name the request budget: {error['message']!r}")
        assert "fields" not in payload, "never a partial list"
    else:
        error = h.error_of(bounded(lambda: h.call(server, "run_gaql", {"query": QUERY})))
        assert error["message"] == UNRECOGNIZED
        no_hints(error)
    assert len(client.field_searches) <= rm.MAX_REQUESTS, (
        f"{len(client.field_searches)} field-service requests; the cap is {rm.MAX_REQUESTS}")


# ---------------------------------------------------------------------------
# S9 and N5: lookup retries are audited as lookups.


def retries(tmp_path):
    return [record for record in h.read_audit_records(tmp_path) if record.get("event") == "retry"]


@pytest.mark.parametrize("customer", [None, h.OTHER_CUSTOMER_ID], ids=["configured-account", "other-account"])
def test_hint_lookup_retries_are_not_audited_as_query_retries(tmp_path, customer):
    client = rm.provider().stub_error(query_failure("UNRECOGNIZED_FIELD", UNRECOGNIZED, "campaign.stauts"))
    client.stub_field_error(ServiceUnavailable("synthetic transient field-service fault"), times=1)
    server = server_for(tmp_path, client)
    args = {"query": QUERY, **({"customer_id": customer} if customer else {})}
    error = h.error_of(h.call(server, "run_gaql", args))
    assert "campaign.status" in {name for hint in hints_of(error, "UNRECOGNIZED_FIELD") for name in hint["fields"]}
    assert len(client.searches) == 1 and len(client.field_searches) >= 2, "the lookup retried; the query did not"
    records = retries(tmp_path)
    unlabelled = [record for record in records if record.get("tool") == "run_gaql" and "lookup" not in record]
    assert not unlabelled, f"a lookup retry reads as a retried query: {unlabelled}"
    lookups = [record for record in records if record.get("lookup") == "field_metadata"]
    assert lookups, f"lookup retries carry lookup == 'field_metadata': {records}"
    queried = customer or h.CUSTOMER_ID
    assert all(record.get("customer_id", queried) == queried for record in lookups), lookups


def test_query_retries_keep_their_records_beside_labelled_lookup_retries(tmp_path):
    client = rm.provider()
    faults = [ServiceUnavailable("synthetic temporary read failure"),
              query_failure("UNRECOGNIZED_FIELD", UNRECOGNIZED, "campaign.stauts")]
    original = client._do_search

    def search(service, method, args, kwargs):
        original(service, method, args, kwargs)
        raise faults.pop(0) if len(faults) > 1 else faults[0]

    client._do_search = search
    client.stub_field_error(ServiceUnavailable("synthetic transient field-service fault"), times=1)
    server = server_for(tmp_path, client)
    error = h.error_of(h.call(server, "run_gaql", {"query": QUERY}))
    hints_of(error, "UNRECOGNIZED_FIELD")
    assert [call.query for call in client.searches] == [QUERY, QUERY]
    records = retries(tmp_path)
    query_retries = [record for record in records if "lookup" not in record]
    lookup_retries = [record for record in records if record.get("lookup") == "field_metadata"]
    assert len(query_retries) == 1 and query_retries[0].get("tool") == "run_gaql", records
    assert query_retries[0].get("customer_id") == h.CUSTOMER_ID
    assert lookup_retries, records


@pytest.mark.parametrize("read_only", [True, False], ids=["readonly", "write-mode"])
def test_metadata_retry_records_are_field_metadata_lookups_without_an_account(tmp_path, read_only):
    client = rm.provider()
    client.stub_field_error(ServiceUnavailable("synthetic transient field-service fault"), times=1)
    server = server_for(tmp_path, client, read_only=read_only)
    payload = h.expect_ok(rm.metadata(server, {"name": "campaign.status"}))
    assert payload["fields"] == rm.expected_fields("campaign.status")
    records = retries(tmp_path)
    assert records, "the transient fault was retried and must be audited"
    for record in records:
        assert record.get("lookup") == "field_metadata", record
        assert "customer_id" not in record, f"schema metadata addresses no account: {record}"


# ---------------------------------------------------------------------------
# N1 and N14.


def test_a_resource_without_attribute_fields_is_returned_not_reported_missing(tmp_path):
    lonely = {"name": "oracle_lonely", "category": "RESOURCE", "data_type": "MESSAGE", "selectable": True,
              "filterable": False, "sortable": False, "is_repeated": False,
              "selectable_with": ["campaign", "segments.date", "metrics.clicks", "metrics.impressions"],
              "attribute_resources": ["customer"]}
    fields = rm.catalogue() + [lonely]
    client = rm.provider(fields)
    server = server_for(tmp_path, client)
    payload = rm.metadata(server, {"name": "oracle_lonely"})
    assert payload == {"name": "oracle_lonely", "fields": [], **rm.expected_names("oracle_lonely", fields),
                       "next_page_token": None}, payload
    rm.assert_bounded_requests(client, 0, ["oracle_lonely"], universe=fields)


@pytest.mark.parametrize("enum", OPERATOR)
@pytest.mark.parametrize("field", ["campaign.advertising_channel_type", "campaign.oracle_enum_150"])
def test_enum_hint_text_has_no_doubled_punctuation(tmp_path, enum, field):
    client = rm.provider().stub_error(query_failure(enum, f"Invalid operator for field '{field}'.", field))
    server = server_for(tmp_path, client)
    query = f"SELECT campaign.id FROM campaign WHERE {field} LIKE '%x%'"
    error = h.error_of(h.call(server, "run_gaql", {"query": query}))
    hints = hints_of(error, enum)
    useful = [value for value in rm.by_name()[field]["enum_values"] if value not in ("UNSPECIFIED", "UNKNOWN")]
    assert any(value in hint["message"] for hint in hints for value in useful), "the enum hint lists values"
    for hint in hints:
        assert ", ...." not in hint["message"] and ".." not in hint["message"], (
            f"doubled punctuation in hint text: {hint['message']!r}")


# ---------------------------------------------------------------------------
# Archives.
