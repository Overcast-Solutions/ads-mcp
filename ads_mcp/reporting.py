"""Curated reporting reads: typed metrics, money strings, bounded rows.

Design rules (spec F006 + the F016 golden contract fixtures): every payload
carries the effective customer_id; date windows are explicit or clock-derived
(last_n_days = N complete days ending yesterday, UTC); micros become decimal
money strings with the account currency ("52.40 USD"); currency-valued floats
(conversion value) render the same way; row-heavy reads are bounded by the
configured row limit with explicit pagination tokens; empty results echo the
queried window instead of erroring.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from ads_mcp.errors import ToolError
from ads_mcp.gaql import extract_field
from ads_mcp.continuation import retained

DEFAULT_CURRENCY = "USD"


def money(micros, currency: str = DEFAULT_CURRENCY) -> str:
    return f"{(int(micros) / 1_000_000):.2f} {currency}"


def money_value(value, currency: str = DEFAULT_CURRENCY) -> str:
    return f"{float(value):.2f} {currency}"


def resolve_window(*, date_range_start=None, date_range_end=None,
                   last_n_days=None, clock=None) -> dict:
    if last_n_days is not None:
        try:
            n = int(last_n_days)
        except (TypeError, ValueError):
            raise ToolError("INVALID_WINDOW", f"last_n_days {last_n_days!r} is not an integer") from None
        if n <= 0 or n > 3650:
            raise ToolError("INVALID_WINDOW", f"last_n_days {n} is outside 1..3650")
        now = datetime.fromtimestamp(clock(), tz=timezone.utc)
        yesterday = (now - timedelta(days=1)).date()
        start = yesterday - timedelta(days=n - 1)
        return {"start": start.isoformat(), "end": yesterday.isoformat()}
    if date_range_start and date_range_end:
        dates = []
        for label, value in (("date_range_start", date_range_start),
                             ("date_range_end", date_range_end)):
            try:
                parsed = date.fromisoformat(value)
                if parsed.isoformat() != value:
                    raise ValueError("date must use YYYY-MM-DD")
            except (TypeError, ValueError):
                raise ToolError("INVALID_WINDOW", f"{label} {value!r} is not YYYY-MM-DD") from None
            dates.append(parsed)
        start, end = dates
        if end < start:
            raise ToolError("INVALID_WINDOW", "date_range_end precedes date_range_start")
        return {"start": start.isoformat(), "end": end.isoformat()}
    raise ToolError(
        "INVALID_WINDOW",
        "provide date_range_start + date_range_end (YYYY-MM-DD) or last_n_days",
    )


def window_clause(window: dict) -> str:
    return f"segments.date BETWEEN '{window['start']}' AND '{window['end']}'"


def _currency(row) -> str:
    code = extract_field(row, "customer.currency_code")
    return code or DEFAULT_CURRENCY


def base_payload(ctx, customer_id=None, **extra) -> dict:
    """Every read echoes the account it ACTUALLY queried, so a caller can
    never mistake which account answered."""
    return {"customer_id": ctx.resolve_customer(customer_id), **extra}


def normalize_campaign_id(campaign_id, *, optional=False) -> str | None:
    """Use one numeric campaign identity for queries, matching and payloads."""
    if campaign_id is None and optional:
        return None
    text = str(campaign_id).strip()
    if not text.isdecimal():
        raise ToolError("INVALID_ID", f"campaign_id must be numeric, got {campaign_id!r}")
    try:
        return str(int(text))
    except ValueError:
        raise ToolError(
            "INVALID_ID", "campaign_id is too long; provide a numeric campaign ID",
        ) from None


def campaign_clause(campaign_id) -> str:
    """Server-side campaign scoping. An agent that guesses a campaign_id must
    get an empty result, never a silent whole-account answer."""
    campaign_id = normalize_campaign_id(campaign_id, optional=True)
    if campaign_id is None:
        return ""
    return f" AND campaign.id = {campaign_id}"


# ---------------------------------------------------------------------------
# Campaigns


CAMPAIGN_QUERY = (
    "SELECT campaign.id, campaign.name, campaign.status, "
    "campaign.serving_status, campaign.primary_status, "
    "campaign.primary_status_reasons, campaign.advertising_channel_type, "
    "campaign.bidding_strategy_type, "
    "campaign.maximize_conversion_value.target_roas, "
    "campaign.maximize_conversions.target_cpa_micros, "
    "campaign.target_roas.target_roas, campaign.target_cpa.target_cpa_micros, "
    "campaign_budget.id, campaign_budget.amount_micros, "
    "customer.currency_code, metrics.impressions, metrics.clicks, "
    "metrics.cost_micros, metrics.conversions, metrics.conversions_value, "
    "metrics.ctr, metrics.average_cpc "
    "FROM campaign WHERE {window} ORDER BY campaign.id"
)


def _bidding_strategy(row, currency) -> dict:
    strategy = {
        "type": row.campaign.bidding_strategy_type.name,
        "target_roas": None,
        "target_cpa": None,
    }
    for path in ("campaign.maximize_conversion_value.target_roas",
                 "campaign.target_roas.target_roas"):
        value = extract_field(row, path)
        if value:
            strategy["target_roas"] = float(value)
    for path in ("campaign.maximize_conversions.target_cpa_micros",
                 "campaign.target_cpa.target_cpa_micros"):
        micros = extract_field(row, path)
        if micros:
            strategy["target_cpa"] = money(micros, currency)
    return strategy


@retained("campaigns")
def get_campaign_performance(ctx, *, window, enabled_only=False, page_token=None,
                             customer_id=None, campaign_id=None) -> dict:
    campaign_id = normalize_campaign_id(campaign_id, optional=True)
    scope = campaign_clause(campaign_id)
    query = CAMPAIGN_QUERY.format(window=window_clause(window) + scope)
    if enabled_only:
        query = query.replace(
            "ORDER BY campaign.id",
            "AND campaign.status = 'ENABLED' ORDER BY campaign.id",
        )
    rows = ctx.search_iter(query, customer_id=customer_id)
    if enabled_only:
        # The GAQL WHERE does this server-side in production; re-applying on
        # the returned rows keeps the guarantee independent of transport.
        rows = (r for r in rows if r.campaign.status.name == "ENABLED")
    if scope:
        rows = (r for r in rows if str(r.campaign.id) == campaign_id)
    def campaigns():
        for row in rows:
            currency = _currency(row)
            yield (
                {
                    "campaign_id": str(row.campaign.id),
                    "name": row.campaign.name,
                    "status": row.campaign.status.name,
                    "serving_status": row.campaign.serving_status.name,
                    "primary_status": row.campaign.primary_status.name,
                    "primary_status_reasons": [
                        r.name for r in row.campaign.primary_status_reasons
                    ],
                    "channel_type": row.campaign.advertising_channel_type.name,
                    "daily_budget": money(row.campaign_budget.amount_micros, currency),
                    "bidding_strategy": _bidding_strategy(row, currency),
                    "impressions": int(row.metrics.impressions),
                    "clicks": int(row.metrics.clicks),
                    "cost": money(row.metrics.cost_micros, currency),
                    "conversions": float(row.metrics.conversions),
                    "conversions_value": money_value(row.metrics.conversions_value, currency),
                    "ctr": float(row.metrics.ctr),
                    "average_cpc": money(row.metrics.average_cpc, currency),
                }
            )
    payload = base_payload(ctx, customer_id=customer_id, window=window,
                           campaigns=campaigns())
    if scope:
        payload["campaign_id"] = campaign_id
    return payload


# ---------------------------------------------------------------------------
# Simple windowed reports (ads / keywords / search terms / geo)


def _windowed(ctx, *, resource: str, select: str, key: str, window,
              shape, page_token=None, customer_id=None, campaign_id=None) -> dict:
    campaign_id = normalize_campaign_id(campaign_id, optional=True)
    scope = campaign_clause(campaign_id)
    query = (
        f"SELECT {select}, customer.currency_code, metrics.impressions, "
        f"metrics.clicks, metrics.cost_micros, metrics.conversions "
        f"FROM {resource} WHERE {window_clause(window)}{scope}"
    )
    rows = ctx.search_iter(query, customer_id=customer_id)
    if scope:
        rows = (r for r in rows if str(r.campaign.id) == campaign_id)
    shaped = (shape(row, _currency(row)) for row in rows)
    payload = base_payload(ctx, customer_id=customer_id, window=window)
    if scope:
        payload["campaign_id"] = campaign_id
    payload[key] = shaped
    return payload


def _core_metrics(row, currency) -> dict:
    return {
        "impressions": int(row.metrics.impressions),
        "clicks": int(row.metrics.clicks),
        "cost": money(row.metrics.cost_micros, currency),
        "conversions": float(row.metrics.conversions),
    }


@retained("ads")
def get_ad_performance(ctx, *, window, page_token=None, customer_id=None, campaign_id=None) -> dict:
    return _windowed(
        ctx,
        resource="ad_group_ad",
        select=(
            "ad_group_ad.ad.id, ad_group_ad.ad.type, ad_group_ad.status, "
            "ad_group.id, ad_group.name, campaign.id, campaign.name, metrics.ctr"
        ),
        key="ads",
        window=window,
        page_token=page_token,
        customer_id=customer_id,
        campaign_id=campaign_id,
        shape=lambda row, cur: {
            "ad_id": str(row.ad_group_ad.ad.id),
            "ad_type": row.ad_group_ad.ad.type_.name,
            "status": row.ad_group_ad.status.name,
            "ad_group_id": str(row.ad_group.id),
            "ad_group_name": row.ad_group.name,
            "campaign_id": str(row.campaign.id),
            "campaign_name": row.campaign.name,
            **_core_metrics(row, cur),
            "ctr": float(row.metrics.ctr),
        },
    )


@retained("keywords")
def get_keyword_performance(ctx, *, window, page_token=None, customer_id=None, campaign_id=None) -> dict:
    def shape(row, cur):
        quality = extract_field(row, "ad_group_criterion.quality_info.quality_score")
        return {
            "criterion_id": str(row.ad_group_criterion.criterion_id),
            "keyword": row.ad_group_criterion.keyword.text,
            "match_type": row.ad_group_criterion.keyword.match_type.name,
            "status": row.ad_group_criterion.status.name,
            "ad_group_id": str(row.ad_group.id),
            "campaign_id": str(row.campaign.id),
            "cpc_bid": money(row.ad_group_criterion.cpc_bid_micros, cur),
            "quality_score": int(quality) if quality is not None else None,
            **_core_metrics(row, cur),
        }

    return _windowed(
        ctx,
        resource="keyword_view",
        select=(
            "ad_group_criterion.criterion_id, ad_group_criterion.keyword.text, "
            "ad_group_criterion.keyword.match_type, ad_group_criterion.status, "
            "ad_group_criterion.cpc_bid_micros, "
            "ad_group_criterion.quality_info.quality_score, ad_group.id, campaign.id"
        ),
        key="keywords",
        window=window,
        page_token=page_token,
        customer_id=customer_id,
        campaign_id=campaign_id,
        shape=shape,
    )


@retained("search_terms")
def get_search_terms(ctx, *, window, page_token=None, customer_id=None, campaign_id=None) -> dict:
    return _windowed(
        ctx,
        resource="search_term_view",
        select="search_term_view.search_term, ad_group.id, campaign.id",
        key="search_terms",
        window=window,
        page_token=page_token,
        customer_id=customer_id,
        campaign_id=campaign_id,
        shape=lambda row, cur: {
            "search_term": row.search_term_view.search_term,
            "ad_group_id": str(row.ad_group.id),
            "campaign_id": str(row.campaign.id),
            **_core_metrics(row, cur),
        },
    )


@retained("locations")
def get_geo_performance(ctx, *, window, page_token=None, customer_id=None, campaign_id=None) -> dict:
    return _windowed(
        ctx,
        resource="geographic_view",
        select=(
            "geographic_view.country_criterion_id, geographic_view.location_type, "
            "campaign.id"
        ),
        key="locations",
        window=window,
        page_token=page_token,
        customer_id=customer_id,
        campaign_id=campaign_id,
        shape=lambda row, cur: {
            "country_id": str(row.geographic_view.country_criterion_id),
            "location_type": row.geographic_view.location_type.name,
            "campaign_id": str(row.campaign.id),
            **_core_metrics(row, cur),
        },
    )


# ---------------------------------------------------------------------------
# Account and resource inventory reads


def get_account_info(ctx, *, customer_id=None) -> dict:
    cid = ctx.resolve_customer(customer_id)
    rows = ctx.search(
        "SELECT customer.id, customer.descriptive_name, customer.currency_code, "
        "customer.time_zone, customer.auto_tagging_enabled, customer.manager, "
        "customer.test_account FROM customer LIMIT 1",
        customer_id=cid,
    )
    if not rows:
        raise ToolError("NOT_FOUND", "customer returned no rows")
    c = rows[0].customer
    return {
        "customer_id": cid,
        "name": c.descriptive_name,
        "currency_code": c.currency_code,
        "time_zone": c.time_zone,
        "auto_tagging_enabled": bool(c.auto_tagging_enabled),
        "manager": bool(c.manager),
        "test_account": bool(c.test_account),
    }


def list_accounts(ctx) -> dict:
    root = ctx.login_header_customer_id() or ctx.resolve_customer()
    rows = ctx.search(
        "SELECT customer_client.id, customer_client.descriptive_name, "
        "customer_client.manager, customer_client.level, customer_client.status "
        "FROM customer_client",
        customer_id=root,
    )
    return base_payload(
        ctx,
        customer_id=root,
        accounts=[
            {
                # SDK numeric IDs omit the leading zeroes of account selectors.
                "customer_id": str(r.customer_client.id).zfill(10),
                "name": r.customer_client.descriptive_name,
                "manager": bool(r.customer_client.manager),
                "level": int(r.customer_client.level),
                "status": r.customer_client.status.name,
            }
            for r in rows
        ],
    )


def get_conversion_actions(ctx, *, customer_id=None) -> dict:
    rows = ctx.search(
        "SELECT conversion_action.id, conversion_action.name, "
        "conversion_action.category, conversion_action.type, "
        "conversion_action.status, conversion_action.counting_type, "
        "conversion_action.primary_for_goal FROM conversion_action",
        customer_id=customer_id,
    )
    return base_payload(
        ctx,
        customer_id=customer_id,
        conversion_actions=[
            {
                "id": str(r.conversion_action.id),
                "name": r.conversion_action.name,
                "category": r.conversion_action.category.name,
                "type": r.conversion_action.type_.name,
                "status": r.conversion_action.status.name,
                "counting_type": r.conversion_action.counting_type.name,
                "primary_for_goal": bool(r.conversion_action.primary_for_goal),
            }
            for r in rows
        ],
    )


def get_negative_keywords(ctx, *, customer_id=None) -> dict:
    rows = ctx.search(
        "SELECT campaign_criterion.criterion_id, campaign_criterion.negative, "
        "campaign_criterion.keyword.text, campaign_criterion.keyword.match_type, "
        "campaign.id FROM campaign_criterion "
        "WHERE campaign_criterion.negative = TRUE "
        "AND campaign_criterion.type = 'KEYWORD'",
        customer_id=customer_id,
    )
    return base_payload(
        ctx,
        customer_id=customer_id,
        negative_keywords=[
            {
                "campaign_id": str(r.campaign.id),
                "criterion_id": str(r.campaign_criterion.criterion_id),
                "keyword": r.campaign_criterion.keyword.text,
                "match_type": r.campaign_criterion.keyword.match_type.name,
            }
            for r in rows
            if r.campaign_criterion.negative
        ],
    )


def list_extensions(ctx, *, customer_id=None) -> dict:
    rows = ctx.search(
        "SELECT campaign_asset.field_type, campaign_asset.status, asset.id, "
        "asset.sitelink_asset.link_text, asset.callout_asset.callout_text, "
        "asset.structured_snippet_asset.header, campaign.id "
        "FROM campaign_asset",
        customer_id=customer_id,
    )
    extensions = []
    for r in rows:
        text = (
            extract_field(r, "asset.sitelink_asset.link_text")
            or extract_field(r, "asset.callout_asset.callout_text")
            or extract_field(r, "asset.structured_snippet_asset.header")
        )
        extensions.append(
            {
                "asset_id": str(r.asset.id),
                "field_type": r.campaign_asset.field_type.name,
                "campaign_id": str(r.campaign.id),
                "status": r.campaign_asset.status.name,
                "text": text,
            }
        )
    return base_payload(ctx, customer_id=customer_id, extensions=extensions)


def search_geo_targets(ctx, *, query: str, customer_id=None) -> dict:
    if not query or not str(query).strip():
        raise ToolError("INVALID_QUERY", "query is empty")
    # Escape the escape character first — otherwise a trailing backslash
    # eats our quote-escape and terminates the string literal.
    safe = str(query).strip().replace("\\", "\\\\").replace("'", "\\'")
    rows = ctx.search(
        "SELECT geo_target_constant.id, geo_target_constant.name, "
        "geo_target_constant.canonical_name, geo_target_constant.country_code, "
        "geo_target_constant.target_type, geo_target_constant.status "
        "FROM geo_target_constant "
        f"WHERE geo_target_constant.name LIKE '%{safe}%'",
        customer_id=customer_id,
    )
    return base_payload(
        ctx,
        customer_id=customer_id,
        results=[
            {
                "id": str(r.geo_target_constant.id),
                "name": r.geo_target_constant.name,
                "canonical_name": r.geo_target_constant.canonical_name,
                "country_code": r.geo_target_constant.country_code,
                "target_type": r.geo_target_constant.target_type,
                "status": r.geo_target_constant.status.name,
            }
            for r in rows
        ],
    )
