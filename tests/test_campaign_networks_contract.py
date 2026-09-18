"""Search network defaults, exact partial updates and observational reporting."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from itertools import product

import pytest

import harness as h
from campaign_networks_oracle import (DEFAULTS, FIELDS, INJECTION, NEW_SOURCE,
    ROOT, TOOL, apply, assert_console_binding, campaign,
    preview, reject, require, setup, stage)
from offline_contract import load_script
from tool_catalog import MUTATION_ARGS


def values_named(value, name):
    if isinstance(value, dict):
        for key, child in value.items():
            if key == name:
                yield child
            yield from values_named(child, name)
    elif isinstance(value, list):
        for child in value:
            yield from values_named(child, name)


@pytest.mark.parametrize('values', [None, {}, dict.fromkeys(FIELDS),
    *[dict(zip(FIELDS, values)) for values in product((False, True), repeat=4)
      if values[0] or not values[1]]])
def test_search_creation_reviews_and_transmits_every_effective_network(tmp_path, values):
    server, provider = setup(tmp_path)
    args = {**deepcopy(MUTATION_ARGS['draft_campaign']), **(values or {})}
    plan = h.expect_ok(h.call(server, 'draft_campaign', args))['plan']
    expected = {key: (values or {}).get(key) if (values or {}).get(key) is not None else default
                for key, default in DEFAULTS.items()}
    assert expected in list(values_named(plan, 'network_settings')), 'Preview omits effective Search networks'
    assert not provider.mutations
    preview(server, plan)
    assert not provider.mutations
    h.expect_ok(apply(server, plan))
    creates = [op.create for call in provider.live_mutations() for op in call.request.operations]
    created = next(message for message in creates if message._pb.DESCRIPTOR.name == 'Campaign')
    assert {key: getattr(created.network_settings, key) for key in FIELDS} == expected
    assert all(created.network_settings._pb.HasField(key) for key in FIELDS)
    assert created.status.name == 'PAUSED'
    assert created.contains_eu_political_advertising.name == 'DOES_NOT_CONTAIN_EU_POLITICAL_ADVERTISING'


BAD_BOOL = [0, 1, -1, 1.0, 'true', 'false', 'null', '', [], {}, [True]]


@pytest.mark.parametrize('tool', ['draft_campaign', TOOL])
@pytest.mark.parametrize('field', FIELDS)
@pytest.mark.parametrize('value', BAD_BOOL)
def test_network_inputs_are_strict_booleans_before_provider_reads(tmp_path, tool, field, value):
    server, provider = setup(tmp_path)
    require(server)
    args = deepcopy(MUTATION_ARGS[tool]) if tool == 'draft_campaign' else {'campaign_id': '701'}
    reject(server, provider, tool, {**args, field: value}, local=True)


@pytest.mark.parametrize('channel', ['DISPLAY', 'PERFORMANCE_MAX'])
@pytest.mark.parametrize('field', FIELDS)
@pytest.mark.parametrize('value', [False, True])
def test_non_search_nonnull_networks_are_locally_refused(tmp_path, channel, field, value):
    server, provider = setup(tmp_path)
    require(server)
    reject(server, provider, 'draft_campaign', {**MUTATION_ARGS['draft_campaign'],
           'channel_type': channel, field: value}, local=True)


@pytest.mark.parametrize('channel', ['DISPLAY', 'PERFORMANCE_MAX'])
@pytest.mark.parametrize('values', [{}, dict.fromkeys(FIELDS)])
def test_other_creation_channels_keep_omission_semantics(tmp_path, channel, values):
    server, provider = setup(tmp_path)
    require(server)
    plan = h.expect_ok(h.call(server, 'draft_campaign', {**MUTATION_ARGS['draft_campaign'],
                       'channel_type': channel, **values}))['plan']
    preview(server, plan)
    h.expect_ok(apply(server, plan))
    wire = next(op.create for call in provider.live_mutations() for op in call.request.operations
                if op.create._pb.DESCRIPTOR.name == 'Campaign')
    assert wire.advertising_channel_type.name == channel
    assert not wire._pb.HasField('network_settings')


@pytest.mark.parametrize('values', [{}, dict.fromkeys(FIELDS),
    {'target_google_search': False, 'target_search_network': True}])
def test_invalid_network_combinations_and_empty_updates_refuse(tmp_path, values):
    server, provider = setup(tmp_path)
    require(server)
    reject(server, provider, TOOL, {'campaign_id': '701', **values})
    assert not provider.live_mutations()


@pytest.mark.parametrize('field', FIELDS)
@pytest.mark.parametrize('value', [False, True])
@pytest.mark.parametrize('status,subtype', [('ENABLED', 'UNSPECIFIED'), ('PAUSED', 'UNSPECIFIED')])
def test_network_updates_preserve_exact_supplied_leaf_masks(tmp_path, field, value, status, subtype):
    server, provider = setup(tmp_path)
    before = {**DEFAULTS, field: not value}
    if field == 'target_google_search':
        before['target_search_network'] = False
    campaign(provider).update(status=status, advertising_channel_sub_type=subtype, network_settings=before)
    plan = stage(server, {field: value, **{key: None for key in FIELDS if key != field}})
    assert plan['irreversible'] is False
    paths = [path for operation in plan['operations'] for path in operation.get('update_mask', [])]
    assert paths == ['network_settings.' + field]
    text = json.dumps(plan)
    assert 'before' in text or 'old' in text
    assert 'after' in text or 'new' in text
    assert any(word in text.lower() for word in ('spend', 'serving', 'delivery'))
    preview(server, plan)
    h.expect_ok(apply(server, plan))
    mutation, = provider.live_mutations()
    assert (mutation.service, mutation.method) == ('CampaignService', 'mutate_campaigns')
    assert mutation.request._pb.DESCRIPTOR.name == 'MutateCampaignsRequest'
    operation, = mutation.request.operations
    assert operation._pb.WhichOneof('operation') == 'update'
    assert operation.update.resource_name == f'customers/{h.CUSTOMER_ID}/campaigns/701'
    assert list(operation.update_mask.paths) == ['network_settings.' + field]
    assert getattr(operation.update.network_settings, field) is value
    assert operation.update.network_settings._pb.HasField(field)
    assert 'status' not in {descriptor.name for descriptor, _ in operation.update._pb.ListFields()}
    assert all(call.customer_id == h.CUSTOMER_ID for call in provider.searches)
    assert len(provider.searches) >= 2


@pytest.mark.parametrize('before,values,valid', [
    ({**DEFAULTS, 'target_search_network': True}, {'target_google_search': False}, False),
    ({**DEFAULTS, 'target_google_search': False}, {'target_search_network': True}, False),
    ({**DEFAULTS, 'target_search_network': True}, {'target_google_search': False, 'target_search_network': False}, True),
    ({**DEFAULTS, 'target_google_search': False}, {'target_google_search': True, 'target_search_network': True}, True),
])
def test_partial_updates_validate_the_result_without_widening_the_mask(tmp_path, before, values, valid):
    server, provider = setup(tmp_path)
    campaign(provider)['network_settings'] = before
    require(server)
    if not valid:
        reject(server, provider, TOOL, {'campaign_id': '701', **values})
        return
    plan = stage(server, values)
    preview(server, plan)
    h.expect_ok(apply(server, plan))
    op = provider.live_mutations()[0].request.operations[0]
    assert set(op.update_mask.paths) == {'network_settings.' + key for key in values}
    assert {key: getattr(op.update.network_settings, key) for key in values} == values


@pytest.mark.parametrize('path,value', [
    ('status', 'REMOVED'), ('status', 918273),
    ('advertising_channel_type', 'DISPLAY'), ('advertising_channel_type', 'PERFORMANCE_MAX'),
    ('advertising_channel_type', 918273), ('advertising_channel_sub_type', 'SEARCH_MOBILE_APP'),
    ('advertising_channel_sub_type', 918273), ('id', 702),
    ('resource_name', f'customers/{h.OTHER_CUSTOMER_ID}/campaigns/701'),
    ('resource_name', f'customers/{h.CUSTOMER_ID}/campaigns/702'),
])
def test_unsupported_or_unverified_campaigns_fail_closed(tmp_path, path, value):
    server, provider = setup(tmp_path)
    require(server)
    # Corruption follows provider filtering, so identity mismatches reach the consumer.
    def corrupt(rows):
        for row in rows:
            row['campaign'][path] = value
        return rows
    provider.corrupt['campaign'] = corrupt
    reject(server, provider, TOOL, {'campaign_id': '701', 'target_content_network': True})


@pytest.mark.parametrize('fault', ['missing', 'duplicate', *FIELDS])
def test_complete_singular_network_state_is_required(tmp_path, fault):
    server, provider = setup(tmp_path)
    require(server)
    if fault == 'missing':
        provider.corrupt['campaign'] = lambda rows: []
    elif fault == 'duplicate':
        provider.corrupt['campaign'] = lambda rows: rows + deepcopy(rows)
    else:
        del campaign(provider)['network_settings'][fault]
    reject(server, provider, TOOL, {'campaign_id': '701', 'target_content_network': True})


@pytest.mark.parametrize('field', [*FIELDS, 'status', 'advertising_channel_type', 'advertising_channel_sub_type', 'resource_name'])
def test_every_relevant_network_state_change_invalidates_the_plan(tmp_path, field):
    server, provider = setup(tmp_path)
    plan = stage(server)
    preview(server, plan)
    if field in FIELDS:
        campaign(provider)['network_settings'][field] = not DEFAULTS[field]
    else:
        campaign(provider)[field] = {'status': 'PAUSED', 'advertising_channel_type': 'DISPLAY',
            'advertising_channel_sub_type': 'SEARCH_MOBILE_APP', 'resource_name': f'customers/{h.OTHER_CUSTOMER_ID}/campaigns/701'}[field]
    assert h.error_of(apply(server, plan))['code'] == 'STALE_PLAN'
    assert not provider.mutations


@pytest.mark.parametrize('identity', [True, 701, '', '0', '-1', '+1', '01', ' 701', '７０１', '701~1', '9223372036854775808'])
def test_campaign_ids_are_canonical_and_locally_bounded(tmp_path, identity):
    server, provider = setup(tmp_path)
    require(server)
    reject(server, provider, TOOL, {'campaign_id': identity, 'target_content_network': True}, local=True)


@pytest.mark.parametrize('case', ['readonly', 'foreign', 'unpreviewed', 'expired', 'replayed', 'concurrent', 'audit'])
def test_network_plans_keep_all_existing_safety_boundaries(tmp_path, monkeypatch, case):
    clock = h.FakeClock()
    server, provider = setup(tmp_path, clock=clock)
    require(server)
    if case == 'readonly':
        ro = h.build_server(tmp_path, client=provider)
        assert TOOL not in h.tool_names(ro)
        assert not provider.mutations
        return
    if case == 'foreign':
        reject(server, provider, TOOL, {'campaign_id': '701', 'target_content_network': True,
                                       'customer_id': h.OTHER_CUSTOMER_ID}, local=True)
        return
    plan = stage(server)
    if case == 'unpreviewed':
        assert h.error_of(apply(server, plan))['code'] == 'DRY_RUN_REQUIRED'
        assert not provider.mutations
        return
    preview(server, plan)
    if case == 'expired':
        clock.advance(901)
        assert h.error_of(apply(server, plan))['code'] == 'PLAN_EXPIRED'
        assert not provider.mutations
    elif case == 'audit':
        from ads_mcp.audit import AuditLog
        from ads_mcp.errors import ToolError
        original = AuditLog.write
        def write(self, record, **kwargs):
            if record['event'] == 'apply_started':
                raise ToolError('AUDIT_WRITE_FAILED', 'Synthetic unavailable audit')
            return original(self, record, **kwargs)
        monkeypatch.setattr(AuditLog, 'write', write)
        assert h.error_of(apply(server, plan))['code'] == 'AUDIT_WRITE_FAILED'
        assert not provider.mutations
    else:
        if case == 'concurrent':
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda _: apply(server, plan), range(2)))
        else:
            results = [apply(server, plan), apply(server, plan)]
        assert sum(result.get('applied') is True for result in results) == 1
        assert [h.error_of(result)['code'] for result in results if 'error' in result] == ['PLAN_CONSUMED']
        assert len(provider.live_mutations()) == 1


@pytest.mark.parametrize('customer', [h.CUSTOMER_ID, h.OTHER_CUSTOMER_ID])
@pytest.mark.parametrize('missing', [None, *FIELDS])
def test_campaign_report_retains_false_and_distinguishes_absent_values(tmp_path, customer, missing):
    provider = h.FakeGoogleAdsClient()
    fixture = h.load_contract_fixture(h.CONTRACT_DIR / 'get_campaign_performance.json')
    for resource, rows in fixture['gaql'].items():
        rows = json.loads(json.dumps(rows).replace(h.CUSTOMER_ID, customer))
        if resource == 'campaign':
            for row in rows:
                row['campaign']['network_settings'] = deepcopy(DEFAULTS)
                if missing:
                    del row['campaign']['network_settings'][missing]
        provider.stub(resource, rows)
    server = h.build_server(tmp_path, client=provider, env={'ADS_MCP_ROW_LIMIT': '1'})
    args = {**fixture['args'], 'customer_id': customer}
    result = h.expect_ok(h.call(server, 'get_campaign_performance', args))
    rows = list(result['campaigns'])
    assert result.get('next_page_token')
    queries = len(provider.searches)
    result = h.expect_ok(h.call(server, 'get_campaign_performance', {**args, 'page_token': result['next_page_token']}))
    rows += result['campaigns']
    assert len(provider.searches) == queries and len(rows) == 2
    expected = {**DEFAULTS, **({missing: None} if missing else {})}
    for row, golden in zip(rows, fixture['golden']['campaigns']):
        assert row.get('network_settings') == expected
        assert {key: row[key] for key in golden if key != 'network_settings'} == {key: value for key, value in golden.items() if key != 'network_settings'}
    assert all(call.customer_id == customer for call in provider.searches)
    query = ' '.join(provider.queries())
    assert all('campaign.network_settings.' + field in query for field in FIELDS)
    assert not provider.mutations


def test_exact_final_catalog_schema_and_authored_capability_inventory(tmp_path):
    from test_pmax_experiment_workflow_contract import EXPECTED_ALL, EXPECTED_READS
    from ads_mcp.tools.registry import all_tool_specs
    server, _ = setup(tmp_path)
    expected = set(EXPECTED_ALL) | {TOOL}
    tools = h.tool_map(server)
    assert set(tools) == expected and len(tools) == 84
    ro, _ = setup(tmp_path, read_only=True)
    assert h.tool_names(ro) == set(EXPECTED_READS) and len(EXPECTED_READS) == 34
    specs = all_tool_specs()
    assert len(specs) == 84 and {spec.name for spec in specs} == expected
    schema = tools[TOOL].input_schema
    assert set(schema['properties']) == {'campaign_id', 'customer_id', *FIELDS}
    assert set(schema.get('required', [])) == {'campaign_id'}
    assert set(FIELDS) <= set(tools['draft_campaign'].input_schema['properties'])
    for name in ('draft_campaign', TOOL):
        for field in FIELDS:
            definition = tools[name].input_schema['properties'][field]
            types = {definition.get('type'), *(part.get('type') for part in definition.get('anyOf', []))}
            assert 'boolean' in types and not types.intersection({'integer', 'number', 'string'})
    records = [tool for capability in json.loads((ROOT / 'tests/fixtures/capability_requirements.json').read_text())['capabilities'] for tool in capability['tools']]
    assert len(records) == 84 and {record['name'] for record in records} == expected
    by_name = {record['name']: record for record in records}
    assert set(by_name[TOOL]['parameters']) == {'campaign_id', 'customer_id', *FIELDS}
    assert set(FIELDS) <= set(by_name['draft_campaign']['parameters'])


@pytest.mark.parametrize('missing', NEW_SOURCE)
def test_archive_requires_network_guide_and_both_independent_oracles(missing):
    checker = load_script('check_release_archives')
    expected = set(checker.REQUIRED_SOURCE) | set(NEW_SOURCE)
    assert set(NEW_SOURCE) <= set(checker.REQUIRED_SOURCE)
    checker.check_inventory(expected, source_archive=True)
    with pytest.raises(ValueError, match='source inventory'):
        checker.check_inventory(expected - {missing}, source_archive=True)


def test_public_network_guidance_and_generated_tool_reference_are_current():
    guide = ROOT / 'docs/campaign-networks.md'
    assert guide.is_file(), 'Missing public network guide'
    text = guide.read_text().lower()
    for term in (*FIELDS, 'restricted', 'partner', 'null', 'false', 'spend', 'preview'):
        assert term in text
    assert 'campaign-networks.md' in (ROOT / 'README.md').read_text()
    reference = (ROOT / 'docs/tools.md').read_text()
    assert TOOL in reference and all(field in reference for field in FIELDS)


@pytest.mark.parametrize('customer', [h.CUSTOMER_ID, h.OTHER_CUSTOMER_ID])
def test_installed_console_network_preview_and_exact_sdk_update(tmp_path, monkeypatch, customer):
    import test_auth_cause_contract as process
    from test_search_url_enum_output_contract import EnumConsole
    monkeypatch.setattr(process, 'INJECTION', INJECTION)
    with EnumConsole(tmp_path / 'console') as console:
        if customer != h.CUSTOMER_ID:
            result = console.call(TOOL, {'campaign_id': '701', 'target_content_network': True, 'customer_id': customer})
            assert h.error_of(result)['code'] == 'PLAN_CUSTOMER_MISMATCH'
            assert not console.events('mutation')
        else:
            plan = h.expect_ok(console.call(TOOL, {'campaign_id': '701', 'target_content_network': True}))['plan']
            assert not console.events('mutation')
            h.expect_ok(console.call('confirm_and_apply', {'plan_id': plan['id'], 'dry_run': True}))
            result = h.expect_ok(console.call('confirm_and_apply', {'plan_id': plan['id'], 'dry_run': False}))
            assert result['applied'] is True
            mutation, = console.events('mutation')
            assert mutation['request'] == {'customer_id': customer, 'operations': [{
                'update': {'resource_name': f'customers/{customer}/campaigns/701',
                           'network_settings': {'target_content_network': True}},
                'update_mask': 'networkSettings.targetContentNetwork'}]}
    assert_console_binding(console)


def test_search_creation_refuses_search_partners_without_google_search(tmp_path):
    server, provider = setup(tmp_path)
    require(server)
    reject(server, provider, 'draft_campaign', {**MUTATION_ARGS['draft_campaign'],
        'target_google_search': False, 'target_search_network': True}, local=True)


def test_network_plan_cannot_apply_through_another_account_context(tmp_path):
    from ads_mcp.guardrails import PlanStore
    store = PlanStore(clock=h.FakeClock())
    server, provider = setup(tmp_path, plan_store=store)
    plan = stage(server)
    preview(server, plan)
    other, second = setup(tmp_path, plan_store=store, env={'GOOGLE_ADS_CUSTOMER_ID': h.OTHER_CUSTOMER_ID})
    assert h.error_of(apply(other, plan))['code'] == 'PLAN_CUSTOMER_MISMATCH'
    assert not second.mutations and not provider.mutations


@pytest.mark.parametrize('phase', ['stage', 'apply'])
def test_installed_unknown_network_campaign_enum_has_no_diagnostic_leak(tmp_path, monkeypatch, phase):
    import test_auth_cause_contract as process
    from test_search_url_enum_output_contract import EnumConsole
    monkeypatch.setattr(process, 'INJECTION', INJECTION)
    with EnumConsole(tmp_path / 'console') as console:
        listing = console.receive(console.send('tools/list', {}))['result']['tools']
        assert TOOL in {item['name'] for item in listing}, 'Missing guarded network tool'
        if phase == 'apply':
            plan = h.expect_ok(console.call(TOOL, {'campaign_id': '701', 'target_content_network': True}))['plan']
            h.expect_ok(console.call('confirm_and_apply', {'plan_id': plan['id'], 'dry_run': True}))
        console.mode(campaigns={h.CUSTOMER_ID: {'status': 918273}})
        result = console.call(TOOL, {'campaign_id': '701', 'target_content_network': True}) if phase == 'stage' else console.call(
            'confirm_and_apply', {'plan_id': plan['id'], 'dry_run': False})
        assert h.error_of(result)['code'] != 'INTERNAL'
        if phase == 'apply':
            assert result['error']['code'] == 'STALE_PLAN'
        assert not console.events('mutation')
        text = json.dumps(result) + json.dumps(console.audit())
        assert '918273' not in text and 'Traceback' not in text
        h.assert_no_secrets(text)
    assert_console_binding(console)
