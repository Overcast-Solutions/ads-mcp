"""Positive and discriminating controls for the synthetic URL test transport."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

import harness as h
from search_url_oracle import (AFTER_FINAL, BEFORE_FINAL, BEFORE_MOBILE, FIXTURES, KINDS, SearchClient,
    TRACKING, assert_plan, assert_provider_queries, entity, rn, standard_data)


@pytest.mark.parametrize("kind", ["ad", "keyword"])
def test_authored_rows_and_goldens_bootstrap_with_genuine_v25_messages(kind):
    fixture = h.load_contract_fixture(FIXTURES / (KINDS[kind]["read"] + ".json"))
    assert fixture["tool"] == KINDS[kind]["read"]
    for customer in (h.CUSTOMER_ID, h.OTHER_CUSTOMER_ID):
        for rows in standard_data(customer).values():
            for value in rows:
                row = h.make_row(value)
                assert row._pb.DESCRIPTOR.full_name == "google.ads.googleads.v25.services.GoogleAdsRow"
    request = h.get_ads_type(KINDS[kind]["request"])
    assert request._pb.DESCRIPTOR.full_name.endswith("." + KINDS[kind]["request"])


@pytest.mark.parametrize("customer", [h.CUSTOMER_ID, h.OTHER_CUSTOMER_ID])
@pytest.mark.parametrize("negative", ["FALSE", "TRUE"])
def test_transport_scopes_boolean_filters_without_python_spelling_leak(customer, negative):
    provider = SearchClient()
    provider.data[customer]["ad_group_criterion"][1]["ad_group_criterion"]["negative"] = True
    query = ("SELECT ad_group_criterion.criterion_id, ad_group_criterion.negative, ad_group_criterion.keyword.text "
             "FROM ad_group_criterion WHERE ad_group_criterion.negative = " + negative +
             " AND ad_group_criterion.ad_group = '" + rn("adGroups", "801", customer) + "' LIMIT 2")
    rows = list(provider.get_service("GoogleAdsService").search(customer_id=customer, query=query))
    assert len(rows) == 1
    assert rows[0].ad_group_criterion.criterion_id == (601 if negative == "FALSE" else 602)
    assert rows[0].ad_group_criterion.negative is (negative == "TRUE")
    assert not rows[0].ad_group_criterion.final_urls
    assert rows[0].ad_group_criterion.keyword.text == "trail equipment"
    assert_provider_queries(provider.searches)


def test_transport_keeps_unfiltered_associations_and_honors_removed_filter():
    provider = SearchClient()
    shared = deepcopy(provider.data[h.CUSTOMER_ID]["ad_group_ad"][0])
    shared["ad_group_ad"].update(ad_group=rn("adGroups", "802"), resource_name=rn("adGroupAds", "802~601"))
    provider.data[h.CUSTOMER_ID]["ad_group_ad"].append(shared)
    query = ("SELECT ad_group_ad.resource_name, ad_group_ad.ad.id, ad_group_ad.status FROM ad_group_ad "
             "WHERE ad_group_ad.ad.id = 601 AND ad_group_ad.status != 'REMOVED' LIMIT 2")
    service = provider.get_service("GoogleAdsService")
    assert len(list(service.search(customer_id=h.CUSTOMER_ID, query=query))) == 2
    shared["ad_group_ad"]["status"] = "REMOVED"
    assert len(list(service.search(customer_id=h.CUSTOMER_ID, query=query))) == 1
    assert_provider_queries(provider.searches)


def test_transport_faults_follow_filtering_and_iteration_is_lazy():
    provider = SearchClient()
    provider.corrupt["ad_group_criterion"] = lambda rows: [deepcopy(rows[0])] * 100
    query = ("SELECT ad_group_criterion.resource_name, ad_group_criterion.criterion_id FROM ad_group_criterion "
             "WHERE ad_group_criterion.criterion_id = 601 LIMIT 2")
    rows = provider.get_service("GoogleAdsService").search(customer_id=h.CUSTOMER_ID, query=query)
    assert provider.pulls["ad_group_criterion"] == 0
    assert len(list(rows)) == 2 and provider.pulls["ad_group_criterion"] == 2


@pytest.mark.parametrize("query,field", [
    ("SELECT ad_group_ad.ad.responsive_search_ad FROM ad_group_ad", "ad_group_ad.ad.responsive_search_ad"),
    ("SELECT ad_group_ad.ad.url_custom_parameters FROM ad_group_ad WHERE ad_group_ad.ad.url_custom_parameters IS NOT NULL", "ad_group_ad.ad.url_custom_parameters"),
    ("SELECT ad_group_criterion.final_urls FROM ad_group_criterion ORDER BY ad_group_criterion.final_urls", "ad_group_criterion.final_urls"),
    ("SELECT campaign.id FROM customer", "campaign.id"),
])
def test_reporting_oracle_rejects_unqueryable_or_unattributed_fields(query, field):
    with pytest.raises(AssertionError, match=field):
        assert_provider_queries([SimpleNamespace(query=query)])


def test_reporting_oracle_accepts_documented_leaves_without_interpreting_literals():
    query = ("SELECT ad_group_ad.ad.responsive_search_ad.headlines, ad_group_ad.ad.responsive_search_ad.descriptions, "
             "ad_group_ad.ad.final_urls, campaign.status FROM ad_group_ad "
             "WHERE ad_group_ad.ad.id = 601 AND ad_group_ad.ad.name != 'campaign.no_such_field' "
             "ORDER BY ad_group_ad.ad.id LIMIT 2")
    assert_provider_queries([SimpleNamespace(query=query)])


def test_review_oracle_distinguishes_before_from_after():
    before = {"final_urls": BEFORE_FINAL, "final_mobile_urls": BEFORE_MOBILE}
    after = {"final_urls": AFTER_FINAL, "final_mobile_urls": BEFORE_MOBILE}
    plan = {"before": before, "after": after, "update_mask": ["final_urls"],
            "campaign": rn("campaigns", "701"), "ad_group": rn("adGroups", "801"),
            "context": ["SEARCH", "SEARCH_STANDARD", "ENABLED"], **TRACKING}
    assert_plan(plan, "ad", before, after, ["final_urls"])
    plan["before"], plan["after"] = after, before
    with pytest.raises(AssertionError):
        assert_plan(plan, "ad", before, after, ["final_urls"])


@pytest.mark.parametrize("kind", ["ad", "keyword"])
def test_transport_updates_only_genuine_masked_destinations(kind):
    provider = SearchClient()
    spec = KINDS[kind]
    snapshot = deepcopy(provider.data)
    request = h.get_ads_type(spec["request"])
    request.customer_id = h.CUSTOMER_ID
    operation = h.get_ads_type("AdOperation" if kind == "ad" else "AdGroupCriterionOperation")
    operation.update.resource_name = rn(spec["path"], "601" if kind == "ad" else "801~601")
    operation.update.final_urls.extend(AFTER_FINAL)
    operation.update_mask.paths.append("final_urls")
    request.operations.append(operation)
    getattr(provider.get_service(spec["service"]), spec["method"])(request=request)
    expected = snapshot[h.CUSTOMER_ID][spec["resource"]][0]
    expected = expected["ad_group_ad"]["ad"] if kind == "ad" else expected["ad_group_criterion"]
    expected["final_urls"] = AFTER_FINAL
    assert provider.data == snapshot
    assert len(provider.live_mutations()) == 1


def test_installed_transport_bootstraps_and_preserves_existing_health(tmp_path, monkeypatch):
    import test_auth_cause_contract as process_plumbing
    from test_search_url_workflow_contract import INSTALLED_INJECTION
    monkeypatch.setattr(process_plumbing, "INJECTION", INSTALLED_INJECTION)
    with process_plumbing.InstalledServer(tmp_path / "installed") as installed:
        result = h.expect_ok(installed.call("health_check"))
        assert result
        queries = [SimpleNamespace(**event) for event in installed.events("search-url query")]
        assert_provider_queries(queries)
        assert not installed.events("search-url mutation")


@pytest.mark.parametrize("module_name", ["test_search_ad_urls_contract", "test_keyword_urls_contract"])
def test_adversarial_state_fixtures_are_valid_sdk_messages(module_name):
    import importlib
    from search_url_oracle import set_path
    module = importlib.import_module(module_name)
    for resource, path, value in module.PARENT_INVALID + module.ENTITY_INVALID + module.DRIFT_FIELDS:
        row = deepcopy(standard_data(h.CUSTOMER_ID)[resource][0])
        set_path(row, path, value)
        h.make_row(row)
