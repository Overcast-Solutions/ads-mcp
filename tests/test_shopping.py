"""F009 — Merchant Center / shopping visibility via the Ads API."""

import harness

WINDOW_ARGS = {"date_range_start": "2026-07-01", "date_range_end": "2026-07-31"}


def _stub_shopping(client):
    cur = {"customer": {"id": 9876543210, "currency_code": "USD"}}
    client.stub(
        "shopping_performance_view",
        [
            {
                "segments": {"product_item_id": "SKU123", "product_title": "ACME Widget Pro"},
                "campaign": {"id": 111},
                "metrics": {
                    "impressions": 500,
                    "clicks": 20,
                    "cost_micros": 12300000,
                    "conversions": 1.0,
                    "conversions_value": 88.0,
                },
                **cur,
            }
        ],
    )
    client.stub(
        "asset_group_listing_group_filter",
        [
            {
                "asset_group_listing_group_filter": {
                    "resource_name": "customers/9876543210/assetGroupListingGroupFilters/501~9001",
                    "id": 9001,
                    "type_": "SUBDIVISION",
                    "asset_group": "customers/9876543210/assetGroups/501",
                },
                "asset_group": {"id": 501, "name": "Retail all products"},
                "campaign": {"id": 111},
            },
            {
                "asset_group_listing_group_filter": {
                    "resource_name": "customers/9876543210/assetGroupListingGroupFilters/501~9002",
                    "id": 9002,
                    "type_": "UNIT_INCLUDED",
                    "parent_listing_group_filter": "customers/9876543210/assetGroupListingGroupFilters/501~9001",
                    "case_value": {"product_brand": {"value": "ACME"}},
                    "asset_group": "customers/9876543210/assetGroups/501",
                },
                "asset_group": {"id": 501, "name": "Retail all products"},
                "campaign": {"id": 111},
            },
        ],
    )
    client.stub(
        "shopping_product",
        [
            {"shopping_product": {"item_id": "SKU123", "title": "ACME Widget Pro", "status": "ELIGIBLE", "campaign": "customers/9876543210/campaigns/111", "merchant_center_id": 555111}},
            {"shopping_product": {"item_id": "SKU124", "title": "ACME Widget Mini", "status": "ELIGIBLE", "campaign": "customers/9876543210/campaigns/111", "merchant_center_id": 555111}},
            {"shopping_product": {"item_id": "SKU125", "title": "ACME Widget Max", "status": "NOT_ELIGIBLE", "campaign": "customers/9876543210/campaigns/111", "merchant_center_id": 555111}},
        ],
    )
    return client


def test_shopping_performance_product_level_metrics(tmp_path, account_client):
    _stub_shopping(account_client)
    server = harness.build_server(tmp_path, client=account_client)
    payload = harness.expect_ok(
        harness.call(
            server, "get_shopping_performance", {"campaign_id": "111", **WINDOW_ARGS}
        )
    )
    assert payload["campaign_id"] == "111"
    products = payload["products"]
    assert products, "no product rows from the recorded fixture"
    product = products[0]
    assert product["item_id"] == "SKU123"
    assert product["title"] == "ACME Widget Pro"
    harness.assert_money(product["cost"])
    assert isinstance(product["conversions"], float)


def test_listing_group_tree_per_asset_group(tmp_path, account_client):
    _stub_shopping(account_client)
    server = harness.build_server(tmp_path, client=account_client)
    payload = harness.expect_ok(
        harness.call(server, "get_listing_groups", {"campaign_id": "111"})
    )
    groups = payload["asset_groups"]
    assert groups and groups[0]["asset_group_id"] == "501"
    nodes = {n["filter_id"]: n for n in groups[0]["nodes"]}
    assert nodes["9001"]["parent_filter_id"] is None
    assert nodes["9001"]["type"] == "SUBDIVISION"
    assert nodes["9002"]["parent_filter_id"] == "9001", (
        "the filter tree must preserve parent links"
    )
    assert nodes["9002"]["dimension"] == {"product_brand": "ACME"}


def test_product_status_summarizes_feed_health(tmp_path, account_client):
    """A feed collapse must be observable from this server."""
    _stub_shopping(account_client)
    server = harness.build_server(tmp_path, client=account_client)
    payload = harness.expect_ok(
        harness.call(server, "get_product_status", {"campaign_id": "111"})
    )
    assert payload["merchant_id"] == "555111"
    assert payload["total_products"] == 3
    assert payload["status_counts"] == {"ELIGIBLE": 2, "NOT_ELIGIBLE": 1}


def test_campaign_without_feed_link_returns_named_result(tmp_path, account_client):
    _stub_shopping(account_client)
    server = harness.build_server(tmp_path, client=account_client)
    payload = harness.call(server, "get_product_status", {"campaign_id": "222"})
    assert "error" not in payload, (
        f"NOT_FEED_LINKED must be a named result, not an error: {payload.get('error')}"
    )
    assert payload["status"] == "NOT_FEED_LINKED"
    assert payload["campaign_id"] == "222"
