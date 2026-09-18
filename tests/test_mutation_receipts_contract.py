"""Confirmed resource receipts from every supported mutation path."""
from concurrent.futures import ThreadPoolExecutor
import json
import re

import pytest

import harness as h
from campaign_networks_oracle import INJECTION, ROOT, assert_console_binding
from mutation_receipts_oracle import (FAMILIES, apply, confirmed, confirmed_steps,
    preview, setup, stage)


def assert_receipts(payload, expected):
    for key in ('created', 'updated'):
        assert payload.get(key) == expected[key], f'Missing or incorrect confirmed {key} receipts'
        assert len(payload[key]) == len(set(payload[key])), 'Receipts contain duplicate identities'
        assert all(isinstance(value, str) and value for value in payload[key])
    assert not set(payload['created']) & set(payload['updated'])


def assert_audit(tmp_path, plan, expected, provider, *, failed=False):
    records = [record for record in h.read_audit_records(tmp_path) if record.get('plan_id') == plan['id']]
    assert records and all(record['customer_id'] == h.CUSTOMER_ID for record in records)
    steps = [record for record in records if record['event'] == 'step_applied']
    confirmed_records = confirmed_steps(provider)
    # Custom actions have their own submission/readback audit contract; their
    # terminal records must still carry empty resource receipt lists.
    if confirmed_records:
        assert len(steps) == len(confirmed_records)
        for record, identities in zip(steps, confirmed_records):
            assert_receipts(record, identities)
    finals = [record for record in records if record['event'] in ('apply_failed', 'applied', 'submitted')]
    assert finals
    assert_receipts(finals[-1], expected)
    if failed:
        assert finals[-1]['event'] == 'apply_failed'
    h.assert_no_secrets(json.dumps(records))


@pytest.mark.parametrize('tool', sorted(FAMILIES))
def test_every_mutation_returns_all_and_only_confirmed_create_update_receipts(tmp_path, tool):
    server, provider, args = setup(tmp_path, tool)
    plan = stage(server, tool, args)
    assert not provider.live_mutations()
    preview(server, plan)
    assert not provider.live_mutations()
    result = h.expect_ok(apply(server, plan))
    expected = confirmed(provider)
    assert {name.split('/')[2] for name in expected['created']} == set(FAMILIES[tool][0])
    assert {name.split('/')[2] for name in expected['updated']} == set(FAMILIES[tool][1])
    assert_receipts(result, expected)
    assert not any(re.search(r'\b(?:campaign|ad_group|asset)\.name\s*=', call.query, re.I)
                   for call in provider.searches), 'Resource receipts must not be reconstructed by name lookup'
    assert all(name.startswith(f'customers/{h.CUSTOMER_ID}/') for key in expected for name in expected[key])
    composite = {'campaignCriteria', 'adGroupCriteria', 'adGroupAds', 'campaignAssets',
        'assetGroupAssets', 'assetGroupSignals', 'assetGroupListingGroupFilters',
        'sharedCriteria', 'campaignSharedSets', 'experimentArms'}
    assert all('~' in name.rsplit('/', 1)[1] for key in expected for name in expected[key]
               if name.split('/')[2] in composite)
    if tool == 'create_pmax_campaign':
        assert result['campaign_resource_name'] in expected['created']
    elif tool == 'create_pmax_url_experiment':
        assert result.get('submitted') is True and result.get('verification')
    elif tool in ('end_pmax_url_experiment', 'promote_pmax_url_experiment'):
        assert result.get('submitted') is True
    else:
        assert result['applied'] is True
    assert_audit(tmp_path, plan, expected, provider)
    before = len(provider.live_mutations())
    assert h.error_of(apply(server, plan))['code'] == 'PLAN_CONSUMED'
    assert len(provider.live_mutations()) == before


@pytest.mark.parametrize('variant,created,updated', [
    ('budget', set(), {'campaignBudgets'}), ('campaign', set(), {'campaigns'}),
    ('targets', {'campaignCriteria'}, set()),
])
def test_update_campaign_separate_budget_campaign_and_target_steps(tmp_path, variant, created, updated):
    server, provider, args = setup(tmp_path, 'update_campaign', variant)
    plan = stage(server, 'update_campaign', args)
    preview(server, plan)
    result = h.expect_ok(apply(server, plan))
    expected = confirmed(provider)
    assert {name.split('/')[2] for name in expected['created']} == created
    assert {name.split('/')[2] for name in expected['updated']} == updated
    assert_receipts(result, expected)
    assert_audit(tmp_path, plan, expected, provider)


@pytest.mark.parametrize('tool,variant,actions', [
    ('update_demographic_targeting', 'replace', {'remove', 'create'}),
    ('update_demographic_targeting', 'update', {'update'}),
    ('set_asset_group_product_selection', 'create_only', {'create'}),
])
def test_mixed_mutation_branches_never_turn_removes_into_receipts(tmp_path, tool, variant, actions):
    from mutation_response_oracle import wire_operations
    server, provider, args = setup(tmp_path, tool, variant)
    plan = stage(server, tool, args)
    preview(server, plan)
    result = h.expect_ok(apply(server, plan))
    assert {op._pb.WhichOneof('operation') for call in provider.live_mutations()
            for _, op in wire_operations(call.request)} == actions
    expected = confirmed(provider)
    assert_receipts(result, expected)
    removed = {op.remove for call in provider.live_mutations() for _, op in wire_operations(call.request)
               if op._pb.WhichOneof('operation') == 'remove'}
    assert not removed.intersection(result['created'] + result['updated'])
    assert_audit(tmp_path, plan, expected, provider)


@pytest.mark.parametrize('tool,fault', [
    *[('draft_campaign', fault) for fault in ('missing', 'empty', 'foreign', 'kind', 'malformed', 'temporary', 'extra', 'contradictory')],
    *[('create_callouts', fault) for fault in ('duplicate', 'missing', 'foreign')],
    ('draft_keywords', 'wrong_parent'),
    ('create_pmax_campaign', 'wrong_oneof'),
    *[('update_ad_group', fault) for fault in ('wrong_id', 'empty', 'foreign')],
    *[('create_pmax_campaign', fault) for fault in ('duplicate', 'swapped', 'missing', 'foreign')],
    *[('create_pmax_url_experiment', fault) for fault in ('duplicate', 'swapped', 'missing', 'foreign')],
])
def test_invalid_provider_receipts_are_uncertain_and_stop_dependent_writes(tmp_path, tool, fault):
    server, provider, args = setup(tmp_path, tool)
    if tool == 'create_callouts':
        args['callouts'] = ['Synthetic shipping', 'Synthetic returns']
    plan = stage(server, tool, args)
    preview(server, plan)
    provider.fault_at = len(provider.observed_responses)
    provider.receipt_fault_kind = fault
    result = apply(server, plan)
    error = h.error_of(result)
    assert re.fullmatch('[A-Z][A-Z0-9_]+', error['code']) and error['code'] != 'INTERNAL'
    assert result.get('partial_changes_possible') is True
    assert result.get('receipts_complete') is False
    assert_receipts(result, {'created': [], 'updated': []})
    assert len(provider.live_mutations()) == 1, 'Invalid receipt must stop dependencies and must never retry'
    before = len(provider.mutations)
    assert h.error_of(apply(server, plan))['code'] == 'PLAN_CONSUMED'
    assert len(provider.mutations) == before
    records = h.read_audit_records(tmp_path)
    text = json.dumps(result) + json.dumps(records)
    assert len(json.dumps(result)) < 20000 and 'provider-secret-' not in text and '/private/path/' not in text
    assert 'Traceback' not in text
    if fault == 'foreign':
        assert 'customers/' + h.OTHER_CUSTOMER_ID + '/' not in text
    assert not [record for record in records if record['event'] == 'step_applied'
                and (record.get('created') or record.get('updated'))]
    h.assert_no_secrets(text)
    assert any(record['event'] == 'apply_failed' and record.get('receipts_complete') is False
               and record.get('created') == [] and record.get('updated') == [] for record in records)


@pytest.mark.parametrize('fault', ['transport', 'rejection', 'missing', 'foreign', 'empty'])
@pytest.mark.parametrize('tool,step', [('draft_campaign', 1), ('draft_campaign', 2), ('draft_sitelinks', 1), ('update_campaign', 2)])
def test_later_failure_retains_prior_receipts_without_claiming_rollback(tmp_path, tool, step, fault):
    server, provider, args = setup(tmp_path, tool)
    plan = stage(server, tool, args)
    preview(server, plan)
    provider.fault_at = step
    provider.receipt_fault_kind = fault
    result = apply(server, plan)
    assert h.error_of(result)['code'] != 'INTERNAL'
    expected = confirmed(provider, before=step)
    assert expected['created'] or expected['updated']
    assert_receipts(result, expected)
    assert result.get('partial_changes_possible') is True and result.get('receipts_complete') is False
    assert len(provider.live_mutations()) == step + 1
    records = h.read_audit_records(tmp_path)
    terminal = [record for record in records if record['event'] == 'apply_failed'][-1]
    assert_receipts(terminal, expected)
    assert terminal.get('partial_changes_possible') is True and terminal.get('receipts_complete') is False
    steps = [record for record in records if record['event'] == 'step_applied']
    expected_steps = confirmed_steps(provider, before=step)
    assert len(steps) == len(expected_steps)
    for record, identities in zip(steps, expected_steps):
        assert_receipts(record, identities)
    h.assert_no_secrets(json.dumps(result) + json.dumps(records))
    count = len(provider.mutations)
    assert h.error_of(apply(server, plan))['code'] == 'PLAN_CONSUMED'
    assert len(provider.mutations) == count


@pytest.mark.parametrize('event', ['apply_started', 'step_applied', 'applied'])
def test_audit_refusal_precedes_writes_and_postwrite_loss_preserves_receipts(tmp_path, monkeypatch, event):
    from ads_mcp.audit import AuditLog
    from ads_mcp.errors import ToolError
    server, provider, args = setup(tmp_path, 'draft_sitelinks')
    plan = stage(server, 'draft_sitelinks', args)
    preview(server, plan)
    original = AuditLog.write
    def write(self, record, **kwargs):
        if record['event'] == event:
            if kwargs.get('critical'):
                raise ToolError('AUDIT_WRITE_FAILED', 'Synthetic unavailable audit')
            return False
        return original(self, record, **kwargs)
    monkeypatch.setattr(AuditLog, 'write', write)
    result = apply(server, plan)
    if event == 'apply_started':
        assert h.error_of(result)['code'] == 'AUDIT_WRITE_FAILED'
        assert not provider.live_mutations()
        assert not result.get('created') and not result.get('updated')
        # This feature's new positive requirement remains discriminating even
        # when the unchanged pre-write guard correctly refuses on the base.
        second = stage(server, 'upload_text_asset', {'asset_name': 'Synthetic second', 'text_content': 'Control'})
        preview(server, second)
        monkeypatch.setattr(AuditLog, 'write', original)
        result = h.expect_ok(apply(server, second))
    else:
        h.expect_ok(result)
        assert result.get('audit_warning')
    assert_receipts(result, confirmed(provider))


@pytest.mark.parametrize('customer', [h.CUSTOMER_ID, h.OTHER_CUSTOMER_ID])
def test_receipts_are_isolated_between_sequential_calls_and_accounts(tmp_path, customer):
    server, provider, args = setup(tmp_path, 'upload_text_asset', customer=customer)
    first = stage(server, 'upload_text_asset', args)
    preview(server, first)
    result = h.expect_ok(apply(server, first))
    expected = confirmed(provider)
    assert_receipts(result, expected)
    second = stage(server, 'upload_text_asset', {**args, 'asset_name': 'Synthetic next'})
    preview(server, second)
    result2 = h.expect_ok(apply(server, second))
    latest = confirmed_steps(provider)[-1]
    assert_receipts(result2, latest)
    assert not set(result['created']) & set(result2['created'])
    assert all(name.startswith(f'customers/{customer}/') for name in result['created'] + result2['created'])


def test_concurrent_confirmation_cannot_replay_or_cross_contaminate_receipts(tmp_path):
    server, provider, args = setup(tmp_path, 'upload_text_asset')
    plans = [stage(server, 'upload_text_asset', {**args, 'asset_name': f'Synthetic {i}'}) for i in range(2)]
    for plan in plans:
        preview(server, plan)
    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(lambda plan: apply(server, plan), [plans[0], plans[0], plans[1]]))
    successes = [result for result in results if result.get('applied')]
    assert len(successes) == 2 and len(provider.live_mutations()) == 2
    assert [h.error_of(result)['code'] for result in results if 'error' in result] == ['PLAN_CONSUMED']
    assert {tuple(result.get('created', [])) for result in successes} == {tuple(step['created']) for step in confirmed_steps(provider)}
    assert all(result.get('updated') == [] for result in successes)
    records = h.read_audit_records(tmp_path)
    for result in successes:
        record = next(record for record in records if record['event'] == 'applied' and record['plan_id'] == result['plan']['id'])
        assert_receipts(record, {key: result[key] for key in ('created', 'updated')})


@pytest.mark.parametrize('fault', [None, 'foreign', 'missing'])
def test_installed_console_receipts_are_provider_returned_and_sanitized(tmp_path, monkeypatch, fault):
    import test_auth_cause_contract as process
    from test_search_url_enum_output_contract import EnumConsole
    monkeypatch.setattr(process, 'INJECTION', INJECTION)
    with EnumConsole(tmp_path / 'console') as console:
        plan = h.expect_ok(console.call('upload_text_asset', {'asset_name': 'Synthetic receipt', 'text_content': 'Independent receipt'}))['plan']
        h.expect_ok(console.call('confirm_and_apply', {'plan_id': plan['id'], 'dry_run': True}))
        assert not console.events('mutation')
        if fault:
            console.mode(receipt_fault=fault)
        result = console.call('confirm_and_apply', {'plan_id': plan['id'], 'dry_run': False})
        mutation, = console.events('mutation')
        if fault:
            assert h.error_of(result)['code'] != 'INTERNAL'
            assert_receipts(result, {'created': [], 'updated': []})
            assert result.get('partial_changes_possible') is True and result.get('receipts_complete') is False
        else:
            assert h.expect_ok(result)['applied'] is True
            assert_receipts(result, {'created': mutation['response_names'], 'updated': []})
            terminal = [record for record in console.audit() if record['event'] == 'applied'][-1]
            assert_receipts(terminal, {'created': mutation['response_names'], 'updated': []})
        assert h.error_of(console.call('confirm_and_apply', {'plan_id': plan['id'], 'dry_run': False}))['code'] == 'PLAN_CONSUMED'
        assert len(console.events('mutation')) == 1
        h.assert_no_secrets(json.dumps(result) + json.dumps(console.audit()))
    assert_console_binding(console)


def test_public_write_guide_explains_receipts_and_unknown_outcomes():
    text = (ROOT / 'docs/writes.md').read_text().lower()
    for term in ('created', 'updated', 'receipt', 'partial', 'unknown', 'retry', 'resource name'):
        assert term in text
    assert 'receipt' in (ROOT / 'CHANGELOG.md').read_text().lower()
