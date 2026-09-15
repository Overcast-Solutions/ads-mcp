"""F037: enum membership does not promise unsupported creation graphs."""
from copy import deepcopy

import pytest

import harness as h
from offline_contract import ROOT, apply, audited, refusal, stage
from tool_catalog import MUTATION_ARGS


UNSUPPORTED = [name for name in h.FakeGoogleAdsClient().enums.AdvertisingChannelTypeEnum.__members__
               if name not in {"UNSPECIFIED", "UNKNOWN", "SEARCH", "DISPLAY", "PERFORMANCE_MAX"}]


@pytest.mark.parametrize("channel", UNSUPPORTED)
def test_pinned_but_unsupported_channel_refuses_before_reads(tmp_path, account_client, channel):
    server = h.build_rw_server(tmp_path, client=account_client)
    error = refusal(h.call(server, "draft_campaign", {**MUTATION_ARGS["draft_campaign"], "channel_type": channel}), account_client, no_reads=True)
    assert channel in error["message"] and any(w in error["message"].lower() for w in ("support", "setting", "prerequisite"))
    audited(tmp_path, error)


@pytest.mark.parametrize("tool", ["draft_campaign", "create_ad_group", "draft_responsive_search_ad"])
def test_removed_is_not_a_creation_status(tmp_path, account_client, tool):
    server = h.build_rw_server(tmp_path, client=account_client)
    error = refusal(h.call(server, tool, {**deepcopy(MUTATION_ARGS[tool]), "status": "REMOVED"}), account_client, no_reads=True)
    assert "status" in error["code"].lower() or "status" in error["message"].lower()
    assert any(word in error["message"].lower() for word in ("creat", "paused", "enabled"))
    audited(tmp_path, error)


@pytest.mark.parametrize("channel", ["SEARCH", "DISPLAY", "PERFORMANCE_MAX"])
@pytest.mark.parametrize("status", ["ENABLED", "PAUSED"])
def test_supported_shells_keep_real_sdk_creation_graph(tmp_path, account_client, channel, status):
    server = h.build_rw_server(tmp_path, client=account_client)
    plan = stage(server, "draft_campaign", {**MUTATION_ARGS["draft_campaign"], "channel_type": channel, "status": status})
    assert h.expect_ok(apply(server, plan))["applied"]
    campaigns = [op.campaign_operation.create for call in account_client.mutations
                 for op in getattr(call.request, "mutate_operations", []) if op._pb.WhichOneof("operation") == "campaign_operation"]
    campaigns += [op.create for call in account_client.mutations if call.service == "CampaignService"
                  for op in call.request.operations if op._pb.WhichOneof("operation") == "create"]
    assert len(campaigns) == 1
    campaign = campaigns[0]
    assert campaign.advertising_channel_type.name == channel and campaign.status.name == status
    assert campaign.contains_eu_political_advertising.name == "DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING"
    assert campaign.campaign_budget
    if channel == "PERFORMANCE_MAX":
        assert campaign._pb.HasField("brand_guidelines_enabled") and not campaign.brand_guidelines_enabled


def test_creation_supported_subset_is_explicit_in_catalog_and_readme(tmp_path, account_client):
    server = h.build_rw_server(tmp_path, client=account_client)
    description = h.tool_map(server)["draft_campaign"].description
    readme = (ROOT / "README.md").read_text()
    for text in (description, readme):
        assert all(name in text.upper() for name in ("SEARCH", "DISPLAY", "PERFORMANCE_MAX"))
        assert "shell" in text.lower() and any(word in text.lower() for word in ("support", "only", "prerequisite"))
    assert "eligib" in readme.lower() and ("non-retail" in readme.lower() or "nonretail" in readme.lower())
