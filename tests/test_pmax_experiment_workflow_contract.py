"""Exact final inventory, authored requirements and installed experiment workflow."""
from copy import deepcopy
from datetime import datetime,timedelta
import asyncio
import json
import re
import shutil
import subprocess
import sys
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

import harness as h
from campaign_networks_oracle import TOOL as NETWORK_TOOL, NEW_SOURCE as NETWORK_SOURCE
import test_auth_cause_contract as process
from capability_oracle import DEFAULT,cli,empty,success
from offline_contract import load_script
from tool_catalog import ALL_WRITE_MODE_TOOLS,READ_TOOLS
from pmax_oracle import PMAX_ADDITIONS,PMAX_READS,SEARCH_URL_ADDITIONS,SEARCH_URL_READS
from shared_targeting_oracle import ADDITIONS as TARGETING_ADDITIONS,READS as TARGETING_READS
from pmax_experiment_oracle import (ROOT,FIXTURES,NEW_SOURCE,ADDITIONS,READS,WRITES,CREATE,END,PROMOTE,
    OPERATION_READ,ARGS,CREATE_ARGS,HANDLE,SIGNATURES,INSTALLED_INJECTION,setup,rn,quiet,break_audit)

PRIOR_ALL=ALL_WRITE_MODE_TOOLS|PMAX_ADDITIONS|SEARCH_URL_ADDITIONS|TARGETING_ADDITIONS
PRIOR_READS=READ_TOOLS|PMAX_READS|SEARCH_URL_READS|TARGETING_READS
EXPECTED_ALL=PRIOR_ALL|ADDITIONS|{NETWORK_TOOL}
EXPECTED_READS=PRIOR_READS|READS


def test_final_inventory_exactly_83_operations_and_34_reads_preserves_every_prior_name(tmp_path):
    from ads_mcp.tools.registry import all_tool_specs
    server,_=setup(tmp_path,ADDITIONS)
    assert len(PRIOR_ALL)==76 and len(PRIOR_READS)==30
    assert h.tool_names(server)==EXPECTED_ALL and len(EXPECTED_ALL)==84
    readonly,_=setup(tmp_path,READS,read_only=True)
    assert h.tool_names(readonly)==EXPECTED_READS and len(EXPECTED_READS)==34
    specs=all_tool_specs();assert len(specs)==len({s.name for s in specs})==84
    assert {s.name for s in specs if s.kind=='read'}==EXPECTED_READS
    assert {s.name for s in specs if s.kind=='apply'}=={'confirm_and_apply'}
    for name,(parameters,required) in SIGNATURES.items():
        schema=h.tool_map(server)[name].input_schema
        assert set(schema['properties'])==set(parameters) and set(schema.get('required',[]))==set(required)


def test_all_seven_independent_capability_records_require_exact_arguments(tmp_path):
    setup(tmp_path,ADDITIONS)
    records=[t for c in json.loads(DEFAULT.read_text())['capabilities'] for t in c['tools']]
    by_name={r['name']:r for r in records}
    assert len(records)==len(by_name)==84 and set(by_name)==EXPECTED_ALL
    for name,(parameters,required) in SIGNATURES.items():
        assert by_name[name]=={'name':name,'parameters':parameters,'required':required,'values':{}}
    result,_=cli(tmp_path,default=True);success(result,empty())


def test_default_parity_executes_all_34_original_and_new_read_goldens(tmp_path):
    setup(tmp_path,ADDITIONS);paths=load_script('parity').fixture_paths(None)
    assert len(paths)==34 and {h.load_contract_fixture(p)['tool'] for p in paths}==EXPECTED_READS
    result=subprocess.run([sys.executable,str(ROOT/'scripts/parity.py'),'--report','-'],cwd=tmp_path,
        env=h.scrubbed_env(),capture_output=True,text=True,timeout=90)
    assert result.returncode==0 and not result.stderr,result.stdout+result.stderr
    names=re.findall(r'(?m)^(\w+)\s+MATCH\s*$',result.stdout)
    assert len(names)==len(set(names))==34 and set(names)==EXPECTED_READS


@pytest.mark.parametrize('missing',sorted(READS))
def test_each_new_read_golden_is_required_in_complete_inventory(tmp_path,missing):
    setup(tmp_path,ADDITIONS);module=load_script('parity');directory=tmp_path/'fixtures';directory.mkdir()
    for path in module.fixture_paths(None):
        if h.load_contract_fixture(path)['tool']!=missing:shutil.copy2(path,directory/path.name)
    with pytest.raises(ValueError,match=missing):module.validate_fixture_inventory(directory)


@pytest.mark.parametrize('missing',NEW_SOURCE)
def test_new_oracle_facts_guides_and_goldens_are_required_in_source_archive(tmp_path,missing):
    setup(tmp_path,ADDITIONS);checker=load_script('check_release_archives')
    from test_search_url_workflow_contract import REQUIRED_SOURCE
    from shared_targeting_oracle import NEW_SOURCE as TARGETING_SOURCE
    expected=set(REQUIRED_SOURCE)|set(TARGETING_SOURCE)|set(NEW_SOURCE)|set(NETWORK_SOURCE)
    assert expected<=set(checker.REQUIRED_SOURCE)
    checker.check_inventory(sorted(expected),source_archive=True)
    with pytest.raises(ValueError):checker.check_inventory(sorted(expected-{missing}),source_archive=True)
    assert all((ROOT/path).is_file() for path in expected)


@pytest.mark.parametrize('read_only',[False,True])
@pytest.mark.parametrize('mutation',['missing','replacement','duplicate'])
def test_installed_checker_requires_exact_names_not_count_equivalence(tmp_path,monkeypatch,read_only,mutation):
    setup(tmp_path,ADDITIONS);module=load_script('check_installed')
    expected=EXPECTED_READS if read_only else EXPECTED_ALL;names=sorted(expected)
    class CatalogClient:
        def __init__(self,server):pass
        async def __aenter__(self):return self
        async def __aexit__(self,*unused):pass
        async def list_tools(self):return SimpleNamespace(tools=[SimpleNamespace(name=n) for n in names])
    monkeypatch.setattr(module.mcp,'Client',CatalogClient)
    assert set(asyncio.run(module.catalog(read_only)))==expected
    names=sorted(expected-{OPERATION_READ})
    if mutation=='replacement':names.append('unapproved_experiment')
    elif mutation=='duplicate':names.append(names[0])
    with pytest.raises(AssertionError):asyncio.run(module.catalog(read_only))


def installed(tmp_path,monkeypatch,**kwargs):
    setup(tmp_path,ADDITIONS)
    monkeypatch.setattr(process,'INJECTION',INSTALLED_INJECTION)
    return process.InstalledServer(tmp_path/'installed',env={'ADS_MCP_REQUIRE_DRY_RUN':'true',**kwargs.pop('env',{})},**kwargs)


def confirm(process,tool,args):
    plan=h.expect_ok(process.call(tool,args))['plan'];params={'plan_id':plan['id'],'dry_run':False,'confirm_irreversible':True}
    assert h.error_of(process.call('confirm_and_apply',params))['code']=='DRY_RUN_REQUIRED'
    assert h.expect_ok(process.call('confirm_and_apply',{**params,'dry_run':True}))['applied'] is False
    result=h.expect_ok(process.call('confirm_and_apply',params));assert result['submitted'] is True
    assert h.error_of(process.call('confirm_and_apply',params))['code']=='PLAN_CONSUMED'
    return result


@pytest.mark.parametrize('customer',[h.CUSTOMER_ID,h.OTHER_CUSTOMER_ID])
def test_installed_console_create_inspect_report_end_promote_and_observe(tmp_path,monkeypatch,customer):
    with installed(tmp_path,monkeypatch,customer=customer) as server:
        listing=server.receive(server.send('tools/list',{}))['result']['tools']
        assert len(listing)==84 and {t['name'] for t in listing}==EXPECTED_ALL
        today=datetime.now(ZoneInfo('America/Denver')).date()
        result=confirm(server,CREATE,{**CREATE_ARGS,'date_start':today.isoformat(),'date_end':(today+timedelta(days=30)).isoformat()})
        assert result['applied'] is True and result['resource_name']==rn('experiments',901,customer)
        ident=result['experiment_id'];server.mode(activate_created=True)
        observed=h.expect_ok(server.call('get_pmax_url_experiment',{'experiment_id':ident}))
        assert observed['experiment']['experiment_id']==ident and observed['campaign']['campaign_id']=='703'
        report=h.expect_ok(server.call('get_pmax_url_experiment_results',{'experiment_id':ident,'date_start':today.isoformat(),'date_end':today.isoformat()}))
        assert report['treatment']['cost_micros']==9007199254740993
        ended=confirm(server,END,{'experiment_id':ident});assert ended['observed']['status']=='ENABLED'
        promotion=confirm(server,PROMOTE,{'experiment_id':ident})
        assert promotion['applied'] is False and promotion['state']=='pending' and promotion['operation_name']==HANDLE
        operation={'experiment_id':ident,'operation_name':HANDLE}
        pending=h.expect_ok(server.call(OPERATION_READ,operation));assert pending['applied'] is False
        server.mode(complete=True,experiment_id=901)
        complete=h.expect_ok(server.call(OPERATION_READ,operation))
        assert complete['completed'] is complete['applied'] is True and complete['promote_status']=='COMPLETED'
        mutations=server.events('experiment mutation');actions=server.events('experiment action')
        assert len([e for e in mutations if not e['validate_only']])==1
        assert len([e for e in actions if not e['validate_only']])==2
        assert all(e['proto'] in ('EndExperimentRequest','PromoteExperimentRequest') for e in actions)
    before=server.events('initial state')[0]['data'];after=server.events('final state')[0]['data']
    other=h.OTHER_CUSTOMER_ID if customer==h.CUSTOMER_ID else h.CUSTOMER_ID
    assert before[other]==after[other] and before[customer]['untouched']==after[customer]['untouched']
    assert before[customer]['campaign'][:2]==after[customer]['campaign'][:2]
    assert not server.stderr


@pytest.mark.parametrize('fault',['stale','uncertain','validation','pre_audit','post_audit','foreign'])
def test_installed_failure_paths_keep_uncertainty_and_no_repeat_action(tmp_path,monkeypatch,fault):
    with installed(tmp_path,monkeypatch) as server:
        if fault=='foreign':
            result=server.call(PROMOTE,{'experiment_id':'301','customer_id':h.OTHER_CUSTOMER_ID})
            assert 'error' in result and not server.events('experiment query') and not server.events('experiment action');return
        if fault=='validation':
            server.mode(reject_validation=True);result=server.call(PROMOTE,ARGS[PROMOTE]);quiet(result)
            assert 'error' in result and 'plan' not in result
            assert all(e['validate_only'] for e in server.events('experiment action'));return
        plan=h.expect_ok(server.call(PROMOTE,ARGS[PROMOTE]))['plan']
        params={'plan_id':plan['id'],'dry_run':False,'confirm_irreversible':True}
        server.call('confirm_and_apply',{**params,'dry_run':True})
        if fault=='pre_audit':break_audit(h.audit_file(server.root))
        else:server.mode(**{'stale':{'drift':True},'uncertain':{'uncertain':True},'post_audit':{'terminal_audit':True}}[fault])
        result=server.call('confirm_and_apply',params);quiet(result)
        writes=[e for e in server.events('experiment action') if not e['validate_only']]
        if fault in ('stale','pre_audit'):
            assert h.error_of(result)['code']==('STALE_PLAN' if fault=='stale' else 'AUDIT_WRITE_FAILED') and not writes
        else:
            assert result.get('applied') is not True and len(writes)==1
            if fault=='post_audit':assert result['submitted'] is True and result['audit_warning']
            assert h.error_of(server.call('confirm_and_apply',params))['code']=='PLAN_CONSUMED'
    assert not server.stderr


def test_installed_readonly_metadata_and_reads_have_no_write_tools(tmp_path,monkeypatch):
    with installed(tmp_path,monkeypatch,env={'ADS_MCP_READ_ONLY':'true'}) as server:
        names={t['name'] for t in server.receive(server.send('tools/list',{}))['result']['tools']}
        assert names==EXPECTED_READS and not names&WRITES
        for tool in READS:h.expect_ok(server.call(tool,ARGS[tool]))
        assert not server.events('experiment action') and not server.events('experiment mutation')
    assert not server.stderr


def test_installed_concurrent_confirmation_submits_pending_promotion_once(tmp_path,monkeypatch):
    with installed(tmp_path,monkeypatch) as server:
        plan=h.expect_ok(server.call(PROMOTE,ARGS[PROMOTE]))['plan']
        server.call('confirm_and_apply',{'plan_id':plan['id'],'dry_run':True})
        arguments={'plan_id':plan['id'],'dry_run':False,'confirm_irreversible':True}
        results=server.concurrent([('confirm_and_apply',arguments),('confirm_and_apply',arguments)])
        assert sum(result.get('submitted') is True for result in results)==1
        assert sum(result.get('error',{}).get('code')=='PLAN_CONSUMED' for result in results)==1
        assert all(result.get('applied') is not True for result in results)
        assert len([e for e in server.events('experiment action') if not e['validate_only']])==1
    assert not server.stderr


@pytest.mark.parametrize('status',['UNSPECIFIED','UNKNOWN','NOT_STARTED','IN_PROGRESS','COMPLETED','FAILED','COMPLETED_WITH_WARNING',999])
def test_installed_every_async_status_remains_separate_from_completed_operation(tmp_path,monkeypatch,status):
    with installed(tmp_path,monkeypatch) as server:
        server.mode(promotion_status=status)
        result=server.call(OPERATION_READ,ARGS[OPERATION_READ]);quiet(result)
        if status==999:assert result.get('error') or result.get('observation_error')
        else:
            assert result['completed'] is True and result['promote_status']==status
            assert result['applied'] is (status in ('COMPLETED','COMPLETED_WITH_WARNING'))
            assert result['warnings'] is (status=='COMPLETED_WITH_WARNING')
        assert not server.events('experiment action') and not server.events('experiment mutation')
    assert not server.stderr


@pytest.mark.parametrize('case',['foreign','wrong_metadata','failed'])
def test_installed_operation_binding_and_terminal_failure_are_content_safe(tmp_path,monkeypatch,case):
    with installed(tmp_path,monkeypatch) as server:
        server.mode(operation_case=case)
        result=server.call(OPERATION_READ,ARGS[OPERATION_READ]);quiet(result)
        assert result.get('applied') is not True
        if case=='failed':assert result['completed'] is True and result['state']=='failed'
        else:assert result.get('error') or result.get('observation_error')
    assert not server.stderr


def test_public_guide_generated_reference_and_changelog_explain_observable_workflow(tmp_path):
    setup(tmp_path,ADDITIONS);guide=ROOT/'docs/pmax-experiments.md'
    assert guide.is_file(),'Add the focused experiment workflow guide'
    text=guide.read_text();lower=text.lower()
    for name in ADDITIONS|{'confirm_and_apply'}:assert name in text
    for words in [('own','credential'),('50','50'),('same','campaign'),('validate','preview'),('micros',),
                  ('relative','absolute'),('submitted','completed','verified'),('pending',),('uncertain','retry'),
                  ('page feed',),('exclusion',),('irreversible',),('timezone',),('366',),('1000',),('provider',)]:
        assert all(word in lower for word in words),words
    assert 'docs/pmax-experiments.md' in (ROOT/'README.md').read_text()
    assert 'experiment' in (ROOT/'CHANGELOG.md').read_text().lower()
    generated=subprocess.run([sys.executable,str(ROOT/'scripts/gen_tools_md.py'),'--stdout'],cwd=ROOT,
        env=h.scrubbed_env(),capture_output=True,text=True,timeout=30)
    assert generated.returncode==0,generated.stdout+generated.stderr
    reference=(ROOT/'docs/tools.md').read_text()
    assert generated.stdout.strip()==reference.strip()
    for tool in ADDITIONS:assert tool in reference
