"""F033: multiple campaigns/merchants and both real SDK asset/tree families."""
import json
import re
from pathlib import Path

import pytest

import harness as h
from offline_contract import ProjectedClient, selected

PUBLIC = {f["name"]: f for f in json.loads((Path(__file__).parent / "fixtures/offline_repair_fields_v25.json").read_text())["fields"]}


def resource(campaign_id, customer=h.CUSTOMER_ID):
    return f"customers/{customer}/campaigns/{campaign_id}"


def test_product_status_uses_campaign_scope_and_linked_merchant(tmp_path):
    client = ProjectedClient()
    client.stub("campaign", [{"campaign": {"id": cid, "shopping_setting": {"merchant_id": merchant}}}
                               for cid, merchant in [(111, 555111), (222, 666222)]])
    data = [(111, 555111, "ELIGIBLE"), (111, 555111, "NOT_ELIGIBLE"), (222, 555111, "ELIGIBLE"),
            (111, 666222, "ELIGIBLE"), (222, 666222, "ELIGIBLE")]
    client.stub("shopping_product", [{"shopping_product": {"item_id": f"SKU{index}", "campaign": resource(cid),
        "merchant_center_id": merchant, "status": status}} for index, (cid, merchant, status) in enumerate(data)])
    def scope(rows, call):
        query = call.query
        campaign_match = re.search(r"shopping_product\.campaign\s*=\s*['\"]([^'\"]+)['\"]", query, re.I)
        merchant_match = re.search(r"shopping_product\.merchant_center_id\s*=\s*['\"]?(\d+)", query, re.I)
        if campaign_match:
            rows = [r for r in rows if r.shopping_product.campaign == campaign_match[1]]
        if merchant_match:
            rows = [r for r in rows if r.shopping_product.merchant_center_id == int(merchant_match[1])]
        return rows
    client.filters["shopping_product"] = scope
    server = h.build_server(tmp_path, client=client)
    payload = h.expect_ok(h.call(server, "get_product_status", {"campaign_id": " 111 "}))
    queries = [s.query for s in client.searches if "FROM shopping_product" in s.query]
    assert queries
    assert all(re.search(r"shopping_product\.campaign\s*=\s*['\"]" + re.escape(resource(111)) + r"['\"]", q) for q in queries)
    assert all(re.search(r"shopping_product\.merchant_center_id\s*=\s*['\"]?555111", q) for q in queries)
    assert payload["total_products"] == 2 and payload["status_counts"] == {"ELIGIBLE": 1, "NOT_ELIGIBLE": 1}
    assert {s.customer_id for s in client.searches} == {h.CUSTOMER_ID}


def test_valid_empty_scoped_feed_remains_success(tmp_path, account_client):
    account_client.stub("shopping_product", [])
    server = h.build_server(tmp_path, client=account_client)
    payload = h.expect_ok(h.call(server, "get_product_status", {"campaign_id": "111"}))
    assert payload["total_products"] == 0 and payload["status_counts"] == {}


def policy_client():
    client = ProjectedClient()
    summary = {"approval_status": "DISAPPROVED", "review_status": "REVIEWED",
               "policy_topic_entries": [{"topic": "DESTINATION_NOT_WORKING", "type_": "PROHIBITED"}]}
    client.stub("ad_group_ad", [{"campaign": {"id": 111, "name": "First"}, "ad_group": {"id": 201},
        "ad_group_ad": {"status": "ENABLED", "ad": {"id": 901}, "policy_summary": summary}}])
    client.stub("asset_group_asset", [{"campaign": {"id": 111, "name": "First"},
        "asset_group_asset": {"status": "ENABLED", "asset": f"customers/{h.CUSTOMER_ID}/assets/701",
            "asset_group": f"customers/{h.CUSTOMER_ID}/assetGroups/501", "field_type": "HEADLINE", "policy_summary": summary}}])
    client.stub("campaign_asset", [{"campaign": {"id": cid, "name": name},
        "campaign_asset": {"campaign": resource(cid), "asset": f"customers/{h.CUSTOMER_ID}/assets/{aid}", "field_type": "SITELINK", "status": status},
        "asset": {"resource_name": f"customers/{h.CUSTOMER_ID}/assets/{aid}", "type_": "SITELINK", "policy_summary": summary}}
        for cid, name, aid, status in [(111, "First", 801, "ENABLED"), (111, "First", 802, "PAUSED"), (222, "Other", 801, "ENABLED")]])
    return client


@pytest.mark.parametrize("enabled,expected,assets", [(False, 4, 3), (True, 3, 2)])
def test_policy_summary_counts_sitelinks_and_pmax_without_other_campaign(tmp_path, enabled, expected, assets):
    client = policy_client()
    server = h.build_server(tmp_path, client=client)
    payload = h.expect_ok(h.call(server, "get_policy_issues", {"campaign_id": "111", "enabled_only": enabled,
                 "topic": "DESTINATION_NOT_WORKING"}))
    assert payload["total_issues"] == expected and payload["sources"] == {"ad": 1, "asset": assets}
    assert payload["topics"] == [{"topic": "DESTINATION_NOT_WORKING", "count": expected}]
    queries = [s.query for s in client.searches if "FROM campaign_asset" in s.query]
    assert queries, "campaign-linked sitelinks were never queried"
    required = {"campaign_asset.campaign", "campaign_asset.asset", "campaign_asset.status", "asset.policy_summary.policy_topic_entries"}
    assert required <= set().union(*(set(selected(q)) for q in queries))


def test_full_policy_pagination_keeps_link_identity_status_and_filters(tmp_path):
    client = policy_client()
    server = h.build_server(tmp_path, client=client, env={"ADS_MCP_ROW_LIMIT": "1"})
    args = {"mode": "full", "campaign_id": "111", "topic": "DESTINATION_NOT_WORKING"}
    issues, tokens = [], set()
    for _ in range(8):
        payload = h.expect_ok(h.call(server, "get_policy_issues", args))
        issues += payload["issues"]
        token = payload.get("next_page_token")
        if not token:
            break
        assert token not in tokens
        tokens.add(token)
        args["page_token"] = token
    links = [i for i in issues if i.get("field_type") == "SITELINK"]
    assert len(issues) == 4 and len(links) == 2
    assert {i["entity_status"] for i in links} == {"ENABLED", "PAUSED"}
    assert {i["campaign_id"] for i in issues} == {"111"}
    assert {i["asset"] for i in links} == {f"customers/{h.CUSTOMER_ID}/assets/801", f"customers/{h.CUSTOMER_ID}/assets/802"}
    absent = h.expect_ok(h.call(server, "get_policy_issues", {"topic": "NO_SYNTHETIC_TOPIC"}))
    assert absent["total_issues"] == 0


def listing_client():
    client = ProjectedClient()
    rows = []
    for cid, gid, label in [(111, 201, "First group"), (111, 202, "Second group"), (222, 203, "Other campaign")]:
        base = f"customers/{h.CUSTOMER_ID}/adGroupCriteria/{gid}~"
        dimensions = [None, {"product_brand": {"value": "Synthetic Brand"}},
                      {"product_custom_attribute": {"index": "INDEX1", "value": "Featured"}},
                      {"product_type": {"level": "LEVEL2", "value": "Boots"}},
                      {"product_category": {"level": "LEVEL3", "category_id": 123}},
                      {"product_channel": {"channel": "ONLINE"}},
                      {"product_condition": {"condition": "NEW"}},
                      {"product_item_id": {"value": "SKU-SYNTHETIC"}}]
        for index, dimension in enumerate(dimensions, 1):
            listing = {"type_": "SUBDIVISION" if index == 1 else "UNIT"}
            if index > 1:
                listing.update(parent_ad_group_criterion=base + "1", case_value=dimension)
            rows.append({"campaign": {"id": cid}, "ad_group": {"id": gid, "name": label, "campaign": resource(cid)},
                "ad_group_criterion": {"criterion_id": index, "resource_name": base + str(index), "status": "ENABLED",
                    "type_": "LISTING_GROUP", "negative": index == 4, "listing_group": listing}})
    client.stub("ad_group_criterion", rows)
    def scope(items, call):
        match = re.search(r"campaign\.id\s*=\s*(\d+)", call.query)
        link = re.search(r"ad_group\.campaign\s*=\s*['\"]([^'\"]+)['\"]", call.query)
        if match:
            return [r for r in items if r.campaign.id == int(match[1])]
        if link:
            return [r for r in items if r.ad_group.campaign == link[1]]
        return items
    client.filters["ad_group_criterion"] = scope
    return client


def test_standard_shopping_tree_preserves_parent_exclusion_and_dimension_qualifiers(tmp_path):
    client = listing_client()
    server = h.build_server(tmp_path, client=client)
    payload = h.expect_ok(h.call(server, "get_listing_groups", {"campaign_id": "111"}))
    assert "ad_groups" in payload, "standard Shopping listing groups are absent"
    groups = {g["ad_group_id"]: g for g in payload["ad_groups"]}
    assert set(groups) == {"201", "202"}
    for group in groups.values():
        nodes = {str(n.get("criterion_id", n.get("filter_id"))): n for n in group["nodes"]}
        assert set(nodes) == {str(i) for i in range(1, 9)}
        assert nodes["1"]["type"] == "SUBDIVISION"
        assert nodes["2"].get("parent_criterion_id", nodes["2"].get("parent_filter_id")) == "1"
        assert nodes["2"]["dimension"] == {"product_brand": "Synthetic Brand"}
        assert nodes["3"]["dimension"] == {"product_custom_attribute": {"index": "INDEX1", "value": "Featured"}}
        assert nodes["4"]["dimension"] == {"product_type": {"level": "LEVEL2", "value": "Boots"}}
        assert nodes["5"]["dimension"] == {"product_category": {"level": "LEVEL3", "category_id": 123}}
        assert nodes["6"]["dimension"] == {"product_channel": "ONLINE"}
        assert nodes["7"]["dimension"] == {"product_condition": "NEW"}
        assert nodes["8"]["dimension"] == {"product_item_id": "SKU-SYNTHETIC"}
        assert nodes["4"].get("negative") is True or nodes["4"].get("excluded") is True or nodes["4"]["type"] == "UNIT_EXCLUDED"
    queries = [s.query for s in client.searches if "FROM ad_group_criterion" in s.query]
    assert queries and all("111" in q and "WHERE" in q for q in queries)
    assert {s.customer_id for s in client.searches} == {h.CUSTOMER_ID}
    for q in queries:
        for name in selected(q):
            if name.startswith("ad_group_criterion.listing_group"):
                assert name in PUBLIC and PUBLIC[name]["selectable"], f"nonselectable listing field: {name}"


def test_empty_listing_description_does_not_infer_ineligibility(tmp_path, fake_client):
    server = h.build_server(tmp_path, client=fake_client)
    payload = h.expect_ok(h.call(server, "get_listing_groups", {"campaign_id": "111"}))
    assert payload["asset_groups"] == [] and not payload.get("ad_groups")
    description = h.tool_map(server)["get_listing_groups"].description.lower()
    assert "smoking gun" not in description, "an empty tree alone does not establish feed eligibility"
    assert "empty" not in description or not any(w in description for w in ("ineligible", "cannot serve", "zero eligible"))
