"""Server-local, bounded projected snapshots for public read continuation.

Only explicitly decorated reads participate. Authoritative writes and health
probes continue to use fresh searches. Limits measure compact UTF-8 JSON row
bytes, not Python RSS or provider network buffers.
"""

from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
from functools import wraps
import hashlib
import hmac
import inspect
import json
import secrets
import threading

from ads_mcp.errors import ToolError

MAX_ROWS = 10_000
MAX_BYTES = 16 * 1024 * 1024
MAX_SNAPSHOTS = 16
TOTAL_BYTES = 64 * 1024 * 1024
LIFETIME = 300
BOUND_GUIDANCE = (
    "Results are truncated at the retained row or byte limit. Narrow the query, "
    "filters or date window to retrieve additional rows. Continuation covers "
    "only this retained prefix."
)
HISTORY_GUIDANCE = (
    "The query reached its 1000-event ceiling. Narrow the date window "
    "to retrieve additional events. Local page tokens cover only "
    "these capped results; they cannot recover events beyond 1000. "
    "If more than 1000 events share one timestamp, use a different "
    "query via run_gaql (upstream maximum LIMIT 10000)."
)


def bounded_rows(rows):
    """Retain a projected prefix, consuming at most one rejected row."""
    retained, total = [], 0
    iterator = iter(rows)
    try:
        for row in iterator:
            if len(retained) == MAX_ROWS:
                return retained, total, True
            encoded = json.dumps(row, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            if total + len(encoded) > MAX_BYTES:
                return retained, total, True
            retained.append(encoded)
            total += len(encoded)
        return retained, total, False
    finally:
        close = getattr(iterator, "close", None)
        if close:
            close()


class SnapshotStore:
    def __init__(self):
        self.lock = threading.RLock()
        self.snapshots = OrderedDict()
        self.total = 0
        self.secret = secrets.token_bytes(32)

    def _drop(self, identifier):
        snapshot = self.snapshots.pop(identifier)
        self.total -= snapshot[3]

    def _expire(self, now):
        for identifier, snapshot in list(self.snapshots.items()):
            if now - snapshot[0] >= LIFETIME:
                self._drop(identifier)

    def _token(self, identifier, offset):
        body = f"{identifier}.{offset}"
        signature = hmac.new(self.secret, body.encode(), hashlib.sha256).hexdigest()
        return f"{body}.{signature}"

    @staticmethod
    def _refuse():
        raise ToolError(
            "INVALID_PAGE_TOKEN",
            "Continuation is invalid, belongs to another scope/server, or has "
            "expired or been evicted. Restart from the first page without a "
            "page token; the new result may differ.",
        )

    def page(self, *, scope, token, size, clock, build, key, count_key, history_cap):
        # Serialize snapshot construction as well as insertion so concurrent
        # builders cannot each retain an extra unaccounted snapshot.
        with self.lock:
            self._expire(clock())
            if token is not None:
                if not isinstance(token, str) or len(token) > 160:
                    self._refuse()
                parts = token.split(".")
                if len(parts) != 3:
                    self._refuse()
                identifier, raw_offset, _ = parts
                if not raw_offset.isascii() or not raw_offset.isdecimal():
                    self._refuse()
                offset = int(raw_offset)
                expected = self._token(identifier, offset)
                if not hmac.compare_digest(token.encode(), expected.encode()):
                    self._refuse()
                snapshot = self.snapshots.get(identifier)
                if snapshot is None or snapshot[1] != scope:
                    self._refuse()
                if offset <= 0 or offset >= len(snapshot[2]) or offset % size:
                    self._refuse()
            else:
                created = clock()
                payload, rows, byte_count, truncated = build()
                # Summary reads return already bounded aggregate metadata.
                if rows is None:
                    return payload
                if count_key:
                    payload[count_key] = len(rows)
                if truncated:
                    payload.update(possibly_truncated=True, guidance=BOUND_GUIDANCE)
                elif history_cap and len(rows) >= history_cap:
                    payload.update(possibly_truncated=True, guidance=HISTORY_GUIDANCE)
                identifier = secrets.token_hex(16)
                snapshot = (created, scope, rows, byte_count, payload)
                offset = 0
                self._expire(clock())
                while self.snapshots and (
                    len(self.snapshots) >= MAX_SNAPSHOTS or self.total + byte_count > TOTAL_BYTES
                ):
                    self._drop(next(iter(self.snapshots)))
                self.snapshots[identifier] = snapshot
                self.total += byte_count
            payload = deepcopy(snapshot[4])
            payload[key] = [json.loads(row) for row in snapshot[2][offset:offset + size]]
            if offset + size < len(snapshot[2]):
                payload["next_page_token"] = self._token(identifier, offset + size)
            return payload


def retained(key, *, count_key=None, history_cap=None):
    """Paginate a lazy projected payload, binding all effective call inputs."""
    def decorate(function):
        signature = inspect.signature(function)

        @wraps(function)
        def call(*args, **kwargs):
            bound = signature.bind(*args, **kwargs)
            bound.apply_defaults()
            inputs = dict(bound.arguments)
            ctx = inputs.pop("ctx")
            token = inputs.pop("page_token", None)
            cid = ctx.resolve_customer(inputs.get("customer_id"))
            inputs["customer_id"] = cid
            size = min(inputs.get("page_size") or ctx.config.row_limit, ctx.config.row_limit)
            if "page_size" in inputs:
                inputs["page_size"] = size
            scope = json.dumps([function.__module__, function.__name__, size, inputs], sort_keys=True)

            def build():
                payload = function(*args, **kwargs)
                if key not in payload:
                    return payload, None, 0, False
                rows, byte_count, truncated = bounded_rows(payload.pop(key))
                return payload, rows, byte_count, truncated

            return ctx.snapshots.page(
                scope=scope, token=token, size=size, clock=ctx.clock,
                build=lambda: ctx.retry_account_read(build, cid), key=key,
                count_key=count_key, history_cap=history_cap,
            )
        return call
    return decorate
