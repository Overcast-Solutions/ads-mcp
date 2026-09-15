"""F003 — Transport reliability: bounded retry on transient faults only."""

import pytest
from google.api_core import exceptions as core_exceptions
from google.auth.exceptions import RefreshError

import harness
from ads_mcp.transport import TransportError, run_with_retry

TRANSIENT = [
    core_exceptions.ServiceUnavailable("gRPC UNAVAILABLE: socket closed"),
    core_exceptions.DeadlineExceeded("gRPC DEADLINE_EXCEEDED"),
    core_exceptions.InternalServerError("gRPC INTERNAL: received RST_STREAM"),
    ConnectionResetError("connection reset by peer mid-response"),
]

NON_TRANSIENT = [
    core_exceptions.InvalidArgument("400: bad request"),
    core_exceptions.PermissionDenied("403: developer token not approved"),
    RefreshError("invalid_grant: token revoked"),
    harness.make_google_ads_exception(["The query is invalid."]),
]


class Flaky:
    def __init__(self, failures, exc):
        self.failures = failures
        self.exc = exc
        self.calls = 0

    def __call__(self):
        self.calls += 1
        if self.calls <= self.failures:
            raise self.exc
        return "ok"


@pytest.mark.parametrize("exc", TRANSIENT, ids=lambda e: type(e).__name__)
def test_transient_faults_retried_to_success(exc):
    sleeps = []
    call = Flaky(2, exc)
    assert run_with_retry(call, sleep=sleeps.append) == "ok"
    assert call.calls == 3
    assert len(sleeps) == 2


def test_backoff_is_exponential_with_jitter():
    first_delays = set()
    for _ in range(12):
        sleeps = []
        call = Flaky(2, core_exceptions.ServiceUnavailable("UNAVAILABLE"))
        run_with_retry(call, sleep=sleeps.append)
        assert len(sleeps) == 2
        assert 0 < sleeps[0] < 30, f"unreasonable first backoff {sleeps[0]}"
        assert sleeps[1] > sleeps[0], (
            f"backoff must grow: {sleeps}"
        )
        first_delays.add(round(sleeps[0], 6))
    assert len(first_delays) > 1, (
        "12 runs produced identical first delays — no jitter"
    )


def test_exhausted_retries_raise_transport_failed_with_fault_and_count():
    call = Flaky(99, core_exceptions.ServiceUnavailable("gRPC UNAVAILABLE: flop"))
    sleeps = []
    with pytest.raises(TransportError) as exc:
        run_with_retry(call, sleep=sleeps.append)
    assert call.calls == 3, f"expected exactly 3 attempts, made {call.calls}"
    message = str(exc.value)
    assert "UNAVAILABLE" in message or "ServiceUnavailable" in message, (
        f"TransportError must name the underlying fault: {message}"
    )
    assert "3" in message, f"TransportError must name the attempt count: {message}"


@pytest.mark.parametrize("exc", NON_TRANSIENT, ids=lambda e: type(e).__name__)
def test_non_transient_faults_never_retried(exc):
    sleeps = []
    call = Flaky(99, exc)
    with pytest.raises(type(exc)):
        run_with_retry(call, sleep=sleeps.append)
    assert call.calls == 1, f"non-transient fault was retried: {call.calls} attempts"
    assert sleeps == [], "no backoff sleep may happen for a non-transient fault"


def test_tool_level_transport_failed_payload(tmp_path, account_client):
    account_client.stub_error(
        core_exceptions.ServiceUnavailable("gRPC UNAVAILABLE: backend flop")
    )
    server = harness.build_server(
        tmp_path, client=account_client, env={"ADS_MCP_RETRY_BASE_SECONDS": "0.01"}
    )
    err = harness.expect_error(
        server,
        "run_gaql",
        {"query": "SELECT campaign.id FROM campaign"},
        code="TRANSPORT_FAILED",
    )
    assert "unavailable" in err["message"].lower()
    assert "3" in err["message"], f"attempt count missing: {err['message']}"
    assert len(account_client.searches) == 3


def test_tool_level_retry_then_success(tmp_path, account_client):
    account_client.stub_error(
        core_exceptions.ServiceUnavailable("gRPC UNAVAILABLE: blip"), times=1
    )
    server = harness.build_server(
        tmp_path, client=account_client, env={"ADS_MCP_RETRY_BASE_SECONDS": "0.01"}
    )
    payload = harness.expect_ok(
        harness.call(server, "run_gaql", {"query": "SELECT campaign.id FROM campaign"})
    )
    assert payload["rows"], "retried call must return the recorded rows"
    assert len(account_client.searches) == 2
