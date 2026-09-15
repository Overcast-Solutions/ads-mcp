"""Policy, recommendations (read), change history, and shopping visibility.

Policy summaries and filtered detail include asset issues. Account-local
change history reports actors and changed fields; shopping views expose
feed-linked performance, listing groups and product status.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ads_mcp.errors import ToolError
from ads_mcp.gaql import extract_field
from ads_mcp.continuation import BOUND_GUIDANCE, bounded_rows, retained
import json
from ads_mcp.reporting import money, normalize_campaign_id, window_clause


CHANGE_HISTORY_MAX_DAYS = 30
CHANGE_EVENT_LIMIT = 1000


# ---------------------------------------------------------------------------
# Policy


def _topic_entries(policy_summary):
    for entry in policy_summary.policy_topic_entries:
        yield {"topic": entry.topic, "type": entry.type_.name}


def _collect_policy_issues(ctx, customer_id=None, campaign_id=None):
    campaign_filter = f" WHERE campaign.id = {campaign_id}" if campaign_id else ""

    ad_rows = ctx.search_iter(
        "SELECT ad_group_ad.status, ad_group_ad.ad.id, "
        "ad_group_ad.policy_summary.approval_status, "
        "ad_group_ad.policy_summary.review_status, "
        "ad_group_ad.policy_summary.policy_topic_entries, "
        "ad_group.id, campaign.id, campaign.name FROM ad_group_ad"
        + campaign_filter,
        customer_id=customer_id,
    )
    for row in ad_rows:
        summary = row.ad_group_ad.policy_summary
        for entry in _topic_entries(summary):
            yield (
                {
                    "entity_type": "AD",
                    "entity_status": row.ad_group_ad.status.name,
                    "ad_id": str(row.ad_group_ad.ad.id),
                    "ad_group_id": str(row.ad_group.id),
                    "campaign_id": str(row.campaign.id),
                    "campaign_name": row.campaign.name,
                    "approval_status": summary.approval_status.name,
                    "review_status": summary.review_status.name,
                    "topic": entry["topic"],
                    "topic_type": entry["type"],
                }
            )

    asset_rows = ctx.search_iter(
        "SELECT asset_group_asset.status, asset_group_asset.asset, "
        "asset_group_asset.asset_group, asset_group_asset.field_type, "
        "asset_group_asset.policy_summary.approval_status, "
        "asset_group_asset.policy_summary.review_status, "
        "asset_group_asset.policy_summary.policy_topic_entries, "
        "campaign.id, campaign.name FROM asset_group_asset" + campaign_filter,
        customer_id=customer_id,
    )
    for row in asset_rows:
        summary = row.asset_group_asset.policy_summary
        for entry in _topic_entries(summary):
            yield (
                {
                    "entity_type": "ASSET_GROUP_ASSET",
                    "entity_status": row.asset_group_asset.status.name,
                    "asset": row.asset_group_asset.asset,
                    "asset_group": row.asset_group_asset.asset_group,
                    "field_type": row.asset_group_asset.field_type.name,
                    "campaign_id": str(row.campaign.id),
                    "campaign_name": row.campaign.name,
                    "approval_status": summary.approval_status.name,
                    "review_status": summary.review_status.name,
                    "topic": entry["topic"],
                    "topic_type": entry["type"],
                }
            )
    sitelink_rows = ctx.search_iter(
        "SELECT campaign_asset.campaign, campaign_asset.asset, "
        "campaign_asset.status, campaign_asset.field_type, "
        "asset.policy_summary.approval_status, "
        "asset.policy_summary.review_status, "
        "asset.policy_summary.policy_topic_entries, campaign.id, campaign.name "
        "FROM campaign_asset WHERE campaign_asset.field_type = 'SITELINK'"
        + (f" AND campaign.id = {campaign_id}" if campaign_id else ""),
        customer_id=customer_id,
    )
    for row in sitelink_rows:
        link = row.campaign_asset
        if link.field_type.name != "SITELINK":
            continue
        summary = row.asset.policy_summary
        for entry in _topic_entries(summary):
            yield (
                {
                    "entity_type": "CAMPAIGN_ASSET",
                    "entity_status": link.status.name,
                    "asset": link.asset,
                    "campaign": link.campaign,
                    "field_type": link.field_type.name,
                    "campaign_id": _tail_id(link.campaign),
                    "campaign_name": row.campaign.name,
                    "approval_status": summary.approval_status.name,
                    "review_status": summary.review_status.name,
                    "topic": entry["topic"],
                    "topic_type": entry["type"],
                }
            )


@retained("issues", count_key="total_issues")
def get_policy_issues(ctx, *, mode="summary", enabled_only=False,
                      campaign_id=None, topic=None, page_token=None,
                      customer_id=None) -> dict:
    mode = (mode or "summary").strip().lower()
    if mode not in ("summary", "full"):
        raise ToolError("INVALID_MODE", f"mode must be summary or full, got {mode!r}")

    cid = ctx.resolve_customer(customer_id)
    campaign_id = normalize_campaign_id(campaign_id, optional=True)
    issues = _collect_policy_issues(ctx, customer_id=cid, campaign_id=campaign_id)
    if enabled_only:
        issues = (i for i in issues if i["entity_status"] == "ENABLED")
    if campaign_id:
        issues = (i for i in issues if i["campaign_id"] == str(campaign_id))
    if topic:
        issues = (i for i in issues if i["topic"] == topic)

    if mode == "summary":
        encoded, _, truncated = bounded_rows(issues)
        issues = [json.loads(row) for row in encoded]
        topics: dict[str, int] = {}
        breakdown: dict[str, int] = {}
        for issue in issues:
            topics[issue["topic"]] = topics.get(issue["topic"], 0) + 1
            breakdown[issue["entity_status"]] = breakdown.get(issue["entity_status"], 0) + 1
        sources = {
            "ad": sum(1 for i in issues if i["entity_type"] == "AD"),
            "asset": sum(
                1 for i in issues
                if i["entity_type"] in ("ASSET_GROUP_ASSET", "CAMPAIGN_ASSET")
            ),
        }
        return {
            **({"possibly_truncated": True, "guidance": BOUND_GUIDANCE} if truncated else {}),
            "customer_id": cid,
            "mode": "summary",
            "total_issues": len(issues),
            "topics": [
                {"topic": t, "count": c}
                for t, c in sorted(topics.items(), key=lambda kv: -kv[1])
            ],
            "entity_status_breakdown": breakdown,
            "sources": sources,
        }

    return {"customer_id": cid, "mode": "full", "issues": issues}


# ---------------------------------------------------------------------------
# Recommendations (read side)


def recommendation_resource_name(ctx, recommendation_id: str) -> str:
    """Bare id or full resource name -> exactly one canonical resource name.

    Recommendation IDs are opaque single segments; preserve their spelling.
    Validate the entire structure before checking customer ownership so malformed
    foreign resource names receive the same grammar error as local ones.
    """
    rid = str(recommendation_id or "").strip()
    match = re.fullmatch(
        r"(?:customers/([0-9]+)/recommendations/)?([^\s/?#\x00-\x1f\x7f-\x9f]+)",
        rid,
    )
    if match is None:
        raise ToolError(
            "INVALID_RECOMMENDATION_ID",
            "recommendation_id must be a nonempty single ID or "
            "customers/{customer_id}/recommendations/{recommendation_id}, "
            "without embedded whitespace, query/fragment delimiters or controls",
        )
    owner, identifier = match.groups()
    if owner is not None and owner != ctx.config.customer_id:
        raise ToolError(
            "PLAN_CUSTOMER_MISMATCH",
            f"recommendation {rid} belongs to customer {owner}, but this "
            f"server is configured for {ctx.config.customer_id}",
        )
    return f"customers/{ctx.config.customer_id}/recommendations/{identifier}"


def list_recommendations(ctx, *, customer_id=None) -> dict:
    cid = ctx.resolve_customer(customer_id)
    rows = ctx.search(
        "SELECT recommendation.resource_name, recommendation.type, "
        "recommendation.dismissed, recommendation.campaign, "
        "recommendation.impact, "
        "recommendation.campaign_budget_recommendation, "
        "customer.currency_code FROM recommendation",
        customer_id=cid,
    )
    recommendations = []
    for row in rows:
        rec = row.recommendation
        currency = extract_field(row, "customer.currency_code") or "USD"
        campaign_res = rec.campaign or ""
        campaign_id = campaign_res.rsplit("/", 1)[-1] if campaign_res else None

        def _impact(metrics):
            return {
                "impressions": int(metrics.impressions),
                "clicks": int(metrics.clicks),
                "cost": money(metrics.cost_micros, currency),
                "conversions": float(metrics.conversions),
            }

        item = {
            "id": rec.resource_name.rsplit("/", 1)[-1],
            "resource_name": rec.resource_name,
            "type": rec.type_.name,
            "campaign_id": campaign_id,
            "dismissed": bool(rec.dismissed),
            "impact": {
                "base": _impact(rec.impact.base_metrics),
                "potential": _impact(rec.impact.potential_metrics),
            },
        }
        current = extract_field(
            row, "recommendation.campaign_budget_recommendation.current_budget_amount_micros"
        )
        recommended = extract_field(
            row, "recommendation.campaign_budget_recommendation.recommended_budget_amount_micros"
        )
        if current or recommended:
            item["budget"] = {
                "current": money(current, currency) if current else None,
                "recommended": money(recommended, currency) if recommended else None,
            }
        recommendations.append(item)
    return {"customer_id": cid, "recommendations": recommendations}


# ---------------------------------------------------------------------------
# Change history


def _walk_changed_value(container, snake_path: str):
    obj = container
    for part in snake_path.split("."):
        if obj is None:
            return None
        obj = getattr(obj, part, None)
    if hasattr(obj, "name") and hasattr(type(obj), "__members__"):
        return obj.name
    if isinstance(obj, (str, int, float, bool)) or obj is None:
        return obj
    return str(obj)


def _history_dates(ctx, start_text, end_text):
    try:
        if any(
            not isinstance(value, str)
            or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value)
            for value in (start_text, end_text)
        ):
            raise ValueError
        start = date.fromisoformat(start_text)
        end = date.fromisoformat(end_text)
    except (TypeError, ValueError):
        raise ToolError(
            "INVALID_WINDOW",
            "date_range_start and date_range_end must be YYYY-MM-DD",
        ) from None
    if end < start:
        raise ToolError("INVALID_WINDOW", "date_range_end precedes date_range_start")
    now = datetime.fromtimestamp(ctx.clock(), tz=timezone.utc)
    # Every account's local date lies within one day of the UTC date.
    # Reject dates impossible in any zone before reading the account; leave
    # boundary decisions to its verified zone below.
    if (
        (end - start).days >= CHANGE_HISTORY_MAX_DAYS
        or start < now.date() - timedelta(days=CHANGE_HISTORY_MAX_DAYS)
        or end > now.date() + timedelta(days=1)
    ):
        raise ToolError(
            "CHANGE_HISTORY_RANGE_EXCEEDED",
            "Change history is limited to the past 30 account-local calendar "
            "dates. Choose an inclusive window from 29 days before today "
            "through today, with start on or before end.",
        )
    return start, end, now


def _validate_history_retention(ctx, cid, start, end, now):
    rows = ctx.search(
        "SELECT customer.time_zone FROM customer LIMIT 1", customer_id=cid
    )
    account = getattr(rows[0], "customer", None) if rows else None
    try:
        zone = ZoneInfo(getattr(account, "time_zone", ""))
    except (ZoneInfoNotFoundError, ValueError, TypeError):
        raise ToolError(
            "ACCOUNT_TIME_ZONE_UNAVAILABLE",
            f"Customer {cid} has a missing or invalid time zone. Verify its "
            "account time zone before requesting change history.",
        ) from None
    today = now.astimezone(zone).date()
    earliest = today - timedelta(days=CHANGE_HISTORY_MAX_DAYS - 1)
    if start < earliest or end > today:
        raise ToolError(
            "CHANGE_HISTORY_RANGE_EXCEEDED",
            "Change history is limited to the past 30 account-local calendar "
            f"dates. Choose start and end within {earliest} through {today}, "
            "inclusive, with start on or before end.",
        )


@retained("changes", history_cap=CHANGE_EVENT_LIMIT)
def get_change_history(ctx, *, date_range_start, date_range_end,
                       resource_type=None, page_token=None,
                       customer_id=None) -> dict:
    start, end, now = _history_dates(ctx, date_range_start, date_range_end)

    type_filter = ""
    if resource_type:
        # Interpolated into a quoted GAQL literal: accept only an enum-shaped
        # token, never caller text.
        token = str(resource_type).strip().upper()
        if not token.replace("_", "").isalnum():
            raise ToolError(
                "INVALID_RESOURCE_TYPE",
                f"resource_type must be an enum name (letters and underscores), "
                f"got {resource_type!r}",
            )
        type_filter = f" AND change_event.change_resource_type = '{token}'"
    # Calendar arithmetic preserves the complete account-local end date,
    # including fractional seconds and days spanning a DST transition.
    end_exclusive = end + timedelta(days=1)
    query = (
        "SELECT change_event.change_date_time, change_event.user_email, "
        "change_event.client_type, change_event.change_resource_type, "
        "change_event.resource_change_operation, change_event.changed_fields, "
        "change_event.old_resource, change_event.new_resource, "
        "change_event.campaign FROM change_event "
        f"WHERE change_event.change_date_time >= '{start} 00:00:00' "
        f"AND change_event.change_date_time < '{end_exclusive} 00:00:00'{type_filter} "
        f"ORDER BY change_event.change_date_time DESC LIMIT {CHANGE_EVENT_LIMIT}"
    )
    cid = ctx.resolve_customer(customer_id)
    _validate_history_retention(ctx, cid, start, end, now)
    rows = ctx.search_iter(query, customer_id=cid)
    def changes():
        for row in rows:
            event = row.change_event
            rtype = event.change_resource_type.name
            container_attr = rtype.lower()
            old_container = getattr(event.old_resource, container_attr, None)
            new_container = getattr(event.new_resource, container_attr, None)
            field_changes = {}
            for path in event.changed_fields.paths:
                key = f"{container_attr}.{path}"
                field_changes[key] = {
                    "old": _walk_changed_value(old_container, path),
                    "new": _walk_changed_value(new_container, path),
                }
            yield (
                {
                    "timestamp": event.change_date_time,
                    "actor": event.user_email,
                    "client_type": event.client_type.name,
                    "resource_type": rtype,
                    "operation": event.resource_change_operation.name,
                    "campaign_id": (event.campaign.rsplit("/", 1)[-1] if event.campaign else None),
                    "changes": field_changes,
                }
            )
    payload = {
        "customer_id": cid,
        "window": {"start": str(start), "end": str(end)},
        "changes": changes(),
    }
    return payload


# ---------------------------------------------------------------------------
# Shopping / Merchant Center visibility


@retained("products")
def get_shopping_performance(ctx, *, campaign_id, window, page_token=None,
                             customer_id=None) -> dict:
    cid = normalize_campaign_id(campaign_id)
    account = ctx.resolve_customer(customer_id)
    rows = ctx.search_iter(
        "SELECT segments.product_item_id, segments.product_title, campaign.id, "
        "customer.currency_code, metrics.impressions, metrics.clicks, "
        "metrics.cost_micros, metrics.conversions, metrics.conversions_value "
        "FROM shopping_performance_view "
        f"WHERE {window_clause(window)} AND campaign.id = {cid}",
        customer_id=account,
    )
    rows = (r for r in rows if str(r.campaign.id) in ("0", cid))
    def products():
        for row in rows:
            currency = extract_field(row, "customer.currency_code") or "USD"
            yield (
                {
                    "item_id": row.segments.product_item_id,
                    "title": row.segments.product_title,
                    "impressions": int(row.metrics.impressions),
                    "clicks": int(row.metrics.clicks),
                    "cost": money(row.metrics.cost_micros, currency),
                    "conversions": float(row.metrics.conversions),
                    "conversions_value": f"{float(row.metrics.conversions_value):.2f} {currency}",
                }
            )
    payload = {"customer_id": account, "campaign_id": cid,
               "window": window, "products": products()}
    return payload


def _tail_id(resource: str) -> str | None:
    if not resource:
        return None
    tail = resource.rsplit("/", 1)[-1]
    return tail.split("~")[-1] if "~" in tail else tail


# The v25 API exposes these selectable leaves, not the case_value message.
# Keep qualifiers alongside their values when serializing a dimension.
_LISTING_DIMENSION_FIELDS = {
    "product_brand": ("value",),
    "product_category": ("category_id", "level"),
    "product_channel": ("channel",),
    "product_condition": ("condition",),
    "product_custom_attribute": ("value", "index"),
    "product_item_id": ("value",),
    "product_type": ("value", "level"),
}


_SHOPPING_DIMENSION_FIELDS = {
    **_LISTING_DIMENSION_FIELDS,
    "product_channel_exclusivity": ("channel_exclusivity",),
}


def _listing_dimension(case_value, fields):
    which = case_value._pb.WhichOneof("dimension")
    if not which:
        return None
    details = {
        field: extract_field(case_value, f"{which}.{field}")
        for field in fields[which]
    }
    value = next(iter(details.values())) if len(details) == 1 else details
    return {which: value}


def get_listing_groups(ctx, *, campaign_id, customer_id=None) -> dict:
    cid = normalize_campaign_id(campaign_id)
    account = ctx.resolve_customer(customer_id)
    dimension_fields = ", ".join(
        f"asset_group_listing_group_filter.case_value.{kind}.{field}"
        for kind, fields in _LISTING_DIMENSION_FIELDS.items()
        for field in fields
    )
    rows = ctx.search(
        "SELECT asset_group_listing_group_filter.resource_name, "
        "asset_group_listing_group_filter.id, "
        "asset_group_listing_group_filter.type, "
        "asset_group_listing_group_filter.parent_listing_group_filter, "
        f"{dimension_fields}, "
        "asset_group_listing_group_filter.asset_group, "
        "asset_group.id, asset_group.name, campaign.id "
        "FROM asset_group_listing_group_filter "
        f"WHERE campaign.id = {cid}",
        customer_id=account,
    )
    rows = [r for r in rows if str(r.campaign.id) in ("0", cid)]
    groups: dict[str, dict] = {}
    for row in rows:
        f = row.asset_group_listing_group_filter
        group_id = str(row.asset_group.id)
        group = groups.setdefault(
            group_id,
            {
                "asset_group_id": group_id,
                "name": row.asset_group.name,
                "nodes": [],
            },
        )
        node = {
            "filter_id": str(f.id),
            "parent_filter_id": _tail_id(f.parent_listing_group_filter),
            "type": f.type_.name,
            "dimension": _listing_dimension(f.case_value, _LISTING_DIMENSION_FIELDS),
        }
        group["nodes"].append(node)

    dimension_fields = ", ".join(
        f"ad_group_criterion.listing_group.case_value.{kind}.{field}"
        for kind, fields in _SHOPPING_DIMENSION_FIELDS.items()
        for field in fields
    )
    shopping_rows = ctx.search(
        "SELECT ad_group.id, ad_group.name, ad_group.campaign, campaign.id, "
        "ad_group_criterion.resource_name, ad_group_criterion.criterion_id, "
        "ad_group_criterion.type, ad_group_criterion.status, "
        "ad_group_criterion.negative, ad_group_criterion.listing_group.type, "
        "ad_group_criterion.listing_group.parent_ad_group_criterion, "
        f"{dimension_fields} FROM ad_group_criterion "
        f"WHERE campaign.id = {cid} AND ad_group_criterion.type = 'LISTING_GROUP'",
        customer_id=account,
    )
    ad_groups: dict[str, dict] = {}
    for row in shopping_rows:
        criterion = row.ad_group_criterion
        if str(row.campaign.id) != cid or criterion.type_.name != "LISTING_GROUP":
            continue
        listing = criterion.listing_group
        group_id = str(row.ad_group.id)
        group = ad_groups.setdefault(
            group_id,
            {"ad_group_id": group_id, "name": row.ad_group.name, "nodes": []},
        )
        group["nodes"].append(
            {
                "criterion_id": str(criterion.criterion_id),
                "parent_criterion_id": _tail_id(listing.parent_ad_group_criterion),
                "type": listing.type_.name,
                "negative": bool(criterion.negative),
                "status": criterion.status.name,
                "dimension": _listing_dimension(
                    listing.case_value, _SHOPPING_DIMENSION_FIELDS
                ),
            }
        )
    payload = {"customer_id": account, "campaign_id": cid,
               "asset_groups": list(groups.values())}
    if ad_groups:
        payload["ad_groups"] = list(ad_groups.values())
    return payload


def get_product_status(ctx, *, campaign_id, customer_id=None) -> dict:
    cid = normalize_campaign_id(campaign_id)
    account = ctx.resolve_customer(customer_id)
    campaign_rows = ctx.search(
        "SELECT campaign.id, campaign.shopping_setting.merchant_id "
        f"FROM campaign WHERE campaign.id = {cid}",
        customer_id=account,
    )
    target = next((r for r in campaign_rows if str(r.campaign.id) == cid), None)
    if target is None:
        raise ToolError("NOT_FOUND", f"campaign {cid} was not found")
    merchant_id = extract_field(target, "campaign.shopping_setting.merchant_id")
    if not merchant_id:
        return {"customer_id": account,
                "status": "NOT_FEED_LINKED", "campaign_id": cid}

    product_rows = ctx.search(
        "SELECT shopping_product.item_id, shopping_product.title, "
        "shopping_product.status FROM shopping_product "
        f"WHERE shopping_product.campaign = 'customers/{account}/campaigns/{cid}' "
        f"AND shopping_product.merchant_center_id = {merchant_id}",
        customer_id=account,
    )
    counts: dict[str, int] = {}
    for row in product_rows:
        status = row.shopping_product.status.name
        counts[status] = counts.get(status, 0) + 1
    return {
        "customer_id": account,
        "campaign_id": cid,
        "merchant_id": str(merchant_id),
        "total_products": len(product_rows),
        "status_counts": counts,
    }
