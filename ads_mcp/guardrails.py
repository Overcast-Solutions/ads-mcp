"""Guardrail core: the plan store and spend caps.

Every mutation returns a single-use, expiring plan; execution happens only
through confirm_and_apply. Caps come exclusively from environment
configuration — no tool parameter can raise, skip, or disable them. Unset
caps refuse budget changes and bid increases (GUARDRAIL_CAP_UNSET).
Verified bid reductions remain allowed.
"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Optional

from ads_mcp.errors import ToolError


def _iso_utc(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


@dataclass
class PlanEntry:
    id: str
    tool: str
    customer_id: str
    summary: str
    operations: list
    irreversible: bool
    expires_epoch: float
    execute: Callable[[Any], Any]
    rechecks: list = field(default_factory=list)
    previewed: bool = False
    consumed: bool = False

    def payload(self) -> dict:
        return {
            "id": self.id,
            "tool": self.tool,
            "summary": self.summary,
            "operations": self.operations,
            "irreversible": self.irreversible,
            "expires_at": _iso_utc(self.expires_epoch),
        }


MAX_RETAINED_PLANS = 500


class PlanStore:
    """Single-use, expiring mutation plans (default TTL 15 minutes).

    Single-use is enforced by :meth:`claim`, which checks and burns the
    plan under one lock. It has to be atomic: the MCP server dispatches sync
    tool functions onto a worker-thread pool, so two `confirm_and_apply`
    calls for the same plan genuinely run in parallel — an MCP client that
    times out and retries a slow apply is enough to duplicate a campaign.
    """

    def __init__(self, *, ttl_seconds: float = 900.0, clock=None):
        self.ttl_seconds = float(ttl_seconds)
        self.clock = clock if clock is not None else time.time
        self._plans: dict[str, PlanEntry] = {}
        self._evicted: set[str] = set()
        self._expired: set[str] = set()
        self._lock = threading.Lock()

    def _evict_locked(self) -> None:
        """Drop expired plans (and, past a hard cap, the oldest) so a
        long-lived unattended process cannot grow without bound."""
        now = self.clock()
        for plan_id in [k for k, v in self._plans.items() if now > v.expires_epoch]:
            del self._plans[plan_id]
            self._expired.add(plan_id)
        if len(self._plans) > MAX_RETAINED_PLANS:
            for plan_id in sorted(
                self._plans, key=lambda k: self._plans[k].expires_epoch
            )[: len(self._plans) - MAX_RETAINED_PLANS]:
                del self._plans[plan_id]
                self._evicted.add(plan_id)
        # The bookkeeping sets must not become the leak they were added to
        # prevent.
        for tracker in (self._evicted, self._expired):
            while len(tracker) > MAX_RETAINED_PLANS:
                tracker.pop()

    def create(self, *, tool: str, customer_id: str, summary: str,
               operations: list, execute, irreversible: bool = False,
               rechecks: Optional[list] = None) -> PlanEntry:
        entry = PlanEntry(
            id=uuid.uuid4().hex[:12],
            tool=tool,
            customer_id=customer_id,
            summary=summary,
            operations=operations,
            irreversible=irreversible,
            expires_epoch=self.clock() + self.ttl_seconds,
            execute=execute,
            rechecks=list(rechecks or []),
        )
        with self._lock:
            self._evict_locked()
            self._plans[entry.id] = entry
        return entry

    def _lookup_locked(self, plan_id: str) -> PlanEntry:
        entry = self._plans.get(str(plan_id))
        if entry is None:
            if str(plan_id) in self._expired:
                raise ToolError(
                    "PLAN_EXPIRED",
                    f"plan {plan_id} expired and has since been swept; "
                    "its execution history is no longer retained. Check prior "
                    "results, the account and audit log before staging a fresh plan",
                )
            if str(plan_id) in self._evicted:
                raise ToolError(
                    "PLAN_EVICTED",
                    f"plan {plan_id} was dropped to bound memory after "
                    f"{MAX_RETAINED_PLANS} retained plans accumulated; "
                    "its execution history is no longer retained. Check prior "
                    "results, the account and audit log before staging a fresh plan",
                )
            raise ToolError(
                "PLAN_NOT_FOUND",
                f"no plan with id {plan_id!r} is retained; execution history is "
                "unavailable. Check prior results, the account and audit log "
                "before staging a fresh plan",
            )
        if self.clock() > entry.expires_epoch:
            raise ToolError(
                "PLAN_EXPIRED",
                f"plan {entry.id} expired at {_iso_utc(entry.expires_epoch)}; "
                "it cannot execute now. An earlier attempt may have applied "
                "changes; check prior results, the account and audit log before "
                "staging a fresh plan",
            )
        return entry

    def discard(self, plan_id: str) -> None:
        with self._lock:
            self._plans.pop(str(plan_id), None)

    def get(self, plan_id: str) -> PlanEntry:
        with self._lock:
            return self._lookup_locked(plan_id)

    def mark_previewed(self, plan_id: str) -> PlanEntry:
        with self._lock:
            entry = self._lookup_locked(plan_id)
            entry.previewed = True
            return entry

    def _claimable_locked(self, plan_id: str, customer_id: str,
                          require_previewed: bool) -> PlanEntry:
        entry = self._lookup_locked(plan_id)
        if entry.customer_id != customer_id:
            raise ToolError(
                "PLAN_CUSTOMER_MISMATCH",
                f"plan {entry.id} was staged for customer "
                f"{entry.customer_id}, not {customer_id}",
            )
        if entry.consumed:
            raise ToolError(
                "PLAN_CONSUMED",
                f"plan {entry.id} has already been attempted; plans are single-use. "
                "Check prior results, the account and audit log before staging again",
            )
        if require_previewed and not entry.previewed:
            raise ToolError(
                "DRY_RUN_REQUIRED",
                "ADS_MCP_REQUIRE_DRY_RUN is on: preview this plan with "
                "dry_run=true before applying; no parameter bypasses this",
            )
        return entry

    def claim(self, plan_id: str, *, customer_id: str, require_previewed: bool,
              validate=None) -> PlanEntry:
        """Validate, then atomically check and BURN; only one caller can win.

        Account reads run outside the bookkeeping lock so previews stay
        responsive. The caller holds its context's application lock from
        before validation through execution and auditing. Recheck store state
        after validation: expiry, eviction or another claim must still refuse.
        """
        with self._lock:
            entry = self._claimable_locked(plan_id, customer_id, require_previewed)
        if validate is not None:
            validate(entry)
        with self._lock:
            entry = self._claimable_locked(plan_id, customer_id, require_previewed)
            entry.consumed = True
            return entry


# ---------------------------------------------------------------------------
# Spend caps — configuration is the only source; tools carry no override.


def _finite(value: float, code: str, label: str) -> float:
    """nan defeats every comparison (nan > cap is False) and inf breaks the
    later micros conversion; neither may reach a cap check."""
    if value != value or value in (float("inf"), float("-inf")):
        raise ToolError(code, f"{label} must be a finite number, got {value!r}")
    return value


def check_budget(ctx, amount: float, current: float | None = None) -> None:
    """``current`` is the entity's existing daily budget where known: a move
    DOWN from it is allowed above a configured cap. An unset cap still refuses
    every budget change, and callers must verify the current account baseline."""
    try:
        value = float(amount)
    except (TypeError, ValueError):
        raise ToolError("INVALID_BUDGET", f"daily_budget {amount!r} is not a number") from None
    _finite(value, "INVALID_BUDGET", "daily_budget")
    if value > 1e9:
        raise ToolError(
            "INVALID_BUDGET", f"daily_budget {value:g} is implausibly large"
        )
    if value < 0.01:
        raise ToolError(
            "INVALID_BUDGET",
            f"daily_budget must be at least 0.01 in the account currency, got {value:g}",
        )
    cap = ctx.config.max_daily_budget
    if cap is None:
        raise ToolError(
            "GUARDRAIL_CAP_UNSET",
            "budget changes are refused because ADS_MCP_MAX_DAILY_BUDGET is "
            "not set; configure the cap to enable any budget mutation",
        )
    if value > cap and not (current is not None and value <= float(current)):
        raise ToolError(
            "BUDGET_CAP_EXCEEDED",
            f"daily_budget {value:g} exceeds the configured cap "
            f"ADS_MCP_MAX_DAILY_BUDGET={cap:g}",
        )


def bid_value(new) -> Decimal:
    """Validate a currency bid without rounding a decimal cap boundary."""
    try:
        new_value = Decimal(str(new))
    except (InvalidOperation, TypeError, ValueError):
        raise ToolError("INVALID_BID", f"bid {new!r} is not a number") from None
    if not new_value.is_finite():
        raise ToolError("INVALID_BID", f"bid must be a finite number, got {new!r}")
    if new_value <= 0:
        raise ToolError("INVALID_BID", f"bid must be positive, got {new_value}")
    if new_value < Decimal("0.000001"):
        raise ToolError("INVALID_BID", "bid must be at least one micro-unit in the account currency")
    if new_value > 1e6:
        raise ToolError("INVALID_BID", f"bid {new_value:g} is implausibly large")
    return new_value


def check_bid(ctx, current, new, account_current=None) -> None:
    """Use only a verified account baseline; ``current`` is compatibility-only.

    A zero baseline must come from successful reads or a new entity's CREATE.
    Never substitute zero for an unavailable lookup. Callers must re-read
    existing resources before invoking this check at application.
    """
    new_value = bid_value(new)
    if account_current is None:
        raise ToolError("BID_BASELINE_UNVERIFIED", "the account bid was not verified")
    account = Decimal(str(account_current))
    if account <= 0:
        check_first_bid(ctx, new_value, baseline_verified=True)
        return
    if new_value <= account:
        return
    cap_pct = ctx.config.max_bid_increase_pct
    if cap_pct is None:
        raise ToolError(
            "GUARDRAIL_CAP_UNSET",
            "bid increases are refused because ADS_MCP_MAX_BID_INCREASE_PCT "
            "is not set; configure the cap to enable bid increases",
        )
    ceiling = account * (1 + Decimal(str(cap_pct)) / 100)
    if new_value > ceiling:
        raise ToolError(
            "BID_CAP_EXCEEDED",
            f"bid increase from {account:g} to {new_value:g} exceeds the "
            f"configured limit ADS_MCP_MAX_BID_INCREASE_PCT={cap_pct:g}",
        )


def check_first_bid(ctx, new: float, *, baseline_verified: bool) -> None:
    """Bound a bid on an entity that has none.

    A percentage cap is meaningless against zero, so first bids are bounded by
    an absolute ceiling. ``baseline_verified`` must be True only when the
    absence of a prior bid was established by successful account reads, or
    by a CREATE with no existing parent bid to inherit.
    """
    value = bid_value(new)
    if not baseline_verified:
        raise ToolError("BID_BASELINE_UNVERIFIED", "the account bid was not verified")
    cap = ctx.config.max_first_bid
    if cap is None:
        raise ToolError(
            "GUARDRAIL_CAP_UNSET",
            "no existing bid applies; configure ADS_MCP_MAX_FIRST_BID to "
            "set a first bid",
        )
    if value > Decimal(str(cap)):
        raise ToolError(
            "FIRST_BID_CAP_EXCEEDED",
            f"first bid {value:g} exceeds ADS_MCP_MAX_FIRST_BID={cap:g}",
        )
