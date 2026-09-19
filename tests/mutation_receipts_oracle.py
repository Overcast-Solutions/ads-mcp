"""Finite mutation scenarios and provider-only receipt observation."""
from copy import deepcopy
from types import MethodType

from google.api_core.exceptions import ServiceUnavailable

import harness as h
from mutation_response_oracle import corrupt_response, response_names, wire_operations
from tool_catalog import MUTATION_ARGS
import pmax_oracle as pmax
import pmax_experiment_oracle as experiment
import search_url_oracle as urls
import shared_targeting_oracle as targeting
from campaign_networks_oracle import NetworkClient, TOOL

# All names and applicable result families are independently authored. This
# must not be populated by a production registry or by observing test output.
FAMILIES = {
    'add_asset_group_audience_signal': (['assetGroupSignals'], []),
    'add_asset_group_search_themes': (['assetGroupSignals'], []),
    'add_negative_keywords': (['campaignCriteria'], []),
    'add_pmax_url_exclusion': (['campaignCriteria'], []),
    'add_shared_negative_keywords': (['sharedCriteria'], []),
    'attach_shared_negative_keyword_list': (['campaignSharedSets'], []),
    'create_ad_group': (['adGroups'], []),
    'create_callouts': (['assets', 'campaignAssets'], []),
    'create_conversion_action': (['conversionActions'], []),
    'create_custom_audience': (['customAudiences'], []),
    'create_pmax_campaign': (['campaignBudgets', 'campaigns', 'assets', 'assetGroups', 'assetGroupAssets', 'campaignCriteria'], []),
    'create_portfolio_bidding_strategy': (['biddingStrategies'], []),
    'create_shared_negative_keyword_list': (['sharedSets'], []),
    'create_structured_snippets': (['assets', 'campaignAssets'], []),
    'draft_campaign': (['campaignBudgets', 'campaigns', 'campaignCriteria', 'adGroups', 'adGroupCriteria'], []),
    'draft_keywords': (['adGroupCriteria'], []),
    'draft_responsive_search_ad': (['adGroupAds'], []),
    'draft_sitelinks': (['assets', 'campaignAssets'], []),
    'exclude_geo_target': (['campaignCriteria'], []),
    'set_campaign_schedule': (['campaignCriteria'], []),
    'upload_image_asset': (['assets'], []),
    'upload_text_asset': (['assets'], []),
    'add_audience_targeting': (['campaignCriteria'], ['campaigns']),
    'create_pmax_url_experiment': (['experiments', 'experimentArms'], ['campaigns']),
    'update_campaign': (['campaignCriteria'], ['campaignBudgets', 'campaigns']),
    'enable_entity': ([], ['campaigns']),
    'pause_entity': ([], ['campaigns']),
    TOOL: ([], ['campaigns']),
    'set_conversion_action_primary_status': ([], ['conversionActions']),
    'set_pmax_final_url_expansion': ([], ['campaigns']),
    'update_ad_group': ([], ['adGroups']),
    'update_keyword_bid': ([], ['adGroupCriteria']),
    'update_keyword_urls': ([], ['adGroupCriteria']),
    'update_responsive_search_ad_urls': ([], ['ads']),
    'set_asset_group_product_selection': (['assetGroupListingGroupFilters'], []),
    'update_demographic_targeting': (['adGroupCriteria'], []),
    'detach_shared_negative_keyword_list': ([], []),
    'remove_asset_group_signals': ([], []),
    'remove_entity': ([], []),
    'remove_extension': ([], []),
    'remove_geo_target': ([], []),
    'remove_keywords': ([], []),
    'remove_negative_keywords': ([], []),
    'remove_pmax_url_exclusions': ([], []),
    'remove_shared_negative_keywords': ([], []),
    'apply_recommendation': ([], []),
    'dismiss_recommendation': ([], []),
    'end_pmax_url_experiment': ([], []),
    'promote_pmax_url_experiment': ([], []),
}
assert len(FAMILIES) == 49


def scenario(tool, variant=None):
    clock = h.FakeClock()
    if tool in experiment.WRITES:
        provider = experiment.ExperimentClient()
        args = deepcopy(experiment.ARGS[tool])
        clock = h.FakeClock(experiment.NOW)
    elif tool in targeting.WRITES:
        provider = targeting.TargetingClient()
        if tool == 'update_demographic_targeting':
            args = {'ad_group_id': '801', 'changes': [{'dimension': 'GENDER', 'value': 'FEMALE', 'action': 'EXCLUDE'}]}
            rows = provider.data[h.CUSTOMER_ID]['ad_group_criterion']
            rows[:] = [row for row in rows if row['ad_group_criterion']['ad_group'] != targeting.rn('adGroups', 801)]
            if variant in ('replace', 'update'):
                rows.append(targeting.demographic('GENDER', 'FEMALE', 610, 801,
                            negative=variant == 'replace', status='ENABLED' if variant == 'replace' else 'PAUSED'))
                args['changes'][0]['action'] = 'INCLUDE'
        else:
            args = deepcopy(targeting.SHARED_ARGS[tool])
    elif tool in pmax.PMAX_MUTATIONS:
        provider = pmax.PMaxClient()
        args = deepcopy(pmax.PMAX_ARGS[tool])
        if tool == 'set_asset_group_product_selection' and variant == 'create_only':
            provider.data[h.CUSTOMER_ID]['asset_group_listing_group_filter'] = []
    elif tool in ('update_keyword_urls', 'update_responsive_search_ad_urls'):
        provider = urls.SearchClient()
        args = urls.args('keyword' if tool == 'update_keyword_urls' else 'ad', final_urls=urls.AFTER_FINAL)
    elif tool == TOOL:
        provider = NetworkClient()
        args = {'campaign_id': '701', 'target_content_network': True}
    else:
        provider = h.stub_standard_account(h.FakeGoogleAdsClient())
        args = deepcopy(MUTATION_ARGS[tool])
        if tool == 'draft_campaign':
            args.update(ad_group_name='Synthetic receipt group', keywords=[{'text': 'synthetic boots', 'match_type': 'EXACT'}])
        elif tool == 'update_campaign':
            args = {'campaign_id': '222'}
            if variant in (None, 'budget'):
                args['daily_budget'] = 45.0
            if variant in (None, 'campaign'):
                args['bidding_strategy'] = 'MAXIMIZE_CONVERSIONS'
            if variant in (None, 'targets'):
                args.update(geo_target_ids=['2840'], language_ids=['1000'])
    provider.observed_responses = []
    provider.fault_at = None
    provider.receipt_fault_kind = None
    original = provider._do_mutation
    def observe(self, service, method, arguments, kwargs):
        index = len(self.observed_responses)
        response = original(service, method, arguments, kwargs)
        call = self.mutations[-1]
        if not call.validate_only and index == self.fault_at:
            if self.receipt_fault_kind == 'transport':
                raise ServiceUnavailable('Synthetic provider detail ' + h.FAKE_REFRESH_TOKEN)
            if self.receipt_fault_kind == 'rejection':
                raise h.make_google_ads_exception(['Synthetic provider detail ' + h.FAKE_REFRESH_TOKEN])
            response = corrupt_response(response, self.receipt_fault_kind)
        self.observed_responses.append((deepcopy(call), deepcopy(response)))
        return response
    provider._do_mutation = MethodType(observe, provider)
    return provider, args, clock


def setup(tmp_path, tool, variant=None, *, customer=h.CUSTOMER_ID, plan_store=None):
    provider, args, clock = scenario(tool, variant)
    if customer != h.CUSTOMER_ID and not hasattr(provider, 'data'):
        from google.protobuf.json_format import MessageToDict
        for resource, rows in list(provider._responses.items()):
            import json
            provider.stub(resource, [json.loads(json.dumps(MessageToDict(row._pb,
                preserving_proto_field_name=True)).replace(h.CUSTOMER_ID, customer)) for row in rows])
    server = h.build_rw_server(tmp_path, client=provider, clock=clock, plan_store=plan_store,
               env={'ADS_MCP_REQUIRE_DRY_RUN': 'true', 'GOOGLE_ADS_CUSTOMER_ID': customer})
    assert tool in h.tool_names(server), 'Missing approved mutation surface: ' + tool
    return server, provider, args


def stage(server, tool, args):
    return h.expect_ok(h.call(server, tool, args))['plan']


def preview(server, plan):
    result = h.expect_ok(h.call(server, 'confirm_and_apply', {'plan_id': plan['id'], 'dry_run': True}))
    assert result['applied'] is False and not result.get('created') and not result.get('updated')
    return result


def apply(server, plan):
    return h.call(server, 'confirm_and_apply', {'plan_id': plan['id'], 'dry_run': False, 'confirm_irreversible': True})


def confirmed_steps(provider, *, before=None):
    steps = []
    observations = provider.observed_responses if before is None else provider.observed_responses[:before]
    for call, response in observations:
        if call.validate_only:
            continue
        created, updated = [], []
        if call.method not in ('apply_recommendation', 'dismiss_recommendation'):
            operations = wire_operations(call.request)
            names = response_names(response)
            assert len(operations) == len(names), 'Provider positive control does not correspond to operations'
            for (_, operation), name in zip(operations, names):
                action = operation._pb.WhichOneof('operation')
                if action == 'create':
                    created.append(name)
                elif action == 'update':
                    updated.append(name)
        steps.append({'created': created, 'updated': updated})
    return steps


def confirmed(provider, *, before=None):
    steps = confirmed_steps(provider, before=before)
    return {key: [name for step in steps for name in step[key]] for key in ('created', 'updated')}
