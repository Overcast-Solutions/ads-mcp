"""Structured tool errors: stable codes, scrubbed messages, JSON payloads.

Contract (tests/harness.py): domain failures are RETURNED as
``{"error": {"code": ..., "message": ...}}`` — never raised through the MCP
layer — so an autonomous agent gets a machine-parseable result either way.
Secret material (developer token, client secret, refresh token) must never
appear in a message; auth failures use fixed messages rather than trusting
upstream exception text.
"""

from __future__ import annotations

from google.ads.googleads.errors import GoogleAdsException
from google.auth.exceptions import GoogleAuthError, RefreshError

from ads_mcp.config import ConfigError
from ads_mcp.transport import TransportError

REAUTH_MESSAGE = (
    "The OAuth refresh token has expired or been revoked (invalid_grant). "
    "Re-run the generate-token helper (ads-mcp-generate-token) to complete "
    "the OAuth consent flow again, then restart the server."
)


class ToolError(Exception):
    """Raise inside a tool to return a structured error payload."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message

    def payload(self) -> dict:
        return {"error": {"code": self.code, "message": self.message}}


class AuthConfigError(ToolError):
    """Credential *configuration* problems (missing/unreadable files) —
    distinct from a revoked token."""


def error_payload(code: str, message: str) -> dict:
    return {"error": {"code": code, "message": message}}


def cloud_project_access_error(exc: GoogleAdsException) -> ToolError | None:
    """Only the explicit provider enum establishes a project approval refusal."""
    for error in exc.failure.errors:
        authorization = error.error_code.authorization_error
        if getattr(authorization, "name", None) == (
            "CLOUD_PROJECT_NOT_APPROVED_FOR_PRODUCTION"
        ):
            return ToolError(
                "ADS_CLOUD_PROJECT_NOT_APPROVED",
                "The OAuth Cloud project is not approved for production "
                "Google Ads API access. Open that project's Google Ads API "
                "Overview in Google Cloud Console and apply for the access "
                "level needed for your account and requested services.",
            )
    return None


def classify_exception(exc: BaseException, scrub=lambda s: s) -> ToolError:
    """Map an exception from the API path onto a stable ToolError."""
    if isinstance(exc, ToolError):
        return exc
    if isinstance(exc, RefreshError):
        # Never include upstream text: RefreshError messages can carry the
        # refresh token and client secret verbatim.
        return ToolError("AUTH_TOKEN_REVOKED", REAUTH_MESSAGE)
    if isinstance(exc, GoogleAuthError):
        return ToolError(
            "AUTH_FAILED",
            "Authentication failed. Check the configured OAuth credentials; "
            "if reauthorization is needed, run ads-mcp-generate-token and "
            "restart the server.",
        )
    if isinstance(exc, TransportError):
        return ToolError("TRANSPORT_FAILED", scrub(str(exc)))
    if isinstance(exc, GoogleAdsException):
        project_error = cloud_project_access_error(exc)
        if project_error is not None:
            return project_error
        messages = "; ".join(
            e.message for e in exc.failure.errors if getattr(e, "message", "")
        ) or "the Google Ads API rejected the request"
        rid = getattr(exc, "request_id", "") or ""
        suffix = f" (request_id={rid})" if rid else ""
        return ToolError("ADS_API_REJECTED", scrub(messages + suffix))
    if isinstance(exc, ConfigError):
        return ToolError("CONFIG_INVALID", scrub(str(exc)))
    return ToolError("INTERNAL", scrub(f"{type(exc).__name__}: {exc}"))
