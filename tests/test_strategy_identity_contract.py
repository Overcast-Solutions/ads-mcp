"""F031: current strategy is selected, retained, and rechecked before writes."""
import json

import pytest
from google.api_core.exceptions import ServiceUnavailable

import harness as h
from offline_contract import ProjectedClient, apply, audited, campaign, refusal, selected, stage

FAMILIES = [
    ("TARGET_CPA", "target_cpa", "target_cpa.target_cpa_micros", 15, 20),
    ("TARGET_ROAS", "target_roas", "target_roas.target_roas", 1.5, 2),
    ("MAXIMIZE_CONVERSIONS", "target_cpa", "maximize_conversions.target_cpa_micros", 15, 20),
    ("MAXIMIZE_CONVERSION_VALUE", "target_roas", "maximize_conversion_value.target_roas", 1.5, 2),
]
REQUIRED = {"campaign.id", "campaign.resource_name", "campaign.bidding_strategy_type", "campaign.bidding_strategy"} | {"campaign." + f[2] for f in FAMILIES}


@pytest.mark.parametrize("family,param,leaf,old,new", FAMILIES)
def test_target_only_preserves_real_strategy_preview_and_wire(tmp_path, family, param, leaf, old, new):
    client = campaign(h.stub_standard_account(ProjectedClient()), family, target=old)
    server = h.build_rw_server(tmp_path, client=client)
    plan = stage(server, "update_campaign", {"campaign_id": "111", param: new})
    fields = set().union(*(set(selected(s.query)) for s in client.searches if "FROM campaign " in s.query))
    assert REQUIRED <= fields, f"strategy decision must select identity and all target leaves: {REQUIRED-fields}"
    operation = next(op for op in plan["operations"] if op["type"] == "update_campaign")
    factor = 1_000_000 if param == "target_cpa" else 1
    assert operation["changes"][leaf] == {"old": old * factor, "new": new * factor}
    assert operation["update_mask"] == [leaf]
    before = len(client.searches)
    assert h.expect_ok(apply(server, plan))["applied"] is True
    assert len(client.searches) > before, "target-only apply must re-read strategy identity"
    op = client.mutations[-1].request.operations[0]
    assert list(op.update_mask.paths) == [leaf]
    assert op.update._pb.WhichOneof("campaign_bidding_strategy") == leaf.split(".")[0]
    assert getattr(getattr(op.update, leaf.split(".")[0]), leaf.split(".")[1]) == new * factor


@pytest.mark.parametrize("family,param,leaf,old,new", FAMILIES)
def test_required_clear_refuses_optional_clear_unsets(tmp_path, family, param, leaf, old, new):
    client = campaign(h.stub_standard_account(ProjectedClient()), family, target=old)
    server = h.build_rw_server(tmp_path, client=client)
    payload = h.call(server, "update_campaign", {"campaign_id": "111", "clear_" + param: True})
    if family.startswith("TARGET_"):
        error = refusal(payload, client)
        assert "strateg" in error["message"].lower() and any(w in error["message"].lower() for w in ("switch", "change", "explicit"))
        audited(tmp_path, error)
    else:
        plan = h.expect_ok(payload)["plan"]
        assert h.expect_ok(apply(server, plan))["applied"]
        op = client.mutations[-1].request.operations[0]
        assert list(op.update_mask.paths) == [leaf]
        parent = getattr(op.update, leaf.split(".")[0])._pb
        assert leaf.split(".")[1] not in {field.name for field, _ in parent.ListFields()}


@pytest.mark.parametrize("family,portfolio,param", [
    ("TARGET_CPA", "customers/9876543210/biddingStrategies/800", "target_cpa"),
    ("UNSPECIFIED", "", "target_roas"), ("UNKNOWN", "", "target_cpa"),
    ("MANUAL_CPC", "", "target_roas"), ("MAXIMIZE_CONVERSIONS", "", "target_roas"),
    ("MAXIMIZE_CONVERSION_VALUE", "", "target_cpa"),
])
def test_unverified_portfolio_or_incompatible_family_never_stages(tmp_path, family, portfolio, param):
    client = campaign(h.stub_standard_account(ProjectedClient()), family, portfolio=portfolio)
    server = h.build_rw_server(tmp_path, client=client)
    error = refusal(h.call(server, "update_campaign", {"campaign_id": "111", param: 2}), client)
    audited(tmp_path, error)


@pytest.mark.parametrize("fault", ["missing", "failed", "id", "resource", "family", "portfolio"])
@pytest.mark.parametrize("edit", [{"target_roas": 2}, {"clear_target_roas": True}])
def test_fresh_strategy_identity_gates_all_combined_operations(tmp_path, fault, edit):
    client = campaign(h.stub_standard_account(ProjectedClient()))
    server = h.build_rw_server(tmp_path, client=client, env={"ADS_MCP_RETRY_BASE_SECONDS": "0.001"})
    plan = stage(server, "update_campaign", {"campaign_id": "111", "daily_budget": 45, "name": "combined", **edit})
    row = client._responses["campaign"][0]
    if fault == "missing":
        client.stub("campaign", [])
    elif fault == "failed":
        client.stub_error(ServiceUnavailable("synthetic state unavailable"))
    elif fault == "id":
        row.campaign.id = 999
    elif fault == "resource":
        row.campaign.resource_name = f"customers/{h.CUSTOMER_ID}/campaigns/999"
    elif fault == "family":
        row.campaign.bidding_strategy_type = client.enums.BiddingStrategyTypeEnum.TARGET_ROAS
    else:
        row.campaign.bidding_strategy = f"customers/{h.CUSTOMER_ID}/biddingStrategies/800"
    error = refusal(apply(server, plan), client)
    audited(tmp_path, error)


@pytest.mark.parametrize("param,value", [("target_roas", .009), ("target_roas", 0), ("target_roas", 1000.01),
    ("target_roas", float("nan")), ("target_roas", float("inf")), ("target_cpa", .0000001),
    ("target_cpa", 0), ("target_cpa", -1), ("target_cpa", float("inf")), ("target_cpa", 1_000_001)])
def test_target_invalid_numbers_are_audited_without_mutation(tmp_path, param, value):
    family = "MAXIMIZE_CONVERSIONS" if param == "target_cpa" else "MAXIMIZE_CONVERSION_VALUE"
    client = campaign(h.stub_standard_account(ProjectedClient()), family)
    server = h.build_rw_server(tmp_path, client=client)
    error = refusal(h.call(server, "update_campaign", {"campaign_id": "111", param: value}), client)
    audited(tmp_path, error)


@pytest.mark.parametrize("param,value", [("target_roas", .01), ("target_roas", 1000), ("target_cpa", .000001), ("target_cpa", 1_000_000)])
def test_target_inclusive_boundaries_and_explicit_switch_apply(tmp_path, account_client, param, value):
    server = h.build_rw_server(tmp_path, client=account_client)
    family = "MAXIMIZE_CONVERSIONS" if param == "target_cpa" else "MAXIMIZE_CONVERSION_VALUE"
    plan = stage(server, "update_campaign", {"campaign_id": "111", "bidding_strategy": family, param: value})
    assert h.expect_ok(apply(server, plan))["applied"]


@pytest.mark.parametrize("family", ["TARGET_CPA", "TARGET_ROAS"])
def test_portfolio_contradictory_target_refuses_before_reads(tmp_path, account_client, family):
    server = h.build_rw_server(tmp_path, client=account_client)
    error = refusal(h.call(server, "create_portfolio_bidding_strategy", {"name": "Synthetic portfolio", "strategy_type": family,
                     "target_cpa": 15, "target_roas": 3}), account_client, no_reads=True)
    audited(tmp_path, error)


@pytest.mark.parametrize("family,param,value", [("TARGET_CPA", "target_cpa", 15), ("TARGET_ROAS", "target_roas", .01), ("TARGET_ROAS", "target_roas", 1000)])
def test_portfolio_single_target_matches_wire(tmp_path, account_client, family, param, value):
    server = h.build_rw_server(tmp_path, client=account_client)
    plan = stage(server, "create_portfolio_bidding_strategy", {"name": "Synthetic", "strategy_type": family, param: value})
    assert h.expect_ok(apply(server, plan))["applied"]
    created = account_client.mutations[-1].request.operations[0].create
    assert created._pb.WhichOneof("scheme") == param
    forbidden = "target_roas" if param == "target_cpa" else "target_cpa"
    assert forbidden not in json.dumps(plan)


@pytest.mark.parametrize("family,param,value", [("TARGET_CPA", "target_cpa", .0000001),
    ("TARGET_CPA", "target_cpa", 0), ("TARGET_CPA", "target_cpa", float("nan")),
    ("TARGET_CPA", "target_cpa", float("inf")), ("TARGET_ROAS", "target_roas", .009),
    ("TARGET_ROAS", "target_roas", 0), ("TARGET_ROAS", "target_roas", float("nan")),
    ("TARGET_ROAS", "target_roas", float("inf")), ("TARGET_ROAS", "target_roas", 1000.01)])
def test_portfolio_invalid_target_boundaries_are_audited(tmp_path, account_client, family, param, value):
    server = h.build_rw_server(tmp_path, client=account_client)
    error = refusal(h.call(server, "create_portfolio_bidding_strategy", {"name": "Synthetic", "strategy_type": family, param: value}), account_client, no_reads=True)
    audited(tmp_path, error)
