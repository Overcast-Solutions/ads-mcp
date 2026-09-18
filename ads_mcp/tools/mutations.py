"""The mutation surface: every write is a plan; confirm_and_apply is the only
execution path.

Registered only when ADS_MCP_READ_ONLY=false. Each tool validates client-side
(text limits, ids, caps), builds a human-readable plan with structured
operations, and stores an execute closure. Nothing here touches a live mutate
call — that happens exclusively in confirm_and_apply, after the dry-run
sequence and a fresh cap re-check.
"""

from __future__ import annotations

import base64
import binascii
import re
import unicodedata
from decimal import Decimal, InvalidOperation
from typing import Annotated
from urllib.parse import urlsplit

from pydantic import Field, StrictBool, StrictStr

from ads_mcp import executors, guardrails
from ads_mcp.errors import ToolError, classify_exception
from ads_mcp.gaql import extract_field
from ads_mcp.insights import recommendation_resource_name
from ads_mcp.reporting import money
from ads_mcp.tools.registry import KIND_APPLY, KIND_MUTATION, ToolSpec, guarded

SPECS: list[ToolSpec] = []

MICROS = 1_000_000

# Google's editorial text limits, enforced client-side before any API call.
HEADLINE_MAX = 30
DESCRIPTION_MAX = 90
RSA_MIN_HEADLINES = 3
RSA_MIN_DESCRIPTIONS = 2
SITELINK_TEXT_MAX = 25
CALLOUT_MAX = 25


def _spec(name: str, description: str, *, kind: str = KIND_MUTATION):
    if not any(s.name == name for s in SPECS):
        SPECS.append(ToolSpec(name, description, kind))
    return description


def _require(value, name: str):
    if value is None or (isinstance(value, str) and not value.strip()):
        raise ToolError("MISSING_ARGUMENT", f"{name} is required")
    return value


def _text_limit(value: str, limit: int, what: str, *, nonblank=False):
    if not isinstance(value, str):
        raise ToolError("INVALID_CREATIVE", f"{what} must be text")
    if nonblank and not value.strip():
        raise ToolError("INVALID_CREATIVE", f"{what} must be nonblank text")
    width = _creative_width(value)
    if width > limit:
        raise ToolError(
            "TEXT_LIMIT_EXCEEDED",
            f"{what} is {width} counted characters; the limit is {limit} "
            "(Unicode W/F count twice)",
        )


def _political_declaration(value):
    if type(value) is not bool:
        raise ToolError("MISSING_ARGUMENT", "contains_eu_political_advertising requires an explicit boolean")
    return value


def _creative_width(text):
    return sum(2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1
               for char in text)


def _destination_url(url, name="final_url"):
    valid = False
    if isinstance(url, str):
        try:
            parsed = urlsplit(url)
            valid = (parsed.scheme in {"http", "https"} and parsed.hostname
                     and not parsed.username and not parsed.password
                     and not any(char.isspace() or ord(char) < 32 for char in url))
            parsed.port  # Reject malformed ports before any account lookup.
        except ValueError:
            valid = False
    if not valid:
        raise ToolError(
            "INVALID_URL", f"{name} must be an HTTP or HTTPS URL string with a host, "
            "without credentials or whitespace"
        )


def _pmax_creative(headlines, long_headlines, descriptions, business_name, final_urls):
    for values, minimum, maximum, limit, label in (
        (headlines, 3, 15, 30, "headlines"),
        (long_headlines, 1, 5, 90, "long_headlines"),
        (descriptions, 2, 5, 90, "descriptions"),
        ([business_name], 1, 1, 25, "business_name"),
    ):
        if not minimum <= len(values) <= maximum:
            raise ToolError("INVALID_CREATIVE", f"{label} requires {minimum}-{maximum} entries")
        for value in values:
            if not value.strip() or _creative_width(value) > limit:
                raise ToolError("INVALID_CREATIVE", f"{label} must be nonblank and at most {limit} counted characters (Unicode W/F count twice)")
    if not any(_creative_width(text) <= 15 for text in headlines):
        raise ToolError("INVALID_CREATIVE", "at least one headline must fit 15 counted characters")
    if not any(_creative_width(text) <= 60 for text in descriptions):
        raise ToolError("INVALID_CREATIVE", "at least one description must fit 60 counted characters")
    if not final_urls:
        raise ToolError("INVALID_URL", "final_urls requires at least one HTTP or HTTPS URL")
    for url in final_urls:
        _destination_url(url, "each final_urls entry")


PMAX_IMAGE_ROLES = {
    "landscape_image_asset_ids": (20, "MARKETING_IMAGE"),
    "square_image_asset_ids": (20, "SQUARE_MARKETING_IMAGE"),
    "logo_asset_ids": (5, "LOGO"),
}


def _pmax_image_ids(**roles):
    result = {}
    for name, (maximum, _field) in PMAX_IMAGE_ROLES.items():
        values = roles[name]
        if not values or not 1 <= len(values) <= maximum:
            raise ToolError("INVALID_IMAGE_ASSETS", f"{name} requires 1-{maximum} explicit existing asset IDs")
        ids = []
        for value in values:
            value = value.strip()
            if not re.fullmatch(r"[0-9]{1,19}", value) or not 0 < int(value) <= 2**63 - 1:
                raise ToolError("INVALID_ID", f"{name} requires positive numeric asset IDs")
            ids.append(str(int(value)))
        if len(ids) != len(set(ids)):
            raise ToolError("INVALID_IMAGE_ASSETS", f"{name} must not contain duplicate IDs")
        result[name] = ids
    return result


def _preserve_auth_cause(ctx, exc):
    """Keep known auth recovery before replacing unavailable safety evidence.

    The outer tool guard owns auth auditing, once per failed invocation.
    """
    error = classify_exception(exc, scrub=ctx.scrub)
    if error.code.startswith("AUTH_"):
        raise error from None


def _verify_pmax_images(ctx, roles):
    ids = {identity for values in roles.values() for identity in values}
    try:
        rows = ctx.search(
            "SELECT asset.id, asset.resource_name, asset.type, "
            "asset.image_asset.full_size.width_pixels, "
            "asset.image_asset.full_size.height_pixels, asset.image_asset.file_size "
            f"FROM asset WHERE asset.id IN ({', '.join(sorted(ids))})"
        )
    except Exception as exc:
        _preserve_auth_cause(ctx, exc)
        raise ToolError("IMAGE_ASSET_UNVERIFIED", "could not verify image assets in the configured account") from None
    verified = {}
    for row in rows:
        asset = row.asset
        identity = str(asset.id)
        if (identity not in ids or identity in verified
                or asset.resource_name != _resource(ctx, "assets", identity)
                or asset.type_.name != "IMAGE"):
            raise ToolError("IMAGE_ASSET_UNVERIFIED", "image asset identity or IMAGE type does not match the configured account")
        width = asset.image_asset.full_size.width_pixels
        height = asset.image_asset.full_size.height_pixels
        size = asset.image_asset.file_size
        if (not all(type(value) is int and value > 0 for value in (width, height, size))
                or size > 5120000):
            raise ToolError("IMAGE_ASSET_UNVERIFIED", "image assets require positive integer dimensions and file size of 1-5120000 bytes")
        verified[identity] = (width, height)
    if set(verified) != ids:
        raise ToolError("IMAGE_ASSET_UNVERIFIED", "one or more requested image assets are missing from the configured account")
    for name, values in roles.items():
        for identity in values:
            width, height = verified[identity]
            if name == "landscape_image_asset_ids":
                # Exact integer arithmetic expresses the local one-pixel allowance.
                valid = width >= 600 and height >= 314 and abs(100 * width - 191 * height) <= 100
            else:
                valid = width == height and width >= (300 if name == "square_image_asset_ids" else 128)
            if not valid:
                raise ToolError("IMAGE_ASSET_UNVERIFIED", f"asset {identity} does not meet {name} dimension requirements")


def _check_customer(ctx, customer_id) -> None:
    """A write always lands on the configured account. Naming a different one
    is refused, never silently discarded — every mutation tool declares this
    parameter so the refusal cannot be dodged by the argument layer dropping
    an undeclared field."""
    if customer_id is None:
        return
    from ads_mcp.config import normalize_customer_id

    requested = normalize_customer_id(customer_id)
    if requested != ctx.config.customer_id:
        raise ToolError(
            "PLAN_CUSTOMER_MISMATCH",
            f"this server is configured to write to customer "
            f"{ctx.config.customer_id}; it will not mutate {requested}. Point "
            "GOOGLE_ADS_CUSTOMER_ID at that account and restart if that is "
            "what you intend.",
        )


def _numeric_id(value, name: str) -> str:
    """A single numeric id, for GAQL interpolation. Digits only."""
    text = str(value).strip()
    # isdigit() accepts superscripts and non-ASCII numerals that int() then
    # rejects deep inside an executor; isdecimal() is the right predicate.
    if not text.isdecimal():
        raise ToolError(
            "INVALID_ID", f"{name} must be numeric, got {str(value)!r}"
        )
    try:
        return str(int(text))
    except ValueError:
        raise ToolError(
            "INVALID_ID", f"{name} is too long; provide a numeric identifier",
        ) from None


def _resource_id(value, name: str = "entity id") -> str:
    """An id as it appears in a resource name.

    Google's composite resources are `{parentId}~{childId}` — an ad is
    `adGroupAds/{adGroupId}~{adId}`, a keyword
    `adGroupCriteria/{adGroupId}~{criterionId}`. Every segment must be
    numeric, but the tilde itself is part of the grammar: rejecting it makes
    ads and keywords unaddressable.
    """
    text = str(value).strip()
    parts = text.split("~")
    if not (1 <= len(parts) <= 2) or not all(p.isdecimal() for p in parts):
        raise ToolError(
            "INVALID_ID",
            f"{name} must be numeric, or numeric segments joined by '~' "
            f"(e.g. adGroupId~adId), got {str(value)!r}",
        )
    return "~".join(_numeric_id(part, name) for part in parts)


def _lifecycle_id(value, kind: str) -> str:
    """Validate the exact resource grammar for a lifecycle entity kind."""
    if kind in ("campaign", "ad_group"):
        return _numeric_id(value, "entity_id")
    parts = str(value).strip().split("~")
    if len(parts) != 2 or not all(part.isdecimal() for part in parts):
        child = "ad_id" if kind == "ad" else "criterion_id"
        raise ToolError(
            "INVALID_ID",
            f"entity_id for {kind} must be ad_group_id~{child}, "
            "with exactly two numeric segments",
        )
    return "~".join(_numeric_id(part, "entity_id") for part in parts)


def _enum_choice(value, allowed, name: str) -> str:
    """Validate an enum-ish argument at PLAN time.

    The plan is the operator's approval artifact: a plan that cannot execute
    should never be offered, and an unknown enum must not surface as an
    AttributeError from deep inside the executor after the plan is burned.
    """
    text = str(value or "").strip().upper()
    if text not in allowed:
        raise ToolError(
            f"INVALID_{name.upper()}",
            f"{name} must be one of {sorted(allowed)}, got {str(value)!r}",
        )
    return text


CAMPAIGN_BIDDING_STRATEGIES = frozenset(
    {"MAXIMIZE_CONVERSIONS", "MAXIMIZE_CONVERSION_VALUE", "MANUAL_CPC",
     "TARGET_CPA", "TARGET_ROAS", "MAXIMIZE_CLICKS"}
)
KEYWORD_MATCH_TYPES = frozenset({"EXACT", "PHRASE", "BROAD"})
DAYS_OF_WEEK = frozenset({
    "MONDAY", "TUESDAY", "WEDNESDAY", "THURSDAY", "FRIDAY", "SATURDAY", "SUNDAY",
})
SCHEDULE_MINUTES = frozenset({0, 15, 30, 45})
def _enum_names(module_path: str, class_name: str) -> frozenset:
    """Enum vocabularies come from the pinned client library, never from a
    hand-typed list that can drift out of sync in both directions."""
    import importlib

    module = importlib.import_module(module_path)
    wrapper = getattr(module, class_name)
    inner = getattr(wrapper, class_name.replace("Enum", ""))
    return frozenset(
        e.name for e in inner if e.name not in ("UNSPECIFIED", "UNKNOWN")
    )


CONVERSION_CATEGORIES = _enum_names(
    "google.ads.googleads.v25.enums.types.conversion_action_category",
    "ConversionActionCategoryEnum",
)
CHANNEL_TYPES = _enum_names(
    "google.ads.googleads.v25.enums.types.advertising_channel_type",
    "AdvertisingChannelTypeEnum",
)
# Enum membership is separate from the creation graphs this tool can supply.
DRAFT_CAMPAIGN_CHANNELS = frozenset({"SEARCH", "DISPLAY", "PERFORMANCE_MAX"})
COUNTING_TYPES = _enum_names(
    "google.ads.googleads.v25.enums.types.conversion_action_counting_type",
    "ConversionActionCountingTypeEnum",
)
ROTATION_MODES = _enum_names(
    "google.ads.googleads.v25.enums.types.ad_group_ad_rotation_mode",
    "AdGroupAdRotationModeEnum",
)
LIFECYCLE_STATUSES = _enum_names(
    "google.ads.googleads.v25.enums.types.campaign_status", "CampaignStatusEnum",
)
# The same pinned domains drive the wire schema and the named plan errors.
# Literal validation in the SDK would return plain text before our error layer.
LifecycleStatus = Annotated[str, Field(json_schema_extra={"enum": sorted(LIFECYCLE_STATUSES)})]
AdRotationMode = Annotated[str, Field(json_schema_extra={"enum": sorted(ROTATION_MODES)})]
_PINNED_BIDDING_TYPES = _enum_names(
    "google.ads.googleads.v25.enums.types.bidding_strategy_type", "BiddingStrategyTypeEnum",
)
if CAMPAIGN_BIDDING_STRATEGIES - (_PINNED_BIDDING_TYPES | {"MAXIMIZE_CLICKS"}):
    raise RuntimeError("campaign bidding strategies drifted from the pinned API")


def _creation_status(value) -> str:
    status = _enum_choice(value or "PAUSED", LIFECYCLE_STATUSES, "status")
    if status not in {"ENABLED", "PAUSED"}:
        raise ToolError(
            "INVALID_CREATION_STATUS",
            f"status {status} is a lifecycle value but cannot be used for creation; "
            "choose PAUSED or ENABLED",
        )
    return status


def _target_values(strategy, target_cpa, target_roas):
    """Targets affect bidding behavior, but are not daily budgets or CPC bids."""
    if target_cpa is not None:
        value = float(target_cpa)
        if not 0 < value <= 1e6 or int(value * MICROS) <= 0:
            raise ToolError(
                "INVALID_TARGET",
                "target_cpa must be finite, positive, at most 1000000, "
                "and remain positive when converted to micros",
            )
    if target_roas is not None and not .01 <= float(target_roas) <= 1000:
        raise ToolError("INVALID_TARGET", "target_roas must be finite and in 0.01..1000")
    if target_cpa is not None and target_roas is not None:
        raise ToolError("CONTRADICTORY_ARGUMENTS", "target_cpa and target_roas cannot coexist")
    if strategy is not None:
        if target_cpa is not None and strategy not in {"TARGET_CPA", "MAXIMIZE_CONVERSIONS"}:
            raise ToolError("CONTRADICTORY_ARGUMENTS", "target_cpa requires CPA or conversions bidding")
        if target_roas is not None and strategy not in {"TARGET_ROAS", "MAXIMIZE_CONVERSION_VALUE"}:
            raise ToolError("CONTRADICTORY_ARGUMENTS", "target_roas requires ROAS or conversion-value bidding")
        if strategy == "TARGET_CPA" and target_cpa is None:
            raise ToolError("MISSING_ARGUMENT", "TARGET_CPA requires target_cpa")
        if strategy == "TARGET_ROAS" and target_roas is None:
            raise ToolError("MISSING_ARGUMENT", "TARGET_ROAS requires target_roas")


def _target_ids(values, name):
    return [_numeric_id(value, name) for value in values or ()]

# Creative and measurement work only: none raises the daily ceiling,
# broadens the network, or creates a campaign. Keep this inspectable and
# fail at import/startup if a dependency change invalidates a name.
SPEND_NEUTRAL_TYPES = frozenset({
    "TEXT_AD", "RESPONSIVE_SEARCH_AD", "RESPONSIVE_SEARCH_AD_ASSET",
    "RESPONSIVE_SEARCH_AD_IMPROVE_AD_STRENGTH",
    "IMPROVE_PERFORMANCE_MAX_AD_STRENGTH", "IMPROVE_DEMAND_GEN_AD_STRENGTH",
    "CALLOUT_ASSET", "SITELINK_ASSET", "CALL_ASSET",
    "KEYWORD_MATCH_TYPE", "OPTIMIZE_AD_ROTATION",
    "IMPROVE_GOOGLE_TAG_COVERAGE", "REFRESH_CUSTOMER_MATCH_LIST",
})
_UNKNOWN_RECOMMENDATION_TYPES = SPEND_NEUTRAL_TYPES - _enum_names(
    "google.ads.googleads.v25.enums.types.recommendation_type",
    "RecommendationTypeEnum",
)
if _UNKNOWN_RECOMMENDATION_TYPES:  # pragma: no cover - dependency drift
    raise RuntimeError(
        "spend-neutral allowlist names absent from the pinned Google Ads "
        f"enum: {sorted(_UNKNOWN_RECOMMENDATION_TYPES)}"
    )

# Proposed daily budgets in the pinned v25 Recommendation messages. '*' walks
# every repeated option. Current budgets, CPA/ROAS targets, and impact cost
# estimates are deliberately excluded: none is the proposed daily ceiling.
# GAQL selects the parent MESSAGE, not these protobuf leaf paths.
RECOMMENDATION_BUDGET_PATHS = (
    *(
        f"{root}.{leaf}"
        for root in (
            "campaign_budget_recommendation",
            "forecasting_campaign_budget_recommendation",
            "marginal_roi_campaign_budget_recommendation",
            "move_unused_budget_recommendation.budget_recommendation",
        )
        for leaf in (
            "recommended_budget_amount_micros",
            "budget_options.*.budget_amount_micros",
        )
    ),
    "maximize_clicks_opt_in_recommendation.recommended_budget_amount_micros",
    "maximize_conversions_opt_in_recommendation.recommended_budget_amount_micros",
    "target_roas_opt_in_recommendation.required_campaign_budget_amount_micros",
    "target_cpa_opt_in_recommendation.options.*.required_campaign_budget_amount_micros",
    "use_broad_match_keyword_recommendation.required_campaign_budget_amount_micros",
    *(
        f"{root}.campaign_budget.recommended_new_amount_micros"
        for root in (
            "forecasting_set_target_cpa_recommendation",
            "forecasting_set_target_roas_recommendation",
            "set_target_cpa_recommendation",
            "set_target_roas_recommendation",
        )
    ),
)
RECOMMENDATION_BUDGET_MESSAGES = tuple(sorted({
    path.split(".", 1)[0] for path in RECOMMENDATION_BUDGET_PATHS
}))


def _recommendation_budget_values(message, path):
    if not path:
        # Unset protobuf scalars read as zero; they establish no budget.
        if message:
            yield Decimal(int(message)) / MICROS
        return
    head, *tail = path
    if head == "*":
        for option in message:
            yield from _recommendation_budget_values(option, tail)
    else:
        yield from _recommendation_budget_values(getattr(message, head), tail)


def _check_recommendation_budget(ctx, resource_name):
    fields = ", ".join(
        f"recommendation.{name}" for name in RECOMMENDATION_BUDGET_MESSAGES
    )
    rows = ctx.search(
        "SELECT recommendation.resource_name, recommendation.type, "
        f"{fields} FROM recommendation"
    )
    rec = next(
        (r.recommendation for r in rows
         if r.recommendation.resource_name == resource_name),
        None,
    )
    if rec is None:
        raise ToolError(
            "RECOMMENDATION_NOT_FOUND",
            f"recommendation {resource_name} was not found on this account; "
            "its spend impact cannot be checked against the configured "
            "caps, so it will not be applied",
        )
    amounts = [
        value
        for path in RECOMMENDATION_BUDGET_PATHS
        for value in _recommendation_budget_values(rec, path.split("."))
    ]
    if amounts:
        amount = max(amounts)
        guardrails.check_budget(ctx, amount)
        return amount
    if rec.type_.name not in SPEND_NEUTRAL_TYPES:
        raise ToolError(
            "SPEND_IMPACT_UNBOUNDED",
            f"recommendation type {rec.type_.name} does not name a daily "
            "budget this server can check against ADS_MCP_MAX_DAILY_BUDGET, "
            "and is not on the known-spend-neutral list; it will not be "
            "applied here. Apply it deliberately in the Google Ads UI "
            "if you want it.",
        )
    return None


# Accepted audience aliases map to Google Ads CustomAudienceType values.
# Preserve these public spellings when constructing provider requests.
CUSTOM_AUDIENCE_ALIASES = {
    "WEBSITE_VISITORS": "AUTO",
    "INTERESTS": "INTEREST",
    "CUSTOM_INTENT": "PURCHASE_INTENT",
    "SEARCH_TERMS": "SEARCH",
}
CUSTOM_AUDIENCE_TYPES = frozenset(
    {"AUTO", "INTEREST", "PURCHASE_INTENT", "SEARCH"} | set(CUSTOM_AUDIENCE_ALIASES)
)


def _resource(ctx, kind: str, *ids) -> str:
    tail = "~".join(_resource_id(i) for i in ids)
    return f"customers/{ctx.config.customer_id}/{kind}/{tail}"


def _first_row(ctx, query: str, matcher=None):
    rows = ctx.search(query)
    if matcher is None:
        return rows[0] if rows else None
    return next((r for r in rows if matcher(r)), None)


def _bid_row(ctx, query, matcher, label):
    """Missing, mismatched and failed reads never establish a zero bid."""
    try:
        row = _first_row(ctx, query, matcher=matcher)
    except Exception as exc:
        _preserve_auth_cause(ctx, exc)
        raise ToolError(
            "BID_BASELINE_UNVERIFIED", f"could not read the account bid for {label}"
        ) from None
    if row is None:
        raise ToolError(
            "BID_BASELINE_UNVERIFIED", f"no matching account bid record for {label}"
        )
    return row


def _ad_group_bid_row(ctx, ad_group_id):
    gid = _numeric_id(ad_group_id, "ad_group_id")
    return _bid_row(
        ctx,
        "SELECT ad_group.id, ad_group.name, ad_group.cpc_bid_micros "
        f"FROM ad_group WHERE ad_group.id = {gid}",
        lambda r: int(r.ad_group.id) == int(gid),
        f"ad group {gid}",
    )


def _account_bid(ctx, ad_group_id, criterion_id=None):
    """Fetch the existing entity's own bid, then keyword parent inheritance."""
    gid = _numeric_id(ad_group_id, "ad_group_id")
    if criterion_id is not None:
        kid = _numeric_id(criterion_id, "criterion_id")
        row = _bid_row(
            ctx,
            "SELECT ad_group_criterion.criterion_id, "
            "ad_group_criterion.cpc_bid_micros, ad_group.id "
            f"FROM ad_group_criterion WHERE ad_group.id = {gid} "
            f"AND ad_group_criterion.criterion_id = {kid}",
            lambda r: (int(r.ad_group.id) == int(gid)
                       and int(r.ad_group_criterion.criterion_id) == int(kid)),
            f"keyword {gid}~{kid}",
        )
        own = int(row.ad_group_criterion.cpc_bid_micros)
        if own > 0:
            return Decimal(own) / MICROS
    row = _ad_group_bid_row(ctx, gid)
    return Decimal(max(0, int(row.ad_group.cpc_bid_micros))) / MICROS


def _check_account_bid(ctx, ad_group_id, new, criterion_id=None):
    account = _account_bid(ctx, ad_group_id, criterion_id)
    guardrails.check_bid(ctx, None, new, account_current=account)
    return account


def _supported_fields(value, allowed, name):
    """Reject unsupported intent before any staging or account lookup."""
    unknown = value.keys() - allowed
    if unknown:
        raise ToolError(
            "UNSUPPORTED_ARGUMENT",
            f"{name} has unsupported fields: {', '.join(sorted(unknown))}",
        )


def _keywords(keywords, *, negative=False):
    """Copy and validate the keyword fields that the executor will send."""
    result = []
    for source in keywords:
        allowed = {"text", "match_type"}
        if not negative:
            allowed.add("cpc_bid_micros")
        _supported_fields(source, allowed, "keyword")
        kw = dict(source)
        text = kw.get("text")
        if not isinstance(text, str) or not text.strip():
            raise ToolError("MISSING_ARGUMENT", "keyword text is required")
        # Keywords have a distinct codepoint/word limit, not creative width.
        if len(text) > 80 or len(text.split()) > 10:
            raise ToolError("INVALID_KEYWORD", "keyword text must fit 80 codepoints and 10 words")
        kw["match_type"] = _enum_choice(
            _require(kw.get("match_type"), "match_type"), KEYWORD_MATCH_TYPES, "match_type"
        )
        if kw.get("cpc_bid_micros") is not None:
            try:
                micros = Decimal(str(kw["cpc_bid_micros"]))
            except (InvalidOperation, ValueError, TypeError):
                raise ToolError("INVALID_BID", "cpc_bid_micros must be an integer") from None
            if not micros.is_finite():
                raise ToolError("INVALID_BID", "cpc_bid_micros must be finite")
            guardrails.bid_value(micros / MICROS)
            if micros != micros.to_integral_value():
                raise ToolError("INVALID_BID", "cpc_bid_micros must be an integer")
            kw["cpc_bid_micros"] = int(micros)
        result.append(kw)
    return result


def _check_keyword_bids(ctx, keywords, ad_group_id=None):
    bids = [Decimal(kw["cpc_bid_micros"]) / MICROS for kw in keywords
            if kw.get("cpc_bid_micros") is not None]
    if not bids:
        return
    # A new campaign's new ad group has no bid. Existing parents must be read.
    baseline = _account_bid(ctx, ad_group_id) if ad_group_id is not None else Decimal(0)
    for bid in bids:
        guardrails.check_bid(ctx, None, bid, account_current=baseline)


def _campaign_row(ctx, campaign_id: str):
    cid = str(campaign_id)
    return _first_row(
        ctx,
        "SELECT campaign.id, campaign.name, campaign.status, "
        "campaign.resource_name, campaign.campaign_budget, "
        "campaign.bidding_strategy_type, campaign.bidding_strategy, "
        "campaign.target_cpa.target_cpa_micros, campaign.target_roas.target_roas, "
        "campaign.maximize_conversion_value.target_roas, "
        "campaign.maximize_conversions.target_cpa_micros, "
        "campaign_budget.id, campaign_budget.resource_name, "
        "campaign_budget.amount_micros, "
        "customer.currency_code FROM campaign "
        f"WHERE campaign.id = {_numeric_id(cid, 'campaign_id')}",
        matcher=lambda r: str(r.campaign.id) == cid,
    )


def _campaign_strategy_identity(ctx, campaign_id, row):
    """Only a verified standard strategy may authorize a target-only edit."""
    if (row is None
            or str(extract_field(row, "campaign.id")) != str(campaign_id)
            or extract_field(row, "campaign.resource_name")
            != _resource(ctx, "campaigns", campaign_id)):
        raise ToolError(
            "CAMPAIGN_STRATEGY_UNVERIFIED",
            f"could not verify campaign {campaign_id}; read the account and stage a fresh plan",
        )
    strategy = extract_field(row, "campaign.bidding_strategy_type")
    portfolio = extract_field(row, "campaign.bidding_strategy")
    if portfolio:
        raise ToolError(
            "PORTFOLIO_TARGET_UNSUPPORTED",
            "target-only edits cannot change a portfolio strategy; "
            "use an explicit supported bidding_strategy switch instead",
        )
    if strategy not in {"TARGET_CPA", "TARGET_ROAS", "MAXIMIZE_CONVERSIONS",
                        "MAXIMIZE_CONVERSION_VALUE"}:
        raise ToolError(
            "CAMPAIGN_STRATEGY_UNVERIFIED",
            "current campaign strategy is missing, unknown or unsupported for "
            "target-only edits; use an explicit supported bidding_strategy switch",
        )
    return (extract_field(row, "campaign.resource_name"), strategy, portfolio)


def _campaign_target_row(ctx, campaign_id):
    try:
        row = _campaign_row(ctx, campaign_id)
    except Exception as exc:
        _preserve_auth_cause(ctx, exc)
        raise ToolError(
            "CAMPAIGN_STRATEGY_UNVERIFIED",
            f"could not read the strategy for campaign {campaign_id}; "
            "read the account and stage a fresh plan",
        ) from None
    _campaign_strategy_identity(ctx, campaign_id, row)
    return row


def _check_campaign_strategy(ctx, campaign_id, expected):
    row = _campaign_target_row(ctx, campaign_id)
    if _campaign_strategy_identity(ctx, campaign_id, row) != expected:
        raise ToolError(
            "CAMPAIGN_STRATEGY_CHANGED",
            "campaign strategy changed since preview; read the account and stage a fresh plan",
        )


def _campaign_target_path(strategy, target, clear):
    families = ({"TARGET_CPA", "MAXIMIZE_CONVERSIONS"} if target == "target_cpa"
                else {"TARGET_ROAS", "MAXIMIZE_CONVERSION_VALUE"})
    if strategy not in families:
        raise ToolError(
            "CONTRADICTORY_ARGUMENTS",
            f"{target} is incompatible with {strategy}; use an explicit bidding_strategy switch",
        )
    if clear and strategy.startswith("TARGET_"):
        replacement = ("MAXIMIZE_CONVERSIONS" if target == "target_cpa"
                       else "MAXIMIZE_CONVERSION_VALUE")
        raise ToolError(
            "REQUIRED_TARGET_CANNOT_CLEAR",
            f"{strategy} requires {target}; explicitly switch bidding_strategy "
            f"to {replacement} to remove the required target",
        )
    leaf = "target_cpa_micros" if target == "target_cpa" else "target_roas"
    return f"{executors.CAMPAIGN_STRATEGY_FIELDS[strategy]}.{leaf}"


def _campaign_budget_row(ctx, campaign_id, expected_budget_id=None):
    """Verify the account resource and its budget before using a baseline.

    At apply, the linked budget must still be the one in the staged executor;
    a different budget cannot authorize changing that original resource.
    """
    _numeric_id(campaign_id, "campaign_id")
    try:
        row = _campaign_row(ctx, campaign_id)
        if row is not None:
            budget_id = int(row.campaign_budget.id)
            budget_resource = _resource(ctx, "campaignBudgets", budget_id)
            verified = (
                int(row.campaign.id) == int(campaign_id)
                and row.campaign.resource_name == _resource(ctx, "campaigns", campaign_id)
                and budget_id > 0
                and row.campaign_budget.resource_name == budget_resource
                and row.campaign.campaign_budget == budget_resource
                and int(row.campaign_budget.amount_micros) >= 0
                and (expected_budget_id is None or budget_id == expected_budget_id)
            )
            if verified:
                return row
    except Exception as exc:
        _preserve_auth_cause(ctx, exc)
        # Do not expose provider errors or treat unavailable evidence as zero.
    raise ToolError(
        "BUDGET_BASELINE_UNVERIFIED",
        f"could not verify the campaign and linked budget for campaign {campaign_id}; "
        "read the account and stage a fresh plan",
    )


def _check_campaign_budget(ctx, campaign_id, budget_id, amount):
    row = _campaign_budget_row(ctx, campaign_id, expected_budget_id=budget_id)
    guardrails.check_budget(
        ctx, amount, current=int(row.campaign_budget.amount_micros) / MICROS,
    )


def _plan_payload(ctx, *, tool, summary, operations, execute,
                  irreversible=False, rechecks=None) -> dict:
    entry = ctx.plan_store.create(
        tool=tool,
        customer_id=ctx.config.customer_id,
        summary=summary,
        operations=operations,
        execute=execute,
        irreversible=irreversible,
        rechecks=rechecks,
    )
    if ctx.audit is not None:
        try:
            # Fail closed at the earliest lifecycle event: an unauditable
            # mutation pipeline refuses to even stage work.
            ctx.audit.write(
                {
                    "event": "plan_created",
                    "tool": tool,
                    "customer_id": ctx.config.customer_id,
                    "outcome": "staged",
                    "plan_id": entry.id,
                    "summary": summary,
                    "operations": operations,
                },
                critical=True,
            )
        except ToolError:
            ctx.plan_store.discard(entry.id)
            raise
    return {"plan": entry.payload()}


def _audit_refusal(ctx, tool: str, err: ToolError):
    if ctx.audit is not None:
        ctx.observe_audit(
            {
                "event": "refused",
                "tool": tool,
                "customer_id": ctx.config.customer_id,
                "outcome": err.code,
                "message": ctx.scrub(err.message),
                "plan_id": ctx.current_plan_id,
            },
        )


def _guarded_mutation(ctx, tool_name, impl, *, plan_id=None):
    def _call(**kwargs):
        try:
            return impl(**kwargs)
        except Exception as exc:
            err = classify_exception(exc, scrub=ctx.scrub)
            _audit_refusal(ctx, tool_name, err)
            ctx.current_tool = tool_name
            raise err from None

    return guarded(ctx, _call, name=tool_name, plan_id=plan_id)


# ---------------------------------------------------------------------------
# Entity kinds shared with ads_mcp.executors (validation + resource paths)

_ENTITY_SERVICES = {
    "campaign": ("CampaignService", "mutate_campaigns", "CampaignOperation",
                 "MutateCampaignsRequest", "campaigns"),
    "ad_group": ("AdGroupService", "mutate_ad_groups", "AdGroupOperation",
                 "MutateAdGroupsRequest", "adGroups"),
    "ad": ("AdGroupAdService", "mutate_ad_group_ads", "AdGroupAdOperation",
           "MutateAdGroupAdsRequest", "adGroupAds"),
    "keyword": ("AdGroupCriterionService", "mutate_ad_group_criteria",
                "AdGroupCriterionOperation", "MutateAdGroupCriteriaRequest",
                "adGroupCriteria"),
}

# Metadata declares status-only support without extending removal's mapping.
StatusEntityKind = Annotated[str, Field(json_schema_extra={
    "enum": sorted([*_ENTITY_SERVICES, "asset_group"]),
})]


# ---------------------------------------------------------------------------
# Registration


def register(server, ctx):  # noqa: C901 — one tool per block, deliberately flat
    cfg = ctx.config

    @server.tool(
        name="end_pmax_url_experiment",
        description=_spec(
            "end_pmax_url_experiment",
            "Stage ending a verified enabled PMax URL experiment that has started and has "
            "not passed its end date in the account timezone. Promotion must be NOT_STARTED. "
            "Uses the dedicated provider validate-only request before creating a plan. "
            "Requires preview, confirm_and_apply and irreversible acknowledgement; this "
            "workflow cannot resume the experiment. Rechecks complete state before one "
            "real action. Returns accepted submission and separately observed state, "
            "without promising a HALTED status or manually reverting settings. Configured account only.",
        ),
    )
    def end_pmax_url_experiment(experiment_id: StrictStr, customer_id: StrictStr | None = None) -> dict:
        from ads_mcp import pmax_experiment_lifecycle

        def impl():
            return _plan_payload(ctx, **pmax_experiment_lifecycle.plan(
                ctx, action="end", experiment_id=experiment_id, customer_id=customer_id,
            ))

        return _guarded_mutation(ctx, "end_pmax_url_experiment", impl)()

    @server.tool(
        name="promote_pmax_url_experiment",
        description=_spec(
            "promote_pmax_url_experiment",
            "Stage permanent promotion of treatment settings for an enabled PMax URL "
            "experiment that has started and has not passed its end date in account time. "
            "Promotion must be NOT_STARTED. Dedicated provider validate-only precedes "
            "the plan; preview, confirm_and_apply and irreversible acknowledgement are "
            "required. Rechecks complete state before one real action. Pending submission "
            "is not application; retain operation_name for later observation. Application "
            "requires verified completion and both treatment settings enabled. Configured account only.",
        ),
    )
    def promote_pmax_url_experiment(experiment_id: StrictStr, customer_id: StrictStr | None = None) -> dict:
        from ads_mcp import pmax_experiment_lifecycle

        def impl():
            return _plan_payload(ctx, **pmax_experiment_lifecycle.plan(
                ctx, action="promote", experiment_id=experiment_id, customer_id=customer_id,
            ))

        return _guarded_mutation(ctx, "promote_pmax_url_experiment", impl)()

    @server.tool(
        name="create_pmax_url_experiment",
        description=_spec(
            "create_pmax_url_experiment",
            "Stage a provider-validated 50/50 final URL expansion experiment on one enabled "
            "PMax campaign in the configured account. Requires expansion explicitly opted out, "
            "no nonremoved experiment collision, an exact NFC name of 1–255 UTF-8 bytes, "
            "and explicit ISO dates within campaign dates. Start must be today through 365 "
            "days ahead in the verified account timezone; duration is at most 366 inclusive days. "
            "Preserves unrelated automation settings. Requires preview and confirm_and_apply; "
            "provider validate-only is separate from preview and cannot guarantee serving. "
            "Accepted creation is followed by readback; unknown verification requires inspection.",
        ),
    )
    def create_pmax_url_experiment(
        campaign_id: StrictStr, name: StrictStr, date_start: StrictStr, date_end: StrictStr,
        customer_id: StrictStr | None = None,
    ) -> dict:
        from ads_mcp import pmax_experiment_create

        def impl():
            return _plan_payload(ctx, **pmax_experiment_create.plan(
                ctx, campaign_id=campaign_id, name=name, date_start=date_start,
                date_end=date_end, customer_id=customer_id,
            ))

        return _guarded_mutation(ctx, "create_pmax_url_experiment", impl)()

    @server.tool(
        name="update_demographic_targeting",
        description=_spec(
            "update_demographic_targeting",
            "Stage 1–20 exact dimension/value/action records in the configured account. "
            "Supports AGE_RANGE, GENDER and INCOME_RANGE on standard Search and Display, "
            "plus PARENTAL_STATUS on Display. Use explicit category enum names and INCLUDE "
            "or EXCLUDE. Replacements refuse direct customization and require irreversible "
            "acknowledgement. Every requested dimension must retain a known unexcluded "
            "category after campaign exclusions and the entire batch. Requires preview and "
            "confirm_and_apply with complete state rechecks; provider geography and policy "
            "limits still apply. Does not establish effective eligibility or alter expansion.",
        ),
    )
    def update_demographic_targeting(
        ad_group_id: StrictStr, changes: list[dict], customer_id: StrictStr | None = None,
    ) -> dict:
        from ads_mcp import demographics

        def impl():
            return _plan_payload(ctx, **demographics.plan(
                ctx, ad_group_id=ad_group_id, changes=changes, customer_id=customer_id,
            ))

        return _guarded_mutation(ctx, "update_demographic_targeting", impl)()

    def _shared_plan(tool, **kwargs):
        from ads_mcp import shared_negatives

        def impl():
            return _plan_payload(ctx, **shared_negatives.plan(ctx, tool=tool, **kwargs))

        return _guarded_mutation(ctx, tool, impl)()

    @server.tool(
        name="create_shared_negative_keyword_list",
        description=_spec(
            "create_shared_negative_keyword_list",
            "Stage an empty negative-keyword list in the configured account. Name requires "
            "original NFC text of 1–255 UTF-8 bytes without edge whitespace or controls. "
            "Active name collisions refuse under NFC and casefold comparison. "
            "Execution requires confirm_and_apply and the configured preview safeguards.",
        ),
    )
    def create_shared_negative_keyword_list(name: StrictStr, customer_id: StrictStr | None = None) -> dict:
        return _shared_plan("create_shared_negative_keyword_list", name=name, customer_id=customer_id)

    @server.tool(
        name="add_shared_negative_keywords",
        description=_spec(
            "add_shared_negative_keywords",
            "Stage 1–100 exact text/match_type records for a shared negative list. "
            "Match types are BROAD, PHRASE and EXACT; original text has local limits of "
            "80 codepoints and 10 words without edge whitespace or controls. "
            "Duplicate text/match pairs refuse under NFC and casefold comparison. "
            "Preview shows complete membership and all affected standard Search/Shopping "
            "campaigns. Execution requires confirm_and_apply; serving may change.",
        ),
    )
    def add_shared_negative_keywords(
        shared_set_id: StrictStr, keywords: list[dict], customer_id: StrictStr | None = None,
    ) -> dict:
        return _shared_plan("add_shared_negative_keywords", shared_set_id=shared_set_id,
                            keywords=keywords, customer_id=customer_id)

    @server.tool(
        name="remove_shared_negative_keywords",
        description=_spec(
            "remove_shared_negative_keywords",
            "Stage removal of 1–100 distinct canonical positive criterion ID strings from "
            "a shared negative list. Complete membership and affected campaigns are reviewed "
            "and rechecked. Requires confirm_and_apply and irreversible acknowledgement; "
            "serving may change across all linked campaigns.",
        ),
    )
    def remove_shared_negative_keywords(
        shared_set_id: StrictStr, criterion_ids: list[StrictStr], customer_id: StrictStr | None = None,
    ) -> dict:
        return _shared_plan("remove_shared_negative_keywords", shared_set_id=shared_set_id,
                            criterion_ids=criterion_ids, customer_id=customer_id)

    @server.tool(
        name="attach_shared_negative_keyword_list",
        description=_spec(
            "attach_shared_negative_keyword_list",
            "Stage association of a shared negative list with 1–100 distinct canonical "
            "campaign ID strings in the configured account. Only enabled/paused standard "
            "Search and Shopping campaigns are supported, including existing links. "
            "Requires confirm_and_apply; complete list and campaign state are rechecked.",
        ),
    )
    def attach_shared_negative_keyword_list(
        shared_set_id: StrictStr, campaign_ids: list[StrictStr], customer_id: StrictStr | None = None,
    ) -> dict:
        return _shared_plan("attach_shared_negative_keyword_list", shared_set_id=shared_set_id,
                            campaign_ids=campaign_ids, customer_id=customer_id)

    @server.tool(
        name="detach_shared_negative_keyword_list",
        description=_spec(
            "detach_shared_negative_keyword_list",
            "Stage removal of 1–100 existing campaign associations from a shared negative "
            "list. Complete state is reviewed and rechecked; list members remain intact. "
            "Requires confirm_and_apply and irreversible acknowledgement. Detachment can "
            "change serving; a surviving campaign and list can subsequently be reattached.",
        ),
    )
    def detach_shared_negative_keyword_list(
        shared_set_id: StrictStr, campaign_ids: list[StrictStr], customer_id: StrictStr | None = None,
    ) -> dict:
        return _shared_plan("detach_shared_negative_keyword_list", shared_set_id=shared_set_id,
                            campaign_ids=campaign_ids, customer_id=customer_id)

    @server.tool(
        name="update_responsive_search_ad_urls",
        description=_spec(
            "update_responsive_search_ad_urls",
            "Stage an existing responsive search ad destination update in the configured "
            "account. Omitted or null lists preserve current values; supplied lists replace "
            "them in order. Empty mobile URLs clear them; final URLs must remain nonempty. "
            "Local limits are 10 unique HTTP(S) URLs per list and 2048 Unicode codepoints "
            "per URL, without user information, whitespace or controls. Exact spelling is "
            "preserved. No-op plans refuse. Execution requires confirm_and_apply; "
            "preview is required by default, configured with ADS_MCP_REQUIRE_DRY_RUN. "
            "Fresh checks bind parent, creative and tracking state before an AdService "
            "URL-only update. Provider policy and validation can still refuse the change.",
        ),
    )
    def update_responsive_search_ad_urls(
        ad_group_id: str, ad_id: str, final_urls: list[str] | None = None,
        final_mobile_urls: list[str] | None = None, customer_id: str | None = None,
    ) -> dict:
        from ads_mcp import search_urls

        def impl():
            _check_customer(ctx, customer_id)
            return _plan_payload(ctx, **search_urls.ad_url_plan(
                ctx, ad_group_id=ad_group_id, ad_id=ad_id,
                final_urls=final_urls, final_mobile_urls=final_mobile_urls,
            ))

        return _guarded_mutation(ctx, "update_responsive_search_ad_urls", impl)()

    @server.tool(
        name="update_keyword_urls",
        description=_spec(
            "update_keyword_urls",
            "Stage destination overrides for an existing positive Search keyword in the "
            "configured account. Omitted or null lists preserve current values; supplied "
            "lists replace them in order. Empty lists clear URLs. Nonempty mobile URLs "
            "require final URLs; clearing finals also requires absent tracking template "
            "and custom parameters. Tracking is never silently erased. Local limits are "
            "10 unique HTTP(S) URLs per list and 2048 Unicode codepoints per URL, with a "
            "host and valid port, without user information, whitespace or controls. Exact "
            "spelling is preserved. No-op plans refuse. Execution requires confirm_and_apply; "
            "preview is required by default, configured with ADS_MCP_REQUIRE_DRY_RUN. "
            "Fresh keyword and parent checks precede a URL-only update. "
            "Keyword text, match type, bids, status and suffix are preserved. Provider "
            "validation and policy review can still refuse the change.",
        ),
    )
    def update_keyword_urls(
        ad_group_id: str, criterion_id: str, final_urls: list[str] | None = None,
        final_mobile_urls: list[str] | None = None, customer_id: str | None = None,
    ) -> dict:
        from ads_mcp import search_urls

        def impl():
            _check_customer(ctx, customer_id)
            return _plan_payload(ctx, **search_urls.keyword_url_plan(
                ctx, ad_group_id=ad_group_id, criterion_id=criterion_id,
                final_urls=final_urls, final_mobile_urls=final_mobile_urls,
            ))

        return _guarded_mutation(ctx, "update_keyword_urls", impl)()

    def _signal_plan(tool, customer_id, **kwargs):
        from ads_mcp import pmax

        def impl():
            _check_customer(ctx, customer_id)
            return _plan_payload(ctx, **pmax.signal_plan(ctx, tool=tool, **kwargs))

        return _guarded_mutation(ctx, tool, impl)()

    @server.tool(
        name="add_asset_group_search_themes",
        description=_spec(
            "add_asset_group_search_themes",
            "Plan search-theme additions to a verified Performance Max asset group. "
            "themes is a nonempty list of distinct, stripped nonblank strings, "
            "at most 80 Unicode codepoints each, without control characters. "
            "The local ceiling is 50 resulting themes including existing themes; "
            "this does not guarantee Google acceptance. Signals guide optimization, "
            "not hard targeting. Requires complete bounded state, fresh apply "
            "validation and the normal preview/confirm flow.",
        ),
    )
    def add_asset_group_search_themes(
        asset_group_id: str,
        themes: list[str],
        customer_id: str | None = None,
    ) -> dict:
        return _signal_plan("add_asset_group_search_themes", customer_id,
                            asset_group_id=asset_group_id, themes=themes)

    @server.tool(
        name="add_asset_group_audience_signal",
        description=_spec(
            "add_asset_group_audience_signal",
            "Plan attachment of an existing enabled Audience as a Performance Max "
            "optimization signal, not hard targeting. Positive numeric audience_id "
            "must belong to the configured account and have CUSTOMER scope or "
            "ASSET_GROUP scope matching asset_group_id. Refuses duplicate attachment. "
            "Does not create audiences or edit composition. Complete signal state "
            "and audience state are bound to the plan and rechecked before apply.",
        ),
    )
    def add_asset_group_audience_signal(
        asset_group_id: str,
        audience_id: str,
        customer_id: str | None = None,
    ) -> dict:
        return _signal_plan("add_asset_group_audience_signal", customer_id,
                            asset_group_id=asset_group_id, audience_id=audience_id)

    @server.tool(
        name="remove_asset_group_signals",
        description=_spec(
            "remove_asset_group_signals",
            "Plan irreversible removal of existing search_theme or audience signals "
            "from a verified Performance Max asset group. signal_ids is a nonempty "
            "list of distinct positive numeric child IDs, without group prefixes or "
            "resource names. Other signal kinds are unsupported and refused. "
            "Signals guide optimization, not hard targeting. Requires complete "
            "bounded state, fresh apply validation and irreversible acknowledgement.",
        ),
    )
    def remove_asset_group_signals(
        asset_group_id: str,
        signal_ids: list[str],
        customer_id: str | None = None,
    ) -> dict:
        return _signal_plan("remove_asset_group_signals", customer_id,
                            asset_group_id=asset_group_id, signal_ids=signal_ids)

    def _url_plan(tool, customer_id, **kwargs):
        from ads_mcp import pmax

        def impl():
            _check_customer(ctx, customer_id)
            return _plan_payload(ctx, **pmax.url_plan(ctx, tool=tool, **kwargs))

        return _guarded_mutation(ctx, tool, impl)()

    @server.tool(
        name="set_pmax_final_url_expansion",
        description=_spec(
            "set_pmax_final_url_expansion",
            "Plan final URL expansion for a verified Performance Max campaign using "
            "the current asset automation setting. enabled is a strict boolean. "
            "Preserves every unrelated automation setting in order. Expansion permits "
            "different landing destinations and generated text for those pages. "
            "Disabling it does not disable independent text customization. Complete "
            "settings and campaign state are rechecked before apply; changed state "
            "requires a fresh preview. Writes target only the configured account.",
        ),
    )
    def set_pmax_final_url_expansion(
        campaign_id: str,
        enabled: StrictBool,
        customer_id: str | None = None,
    ) -> dict:
        return _url_plan("set_pmax_final_url_expansion", customer_id,
                         campaign_id=campaign_id, enabled=enabled)

    @server.tool(
        name="add_pmax_url_exclusion",
        description=_spec(
            "add_pmax_url_exclusion",
            "Plan one negative WEBPAGE URL exclusion for a verified Performance Max "
            "campaign. EXACT requires an HTTP(S) URL without user info; CONTAINS "
            "accepts a nonblank URL fragment. Whitespace, controls and duplicate rules "
            "are refused. Creates exactly one URL condition. Exclusions are not "
            "universal destination blocks: explicitly supplied final URLs and applicable "
            "Merchant Center inventory can still serve. Requires complete bounded state "
            "and a fresh recheck before apply; writes use only the configured account.",
        ),
    )
    def add_pmax_url_exclusion(
        campaign_id: str,
        url: str,
        match_type: Annotated[str, Field(json_schema_extra={"enum": ["EXACT", "CONTAINS"]})] = "EXACT",
        customer_id: str | None = None,
    ) -> dict:
        return _url_plan("add_pmax_url_exclusion", customer_id,
                         campaign_id=campaign_id, url=url, match_type=match_type)

    @server.tool(
        name="remove_pmax_url_exclusions",
        description=_spec(
            "remove_pmax_url_exclusions",
            "Plan irreversible removal of exact existing negative WEBPAGE URL criteria "
            "from a verified Performance Max campaign. criterion_ids is a nonempty "
            "list of distinct positive numeric child IDs, without campaign prefixes "
            "or resource names. Preserves unrelated criteria and previews every removed "
            "condition. Requires complete bounded state, a fresh recheck, preview and "
            "irreversible acknowledgement. Exclusions are not universal destination "
            "blocks: explicitly supplied final URLs and applicable Merchant Center "
            "inventory can still serve. Writes use only the configured account.",
        ),
    )
    def remove_pmax_url_exclusions(
        campaign_id: str,
        criterion_ids: list[str],
        customer_id: str | None = None,
    ) -> dict:
        return _url_plan("remove_pmax_url_exclusions", customer_id,
                         campaign_id=campaign_id, criterion_ids=criterion_ids)

    @server.tool(
        name="set_asset_group_product_selection",
        description=_spec(
            "set_asset_group_product_selection",
            "Plan irreversible replacement of an existing feed-linked Performance Max "
            "asset group's complete product tree. item_ids must contain 1 to 998 distinct "
            "trimmed, case-preserved Item IDs, at most 128 Unicode codepoints each, without "
            "controls or internal whitespace. Includes these items and excludes everything "
            "else. Supports empty trees, a single all-products unit, or flat Item-ID trees "
            "with one catch-all; nested trees and other dimensions are refused. Reads at "
            "most 1000 existing nodes plus one lookahead and requires complete state. "
            "Shows every before/after node and rechecks group, campaign feed and tree before "
            "apply; changed state returns STALE_PLAN. Uses one atomic v25 tree request, "
            "normal preview and irreversible acknowledgement. Alters inventory eligibility "
            "and may affect delivery and spend. Provider acceptance is not guaranteed. "
            "Writes only to the configured account; verify afterwards with get_listing_groups.",
        ),
    )
    def set_asset_group_product_selection(
        asset_group_id: str,
        item_ids: list[str],
        customer_id: str | None = None,
    ) -> dict:
        from ads_mcp import pmax

        def impl():
            _check_customer(ctx, customer_id)
            return _plan_payload(ctx, **pmax.product_selection_plan(
                ctx, asset_group_id=asset_group_id, item_ids=item_ids,
            ))

        return _guarded_mutation(ctx, "set_asset_group_product_selection", impl)()

    def entity_kind(entity_type: str) -> str:
        kind = str(entity_type or "").strip().lower()
        if kind not in _ENTITY_SERVICES:
            raise ToolError(
                "INVALID_ENTITY_TYPE",
                f"entity_type must be one of {sorted(_ENTITY_SERVICES)}, got {entity_type!r}",
            )
        return kind

    # -- update_campaign -----------------------------------------------------

    @server.tool(
        name="update_campaign",
        description=_spec(
            "update_campaign",
            "Stage campaign changes: daily budget, status, name, bidding strategy, "
            "additive geo/language criteria, and tROAS/tCPA "
            "targets. Target-only edits preserve the verified current standard "
            "strategy. clear_target_roas / clear_target_cpa unset optional "
            "maximize targets; required TARGET_ROAS / TARGET_CPA targets need "
            "an explicit bidding_strategy switch to remove them. "
            "Strategy and target changes are uncapped by the daily-budget/CPC "
            "cap model. Returns a plan; nothing applies until confirm_and_apply.",
        ),
    )
    def update_campaign(
        campaign_id: str,
        daily_budget: float | None = None,
        name: str | None = None,
        status: str | None = None,
        target_roas: float | None = None,
        target_cpa: float | None = None,
        clear_target_roas: bool = False,
        clear_target_cpa: bool = False,
        customer_id: str | None = None,
        bidding_strategy: str | None = None,
        geo_target_ids: list[str] | None = None,
        language_ids: list[str] | None = None,
    ) -> dict:
        def impl(campaign_id, **_kw):
            _check_customer(ctx, customer_id)
            campaign_id = _numeric_id(
                _require(campaign_id, "campaign_id"), "campaign_id"
            )
            from ads_mcp.config import normalize_customer_id

            _check_customer(ctx, customer_id)
            strategy = (_enum_choice(bidding_strategy, CAMPAIGN_BIDDING_STRATEGIES,
                                     "bidding_strategy") if bidding_strategy is not None else None)
            _target_values(strategy, target_cpa, target_roas)
            target_edits = (
                ("target_roas", target_roas, clear_target_roas),
                ("target_cpa", target_cpa, clear_target_cpa),
            )
            for target, value, clear in target_edits:
                if clear and value is not None:
                    raise ToolError(
                        "CONTRADICTORY_ARGUMENTS",
                        f"{target} and clear_{target} cannot both be given",
                    )
            preserve_strategy = strategy is None and any(
                value is not None or clear for _, value, clear in target_edits
            )
            geos = _target_ids(geo_target_ids, "geo_target_ids")
            languages = _target_ids(language_ids, "language_ids")
            row = (_campaign_budget_row(ctx, campaign_id) if daily_budget is not None
                   else _campaign_target_row(ctx, campaign_id) if preserve_strategy
                   else _campaign_row(ctx, campaign_id))
            if row is None:
                raise ToolError("NOT_FOUND", f"campaign {campaign_id} was not found")
            currency = extract_field(row, "customer.currency_code") or "USD"
            operations: list[dict] = []
            executes: list = []
            rechecks: list = []
            summaries: list[str] = []
            target_strategy = strategy
            if preserve_strategy:
                identity = _campaign_strategy_identity(ctx, campaign_id, row)
                target_strategy = identity[1]
                rechecks.append(
                    lambda c, cid=str(campaign_id), expected=identity:
                        _check_campaign_strategy(c, cid, expected)
                )

            if daily_budget is not None:
                budget_id = int(row.campaign_budget.id)
                current_budget = int(row.campaign_budget.amount_micros) / MICROS
                guardrails.check_budget(ctx, daily_budget, current=current_budget)
                rechecks.append(
                    lambda c, v=float(daily_budget), cid=str(campaign_id), bid=budget_id:
                        _check_campaign_budget(c, cid, bid, v)
                )
                old = money(row.campaign_budget.amount_micros, currency)
                new = money(int(float(daily_budget) * MICROS), currency)
                operations.append(
                    {
                        "type": "update_budget",
                        "resource": _resource(ctx, "campaignBudgets", row.campaign_budget.id),
                        "update_mask": ["amount_micros"],
                        "changes": {"daily_budget": {"old": old, "new": new}},
                    }
                )
                executes.append(
                    executors.update_budget(
                        budget_id=budget_id,
                        amount_micros=int(float(daily_budget) * MICROS),
                    )
                )
                summaries.append(f"set daily budget {old} -> {new}")

            campaign_sets: list = []
            campaign_masks: list[str] = []
            campaign_changes: dict = {}
            if strategy is not None:
                field = executors.CAMPAIGN_STRATEGY_FIELDS[strategy]
                campaign_sets.append((field, {}))
                # Leaf masks clear every prior target, including when switching
                # between the two maximize strategies. No empty parent mask.
                campaign_masks.extend(executors.CAMPAIGN_TARGET_FIELDS)
                campaign_masks.extend(executors.strategy_mask_paths(field))
                campaign_changes["bidding_strategy"] = {"old": None, "new": strategy}
                for target in executors.CAMPAIGN_TARGET_FIELDS:
                    campaign_changes[target] = {
                        "old": extract_field(row, f"campaign.{target}"), "new": None,
                    }
                summaries.append(f"switch bidding strategy to {strategy}; clear previous targets")
            if name is not None:
                campaign_sets.append(("name", str(name)))
                campaign_masks.append("name")
                campaign_changes["name"] = {"old": row.campaign.name, "new": str(name)}
                summaries.append(f"rename to {name!r}")
            if status is not None:
                status_name = str(status).strip().upper()
                if status_name not in ("ENABLED", "PAUSED"):
                    raise ToolError(
                        "INVALID_STATUS",
                        f"status must be ENABLED or PAUSED, got {status!r}",
                    )
                campaign_sets.append(("status", status_name))
                campaign_masks.append("status")
                campaign_changes["status"] = {
                    "old": row.campaign.status.name, "new": status_name,
                }
                summaries.append(f"set status {status_name}")
            for target, value, clear in target_edits:
                if value is None and not clear:
                    continue
                target_path = _campaign_target_path(target_strategy, target, clear)
                label = "tCPA" if target == "target_cpa" else "tROAS"
                new_value = None
                if clear:
                    summaries.append(f"clear {label} (explicit unset, leaf mask)")
                else:
                    new_value = (int(float(value) * MICROS) if target == "target_cpa"
                                 else float(value))
                    campaign_sets.append((target_path, new_value))
                    display = (money(new_value, currency) if target == "target_cpa"
                               else f"{new_value:g}")
                    summaries.append(f"set {label} {display}")
                campaign_masks.append(target_path)
                campaign_changes[target_path] = {
                    "old": extract_field(row, f"campaign.{target_path}"),
                    "new": new_value,
                }

            if campaign_masks:
                campaign_masks = list(dict.fromkeys(campaign_masks))
                operations.append(
                    {
                        "type": "update_campaign",
                        "resource": _resource(ctx, "campaigns", campaign_id),
                        "update_mask": campaign_masks,
                        "changes": campaign_changes,
                    }
                )
                executes.append(
                    executors.update_campaign(
                        campaign_id=str(campaign_id),
                        set_fields=list(campaign_sets),
                        mask_paths=list(campaign_masks),
                    )
                )

            if geos or languages:
                campaign_resource = _resource(ctx, "campaigns", campaign_id)
                for geo in geos:
                    operations.append({"type": "create_campaign_criterion",
                                       "campaign": campaign_resource,
                                       "location": {"geo_target_constant": f"geoTargetConstants/{geo}"}})
                for language in languages:
                    operations.append({"type": "create_campaign_criterion",
                                       "campaign": campaign_resource,
                                       "language": {"language_constant": f"languageConstants/{language}"}})
                executes.append(executors.add_campaign_targets(
                    campaign_id=str(campaign_id), geo_target_ids=geos,
                    language_ids=languages,
                ))
                summaries.append("add geographic and language criteria")

            if not operations:
                raise ToolError(
                    "NO_CHANGES",
                    "update_campaign was called with nothing to change",
                )

            def execute(c):
                for fn in executes:
                    fn(c)

            return _plan_payload(
                ctx,
                tool="update_campaign",
                summary=f"Campaign {campaign_id} ({row.campaign.name}): "
                        + "; ".join(summaries),
                operations=operations,
                execute=execute,
                rechecks=rechecks,
            )

        return _guarded_mutation(ctx, "update_campaign", impl)(
            campaign_id=campaign_id
        )

    # -- pause / enable / remove --------------------------------------------

    def _status_tool(tool_name, status_name, verb):
        effect = (
            "Enabling may affect spend under the existing campaign budget. "
            if status_name == "ENABLED"
            else "Spend-neutral. "
        )
        @server.tool(
            name=tool_name,
            description=_spec(
                tool_name,
                f"Stage a plan to {verb} a campaign, PMax asset group, ad group, ad, or "
                f"keyword. {effect}Applies only via confirm_and_apply. "
                "entity_id is a single numeric ID for campaign/ad_group/asset_group; "
                "ad requires ad_group_id~ad_id and keyword requires "
                "ad_group_id~criterion_id. Surrounding whitespace and "
                "leading zeroes in each segment are normalized. Asset-group "
                "plans show actual status and the verified PMax parent, and "
                "refuse changed state with STALE_PLAN before applying. "
                "Asset-group enabling may resume delivery and spend under "
                "the existing campaign budget.",
            ),
        )
        def _tool(entity_type: StatusEntityKind, entity_id: str, customer_id: str | None = None) -> dict:
            def impl(entity_id, **_kw):
                _check_customer(ctx, customer_id)
                if entity_type.strip().lower() == "asset_group":
                    from ads_mcp import pmax

                    return _plan_payload(ctx, **pmax.status_plan(
                        ctx, tool=tool_name, asset_group_id=entity_id,
                        status_name=status_name,
                    ))
                kind = entity_kind(entity_type)
                entity_id = _lifecycle_id(entity_id, kind)
                path = _ENTITY_SERVICES[kind][4]
                return _plan_payload(
                    ctx,
                    tool=tool_name,
                    summary=f"{verb.capitalize()} {kind} {entity_id}",
                    operations=[
                        {
                            "type": tool_name,
                            "resource": _resource(ctx, path, entity_id),
                            "update_mask": ["status"],
                            "changes": {"status": {"old": None, "new": status_name}},
                        }
                    ],
                    execute=executors.set_entity_status(
                        entity_type=kind, entity_id=str(entity_id),
                        status_name=status_name,
                    ),
                )

            return _guarded_mutation(ctx, tool_name, impl)(
                entity_id=entity_id
            )

        return _tool

    _status_tool("pause_entity", "PAUSED", "pause")
    _status_tool("enable_entity", "ENABLED", "enable")

    @server.tool(
        name="remove_entity",
        description=_spec(
            "remove_entity",
            "Stage the IRREVERSIBLE removal of a campaign, ad group, ad, or "
            "keyword. Prefer pause_entity when temporary. entity_id is a "
            "single numeric ID for campaign/ad_group; ad requires "
            "ad_group_id~ad_id and keyword requires ad_group_id~criterion_id. "
            "Surrounding whitespace and leading zeroes in each segment "
            "are normalized.",
        ),
    )
    def remove_entity(entity_type: str, entity_id: str, customer_id: str | None = None) -> dict:
        def impl(entity_id, **_kw):
            _check_customer(ctx, customer_id)
            kind = entity_kind(entity_type)
            entity_id = _lifecycle_id(entity_id, kind)
            path = _ENTITY_SERVICES[kind][4]
            return _plan_payload(
                ctx,
                tool="remove_entity",
                summary=f"IRREVERSIBLE: remove {kind} {entity_id} permanently",
                operations=[
                    {
                        "type": "remove",
                        "resource": _resource(ctx, path, entity_id),
                    }
                ],
                execute=executors.remove_entities(
                    entity_type=kind,
                    resource_names=[_resource(ctx, path, entity_id)],
                ),
                irreversible=True,
            )

        return _guarded_mutation(ctx, "remove_entity", impl)(
            entity_id=entity_id
        )

    # -- campaign creation ---------------------------------------------------

    def _creation_tool(tool_name):
        def build(args: dict, customer_id: str | None = None) -> dict:
            budget = args.get("daily_budget")
            _require(args.get("campaign_name"), "campaign_name")
            guardrails.check_budget(ctx, budget)
            keywords = args.get("keywords", [])
            _check_keyword_bids(ctx, keywords)
            rechecks = [
                lambda c, v=float(budget): guardrails.check_budget(c, v),
                lambda c, kws=keywords: _check_keyword_bids(c, kws),
            ]
            operations = [
                {
                    "type": tool_name,
                    "campaign_name": args["campaign_name"],
                    "changes": {
                        "daily_budget": {
                            "old": None,
                            "new": money(int(float(budget) * MICROS), "account currency"),
                        }
                    },
                    "details": {
                        k: v for k, v in args.items()
                        if k not in ("campaign_name", "daily_budget")
                    },
                }
            ]
            if tool_name == "create_pmax_campaign":
                roles = {name: args[name] for name in PMAX_IMAGE_ROLES}
                _verify_pmax_images(ctx, roles)
                rechecks.append(lambda c: _verify_pmax_images(c, roles))
                execute = executors.create_pmax(
                    name=args["campaign_name"],
                    daily_budget=budget,
                    bidding_strategy=args.get("bidding_strategy", "MAXIMIZE_CONVERSION_VALUE"),
                    final_urls=args.get("final_urls", []),
                    headlines=args.get("headlines", []),
                    long_headlines=args.get("long_headlines", []),
                    descriptions=args.get("descriptions", []),
                    business_name=args.get("business_name", ""),
                    geo_target_ids=args.get("geo_target_ids", []),
                    start_paused=args["start_paused"],
                    contains_eu_political_advertising=args["contains_eu_political_advertising"],
                    **roles,
                )
            else:
                execute = executors.create_campaign(
                    name=args["campaign_name"],
                    daily_budget=budget,
                    bidding_strategy=args.get("bidding_strategy", "MAXIMIZE_CONVERSIONS"),
                    channel_type=args["channel_type"],
                    geo_target_ids=args.get("geo_target_ids", []),
                    language_ids=args.get("language_ids", []),
                    ad_group_name=args.get("ad_group_name"),
                    keywords=keywords,
                    target_cpa=args["target_cpa"],
                    target_roas=args["target_roas"],
                    status=args["status"],
                    contains_eu_political_advertising=args["contains_eu_political_advertising"],
                    network_settings=args.get("network_settings"),
                )
            return _plan_payload(
                ctx,
                tool=tool_name,
                summary=(
                    f"Create campaign {args['campaign_name']!r} "
                    f"({args.get('status', 'PAUSED' if args.get('start_paused', True) else 'ENABLED')}) "
                    f"with daily budget "
                    f"{money(int(float(budget) * MICROS), 'account currency')}"
                    + ("; campaign shell with no asset group or serving creative; "
                       "use create_pmax_campaign for complete creation"
                       if args.get("channel_type") == "PERFORMANCE_MAX" else "")
                ),
                operations=operations,
                execute=execute,
                rechecks=rechecks,
            )

        return build

    draft_campaign_build = _creation_tool("draft_campaign")

    @server.tool(
        name="draft_campaign",
        description=_spec(
            "draft_campaign",
            "Stage a new campaign (status defaults to PAUSED): channel, name, budget, "
            "bidding strategy, geo and language targeting. Budget cap applies "
            "at plan time and again at apply. Optional ad_group_name and "
            "keywords ({text, match_type, optional cpc_bid_micros}) create "
            "an ad group with the campaign's status and enabled keywords under those parents; "
            "keyword bids use ADS_MCP_MAX_FIRST_BID at both gates. CPA/ROAS targets are uncapped. "
            "Creation supports only SEARCH, DISPLAY and PERFORMANCE_MAX; other channels "
            "need channel-specific settings this tool cannot supply. Creation status must be "
            "PAUSED or ENABLED; REMOVED is a lifecycle enum value, not a creation status. "
            "Requires an explicit contains_eu_political_advertising boolean. PERFORMANCE_MAX "
            "creates a campaign shell with no asset group or serving creative and rejects "
            "ad-group/keyword children; use create_pmax_campaign for complete non-retail "
            "creation. Provider/account eligibility and strategy compatibility still apply; "
            "a supported shell does not establish serving readiness. Search defaults to "
            "Google Search only; null network options use defaults. Search Partners "
            "requires Google Search. Restricted partner targeting is provider/account-dependent.",
        ),
    )
    def draft_campaign(
        campaign_name: str,
        daily_budget: float,
        bidding_strategy: str = "MAXIMIZE_CONVERSIONS",
        geo_target_ids: list[str] | None = None,
        language_ids: list[str] | None = None,
        customer_id: str | None = None,
        ad_group_name: str | None = None,
        keywords: list[dict] | None = None,
        channel_type: str = "SEARCH",
        target_cpa: float | None = None,
        target_roas: float | None = None,
        status: LifecycleStatus | None = "PAUSED",
        contains_eu_political_advertising: StrictBool | None = None,
        target_google_search: StrictBool | None = None,
        target_search_network: StrictBool | None = None,
        target_partner_search_network: StrictBool | None = None,
        target_content_network: StrictBool | None = None,
    ) -> dict:
        def impl(**_kw):
            from ads_mcp.campaign_networks import creation_settings

            _check_customer(ctx, customer_id)
            declaration = _political_declaration(contains_eu_political_advertising)
            channel = _enum_choice(channel_type, CHANNEL_TYPES, "channel_type")
            networks = creation_settings(
                channel, target_google_search=target_google_search,
                target_search_network=target_search_network,
                target_partner_search_network=target_partner_search_network,
                target_content_network=target_content_network,
            )
            if channel not in DRAFT_CAMPAIGN_CHANNELS:
                raise ToolError(
                    "UNSUPPORTED_CREATION_CHANNEL",
                    f"draft_campaign cannot supply the channel-specific prerequisites for "
                    f"{channel}; choose SEARCH, DISPLAY or a PERFORMANCE_MAX shell, or "
                    "configure this channel through Google Ads with its required settings",
                )
            status_name = _creation_status(status)
            if channel == "PERFORMANCE_MAX" and (ad_group_name is not None or keywords is not None):
                raise ToolError("CONTRADICTORY_ARGUMENTS", "PERFORMANCE_MAX campaign shells do not support ad_group_name or keywords; use create_pmax_campaign")
            if keywords:
                _require(ad_group_name, "ad_group_name")
            strategy = _enum_choice(bidding_strategy, CAMPAIGN_BIDDING_STRATEGIES,
                                    "bidding_strategy")
            _target_values(strategy, target_cpa, target_roas)
            return draft_campaign_build(
                {
                    "campaign_name": campaign_name,
                    "daily_budget": daily_budget,
                    "bidding_strategy": _enum_choice(
                        bidding_strategy, CAMPAIGN_BIDDING_STRATEGIES,
                        "bidding_strategy",
                    ),
                    "geo_target_ids": _target_ids(geo_target_ids, "geo_target_ids"),
                    "language_ids": _target_ids(language_ids, "language_ids"),
                    "ad_group_name": ad_group_name,
                    "keywords": _keywords(keywords or []),
                    "channel_type": channel,
                    **({"network_settings": networks} if networks is not None else {}),
                    "contains_eu_political_advertising": declaration,
                    "target_cpa": target_cpa,
                    "target_roas": target_roas,
                    "status": status_name,
                }
            )

        return _guarded_mutation(ctx, "draft_campaign", impl)()

    @server.tool(
        name="set_campaign_networks",
        description=_spec(
            "set_campaign_networks",
            "Stage exact network changes for an enabled or paused standard Search campaign. "
            "Supply at least one boolean; null omits a field. Search Partners requires "
            "Google Search. Restricted partner targeting is provider/account-dependent. "
            "Preview shows before/after values and only supplied network leaf masks. "
            "Complete campaign state is rechecked before confirm_and_apply. Enabling "
            "networks may change serving and spend; provider eligibility remains authoritative.",
        ),
    )
    def set_campaign_networks(
        campaign_id: StrictStr,
        target_google_search: StrictBool | None = None,
        target_search_network: StrictBool | None = None,
        target_partner_search_network: StrictBool | None = None,
        target_content_network: StrictBool | None = None,
        customer_id: str | None = None,
    ) -> dict:
        def impl(**_kw):
            from ads_mcp.campaign_networks import network_plan

            _check_customer(ctx, customer_id)
            return _plan_payload(ctx, **network_plan(
                ctx, campaign_id=campaign_id, target_google_search=target_google_search,
                target_search_network=target_search_network,
                target_partner_search_network=target_partner_search_network,
                target_content_network=target_content_network,
            ))

        return _guarded_mutation(ctx, "set_campaign_networks", impl)()

    pmax_build = _creation_tool("create_pmax_campaign")

    @server.tool(
        name="create_pmax_campaign",
        description=_spec(
            "create_pmax_campaign",
            "Stage a new Performance Max campaign (start_paused defaults true) with its asset "
            "group in one atomic request: headlines 3-15 (30 counted characters, one <=15), "
            "long headlines 1-5 (90), descriptions 2-5 (90, one <=60), business name (25), "
            "final URLs and geo targeting. Unicode W/F characters count twice. Requires explicit "
            "contains_eu_political_advertising and existing landscape_image_asset_ids (1-20), "
            "square_image_asset_ids (1-20), logo_asset_ids (1-5). Images are verified in the "
            "configured account at staging and apply; brand guidelines are disabled. "
            "Budget cap applies at plan time and again at apply. This creates a complete "
            "initial non-retail graph; provider/account eligibility and policy checks still "
            "apply, and local validation does not establish serving readiness.",
        ),
    )
    def create_pmax_campaign(
        campaign_name: str,
        daily_budget: float,
        final_urls: list[str],
        headlines: list[str],
        long_headlines: list[str],
        descriptions: list[str],
        business_name: str,
        bidding_strategy: str = "MAXIMIZE_CONVERSION_VALUE",
        geo_target_ids: list[str] | None = None,
        customer_id: str | None = None,
        start_paused: bool = True,
        contains_eu_political_advertising: StrictBool | None = None,
        landscape_image_asset_ids: list[str] | None = None,
        square_image_asset_ids: list[str] | None = None,
        logo_asset_ids: list[str] | None = None,
    ) -> dict:
        def impl(**_kw):
            _check_customer(ctx, customer_id)
            declaration = _political_declaration(contains_eu_political_advertising)
            _pmax_creative(headlines, long_headlines, descriptions, business_name, final_urls)
            roles = _pmax_image_ids(
                landscape_image_asset_ids=landscape_image_asset_ids,
                square_image_asset_ids=square_image_asset_ids,
                logo_asset_ids=logo_asset_ids,
            )
            return pmax_build(
                {
                    "campaign_name": campaign_name,
                    "daily_budget": daily_budget,
                    "bidding_strategy": _enum_choice(
                        bidding_strategy, {"MAXIMIZE_CONVERSIONS", "MAXIMIZE_CONVERSION_VALUE"},
                        "bidding_strategy",
                    ),
                    "final_urls": final_urls,
                    "headlines": headlines,
                    "long_headlines": long_headlines,
                    "descriptions": descriptions,
                    "business_name": business_name,
                    "geo_target_ids": _target_ids(geo_target_ids, "geo_target_ids"),
                    "start_paused": start_paused,
                    "contains_eu_political_advertising": declaration,
                    **roles,
                }
            )

        return _guarded_mutation(ctx, "create_pmax_campaign", impl)()

    # -- ad groups -----------------------------------------------------------

    @server.tool(
        name="create_ad_group",
        description=_spec(
            "create_ad_group",
            "Stage a new ad group in a campaign (status defaults to PAUSED). Optional cpc_bid_micros "
            "uses ADS_MCP_MAX_FIRST_BID at staging and application. Creation status must be "
            "PAUSED or ENABLED; the lifecycle enum value REMOVED is refused for creation.",
        ),
    )
    def create_ad_group(
        campaign_id: str, ad_group_name: str, customer_id: str | None = None,
        cpc_bid_micros: int | None = None,
        status: LifecycleStatus | None = "PAUSED",
    ) -> dict:
        def impl(campaign_id, **_kw):
            _check_customer(ctx, customer_id)
            campaign_id = _numeric_id(
                _require(campaign_id, "campaign_id"), "campaign_id"
            )
            _require(ad_group_name, "ad_group_name")
            status_name = _creation_status(status)
            changes = {"name": {"old": None, "new": ad_group_name},
                       "status": {"old": None, "new": status_name}}
            rechecks = []
            if cpc_bid_micros is not None:
                bid = Decimal(cpc_bid_micros) / MICROS
                guardrails.check_first_bid(ctx, bid, baseline_verified=True)
                changes["cpc_bid_micros"] = {"old": None, "new": cpc_bid_micros}
                rechecks.append(
                    lambda c, new=bid: guardrails.check_first_bid(
                        c, new, baseline_verified=True
                    )
                )
            return _plan_payload(
                ctx,
                tool="create_ad_group",
                summary=f"Create ad group {ad_group_name!r} in campaign {campaign_id}",
                operations=[
                    {
                        "type": "create_ad_group",
                        "campaign": _resource(ctx, "campaigns", campaign_id),
                        "changes": changes,
                    }
                ],
                execute=executors.create_ad_group(
                    campaign_id=str(campaign_id), name=ad_group_name,
                    cpc_bid_micros=cpc_bid_micros,
                    status=status_name,
                ),
                rechecks=rechecks,
            )

        return _guarded_mutation(ctx, "create_ad_group", impl)(
            campaign_id=campaign_id
        )

    @server.tool(
        name="update_ad_group",
        description=_spec(
            "update_ad_group",
            "Stage ad-group changes: name, status, ad rotation mode, or default CPC bid "
            "(bid increases are checked against the ACCOUNT's current bid and "
            "the configured cap, or ADS_MCP_MAX_FIRST_BID for a verified zero "
            "bid). Application reads the account baseline again.",
        ),
    )
    def update_ad_group(
        ad_group_id: str,
        name: str | None = None,
        status: str | None = None,
        cpc_bid_micros: int | None = None,
        customer_id: str | None = None,
        ad_rotation_mode: AdRotationMode | None = None,
    ) -> dict:
        def impl(ad_group_id, **_kw):
            _check_customer(ctx, customer_id)
            ad_group_id = _numeric_id(
                _require(ad_group_id, "ad_group_id"), "ad_group_id"
            )
            if cpc_bid_micros is not None:
                guardrails.bid_value(Decimal(cpc_bid_micros) / MICROS)
                row = _ad_group_bid_row(ctx, ad_group_id)
            else:
                row = _first_row(
                    ctx,
                    "SELECT ad_group.id, ad_group.name, ad_group.cpc_bid_micros "
                    f"FROM ad_group WHERE ad_group.id = {_numeric_id(ad_group_id, 'ad_group_id')}",
                    matcher=lambda r: str(r.ad_group.id) == str(ad_group_id),
                )
            if row is None:
                raise ToolError("NOT_FOUND", f"ad group {ad_group_id} was not found")
            masks, changes, sets = [], {}, []
            if ad_rotation_mode is not None:
                rotation = _enum_choice(ad_rotation_mode, ROTATION_MODES, "ad_rotation_mode")
                masks.append("ad_rotation_mode")
                changes["ad_rotation_mode"] = {"old": None, "new": rotation}
                sets.append(("ad_rotation_mode", rotation))
            if name is not None:
                masks.append("name")
                changes["name"] = {"old": row.ad_group.name, "new": str(name)}
                sets.append(("name", str(name)))
            if status is not None:
                status_name = str(status).strip().upper()
                if status_name not in ("ENABLED", "PAUSED"):
                    raise ToolError("INVALID_STATUS", f"status must be ENABLED or PAUSED, got {status!r}")
                masks.append("status")
                changes["status"] = {"old": None, "new": status_name}
                sets.append(("status", status_name))
            if cpc_bid_micros is not None:
                current = int(row.ad_group.cpc_bid_micros)
                guardrails.check_bid(
                    ctx, None, Decimal(cpc_bid_micros) / MICROS,
                    account_current=Decimal(current) / MICROS,
                )
                masks.append("cpc_bid_micros")
                changes["cpc_bid_micros"] = {"old": current, "new": int(cpc_bid_micros)}
                sets.append(("cpc_bid_micros", int(cpc_bid_micros)))
            if not masks:
                raise ToolError("NO_CHANGES", "update_ad_group was called with nothing to change")
            return _plan_payload(
                ctx,
                tool="update_ad_group",
                summary=f"Ad group {ad_group_id}: update {', '.join(masks)}",
                operations=[
                    {
                        "type": "update_ad_group",
                        "resource": _resource(ctx, "adGroups", ad_group_id),
                        "update_mask": masks,
                        "changes": changes,
                    }
                ],
                execute=executors.update_ad_group(
                    ad_group_id=str(ad_group_id), set_fields=list(sets),
                    mask_paths=list(masks),
                ),
                rechecks=(
                    [lambda c, gid=str(ad_group_id), new=Decimal(cpc_bid_micros) / MICROS:
                        _check_account_bid(c, gid, new)]
                    if cpc_bid_micros is not None else []
                ),
            )

        return _guarded_mutation(ctx, "update_ad_group", impl)(
            ad_group_id=ad_group_id
        )

    # -- ads and keywords ----------------------------------------------------

    @server.tool(
        name="draft_responsive_search_ad",
        description=_spec(
            "draft_responsive_search_ad",
            "Stage a responsive search ad (3-15 headlines <=30 chars, 2-4 "
            "descriptions <=90 chars), optional path1/path2 (<=15 chars each), "
            "and status (defaults PAUSED) — limits validated before any API call. "
            "Creation status must be PAUSED or ENABLED; the lifecycle enum value REMOVED "
            "is refused for creation. final_url must be an HTTP or HTTPS URL string "
            "with a host, without credentials or whitespace. Local validation does not "
            "fetch the destination or guarantee provider acceptance.",
        ),
    )
    def draft_responsive_search_ad(
        ad_group_id: str,
        headlines: list[str],
        descriptions: list[str],
        final_url: str,
        customer_id: str | None = None,
        path1: str | None = None,
        path2: str | None = None,
        status: LifecycleStatus | None = "PAUSED",
    ) -> dict:
        def impl(ad_group_id, **_kw):
            _check_customer(ctx, customer_id)
            ad_group_id = _numeric_id(
                _require(ad_group_id, "ad_group_id"), "ad_group_id"
            )
            _destination_url(final_url)
            status_name = _creation_status(status)
            for path, label in ((path1, "path1"), (path2, "path2")):
                if path is not None:
                    _text_limit(path, 15, label)
            if not RSA_MIN_HEADLINES <= len(headlines) <= 15:
                raise ToolError(
                    "TEXT_COUNT_INVALID",
                    f"a responsive search ad needs {RSA_MIN_HEADLINES}-15 "
                    f"headlines; got {len(headlines)}",
                )
            if not RSA_MIN_DESCRIPTIONS <= len(descriptions) <= 4:
                raise ToolError(
                    "TEXT_COUNT_INVALID",
                    f"a responsive search ad needs {RSA_MIN_DESCRIPTIONS}-4 "
                    f"descriptions; got {len(descriptions)}",
                )
            for h in headlines:
                _text_limit(h, HEADLINE_MAX, "headline", nonblank=True)
            for d in descriptions:
                _text_limit(d, DESCRIPTION_MAX, "description", nonblank=True)
            return _plan_payload(
                ctx,
                tool="draft_responsive_search_ad",
                summary=f"Create RSA in ad group {ad_group_id} ({len(headlines)} "
                        f"headlines, {len(descriptions)} descriptions)",
                operations=[
                    {
                        "type": "create_rsa",
                        "ad_group": _resource(ctx, "adGroups", ad_group_id),
                        "changes": {
                            "headlines": {"old": None, "new": headlines},
                            "descriptions": {"old": None, "new": descriptions},
                            "final_url": {"old": None, "new": final_url},
                            "path1": {"old": None, "new": path1},
                            "path2": {"old": None, "new": path2},
                            "status": {"old": None, "new": status_name},
                        },
                    }
                ],
                execute=executors.create_rsa(
                    ad_group_id=str(ad_group_id), headlines=list(headlines),
                    descriptions=list(descriptions), final_url=final_url,
                    path1=path1, path2=path2, status=status_name,
                ),
            )

        return _guarded_mutation(ctx, "draft_responsive_search_ad", impl)(
            ad_group_id=ad_group_id
        )

    @server.tool(
        name="draft_keywords",
        description=_spec(
            "draft_keywords",
            "Stage adding keywords ({text, match_type, optional cpc_bid_micros}) "
            "to an ad group. Every keyword requires an explicit match_type. "
            "Explicit bids use the existing parent's bid and "
            "percentage cap, or ADS_MCP_MAX_FIRST_BID if no bid applies. "
            "Application reads the parent bid again.",
        ),
    )
    def draft_keywords(ad_group_id: str, keywords: list[dict], customer_id: str | None = None) -> dict:
        def impl(ad_group_id, **_kw):
            _check_customer(ctx, customer_id)
            ad_group_id = _numeric_id(
                _require(ad_group_id, "ad_group_id"), "ad_group_id"
            )
            if not keywords:
                raise ToolError("MISSING_ARGUMENT", "keywords is empty")
            checked = _keywords(keywords)
            _check_keyword_bids(ctx, checked, ad_group_id)
            return _plan_payload(
                ctx,
                tool="draft_keywords",
                summary=f"Add {len(keywords)} keyword(s) to ad group {ad_group_id}",
                operations=[
                    {
                        "type": "create_keywords",
                        "ad_group": _resource(ctx, "adGroups", ad_group_id),
                        "changes": {"keywords": {"old": None, "new": checked}},
                    }
                ],
                execute=executors.create_keywords(
                    ad_group_id=str(ad_group_id), keywords=checked
                ),
                rechecks=[lambda c, kws=checked, gid=str(ad_group_id):
                          _check_keyword_bids(c, kws, gid)],
            )

        return _guarded_mutation(ctx, "draft_keywords", impl)(
            ad_group_id=ad_group_id
        )

    @server.tool(
        name="remove_keywords",
        description=_spec(
            "remove_keywords",
            "Stage the IRREVERSIBLE removal of keywords by criterion id.",
        ),
    )
    def remove_keywords(ad_group_id: str, criterion_ids: list[str], customer_id: str | None = None) -> dict:
        def impl(ad_group_id, criterion_ids, **_kw):
            _check_customer(ctx, customer_id)
            ad_group_id = _numeric_id(
                _require(ad_group_id, "ad_group_id"), "ad_group_id"
            )
            criterion_ids = _target_ids(criterion_ids, "criterion_ids")
            if not criterion_ids:
                raise ToolError("MISSING_ARGUMENT", "criterion_ids is empty")
            return _plan_payload(
                ctx,
                tool="remove_keywords",
                summary=f"IRREVERSIBLE: remove {len(criterion_ids)} keyword(s) "
                        f"from ad group {ad_group_id}",
                operations=[
                    {
                        "type": "remove",
                        "resource": _resource(ctx, "adGroupCriteria", ad_group_id, cid),
                    }
                    for cid in criterion_ids
                ],
                execute=executors.remove_entities(
                    entity_type="keyword",
                    resource_names=[
                        _resource(ctx, "adGroupCriteria", ad_group_id, cid)
                        for cid in criterion_ids
                    ],
                ),
                irreversible=True,
            )

        return _guarded_mutation(ctx, "remove_keywords", impl)(
            ad_group_id=ad_group_id, criterion_ids=criterion_ids
        )

    @server.tool(
        name="update_keyword_bid",
        description=_spec(
            "update_keyword_bid",
            "Stage a keyword CPC bid change using its account bid or inherited "
            "ad-group bid. current_bid is ignored compatibility input. "
            "A verified zero baseline uses ADS_MCP_MAX_FIRST_BID; otherwise "
            "increases use ADS_MCP_MAX_BID_INCREASE_PCT. Application reads "
            "the baseline again; verified cuts need no configured bid cap.",
        ),
    )
    def update_keyword_bid(
        ad_group_id: str, criterion_id: str, current_bid: float, new_bid: float,
        customer_id: str | None = None,
    ) -> dict:
        def impl(ad_group_id, criterion_id, **_kw):
            _check_customer(ctx, customer_id)
            ad_group_id = _numeric_id(
                _require(ad_group_id, "ad_group_id"), "ad_group_id"
            )
            criterion_id = _numeric_id(
                _require(criterion_id, "criterion_id"), "criterion_id"
            )
            new_value = guardrails.bid_value(new_bid)
            account_bid = _check_account_bid(
                ctx, ad_group_id, new_value, criterion_id
            )
            # The operator approves against the ACCOUNT's value, not the
            # caller's claim: showing "0.05 -> 0.06" for what is really a cut
            # from $1.20 is an approval given on a false premise.
            old_micros = int(account_bid * MICROS) if account_bid > 0 else None
            return _plan_payload(
                ctx,
                tool="update_keyword_bid",
                summary=(
                    f"Keyword {criterion_id}: bid {account_bid:g} -> {new_bid:g}"
                    if account_bid > 0
                    else f"Keyword {criterion_id}: set bid to {new_bid:g} "
                         "(no explicit current bid on record)"
                ),
                operations=[
                    {
                        "type": "update_bid",
                        "resource": _resource(ctx, "adGroupCriteria", ad_group_id, criterion_id),
                        "update_mask": ["cpc_bid_micros"],
                        "changes": {
                            "cpc_bid_micros": {
                                "old": old_micros,
                                "new": int(new_value * MICROS),
                            }
                        },
                    }
                ],
                execute=executors.update_keyword_bid(
                    ad_group_id=str(ad_group_id), criterion_id=str(criterion_id),
                    new_bid_micros=int(new_value * MICROS),
                ),
                rechecks=[
                    lambda c, gid=str(ad_group_id), kid=str(criterion_id), new=new_value:
                        _check_account_bid(c, gid, new, kid)
                ],
            )

        return _guarded_mutation(ctx, "update_keyword_bid", impl)(
            ad_group_id=ad_group_id, criterion_id=criterion_id
        )

    @server.tool(
        name="add_negative_keywords",
        description=_spec(
            "add_negative_keywords",
            "Stage campaign-level negative keywords. match_type defaults EXACT; "
            "each keyword may be text or a {text, match_type} override.",
        ),
    )
    def add_negative_keywords(
        campaign_id: str, keywords: list[str | dict], customer_id: str | None = None,
        match_type: str = "EXACT",
    ) -> dict:
        def impl(campaign_id, **_kw):
            _check_customer(ctx, customer_id)
            campaign_id = _numeric_id(
                _require(campaign_id, "campaign_id"), "campaign_id"
            )
            if not keywords:
                raise ToolError("MISSING_ARGUMENT", "keywords is empty")
            default_match = _enum_choice(match_type, KEYWORD_MATCH_TYPES, "match_type")
            checked = _keywords([
                {"text": kw, "match_type": default_match} if isinstance(kw, str)
                else {"match_type": default_match, **kw}
                for kw in keywords
            ], negative=True)
            return _plan_payload(
                ctx,
                tool="add_negative_keywords",
                summary=f"Add {len(keywords)} negative keyword(s) to campaign {campaign_id}",
                operations=[
                    {
                        "type": "create_negative_keywords",
                        "campaign": _resource(ctx, "campaigns", campaign_id),
                        "changes": {"keywords": {"old": None, "new": checked}},
                    }
                ],
                execute=executors.add_negative_keywords(
                    campaign_id=str(campaign_id), keywords=checked
                ),
            )

        return _guarded_mutation(ctx, "add_negative_keywords", impl)(
            campaign_id=campaign_id
        )

    @server.tool(
        name="remove_negative_keywords",
        description=_spec(
            "remove_negative_keywords",
            "Stage the IRREVERSIBLE removal of campaign negative keywords.",
        ),
    )
    def remove_negative_keywords(campaign_id: str, criterion_ids: list[str], customer_id: str | None = None) -> dict:
        def impl(campaign_id, criterion_ids, **_kw):
            _check_customer(ctx, customer_id)
            campaign_id = _numeric_id(
                _require(campaign_id, "campaign_id"), "campaign_id"
            )
            criterion_ids = _target_ids(criterion_ids, "criterion_ids")
            if not criterion_ids:
                raise ToolError("MISSING_ARGUMENT", "criterion_ids is empty")
            return _plan_payload(
                ctx,
                tool="remove_negative_keywords",
                summary=f"IRREVERSIBLE: remove {len(criterion_ids)} negative "
                        f"keyword(s) from campaign {campaign_id}",
                operations=[
                    {
                        "type": "remove",
                        "resource": _resource(ctx, "campaignCriteria", campaign_id, cid),
                    }
                    for cid in criterion_ids
                ],
                execute=executors.remove_campaign_criteria(
                    resource_names=[
                        _resource(ctx, "campaignCriteria", campaign_id, cid)
                        for cid in criterion_ids
                    ],
                ),
                irreversible=True,
            )

        return _guarded_mutation(ctx, "remove_negative_keywords", impl)(
            campaign_id=campaign_id, criterion_ids=criterion_ids
        )

    # -- assets / extensions -------------------------------------------------

    @server.tool(
        name="draft_sitelinks",
        description=_spec(
            "draft_sitelinks",
            "Stage sitelink assets for a campaign (link text <=25 chars, "
            "validated client-side). Every final_url must be an HTTP or HTTPS URL "
            "string with a host, without credentials or whitespace. Local validation "
            "does not fetch destinations or guarantee provider acceptance.",
        ),
    )
    def draft_sitelinks(campaign_id: str, sitelinks: list[dict], customer_id: str | None = None) -> dict:
        def impl(campaign_id, **_kw):
            _check_customer(ctx, customer_id)
            campaign_id = _numeric_id(
                _require(campaign_id, "campaign_id"), "campaign_id"
            )
            if not sitelinks:
                raise ToolError("MISSING_ARGUMENT", "sitelinks is empty")
            for link in sitelinks:
                _supported_fields(
                    link, {"link_text", "final_url", "description1", "description2"},
                    "sitelink",
                )
                _require(link.get("link_text"), "link_text")
                _destination_url(link.get("final_url"))
                _text_limit(link["link_text"], SITELINK_TEXT_MAX, "sitelink link_text")
                if "description1" in link or "description2" in link:
                    for label in ("description1", "description2"):
                        description = _require(link.get(label), label)
                        _text_limit(description, 35, label)
            return _plan_payload(
                ctx,
                tool="draft_sitelinks",
                summary=f"Add {len(sitelinks)} sitelink(s) to campaign {campaign_id}",
                operations=[
                    {
                        "type": "create_sitelinks",
                        "campaign": _resource(ctx, "campaigns", campaign_id),
                        "changes": {"sitelinks": {"old": None, "new": sitelinks}},
                    }
                ],
                execute=executors.create_sitelinks(
                    campaign_id=str(campaign_id), sitelinks=list(sitelinks)
                ),
            )

        return _guarded_mutation(ctx, "draft_sitelinks", impl)(
            campaign_id=campaign_id
        )

    @server.tool(
        name="create_callouts",
        description=_spec(
            "create_callouts",
            "Stage callout assets (<=25 chars each, validated client-side).",
        ),
    )
    def create_callouts(campaign_id: str, callouts: list[str], customer_id: str | None = None) -> dict:
        def impl(campaign_id, **_kw):
            _check_customer(ctx, customer_id)
            campaign_id = _numeric_id(
                _require(campaign_id, "campaign_id"), "campaign_id"
            )
            if not callouts:
                raise ToolError("MISSING_ARGUMENT", "callouts is empty")
            for text in callouts:
                _text_limit(text, CALLOUT_MAX, "callout", nonblank=True)
            return _plan_payload(
                ctx,
                tool="create_callouts",
                summary=f"Add {len(callouts)} callout(s) to campaign {campaign_id}",
                operations=[
                    {
                        "type": "create_callouts",
                        "campaign": _resource(ctx, "campaigns", campaign_id),
                        "changes": {"callouts": {"old": None, "new": callouts}},
                    }
                ],
                execute=executors.create_callouts(
                    campaign_id=str(campaign_id), callouts=list(callouts)
                ),
            )

        return _guarded_mutation(ctx, "create_callouts", impl)(
            campaign_id=campaign_id
        )

    @server.tool(
        name="create_structured_snippets",
        description=_spec(
            "create_structured_snippets",
            "Stage structured snippet assets (header + values).",
        ),
    )
    def create_structured_snippets(campaign_id: str, header: str, values: list[str], customer_id: str | None = None) -> dict:
        def impl(campaign_id, **_kw):
            _check_customer(ctx, customer_id)
            campaign_id = _numeric_id(
                _require(campaign_id, "campaign_id"), "campaign_id"
            )
            _require(header, "header")
            if not 3 <= len(values) <= 10:
                raise ToolError("TEXT_COUNT_INVALID", "structured snippets require 3-10 values")
            for value in values:
                _require(value, "snippet value")
                _text_limit(value, 25, "snippet value")
            return _plan_payload(
                ctx,
                tool="create_structured_snippets",
                summary=f"Add structured snippets ({header}) to campaign {campaign_id}",
                operations=[
                    {
                        "type": "create_structured_snippets",
                        "campaign": _resource(ctx, "campaigns", campaign_id),
                        "changes": {header: {"old": None, "new": values}},
                    }
                ],
                execute=executors.create_structured_snippets(
                    campaign_id=str(campaign_id), header=header, values=list(values)
                ),
            )

        return _guarded_mutation(ctx, "create_structured_snippets", impl)(
            campaign_id=campaign_id
        )

    @server.tool(
        name="remove_extension",
        description=_spec(
            "remove_extension",
            "Stage the IRREVERSIBLE removal of a campaign extension/asset "
            "link (SITELINK, CALLOUT, STRUCTURED_SNIPPET).",
        ),
    )
    def remove_extension(campaign_id: str, asset_id: str, field_type: str, customer_id: str | None = None) -> dict:
        def impl(campaign_id, asset_id, **_kw):
            _check_customer(ctx, customer_id)
            campaign_id = _numeric_id(
                _require(campaign_id, "campaign_id"), "campaign_id"
            )
            asset_id = _numeric_id(_require(asset_id, "asset_id"), "asset_id")
            ftype = str(field_type or "").strip().upper()
            if ftype not in ("SITELINK", "CALLOUT", "STRUCTURED_SNIPPET"):
                raise ToolError(
                    "INVALID_FIELD_TYPE",
                    "field_type must be SITELINK, CALLOUT, or STRUCTURED_SNIPPET",
                )
            return _plan_payload(
                ctx,
                tool="remove_extension",
                summary=f"IRREVERSIBLE: remove {ftype} asset {asset_id} from "
                        f"campaign {campaign_id}",
                operations=[
                    {
                        "type": "remove",
                        "resource": (
                            f"customers/{ctx.config.customer_id}/campaignAssets/"
                            f"{_numeric_id(campaign_id, 'campaign_id')}~"
                            f"{_numeric_id(asset_id, 'asset_id')}~{ftype}"
                        ),
                    }
                ],
                execute=executors.remove_campaign_asset(
                    campaign_id=str(campaign_id), asset_id=str(asset_id),
                    field_type=ftype,
                ),
                irreversible=True,
            )

        return _guarded_mutation(ctx, "remove_extension", impl)(
            campaign_id=campaign_id, asset_id=asset_id
        )

    @server.tool(
        name="upload_image_asset",
        description=_spec(
            "upload_image_asset",
            "Stage an image asset upload (base64 payload validated).",
        ),
    )
    def upload_image_asset(asset_name: str, image_data_base64: str, customer_id: str | None = None) -> dict:
        def impl(**_kw):
            _check_customer(ctx, customer_id)
            _require(asset_name, "asset_name")
            try:
                raw = base64.b64decode(image_data_base64 or "", validate=True)
            except (binascii.Error, ValueError):
                raise ToolError("INVALID_IMAGE", "image_data_base64 is not valid base64") from None
            if not raw:
                raise ToolError("INVALID_IMAGE", "image payload is empty")
            return _plan_payload(
                ctx,
                tool="upload_image_asset",
                summary=f"Upload image asset {asset_name!r} ({len(raw)} bytes)",
                operations=[
                    {
                        "type": "upload_image_asset",
                        "changes": {"asset_name": {"old": None, "new": asset_name},
                                    "bytes": {"old": None, "new": len(raw)}},
                    }
                ],
                execute=executors.create_image_asset(
                    asset_name=asset_name, image_bytes=raw
                ),
            )

        return _guarded_mutation(ctx, "upload_image_asset", impl)()

    @server.tool(
        name="upload_text_asset",
        description=_spec(
            "upload_text_asset",
            "Stage a text asset upload.",
        ),
    )
    def upload_text_asset(asset_name: str, text_content: str, customer_id: str | None = None) -> dict:
        def impl(**_kw):
            _check_customer(ctx, customer_id)
            _require(asset_name, "asset_name")
            _require(text_content, "text_content")
            return _plan_payload(
                ctx,
                tool="upload_text_asset",
                summary=f"Upload text asset {asset_name!r}",
                operations=[
                    {
                        "type": "upload_text_asset",
                        "changes": {"text": {"old": None, "new": text_content}},
                    }
                ],
                execute=executors.create_text_asset(
                    asset_name=asset_name, text=text_content
                ),
            )

        return _guarded_mutation(ctx, "upload_text_asset", impl)()

    # -- audiences / geo / conversions / bidding / schedule ------------------

    @server.tool(
        name="create_custom_audience",
        description=_spec(
            "create_custom_audience",
            "Stage a custom audience (website visitors / interests / URLs).",
        ),
    )
    def create_custom_audience(
        audience_name: str, audience_type: str, urls_or_rules: list[str],
        customer_id: str | None = None,
    ) -> dict:
        def impl(**_kw):
            _check_customer(ctx, customer_id)
            _require(audience_name, "audience_name")
            atype = _enum_choice(audience_type, CUSTOM_AUDIENCE_TYPES, "audience_type")
            atype = CUSTOM_AUDIENCE_ALIASES.get(atype, atype)
            if not urls_or_rules:
                raise ToolError("MISSING_ARGUMENT", "urls_or_rules is empty")
            return _plan_payload(
                ctx,
                tool="create_custom_audience",
                summary=f"Create custom audience {audience_name!r} ({atype})",
                operations=[
                    {
                        "type": "create_custom_audience",
                        "changes": {"members": {"old": None, "new": urls_or_rules}},
                    }
                ],
                execute=executors.create_custom_audience(
                    name=audience_name, audience_type=atype,
                    members=list(urls_or_rules),
                ),
            )

        return _guarded_mutation(ctx, "create_custom_audience", impl)()

    @server.tool(
        name="add_audience_targeting",
        description=_spec(
            "add_audience_targeting",
            "Stage attaching an audience to a campaign (TARGETING or "
            "OBSERVATION mode).",
        ),
    )
    def add_audience_targeting(
        campaign_id: str, audience_id: str, targeting_mode: str = "OBSERVATION",
        customer_id: str | None = None,
    ) -> dict:
        def impl(campaign_id, audience_id, **_kw):
            _check_customer(ctx, customer_id)
            campaign_id = _numeric_id(
                _require(campaign_id, "campaign_id"), "campaign_id"
            )
            audience_id = _numeric_id(
                _require(audience_id, "audience_id"), "audience_id"
            )
            mode = str(targeting_mode).strip().upper()
            if mode not in ("TARGETING", "OBSERVATION"):
                raise ToolError(
                    "INVALID_TARGETING_MODE",
                    f"targeting_mode must be TARGETING or OBSERVATION, got {targeting_mode!r}",
                )
            return _plan_payload(
                ctx,
                tool="add_audience_targeting",
                summary=f"Attach audience {audience_id} to campaign {campaign_id} ({mode})",
                operations=[
                    {
                        "type": "add_audience_targeting",
                        "campaign": _resource(ctx, "campaigns", campaign_id),
                        "changes": {"audience": {"old": None, "new": audience_id},
                                    "mode": {"old": None, "new": mode}},
                    }
                ],
                execute=executors.add_audience_criterion(
                    campaign_id=str(campaign_id), audience_id=str(audience_id),
                    targeting_mode=mode,
                ),
            )

        return _guarded_mutation(ctx, "add_audience_targeting", impl)(
            campaign_id=campaign_id, audience_id=audience_id
        )

    def _geo_tool(tool_name, verb, irreversible=False):
        @server.tool(
            name=tool_name,
            description=_spec(
                tool_name,
                f"Stage {verb} of a geo target on a campaign."
                + (" IRREVERSIBLE." if irreversible else ""),
            ),
        )
        def _tool(campaign_id: str, geo_target_id: str, customer_id: str | None = None) -> dict:
            def impl(campaign_id, geo_target_id, **_kw):
                _check_customer(ctx, customer_id)
                campaign_id = _numeric_id(
                    _require(campaign_id, "campaign_id"), "campaign_id"
                )
                geo_target_id = _numeric_id(
                    _require(geo_target_id, "geo_target_id"), "geo_target_id"
                )
                prefix = "IRREVERSIBLE: " if irreversible else ""
                # The two tools do different things and their plans must say
                # so: removal targets an existing criterion by resource name;
                # exclusion CREATES a new negative criterion the API will name.
                if irreversible:
                    operations = [
                        {
                            "type": "remove",
                            "resource": _resource(
                                ctx, "campaignCriteria", campaign_id, geo_target_id
                            ),
                        }
                    ]
                else:
                    operations = [
                        {
                            "type": "create_negative_geo_criterion",
                            "campaign": _resource(ctx, "campaigns", campaign_id),
                            "changes": {
                                "excluded_geo_target": {
                                    "old": None,
                                    "new": f"geoTargetConstants/{_numeric_id(geo_target_id, 'geo_target_id')}",
                                }
                            },
                        }
                    ]
                return _plan_payload(
                    ctx,
                    tool=tool_name,
                    summary=f"{prefix}{verb.capitalize()} geo target "
                            f"{geo_target_id} on campaign {campaign_id}",
                    operations=operations,
                    execute=(
                        executors.remove_campaign_criteria(
                            resource_names=[
                                _resource(ctx, "campaignCriteria", campaign_id,
                                          geo_target_id)
                            ]
                        )
                        if irreversible
                        else executors.exclude_geo_criterion(
                            campaign_id=str(campaign_id),
                            geo_target_id=str(geo_target_id),
                        )
                    ),
                    irreversible=irreversible,
                )

            return _guarded_mutation(ctx, tool_name, impl)(
                campaign_id=campaign_id, geo_target_id=geo_target_id
            )

        return _tool

    _geo_tool("remove_geo_target", "removal", irreversible=True)
    _geo_tool("exclude_geo_target", "exclusion")

    @server.tool(
        name="create_conversion_action",
        description=_spec(
            "create_conversion_action",
            "Stage a new conversion action with counting_type (default ONE_PER_CLICK) "
            "and click_through_lookback_window_days (1-90, default 30).",
        ),
    )
    def create_conversion_action(
        name: str, category: str = "DEFAULT", customer_id: str | None = None,
        counting_type: str = "ONE_PER_CLICK",
        click_through_lookback_window_days: int = 30,
    ) -> dict:
        def impl(**_kw):
            _check_customer(ctx, customer_id)
            _require(name, "name")
            category_name = _enum_choice(category, CONVERSION_CATEGORIES, "category")
            counting = _enum_choice(counting_type, COUNTING_TYPES, "counting_type")
            if not 1 <= click_through_lookback_window_days <= 90:
                raise ToolError("INVALID_LOOKBACK_WINDOW", "click_through_lookback_window_days must be 1-90")
            return _plan_payload(
                ctx,
                tool="create_conversion_action",
                summary=f"Create conversion action {name!r} ({category_name})",
                operations=[
                    {
                        "type": "create_conversion_action",
                        "changes": {
                            "name": {"old": None, "new": name},
                            "category": {"old": None, "new": category_name},
                            "counting_type": {"old": None, "new": counting},
                            "click_through_lookback_window_days": {
                                "old": None, "new": click_through_lookback_window_days,
                            },
                        },
                    }
                ],
                execute=executors.create_conversion_action(
                    name=name, category=category_name, counting_type=counting,
                    click_through_lookback_window_days=click_through_lookback_window_days,
                ),
            )

        return _guarded_mutation(ctx, "create_conversion_action", impl)()

    @server.tool(
        name="set_conversion_action_primary_status",
        description=_spec(
            "set_conversion_action_primary_status",
            "Stage flipping a conversion action's primary-for-goal flag.",
        ),
    )
    def set_conversion_action_primary_status(
        conversion_action_id: str, primary: bool,
        customer_id: str | None = None,
    ) -> dict:
        def impl(conversion_action_id, **_kw):
            _check_customer(ctx, customer_id)
            conversion_action_id = _numeric_id(
                _require(conversion_action_id, "conversion_action_id"), "conversion_action_id"
            )
            return _plan_payload(
                ctx,
                tool="set_conversion_action_primary_status",
                summary=f"Set conversion action {conversion_action_id} "
                        f"primary_for_goal={bool(primary)}",
                operations=[
                    {
                        "type": "update_conversion_action",
                        "resource": _resource(ctx, "conversionActions", conversion_action_id),
                        "update_mask": ["primary_for_goal"],
                        "changes": {"primary_for_goal": {"old": None, "new": bool(primary)}},
                    }
                ],
                execute=executors.set_conversion_action_primary(
                    conversion_action_id=str(conversion_action_id),
                    primary=bool(primary),
                ),
            )

        return _guarded_mutation(ctx, "set_conversion_action_primary_status", impl)(
            conversion_action_id=conversion_action_id
        )

    @server.tool(
        name="create_portfolio_bidding_strategy",
        description=_spec(
            "create_portfolio_bidding_strategy",
            "Stage a portfolio bidding strategy (TARGET_CPA / TARGET_ROAS).",
        ),
    )
    def create_portfolio_bidding_strategy(
        name: str, strategy_type: str, target_cpa: float | None = None,
        target_roas: float | None = None,
        customer_id: str | None = None,
    ) -> dict:
        def impl(**_kw):
            _check_customer(ctx, customer_id)
            _require(name, "name")
            stype = str(strategy_type).strip().upper()
            if stype not in ("TARGET_CPA", "TARGET_ROAS"):
                raise ToolError(
                    "INVALID_STRATEGY_TYPE",
                    f"strategy_type must be TARGET_CPA or TARGET_ROAS, got {strategy_type!r}",
                )
            _target_values(stype, target_cpa, target_roas)
            changes = {"type": {"old": None, "new": stype}}
            if target_cpa is not None:
                changes["target_cpa"] = {
                    "old": None,
                    "new": money(int(float(target_cpa) * MICROS), "account currency"),
                }
            if target_roas is not None:
                changes["target_roas"] = {"old": None, "new": float(target_roas)}
            return _plan_payload(
                ctx,
                tool="create_portfolio_bidding_strategy",
                summary=f"Create portfolio bidding strategy {name!r} ({stype})",
                operations=[{"type": "create_bidding_strategy", "changes": changes}],
                execute=executors.create_bidding_strategy(
                    name=name, strategy_type=stype, target_cpa=target_cpa,
                    target_roas=target_roas,
                ),
            )

        return _guarded_mutation(ctx, "create_portfolio_bidding_strategy", impl)()

    @server.tool(
        name="set_campaign_schedule",
        description=_spec(
            "set_campaign_schedule",
            "Stage adding ad-schedule criteria (day/hour/minute windows) to a "
            "campaign. Adds windows and does not replace existing windows.",
        ),
    )
    def set_campaign_schedule(campaign_id: str, schedules: list[dict], customer_id: str | None = None) -> dict:
        def impl(campaign_id, **_kw):
            _check_customer(ctx, customer_id)
            campaign_id = _numeric_id(
                _require(campaign_id, "campaign_id"), "campaign_id"
            )
            if not schedules:
                raise ToolError("MISSING_ARGUMENT", "schedules is empty")
            for sched in schedules:
                _supported_fields(
                    sched,
                    {"day_of_week", "start_hour", "end_hour", "start_minute", "end_minute"},
                    "schedule",
                )
                if not sched.get("day_of_week"):
                    raise ToolError("MISSING_ARGUMENT", "schedule day_of_week is required")
                sched["day_of_week"] = _enum_choice(
                    sched["day_of_week"], DAYS_OF_WEEK, "day_of_week"
                )
                for bound in ("start_hour", "end_hour"):
                    hour = sched.get(bound, 0 if bound == "start_hour" else 24)
                    maximum = 23 if bound == "start_hour" else 24
                    if type(hour) is not int or not 0 <= hour <= maximum:
                        raise ToolError(
                            "INVALID_SCHEDULE", f"{bound} must be an integer from 0 to {maximum}"
                        )
                    sched[bound] = hour
                for bound in ("start_minute", "end_minute"):
                    minute = sched.get(bound, 0)
                    if type(minute) is not int or minute not in SCHEDULE_MINUTES:
                        raise ToolError(
                            "INVALID_SCHEDULE",
                            f"{bound} must be an integer in {sorted(SCHEDULE_MINUTES)}",
                        )
                    sched[bound] = minute
                if sched["end_hour"] == 24 and sched["end_minute"] != 0:
                    raise ToolError("INVALID_SCHEDULE", "24:00 is the only allowed 24-hour endpoint")
                start = sched["start_hour"] * 60 + sched["start_minute"]
                end = sched["end_hour"] * 60 + sched["end_minute"]
                if start >= end:
                    raise ToolError("INVALID_SCHEDULE", "schedule start must precede its end")
            return _plan_payload(
                ctx,
                tool="set_campaign_schedule",
                summary=f"Add {len(schedules)} ad-schedule window(s) to campaign "
                        f"{campaign_id}; does not replace existing windows",
                operations=[
                    {
                        "type": "set_campaign_schedule",
                        "campaign": _resource(ctx, "campaigns", campaign_id),
                        "changes": {"schedules": {"old": None, "new": schedules}},
                    }
                ],
                execute=executors.create_ad_schedules(
                    campaign_id=str(campaign_id), schedules=list(schedules)
                ),
            )

        return _guarded_mutation(ctx, "set_campaign_schedule", impl)(
            campaign_id=campaign_id
        )

    # -- recommendations -----------------------------------------------------

    @server.tool(
        name="apply_recommendation",
        description=_spec(
            "apply_recommendation",
            "Stage applying a Google recommendation (bare id or full resource "
            "name, normalized to one account-scoped resource). Budget "
            "recommendations are checked against the spend "
            "cap. Applies only via confirm_and_apply.",
        ),
    )
    def apply_recommendation(recommendation_id: str, customer_id: str | None = None) -> dict:
        def impl(**_kw):
            _check_customer(ctx, customer_id)
            resource = recommendation_resource_name(ctx, recommendation_id)
            amount = _check_recommendation_budget(ctx, resource)

            def recheck(c):
                # The applying server supplies the current cap. Check both
                # the approved figure and a fresh account read, since options
                # can change between staging and confirmation.
                if amount is not None:
                    guardrails.check_budget(c, amount)
                _check_recommendation_budget(c, resource)

            execute = executors.apply_recommendation(resource_name=resource)

            return _plan_payload(
                ctx,
                tool="apply_recommendation",
                summary=f"Apply recommendation {resource}",
                operations=[{"type": "apply_recommendation", "resource": resource}],
                execute=execute,
                rechecks=[recheck],
            )

        return _guarded_mutation(ctx, "apply_recommendation", impl)()

    @server.tool(
        name="dismiss_recommendation",
        description=_spec(
            "dismiss_recommendation",
            "Stage dismissing a Google recommendation (bare id or full "
            "resource name — both normalize).",
        ),
    )
    def dismiss_recommendation(recommendation_id: str, customer_id: str | None = None) -> dict:
        def impl(**_kw):
            _check_customer(ctx, customer_id)
            resource = recommendation_resource_name(ctx, recommendation_id)

            execute = executors.dismiss_recommendation(resource_name=resource)

            return _plan_payload(
                ctx,
                tool="dismiss_recommendation",
                summary=f"Dismiss recommendation {resource}",
                operations=[{"type": "dismiss_recommendation", "resource": resource}],
                execute=execute,
            )

        return _guarded_mutation(ctx, "dismiss_recommendation", impl)()

    # -- confirm_and_apply ---------------------------------------------------

    @server.tool(
        name="confirm_and_apply",
        description=_spec(
            "confirm_and_apply",
            "Execute a previously staged plan. dry_run=true (the default) "
            "previews without applying and does not consume the plan; "
            "dry_run=false requires a prior preview when "
            "ADS_MCP_REQUIRE_DRY_RUN is on (the default). Plans are "
            "single-use, expire after the TTL, and are bound to the customer "
            "they were staged for. An irreversible plan also requires "
            "confirm_irreversible=true at apply; otherwise it returns "
            "IRREVERSIBLE_CONFIRMATION_REQUIRED without consuming the plan. "
            "This acknowledgement does not bypass the dry-run requirement "
            "or any other guardrail.",
            kind=KIND_APPLY,
        ),
    )
    def confirm_and_apply(
        plan_id: str, dry_run: bool = True, customer_id: str | None = None,
        confirm_irreversible: bool = False,
    ) -> dict:
        def impl(**_kw):
            _check_customer(ctx, customer_id)
            if dry_run:
                entry = ctx.plan_store.get(plan_id)
                if entry.customer_id != ctx.config.customer_id:
                    raise ToolError(
                        "PLAN_CUSTOMER_MISMATCH",
                        f"plan {entry.id} was staged for customer "
                        f"{entry.customer_id}, not {ctx.config.customer_id}",
                    )
                if ctx.audit is not None:
                    ctx.audit.write(
                        {
                            "event": "dry_run",
                            "tool": entry.tool,
                            "customer_id": ctx.config.customer_id,
                            "outcome": "previewed",
                            "plan_id": entry.id,
                            "operations": entry.operations,
                        },
                        critical=True,
                    )
                ctx.plan_store.mark_previewed(plan_id)
                return {"applied": False, "plan": entry.payload()}

            def validate(entry):
                if entry.irreversible and not confirm_irreversible:
                    raise ToolError(
                        "IRREVERSIBLE_CONFIRMATION_REQUIRED",
                        "this plan is irreversible: review its preview and "
                        "set confirm_irreversible=true to acknowledge before "
                        "applying; the plan has not been consumed",
                    )
                for recheck in entry.rechecks:
                    recheck(ctx)

            # The context's application lock covers rechecks through the last
            # write and audit outcome, including across distinct plans. The
            # store separately burns each plan atomically before execution.
            entry = ctx.plan_store.claim(
                plan_id,
                customer_id=ctx.config.customer_id,
                require_previewed=ctx.config.require_dry_run,
                validate=validate,
            )
            ctx.clear_audit_loss()
            ctx.current_plan_id = entry.id
            # Step records should name the mutation the operator approved,
            # not the apply verb.
            applying_tool, ctx.current_tool = ctx.current_tool, entry.tool
            # Prove the audit log is writable BEFORE touching the account: a
            # change we cannot record is a change we do not make.
            if ctx.audit is not None:
                ctx.audit.write(
                    {
                        "event": "apply_started",
                        "tool": entry.tool,
                        "customer_id": ctx.config.customer_id,
                        "outcome": "starting",
                        "plan_id": entry.id,
                        "summary": entry.summary,
                    },
                    critical=True,
                )
            try:
                execution_result = entry.execute(ctx)
            except BaseException as exc:  # noqa: BLE001 — recorded, re-raised
                if ctx.audit is not None:
                    ctx.audit.write(
                        {
                            "event": "apply_failed",
                            "tool": entry.tool,
                            "customer_id": ctx.config.customer_id,
                            "outcome": type(exc).__name__,
                            "plan_id": entry.id,
                            "message": ctx.scrub(str(exc))[:500],
                            # A multi-step flow can fail after earlier steps
                            # landed; every step that reached the API has its
                            # own step_applied record above this one. Read
                            # them before assuming nothing changed.
                            "partial_changes_possible": True,
                        },
                        critical=False,
                    )
                if ctx.audit_loss:
                    raise ToolError(
                        "AUDIT_WRITE_FAILED",
                        "a step of this change reached the Google Ads API but "
                        "could not be written to the audit log, and a later "
                        f"step then failed ({type(exc).__name__}). The account "
                        "may hold a partial change with no record of it — "
                        "reconcile manually.",
                    ) from None
                raise
            ctx.current_tool = applying_tool
            result = {"applied": True, "plan": entry.payload()}
            experiment_action = entry.tool in {
                "create_pmax_url_experiment", "end_pmax_url_experiment", "promote_pmax_url_experiment",
            }
            if entry.tool == "create_pmax_campaign" or experiment_action:
                result.update(execution_result)
            if ctx.audit is not None:
                # Experiment receipt and readback have separate outcomes.
                wrote = ctx.audit.write(
                    {
                        "event": "applied" if result["applied"] else "submitted",
                        "tool": entry.tool,
                        "customer_id": ctx.config.customer_id,
                        "outcome": result.get("state", result.get("verification", "unknown")) if experiment_action else "success",
                        "plan_id": entry.id,
                        "summary": entry.summary,
                        "operations": entry.operations,
                    },
                    critical=False,
                )
                if not wrote or ctx.audit_loss:
                    # The change landed; we simply could not record it. Say so
                    # rather than returning a clean success the audit denies.
                    result["audit_warning"] = (
                        ("THE CHANGE WAS SUBMITTED but one or more audit records "
                         if experiment_action else "THE CHANGE WAS APPLIED but one or more audit records ") +
                        "could not be written to the configured audit log. "
                        "Reconcile this plan against the account manually."
                    )
                    if experiment_action:
                        result["applied"] = False
            return result

        call = _guarded_mutation(ctx, "confirm_and_apply", impl, plan_id=plan_id)
        if dry_run:
            return call()
        # Include error classification and refusal auditing in the interval.
        # The context manager releases it on every return or exception.
        with ctx.application_lock:
            return call()
