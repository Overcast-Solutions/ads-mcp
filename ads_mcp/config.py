"""Environment-driven configuration.

Environment contract: required
GOOGLE_ADS_CUSTOMER_ID,
GOOGLE_ADS_CREDENTIALS_PATH, GOOGLE_ADS_TOKEN_PATH; optional
GOOGLE_ADS_LOGIN_CUSTOMER_ID, ignored legacy GOOGLE_ADS_DEVELOPER_TOKEN,
and every ADS_MCP_* guardrail variable
(safe defaults: read-only on, dry-run required, caps refuse when unset).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional


class ConfigError(Exception):
    """Missing/malformed configuration. The message names the exact variable."""


_REQUIRED = {
    "GOOGLE_ADS_CUSTOMER_ID": "123-456-7890",
    "GOOGLE_ADS_CREDENTIALS_PATH": "/path/to/oauth_client.json",
    "GOOGLE_ADS_TOKEN_PATH": "/path/to/oauth_refresh.json",
}


def normalize_customer_id(raw: str) -> str:
    """Normalize ten decimal digits to ASCII, preserving leading zeroes."""
    value = (raw or "").strip()
    cleaned = value.replace("-", "")
    if not (len(cleaned) == 10 and cleaned.isdecimal()):
        raise ConfigError(
            f"customer id {value!r} is not a valid Google Ads customer id "
            "(expected 10 digits, e.g. 123-456-7890)"
        )
    return "".join(str(int(digit)) for digit in cleaned)


def _expand(path: str) -> str:
    import os

    return os.path.expandvars(os.path.expanduser(str(path).strip()))


def _parse_bool(env: Mapping[str, str], name: str, default: bool) -> bool:
    """Fail-safe boolean: only an explicit recognized value moves off the
    default. Junk never errors AND never lands on the permissive side —
    ADS_MCP_READ_ONLY=banana keeps the server read-only."""
    raw = env.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    lowered = str(raw).strip().lower()
    if lowered in ("true", "1", "yes", "on"):
        return True
    if lowered in ("false", "0", "no", "off"):
        return False
    return default


def _parse_float(env: Mapping[str, str], name: str, default=None):
    raw = env.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        value = float(raw)
    except ValueError:
        raise ConfigError(f"{name}={raw!r} is not a number (e.g. 50.00)") from None
    # nan/inf parse as floats but silently disable every comparison that uses
    # them (nan > cap is always False) and serialize as invalid JSON.
    if value != value or value in (float("inf"), float("-inf")):
        raise ConfigError(f"{name}={raw!r} is not a finite number (e.g. 50.00)")
    if value < 0:
        raise ConfigError(f"{name}={raw!r} must not be negative (e.g. 50.00)")
    return value


@dataclass(frozen=True)
class Config:
    """Resolved server configuration."""

    developer_token: str
    customer_id: str
    login_customer_id: Optional[str]
    credentials_path: str
    token_path: str
    read_only: bool
    require_dry_run: bool
    max_daily_budget: Optional[float]
    max_bid_increase_pct: Optional[float]
    max_first_bid: Optional[float]
    audit_log: Optional[str]
    retry_base_seconds: float
    plan_ttl_seconds: float
    row_limit: int


def load_config(env: Mapping[str, str]) -> Config:
    for var, example in _REQUIRED.items():
        value = env.get(var)
        if value is None or not str(value).strip():
            raise ConfigError(
                f"{var} is required but missing or empty (e.g. {var}={example})"
            )

    login_raw = env.get("GOOGLE_ADS_LOGIN_CUSTOMER_ID")
    login_id = None
    if login_raw is not None and str(login_raw).strip():
        login_id = normalize_customer_id(str(login_raw))

    row_limit = int(_parse_float(env, "ADS_MCP_ROW_LIMIT", 1000.0))
    if row_limit < 1:
        raise ConfigError(
            f"ADS_MCP_ROW_LIMIT={env.get('ADS_MCP_ROW_LIMIT')!r} must be at "
            "least 1 (a zero limit paginates forever) (e.g. 1000)"
        )

    retry_base = _parse_float(env, "ADS_MCP_RETRY_BASE_SECONDS", 1.0)
    if not 0 < retry_base <= 30:
        raise ConfigError(
            f"ADS_MCP_RETRY_BASE_SECONDS={env.get('ADS_MCP_RETRY_BASE_SECONDS')!r} "
            "must be greater than 0 and at most 30 (a larger base hangs the "
            "client inside a tool call) (e.g. 1.0)"
        )
    plan_ttl = _parse_float(env, "ADS_MCP_PLAN_TTL_SECONDS", 900.0)
    if not 30 <= plan_ttl <= 86400:
        raise ConfigError(
            f"ADS_MCP_PLAN_TTL_SECONDS={env.get('ADS_MCP_PLAN_TTL_SECONDS')!r} "
            "must be between 30 and 86400 seconds (below 30 a plan expires "
            "before it can be previewed) (e.g. 900)"
        )

    read_only = _parse_bool(env, "ADS_MCP_READ_ONLY", True)
    audit_log = str(env["ADS_MCP_AUDIT_LOG"]) if env.get("ADS_MCP_AUDIT_LOG") else None
    if not read_only and not audit_log:
        raise ConfigError(
            "ADS_MCP_AUDIT_LOG is required when mutations are enabled "
            "(ADS_MCP_READ_ONLY=false): an unauditable write path is refused "
            "(e.g. ADS_MCP_AUDIT_LOG=/var/log/ads-mcp/audit.jsonl)"
        )

    return Config(
        developer_token=str(env.get("GOOGLE_ADS_DEVELOPER_TOKEN") or "").strip(),
        customer_id=normalize_customer_id(str(env["GOOGLE_ADS_CUSTOMER_ID"])),
        login_customer_id=login_id,
        # MCP `env` blocks are passed without shell expansion, and the
        # README's own snippet uses ~ — expand it ourselves.
        credentials_path=_expand(env["GOOGLE_ADS_CREDENTIALS_PATH"]),
        token_path=_expand(env["GOOGLE_ADS_TOKEN_PATH"]),
        read_only=read_only,
        require_dry_run=_parse_bool(env, "ADS_MCP_REQUIRE_DRY_RUN", True),
        max_daily_budget=_parse_float(env, "ADS_MCP_MAX_DAILY_BUDGET"),
        max_bid_increase_pct=_parse_float(env, "ADS_MCP_MAX_BID_INCREASE_PCT"),
        max_first_bid=_parse_float(env, "ADS_MCP_MAX_FIRST_BID"),
        audit_log=audit_log,
        retry_base_seconds=retry_base,
        plan_ttl_seconds=plan_ttl,
        row_limit=row_limit,
    )
