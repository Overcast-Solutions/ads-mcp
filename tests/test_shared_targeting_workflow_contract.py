"""F079: exact final inventory and real installed shared targeting workflows."""
from copy import deepcopy
import asyncio
import json
import re
import shutil
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

import harness as h
from campaign_networks_oracle import TOOL as NETWORK_TOOL, NEW_SOURCE as NETWORK_SOURCE
import test_auth_cause_contract as process
from capability_oracle import DEFAULT,cli,empty,success
from offline_contract import load_script
from pmax_oracle import PMAX_ADDITIONS,PMAX_READS
from search_url_oracle import ADDITIONS as SEARCH_ADDITIONS,READS as SEARCH_READS
from tool_catalog import ALL_WRITE_MODE_TOOLS,READ_TOOLS
from shared_targeting_oracle import (ROOT,FIXTURES,ADDITIONS,READS,NEW_SOURCE,SIGNATURES,SHARED_ARGS,CATEGORIES,
    setup,rn,INSTALLED_INJECTION,assert_queries,break_audit)
from shared_targeting_oracle import assert_expansion
from pmax_oracle import expected_pending
from pmax_experiment_oracle import ADDITIONS as EXPERIMENT_ADDITIONS, READS as EXPERIMENT_READS, FIXTURES as EXPERIMENT_FIXTURES, NEW_SOURCE as EXPERIMENT_SOURCE

PRIOR_ALL=ALL_WRITE_MODE_TOOLS|PMAX_ADDITIONS|SEARCH_ADDITIONS
PRIOR_READS=READ_TOOLS|PMAX_READS|SEARCH_READS
EXPECTED_ALL=PRIOR_ALL|ADDITIONS
EXPECTED_READS=PRIOR_READS|READS


def final_server(tmp_path):
    return setup(tmp_path,ADDITIONS)


def test_final_exact_76_operations_and_30_reads_preserve_all_previous_names(tmp_path):
    from ads_mcp.tools.registry import all_tool_specs
    server,_=final_server(tmp_path)
    assert len(PRIOR_ALL)==67 and len(PRIOR_READS)==27
    assert len(EXPECTED_ALL)==76 and len(EXPECTED_READS)==30
    assert_expansion(h.tool_names(server),EXPECTED_ALL,EXPERIMENT_ADDITIONS|{NETWORK_TOOL})
    readonly,_=setup(tmp_path,READS,read_only=True)
    assert_expansion(h.tool_names(readonly),EXPECTED_READS,EXPERIMENT_READS)
    specs=all_tool_specs()
    assert 76<=len(specs)==len({s.name for s in specs})<=84
    assert_expansion({s.name for s in specs if s.kind=='read'},EXPECTED_READS,EXPERIMENT_READS)
    assert {s.name for s in specs if s.kind=='apply'}=={'confirm_and_apply'}
    for name,(parameters,required) in SIGNATURES.items():
        schema=h.tool_map(server)[name].input_schema
        assert set(schema['properties'])==set(parameters) and set(schema.get('required',[]))==set(required)


def test_default_capability_requirements_keep_exact_required_parameters(tmp_path):
    final_server(tmp_path)
    records=[t for capability in json.loads(DEFAULT.read_text())['capabilities'] for t in capability['tools']]
    by_name={r['name']:r for r in records}
    assert len(records)==len(by_name)==84 and set(by_name)==EXPECTED_ALL|EXPERIMENT_ADDITIONS | {NETWORK_TOOL}
    for name,(parameters,required) in SIGNATURES.items():
        assert by_name[name]=={'name':name,'parameters':parameters,'required':required,'values':{}}
    result,_=cli(tmp_path,default=True);server,_=final_server(tmp_path);success(result,expected_pending(server))


def all_fixtures():
    return [*h.CONTRACT_DIR.glob('*.json'),*(ROOT/'tests/fixtures/pmax').glob('*.json'),
        *(ROOT/'tests/fixtures/search_urls').glob('*.json'),*FIXTURES.glob('*.json'),
        *[p for p in load_script('parity').fixture_paths(None) if p.parent==EXPERIMENT_FIXTURES]]


def test_final_default_parity_executes_all_30_authored_reads(tmp_path):
    server,_=final_server(tmp_path)
    paths=load_script('parity').fixture_paths(None)
    expected=EXPECTED_READS|(h.tool_names(server)&EXPERIMENT_READS)
    assert len(paths)==len(expected) and {h.load_contract_fixture(p)['tool'] for p in paths}==expected
    result=subprocess.run([sys.executable,str(ROOT/'scripts/parity.py'),'--report','-'],cwd=tmp_path,
        env=h.scrubbed_env(),capture_output=True,text=True,timeout=90)
    assert result.returncode==0 and not result.stderr,result.stdout+result.stderr
    names=re.findall(r'(?m)^(\w+)\s+MATCH\s*$',result.stdout)
    assert len(names)==len(set(names))==len(expected) and set(names)==expected


@pytest.mark.parametrize('missing',sorted(READS))
def test_missing_new_golden_is_rejected_in_complete_final_inventory(tmp_path,missing):
    final_server(tmp_path);directory=tmp_path/'fixtures';directory.mkdir()
    for path in all_fixtures():
        if h.load_contract_fixture(path)['tool']!=missing:shutil.copy2(path,directory/path.name)
    with pytest.raises(ValueError,match=missing):load_script('parity').validate_fixture_inventory(directory)


@pytest.mark.parametrize('missing',NEW_SOURCE)
def test_each_new_oracle_golden_helper_and_guide_is_mandatory_in_sdist(tmp_path,missing):
    final_server(tmp_path);checker=load_script('check_release_archives')
    from test_search_url_workflow_contract import REQUIRED_SOURCE
    expected=set(REQUIRED_SOURCE)|set(NEW_SOURCE)
    expected|={path for path in NETWORK_SOURCE if path in checker.REQUIRED_SOURCE}
    expected|={path for path in EXPERIMENT_SOURCE if path in checker.REQUIRED_SOURCE}
    assert set(NEW_SOURCE)<=set(checker.REQUIRED_SOURCE)
    checker.check_inventory(sorted(expected),source_archive=True)
    with pytest.raises(ValueError):checker.check_inventory(sorted(expected-{missing}),source_archive=True)
    assert all((ROOT/path).is_file() for path in expected)


@pytest.mark.parametrize('read_only',[False,True])
@pytest.mark.parametrize('mutation',['same_count_substitution','duplicate','missing_new'])
def test_installed_catalog_checker_rejects_false_inventory_equivalence(tmp_path,monkeypatch,read_only,mutation):
    final_server(tmp_path);module=load_script('check_installed')
    expected=set(module.EXPECTED_READS if read_only else module.EXPECTED_WRITE_MODE)
    assert_expansion(expected,EXPECTED_READS if read_only else EXPECTED_ALL,EXPERIMENT_READS if read_only else EXPERIMENT_ADDITIONS | {NETWORK_TOOL})
    names=sorted(expected)
    class CatalogClient:
        def __init__(self,server):pass
        async def __aenter__(self):return self
        async def __aexit__(self,*unused):pass
        async def list_tools(self):return SimpleNamespace(tools=[SimpleNamespace(name=n) for n in names])
    monkeypatch.setattr(module.mcp,'Client',CatalogClient)
    assert set(asyncio.run(module.catalog(read_only)))==expected
    remove='get_demographic_targeting'
    if mutation=='same_count_substitution':names=sorted((expected-{remove})|{'unapproved_targeting'})
    elif mutation=='duplicate':names=[*sorted(expected-{remove}),sorted(expected)[0]]
    else:names=sorted(expected-{remove})
    with pytest.raises(AssertionError):asyncio.run(module.catalog(read_only))


def install(tmp_path,monkeypatch,**kwargs):
    final_server(tmp_path)
    monkeypatch.setattr(process,'INJECTION',INSTALLED_INJECTION)
    return process.InstalledServer(tmp_path/'installed',env={'ADS_MCP_REQUIRE_DRY_RUN':'true',**kwargs.pop('env',{})},**kwargs)


def installed_plan(installed,tool,args):
    plan=h.expect_ok(installed.call(tool,args))['plan']
    live=lambda:[e for e in installed.events('targeting mutation') if not e['validate_only']]
    count=len(live());confirm={'plan_id':plan['id'],'dry_run':False,'confirm_irreversible':True}
    assert h.error_of(installed.call('confirm_and_apply',confirm))['code']=='DRY_RUN_REQUIRED'
    assert h.expect_ok(installed.call('confirm_and_apply',{**confirm,'dry_run':True}))['applied'] is False
    assert len(live())==count
    if plan['irreversible']:
        assert h.error_of(installed.call('confirm_and_apply',{**confirm,'confirm_irreversible':False}))['code']=='IRREVERSIBLE_CONFIRMATION_REQUIRED'
    assert h.expect_ok(installed.call('confirm_and_apply',confirm))['applied'] is True
    assert len(live())==count+1
    event=live()[-1]
    assert event['request'].get('partial_failure',False) is False
    assert h.error_of(installed.call('confirm_and_apply',confirm))['code']=='PLAN_CONSUMED'
    assert len(live())==count+1
    assert any(row.get('plan_id')==plan['id'] and row['event']=='applied' for row in installed.audit())
    return event


@pytest.mark.parametrize('customer',[h.CUSTOMER_ID,h.OTHER_CUSTOMER_ID])
def test_installed_shared_list_create_inspect_members_link_unlink_and_reread(tmp_path,monkeypatch,customer):
    with install(tmp_path,monkeypatch,customer=customer) as installed:
        listing=installed.receive(installed.send('tools/list',{}))['result']['tools']
        assert 76<=len(listing)==len({t['name'] for t in listing})<=84
        assert_expansion({t['name'] for t in listing},EXPECTED_ALL,EXPERIMENT_ADDITIONS|{NETWORK_TOOL})
        created=installed_plan(installed,'create_shared_negative_keyword_list',{'name':'Autumn test list'})
        assert created['proto']=='MutateSharedSetsRequest' and created['service']=='SharedSetService'
        assert created['request']['customer_id']==customer and created['request']['operations'][0]['create']['name']=='Autumn test list'
        catalog=h.expect_ok(installed.call('list_shared_negative_keyword_lists'))
        new=next(item for item in catalog['lists'] if item['name']=='Autumn test list')
        ident=new['shared_set_id'];assert ident=='9001' and new['member_count']==new['reference_count']==0
        keyword={'text':'original seasonal term','match_type':'PHRASE'}
        added=installed_plan(installed,'add_shared_negative_keywords',{'shared_set_id':ident,'keywords':[keyword]})
        assert added['proto']=='MutateSharedCriteriaRequest' and added['service']=='SharedCriterionService'
        assert added['request']['operations']==[{'create':{'shared_set':rn('sharedSets',ident,customer),'keyword':keyword}}]
        detail=h.expect_ok(installed.call('get_shared_negative_keyword_list',{'shared_set_id':ident}))
        member_id=detail['keywords'][0]['criterion_id'];assert detail['keywords'][0]['text']==keyword['text']
        linked=installed_plan(installed,'attach_shared_negative_keyword_list',{'shared_set_id':ident,'campaign_ids':['701','702']})
        assert linked['proto']=='MutateCampaignSharedSetsRequest'
        assert linked['request']['operations']==[{'create':{'campaign':rn('campaigns',cid,customer),'shared_set':rn('sharedSets',ident,customer)}} for cid in ('701','702')]
        detail=h.expect_ok(installed.call('get_shared_negative_keyword_list',{'shared_set_id':ident}))
        assert {c['campaign_id'] for c in detail['campaigns']}=={'701','702'}
        removed=installed_plan(installed,'remove_shared_negative_keywords',{'shared_set_id':ident,'criterion_ids':[member_id]})
        assert removed['request']['operations']==[{'remove':rn('sharedCriteria',ident+'~'+member_id,customer)}]
        unlinked=installed_plan(installed,'detach_shared_negative_keyword_list',{'shared_set_id':ident,'campaign_ids':['701','702']})
        assert unlinked['request']['operations']==[{'remove':rn('campaignSharedSets',cid+'~'+ident,customer)} for cid in ('701','702')]
        final=h.expect_ok(installed.call('get_shared_negative_keyword_list',{'shared_set_id':ident}))
        assert final['keywords']==[] and final['campaigns']==[]
        assert final['shared_set']['member_count']==final['shared_set']['reference_count']==0
        assert_queries([SimpleNamespace(**e) for e in installed.events('targeting query')])
    start=installed.events('initial state')[0]['data'];end=installed.events('final state')[0]['data']
    other=h.OTHER_CUSTOMER_ID if customer==h.CUSTOMER_ID else h.CUSTOMER_ID
    assert end[other]==start[other]
    expected=deepcopy(start[customer]);expected['shared_set']=end[customer]['shared_set']
    assert expected==end[customer] and end[customer]['shared_set'][:2]==start[customer]['shared_set']
    assert all(not thread.is_alive() for thread in installed.readers) and installed.process.poll()==0
    assert not installed.stderr


@pytest.mark.parametrize('channel',['SEARCH','DISPLAY'])
def test_installed_demographic_inspect_replace_and_status_update_preserve_state(tmp_path,monkeypatch,channel):
    ident='801' if channel=='SEARCH' else '804'
    with install(tmp_path,monkeypatch) as installed:
        initial=h.expect_ok(installed.call('get_demographic_targeting',{'ad_group_id':ident}))
        dimension,value=('AGE_RANGE','AGE_RANGE_25_34') if channel=='SEARCH' else ('PARENTAL_STATUS','PARENT')
        action='EXCLUDE' if channel=='SEARCH' else 'INCLUDE'
        event=installed_plan(installed,'update_demographic_targeting',{'ad_group_id':ident,'changes':[{'dimension':dimension,'value':value,'action':action}]})
        assert event['service']=='AdGroupCriterionService' and event['proto']=='MutateAdGroupCriteriaRequest'
        ops=event['request']['operations'];assert list(ops[0])==['remove'] and list(ops[1])==['create']
        old=next(c for c in initial['criteria'] if c['dimension']==dimension and c['value']==value)
        assert ops[0]['remove']==old['resource_name']
        created=ops[1]['create'];assert created['ad_group']==rn('adGroups',ident)
        assert created.get('negative',False)==(action=='EXCLUDE')
        assert set(created)<={'ad_group','negative','status',dimension.lower()}
        reread=h.expect_ok(installed.call('get_demographic_targeting',{'ad_group_id':ident}))
        changed=next(c for c in reread['criteria'] if c['dimension']==dimension and c['value']==value)
        assert changed['negative']==(action=='EXCLUDE') and changed['resource_name']!=old['resource_name']
        assert reread['ad_group']==initial['ad_group'] and reread['campaign']==initial['campaign']
        assert 'effective' in reread['eligibility_note'].lower()
    before=installed.events('initial state')[0]['data'];after=installed.events('final state')[0]['data']
    before[h.CUSTOMER_ID]['ad_group_criterion']=[r for r in before[h.CUSTOMER_ID]['ad_group_criterion'] if r['ad_group_criterion']['ad_group']!=rn('adGroups',ident)]
    after[h.CUSTOMER_ID]['ad_group_criterion']=[r for r in after[h.CUSTOMER_ID]['ad_group_criterion'] if r['ad_group_criterion']['ad_group']!=rn('adGroups',ident)]
    assert before==after and not installed.stderr


@pytest.mark.parametrize('kind',['shared','demographic'])
@pytest.mark.parametrize('fault',['stale','uncertain','stage_audit','pre_audit','terminal_audit','foreign'])
def test_installed_failure_paths_preserve_no_success_and_no_retry(tmp_path,monkeypatch,kind,fault):
    tool='add_shared_negative_keywords' if kind=='shared' else 'update_demographic_targeting'
    args=SHARED_ARGS[tool] if kind=='shared' else {'ad_group_id':'801','changes':[{'dimension':'GENDER','value':'FEMALE','action':'EXCLUDE'}]}
    with install(tmp_path,monkeypatch) as installed:
        if fault=='foreign':
            count=len(installed.events('targeting query'))
            result=installed.call(tool,{**args,'customer_id':h.OTHER_CUSTOMER_ID})
            assert 'error' in result and len(installed.events('targeting query'))==count and not installed.events('targeting mutation')
            return
        if fault=='stage_audit':
            break_audit(h.audit_file(installed.root))
            result=installed.call(tool,args)
            assert h.error_of(result)['code']=='AUDIT_WRITE_FAILED' and 'plan' not in result
            assert not installed.events('targeting mutation');return
        plan=h.expect_ok(installed.call(tool,args))['plan'];confirm={'plan_id':plan['id'],'dry_run':False,'confirm_irreversible':True}
        h.expect_ok(installed.call('confirm_and_apply',{**confirm,'dry_run':True}))
        if fault=='pre_audit':break_audit(h.audit_file(installed.root))
        else:installed.mode(**{'stale':{'drift':True},'uncertain':{'uncertain':True},'terminal_audit':{'terminal_audit':True}}[fault])
        result=installed.call('confirm_and_apply',confirm)
        writes=[e for e in installed.events('targeting mutation') if not e['validate_only']]
        if fault in ('stale','pre_audit'):
            assert h.error_of(result)['code']==('STALE_PLAN' if fault=='stale' else 'AUDIT_WRITE_FAILED') and not writes
        elif fault=='uncertain':
            assert h.error_of(result)['code']!='INTERNAL' and len(writes)==1
            assert h.error_of(installed.call('confirm_and_apply',confirm))['code']=='PLAN_CONSUMED'
            assert len([e for e in installed.events('targeting mutation') if not e['validate_only']])==1
        else:assert h.expect_ok(result)['applied'] is True and result.get('audit_warning') and len(writes)==1
    assert not installed.stderr


def test_installed_expiry_uses_real_configured_lifetime(tmp_path,monkeypatch):
    with install(tmp_path,monkeypatch,env={'ADS_MCP_PLAN_TTL_SECONDS':'30'}) as installed:
        plan=h.expect_ok(installed.call('add_shared_negative_keywords',SHARED_ARGS['add_shared_negative_keywords']))['plan']
        expiry=h.parse_iso_utc(plan['expires_at']).timestamp();delay=expiry-time.time()+0.2
        assert 0<delay<31
        time.sleep(delay)
        result=installed.call('confirm_and_apply',{'plan_id':plan['id'],'dry_run':False})
        assert h.error_of(result)['code']=='PLAN_EXPIRED' and not installed.events('targeting mutation')
    assert not installed.stderr


def test_unrelated_installed_warnings_remain_visible(tmp_path,monkeypatch):
    with install(tmp_path,monkeypatch) as installed:
        installed.mode(unrelated_warning=True)
        h.expect_ok(installed.call('get_shared_negative_keyword_list',{'shared_set_id':'401'}))
    assert 'Synthetic unrelated diagnostic' in ''.join(installed.stderr)


def test_public_guide_and_generated_reference_explain_local_scope_and_provider_limits(tmp_path):
    final_server(tmp_path);guide=ROOT/'docs/shared-targeting.md'
    assert guide.is_file(),'Add the focused shared targeting guide'
    text=guide.read_text();lower=text.lower()
    for name in ADDITIONS|{'confirm_and_apply'}:assert name in text
    for words in [('own','credential'),('read-only',),('same','account'),('search','shopping','display'),
        ('preview','apply','re-read'),('all','campaign'),('remove','recreate'),('confirm_irreversible',),
        ('default','exclusion','effective'),('country','policy'),('optimized','targeting'),
        ('experiment',),('stale','expired','uncertain'),('local','undetermined'),('5000','1000'),('16','mib')]:
        assert all(word in lower for word in words),words
    assert 'example.invalid' in text
    assert 'no retry' in lower or 'not retry' in lower or 'never retry' in lower
    assert 'developers.google.com/google-ads/api/' in text and 'support.google.com/' in text
    assert 'live' in lower and 'acceptance' in lower
    assert not re.search(r'google (?:always|universally) (?:forbids|prohibits).*undetermined',lower)
    for path in (ROOT/'README.md',ROOT/'docs/migration.md'):
        content=path.read_text();assert any(total in content and reads in content for total,reads in (('76','30'),('79','33'),('80','33'),('83','34'),('84','34')))
    assert 'docs/shared-targeting.md' in (ROOT/'README.md').read_text()
    changelog=(ROOT/'CHANGELOG.md').read_text().lower()
    assert all(word in changelog for word in ('unreleased','shared','demographic'))
    generated=subprocess.run([sys.executable,str(ROOT/'scripts/gen_tools_md.py'),'--stdout'],env=h.scrubbed_env(),capture_output=True,text=True,timeout=30)
    assert generated.returncode==0 and generated.stdout.strip()==(ROOT/'docs/tools.md').read_text().strip()
    assert all(name in generated.stdout for name in ADDITIONS)
    assert sum(line.startswith('```') for line in text.splitlines())%2==0
    for target in re.findall(r'\[[^\]]+\]\(([^)]+)\)',text):
        if '://' in target:continue
        relative,_,anchor=target.partition('#');linked=guide.parent/relative if relative else guide
        assert linked.is_file(),target
        if anchor:
            headings=[re.sub(r'[^a-z0-9 _-]','',line.lstrip('# ').lower()).replace(' ','-') for line in linked.read_text().splitlines() if line.startswith('#')]
            assert anchor in headings,target


@pytest.mark.parametrize('channel',['SEARCH','DISPLAY'])
def test_installed_positive_enable_has_only_status_mask_and_retains_direct_settings(tmp_path,monkeypatch,channel):
    from shared_targeting_oracle import demographic
    final_server(tmp_path);ident=801 if channel=='SEARCH' else 804
    row=demographic('GENDER','FEMALE',680,ident,status='PAUSED',bid_modifier=1,cpc_bid_micros=0,
        final_urls=['https://example.invalid/Preserved'],labels=[rn('labels',44)])
    seed="\ntransport.data[h.CUSTOMER_ID]['ad_group_criterion'].append("+repr(row)+")\n"
    monkeypatch.setattr(process,'INJECTION',INSTALLED_INJECTION+seed)
    with process.InstalledServer(tmp_path/'installed',env={'ADS_MCP_REQUIRE_DRY_RUN':'true'}) as installed:
        inspected=h.expect_ok(installed.call('get_demographic_targeting',{'ad_group_id':str(ident)}))
        before=next(item for item in inspected['criteria'] if item['criterion_id']=='680')
        event=installed_plan(installed,'update_demographic_targeting',{'ad_group_id':str(ident),
            'changes':[{'dimension':'GENDER','value':'FEMALE','action':'INCLUDE'}]})
        assert event['request']['operations']==[{'update':{'resource_name':rn('adGroupCriteria',f'{ident}~680'),'status':'ENABLED'},'update_mask':'status'}]
        inspected=h.expect_ok(installed.call('get_demographic_targeting',{'ad_group_id':str(ident)}))
        after=next(item for item in inspected['criteria'] if item['criterion_id']=='680')
        assert after=={**before,'status':'ENABLED'}
    assert not installed.stderr
