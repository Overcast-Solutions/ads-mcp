"""F067: inspect and change existing PMax groups without creating/removing them."""
import json

import pytest

import harness as h
from pmax_oracle import (BAD_IDS, SAFETY_CASES, apply, campaign_row, checked_apply, changed,
    golden, group_row, preview, read_bound, read_walk, rejected, rn, safeguard, setup, stage, stale)


def test_asset_group_read_has_an_independently_authored_payload(tmp_path):
    golden(tmp_path, "get_asset_groups")


@pytest.mark.parametrize("customer", [h.CUSTOMER_ID, h.OTHER_CUSTOMER_ID])
@pytest.mark.parametrize("campaign,groups", [("701", ["801", "802"]), ("702", ["803"])])
def test_read_scopes_two_accounts_and_two_campaigns(tmp_path, customer, campaign, groups):
    server, provider = setup(tmp_path, ["get_asset_groups"], read_only=True)
    result = h.expect_ok(h.call(server, "get_asset_groups", {"campaign_id": campaign, "customer_id": customer}))
    assert result["customer_id"] == customer and result["campaign_id"] == campaign
    assert [r["asset_group_id"] for r in result["asset_groups"]] == groups
    for row in result["asset_groups"]:
        assert row["resource_name"] == rn("assetGroups", row["asset_group_id"], customer)
        assert row["campaign_id"] == campaign and customer in row["name"]
        assert row["status"] == "ENABLED" and row["primary_status"] == "ELIGIBLE"
        assert row["final_urls"] == [f"https://example.invalid/{row['asset_group_id']}"]
    assert provider.searches and all(s.customer_id == customer for s in provider.searches)
    assert any("campaign.id" in s.query and campaign in s.query for s in provider.searches)
    assert not provider.mutations


def test_empty_groups_is_valid_only_after_parent_verification(tmp_path):
    server, provider = setup(tmp_path, ["get_asset_groups"], read_only=True)
    provider.data[h.CUSTOMER_ID]["asset_group"] = []
    assert h.expect_ok(h.call(server, "get_asset_groups", {"campaign_id": "701"}))["asset_groups"] == []
    assert any("FROM campaign" in s.query for s in provider.searches)
    rejected(server, provider, "get_asset_groups", {"campaign_id": "703"})
    rejected(server, provider, "get_asset_groups", {"campaign_id": "999"})


@pytest.mark.parametrize("bad", BAD_IDS)
def test_read_identifier_refuses_locally(tmp_path, bad):
    server, provider = setup(tmp_path, ["get_asset_groups"], read_only=True)
    rejected(server, provider, "get_asset_groups", {"campaign_id": bad}, local=True)


@pytest.mark.parametrize("case", ["walk", "account", "filter", "tool", "tamper", "expiry"])
def test_asset_group_read_retains_bounded_account_filter_bound_pages(tmp_path, case):
    read_walk(tmp_path, "get_asset_groups", {"campaign_id": "701"}, "asset_groups", "asset_group",
              lambda cid: [group_row(810 + i, customer=cid) for i in range(3)], case=case)


def test_asset_group_read_has_10000_row_capacity_and_one_lookahead(tmp_path):
    read_bound(tmp_path, "get_asset_groups", {"campaign_id": "701"}, "asset_groups", "asset_group",
               lambda cid: [group_row(10000 + i, customer=cid) for i in range(10002)])


@pytest.mark.parametrize("tool,old,new", [("pause_entity", "ENABLED", "PAUSED"), ("enable_entity", "PAUSED", "ENABLED")])
def test_status_plan_uses_actual_state_and_genuine_status_only_request(tmp_path, tool, old, new):
    server, provider = setup(tmp_path, ["get_asset_groups"])
    provider.data[h.CUSTOMER_ID]["asset_group"][0]["asset_group"]["status"] = old
    before = json.dumps(provider.data, sort_keys=True)
    plan = stage(server, tool, {"entity_type": "asset_group", "entity_id": " 000801 ", "customer_id": h.CUSTOMER_ID_DASHED})
    text = json.dumps(plan)
    assert old in text and new in text and "701" in text
    assert any(word in text.lower() for word in ("spend", "budget")) and "deliver" in text.lower()
    call = checked_apply(server, provider, plan)
    assert call.service == "AssetGroupService" and call.method == "mutate_asset_groups"
    req = call.request
    assert req._pb.DESCRIPTOR.name == "MutateAssetGroupsRequest"
    assert req.customer_id == h.CUSTOMER_ID
    assert "partial_failure" not in req._pb.DESCRIPTOR.fields_by_name
    assert len(req.operations) == 1
    operation = req.operations[0]
    assert operation._pb.DESCRIPTOR.name == "AssetGroupOperation"
    assert operation._pb.WhichOneof("operation") == "update"
    assert operation.update.resource_name == rn("assetGroups", "801")
    assert operation.update.status.name == new and list(operation.update_mask.paths) == ["status"]
    assert {field.name for field, value in operation.update._pb.ListFields()} == {"resource_name", "status"}
    assert json.dumps(provider.data, sort_keys=True) == before, "transport fixture never mutates unrelated state"


@pytest.mark.parametrize("tool", ["pause_entity", "enable_entity"])
@pytest.mark.parametrize("bad", BAD_IDS)
def test_status_invalid_id_is_rejected_before_reads(tmp_path, tool, bad):
    server, provider = setup(tmp_path, ["get_asset_groups"])
    rejected(server, provider, tool, {"entity_type": "asset_group", "entity_id": bad}, local=True)


@pytest.mark.parametrize("problem", ["group_missing", "group_duplicate", "group_foreign", "group_removed", "group_inconsistent",
    "parent_missing", "parent_duplicate", "parent_foreign", "parent_removed", "parent_search"])
def test_status_requires_exact_unique_live_group_and_pmax_parent(tmp_path, problem):
    server, provider = setup(tmp_path, ["get_asset_groups"])
    resource = "asset_group" if problem.startswith("group") else "campaign"
    if problem.endswith("missing"):
        provider.corrupt[resource] = lambda rows: []
    elif problem.endswith("duplicate"):
        provider.corrupt[resource] = lambda rows: rows + rows
    elif problem.endswith("foreign"):
        provider.corrupt[resource] = lambda rows: [changed(r, resource + ".resource_name", rn("assetGroups" if resource == "asset_group" else "campaigns", "801" if resource == "asset_group" else "701", h.OTHER_CUSTOMER_ID)) for r in rows]
    elif problem.endswith("removed"):
        provider.corrupt[resource] = lambda rows: [changed(r, resource + ".status", "REMOVED") for r in rows]
    elif problem.endswith("search"):
        provider.corrupt[resource] = lambda rows: [changed(r, "campaign.advertising_channel_type", "SEARCH") for r in rows]
    else:
        provider.corrupt[resource] = lambda rows: [changed(r, "asset_group.id", 899) for r in rows]
    rejected(server, provider, "pause_entity", {"entity_type": "asset_group", "entity_id": "801"})


@pytest.mark.parametrize("field,value", [("asset_group.status", "PAUSED"), ("asset_group.campaign", rn("campaigns", "702")),
    ("campaign.status", "PAUSED"), ("campaign.advertising_channel_type", "SEARCH")])
def test_status_rechecks_group_and_parent_and_refuses_drift(tmp_path, field, value):
    server, provider = setup(tmp_path, ["get_asset_groups"])
    plan = stage(server, "pause_entity", {"entity_type": "asset_group", "entity_id": "801"})
    resource = field.split(".")[0]
    provider.corrupt[resource] = lambda rows: [changed(r, field, value) for r in rows]
    stale(server, provider, plan)


def test_asset_group_removal_is_not_enabled_through_shared_lifecycle_map(tmp_path):
    server, provider = setup(tmp_path, ["get_asset_groups"])
    rejected(server, provider, "remove_entity", {"entity_type": "asset_group", "entity_id": "801"}, local=True)


@pytest.mark.parametrize("tool", ["pause_entity", "enable_entity"])
@pytest.mark.parametrize("case", SAFETY_CASES)
def test_group_lifecycle_keeps_each_application_safeguard(tmp_path, monkeypatch, tool, case):
    safeguard(tmp_path, monkeypatch, tool, case, args={"entity_type": "asset_group", "entity_id": "801"}, prerequisite=["get_asset_groups"])


@pytest.mark.parametrize("field,value", [("asset_group.resource_name", rn("assetGroups", "801", h.OTHER_CUSTOMER_ID)),
    ("asset_group.campaign", rn("campaigns", "702"))])
def test_group_read_cannot_leak_a_foreign_result_row(tmp_path, field, value):
    server, provider = setup(tmp_path, ["get_asset_groups"], read_only=True)
    provider.corrupt["asset_group"] = lambda rows: [changed(row, field, value) for row in rows]
    payload = h.call(server, "get_asset_groups", {"campaign_id": "701"})
    if "error" in payload:
        assert h.error_of(payload)["code"] != "INTERNAL"
    else:
        assert payload["asset_groups"] == []
    assert not provider.mutations
