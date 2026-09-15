"""F039: resource-kind grammar and canonical campaign scope through real MCP."""
import re

import pytest
from google.ads.googleads.v25.services.services.ad_group_ad_service import AdGroupAdServiceClient
from google.ads.googleads.v25.services.services.ad_group_criterion_service import AdGroupCriterionServiceClient
from google.ads.googleads.v25.services.services.ad_group_service import AdGroupServiceClient
from google.ads.googleads.v25.services.services.campaign_service import CampaignServiceClient

import harness as h
from offline_contract import ProjectedClient


LIFECYCLE = ["pause_entity", "enable_entity", "remove_entity"]
ENTITIES = {
    "campaign": ("111", "00111", "campaigns", CampaignServiceClient.parse_campaign_path),
    "ad_group": ("201", "00201", "adGroups", AdGroupServiceClient.parse_ad_group_path),
    "ad": ("201~901", "00201~00901", "adGroupAds", AdGroupAdServiceClient.parse_ad_group_ad_path),
    "keyword": ("201~401", "00201~00401", "adGroupCriteria", AdGroupCriterionServiceClient.parse_ad_group_criterion_path),
}
REPORTS = {
    "get_campaign_performance": ({"last_n_days": 1}, "campaigns"),
    "get_ad_performance": ({"last_n_days": 1}, "ads"),
    "get_keyword_performance": ({"last_n_days": 1}, "keywords"),
    "get_search_terms": ({"last_n_days": 1}, "search_terms"),
    "get_geo_performance": ({"last_n_days": 1}, "locations"),
    "get_policy_issues": ({"mode": "full"}, "issues"),
    "get_shopping_performance": ({"last_n_days": 1}, "products"),
    "get_listing_groups": ({}, "asset_groups"),
    "get_product_status": ({}, None),
}
OPTIONAL = list(REPORTS)[:6]


@pytest.mark.parametrize("tool", LIFECYCLE)
@pytest.mark.parametrize("kind,identifier", [(kind, value) for kind in ENTITIES
    for value in (("111~222", "111~222~333") if kind in ("campaign", "ad_group")
                  else ("901", "111~222~333"))] +
    [(kind, value) for kind in ENTITIES for value in (" ", "-1", "1.5", "NaN", "1 OR 1=1", "1~", "~1")])
def test_wrong_resource_grammar_refuses_before_any_account_call(tmp_path, account_client, tool, kind, identifier):
    server = h.build_rw_server(tmp_path, client=account_client)
    h.expect_error(server, tool, {"entity_type": kind, "entity_id": identifier}, code="INVALID_ID")
    assert not account_client.searches and not account_client.mutations
    assert not any(r.get("event") == "plan_created" for r in h.read_audit_records(tmp_path))


@pytest.mark.parametrize("tool", LIFECYCLE)
@pytest.mark.parametrize("kind", ENTITIES)
@pytest.mark.parametrize("padded", [False, True])
def test_valid_resource_identity_is_identical_in_preview_and_real_sdk(tmp_path, account_client, tool, kind, padded):
    canonical, zeros, collection, parse = ENTITIES[kind]
    supplied = f"  {zeros}  " if padded else canonical
    server = h.build_rw_server(tmp_path, client=account_client, env={"ADS_MCP_REQUIRE_DRY_RUN": "true"})
    plan = h.expect_ok(h.call(server, tool, {"entity_type": kind, "entity_id": supplied}))["plan"]
    resource = f"customers/{h.CUSTOMER_ID}/{collection}/{canonical}"
    assert plan["operations"][0]["resource"] == resource
    assert parse(resource), "the genuine SDK parser must recognize the approved resource"
    assert not account_client.mutations
    preview = h.expect_ok(h.call(server, "confirm_and_apply", {"plan_id": plan["id"], "dry_run": True}))
    assert preview["plan"]["operations"] == plan["operations"]
    result = h.expect_ok(h.call(server, "confirm_and_apply", {"plan_id": plan["id"], "dry_run": False, "confirm_irreversible": True}))
    assert result["applied"] and len(account_client.live_mutations()) == 1
    request = account_client.live_mutations()[0].request
    assert request.customer_id == h.CUSTOMER_ID
    op = request.operations[0]
    assert (op.remove if tool == "remove_entity" else op.update.resource_name) == resource
    assert op._pb.WhichOneof("operation") == ("remove" if tool == "remove_entity" else "update")
    if tool != "remove_entity":
        assert op.update.status.name == ("PAUSED" if tool == "pause_entity" else "ENABLED")
    h.expect_error(server, "confirm_and_apply", {"plan_id": plan["id"], "dry_run": False, "confirm_irreversible": True}, code="PLAN_CONSUMED")
    assert len(account_client.live_mutations()) == 1


@pytest.mark.parametrize("tool", LIFECYCLE)
def test_valid_composite_id_does_not_change_mutation_account_binding(tmp_path, account_client, tool):
    server = h.build_rw_server(tmp_path, client=account_client)
    h.error_of(h.call(server, tool, {"entity_type": "ad", "entity_id": "00201~00901", "customer_id": h.OTHER_CUSTOMER_ID}))
    assert not account_client.searches and not account_client.mutations


def scoped_client():
    client = ProjectedClient()
    rows = []
    for index, cid in enumerate((111, 222, 111), 1):
        rows.append({"campaign": {"id": cid, "name": f"Campaign {cid}", "status": "ENABLED",
            "shopping_setting": {"merchant_id": cid + 5000}},
            "customer": {"currency_code": "EUR"}, "metrics": {"clicks": index},
            "ad_group": {"id": cid + 1000, "name": f"Group {cid}"},
            "ad_group_ad": {"ad": {"id": index}, "status": "ENABLED", "policy_summary": {
                "policy_topic_entries": [{"topic": f"topic-{index}", "type_": "PROHIBITED"}]}},
            "ad_group_criterion": {"criterion_id": index, "keyword": {"text": f"seed-{index}", "match_type": "EXACT"}},
            "search_term_view": {"search_term": f"term-{index}"},
            "geographic_view": {"country_criterion_id": index},
            "segments": {"product_item_id": f"item-{index}", "product_title": f"Item {index}"},
            "asset_group": {"id": cid + 2000, "name": f"Assets {cid}"},
            "asset_group_listing_group_filter": {"id": index, "type_": "UNIT_INCLUDED"}})
    for resource in ("ad_group_ad", "keyword_view", "search_term_view", "geographic_view",
                     "shopping_performance_view", "asset_group_listing_group_filter"):
        client.stub(resource, rows)
    client.stub("campaign", rows[:2])
    client.stub("shopping_product", [{"shopping_product": {"item_id": f"p-{i}",
        "campaign": f"customers/{h.OTHER_CUSTOMER_ID}/campaigns/{cid}", "merchant_center_id": cid + 5000,
        "status": "ELIGIBLE"}} for i, cid in enumerate((111, 222, 111))])
    # A genuine product query must scope both campaign and merchant. Those
    # fields are deliberately absent from the production SELECT projection.
    def products(items, call):
        campaign = re.search(r"shopping_product\.campaign\s*=\s*'([^']+)'", call.query)
        merchant = re.search(r"shopping_product\.merchant_center_id\s*=\s*(\d+)", call.query)
        assert campaign and merchant
        return [r for r in items if r.shopping_product.campaign == campaign[1]
            and r.shopping_product.merchant_center_id == int(merchant[1])]
    client.filters["shopping_product"] = products
    return client


@pytest.mark.parametrize("tool", REPORTS)
@pytest.mark.parametrize("identifier", ["", " ", "1~2", "1.2", "-1", "NaN", "Infinity", "111 OR 1=1"])
def test_bad_campaign_filters_never_widen_or_contact_accounts(tmp_path, tool, identifier):
    client = scoped_client()
    server = h.build_server(tmp_path, client=client)
    h.expect_error(server, tool, {**REPORTS[tool][0], "campaign_id": identifier}, code="INVALID_ID")
    assert not client.searches and not client.mutations


@pytest.mark.parametrize("tool", REPORTS)
@pytest.mark.parametrize("identifier", ["111", " 111 ", "00111", " 00111 "])
def test_canonical_campaign_scope_survives_multi_campaign_local_pages(tmp_path, tool, identifier):
    client = scoped_client()
    server = h.build_server(tmp_path, client=client, env={"ADS_MCP_ROW_LIMIT": "1"})
    args = {**REPORTS[tool][0], "customer_id": h.OTHER_CUSTOMER_ID, "campaign_id": identifier}
    key = REPORTS[tool][1]
    seen, tokens = [], set()
    for _ in range(5):
        payload = h.expect_ok(h.call(server, tool, args))
        assert payload["customer_id"] == h.OTHER_CUSTOMER_ID
        if tool != "get_policy_issues":
            assert payload["campaign_id"] == "111"
        if key:
            seen += payload[key]
        token = payload.get("next_page_token")
        if not token:
            break
        assert token not in tokens
        tokens.add(token)
        args["page_token"] = token
    assert not payload.get("next_page_token")
    if tool == "get_product_status":
        assert payload["total_products"] == 2 and payload["merchant_id"] == "5111"
    elif tool == "get_listing_groups":
        assert len(seen) == 1 and seen[0]["asset_group_id"] == "2111"
        assert [n["filter_id"] for n in seen[0]["nodes"]] == ["1", "3"]
    elif tool == "get_shopping_performance":
        assert [r["item_id"] for r in seen] == ["item-1", "item-3"]
    else:
        assert len(seen) == (1 if tool == "get_campaign_performance" else 2)
        assert {r["campaign_id"] for r in seen} == {"111"}
    for call in client.searches:
        assert call.customer_id == h.OTHER_CUSTOMER_ID
        if "FROM shopping_product" not in call.query:
            assert re.search(r"campaign\.id\s*=\s*111\b", call.query), call.query
            assert not re.search(r"campaign\.id\s*=\s*0+111\b", call.query)
    assert not client.mutations


@pytest.mark.parametrize("tool", OPTIONAL)
def test_none_preserves_existing_omitted_filter_behavior(tmp_path, tool):
    client = scoped_client()
    server = h.build_server(tmp_path, client=client)
    args = {**REPORTS[tool][0], "customer_id": h.OTHER_CUSTOMER_ID}
    omitted = h.expect_ok(h.call(server, tool, args))
    explicit = h.expect_ok(h.call(server, tool, {**args, "campaign_id": None}))
    assert omitted == explicit
    assert {r["campaign_id"] for r in explicit[REPORTS[tool][1]]} == {"111", "222"}
    assert not any(re.search(r"campaign\.id\s*=", c.query) for c in client.searches)


def test_documented_lifecycle_ids_explain_the_composite_form():
    from pathlib import Path
    text = (Path(__file__).resolve().parents[1] / "README.md").read_text().lower()
    assert "~" in text and "ad_group" in text and "keyword" in text
    assert any(word in text for word in ("composite", "parent", "adgroupid", "ad_group_id"))
