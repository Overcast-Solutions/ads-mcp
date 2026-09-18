"""Positive and malformed controls for synthetic SDK response fidelity."""
from copy import deepcopy

import pytest

import harness as h
from campaign_networks_oracle import FIELDS, DEFAULTS, NetworkClient, TOOL
from mutation_receipts_oracle import FAMILIES, apply, confirmed, preview, setup, stage
from mutation_response_oracle import corrupt_response, response_names, wire_operations
from offline_contract import project


@pytest.mark.parametrize('tool', sorted(set(FAMILIES) - {TOOL}))
def test_existing_workflow_provider_controls_are_real_and_operation_correlated(tmp_path, tool):
    server, provider, args = setup(tmp_path, tool)
    plan = stage(server, tool, args)
    preview(server, plan)
    h.expect_ok(apply(server, plan))
    expected = confirmed(provider)
    assert {name.split('/')[2] for name in expected['created']} == set(FAMILIES[tool][0])
    assert {name.split('/')[2] for name in expected['updated']} == set(FAMILIES[tool][1])
    for call, response in provider.observed_responses:
        assert response._pb.DESCRIPTOR.full_name.startswith('google.ads.googleads.v25.services.')
        if call.validate_only or call.method in ('apply_recommendation', 'dismiss_recommendation'):
            continue
        names = response_names(response)
        assert len(names) == len(wire_operations(call.request))
        assert len(set(names)) == len(names)
        for (_, operation), identity in zip(wire_operations(call.request), names):
            action = operation._pb.WhichOneof('operation')
            assert identity.startswith(f'customers/{h.CUSTOMER_ID}/')
            assert '/mocked/' not in identity
            if action in ('update', 'remove'):
                assert identity == (operation.update.resource_name if action == 'update' else operation.remove)


@pytest.mark.parametrize('fault', ['missing', 'empty', 'foreign', 'kind', 'malformed', 'temporary', 'extra', 'duplicate', 'swapped'])
def test_malformed_response_controls_change_only_the_real_provider_message(tmp_path, fault):
    server, provider, args = setup(tmp_path, 'create_callouts')
    args['callouts'] = ['Synthetic shipping', 'Synthetic returns']
    plan = stage(server, 'create_callouts', args)
    preview(server, plan)
    h.expect_ok(apply(server, plan))
    call, original = provider.observed_responses[0]
    before = deepcopy(original)
    corrupted = corrupt_response(original, fault)
    assert type(corrupted) is type(original) and corrupted._pb.DESCRIPTOR.name == 'MutateAssetsResponse'
    assert original == before and corrupted != original
    assert response_names(corrupted) != response_names(original)
    assert call.request._pb.DESCRIPTOR.name == 'MutateAssetsRequest'


def test_network_projection_preserves_explicit_false_and_absence():
    provider = NetworkClient()
    row = h.make_row(provider.data[h.CUSTOMER_ID]['campaign'][0])
    selected = ['campaign.network_settings.' + field for field in FIELDS]
    projected = project(row, selected)
    assert {field: getattr(projected.campaign.network_settings, field) for field in FIELDS} == DEFAULTS
    assert all(projected.campaign.network_settings._pb.HasField(field) for field in FIELDS)
    row.campaign.network_settings._pb.ClearField('target_content_network')
    assert not project(row, selected).campaign.network_settings._pb.HasField('target_content_network')
