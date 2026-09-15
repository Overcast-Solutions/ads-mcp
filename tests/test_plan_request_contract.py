"""F035: exhaustive registered identity inputs and approved amount/creative."""
from copy import deepcopy
import json

import pytest

import harness as h
from offline_contract import apply, audited, refusal, stage, wire
from tool_catalog import MUTATION_ARGS, OPTIONAL_ARGS


def identity_cases():
    cases = []
    for tool, base in MUTATION_ARGS.items():
        args = {**deepcopy(base), **deepcopy(OPTIONAL_ARGS.get(tool, {}))}
        for key in args:
            if key.endswith(("_id", "_ids")):
                cases.append(pytest.param(tool, args, key, id=tool + "-" + key))
    for tool in ("pause_entity", "enable_entity", "remove_entity"):
        for kind, identity in (("ad", "201~901"), ("keyword", "201~401"), ("ad_group", "201")):
            cases.append(pytest.param(tool, {"entity_type": kind, "entity_id": identity}, "entity_id", id=tool + "-" + kind))
    return cases


@pytest.mark.parametrize("tool,args,key", identity_cases())
def test_each_accepted_identifier_has_identical_canonical_plan_and_sdk_request(tmp_path, tool, args, key):
    outcomes = []
    for padded in (False, True):
        path = tmp_path / ("padded" if padded else "canonical")
        path.mkdir()
        client = h.stub_standard_account(h.FakeGoogleAdsClient())
        server = h.build_rw_server(path, client=client)
        supplied = deepcopy(args)
        if padded:
            value = supplied[key]
            supplied[key] = [" \t" + v + "\n " for v in value] if isinstance(value, list) else " \t" + value + "\n "
        plan = stage(server, tool, supplied)
        assert h.expect_ok(apply(server, plan))["applied"]
        assert client.mutations
        outcomes.append((plan["operations"], wire(client)))
        assert all(s.customer_id == h.CUSTOMER_ID for s in client.searches)
        assert all(c.request.customer_id == h.CUSTOMER_ID for c in client.mutations)
    assert outcomes[1] == outcomes[0], f"{tool}.{key}: canonical preview/wire changed when only whitespace changed"


@pytest.mark.parametrize("tool,args,key", identity_cases())
def test_each_identifier_rejects_malformed_before_account_calls(tmp_path, tool, args, key):
    # Recommendation IDs are opaque Google identifiers; their separate full
    # resource account grammar is covered by the existing locked suite.
    if key == "recommendation_id":
        bad = f"customers/{h.OTHER_CUSTOMER_ID}/recommendations/777"
    else:
        bad = "111 OR 1=1"
    supplied = deepcopy(args)
    supplied[key] = [bad] if isinstance(supplied[key], list) else bad
    client = h.stub_standard_account(h.FakeGoogleAdsClient())
    server = h.build_rw_server(tmp_path, client=client)
    error = refusal(h.call(server, tool, supplied), client, no_reads=True)
    audited(tmp_path, error)


@pytest.mark.parametrize("currency", ["EUR", "JPY"])
@pytest.mark.parametrize("tool", ["draft_campaign", "create_pmax_campaign", "create_portfolio_bidding_strategy"])
def test_creation_currency_is_verified_or_explicitly_unspecified_through_apply(tmp_path, account_client, currency, tool):
    for rows in account_client._responses.values():
        for row in rows:
            if row.customer.currency_code:
                row.customer.currency_code = currency
    server = h.build_rw_server(tmp_path, client=account_client)
    plan = stage(server, tool, deepcopy(MUTATION_ARGS[tool]))
    result = h.expect_ok(apply(server, plan))
    for artifact in (plan, result):
        text = json.dumps(artifact)
        assert "USD" not in text, f"{currency} account creation was represented as USD"
        assert currency in text or "account currency" in text.lower()
    money_values = []
    def amounts(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if key in ("amount_micros", "target_cpa_micros"):
                    money_values.append(int(child))
                amounts(child)
        elif isinstance(value, list):
            for child in value:
                amounts(child)
    amounts(wire(account_client))
    assert (5_000_000 if tool == "create_portfolio_bidding_strategy" else 10_000_000) in money_values


@pytest.mark.parametrize("blank", ["", " ", "\t\n", "\u2003\u00a0"])
@pytest.mark.parametrize("kind", ["headlines", "descriptions", "callouts"])
def test_blank_creative_is_named_and_audited_before_any_account_call(tmp_path, account_client, blank, kind):
    tool = "create_callouts" if kind == "callouts" else "draft_responsive_search_ad"
    args = deepcopy(MUTATION_ARGS[tool])
    args[kind][0] = blank
    server = h.build_rw_server(tmp_path, client=account_client)
    error = refusal(h.call(server, tool, args), account_client, no_reads=True)
    audited(tmp_path, error)
