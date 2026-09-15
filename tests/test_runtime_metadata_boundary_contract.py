"""F049: truthful account currency and bounded numeric input via installed MCP.

The neutral console/transport driver has no dependency on F047 repairs. Account
rows and request/forecast messages are genuine SDK protos; accounts and RPC
replies are synthetic. Positive controls deliberately preserve existing policy.
"""
from copy import deepcopy
import json
import re

import pytest
from google.ads.googleads.v25.services.services.ad_group_ad_service import AdGroupAdServiceClient
from google.ads.googleads.v25.services.services.ad_group_criterion_service import AdGroupCriterionServiceClient
from google.ads.googleads.v25.services.services.ad_group_service import AdGroupServiceClient
from google.ads.googleads.v25.services.services.campaign_service import CampaignServiceClient

import harness as h
from test_auth_cause_contract import InstalledServer
from test_identifier_boundary_contract import REPORTS
from tool_catalog import MUTATION_ARGS


ACCOUNTS = (h.CUSTOMER_ID, h.OTHER_CUSTOMER_ID)
METRICS = {"clicks": 2.5, "cost_micros": 5000000, "average_cpc_micros": 2000000}
BIG = "9" * 5000


def forecast(server, customer):
    return server.call("get_keyword_forecasts", {"keywords": ["synthetic neutral seed"], "customer_id": customer})


@pytest.mark.parametrize("customer", ACCOUNTS)
@pytest.mark.parametrize("currency", [None, "", " \t\n "], ids=["absent", "empty", "whitespace"])
def test_unverified_currency_refuses_before_forecast_rpc_after_other_account_success(tmp_path, customer, currency):
    other = next(account for account in ACCOUNTS if account != customer)
    metadata = {"time_zone": "Etc/UTC"}
    if currency is not None:
        metadata["currency_code"] = currency
    with InstalledServer(tmp_path) as server:
        server.mode(accounts={customer: metadata, other: {"time_zone": "Etc/UTC", "currency_code": "EUR"}}, metrics=METRICS)
        control = h.expect_ok(forecast(server, other))
        assert control["forecast"]["cost"] == "5.00 EUR"
        before = len(server.events("forecast"))
        result = forecast(server, customer)
        error = h.error_of(result)
        assert error["code"] == "ACCOUNT_CURRENCY_UNAVAILABLE", result
        assert "currency" in error["message"].lower() and len(error["message"]) <= 600
        assert len(server.events("forecast")) == before, "missing currency must fail before any forecast RPC"
        assert [r["customer_id"] for r in server.events("search")] == [other, customer]
        assert not server.events("mutation")


@pytest.mark.parametrize("metrics", [None, {}, {"clicks": 0, "cost_micros": 0, "average_cpc_micros": 0}, {"clicks": 0.25}, METRICS])
def test_positive_control_two_accounts_keep_currency_presence_and_fractional_metrics(tmp_path, metrics):
    with InstalledServer(tmp_path) as server:
        currencies = {h.CUSTOMER_ID: "USD", h.OTHER_CUSTOMER_ID: "EUR"}
        server.mode(accounts={cid: {"time_zone": "Etc/UTC", "currency_code": code}
                              for cid, code in currencies.items()}, metrics=metrics)
        for customer in [h.OTHER_CUSTOMER_ID, h.CUSTOMER_ID, h.OTHER_CUSTOMER_ID]:
            result = h.expect_ok(forecast(server, customer))
            assert result["customer_id"] == customer
            values = result["forecast"]
            assert values["impressions"] is None
            assert values["clicks"] == (metrics or {}).get("clicks")
            for key, field in [("cost", "cost_micros"), ("average_cpc", "average_cpc_micros")]:
                expected = None if field not in (metrics or {}) else f"{metrics[field] / 1000000:.2f} {currencies[customer]}"
                assert values[key] == expected
            request = server.events("forecast")[-1]
            assert request["request"]["customer_id"] == customer
            assert request["request"]["currency_code"] == currencies[customer]
            assert request["login_customer_id"] == h.LOGIN_CUSTOMER_ID
        assert not server.events("mutation")


def test_positive_control_currency_is_not_a_new_registry_and_time_zone_checks_remain(tmp_path):
    with InstalledServer(tmp_path) as server:
        server.mode(accounts={h.CUSTOMER_ID: {"time_zone": "Etc/UTC", "currency_code": "XYZ"}}, metrics=METRICS)
        assert h.expect_ok(forecast(server, h.CUSTOMER_ID))["forecast"]["cost"] == "5.00 XYZ"
        server.mode(accounts={h.CUSTOMER_ID: {"time_zone": "not/a/timezone", "currency_code": "EUR"}}, metrics=METRICS)
        before = len(server.events("forecast"))
        assert h.error_of(forecast(server, h.CUSTOMER_ID))["code"] == "ACCOUNT_TIME_ZONE_UNAVAILABLE"
        assert len(server.events("forecast")) == before


def bounded_invalid(server, tool, args):
    result = server.call(tool, args)
    error = h.error_of(result)
    assert error["code"] == "INVALID_ID", error
    assert 1 <= len(error["message"]) <= 600, "oversized input must not be echoed wholesale"
    text = json.dumps(result).lower()
    assert any(word in error["message"].lower() for word in ("id", "identifier", "numeric", "digit"))
    for forbidden in ("internal", "traceback", "valueerror", "sys.set_int_max_str_digits", "integer string conversion", "4300 digits", BIG):
        assert forbidden.lower() not in text
    assert not server.events("search") and not server.events("mutation") and not server.events("forecast")
    records = server.audit() if h.audit_file(server.root).exists() else []
    assert "plan" not in result and not [r for r in records if r["event"] == "plan_created"]


def test_positive_control_invalid_shopping_campaign_refuses_without_audit_file(tmp_path):
    with InstalledServer(tmp_path) as server:
        bounded_invalid(server, "get_shopping_performance", {
            **REPORTS["get_shopping_performance"][0],
            "campaign_id": "not-a-campaign-id",
            "customer_id": h.OTHER_CUSTOMER_ID,
        })
    assert not h.audit_file(tmp_path).exists()


@pytest.mark.parametrize("tool", ["pause_entity", "enable_entity", "remove_entity"])
@pytest.mark.parametrize("kind,identifier", [
    ("campaign", BIG), ("ad_group", BIG),
    ("ad", BIG + "~901"), ("ad", "201~" + BIG),
    ("keyword", BIG + "~401"), ("keyword", "201~" + BIG),
], ids=["campaign", "ad-group", "ad-parent", "ad-child", "keyword-parent", "keyword-child"])
def test_oversized_simple_and_composite_lifecycle_ids_refuse_before_calls(tmp_path, tool, kind, identifier):
    with InstalledServer(tmp_path) as server:
        bounded_invalid(server, tool, {"entity_type": kind, "entity_id": identifier})


@pytest.mark.parametrize("tool,args", [
    ("update_campaign", {"campaign_id": BIG, "name": "Neutral rename"}),
    ("update_keyword_bid", {"ad_group_id": BIG, "criterion_id": "401", "current_bid": 1.2, "new_bid": 1.25}),
    ("update_keyword_bid", {"ad_group_id": "201", "criterion_id": BIG, "current_bid": 1.2, "new_bid": 1.25}),
    ("pause_entity", {"entity_type": "campaign", "entity_id": "٩" * 5000}),
])
def test_oversized_shared_mutation_and_decimal_unicode_ids_are_named(tmp_path, tool, args):
    with InstalledServer(tmp_path) as server:
        bounded_invalid(server, tool, args)


@pytest.mark.parametrize("tool", REPORTS)
def test_oversized_campaign_read_scope_never_reaches_account(tmp_path, tool):
    with InstalledServer(tmp_path) as server:
        bounded_invalid(server, tool, {**REPORTS[tool][0], "campaign_id": BIG, "customer_id": h.OTHER_CUSTOMER_ID})


@pytest.mark.parametrize("role", ["landscape_image_asset_ids", "square_image_asset_ids", "logo_asset_ids"])
def test_positive_control_oversized_pmax_asset_ids_already_refuse_cleanly(tmp_path, role):
    args = deepcopy(MUTATION_ARGS["create_pmax_campaign"])
    args[role] = [BIG]
    with InstalledServer(tmp_path) as server:
        bounded_invalid(server, "create_pmax_campaign", args)


ENTITIES = {
    "campaign": ("111", "campaigns", CampaignServiceClient.parse_campaign_path),
    "ad_group": ("201", "adGroups", AdGroupServiceClient.parse_ad_group_path),
    "ad": ("201~901", "adGroupAds", AdGroupAdServiceClient.parse_ad_group_ad_path),
    "keyword": ("201~401", "adGroupCriteria", AdGroupCriterionServiceClient.parse_ad_group_criterion_path),
}


@pytest.mark.parametrize("kind", ENTITIES)
@pytest.mark.parametrize("spelling", ["ordinary", "leading-zero", "unicode"])
def test_positive_control_supported_id_spellings_keep_exact_sdk_identity(tmp_path, kind, spelling):
    canonical, collection, parse = ENTITIES[kind]
    supplied = canonical
    if spelling == "leading-zero":
        supplied = "  " + "~".join("000" + piece for piece in canonical.split("~")) + "  "
    elif spelling == "unicode":
        supplied = canonical.translate(str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩"))
    with InstalledServer(tmp_path) as server:
        plan = h.expect_ok(server.call("pause_entity", {"entity_type": kind, "entity_id": supplied}))["plan"]
        resource = f"customers/{h.CUSTOMER_ID}/{collection}/{canonical}"
        assert plan["operations"][0]["resource"] == resource and parse(resource)
        assert h.expect_ok(server.call("confirm_and_apply", {"plan_id": plan["id"], "dry_run": False}))["applied"]
        requests = server.events("mutation")
        assert len(requests) == 1 and requests[0]["request"]["customer_id"] == h.CUSTOMER_ID
        assert requests[0]["request"]["operations"][0]["update"]["resource_name"] == resource


@pytest.mark.parametrize("identifier", ["111", " 000111 ", "١١١"])
def test_positive_control_campaign_read_scope_remains_canonical(tmp_path, identifier):
    with InstalledServer(tmp_path) as server:
        result = h.expect_ok(server.call("get_campaign_performance", {"campaign_id": identifier, "last_n_days": 1,
            "customer_id": h.OTHER_CUSTOMER_ID}))
        assert result["customer_id"] == h.OTHER_CUSTOMER_ID and result["campaign_id"] == "111"
        assert {row["campaign_id"] for row in result["campaigns"]} == {"111"}
        assert all(re.search(r"campaign\.id\s*=\s*111\b", row["query"]) for row in server.events("search"))
        assert {row["customer_id"] for row in server.events("search")} == {h.OTHER_CUSTOMER_ID}


def test_positive_control_pmax_leading_zeros_preserve_verified_asset_links(tmp_path):
    args = deepcopy(MUTATION_ARGS["create_pmax_campaign"])
    for role, identity in [("landscape_image_asset_ids", "801"), ("square_image_asset_ids", "802"), ("logo_asset_ids", "803")]:
        args[role] = [" 000" + identity + " "]
    with InstalledServer(tmp_path) as server:
        plan = h.expect_ok(server.call("create_pmax_campaign", args))["plan"]
        assert h.expect_ok(server.call("confirm_and_apply", {"plan_id": plan["id"], "dry_run": False}))["applied"]
        operations = server.events("mutation")[0]["request"]["mutate_operations"]
        links = [op["asset_group_asset_operation"]["create"] for op in operations if "asset_group_asset_operation" in op]
        resources = {link["asset"] for link in links}
        assert {f"customers/{h.CUSTOMER_ID}/assets/{identity}" for identity in ("801", "802", "803")} <= resources


@pytest.mark.parametrize("customer", ACCOUNTS)
@pytest.mark.parametrize("tool", ["apply_recommendation", "dismiss_recommendation"])
@pytest.mark.parametrize("identity", ["000777", "Rec_A-09"])
def test_positive_control_opaque_recommendation_identity_is_unchanged(tmp_path, customer, tool, identity):
    with InstalledServer(tmp_path, customer=customer) as server:
        server.mode(recommendations=[identity])
        resource = f"customers/{customer}/recommendations/{identity}"
        plan = h.expect_ok(server.call(tool, {"recommendation_id": " " + resource + " "}))["plan"]
        assert h.expect_ok(server.call("confirm_and_apply", {"plan_id": plan["id"], "dry_run": False}))["applied"]
        request = server.events("mutation")[0]["request"]
        assert request["customer_id"] == customer
        assert request["operations"][0]["resource_name"] == resource
