"""Read-surface tools. Registered in every mode, including read-only."""

from __future__ import annotations

import json

from google.protobuf import json_format
from pydantic import StrictInt

from ads_mcp.audit import audit_writable
from ads_mcp.errors import ToolError, classify_exception
from ads_mcp.tools.registry import KIND_READ, ToolSpec, guarded
from ads_mcp.transport import TransportError

JS_SAFE_MAX = 2**53 - 1

SPECS: list[ToolSpec] = []


def _spec(name: str, description: str):
    if not any(s.name == name for s in SPECS):
        SPECS.append(ToolSpec(name, description, KIND_READ))
    return description


def row_to_json(row) -> dict:
    """A GoogleAdsRow as JSON keeping native proto JSON types, with int64
    values surfaced as ints while they are JS-safe."""
    raw = json_format.MessageToDict(
        row._pb, preserving_proto_field_name=True
    )
    return _revive_ints(raw)


def _revive_ints(node):
    if isinstance(node, dict):
        return {k: _revive_ints(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_revive_ints(v) for v in node]
    if isinstance(node, str) and node.lstrip("-").isdigit():
        try:
            value = int(node)
        except ValueError:
            return node
        if -JS_SAFE_MAX <= value <= JS_SAFE_MAX:
            return value
    return node


def register(server, ctx):
    cfg = ctx.config

    @server.tool(
        name="run_gaql",
        description=_spec(
            "run_gaql",
            "Execute a raw GAQL query and return rows in json, table, or csv "
            "format. The workhorse read: any resource, any field the API "
            "exposes, passed through faithfully.",
        ),
    )
    def run_gaql(
        query: str,
        customer_id: str | None = None,
        format: str = "json",
        page_size: StrictInt | None = None,
        page_token: str | None = None,
    ) -> dict:
        from ads_mcp import gaql

        return guarded(ctx, gaql.run, name="run_gaql")(
            ctx=ctx,
            query=query,
            customer_id=customer_id,
            format=format,
            page_size=page_size,
            page_token=page_token,
        )

    @server.tool(
        name="health_check",
        description=_spec(
            "health_check",
            "Truthful liveness: performs a real authenticated Google Ads read "
            "and reports config, credentials, and guardrail state. OK means "
            "the API answered — never that config files merely exist.",
        ),
    )
    def health_check() -> dict:
        return guarded(ctx, _health_impl, name="health_check")(ctx=ctx)

    from ads_mcp import reporting

    @server.tool(
        name="get_asset_groups",
        description=_spec(
            "get_asset_groups",
            "List existing asset groups for a verified Performance Max campaign, "
            "including status, primary status and final URLs. campaign_id is a "
            "positive numeric ID. Results use retained, account- and "
            "campaign-bound pagination with explicit truncation guidance. "
            "customer_id may explicitly select another accessible account.",
        ),
    )
    def get_asset_groups(
        campaign_id: str,
        customer_id: str | None = None,
        page_token: str | None = None,
    ) -> dict:
        from ads_mcp import pmax

        return guarded(ctx, pmax.get_asset_groups, name="get_asset_groups")(
            ctx=ctx, campaign_id=campaign_id, customer_id=customer_id,
            page_token=page_token,
        )

    @server.tool(
        name="get_asset_group_signals",
        description=_spec(
            "get_asset_group_signals",
            "Inspect a verified Performance Max asset group's optimization signals. "
            "Returns composite signal IDs, kinds, theme text or Audience resources, "
            "and available approval diagnostics. Other kinds remain visible as "
            "unsupported. asset_group_id is a positive numeric ID. Results use "
            "retained account- and group-bound pagination with truncation guidance.",
        ),
    )
    def get_asset_group_signals(
        asset_group_id: str,
        customer_id: str | None = None,
        page_token: str | None = None,
    ) -> dict:
        from ads_mcp import pmax

        return guarded(ctx, pmax.get_asset_group_signals, name="get_asset_group_signals")(
            ctx=ctx, asset_group_id=asset_group_id, customer_id=customer_id,
            page_token=page_token,
        )

    @server.tool(
        name="list_audiences",
        description=_spec(
            "list_audiences",
            "List existing Audience resources with identity, name, status, scope "
            "and asset-group binding. Uses retained account-bound pagination. "
            "customer_id may select another accessible account. Does not create "
            "audiences or change their composition.",
        ),
    )
    def list_audiences(
        customer_id: str | None = None,
        page_token: str | None = None,
    ) -> dict:
        from ads_mcp import pmax

        return guarded(ctx, pmax.list_audiences, name="list_audiences")(
            ctx=ctx, customer_id=customer_id, page_token=page_token,
        )

    @server.tool(
        name="get_pmax_url_settings",
        description=_spec(
            "get_pmax_url_settings",
            "Inspect verified Performance Max automation settings and negative WEBPAGE "
            "URL exclusions with complete condition structure. Distinguishes explicit "
            "opt-in/out or UNSPECIFIED from absent provider-default settings. Google "
            "documents expansion as enabled by default for PMax; absence is not opt-out. "
            "Uses retained account- and campaign-bound pagination with truncation guidance. "
            "Exclusions are not universal destination blocks: explicitly supplied final "
            "URLs and applicable Merchant Center inventory can still serve.",
        ),
    )
    def get_pmax_url_settings(
        campaign_id: str,
        customer_id: str | None = None,
        page_token: str | None = None,
    ) -> dict:
        from ads_mcp import pmax

        return guarded(ctx, pmax.get_pmax_url_settings, name="get_pmax_url_settings")(
            ctx=ctx, campaign_id=campaign_id, customer_id=customer_id, page_token=page_token,
        )

    def _windowed_call(impl, name, date_range_start, date_range_end, last_n_days,
                       **kwargs):
        def call():
            window = reporting.resolve_window(
                date_range_start=date_range_start,
                date_range_end=date_range_end,
                last_n_days=last_n_days,
                clock=ctx.clock,
            )
            return impl(ctx=ctx, window=window, **kwargs)

        return guarded(ctx, call, name=name)()

    @server.tool(
        name="get_account_info",
        description=_spec(
            "get_account_info",
            "Account identity: id, name, currency, time zone, auto-tagging, "
            "manager/test flags — from a live authenticated read.",
        ),
    )
    def get_account_info(customer_id: str | None = None) -> dict:
        return guarded(ctx, reporting.get_account_info)(
            ctx=ctx, customer_id=customer_id
        )

    @server.tool(
        name="get_campaign_performance",
        description=_spec(
            "get_campaign_performance",
            "Campaign metrics for a date window (explicit range or "
            "last_n_days), with budget, bidding strategy incl. targets, and "
            "the serving/primary status trio. Money is decimal with currency. "
            "enabled_only=true filters server-side.",
        ),
    )
    def get_campaign_performance(
        date_range_start: str | None = None,
        date_range_end: str | None = None,
        last_n_days: int | None = None,
        enabled_only: bool = False,
        page_token: str | None = None,
        customer_id: str | None = None,
        campaign_id: str | None = None,
    ) -> dict:
        return _windowed_call(
            reporting.get_campaign_performance, "get_campaign_performance",
            date_range_start, date_range_end, last_n_days,
            enabled_only=enabled_only,
            page_token=page_token,
            customer_id=customer_id,
            campaign_id=campaign_id,
        )

    def _windowed_tool(name, description, impl):
        @server.tool(name=name, description=_spec(name, description))
        def _tool(
            date_range_start: str | None = None,
            date_range_end: str | None = None,
            last_n_days: int | None = None,
            page_token: str | None = None,
            customer_id: str | None = None,
            campaign_id: str | None = None,
        ) -> dict:
            return _windowed_call(
                impl, name, date_range_start, date_range_end, last_n_days,
                page_token=page_token,
                customer_id=customer_id,
                campaign_id=campaign_id,
            )

        return _tool

    _windowed_tool(
        "get_ad_performance",
        "Ad-level metrics for a date window; bounded with pagination tokens.",
        reporting.get_ad_performance,
    )
    _windowed_tool(
        "get_keyword_performance",
        "Keyword metrics (text, match type, bid, quality signals) for a date "
        "window; bounded with pagination tokens.",
        reporting.get_keyword_performance,
    )
    _windowed_tool(
        "get_search_terms",
        "Search-term report for a date window; bounded with pagination "
        "tokens — no unbounded dumps.",
        reporting.get_search_terms,
    )
    _windowed_tool(
        "get_geo_performance",
        "Geographic performance for a date window; bounded with pagination "
        "tokens.",
        reporting.get_geo_performance,
    )

    @server.tool(
        name="list_accounts",
        description=_spec(
            "list_accounts",
            "Accessible accounts under the configured login customer.",
        ),
    )
    def list_accounts() -> dict:
        return guarded(ctx, reporting.list_accounts)(ctx=ctx)

    @server.tool(
        name="get_conversion_actions",
        description=_spec(
            "get_conversion_actions",
            "Conversion actions with category, type, status, counting type, "
            "and primary-for-goal flags.",
        ),
    )
    def get_conversion_actions(customer_id: str | None = None) -> dict:
        return guarded(ctx, reporting.get_conversion_actions)(
            ctx=ctx, customer_id=customer_id
        )

    @server.tool(
        name="get_negative_keywords",
        description=_spec(
            "get_negative_keywords",
            "Campaign-level negative keywords.",
        ),
    )
    def get_negative_keywords(customer_id: str | None = None) -> dict:
        return guarded(ctx, reporting.get_negative_keywords)(
            ctx=ctx, customer_id=customer_id
        )

    @server.tool(
        name="list_extensions",
        description=_spec(
            "list_extensions",
            "Campaign-level extensions/assets (sitelinks, callouts, "
            "structured snippets) with status.",
        ),
    )
    def list_extensions(customer_id: str | None = None) -> dict:
        return guarded(ctx, reporting.list_extensions)(
            ctx=ctx, customer_id=customer_id
        )

    @server.tool(
        name="search_geo_targets",
        description=_spec(
            "search_geo_targets",
            "Search geo target constants by name (e.g. 'United States').",
        ),
    )
    def search_geo_targets(query: str, customer_id: str | None = None) -> dict:
        return guarded(ctx, reporting.search_geo_targets)(
            ctx=ctx, query=query, customer_id=customer_id
        )

    from ads_mcp import keyword_planner

    @server.tool(
        name="discover_keywords",
        description=_spec(
            "discover_keywords",
            "Keyword ideas for seed terms and/or a page URL, with average "
            "monthly searches and competition. Computed by "
            "KeywordPlanIdeaService — nothing is created in the account. "
            "Bounded by the row limit with pagination tokens.",
        ),
    )
    def discover_keywords(
        seed_keywords: list[str] | None = None,
        page_url: str | None = None,
        page_token: str | None = None,
        customer_id: str | None = None,
    ) -> dict:
        return guarded(ctx, keyword_planner.discover_keywords)(
            ctx=ctx,
            seed_keywords=seed_keywords,
            page_url=page_url,
            page_token=page_token,
            customer_id=customer_id,
        )

    @server.tool(
        name="get_keyword_forecasts",
        description=_spec(
            "get_keyword_forecasts",
            "Forecast clicks, cost, and average CPC for supplied keywords. "
            "Impressions are unavailable from the v25 nonpersisting API and "
            "returned as null. Other absent estimates are null; explicit zero "
            "and fractional clicks are preserved. Uses a strict YYYY-MM-DD "
            "date range. Start must be in "
            "the future and end within one calendar year of today in the "
            "requested account's time zone (February 29 clamps to February "
            "28). Defaults to the next 30 complete account-local days; "
            "missing or invalid account time zones are refused. Money "
            "is decimal with the account currency. Computed by "
            "KeywordPlanIdeaService — no KeywordPlan is ever created. "
            "keyword_texts is an alias for keywords; both must agree if supplied.",
        ),
    )
    def get_keyword_forecasts(
        keywords: list[str] | None = None,
        date_range_start: str | None = None,
        date_range_end: str | None = None,
        customer_id: str | None = None,
        keyword_texts: list[str] | None = None,
    ) -> dict:
        return guarded(ctx, keyword_planner.get_keyword_forecasts)(
            ctx=ctx,
            keywords=keywords,
            keyword_texts=keyword_texts,
            date_range_start=date_range_start,
            date_range_end=date_range_end,
            customer_id=customer_id,
        )

    from ads_mcp import insights

    @server.tool(
        name="get_policy_issues",
        description=_spec(
            "get_policy_issues",
            "Policy issues across ads, PMax asset-group assets, and "
            "campaign-linked sitelink assets. "
            "mode=summary returns a bounded topic histogram + entity-status "
            "breakdown; mode=full paginates with filters (enabled_only, "
            "campaign_id, topic) — never an unbounded dump.",
        ),
    )
    def get_policy_issues(
        mode: str = "summary",
        enabled_only: bool = False,
        campaign_id: str | None = None,
        topic: str | None = None,
        page_token: str | None = None,
        customer_id: str | None = None,
    ) -> dict:
        return guarded(ctx, insights.get_policy_issues)(
            ctx=ctx, mode=mode, enabled_only=enabled_only,
            campaign_id=campaign_id, topic=topic, page_token=page_token,
            customer_id=customer_id,
        )

    @server.tool(
        name="list_recommendations",
        description=_spec(
            "list_recommendations",
            "Google's active recommendations with typed impact projections "
            "(base vs potential cost and conversions). Read-only; applying "
            "or dismissing routes through the guardrail plan flow.",
        ),
    )
    def list_recommendations(customer_id: str | None = None) -> dict:
        return guarded(ctx, insights.list_recommendations)(
            ctx=ctx, customer_id=customer_id
        )

    @server.tool(
        name="get_change_history",
        description=_spec(
            "get_change_history",
            "Who changed what, when, with which client: change events with "
            "actor email, client type, and old/new values per changed field. "
            "Strict YYYY-MM-DD dates, inclusive from 29 days before today "
            "through today in the verified account time zone. Includes the "
            "complete end date, including fractional seconds, up to but "
            "excluding the next calendar day's midnight. This local "
            "30-calendar-date policy is distinct from provider sub-day "
            "retention. LIMIT 1000; a full result signals possibly_truncated "
            "on every local page with narrowing guidance. Local tokens "
            "cannot recover beyond that cap. More than 1000 events at one "
            "timestamp may need a different run_gaql query; upstream maximum "
            "LIMIT 10000.",
        ),
    )
    def get_change_history(
        date_range_start: str,
        date_range_end: str,
        resource_type: str | None = None,
        page_token: str | None = None,
        customer_id: str | None = None,
    ) -> dict:
        return guarded(ctx, insights.get_change_history)(
            ctx=ctx, date_range_start=date_range_start,
            date_range_end=date_range_end, resource_type=resource_type,
            page_token=page_token, customer_id=customer_id,
        )

    @server.tool(
        name="get_shopping_performance",
        description=_spec(
            "get_shopping_performance",
            "Product-level shopping metrics for a campaign and date window.",
        ),
    )
    def get_shopping_performance(
        campaign_id: str,
        date_range_start: str | None = None,
        date_range_end: str | None = None,
        last_n_days: int | None = None,
        page_token: str | None = None,
        customer_id: str | None = None,
    ) -> dict:
        return _windowed_call(
            insights.get_shopping_performance, "get_shopping_performance",
            date_range_start, date_range_end, last_n_days,
            campaign_id=campaign_id,
            page_token=page_token, customer_id=customer_id,
        )

    @server.tool(
        name="get_listing_groups",
        description=_spec(
            "get_listing_groups",
            "Listing-group trees per PMax asset group and standard Shopping "
            "ad group for the requested campaign, preserving parent links, "
            "dimensions, and exclusions. Empty trees are valid results.",
        ),
    )
    def get_listing_groups(campaign_id: str, customer_id: str | None = None) -> dict:
        return guarded(ctx, insights.get_listing_groups)(
            ctx=ctx, campaign_id=campaign_id, customer_id=customer_id
        )

    @server.tool(
        name="get_product_status",
        description=_spec(
            "get_product_status",
            "Merchant Center feed health as visible from the Ads API: linked "
            "merchant id, product counts by eligibility status scoped to the "
            "requested campaign and linked merchant. A campaign "
            "without a feed link returns NOT_FEED_LINKED, not an error.",
        ),
    )
    def get_product_status(campaign_id: str, customer_id: str | None = None) -> dict:
        return guarded(ctx, insights.get_product_status)(
            ctx=ctx, campaign_id=campaign_id, customer_id=customer_id
        )


def _run_gaql_impl(*, query: str, customer_id=None, format="json", ctx) -> dict:
    rows = ctx.search(query, customer_id=customer_id)
    json_rows = [row_to_json(r) for r in rows]
    return {"rows": json_rows, "row_count": len(json_rows)}


def _num(value):
    """Floats that are whole numbers render as ints (900.0 -> 900)."""
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def _audit_writable(cfg) -> bool | None:
    """Assess the audit destination under the secure writer's file rules."""
    if not cfg.audit_log:
        return None
    return audit_writable(cfg.audit_log)


def _health_impl(*, ctx) -> dict:
    cfg = ctx.config
    payload = {
        "status": "OK",
        "config": {
            "customer_id": cfg.customer_id,
            "login_customer_id": cfg.login_customer_id,
            # Compatibility field reports legacy configuration presence only;
            # access is determined by the OAuth Cloud project.
            "developer_token": "present" if cfg.developer_token else "absent",
            "credentials_path": cfg.credentials_path,
            "token_path": cfg.token_path,
        },
        "credentials": {},
        "guardrails": {
            "read_only": cfg.read_only,
            "require_dry_run": cfg.require_dry_run,
            "max_daily_budget": _num(cfg.max_daily_budget),
            "max_bid_increase_pct": _num(cfg.max_bid_increase_pct),
            "max_first_bid": _num(cfg.max_first_bid),
            "plan_ttl_seconds": _num(cfg.plan_ttl_seconds),
            "row_limit": cfg.row_limit,
            "audit_log": cfg.audit_log,
        },
    }
    # A write-enabled server whose audit path cannot be appended to will
    # refuse every apply; health must not report that as OK. (The boolean
    # itself is not added to the guardrails block: the F016 golden contract
    # pins that payload exactly, so surfacing it needs a spec amendment.)
    if _audit_writable(cfg) is False:
        payload["status"] = "AUDIT_UNWRITABLE"
    try:
        ctx.search("SELECT customer.id FROM customer LIMIT 1")
        payload["credentials"] = {"state": "OK"}
    except BaseException as exc:  # noqa: BLE001 — health reports, never raises
        err = classify_exception(exc, scrub=ctx.scrub)
        ctx.audit_auth_failure(err)
        payload["credentials"] = {
            "state": "FAILED",
            "code": err.code,
            "message": ctx.scrub(err.message),
        }
        if err.code in ("AUTH_TOKEN_REVOKED", "AUTH_FAILED"):
            payload["status"] = "AUTH_DEAD"
        elif err.code == "TRANSPORT_FAILED":
            payload["status"] = "TRANSPORT_FAILED"
        elif err.code.startswith("AUTH_CONFIG_") or err.code == "CONFIG_INVALID":
            payload["status"] = "CONFIG_INVALID"
        else:
            payload["status"] = "DEGRADED"
    return payload
