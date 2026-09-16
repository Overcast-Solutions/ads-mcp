"""Offline installed-archive smoke check. Run with python -I outside source."""

import asyncio
import importlib.metadata
import json
from pathlib import Path
import socket
import subprocess
import sys

import ads_mcp
import mcp
from ads_mcp.config import Config
from ads_mcp.server import create_server


EXPECTED_READS = frozenset({
    "discover_keywords", "get_account_info", "get_ad_performance",
    "get_asset_group_signals", "get_asset_groups", "get_campaign_performance",
    "get_change_history", "get_conversion_actions", "get_geo_performance",
    "get_keyword_forecasts", "get_keyword_performance", "get_keyword_urls",
    "get_listing_groups", "get_negative_keywords", "get_pmax_url_settings",
    "get_policy_issues", "get_product_status", "get_responsive_search_ad_urls",
    "get_search_terms", "get_shopping_performance", "health_check",
    "list_accounts", "list_audiences", "list_extensions", "list_recommendations",
    "run_gaql", "search_geo_targets",
    "list_shared_negative_keyword_lists", "get_shared_negative_keyword_list",
    "get_demographic_targeting",
})
EXPECTED_WRITE_MODE = EXPECTED_READS | frozenset({
    "add_asset_group_audience_signal", "add_asset_group_search_themes",
    "add_audience_targeting", "add_negative_keywords", "add_pmax_url_exclusion",
    "apply_recommendation", "confirm_and_apply", "create_ad_group",
    "create_callouts", "create_conversion_action", "create_custom_audience",
    "create_pmax_campaign", "create_portfolio_bidding_strategy",
    "create_structured_snippets", "dismiss_recommendation", "draft_campaign",
    "draft_keywords", "draft_responsive_search_ad", "draft_sitelinks",
    "enable_entity", "exclude_geo_target", "pause_entity",
    "remove_asset_group_signals", "remove_entity", "remove_extension",
    "remove_geo_target", "remove_keywords", "remove_negative_keywords",
    "remove_pmax_url_exclusions", "set_asset_group_product_selection",
    "set_campaign_schedule", "set_conversion_action_primary_status",
    "set_pmax_final_url_expansion", "update_ad_group", "update_campaign",
    "update_keyword_bid", "update_keyword_urls", "update_responsive_search_ad_urls",
    "upload_image_asset", "upload_text_asset",
    "create_shared_negative_keyword_list", "add_shared_negative_keywords",
    "remove_shared_negative_keywords", "attach_shared_negative_keyword_list",
    "detach_shared_negative_keyword_list",
    "update_demographic_targeting",
})


def no_network(*args, **kwargs):
    raise RuntimeError("Installed catalog checks must not contact a provider")


async def catalog(read_only):
    config = Config(
        developer_token="", customer_id="0000000000", login_customer_id=None,
        credentials_path="", token_path="", read_only=read_only,
        require_dry_run=True, max_daily_budget=None, max_bid_increase_pct=None,
        max_first_bid=None, audit_log=None, retry_base_seconds=1.0,
        plan_ttl_seconds=900, row_limit=1000,
    )
    async with mcp.Client(create_server(config, client=object())) as client:
        result = await client.list_tools()
    names = sorted(tool.name for tool in result.tools)
    expected = EXPECTED_READS if read_only else EXPECTED_WRITE_MODE
    assert len(names) == len(expected) and set(names) == expected
    return names


def main():
    module = Path(ads_mcp.__file__).resolve()
    assert Path(sys.prefix).resolve() in module.parents, "repository import fallback"
    distribution = importlib.metadata.distribution("ads-mcp")
    assert distribution.metadata["License-Expression"] == "Apache-2.0"
    for name, args in (("ads-mcp", ["--version"]),
                       ("ads-mcp", ["--help"]),
                       ("ads-mcp-generate-token", ["--help"]),
                       ("ads-mcp-export-source", ["--help"])):
        subprocess.run([str(Path(sys.executable).with_name(name)), *args], check=True)
    socket.create_connection = no_network
    socket.socket.connect = no_network
    read_names = asyncio.run(catalog(True))
    write_names = asyncio.run(catalog(False))
    print(json.dumps({"version": distribution.version,
                      "installed_module": str(module),
                      "read_catalog": read_names, "write_catalog": write_names}))


if __name__ == "__main__":
    main()
