"""Keyword Planner: idea discovery and forecasts.

Both tools are READS. They are implemented on KeywordPlanIdeaService
(`generate_keyword_ideas`, `generate_keyword_forecast_metrics`), which compute
and return — they persist nothing. The older KeywordPlanService route builds a
real KeywordPlan (with campaign and ad-group children) in the account, which
would put a live mutate path inside read-only mode; it is deliberately not
used here.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ads_mcp.errors import ToolError
from ads_mcp.continuation import retained
from ads_mcp.reporting import base_payload, money

DEFAULT_FORECAST_DAYS = 30

# The real API refuses a forecast campaign without a bidding strategy; a flat
# manual CPC keeps the request spend-neutral (nothing is created, ever).
FORECAST_MANUAL_CPC_MICROS = 1_000_000


def _planner_service(ctx):
    client = ctx.client()
    # Cross-account reads need the manager login on the client, same as GAQL.
    login = ctx.login_header_customer_id()
    if login and getattr(client, "login_customer_id", None) != login:
        client.login_customer_id = login
    return client, client.get_service("KeywordPlanIdeaService")


def _forecast_account(ctx, customer_id):
    rows = ctx.search(
        "SELECT customer.currency_code, customer.time_zone FROM customer LIMIT 1",
        customer_id=customer_id,
    )
    account = getattr(rows[0], "customer", None) if rows else None
    zone_name = getattr(account, "time_zone", "")
    try:
        zone = ZoneInfo(zone_name)
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        raise ToolError(
            "ACCOUNT_TIME_ZONE_UNAVAILABLE",
            f"Customer {customer_id} has a missing or invalid time zone; "
            "a valid account time zone is required for forecasts",
        ) from None
    currency = getattr(account, "currency_code", "")
    if not isinstance(currency, str) or not currency.strip():
        raise ToolError(
            "ACCOUNT_CURRENCY_UNAVAILABLE",
            "Account currency is missing; verify the requested account's "
            "currency metadata before requesting forecasts",
        )
    return currency, zone


def _enum_name(value):
    """Enum members render as their name; fixture strings pass through."""
    return getattr(value, "name", value)


@retained("ideas")
def discover_keywords(ctx, *, seed_keywords=None, page_url=None,
                      customer_id=None, page_token=None) -> dict:
    seeds = [str(s).strip() for s in (seed_keywords or []) if str(s).strip()]
    url = str(page_url).strip() if page_url else ""
    if not seeds and not url:
        raise ToolError(
            "MISSING_ARGUMENT",
            "provide seed_keywords (non-empty) and/or page_url to discover "
            "keyword ideas",
        )
    cid = ctx.resolve_customer(customer_id)
    ctx.current_customer = cid
    client, service = _planner_service(ctx)
    request = client.get_type("GenerateKeywordIdeasRequest")
    request.customer_id = cid
    if seeds and url:
        request.keyword_and_url_seed.url = url
        request.keyword_and_url_seed.keywords.extend(seeds)
    elif seeds:
        request.keyword_seed.keywords.extend(seeds)
    else:
        request.url_seed.url = url

    def _read_ideas():
        response = service.generate_keyword_ideas(request=request)
        # The SDK pager's ``results`` proxies only its current response.
        # Iteration follows every provider token, including empty pages.
        if hasattr(response, "pages"):
            return response
        return getattr(response, "results", []) or []

    results = _read_ideas()
    def ideas():
        for result in results:
            metrics = getattr(result, "keyword_idea_metrics", None)
            yield (
                {
                    "keyword": result.text,
                    "avg_monthly_searches": int(
                        getattr(metrics, "avg_monthly_searches", 0) or 0
                    ),
                    "competition": _enum_name(
                        getattr(metrics, "competition", "UNSPECIFIED")
                    ),
                }
            )
    return base_payload(ctx, customer_id=customer_id, ideas=ideas())


def _anniversary(today: date) -> date:
    try:
        return today.replace(year=today.year + 1)
    except ValueError:
        # February 29 has no exact anniversary in the following year.
        return today.replace(year=today.year + 1, day=28)


def _valid_forecast_dates(start: date, end: date, today: date) -> bool:
    return today < start <= end <= _anniversary(today)


def _parse_forecast_dates(now, date_range_start, date_range_end):
    """Reject malformed and globally impossible windows before account reads."""
    if date_range_start is not None or date_range_end is not None:
        if date_range_start is None or date_range_end is None:
            raise ToolError(
                "INVALID_WINDOW",
                "provide both date_range_start and date_range_end "
                "(YYYY-MM-DD), or neither for the default forecast window",
            )
        parsed = []
        for label, value in (("date_range_start", date_range_start),
                             ("date_range_end", date_range_end)):
            try:
                if not isinstance(value, str) or not re.fullmatch(
                    r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value
                ):
                    raise ValueError
                parsed.append(date.fromisoformat(value))
            except ValueError:
                raise ToolError(
                    "INVALID_WINDOW", f"{label} {value!r} is not YYYY-MM-DD"
                ) from None
        start, end = parsed
        if end < start:
            raise ToolError(
                "INVALID_WINDOW", "date_range_end precedes date_range_start"
            )
        # Modern account timezones span UTC-12 through UTC+14. A date can
        # still be valid near midnight even when it fails the UTC window.
        earliest = (now - timedelta(hours=12)).date()
        latest = (now + timedelta(hours=14)).date()
        possible_days = (
            earliest + timedelta(days=offset)
            for offset in range((latest - earliest).days + 1)
        )
        if not any(_valid_forecast_dates(start, end, day) for day in possible_days):
            raise ToolError(
                "INVALID_WINDOW",
                "Forecast start must be in the future and end no later than "
                "one calendar year from today in the account time zone",
            )
        return start, end
    return None


def _forecast_window(now, zone, dates) -> dict:
    today = now.astimezone(zone).date()
    if dates is not None:
        start, end = dates
        if not _valid_forecast_dates(start, end, today):
            raise ToolError(
                "INVALID_WINDOW",
                f"Forecast start must be after {today.isoformat()} and end "
                f"no later than {_anniversary(today).isoformat()} in the "
                "account time zone",
            )
        return {"start": start.isoformat(), "end": end.isoformat()}
    # Default: the next N complete days starting tomorrow (forecasts look
    # forward, unlike the reporting reads).
    start = today + timedelta(days=1)
    end = start + timedelta(days=DEFAULT_FORECAST_DAYS - 1)
    return {"start": start.isoformat(), "end": end.isoformat()}


def get_keyword_forecasts(ctx, *, keywords=None, date_range_start=None,
                          date_range_end=None, customer_id=None, keyword_texts=None) -> dict:
    if keywords is not None and keyword_texts is not None and keywords != keyword_texts:
        raise ToolError("CONTRADICTORY_ARGUMENTS", "keywords and keyword_texts must agree when both are supplied")
    if keywords is None:
        keywords = keyword_texts
    terms = [str(k).strip() for k in (keywords or []) if str(k).strip()]
    if not terms:
        raise ToolError(
            "MISSING_ARGUMENT", "keywords is empty — supply at least one "
            "keyword to forecast"
        )
    now = datetime.fromtimestamp(ctx.clock(), tz=timezone.utc)
    dates = _parse_forecast_dates(now, date_range_start, date_range_end)
    cid = ctx.resolve_customer(customer_id)
    ctx.current_customer = cid
    currency, zone = _forecast_account(ctx, cid)
    window = _forecast_window(now, zone, dates)
    client, service = _planner_service(ctx)
    request = client.get_type("GenerateKeywordForecastMetricsRequest")
    request.customer_id = cid
    request.currency_code = currency
    request.forecast_period.start_date = window["start"]
    request.forecast_period.end_date = window["end"]
    campaign = request.campaign
    campaign.bidding_strategy.manual_cpc_bidding_strategy.max_cpc_bid_micros = (
        FORECAST_MANUAL_CPC_MICROS
    )
    ad_group = client.get_type("ForecastAdGroup")
    broad = client.enums.KeywordMatchTypeEnum.BROAD
    for text in terms:
        info = client.get_type("KeywordInfo")
        info.text = text
        info.match_type = broad
        ad_group.keywords.append(info)
    campaign.ad_groups.append(ad_group)
    response = ctx.retry_account_read(
        lambda: service.generate_keyword_forecast_metrics(request=request), cid
    )
    metrics = getattr(response, "campaign_forecast_metrics", None)

    def _metric(name):
        if metrics is None:
            return None
        # Protobuf getters return zero for absent optional scalars. Presence
        # distinguishes that default from an explicitly supplied zero estimate.
        pb = getattr(metrics, "_pb", None)
        if pb is not None and not pb.HasField(name):
            return None
        return getattr(metrics, name, None)

    cost = _metric("cost_micros")
    average_cpc = _metric("average_cpc_micros")

    forecast = {
        # The v25 nonpersisting forecast response has no impressions field.
        "impressions": None,
        "clicks": _metric("clicks"),
        "cost": money(int(cost), currency) if cost is not None else None,
        "average_cpc": (
            money(int(average_cpc), currency) if average_cpc is not None else None
        ),
    }
    return base_payload(
        ctx, customer_id=customer_id, window=window, forecast=forecast
    )
