"""F070: bounded complete Item-ID trees and one atomic replacement request."""
from copy import deepcopy
import json
import re

import pytest

import harness as h
from pmax_oracle import (BAD_IDS, BAD_TEXT, PMAX_ARGS, SAFETY_CASES, apply, checked_apply, changed,
    preview, rejected, rn, safeguard, setup, stage, stale, tree_row)

TOOL = "set_asset_group_product_selection"
RESOURCE = "asset_group_listing_group_filter"


def tree(provider):
    return provider.data[h.CUSTOMER_ID][RESOURCE]


def assert_replacement(call, items, old_ids=("1000", "1001", "1002", "1003")):
    assert call.service == "AssetGroupListingGroupFilterService"
    assert call.method == "mutate_asset_group_listing_group_filters"
    req = call.request
    assert req._pb.DESCRIPTOR.name == "MutateAssetGroupListingGroupFiltersRequest"
    assert "partial_failure" not in req._pb.DESCRIPTOR.fields_by_name, "the dedicated request is intrinsically atomic"
    assert req.customer_id == h.CUSTOMER_ID and not req.validate_only
    operations = list(req.operations)
    assert len(operations) == len(old_ids) + len(items) + 2
    assert all(op._pb.DESCRIPTOR.name == "AssetGroupListingGroupFilterOperation" for op in operations)
    removed = [op.remove for op in operations if op._pb.WhichOneof("operation") == "remove"]
    assert set(removed) == {rn("assetGroupListingGroupFilters", f"801~{ident}") for ident in old_ids}
    if len(old_ids) > 1:
        assert removed[-1] == rn("assetGroupListingGroupFilters", "801~1000"), "remove children before root"
    assert all(op._pb.WhichOneof("operation") == "remove" for op in operations[:len(old_ids)])
    creates = [op.create for op in operations[len(old_ids):]]
    assert len(creates) == len(items) + 2 and all(op._pb.WhichOneof("operation") == "create" for op in operations[len(old_ids):])
    root = creates[0]
    assert root.type_.name == "SUBDIVISION" and not root.parent_listing_group_filter
    assert not root._pb.HasField("case_value")
    assert re.fullmatch(r"customers/9876543210/assetGroupListingGroupFilters/801~-\d+", root.resource_name)
    temporary_names = [node.resource_name for node in creates if node.resource_name]
    assert len(set(temporary_names)) == len(temporary_names)
    assert all(re.fullmatch(r"customers/9876543210/assetGroupListingGroupFilters/801~-[1-9]\d*", name) for name in temporary_names)
    assert all(node.asset_group == rn("assetGroups", "801") and node.listing_source.name == "SHOPPING" for node in creates)
    assert all(node.parent_listing_group_filter == root.resource_name for node in creates[1:])
    included = [node for node in creates[1:] if node.type_.name == "UNIT_INCLUDED"]
    catchall = [node for node in creates[1:] if node.type_.name == "UNIT_EXCLUDED"]
    assert [node.case_value.product_item_id.value for node in included] == items
    assert all(node.case_value._pb.WhichOneof("dimension") == "product_item_id" for node in creates[1:])
    assert len(catchall) == 1 and not catchall[0].case_value.product_item_id._pb.HasField("value")
    assert all("802~" not in str(op) for op in operations), "sibling group cannot be changed"


def test_tree_preview_and_wire_preserve_case_entire_before_state_and_sibling(tmp_path):
    server, provider = setup(tmp_path, [TOOL])
    sibling_before = deepcopy(tree(provider)[-1])
    listing_before = h.expect_ok(h.call(server, "get_listing_groups", {"campaign_id": "701"}))
    plan = stage(server, TOOL, {"asset_group_id": "801", "item_ids": [" New-A ", "new-a"]})
    assert plan["irreversible"] is True
    text = json.dumps(plan)
    for old in ("Old-A", "Old-B", "UNIT_INCLUDED", "UNIT_EXCLUDED", "1000", "1001", "1002", "1003"):
        assert old in text, "complete before-state must survive in preview"
    assert "New-A" in text and "new-a" in text
    assert any(word in text.lower() for word in ("eligib", "inventory", "product"))
    assert any(word in text.lower() for word in ("spend", "deliver"))
    assert "spend-neutral" not in text.lower()
    call = checked_apply(server, provider, plan)
    assert_replacement(call, ["New-A", "new-a"])
    assert tree(provider)[-1] == sibling_before
    listing_after = h.expect_ok(h.call(server, "get_listing_groups", {"campaign_id": "701"}))
    before = next(r for r in listing_before["asset_groups"] if r["asset_group_id"] == "802")
    after = next(r for r in listing_after["asset_groups"] if r["asset_group_id"] == "802")
    assert before == after and before["nodes"], "unchanged complete sibling remains inspectable"


@pytest.mark.parametrize("shape", ["empty", "all_included", "all_excluded", "flat"])
def test_each_supported_initial_tree_shape(tmp_path, shape):
    server, provider = setup(tmp_path, [TOOL])
    if shape == "empty":
        provider.data[h.CUSTOMER_ID][RESOURCE] = [tree_row(2000, 802, type_="UNIT_INCLUDED")]
        ids = ()
    elif shape.startswith("all_"):
        provider.data[h.CUSTOMER_ID][RESOURCE] = [tree_row(type_="UNIT_INCLUDED" if shape == "all_included" else "UNIT_EXCLUDED")]
        ids = ("1000",)
    else:
        ids = ("1000", "1001", "1002", "1003")
    assert_replacement(checked_apply(server, provider, stage(server, TOOL)), ["New-A", "new-a"], ids)


@pytest.mark.parametrize("count", [1, 997, 998])
def test_input_item_count_edges_produce_exact_complete_tree(tmp_path, count):
    server, provider = setup(tmp_path, [TOOL])
    items = [f"SKU-{i}" for i in range(count)]
    assert_replacement(checked_apply(server, provider, stage(server, TOOL, {"asset_group_id": "801", "item_ids": items})), items)


@pytest.mark.parametrize("items", [[], ["x"] * 999, [f"SKU-{i}" for i in range(999)], ["Same", " Same "], "SKU", {}, None])
def test_invalid_selection_shapes_counts_and_duplicates_refuse_locally(tmp_path, items):
    server, provider = setup(tmp_path, [TOOL])
    rejected(server, provider, TOOL, {"asset_group_id": "801", "item_ids": items}, local=True)


@pytest.mark.parametrize("bad", BAD_TEXT + ["has space", "x" * 129])
def test_item_id_value_and_unicode_length_bounds(tmp_path, bad):
    server, provider = setup(tmp_path, [TOOL])
    rejected(server, provider, TOOL, {"asset_group_id": "801", "item_ids": [bad]}, local=True)


def test_exact_128_codepoint_item_id_is_supported_and_case_sensitive(tmp_path):
    server, provider = setup(tmp_path, [TOOL])
    items = ["é" * 128, "Case", "case"]
    assert_replacement(checked_apply(server, provider, stage(server, TOOL, {"asset_group_id": "801", "item_ids": items})), items)


@pytest.mark.parametrize("bad", BAD_IDS)
def test_group_identity_is_positive_local_numeric(tmp_path, bad):
    server, provider = setup(tmp_path, [TOOL])
    rejected(server, provider, TOOL, {**PMAX_ARGS[TOOL], "asset_group_id": bad}, local=True)


@pytest.mark.parametrize("issue", ["no_feed", "search", "removed_group", "removed_campaign", "missing_group", "missing_campaign", "foreign_group"])
def test_product_selection_requires_existing_feed_linked_pmax_parent(tmp_path, issue):
    server, provider = setup(tmp_path, [TOOL])
    if issue == "no_feed":
        provider.data[h.CUSTOMER_ID]["campaign"][0]["campaign"]["shopping_setting"] = {"merchant_id": 0}
    elif issue == "search":
        provider.data[h.CUSTOMER_ID]["campaign"][0]["campaign"]["advertising_channel_type"] = "SEARCH"
    elif issue.startswith("removed"):
        resource = "asset_group" if issue.endswith("group") else "campaign"
        provider.data[h.CUSTOMER_ID][resource][0][resource]["status"] = "REMOVED"
    elif issue.startswith("missing"):
        provider.corrupt["asset_group" if issue.endswith("group") else "campaign"] = lambda rows: []
    else:
        provider.corrupt["asset_group"] = lambda rows: [changed(r, "asset_group.resource_name", rn("assetGroups", "801", h.OTHER_CUSTOMER_ID)) for r in rows]
    rejected(server, provider, TOOL, PMAX_ARGS[TOOL])


@pytest.mark.parametrize("issue", ["brand", "nested", "cycle", "two_roots", "no_root", "missing_parent", "duplicate_id", "duplicate_item",
    "missing_catchall", "two_catchalls", "wrong_group", "foreign", "wrong_source", "identity_mismatch", "empty_item", "root_dimension", "unit_children"])
def test_unsupported_or_inconsistent_topology_is_not_silently_flattened(tmp_path, issue):
    server, provider = setup(tmp_path, [TOOL])
    rows = tree(provider)
    root, first, second, other = [row[RESOURCE] for row in rows[:4]]
    if issue == "brand":
        first["case_value"] = {"product_brand": {"value": "Synthetic"}}
    elif issue == "nested":
        first["type_"] = "SUBDIVISION"
        second["parent_listing_group_filter"] = first["resource_name"]
    elif issue == "cycle":
        root["parent_listing_group_filter"] = first["resource_name"]
    elif issue == "two_roots":
        rows.insert(0, tree_row(999))
    elif issue == "no_root":
        rows.pop(0)
    elif issue == "missing_parent":
        first["parent_listing_group_filter"] = rn("assetGroupListingGroupFilters", "801~999")
    elif issue == "duplicate_id":
        rows.insert(0, deepcopy(rows[0]))
    elif issue == "duplicate_item":
        second["case_value"] = deepcopy(first["case_value"])
    elif issue == "missing_catchall":
        rows.pop(3)
    elif issue == "two_catchalls":
        rows.insert(0, tree_row(1004, parent=1000, type_="UNIT_EXCLUDED"))
    elif issue == "wrong_group":
        provider.corrupt[RESOURCE] = lambda values: [changed(r, RESOURCE + ".asset_group", rn("assetGroups", "802")) for r in values]
    elif issue == "foreign":
        first["resource_name"] = rn("assetGroupListingGroupFilters", "801~1001", h.OTHER_CUSTOMER_ID)
    elif issue == "wrong_source":
        first["listing_source"] = "WEBPAGE"
    elif issue == "identity_mismatch":
        first["id"] = 999
    elif issue == "empty_item":
        first["case_value"]["product_item_id"]["value"] = ""
    elif issue == "root_dimension":
        root["case_value"] = {"product_item_id": {"value": "Not a root"}}
    else:
        root["type_"] = "UNIT_INCLUDED"
    rejected(server, provider, TOOL, PMAX_ARGS[TOOL])


@pytest.mark.parametrize("count,accepted", [(999, True), (1000, True), (1001, False), (3000, False)])
def test_complete_tree_source_limit_has_one_lookahead(tmp_path, count, accepted):
    server, provider = setup(tmp_path, [TOOL])
    provider.data[h.CUSTOMER_ID][RESOURCE] = [tree_row()] + [
        tree_row(1001 + i, parent=1000, item=f"Old-{i}", type_="UNIT_INCLUDED") for i in range(count - 2)] + [
        tree_row(9999, parent=1000, type_="UNIT_EXCLUDED")]
    if accepted:
        plan = stage(server, TOOL)
        text = json.dumps(plan)
        assert "Old-0" in text and f"Old-{count - 3}" in text and "9999" in text
    else:
        rejected(server, provider, TOOL, PMAX_ARGS[TOOL])
    assert provider.pulls[RESOURCE] == min(count, 1001), "source cap must be 1000 plus one lookahead"


def test_incomplete_tree_stream_never_yields_a_plan(tmp_path):
    server, provider = setup(tmp_path, [TOOL])
    provider.fail_after[RESOURCE] = 2
    rejected(server, provider, TOOL, PMAX_ARGS[TOOL])


@pytest.mark.parametrize("change", ["item", "type", "parent", "addition", "group_status", "campaign_status", "feed"])
def test_tree_apply_refuses_stale_relevant_state(tmp_path, change):
    server, provider = setup(tmp_path, [TOOL])
    plan = stage(server, TOOL)
    if change == "item":
        tree(provider)[1][RESOURCE]["case_value"]["product_item_id"]["value"] = "Changed"
    elif change == "type":
        tree(provider)[1][RESOURCE]["type_"] = "UNIT_EXCLUDED"
    elif change == "parent":
        tree(provider)[1][RESOURCE]["parent_listing_group_filter"] = rn("assetGroupListingGroupFilters", "801~777")
    elif change == "addition":
        tree(provider).append(tree_row(1004, parent=1000, item="New external", type_="UNIT_INCLUDED"))
    elif change == "feed":
        provider.data[h.CUSTOMER_ID]["campaign"][0]["campaign"]["shopping_setting"]["merchant_id"] = 77777
    else:
        resource = "asset_group" if change == "group_status" else "campaign"
        provider.data[h.CUSTOMER_ID][resource][0][resource]["status"] = "PAUSED"
    stale(server, provider, plan)


@pytest.mark.parametrize("case", SAFETY_CASES + ["acknowledgement"])
def test_tree_replacement_inherits_all_guardrails(tmp_path, monkeypatch, case):
    safeguard(tmp_path, monkeypatch, TOOL, case)


def test_product_selection_never_registers_readonly(tmp_path):
    server, provider = setup(tmp_path, ["get_asset_groups"], read_only=True)
    assert TOOL not in h.tool_names(server)
    result = h.call_result(server, TOOL, PMAX_ARGS[TOOL])
    assert result.is_error and "unknown tool" in h.result_text(result).lower()
