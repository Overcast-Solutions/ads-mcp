"""Bounded retry for transient transport faults.

Transient (gRPC UNAVAILABLE / DEADLINE_EXCEEDED / INTERNAL, connection reset)
is retried with capped exponential backoff and jitter; auth errors and
invalid-argument/permission failures are never retried. Retries apply to
reads; an uncertain provider mutation must not be repeated automatically.
"""

from __future__ import annotations

import random
import time

from google.api_core import exceptions as core_exceptions
from google.auth.exceptions import GoogleAuthError


class TransportError(Exception):
    """A request exhausted its retry budget (surfaces as TRANSPORT_FAILED)."""


_TRANSIENT_CORE = (
    core_exceptions.ServiceUnavailable,
    core_exceptions.DeadlineExceeded,
    core_exceptions.InternalServerError,
    core_exceptions.TooManyRequests,
    core_exceptions.Aborted,
)


def is_transient(exc: BaseException) -> bool:
    if isinstance(exc, _TRANSIENT_CORE):
        return True
    if isinstance(exc, GoogleAuthError):
        return False
    # Raw socket-level faults ("connection reset by peer mid-response") that
    # arrive outside the google.api_core hierarchy.
    if isinstance(exc, ConnectionError):
        return True
    return False


def run_with_retry(call, *, max_attempts: int = 3, sleep=None, rng=None,
                   base_seconds: float = 1.0, on_retry=None):
    """Execute ``call()``, retrying transient faults only.

    ``sleep`` and ``rng`` are injection seams for the oracle; production uses
    ``time.sleep`` and ``random.random``. ``on_retry(attempt, exc)`` fires
    before each backoff (audit hook).
    """
    do_sleep = time.sleep if sleep is None else sleep
    do_rng = random.random if rng is None else rng
    last: BaseException | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return call()
        except BaseException as exc:  # noqa: BLE001 — classified right here
            if not is_transient(exc):
                raise
            last = exc
            if attempt == max_attempts:
                break
            if on_retry is not None:
                on_retry(attempt, exc)
            # Exponential doubling with multiplicative jitter; first delay
            # stays well under 30s for any sane base.
            do_sleep(base_seconds * (2 ** (attempt - 1)) * (1.0 + do_rng()))
    raise TransportError(
        f"transient transport fault persisted after {max_attempts} attempts: "
        f"{type(last).__name__}: {last}"
    )
