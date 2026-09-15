"""F024: complete creation graph and prerequisites, through public MCP tools.

Offline SDK requests establish request fidelity, not Google policy acceptance.
"""
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
import re

import pytest
from google.api_core.exceptions import ServiceUnavailable
from google.protobuf.json_format import MessageToDict

import harness as h
from tool_catalog import MUTATION_ARGS


def pmax(**changes):
    return {**deepcopy(MUTATION_ARGS["create_pmax_campaign"]), **changes}


def stage(server, tool="create_pmax_campaign", args=None):
    return h.expect_ok(h.call(server, tool, pmax() if args is None else args))["plan"]


def apply(server, plan):
    preview = h.expect_ok(h.call(server, "confirm_and_apply", {"plan_id": plan["id"], "dry_run": True}))
    assert preview["applied"] is False
    assert preview["plan"]["operations"] == plan["operations"]
    return h.call(server, "confirm_and_apply", {"plan_id": plan["id"], "dry_run": False})


def refused(payload, path, client, *, no_reads=True):
    error = h.error_of(payload)
    assert re.fullmatch(r"[A-Z][A-Z0-9_]+", error["code"])
    assert error["code"] != "INTERNAL" and error["message"]
    assert "Traceback" not in json.dumps(payload)
    assert not client.mutations
    if no_reads:
        assert not client.searches and not client.planner_calls()
    records = h.read_audit_records(path)
    assert any(r["event"] == "refused" and r["outcome"] == error["code"] for r in records)
    h.assert_no_secrets(json.dumps(records) + json.dumps(payload))


def aggregate_creates(request):
    found = []
    for wrapper in request.mutate_operations:
        field = wrapper._pb.WhichOneof("operation")
        assert field is not None
        operation = getattr(wrapper, field)
        assert operation._pb.WhichOneof("operation") == "create"
        found.append((field.removesuffix("_operation"), operation.create))
    return found


def all_strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for child in value.values():
            yield from all_strings(child)
    elif isinstance(value, list):
        for child in value:
            yield from all_strings(child)


@pytest.mark.parametrize("declaration,start_paused", [(False, True), (True, False)])
def test_pmax_entire_graph_is_one_atomic_sdk_request(tmp_path, account_client, declaration, start_paused):
    server = h.build_rw_server(tmp_path, client=account_client,
                               env={"ADS_MCP_REQUIRE_DRY_RUN": "true"})
    args = pmax(contains_eu_political_advertising=declaration, start_paused=start_paused,
                geo_target_ids=["2840", "2124"])
    plan = stage(server, args=args)
    assert not account_client.mutations
    assert account_client.searches, "explicit asset prerequisites were never verified"
    assert all(s.customer_id == h.CUSTOMER_ID for s in account_client.searches)
    query = " ".join(s.query for s in account_client.searches)
    for field in ("asset.id", "asset.resource_name", "asset.type", "asset.image_asset.file_size",
                  "asset.image_asset.full_size.width_pixels", "asset.image_asset.full_size.height_pixels"):
        assert field in query
    text = json.dumps(plan)
    assert '"contains_eu_political_advertising": ' + str(declaration).lower() in text
    for key in ("landscape_image_asset_ids", "square_image_asset_ids", "logo_asset_ids"):
        assert key in text and all(v in text for v in args[key])
    h.expect_error(server, "confirm_and_apply", {"plan_id": plan["id"], "dry_run": False}, code="DRY_RUN_REQUIRED")
    before_reads = len(account_client.searches)
    result = h.expect_ok(apply(server, plan))
    assert result["applied"] is True
    assert len(account_client.searches) > before_reads
    assert len(account_client.mutations) == 1
    call = account_client.mutations[0]
    assert (call.service, call.method) == ("GoogleAdsService", "mutate")
    request = call.request
    assert request._pb.DESCRIPTOR.full_name.endswith(".MutateGoogleAdsRequest")
    assert request.customer_id == h.CUSTOMER_ID
    assert request.partial_failure is False and request.validate_only is False
    creates = aggregate_creates(request)
    assert Counter(k for k, _ in creates) == {
        "campaign_budget": 1, "campaign": 1, "asset": 7,
        "asset_group": 1, "asset_group_asset": 10, "campaign_criterion": 2}
    groups = {k: [m for kind, m in creates if kind == k] for k, _ in creates}
    budget, campaign, group = groups["campaign_budget"][0], groups["campaign"][0], groups["asset_group"][0]
    assert budget.amount_micros == 10_000_000 and budget.explicitly_shared is False
    assert budget._pb.HasField("explicitly_shared")
    assert campaign.campaign_budget == budget.resource_name
    assert campaign.advertising_channel_type.name == "PERFORMANCE_MAX"
    assert campaign.status.name == ("PAUSED" if start_paused else "ENABLED")
    assert campaign.contains_eu_political_advertising.name == (
        "CONTAINS_EU_POLITICAL_ADVERTISING" if declaration else "DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING")
    assert campaign.brand_guidelines_enabled is False
    assert campaign._pb.HasField("brand_guidelines_enabled")
    assert campaign._pb.HasField("maximize_conversion_value")
    assert group.campaign == campaign.resource_name
    assert group.status.name == campaign.status.name
    assert list(group.final_urls) == args["final_urls"]
    created_texts = {a.resource_name: a.text_asset.text for a in groups["asset"]}
    actual_links = Counter((a.field_type.name, created_texts.get(a.asset, a.asset))
                           for a in groups["asset_group_asset"])
    expected_links = Counter(
        [("HEADLINE", t) for t in args["headlines"]]
        + [("LONG_HEADLINE", t) for t in args["long_headlines"]]
        + [("DESCRIPTION", t) for t in args["descriptions"]]
        + [("BUSINESS_NAME", args["business_name"]),
           ("MARKETING_IMAGE", f"customers/{h.CUSTOMER_ID}/assets/801"),
           ("SQUARE_MARKETING_IMAGE", f"customers/{h.CUSTOMER_ID}/assets/802"),
           ("LOGO", f"customers/{h.CUSTOMER_ID}/assets/803")])
    assert actual_links == expected_links
    assert all(a.asset_group == group.resource_name for a in groups["asset_group_asset"])
    assert {a.location.geo_target_constant for a in groups["campaign_criterion"]} == {"geoTargetConstants/2840", "geoTargetConstants/2124"}
    assert all(a.campaign == campaign.resource_name for a in groups["campaign_criterion"])
    # Global negative IDs are unique; every reference resolves backwards.
    seen, negative_ids = set(), set()
    for kind, message in creates:
        raw = MessageToDict(message._pb, preserving_proto_field_name=True)
        resource = raw.pop("resource_name", "")
        for value in all_strings(raw):
            if re.search(r"/-\d+$", value):
                assert value in seen, f"forward or dangling temporary reference: {value}"
        if kind in {"campaign_budget", "campaign", "asset", "asset_group"}:
            assert re.fullmatch(r"customers/" + h.CUSTOMER_ID + r"/[A-Za-z]+/-\d+", resource)
            temporary = int(resource.rsplit("/", 1)[1])
            assert temporary not in negative_ids
            negative_ids.add(temporary)
            seen.add(resource)
    campaign_index = next(i for i, (kind, _) in enumerate(creates, 1) if kind == "campaign")
    expected_campaign = f"customers/{h.CUSTOMER_ID}/campaigns/{1000 + campaign_index}"
    assert expected_campaign in set(all_strings(result)), "applied result must expose the created campaign resource"
    audit = h.read_audit_records(tmp_path)
    steps = [r for r in audit if r["event"] == "step_applied"]
    assert len(steps) == 1 and steps[0]["operations"] == len(creates)
    assert steps[0]["service"] == "GoogleAdsService" and steps[0]["method"] == "mutate"
    assert steps[0]["plan_id"] == plan["id"]
    assert {"plan_created", "dry_run", "applied"} <= {r["event"] for r in audit}
    h.assert_no_secrets(json.dumps(audit))


@pytest.mark.parametrize("field", ["contains_eu_political_advertising", "landscape_image_asset_ids", "square_image_asset_ids", "logo_asset_ids"])
def test_missing_prerequisite_is_audited_before_calls(tmp_path, account_client, field):
    server = h.build_rw_server(tmp_path, client=account_client)
    args = pmax()
    del args[field]
    refused(h.call(server, "create_pmax_campaign", args), tmp_path, account_client)


@pytest.mark.parametrize("changes", [
    {"landscape_image_asset_ids": []}, {"square_image_asset_ids": []}, {"logo_asset_ids": []},
    {"landscape_image_asset_ids": ["801", "801"]},
    {"square_image_asset_ids": ["802", "802"]}, {"logo_asset_ids": ["803", "803"]},
    {"landscape_image_asset_ids": [str(i) for i in range(1, 22)]},
    {"square_image_asset_ids": [str(i) for i in range(1, 22)]},
    {"logo_asset_ids": [str(i) for i in range(1, 7)]},
    {"landscape_image_asset_ids": ["junk"]}, {"logo_asset_ids": ["-803"]}, {"logo_asset_ids": ["0"]},
    {"square_image_asset_ids": [f"customers/{h.OTHER_CUSTOMER_ID}/assets/802"]},
    {"headlines": ["a", "b"]}, {"headlines": ["x"] * 16},
    {"headlines": ["a" * 16] * 3}, {"headlines": ["a" * 31, "b", "c"]},
    {"long_headlines": []}, {"long_headlines": ["x"] * 6}, {"long_headlines": ["x" * 91]},
    {"descriptions": ["one"]}, {"descriptions": ["x"] * 6},
    {"descriptions": ["x" * 61] * 2}, {"descriptions": ["x" * 91, "short"]},
    {"business_name": ""}, {"business_name": "x" * 26},
    {"final_urls": []}, {"final_urls": [""]}, {"final_urls": ["not-a-url"]}, {"final_urls": ["https://"]},
    {"headlines": ["界" * 15 + "a", "short", "ok"]},
    {"headlines": ["界" * 8] * 3},
    {"long_headlines": ["界" * 45 + "a"]},
    {"descriptions": ["界" * 30 + "a"] * 2},
    {"business_name": "界" * 13},
])
def test_pmax_invalid_inputs_precede_asset_reads(tmp_path, account_client, changes):
    server = h.build_rw_server(tmp_path, client=account_client)
    refused(h.call(server, "create_pmax_campaign", pmax(**changes)), tmp_path, account_client)


@pytest.mark.parametrize("asset_index,width,height,size,valid", [
    (0, 600, 314, 5120000, True), (0, 1200, 628, 5120000, True),
    (0, 765, 400, 5120000, True), (0, 766, 400, 5120000, False),
    (0, 763, 400, 5120000, True), (0, 762, 400, 5120000, False),
    (0, 599, 314, 5120000, False), (0, 600, 313, 5120000, False),
    (1, 300, 300, 5120000, True), (1, 299, 299, 100, False),
    (1, 301, 300, 100, False), (2, 128, 128, 5120000, True),
    (2, 127, 127, 100, False), (2, 128, 129, 100, False),
    (0, 1200, 628, 5120001, False), (0, 0, 628, 100, False),
    (0, 1200, 0, 100, False), (0, 1200, 628, 0, False),
    (0, -1, 628, 100, False), (0, 1200, 628, -1, False),
])
def test_image_metadata_boundaries(tmp_path, account_client, asset_index, width, height, size, valid):
    asset = account_client._responses["asset"][asset_index].asset
    asset.image_asset.full_size.width_pixels = width
    asset.image_asset.full_size.height_pixels = height
    asset.image_asset.file_size = size
    server = h.build_rw_server(tmp_path, client=account_client)
    result = h.call(server, "create_pmax_campaign", pmax())
    if valid:
        plan = h.expect_ok(result)["plan"]
        assert account_client.searches
        h.expect_ok(apply(server, plan))
        assert len(account_client.mutations) == 1
    else:
        refused(result, tmp_path, account_client, no_reads=False)


@pytest.mark.parametrize("fault", ["empty", "wrong_type", "wrong_account", "wrong_id", "failure", "missing_metadata", "dimensions", "oversize"])
@pytest.mark.parametrize("at_apply", [False, True])
def test_asset_lookup_cannot_authorize_missing_or_changed_resources(tmp_path, account_client, fault, at_apply):
    server = h.build_rw_server(tmp_path, client=account_client)
    plan = stage(server) if at_apply else None
    row = account_client._responses["asset"][0]
    if fault == "empty":
        account_client._responses["asset"] = []
    elif fault == "wrong_type":
        row.asset.type_ = account_client.enums.AssetTypeEnum.TEXT
    elif fault == "wrong_account":
        row.asset.resource_name = f"customers/{h.OTHER_CUSTOMER_ID}/assets/801"
    elif fault == "wrong_id":
        row.asset.id = 999
    elif fault == "failure":
        account_client.stub_error(RuntimeError("synthetic lookup unavailable"))
    elif fault == "dimensions":
        row.asset.image_asset.full_size.width_pixels = 10
    elif fault == "oversize":
        row.asset.image_asset.file_size = 5120001
    else:
        row.asset._pb.ClearField("image_asset")
    result = apply(server, plan) if at_apply else h.call(server, "create_pmax_campaign", pmax())
    refused(result, tmp_path, account_client, no_reads=False)


def test_maximum_role_counts_and_double_width_text_apply(tmp_path, account_client):
    rows, role_ids = [], {}
    for key, count, dimensions, start in [
        ("landscape_image_asset_ids", 20, (600, 314), 1100),
        ("square_image_asset_ids", 20, (300, 300), 1200),
        ("logo_asset_ids", 5, (128, 128), 1300),
    ]:
        role_ids[key] = [str(start + i) for i in range(count)]
        for identity in role_ids[key]:
            rows.append({"asset": {"id": int(identity), "type_": "IMAGE",
                "resource_name": f"customers/{h.CUSTOMER_ID}/assets/{identity}",
                "image_asset": {"file_size": 5120000, "full_size": {
                    "width_pixels": dimensions[0], "height_pixels": dimensions[1]}}}})
    account_client.stub("asset", rows)
    args = pmax(**role_ids, headlines=["界" * 7 + "a"] + ["界" * 14 + f"{i:02}" for i in range(14)],
                long_headlines=["Ａ" * 44 + f"{i:02}" for i in range(5)],
                descriptions=["界" * 30] + ["界" * 44 + f"{i:02}" for i in range(4)],
                business_name="界" * 12 + "a")
    server = h.build_rw_server(tmp_path, client=account_client)
    result = h.expect_ok(apply(server, stage(server, args=args)))
    assert result["applied"] and len(account_client.mutations) == 1
    creates = aggregate_creates(account_client.mutations[0].request)
    links = [m for k, m in creates if k == "asset_group_asset"]
    assert Counter(m.field_type.name for m in links) == {
        "HEADLINE": 15, "LONG_HEADLINE": 5, "DESCRIPTION": 5, "BUSINESS_NAME": 1,
        "MARKETING_IMAGE": 20, "SQUARE_MARKETING_IMAGE": 20, "LOGO": 5}


def test_pmax_single_use_under_concurrency(tmp_path, account_client):
    server = h.build_rw_server(tmp_path, client=account_client)
    plan = stage(server)
    h.expect_ok(h.call(server, "confirm_and_apply", {"plan_id": plan["id"], "dry_run": True}))
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda _: h.call(server, "confirm_and_apply", {"plan_id": plan["id"], "dry_run": False}), range(6)))
    assert sum(r.get("applied", False) for r in results) == 1
    assert len(account_client.mutations) == 1
    assert [r["error"]["code"] for r in results if "error" in r] == ["PLAN_CONSUMED"] * 5


def test_pmax_mutation_is_not_retried(tmp_path, account_client, monkeypatch):
    server = h.build_rw_server(tmp_path, client=account_client)
    plan = stage(server)
    original = account_client._do_mutation
    def fail(*args, **kwargs):
        original(*args, **kwargs)
        raise ServiceUnavailable("synthetic lost response")
    monkeypatch.setattr(account_client, "_do_mutation", fail)
    h.error_of(apply(server, plan))
    assert len(account_client.mutations) == 1
    assert account_client.mutations[0].method == "mutate"
    audit = h.read_audit_records(tmp_path)
    assert not any(r["event"] in {"applied", "step_applied"} for r in audit)
    assert any(r["event"] == "apply_failed" for r in audit)


@pytest.mark.parametrize("tool", ["draft_campaign", "create_pmax_campaign"])
def test_creation_declares_political_input_and_refuses_omission(tmp_path, account_client, tool):
    server = h.build_rw_server(tmp_path, client=account_client)
    assert "contains_eu_political_advertising" in h.tool_map(server)[tool].input_schema["properties"]
    args = deepcopy(MUTATION_ARGS[tool])
    del args["contains_eu_political_advertising"]
    refused(h.call(server, tool, args), tmp_path, account_client)


@pytest.mark.parametrize("channel", ["SEARCH", "PERFORMANCE_MAX"])
@pytest.mark.parametrize("declaration", [False, True])
def test_draft_preserves_explicit_declaration_and_supported_channels(tmp_path, account_client, channel, declaration):
    server = h.build_rw_server(tmp_path, client=account_client)
    args = {**MUTATION_ARGS["draft_campaign"], "channel_type": channel,
            "contains_eu_political_advertising": declaration, "status": "ENABLED"}
    if channel == "SEARCH":
        args.update(ad_group_name="Example group", keywords=[{"text": "widgets", "match_type": "EXACT"}])
    plan = stage(server, "draft_campaign", args)
    assert '"contains_eu_political_advertising": ' + str(declaration).lower() in json.dumps(plan)
    h.expect_ok(apply(server, plan))
    messages = [op.create for call in account_client.mutations for op in call.request.operations]
    campaign = next(m for m in messages if m._pb.DESCRIPTOR.name == "Campaign")
    assert campaign.contains_eu_political_advertising.name == (
        "CONTAINS_EU_POLITICAL_ADVERTISING" if declaration else "DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING")
    assert campaign.status.name == "ENABLED"
    assert campaign.advertising_channel_type.name == channel
    assert campaign._pb.HasField("maximize_conversions")
    if channel == "PERFORMANCE_MAX":
        assert campaign.brand_guidelines_enabled is False
        assert campaign._pb.HasField("brand_guidelines_enabled")
        budget = next(m for m in messages if m._pb.DESCRIPTOR.name == "CampaignBudget")
        assert budget.explicitly_shared is False
        assert budget._pb.HasField("explicitly_shared")
        assert not any(m._pb.DESCRIPTOR.name in {"AssetGroup", "AdGroup", "AdGroupCriterion"} for m in messages)
        summary = plan["summary"].lower()
        assert "shell" in summary and "asset group" in summary and ("no" in summary or "without" in summary)
    else:
        group = next(m for m in messages if m._pb.DESCRIPTOR.name == "AdGroup")
        keyword = next(m for m in messages if m._pb.DESCRIPTOR.name == "AdGroupCriterion")
        assert group.status.name == "ENABLED" and keyword.status.name == "ENABLED"


@pytest.mark.parametrize("children", [{"ad_group_name": "Example"}, {"ad_group_name": "Example", "keywords": [{"text": "widget", "match_type": "EXACT"}]}])
def test_pmax_shell_refuses_incompatible_children(tmp_path, account_client, children):
    server = h.build_rw_server(tmp_path, client=account_client)
    args = {**MUTATION_ARGS["draft_campaign"], "channel_type": "PERFORMANCE_MAX", **children}
    result = h.call(server, "draft_campaign", args)
    refused(result, tmp_path, account_client)
    assert "create_pmax_campaign" in result["error"]["message"]


def test_recorded_adapter_uses_real_aggregate_result_oneofs(account_client):
    from guardrail_checks import _wire_operations
    request = h.get_ads_type("MutateGoogleAdsRequest")
    request.customer_id = h.CUSTOMER_ID
    for kind in ("campaign_budget", "campaign", "asset", "asset_group", "asset_group_asset", "campaign_criterion"):
        op = h.get_ads_type("MutateOperation")
        getattr(op, kind + "_operation").create._pb.SetInParent()
        request.mutate_operations.append(op)
    result = account_client.get_service("GoogleAdsService").mutate(request=request)
    assert len(_wire_operations(request)) == 6
    assert result._pb.DESCRIPTOR.name == "MutateGoogleAdsResponse"
    assert len(result.mutate_operation_responses) == 6
    assert result.mutate_operation_responses[0].campaign_budget_result.resource_name == f"customers/{h.CUSTOMER_ID}/campaignBudgets/1001"
    assert result.mutate_operation_responses[1].campaign_result.resource_name == f"customers/{h.CUSTOMER_ID}/campaigns/1002"
    assert [r._pb.WhichOneof("response") for r in result.mutate_operation_responses] == [
        "campaign_budget_result", "campaign_result", "asset_result", "asset_group_result", "asset_group_asset_result", "campaign_criterion_result"]
    separate = h.get_ads_type("MutateCampaignsRequest")
    separate.operations.append(h.get_ads_type("CampaignOperation"))
    assert len(_wire_operations(separate)) == 1


@pytest.mark.parametrize("predicate", ["asset.id IN (801, 803)", f"asset.resource_name IN ('customers/{h.CUSTOMER_ID}/assets/801', 'customers/{h.CUSTOMER_ID}/assets/803')"])
def test_recorded_asset_projection_retains_real_metadata(account_client, predicate):
    rows = account_client.get_service("GoogleAdsService").search(customer_id=h.CUSTOMER_ID,
        query="SELECT asset.id, asset.resource_name, asset.type, asset.image_asset.full_size.width_pixels, asset.image_asset.full_size.height_pixels, asset.image_asset.file_size FROM asset WHERE " + predicate)
    assert [r.asset.id for r in rows] == [801, 803]
    assert rows[0].asset.type_.name == "IMAGE"
    assert rows[0].asset.image_asset.full_size.width_pixels == 1200
    assert rows[0].asset.image_asset.full_size.height_pixels == 628
    assert rows[0].asset.image_asset.file_size == 5120000


def test_pmax_caps_are_enforced_at_both_gates(tmp_path, account_client):
    from ads_mcp.guardrails import PlanStore
    store = PlanStore(clock=h.FakeClock())
    server = h.build_rw_server(tmp_path, client=account_client, plan_store=store)
    rejected = h.call(server, "create_pmax_campaign", pmax(daily_budget=101))
    refused(rejected, tmp_path, account_client)
    assert rejected["error"]["code"] == "BUDGET_CAP_EXCEEDED"
    plan = stage(server, args=pmax(daily_budget=100))
    lower = h.build_rw_server(tmp_path, client=account_client, plan_store=store,
                               env={"ADS_MCP_MAX_DAILY_BUDGET": "99"})
    rejected = apply(lower, plan)
    refused(rejected, tmp_path, account_client, no_reads=False)
    assert rejected["error"]["code"] == "BUDGET_CAP_EXCEEDED"


def test_pmax_refuses_other_account_before_asset_reads(tmp_path, account_client):
    server = h.build_rw_server(tmp_path, client=account_client)
    result = h.call(server, "create_pmax_campaign", pmax(customer_id=h.OTHER_CUSTOMER_ID))
    refused(result, tmp_path, account_client)
    assert result["error"]["code"] == "PLAN_CUSTOMER_MISMATCH"


def test_draft_default_parent_status_and_description_match_enabled_keywords(tmp_path, account_client):
    server = h.build_rw_server(tmp_path, client=account_client)
    args = {**MUTATION_ARGS["draft_campaign"], "ad_group_name": "Example group",
            "keywords": [{"text": "widgets", "match_type": "EXACT"}]}
    plan = stage(server, "draft_campaign", args)
    h.expect_ok(apply(server, plan))
    messages = [op.create for call in account_client.mutations for op in call.request.operations]
    statuses = {m._pb.DESCRIPTOR.name: m.status.name for m in messages
                if m._pb.DESCRIPTOR.name in {"Campaign", "AdGroup", "AdGroupCriterion"}}
    assert statuses == {"Campaign": "PAUSED", "AdGroup": "PAUSED", "AdGroupCriterion": "ENABLED"}
    description = h.tool_map(server)["draft_campaign"].description.lower()
    assert "enabled" in description and "keyword" in description and "ad group" in description
