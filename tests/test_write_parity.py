"""Write-surface parameter behavior and project requirements coverage."""

import json

import pytest

import harness
from pmax_oracle import PMAX_ADDITIONS, SEARCH_URL_ADDITIONS, expected_pending
from shared_targeting_oracle import ADDITIONS as TARGETING_ADDITIONS
from pmax_experiment_oracle import ADDITIONS as EXPERIMENT_ADDITIONS
from campaign_networks_oracle import TOOL as NETWORK_TOOL
from tool_catalog import MUTATION_ARGS, OPTIONAL_ARGS

from capability_oracle import DEFAULT, cli, compare, empty, record, requirement, success
from tool_catalog import ALL_WRITE_MODE_TOOLS


def _plan(server, tool, extra):
    payload = harness.call(server, tool, {**MUTATION_ARGS[tool], **extra})
    assert "error" not in payload, f"{tool} refused {extra}: {payload.get('error')}"
    return payload["plan"]


@pytest.mark.parametrize("tool", sorted(OPTIONAL_ARGS))
def test_every_added_parameter_is_declared(tmp_path, account_client, tool):
    """Declared, not merely tolerated.

    The MCP argument layer silently DROPS undeclared parameters, so a tool
    that ignores a new argument still returns a happy plan. Asserting the
    parameter reaches the wire schema is what makes this test mean anything.
    """
    server = harness.build_rw_server(tmp_path, client=account_client)
    schema = harness.tool_map(server)[tool].input_schema or {}
    declared = set((schema.get("properties") or {}))
    missing = set(OPTIONAL_ARGS[tool]) - declared
    assert not missing, f"{tool} does not declare {sorted(missing)} — it would be dropped"


@pytest.mark.parametrize("tool", sorted(OPTIONAL_ARGS))
def test_every_added_parameter_reaches_the_plan(tmp_path, account_client, tool):
    """And its VALUE must show up in the staged operations."""
    server = harness.build_rw_server(tmp_path, client=account_client)
    plan = _plan(server, tool, OPTIONAL_ARGS[tool])
    text = json.dumps(plan)
    for key, value in OPTIONAL_ARGS[tool].items():
        needle = value[0] if isinstance(value, list) else value
        assert str(needle) in text or str(needle).upper() in text.upper(), (
            f"{tool}: {key}={value!r} never appears in the plan — it was accepted "
            f"and ignored"
        )


def test_bidding_strategy_is_a_masked_campaign_update(tmp_path, account_client):
    server = harness.build_rw_server(tmp_path, client=account_client)
    plan = _plan(server, "update_campaign", {"bidding_strategy": "MAXIMIZE_CONVERSIONS"})
    masks = [p for op in plan["operations"] for p in op.get("update_mask", [])]
    assert any("maximize_conversions" in m for m in masks), masks
    # switching away must clear the previous strategy's target, not strand it
    assert any("target_roas" in m for m in masks), (
        f"switching strategy must clear the old target: {masks}"
    )


def test_geo_and_language_are_criterion_adds_not_masked_updates(tmp_path, account_client):
    server = harness.build_rw_server(tmp_path, client=account_client)
    plan = _plan(server, "update_campaign",
                 {"geo_target_ids": ["2840"], "language_ids": ["1000"]})
    text = json.dumps(plan["operations"])
    assert "criteri" in text.lower(), f"geo/language are CampaignCriterion creates: {text}"
    for op in plan["operations"]:
        if "geo" in json.dumps(op).lower():
            assert not op.get("update_mask"), "criterion adds carry no field mask"


def test_negative_keyword_match_type_is_honoured(tmp_path, account_client):
    server = harness.build_rw_server(tmp_path, client=account_client)
    plan = _plan(server, "add_negative_keywords", {"match_type": "PHRASE"})
    assert "PHRASE" in json.dumps(plan["operations"])


def test_negative_keywords_default_to_exact_not_broad(tmp_path, account_client):
    server = harness.build_rw_server(tmp_path, client=account_client)
    plan = _plan(server, "add_negative_keywords", {})
    assert "EXACT" in json.dumps(plan["operations"]), (
        "the safe default for a negative is EXACT; BROAD over-blocks"
    )


def test_draft_keywords_requires_an_explicit_match_type(tmp_path, account_client):
    server = harness.build_rw_server(tmp_path, client=account_client)
    payload = harness.call(server, "draft_keywords",
                           {"ad_group_id": "201", "keywords": [{"text": "kw"}]})
    err = harness.error_of(payload)
    assert "match_type" in err["message"], (
        "defaulting to BROAD is the widest, highest-spend match type"
    )


def test_new_entities_are_created_paused(tmp_path, account_client):
    server = harness.build_rw_server(tmp_path, client=account_client)
    ag = _plan(server, "create_ad_group", {})
    assert "PAUSED" in json.dumps(ag["operations"]), "ad groups must be created paused"
    pmax = _plan(server, "create_pmax_campaign", {})
    assert "PAUSED" in json.dumps(pmax["operations"]) or "paused" in pmax["summary"].lower()


def test_lookback_window_is_bounded(tmp_path, account_client):
    server = harness.build_rw_server(tmp_path, client=account_client)
    _plan(server, "create_conversion_action", {"click_through_lookback_window_days": 30})
    for bad in (0, 91, -1):
        harness.expect_error(
            server, "create_conversion_action",
            {**MUTATION_ARGS["create_conversion_action"],
             "click_through_lookback_window_days": bad},
            code="INVALID_LOOKBACK_WINDOW")


def test_channel_type_allows_non_search_campaigns(tmp_path, account_client):
    server = harness.build_rw_server(tmp_path, client=account_client)
    plan = _plan(server, "draft_campaign", {"channel_type": "DISPLAY"})
    assert "DISPLAY" in json.dumps(plan)


@pytest.mark.parametrize("tool,bad", [
    ("update_campaign", {"bidding_strategy": "NOT_A_STRATEGY"}),
    ("draft_campaign", {"channel_type": "NOT_A_CHANNEL"}),
    ("add_negative_keywords", {"match_type": "NOT_A_MATCH"}),
    ("update_ad_group", {"ad_rotation_mode": "NOT_A_MODE"}),
    ("create_conversion_action", {"counting_type": "NOT_A_COUNT"}),
])
def test_added_parameters_validate_at_plan_time(tmp_path, account_client, tool, bad):
    server = harness.build_rw_server(tmp_path, client=account_client)
    payload = harness.call(server, tool, {**MUTATION_ARGS[tool], **bad})
    harness.error_of(payload)
    assert account_client.mutations == [], "validation must precede any API call"


# The workflow checker complements the behavioral assertions above.


def test_authored_requirements_cover_the_public_tool_set():
    data = json.loads(DEFAULT.read_text())
    names = [tool["name"] for capability in data["capabilities"] for tool in capability["tools"]]
    assert len(names) == len(set(names))
    assert set(names) == ALL_WRITE_MODE_TOOLS | PMAX_ADDITIONS | SEARCH_URL_ADDITIONS | TARGETING_ADDITIONS | EXPERIMENT_ADDITIONS | {NETWORK_TOOL}


def test_preview_bypass_names_are_forbidden_even_in_custom_contracts():
    data = json.loads(DEFAULT.read_text())
    names = {"bypass_require_dry_run", "confirmed_twice"}
    assert set(data["forbidden_parameters"]) == names
    actual = [record(), record({"properties": {name: {"type": "boolean"} for name in names}}, "extra")]
    assert compare(requirement(forbidden=["different_guardrail_override"]), actual) == empty(
        forbidden_parameters=sorted("extra." + name for name in names))


def test_default_requirements_check_reports_exact_pending_additions(tmp_path):
    result, _ = cli(tmp_path, default=True)
    server = harness.build_rw_server(tmp_path, client=harness.FakeGoogleAdsClient())
    success(result, expected_pending(server))


def test_requirements_check_detects_missing_tools_and_parameters():
    value = requirement(name="run_gaql", parameters=["query", "customer_id"])
    value["capabilities"][0]["tools"].append({"name": "discover_keywords", "parameters": [], "required": [], "values": {}})
    actual = [record({"properties": {"query": {}}}, "run_gaql")]
    assert compare(value, actual) == empty(missing_tools=["discover_keywords"], missing_parameters=["run_gaql.customer_id"])
