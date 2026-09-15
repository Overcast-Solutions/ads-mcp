"""The locked tool catalog: names, kinds, and minimal valid arguments.

Single source for the exhaustive registry/catalog tests (F013), the
read-only registration tests (F014), and the contract-fixture drift guard
(F016). The project feature contract defines the accepted public operations.
"""

READ_TOOLS = frozenset(
    {
        "run_gaql",
        "health_check",
        "get_account_info",
        "list_accounts",
        "get_campaign_performance",
        "get_ad_performance",
        "get_keyword_performance",
        "get_search_terms",
        "get_geo_performance",
        "get_policy_issues",
        "list_recommendations",
        "get_change_history",
        "get_shopping_performance",
        "get_listing_groups",
        "get_product_status",
        "get_conversion_actions",
        "get_negative_keywords",
        "list_extensions",
        "search_geo_targets",
        "discover_keywords",
        "get_keyword_forecasts",
    }
)

MUTATION_TOOLS = frozenset(
    {
        "update_campaign",
        "pause_entity",
        "enable_entity",
        "remove_entity",
        "draft_campaign",
        "create_pmax_campaign",
        "create_ad_group",
        "update_ad_group",
        "draft_responsive_search_ad",
        "draft_keywords",
        "remove_keywords",
        "update_keyword_bid",
        "add_negative_keywords",
        "remove_negative_keywords",
        "draft_sitelinks",
        "create_callouts",
        "create_structured_snippets",
        "remove_extension",
        "upload_image_asset",
        "upload_text_asset",
        "create_custom_audience",
        "add_audience_targeting",
        "remove_geo_target",
        "exclude_geo_target",
        "create_conversion_action",
        "set_conversion_action_primary_status",
        "create_portfolio_bidding_strategy",
        "set_campaign_schedule",
        "apply_recommendation",
        "dismiss_recommendation",
    }
)

APPLY_TOOL = "confirm_and_apply"
ALL_WRITE_MODE_TOOLS = READ_TOOLS | MUTATION_TOOLS | {APPLY_TOOL}

# Plans for these tools must be flagged irreversible in their summary.
IRREVERSIBLE_TOOLS = frozenset(
    {
        "remove_entity",
        "remove_keywords",
        "remove_negative_keywords",
        "remove_extension",
        "remove_geo_target",
    }
)

# 1x1 transparent PNG.
TINY_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNgYGBg"
    "AAAABQABh6FO1AAAAABJRU5ErkJggg=="
)

# Minimal valid arguments for every mutation tool, used by the exhaustive
# registry walk (test_catalog.py). All spend values sit BELOW the walk's
# configured caps (daily budget 100, bid increase 100%) so every call must
# yield a plan, not a refusal. Entity ids reference the standard fixture
# account in harness.stub_standard_account.
MUTATION_ARGS = {
    "update_campaign": {"campaign_id": "111", "daily_budget": 45.0},
    "pause_entity": {"entity_type": "campaign", "entity_id": "111"},
    "enable_entity": {"entity_type": "campaign", "entity_id": "222"},
    "remove_entity": {"entity_type": "ad", "entity_id": "201~901"},
    "draft_campaign": {
        "contains_eu_political_advertising": False,
        "campaign_name": "Oracle Draft",
        "daily_budget": 10.0,
        "bidding_strategy": "MAXIMIZE_CONVERSIONS",
        "geo_target_ids": ["2840"],
        "language_ids": ["1000"],
    },
    "create_pmax_campaign": {
        "contains_eu_political_advertising": False,
        "landscape_image_asset_ids": ["801"],
        "square_image_asset_ids": ["802"],
        "logo_asset_ids": ["803"],
        "campaign_name": "Oracle PMax",
        "daily_budget": 10.0,
        "bidding_strategy": "MAXIMIZE_CONVERSION_VALUE",
        "final_urls": ["https://example.com"],
        "headlines": ["Buy widgets", "Great widgets", "Widgets now"],
        "long_headlines": ["Widgets that survive an autonomous agent"],
        "descriptions": ["Widgets for people", "Widgets for agents"],
        "business_name": "ACME",
        "geo_target_ids": ["2840"],
    },
    "create_ad_group": {"campaign_id": "222", "ad_group_name": "Oracle AG"},
    "update_ad_group": {"ad_group_id": "201", "name": "Renamed AG"},
    "draft_responsive_search_ad": {
        "ad_group_id": "201",
        "headlines": ["Buy widgets", "Great widgets", "Widgets now"],
        "descriptions": ["Widgets for people", "Widgets for agents"],
        "final_url": "https://example.com",
    },
    "draft_keywords": {
        "ad_group_id": "201",
        "keywords": [{"text": "acme widgets", "match_type": "EXACT"}],
    },
    "remove_keywords": {"ad_group_id": "201", "criterion_ids": ["401"]},
    "update_keyword_bid": {
        "ad_group_id": "201",
        "criterion_id": "401",
        "current_bid": 1.0,
        "new_bid": 1.2,
    },
    # Additional management operations are exercised through both staging
    # and application by test_guardrail_regression.
    "add_negative_keywords": {"campaign_id": "222", "keywords": ["free"]},
    "remove_negative_keywords": {"campaign_id": "222", "criterion_ids": ["444"]},
    "draft_sitelinks": {
        "campaign_id": "222",
        "sitelinks": [
            {
                "link_text": "Spring Sale",
                "final_url": "https://example.com/sale",
                "description1": "Big savings",
                "description2": "While stocks last",
            }
        ],
    },
    "create_callouts": {"campaign_id": "222", "callouts": ["Free shipping"]},
    "create_structured_snippets": {
        "campaign_id": "222",
        "header": "Brands",
        "values": ["ACME", "Example", "Sample"],
    },
    "remove_extension": {
        "campaign_id": "222",
        "asset_id": "777001",
        "field_type": "SITELINK",
    },
    "upload_image_asset": {
        "asset_name": "Oracle image",
        "image_data_base64": TINY_PNG_B64,
    },
    "upload_text_asset": {"asset_name": "Oracle text", "text_content": "Widgets"},
    "create_custom_audience": {
        "audience_name": "Oracle audience",
        "audience_type": "WEBSITE_VISITORS",
        "urls_or_rules": ["example.com"],
    },
    "add_audience_targeting": {
        "campaign_id": "222",
        "audience_id": "606",
        "targeting_mode": "OBSERVATION",
    },
    "remove_geo_target": {"campaign_id": "222", "geo_target_id": "2840"},
    "exclude_geo_target": {"campaign_id": "222", "geo_target_id": "2276"},
    "create_conversion_action": {"name": "Oracle Signup"},
    "set_conversion_action_primary_status": {
        "conversion_action_id": "555",
        "primary": False,
    },
    "create_portfolio_bidding_strategy": {
        "name": "Oracle tCPA",
        "strategy_type": "TARGET_CPA",
        "target_cpa": 5.0,
    },
    "set_campaign_schedule": {
        "campaign_id": "222",
        "schedules": [
            {
                "day_of_week": "MONDAY",
                "start_hour": 9,
                "start_minute": 0,
                "end_hour": 17,
                "end_minute": 0,
            }
        ],
    },
    "apply_recommendation": {"recommendation_id": "777"},
    "dismiss_recommendation": {"recommendation_id": "777"},
}

# The table above must cover the catalog exactly; a mismatch is a harness
# authoring error and must fail collection, not pass as a green test.
assert set(MUTATION_ARGS) == MUTATION_TOOLS, (
    sorted(set(MUTATION_ARGS) ^ MUTATION_TOOLS)
)

# Optional arguments per tool, layered on top of MUTATION_ARGS. F019 requires
# each argument to remain declared, validated and preserved in its plan.
OPTIONAL_ARGS = {
    "update_campaign": {"bidding_strategy": "MAXIMIZE_CONVERSIONS",
                        "geo_target_ids": ["2840"], "language_ids": ["1000"]},
    "draft_campaign": {"channel_type": "DISPLAY", "target_cpa": 5.0,
                       "ad_group_name": "Oracle AG", "status": "PAUSED"},
    "create_ad_group": {"cpc_bid_micros": 500000, "status": "PAUSED"},
    "update_ad_group": {"ad_rotation_mode": "OPTIMIZE"},
    "add_negative_keywords": {"match_type": "PHRASE"},
    "draft_responsive_search_ad": {"path1": "sale", "path2": "today",
                                   "status": "PAUSED"},
    "create_conversion_action": {"counting_type": "ONE_PER_CLICK",
                                 "click_through_lookback_window_days": 30},
    "create_pmax_campaign": {"start_paused": True},
}
assert set(OPTIONAL_ARGS) <= MUTATION_TOOLS, sorted(set(OPTIONAL_ARGS) - MUTATION_TOOLS)
