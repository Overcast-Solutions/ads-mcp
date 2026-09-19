"""Synthetic SDK mutate responses, independent of product receipt extraction."""
from copy import deepcopy

import harness as h

# API resource spellings, authored independently of the product registry.
PATHS = {
    'CampaignBudget': 'campaignBudgets', 'Campaign': 'campaigns',
    'AdGroup': 'adGroups', 'AdGroupAd': 'adGroupAds', 'Ad': 'ads',
    'AdGroupCriterion': 'adGroupCriteria', 'CampaignCriterion': 'campaignCriteria',
    'Asset': 'assets', 'CampaignAsset': 'campaignAssets',
    'CustomAudience': 'customAudiences', 'ConversionAction': 'conversionActions',
    'BiddingStrategy': 'biddingStrategies', 'AssetGroup': 'assetGroups',
    'AssetGroupAsset': 'assetGroupAssets', 'AssetGroupSignal': 'assetGroupSignals',
    'AssetGroupListingGroupFilter': 'assetGroupListingGroupFilters',
    'SharedSet': 'sharedSets', 'SharedCriterion': 'sharedCriteria',
    'CampaignSharedSet': 'campaignSharedSets', 'Experiment': 'experiments',
    'ExperimentArm': 'experimentArms',
}


def wire_operations(request):
    if 'mutate_operations' in request._pb.DESCRIPTOR.fields_by_name:
        return [(field.removesuffix('_operation'), getattr(wrapper, field))
                for wrapper in request.mutate_operations
                for field in [wrapper._pb.WhichOneof('operation')]]
    return [(None, op) for op in request.operations]


def success_response(owner, service, method, request):
    """Respond like a provider; never import product request/receipt helpers.

    Known temporary names are resolved to fresh provider IDs. Dedicated update
    and remove results echo the submitted identity, as genuine SDK results do.
    Counters live on one synthetic client; response objects are independent.
    """
    if method in ('apply_recommendation', 'dismiss_recommendation'):
        name = 'ApplyRecommendationResponse' if method.startswith('apply') else 'DismissRecommendationResponse'
        result = h.get_ads_type(name)
        for op in request.operations:
            result.results.append({'resource_name': op.resource_name})
        return result
    result = h.get_ads_type(request._pb.DESCRIPTOR.name.removesuffix('Request') + 'Response')
    if getattr(request, 'validate_only', False):
        return result
    customer = request.customer_id or h.CUSTOMER_ID
    temporary = {}
    owner._receipt_sequence = getattr(owner, '_receipt_sequence', 1000)
    for field, op in wire_operations(request):
        action = op._pb.WhichOneof('operation')
        # A pre-existing harness control probes the result oneof with an empty
        # create message. Preserve that transport-only fixture's semantics.
        entity = op.create if action in ('create', None) else (op.update if action == 'update' else None)
        if action in ('update', 'remove'):
            identity = op.remove if action == 'remove' else entity.resource_name
        else:
            owner._receipt_sequence += 1
            ident = str(owner._receipt_sequence)
            kind = entity._pb.DESCRIPTOR.name
            path = PATHS[kind]
            if kind == 'AssetGroupListingGroupFilter':
                ident = str(100000 + owner._receipt_sequence)
            def parent(name):
                value = getattr(entity, name, '')
                return temporary.get(value, value).rsplit('/', 1)[-1] or ident
            if kind in ('AdGroupCriterion', 'AdGroupAd'):
                leaf = parent('ad_group') + '~' + ident
            elif kind in ('CampaignCriterion',):
                leaf = parent('campaign') + '~' + ident
            elif kind in ('AssetGroupSignal', 'AssetGroupListingGroupFilter'):
                leaf = parent('asset_group') + '~' + ident
            elif kind == 'SharedCriterion':
                leaf = parent('shared_set') + '~' + ident
            elif kind == 'ExperimentArm':
                leaf = parent('experiment') + '~' + ident
            elif kind == 'CampaignSharedSet':
                leaf = parent('campaign') + '~' + parent('shared_set')
            elif kind in ('CampaignAsset', 'AssetGroupAsset'):
                leaf = parent('campaign' if kind == 'CampaignAsset' else 'asset_group') + '~' + parent('asset') + '~' + entity.field_type.name
            else:
                leaf = ident
            identity = f'customers/{customer}/{path}/{leaf}'
            if entity.resource_name:
                temporary[entity.resource_name] = identity
        if field is None:
            result.results.append({'resource_name': identity})
        else:
            item = h.get_ads_type('MutateOperationResponse')
            getattr(item, field + '_result').resource_name = identity
            result.mutate_operation_responses.append(item)
    return result


def response_names(response):
    if 'mutate_operation_responses' in response._pb.DESCRIPTOR.fields_by_name:
        return [getattr(item, item._pb.WhichOneof('response')).resource_name
                for item in response.mutate_operation_responses]
    return [item.resource_name for item in response.results]


def entries(response):
    if 'mutate_operation_responses' in response._pb.DESCRIPTOR.fields_by_name:
        return [getattr(item, item._pb.WhichOneof('response')) for item in response.mutate_operation_responses]
    return list(response.results)


def corrupt_response(response, fault):
    """Change genuine result messages without normalizing the invalid value."""
    response = deepcopy(response)
    rows = entries(response)
    if fault == 'contradictory':
        response.partial_failure_error.code = 13
        response.partial_failure_error.message = 'provider-secret-' + h.FAKE_REFRESH_TOKEN
    elif fault == 'wrong_oneof':
        assert response.mutate_operation_responses
        response.mutate_operation_responses[0].campaign_result.resource_name = f'customers/{h.CUSTOMER_ID}/campaigns/818181'
    elif fault == 'missing':
        field = 'mutate_operation_responses' if 'mutate_operation_responses' in response._pb.DESCRIPTOR.fields_by_name else 'results'
        getattr(response, field).clear()
    elif fault == 'extra':
        field = 'mutate_operation_responses' if 'mutate_operation_responses' in response._pb.DESCRIPTOR.fields_by_name else 'results'
        getattr(response, field).append(deepcopy(getattr(response, field)[0]))
    elif fault == 'duplicate':
        assert len(rows) >= 2
        rows[-1].resource_name = rows[0].resource_name
    elif fault == 'swapped':
        assert len(rows) >= 2
        rows[0].resource_name, rows[1].resource_name = rows[1].resource_name, rows[0].resource_name
    else:
        original = rows[0].resource_name
        values = {
            'empty': '', 'foreign': original.replace(h.CUSTOMER_ID, h.OTHER_CUSTOMER_ID),
            'kind': original.replace('/assets/', '/campaigns/').replace('/campaignBudgets/', '/assets/'),
            'wrong_id': original.rsplit('/', 1)[0] + '/919191',
            'wrong_parent': original.rsplit('/', 1)[0] + '/919191~' + original.rsplit('~', 1)[-1],
            'malformed': 'provider-secret-' + h.FAKE_REFRESH_TOKEN + '/private/path/' + 'x' * 10000,
            'temporary': original.rsplit('/', 1)[0] + '/-17',
        }
        rows[0].resource_name = values[fault]
    return response
