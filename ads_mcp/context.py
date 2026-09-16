"""Shared per-server context handed to every tool implementation."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from ads_mcp import auth
from ads_mcp.continuation import SnapshotStore
from ads_mcp.transport import run_with_retry


@dataclass
class ServerContext:
    config: Any
    injected_client: Any = None
    plan_store: Any = None
    clock: Callable[[], float] = None
    secrets: Optional[auth.Secrets] = None
    audit: Any = None
    _client: Any = field(default=None, repr=False)
    # Tool calls run on a worker-thread pool, so per-call state must not be
    # shared: a retry record would otherwise be stamped with another call's
    # tool name.
    _local: threading.local = field(default_factory=threading.local, repr=False)
    # Keep each apply's account rechecks, dependent writes and audit outcome
    # together. Previews use only the plan store's short bookkeeping lock.
    application_lock: Any = field(default_factory=threading.Lock, repr=False)
    snapshots: SnapshotStore = field(default_factory=SnapshotStore, repr=False)

    @property
    def current_tool(self) -> str:
        return getattr(self._local, "tool", "")

    @current_tool.setter
    def current_tool(self, value: str) -> None:
        self._local.tool = value

    @property
    def current_plan_id(self):
        return getattr(self._local, "plan_id", None)

    @current_plan_id.setter
    def current_plan_id(self, value) -> None:
        self._local.plan_id = value

    def client(self):
        """The GoogleAdsClient: injected by the oracle, else built once from
        real credentials (structured AUTH_CONFIG_* on failure)."""
        if self.injected_client is not None:
            return self.injected_client
        if self._client is None:
            self._client = auth.build_client(self.config)
        return self._client

    def scrub(self, text: str) -> str:
        return self.secrets.scrub(text) if self.secrets else text

    @property
    def current_customer(self) -> str:
        """The account the in-flight call actually addresses (thread-local),
        so audit records written during a cross-account read carry the id
        that was queried, not the configured one."""
        return getattr(self._local, "customer", None) or self.config.customer_id

    @current_customer.setter
    def current_customer(self, value) -> None:
        self._local.customer = value

    def retry(self, call):
        def _on_retry(attempt, exc):
            self.observe_audit(
                {
                    "event": "retry",
                    "tool": self.current_tool or "unknown",
                    "customer_id": self.current_customer,
                    "outcome": f"transient fault, attempt {attempt}",
                    "fault": type(exc).__name__,
                },
            )

        return run_with_retry(
            call,
            base_seconds=self.config.retry_base_seconds,
            on_retry=_on_retry,
        )

    def observe_audit(self, record: dict) -> bool:
        """Best-effort observation; logging failure must not blind reads.

        Mutation preflight writes use the audit's critical path directly.
        """
        if self.audit is None:
            return False
        try:
            return self.audit.write(record, critical=False)
        except Exception:
            return False

    def audit_auth_failure(self, error) -> None:
        if error.code == "AUTH_TOKEN_REVOKED":
            record = {
                "event": "auth_failure",
                "tool": self.current_tool,
                "customer_id": self.current_customer,
                "outcome": error.code,
            }
            if self.current_plan_id is not None:
                record["plan_id"] = self.current_plan_id
            self.observe_audit(record)

    def mutate_once(self, call):
        """Send a MUTATION exactly once — never retried.

        Google Ads mutates carry no idempotency key, so a retry after a lost
        response does not repair the call, it repeats it: the classic
        "server applied it, response never arrived" fault would create the
        campaign (or keyword, or asset) two or three times. Reads retry;
        writes get one attempt and an error that says so — because a caller
        told only "INTERNAL" will re-plan and re-apply, recreating exactly
        the duplicate this refusal exists to prevent.
        """
        from ads_mcp.errors import ToolError
        from ads_mcp.transport import is_transient

        try:
            return call()
        except BaseException as exc:  # noqa: BLE001 — reclassified below
            if is_transient(exc):
                raise ToolError(
                    "MUTATION_TRANSPORT_FAILED",
                    "the write was sent but its response was lost "
                    f"({type(exc).__name__}). It is NOT retried, because a "
                    "retry would risk applying it twice — the change MAY "
                    "have landed. Check the account (and the audit log) "
                    "before re-planning.",
                ) from None
            raise

    def audit_step(self, *, service: str, method: str, operations: int) -> None:
        """Record a mutate request that actually reached the API.

        Written per step, so a multi-step apply whose later step fails still
        leaves an exact record of what landed — the audit log must never
        deny a change that happened.
        """
        if self.audit is None:
            return
        wrote = self.audit.write(
            {
                "event": "step_applied",
                "tool": self.current_tool or "unknown",
                "customer_id": self.config.customer_id,
                "outcome": "sent",
                "plan_id": self.current_plan_id,
                "service": service,
                "method": method,
                "operations": operations,
            },
            critical=False,
        )
        if not wrote:
            # Confirmation surfaces this loss in its structured result, even
            # when a later step fails. Keep private log paths off stderr.
            self._local.audit_loss = True

    @property
    def audit_loss(self) -> bool:
        return getattr(self._local, "audit_loss", False)

    def clear_audit_loss(self) -> None:
        self._local.audit_loss = False

    def login_header_customer_id(self) -> str | None:
        """The login-customer id sent with cross-account reads under an MCC.

        Swapping customer_id alone is not enough: without this header the API
        answers USER_PERMISSION_DENIED for any account other than the one the
        credentials belong to.
        """
        return self.config.login_customer_id

    def resolve_customer(self, customer_id=None) -> str:
        """The account a call actually addresses. Normalizes and validates.

        Only an ABSENT customer_id falls back to the configured account; an
        explicit malformed value (including an empty string) is refused with
        the offending value quoted, before any API call.
        """
        from ads_mcp.config import ConfigError, normalize_customer_id
        from ads_mcp.errors import ToolError

        if customer_id is None:
            return self.config.customer_id
        try:
            return normalize_customer_id(str(customer_id))
        except ConfigError as exc:
            raise ToolError("INVALID_CUSTOMER_ID", str(exc)) from None

    def retry_account_read(self, call, customer_id: str):
        """Retry a complete read and attribute permission failures to its account.

        Materialize lazy provider pages inside ``call`` so failures while
        fetching subsequent pages receive the same handling as the first.
        """
        from google.api_core import exceptions as core_exceptions
        from google.ads.googleads.errors import GoogleAdsException

        from ads_mcp.errors import ToolError

        cid = self.resolve_customer(customer_id)
        self.current_customer = cid
        login = self.login_header_customer_id()
        try:
            return self.retry(call)
        except GoogleAdsException as exc:
            inaccessible = {
                "USER_PERMISSION_DENIED",
                "CUSTOMER_NOT_ENABLED",
                "INVALID_LOGIN_CUSTOMER_ID_SERVING_CUSTOMER_ID_COMBINATION",
            }
            for error in exc.failure.errors:
                authorization = error.error_code.authorization_error
                if getattr(authorization, "name", None) in inaccessible:
                    raise ToolError(
                        "ACCOUNT_NOT_ACCESSIBLE",
                        f"customer {cid} is not reachable with these credentials "
                        f"(login customer {login or 'unset'}): {authorization.name}",
                    ) from None
            raise
        except core_exceptions.PermissionDenied as exc:
            raise ToolError(
                "ACCOUNT_NOT_ACCESSIBLE",
                f"customer {cid} is not reachable with these credentials "
                f"(login customer {login or 'unset'}): {type(exc).__name__}",
            ) from None

    def search(self, query: str, customer_id: str | None = None):
        """One GAQL search through the retry layer; returns row list."""
        cid = self.resolve_customer(customer_id)
        return self.retry_account_read(lambda: list(self.search_iter(query, cid)), cid)

    def search_iter(self, query: str, customer_id: str | None = None):
        """Lazy search; caller must consume inside retry_account_read.

        Public retained reads bound consumption after projection. Ordinary
        search callers still materialize a fresh complete result above.
        """
        cid = self.resolve_customer(customer_id)
        client = self.client()
        self.current_customer = cid
        # Cross-account reads need the manager login on the client, or the API
        # refuses them outright.
        login = self.login_header_customer_id()
        if login and getattr(client, "login_customer_id", None) != login:
            client.login_customer_id = login
        service = client.get_service("GoogleAdsService")
        return service.search(customer_id=cid, query=query)
