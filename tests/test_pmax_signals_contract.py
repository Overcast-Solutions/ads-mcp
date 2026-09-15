"""F068: optimization signals, existing audiences and supported removals."""
from copy import deepcopy
import json

import pytest

import harness as h
from pmax_oracle import (BAD_IDS, BAD_TEXT, PMAX_ARGS, SAFETY_CASES, apply, audience_row, checked_apply, changed,
    golden, read_bound, read_walk, rejected, rn, safeguard, setup, signal_row, stage, stale)

TOOLS = ["get_asset_group_signals", "list_audiences", "add_asset_group_search_themes",
         "add_asset_group_audience_signal", "remove_asset_group_signals"]
WRITES = TOOLS[2:]


@pytest.mark.parametrize("tool", TOOLS[:2])
def test_signal_read_payloads_are_independently_authored(tmp_path, tool):
    golden(tmp_path, tool)


@pytest.mark.parametrize("customer", [h.CUSTOMER_ID, h.OTHER_CUSTOMER_ID])
@pytest.mark.parametrize("group,ids", [("801", ["801~701", "801~702"]), ("802", ["802~703"])])
def test_signal_reads_scope_two_groups_and_accounts(tmp_path, customer, group, ids):
    server, provider = setup(tmp_path, TOOLS[:2], read_only=True)
    payload = h.expect_ok(h.call(server, "get_asset_group_signals", {"asset_group_id": group, "customer_id": customer}))
    assert payload["customer_id"] == customer and payload["asset_group_id"] == group
    assert [r["signal_id"] for r in payload["signals"]] == ids
    for row in payload["signals"]:
        assert row["resource_name"] == rn("assetGroupSignals", row["signal_id"], customer)
        assert row["asset_group_id"] == group and row["kind"] in ("search_theme", "audience")
        assert row["approval_status"] == "APPROVED" and row["disapproval_reasons"] == []
    assert all(s.customer_id == customer for s in provider.searches)
    audiences = h.expect_ok(h.call(server, "list_audiences", {"customer_id": customer}))["audiences"]
    assert [r["audience_id"] for r in audiences] == ["901", "902", "903"]
    assert [r["asset_group_id"] for r in audiences] == [None, "801", "802"]
    assert [r["scope"] for r in audiences] == ["CUSTOMER", "ASSET_GROUP", "ASSET_GROUP"]
    assert all(r["resource_name"] == rn("audiences", r["audience_id"], customer) for r in audiences)
    assert all(s.customer_id == customer for s in provider.searches)


@pytest.mark.parametrize("kind", ["local_services_id", "vertical_ads_item_group_rule_list", "unknown"])
def test_unsupported_same_group_signals_remain_visible_and_cannot_be_removed(tmp_path, kind):
    server, provider = setup(tmp_path, TOOLS)
    provider.data[h.CUSTOMER_ID]["asset_group_signal"] = [signal_row(kind=kind)]
    payload = h.expect_ok(h.call(server, "get_asset_group_signals", {"asset_group_id": "801"}))
    assert len(payload["signals"]) == 1
    signal = payload["signals"][0]
    assert signal["signal_id"] == "801~701" and signal["kind"] == kind and signal["supported"] is False
    assert signal["search_theme"] is None and signal["audience_resource_name"] is None
    rejected(server, provider, "remove_asset_group_signals", {"asset_group_id": "801", "signal_ids": ["701"]})


@pytest.mark.parametrize("tool,resource,key,args", [
    ("get_asset_group_signals", "asset_group_signal", "signals", {"asset_group_id": "801"}),
    ("list_audiences", "audience", "audiences", {}),
])
@pytest.mark.parametrize("case", ["walk", "account", "tool", "tamper", "expiry"])
def test_signal_reads_retain_continuations(tmp_path, tool, resource, key, args, case):
    rows = (lambda cid: [signal_row(710 + i, customer=cid, value=f"Theme {i}") for i in range(3)]) if key == "signals" else (
        lambda cid: [audience_row(910 + i, cid) for i in range(3)])
    read_walk(tmp_path, tool, args, key, resource, rows, case=case)


def test_signal_token_cannot_switch_group(tmp_path):
    read_walk(tmp_path, "get_asset_group_signals", {"asset_group_id": "801"}, "signals", "asset_group_signal",
        lambda cid: [signal_row(710 + i, customer=cid, value=f"Theme {i}") for i in range(3)], case="filter")


@pytest.mark.parametrize("tool", ["get_asset_group_signals", "list_audiences"])
def test_signal_public_read_limits(tmp_path, tool):
    signals = tool == "get_asset_group_signals"
    read_bound(tmp_path, tool, {"asset_group_id": "801"} if signals else {}, "signals" if signals else "audiences",
        "asset_group_signal" if signals else "audience",
        (lambda cid: [signal_row(10000 + i, customer=cid, value=f"Theme {i}") for i in range(10002)]) if signals else (
            lambda cid: [audience_row(10000 + i, cid) for i in range(10002)]))


@pytest.mark.parametrize("tool", WRITES)
def test_all_signal_writes_are_unregistered_readonly(tmp_path, tool):
    server, provider = setup(tmp_path, TOOLS[:2], read_only=True)
    assert tool not in h.tool_names(server)
    result = h.call_result(server, tool, PMAX_ARGS[tool])
    assert result.is_error and "unknown tool" in h.result_text(result).lower()
    assert not provider.searches and not provider.mutations


@pytest.mark.parametrize("tool", WRITES)
def test_signal_requests_use_only_correct_service_and_full_batch(tmp_path, tool):
    server, provider = setup(tmp_path, TOOLS)
    plan = stage(server, tool)
    text = json.dumps(plan).lower()
    assert "signal" in text and any(word in text for word in ("optimization", "optimisation"))
    assert "target" in text, "preview must distinguish signal from hard targeting"
    call = checked_apply(server, provider, plan)
    assert call.service == "AssetGroupSignalService" and call.method == "mutate_asset_group_signals"
    req = call.request
    assert req._pb.DESCRIPTOR.name == "MutateAssetGroupSignalsRequest" and req.customer_id == h.CUSTOMER_ID and not req.partial_failure
    if tool == "add_asset_group_search_themes":
        assert [op.create.search_theme.text for op in req.operations] == ["New season", "Trail equipment"]
        assert all(op.create.asset_group == rn("assetGroups", "801") for op in req.operations)
        assert all(op.create._pb.WhichOneof("signal") == "search_theme" for op in req.operations)
    elif tool == "add_asset_group_audience_signal":
        assert len(req.operations) == 1 and req.operations[0].create.audience.audience == rn("audiences", "902")
        assert req.operations[0].create.asset_group == rn("assetGroups", "801")
    else:
        assert [op.remove for op in req.operations] == [rn("assetGroupSignals", "801~701"), rn("assetGroupSignals", "801~702")]
        assert plan["irreversible"] is True
    assert "customAudiences" not in str(req) and "userLists" not in str(req)


@pytest.mark.parametrize("bad", BAD_TEXT + ["x" * 81])
def test_search_theme_invalid_values_refuse_before_reads(tmp_path, bad):
    server, provider = setup(tmp_path, TOOLS)
    rejected(server, provider, "add_asset_group_search_themes", {"asset_group_id": "801", "themes": [bad]}, local=True)


@pytest.mark.parametrize("themes", [[], "theme", {"text": "theme"}, ["Same", " Same "], ["x"] * 51])
def test_search_theme_list_shape_uniqueness_and_requested_count_are_local(tmp_path, themes):
    server, provider = setup(tmp_path, TOOLS)
    rejected(server, provider, "add_asset_group_search_themes", {"asset_group_id": "801", "themes": themes}, local=True)


@pytest.mark.parametrize("count,accepted", [(48, True), (49, True), (50, False)])
def test_resulting_theme_limit_counts_existing_themes(tmp_path, count, accepted):
    server, provider = setup(tmp_path, TOOLS)
    args = {"asset_group_id": "801", "themes": [f"New {i}" for i in range(count)]}
    if accepted:
        plan = stage(server, "add_asset_group_search_themes", args)
        call = checked_apply(server, provider, plan)
        assert len(call.request.operations) == count
    else:
        rejected(server, provider, "add_asset_group_search_themes", args)


def test_theme_unicode_codepoints_strip_and_existing_duplicates(tmp_path):
    server, provider = setup(tmp_path, TOOLS)
    plan = stage(server, "add_asset_group_search_themes", {"asset_group_id": "801", "themes": [" " + "é" * 80 + " "]})
    call = checked_apply(server, provider, plan)
    assert call.request.operations[0].create.search_theme.text == "é" * 80
    provider.mutations.clear()
    rejected(server, provider, "add_asset_group_search_themes", {"asset_group_id": "801", "themes": [" Trail footwear "]})


@pytest.mark.parametrize("audience,accepted", [("902", True), ("904", True), ("901", False), ("903", False), ("999", False)])
def test_audience_must_be_existing_enabled_and_scoped_to_selected_group(tmp_path, audience, accepted):
    server, provider = setup(tmp_path, TOOLS)
    provider.data[h.CUSTOMER_ID]["audience"].append(audience_row(904))
    args = {"asset_group_id": "801", "audience_id": audience}
    if accepted:
        call = checked_apply(server, provider, stage(server, "add_asset_group_audience_signal", args))
        assert call.request.operations[0].create.audience.audience == rn("audiences", audience)
    else:
        rejected(server, provider, "add_asset_group_audience_signal", args)


@pytest.mark.parametrize("issue", ["removed", "unknown_scope", "foreign", "duplicate", "mismatched_id"])
def test_ambiguous_or_unverifiable_audience_is_not_authority(tmp_path, issue):
    server, provider = setup(tmp_path, TOOLS)
    if issue == "duplicate":
        provider.corrupt["audience"] = lambda rows: rows + rows
    else:
        field, value = {"removed": ("status", "REMOVED"), "unknown_scope": ("scope", "UNKNOWN"),
            "foreign": ("resource_name", rn("audiences", "902", h.OTHER_CUSTOMER_ID)), "mismatched_id": ("id", 909)}[issue]
        provider.corrupt["audience"] = lambda rows: [changed(r, "audience." + field, value) for r in rows]
    rejected(server, provider, "add_asset_group_audience_signal", PMAX_ARGS["add_asset_group_audience_signal"])


@pytest.mark.parametrize("tool,field", [("add_asset_group_search_themes", "asset_group_id"),
    ("add_asset_group_audience_signal", "audience_id"), ("remove_asset_group_signals", "signal_ids")])
@pytest.mark.parametrize("bad", BAD_IDS)
def test_signal_identifiers_are_strict_and_local(tmp_path, tool, field, bad):
    server, provider = setup(tmp_path, TOOLS)
    rejected(server, provider, tool, {**PMAX_ARGS[tool], field: [bad] if field == "signal_ids" else bad}, local=True)


@pytest.mark.parametrize("ids", [[], ["701", "0701"], ["701", "701"], ["703"], ["999"]])
def test_signal_removals_are_unique_existing_children_of_explicit_group(tmp_path, ids):
    server, provider = setup(tmp_path, TOOLS)
    rejected(server, provider, "remove_asset_group_signals", {"asset_group_id": "801", "signal_ids": ids}, local=not ids or len(ids) > 1)


@pytest.mark.parametrize("tool", WRITES)
@pytest.mark.parametrize("issue", ["duplicate", "foreign", "other_group", "incomplete", "over_limit"])
def test_signal_mutations_require_complete_consistent_source(tmp_path, tool, issue):
    server, provider = setup(tmp_path, TOOLS)
    if issue == "duplicate":
        provider.corrupt["asset_group_signal"] = lambda rows: rows + rows
    elif issue == "foreign":
        provider.corrupt["asset_group_signal"] = lambda rows: [changed(r, "asset_group_signal.resource_name", rn("assetGroupSignals", "801~701", h.OTHER_CUSTOMER_ID)) for r in rows]
    elif issue == "other_group":
        provider.corrupt["asset_group_signal"] = lambda rows: [changed(r, "asset_group_signal.asset_group", rn("assetGroups", "802")) for r in rows]
    elif issue == "incomplete":
        provider.fail_after["asset_group_signal"] = 1
    else:
        provider.data[h.CUSTOMER_ID]["asset_group_signal"] = [signal_row(10000 + i, value=f"Theme {i}") for i in range(10002)]
    rejected(server, provider, tool, PMAX_ARGS[tool])
    assert provider.pulls["asset_group_signal"] <= 10001


@pytest.mark.parametrize("tool", WRITES)
@pytest.mark.parametrize("change", ["signal", "group", "parent"])
def test_signal_mutations_recheck_relevant_state(tmp_path, tool, change):
    server, provider = setup(tmp_path, TOOLS)
    plan = stage(server, tool)
    if change == "signal":
        provider.data[h.CUSTOMER_ID]["asset_group_signal"][0]["asset_group_signal"]["search_theme"]["text"] = "Changed"
    else:
        resource = "asset_group" if change == "group" else "campaign"
        provider.data[h.CUSTOMER_ID][resource][0][resource]["status"] = "PAUSED"
    stale(server, provider, plan)


def test_audience_change_after_staging_refuses(tmp_path):
    server, provider = setup(tmp_path, TOOLS)
    plan = stage(server, "add_asset_group_audience_signal")
    provider.data[h.CUSTOMER_ID]["audience"][1]["audience"]["asset_group"] = rn("assetGroups", "802")
    stale(server, provider, plan)


@pytest.mark.parametrize("kind", ["local_services_id", "vertical_ads_item_group_rule_list", "unknown"])
def test_removal_rechecks_supported_kind_at_apply(tmp_path, kind):
    server, provider = setup(tmp_path, TOOLS)
    plan = stage(server, "remove_asset_group_signals")
    provider.data[h.CUSTOMER_ID]["asset_group_signal"][0] = signal_row(kind=kind)
    stale(server, provider, plan)


@pytest.mark.parametrize("tool", WRITES)
@pytest.mark.parametrize("case", SAFETY_CASES)
def test_every_signal_operation_inherits_guardrails(tmp_path, monkeypatch, tool, case):
    safeguard(tmp_path, monkeypatch, tool, case)


def test_signal_removal_requires_irreversible_acknowledgement(tmp_path, monkeypatch):
    safeguard(tmp_path, monkeypatch, "remove_asset_group_signals", "acknowledgement")


@pytest.mark.parametrize("bad", BAD_IDS)
def test_signal_read_identifier_validation_precedes_account_access(tmp_path, bad):
    server, provider = setup(tmp_path, TOOLS[:2], read_only=True)
    rejected(server, provider, "get_asset_group_signals", {"asset_group_id": bad}, local=True)


def test_signal_and_audience_reads_return_verified_empty_lists(tmp_path):
    server, provider = setup(tmp_path, TOOLS[:2], read_only=True)
    provider.data[h.CUSTOMER_ID]["asset_group_signal"] = []
    provider.data[h.CUSTOMER_ID]["audience"] = []
    assert h.expect_ok(h.call(server, "get_asset_group_signals", {"asset_group_id": "801"}))["signals"] == []
    assert h.expect_ok(h.call(server, "list_audiences"))["audiences"] == []
    rejected(server, provider, "get_asset_group_signals", {"asset_group_id": "999"})


@pytest.mark.parametrize("tool,resource,key,field,value", [
    ("get_asset_group_signals", "asset_group_signal", "signals", "asset_group_signal.resource_name", rn("assetGroupSignals", "801~701", h.OTHER_CUSTOMER_ID)),
    ("get_asset_group_signals", "asset_group_signal", "signals", "asset_group_signal.asset_group", rn("assetGroups", "802")),
    ("list_audiences", "audience", "audiences", "audience.resource_name", rn("audiences", "901", h.OTHER_CUSTOMER_ID)),
])
def test_signal_read_rejects_or_excludes_foreign_result_rows(tmp_path, tool, resource, key, field, value):
    server, provider = setup(tmp_path, TOOLS[:2], read_only=True)
    provider.corrupt[resource] = lambda rows: [changed(row, field, value) for row in rows]
    payload = h.call(server, tool, {"asset_group_id": "801"} if tool == "get_asset_group_signals" else {})
    if "error" in payload:
        assert h.error_of(payload)["code"] != "INTERNAL"
    else:
        assert payload[key] == []
    assert not provider.mutations
