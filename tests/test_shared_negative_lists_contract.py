"""F077: same-account list inspection, bounded impact, and guarded requests."""
from copy import deepcopy
import json
import re

import pytest
from google.protobuf.json_format import MessageToDict

import harness as h
from offline_contract import refusal
from shared_targeting_oracle import (SHARED_READS,SHARED_WRITES,SHARED_ARGS,SIGNATURES,BAD_IDS,BAD_CUSTOMERS,
    setup,stage,preview,apply,checked_apply,rejected,golden,rn,member,shared_set,link,campaign,
    assert_queries,require_projection,corrupt_field,assert_stale,safety,TargetingClient,INSTALLED_INJECTION)


@pytest.mark.parametrize('name',sorted(SHARED_READS|SHARED_WRITES))
def test_exact_metadata_and_read_only_boundary(tmp_path,name):
    server,_=setup(tmp_path,[name]);schema=h.tool_map(server)[name].input_schema
    parameters,required=SIGNATURES[name]
    assert set(schema['properties'])==set(parameters)
    assert set(schema.get('required',[]))==set(required)
    ro=h.build_server(tmp_path,client=TargetingClient())
    assert (name in h.tool_names(ro))==(name in SHARED_READS)


@pytest.mark.parametrize('name',sorted(SHARED_READS))
def test_authored_read_golden(tmp_path,name):
    golden(tmp_path,name)


@pytest.mark.parametrize('name',sorted(SHARED_READS))
@pytest.mark.parametrize('customer',[None,h.CUSTOMER_ID,h.CUSTOMER_ID_DASHED,h.OTHER_CUSTOMER_ID])
def test_each_query_surface_preserves_explicit_account_and_sibling_isolation(tmp_path,name,customer):
    server,provider=setup(tmp_path,[name],read_only=True)
    args={} if name.startswith('list_') else {'shared_set_id':'401'}
    args['customer_id']=customer
    before=deepcopy(provider.data)
    payload=h.expect_ok(h.call(server,name,args));wanted=h.CUSTOMER_ID if customer is None else customer.replace('-','')
    assert payload['customer_id']==wanted
    assert {call.customer_id for call in provider.searches}=={wanted}
    text=json.dumps(payload)
    assert ('customers/'+(h.OTHER_CUSTOMER_ID if wanted==h.CUSTOMER_ID else h.CUSTOMER_ID)+'/') not in text
    if name.startswith('get_'):
        assert rn('sharedCriteria','402~603',wanted) not in text
        assert rn('campaignSharedSets','703~402',wanted) not in text
        for identity in ('401~601','401~602'):
            assert rn('sharedCriteria',identity,wanted) in text
        assert 'SHOPPING' in text and 'SEARCH' in text
    assert provider.data==before and not provider.mutations


@pytest.mark.parametrize('tool',sorted(SHARED_WRITES))
def test_service_specific_atomic_request_and_unrelated_state_preservation(tmp_path,tool):
    server,provider=setup(tmp_path,[tool]);args=deepcopy(SHARED_ARGS[tool]);plan=stage(server,tool,args)
    assert plan['irreversible']==(tool.startswith('remove_') or tool.startswith('detach_'))
    if 'shared_set_id' in args:
        text=json.dumps(plan)
        for identity in (rn('sharedSets',401),rn('sharedCriteria','401~601'),rn('sharedCriteria','401~602'),
                         rn('campaigns',701),rn('campaigns',702)):
            assert identity in text,identity
        assert 'SHOPPING' in text and 'SEARCH' in text and 'ENABLED' in text
    service,method,request_type=(('SharedSetService','mutate_shared_sets','MutateSharedSetsRequest') if tool.startswith('create_') else
        ('SharedCriterionService','mutate_shared_criteria','MutateSharedCriteriaRequest') if tool.startswith(('add_','remove_')) else
        ('CampaignSharedSetService','mutate_campaign_shared_sets','MutateCampaignSharedSetsRequest'))
    request,before=checked_apply(server,provider,plan,service,method,request_type)
    assert len(request.operations)==1
    operation=request.operations[0];kind=operation._pb.WhichOneof('operation')
    raw=MessageToDict(operation._pb,preserving_proto_field_name=True)
    if tool.startswith('create_'):
        assert kind=='create' and operation.create.name==args['name']
        assert operation.create.type_.name=='NEGATIVE_KEYWORDS'
        assert set(raw['create'])<={'name','type_','type','status'}
        expected=deepcopy(before)
        expected[h.CUSTOMER_ID]['shared_set'].append(shared_set(9001,name=args['name'],member_count=0,reference_count=0))
    elif tool.startswith('add_'):
        assert kind=='create' and operation.create.shared_set==rn('sharedSets',401)
        assert set(raw['create'])=={'shared_set','keyword'},'Do not set optional negative; container provides semantics'
        assert operation.create.keyword.text=='sample clearance' and operation.create.keyword.match_type.name=='PHRASE'
        expected=deepcopy(before)
        expected[h.CUSTOMER_ID]['shared_criterion'].append(member(9001,keyword=args['keywords'][0]))
        expected[h.CUSTOMER_ID]['shared_set'][0]['shared_set']['member_count']=3
    elif tool.startswith('remove_'):
        assert raw=={'remove':rn('sharedCriteria','401~601')}
        expected=deepcopy(before);expected[h.CUSTOMER_ID]['shared_criterion'].pop(0)
        expected[h.CUSTOMER_ID]['shared_set'][0]['shared_set']['member_count']=1
    elif tool.startswith('attach_'):
        assert kind=='create' and raw['create']=={'campaign':rn('campaigns',703),'shared_set':rn('sharedSets',401)}
        expected=deepcopy(before);expected[h.CUSTOMER_ID]['campaign_shared_set'].append(link(703))
        expected[h.CUSTOMER_ID]['shared_set'][0]['shared_set']['reference_count']=3
    else:
        assert raw=={'remove':rn('campaignSharedSets','701~401')}
        expected=deepcopy(before);expected[h.CUSTOMER_ID]['campaign_shared_set'].pop(0)
        expected[h.CUSTOMER_ID]['shared_set'][0]['shared_set']['reference_count']=1
    # Compare canonical protobuf JSON to ignore only equivalent Python int64/enum
    # representation; every unrelated resource and writable setting is retained.
    for cid in before:
        for resource in expected[cid]:
            if resource=='untouched_budget_state':
                assert provider.data[cid][resource]==expected[cid][resource]
            else:
                normalize=lambda rows:[MessageToDict(h.make_row(row)._pb,preserving_proto_field_name=True) for row in rows]
                assert normalize(provider.data[cid][resource])==normalize(expected[cid][resource]),resource


@pytest.mark.parametrize('tool',['remove_shared_negative_keywords','detach_shared_negative_keyword_list'])
def test_any_removal_needs_acknowledgement(tmp_path,tool):
    server,provider=setup(tmp_path,[tool]);plan=stage(server,tool,SHARED_ARGS[tool]);preview(server,plan)
    assert plan['irreversible'] is True
    assert refusal(apply(server,plan,ack=False),provider)['code']=='IRREVERSIBLE_CONFIRMATION_REQUIRED'
    assert not provider.live_mutations()
    assert h.expect_ok(apply(server,plan))['applied'] is True


@pytest.mark.parametrize('tool',sorted(SHARED_WRITES-{'create_shared_negative_keyword_list'}))
@pytest.mark.parametrize('value',BAD_IDS)
def test_original_resource_id_types_refuse_before_reads(tmp_path,tool,value):
    server,provider=setup(tmp_path,[tool]);args={**deepcopy(SHARED_ARGS[tool]),'shared_set_id':value}
    rejected(server,provider,tool,args,local=True)


@pytest.mark.parametrize('name',sorted(SHARED_READS|SHARED_WRITES))
@pytest.mark.parametrize('value',BAD_CUSTOMERS)
def test_account_type_and_configured_mutation_binding(tmp_path,name,value):
    server,provider=setup(tmp_path,[name]);args=deepcopy(SHARED_ARGS.get(name,{}))
    if name=='get_shared_negative_keyword_list':args['shared_set_id']='401'
    rejected(server,provider,name,{**args,'customer_id':value},local=True)


@pytest.mark.parametrize('name',sorted(SHARED_READS|SHARED_WRITES))
def test_extra_keys_and_secret_named_keys_refuse_before_provider_reads(tmp_path,name):
    server,provider=setup(tmp_path,[name]);args=deepcopy(SHARED_ARGS.get(name,{}))
    if name=='get_shared_negative_keyword_list':args['shared_set_id']='401'
    for extra in ({'unexpected':True},dict([(h.FAKE_REFRESH_TOKEN,'synthetic-private')]),{'bypass_require_dry_run':True}):
        rejected(server,provider,name,{**args,**extra},local=True)


@pytest.mark.parametrize('value',[None,True,3,[],{},'', ' ', ' name','name ', 'a\x00b','a\nb','a\x85b','a\ud800b','x'*256,'é'*128])
def test_create_name_original_type_unicode_and_byte_boundaries(tmp_path,value):
    server,provider=setup(tmp_path,['create_shared_negative_keyword_list'])
    rejected(server,provider,'create_shared_negative_keyword_list',{'name':value},local=True)


@pytest.mark.parametrize('name',['A','é'*127+'a','Promotions – été','Café: 50% off / 2026'])
def test_valid_name_bytes_and_punctuation_are_preserved(tmp_path,name):
    server,provider=setup(tmp_path,['create_shared_negative_keyword_list']);plan=stage(server,'create_shared_negative_keyword_list',{'name':name,'customer_id':None})
    request,_=checked_apply(server,provider,plan,'SharedSetService','mutate_shared_sets','MutateSharedSetsRequest')
    assert request.operations[0].create.name==name


@pytest.mark.parametrize('phase',['stage','apply'])
def test_active_name_collision_uses_nfc_casefold_without_rewriting(tmp_path,phase):
    server,provider=setup(tmp_path,['create_shared_negative_keyword_list'])
    args={'name':'Café LIST'}
    if phase=='apply':plan=stage(server,'create_shared_negative_keyword_list',args)
    provider.data[h.CUSTOMER_ID]['shared_set'][0]['shared_set']['name']='Cafe\u0301 list'
    if phase=='stage':rejected(server,provider,'create_shared_negative_keyword_list',args)
    else:assert_stale(server,provider,plan)

BAD_KEYWORDS=[None,True,2,{},'[]','null',[],[{}],[{'text':'valid'}],[{'text':'valid','match_type':'EXACT','extra':True}]]
BAD_KEYWORDS += [[{'text':value,'match_type':'EXACT'}] for value in (None,True,2,[],{},'', ' ', ' valid','valid ','a\nb','a\ud800b','x'*81,'one '*10+'eleven')]
BAD_KEYWORDS += [[{'text':'valid','match_type':value}] for value in (None,True,2,[],{},'', 'exact','UNKNOWN','UNSPECIFIED','EXACT ')]
BAD_KEYWORDS += [[{'text':'valid','match_type':'EXACT'}]*2,[{'text':'CAFÉ','match_type':'EXACT'},{'text':'Cafe\u0301','match_type':'EXACT'}],
                 [{'text':f'new keyword {i}','match_type':'EXACT'} for i in range(101)]]


@pytest.mark.parametrize('keywords',BAD_KEYWORDS)
def test_nested_keyword_types_extra_fields_bounds_and_duplicates_refuse_locally(tmp_path,keywords):
    server,provider=setup(tmp_path,['add_shared_negative_keywords'])
    rejected(server,provider,'add_shared_negative_keywords',{'shared_set_id':'401','keywords':keywords},local=True)


@pytest.mark.parametrize('keywords',[
    [{'text':'é'*80,'match_type':'EXACT'}],
    [{'text':'un deux trois quatre cinq six sept huit neuf dix','match_type':'PHRASE'}],
    [{'text':'Café + tea / 50%','match_type':match} for match in ('BROAD','PHRASE','EXACT')],
    [{'text':f'fresh word {i}','match_type':'BROAD'} for i in range(100)]])
def test_keyword_valid_neighbors_preserve_original_text_and_batch(tmp_path,keywords):
    server,provider=setup(tmp_path,['add_shared_negative_keywords'])
    plan=stage(server,'add_shared_negative_keywords',{'shared_set_id':'401','keywords':keywords})
    request,_=checked_apply(server,provider,plan,'SharedCriterionService','mutate_shared_criteria','MutateSharedCriteriaRequest')
    assert [(op.create.keyword.text,op.create.keyword.match_type.name) for op in request.operations]==[(k['text'],k['match_type']) for k in keywords]
    assert all(set(MessageToDict(op.create._pb,preserving_proto_field_name=True))=={'shared_set','keyword'} for op in request.operations)


@pytest.mark.parametrize('tool,key',[('remove_shared_negative_keywords','criterion_ids'),('attach_shared_negative_keyword_list','campaign_ids'),('detach_shared_negative_keyword_list','campaign_ids')])
@pytest.mark.parametrize('values',[None,True,1,{},'[]','["601"]',[],['601','601'],['0601'],[True],[601],[None],[[]],['0'],[str(i) for i in range(1,102)]])
def test_id_batch_raw_types_distinctness_and_limits(tmp_path,tool,key,values):
    server,provider=setup(tmp_path,[tool]);rejected(server,provider,tool,{'shared_set_id':'401',key:values},local=True)


@pytest.mark.parametrize('tool',sorted(SHARED_WRITES))
def test_foreign_mutation_account_is_not_silently_replaced(tmp_path,tool):
    server,provider=setup(tmp_path,[tool]);rejected(server,provider,tool,{**SHARED_ARGS[tool],'customer_id':h.OTHER_CUSTOMER_ID},local=True)


@pytest.mark.parametrize('tool,args',[
    ('add_shared_negative_keywords',{'shared_set_id':'401','keywords':[{'text':'CLEARANCE ITEM 601','match_type':'EXACT'}]}),
    ('remove_shared_negative_keywords',{'shared_set_id':'401','criterion_ids':['603']}),
    ('attach_shared_negative_keyword_list',{'shared_set_id':'401','campaign_ids':['701']}),
    ('detach_shared_negative_keyword_list',{'shared_set_id':'401','campaign_ids':['703']})])
def test_existing_additions_missing_removals_and_link_noops_refuse(tmp_path,tool,args):
    server,provider=setup(tmp_path,[tool]);rejected(server,provider,tool,args)


@pytest.mark.parametrize('negative',[False,True])
def test_member_negative_flag_is_observed_metadata_not_a_positive_keyword(tmp_path,negative):
    server,provider=setup(tmp_path,['get_shared_negative_keyword_list','add_shared_negative_keywords'])
    provider.data[h.CUSTOMER_ID]['shared_criterion'][0]['shared_criterion']['negative']=negative
    payload=h.expect_ok(h.call(server,'get_shared_negative_keyword_list',{'shared_set_id':'401'}))
    assert any(row['criterion_id']=='601' and row['negative'] is negative for row in payload['keywords'])
    plan=stage(server,'add_shared_negative_keywords',SHARED_ARGS['add_shared_negative_keywords'])
    provider.data[h.CUSTOMER_ID]['shared_criterion'][0]['shared_criterion']['negative']=not negative
    assert_stale(server,provider,plan)


@pytest.mark.parametrize('tool',sorted(SHARED_WRITES-{'create_shared_negative_keyword_list'}))
@pytest.mark.parametrize('field,value',[('advertising_channel_type','DISPLAY'),('advertising_channel_type','PERFORMANCE_MAX'),
    ('advertising_channel_sub_type','SEARCH_MOBILE_APP'),('status','REMOVED')])
def test_unsupported_existing_attachment_blocks_all_edits(tmp_path,tool,field,value):
    server,provider=setup(tmp_path,[tool]);provider.data[h.CUSTOMER_ID]['campaign'][1]['campaign'][field]=value
    rejected(server,provider,tool,SHARED_ARGS[tool])


@pytest.mark.parametrize('status',['ENABLED','PAUSED'])
@pytest.mark.parametrize('channel',['SEARCH','SHOPPING'])
def test_selected_supported_campaigns_allow_attach(tmp_path,status,channel):
    tool='attach_shared_negative_keyword_list';server,provider=setup(tmp_path,[tool])
    provider.data[h.CUSTOMER_ID]['campaign'][2]['campaign'].update(status=status,advertising_channel_type=channel)
    plan=stage(server,tool,SHARED_ARGS[tool]);checked_apply(server,provider,plan,'CampaignSharedSetService','mutate_campaign_shared_sets','MutateCampaignSharedSetsRequest')


@pytest.mark.parametrize('resource,field,value',[
    ('shared_set','shared_set.resource_name',rn('sharedSets',401,h.OTHER_CUSTOMER_ID)),
    ('shared_set','shared_set.id',999),('shared_set','shared_set.id',0),('shared_set','shared_set.type_',123456),
    ('shared_set','shared_set.type_','ACCOUNT_LEVEL_NEGATIVE_KEYWORDS'),('shared_set','shared_set.status','REMOVED'),
    ('shared_set','shared_set.status',123456),('shared_set','shared_set.member_count',-1),('shared_set','shared_set.member_count',3),
    ('shared_set','shared_set.reference_count',0),('shared_set','shared_set.reference_count',3),
    ('shared_criterion','shared_criterion.resource_name',rn('sharedCriteria','402~601')),
    ('shared_criterion','shared_criterion.shared_set',rn('sharedSets',402)),
    ('shared_criterion','shared_criterion.criterion_id',0),('shared_criterion','shared_criterion.type_',123456),
    ('shared_criterion','shared_criterion.keyword.match_type',123456),('shared_criterion','shared_criterion.type_','PLACEMENT'),
    ('campaign_shared_set','campaign_shared_set.resource_name',rn('campaignSharedSets','703~401')),
    ('campaign_shared_set','campaign_shared_set.campaign',rn('campaigns',701,h.OTHER_CUSTOMER_ID)),
    ('campaign_shared_set','campaign_shared_set.status',123456),
    ('campaign','campaign.id',999),('campaign','campaign.status',123456),
    ('campaign','campaign.advertising_channel_type',123456),('campaign','campaign.advertising_channel_sub_type',123456)])
def test_unverifiable_resource_and_enum_state_refuses_cleanly(tmp_path,resource,field,value,capfd):
    tool='add_shared_negative_keywords';server,provider=setup(tmp_path,[tool]);corrupt_field(provider,resource,field,value)
    rejected(server,provider,tool,SHARED_ARGS[tool]);output=capfd.readouterr()
    assert not output.err and 'Traceback' not in output.out


@pytest.mark.parametrize('resource',['shared_set','shared_criterion','campaign_shared_set','campaign'])
@pytest.mark.parametrize('fault',['duplicate','missing','partial'])
def test_complete_state_requires_unique_rows_and_successful_iteration(tmp_path,resource,fault):
    tool='add_shared_negative_keywords';server,provider=setup(tmp_path,[tool])
    if fault=='duplicate':provider.corrupt[resource]=lambda rows:rows+deepcopy(rows[:1])
    elif fault=='missing':provider.corrupt[resource]=lambda rows:[]
    else:provider.fail_after[resource]=1
    rejected(server,provider,tool,SHARED_ARGS[tool])


@pytest.mark.parametrize('resource,cap',[('shared_set',100),('shared_criterion',5000),('campaign_shared_set',1000)])
@pytest.mark.parametrize('over',[False,True])
def test_realistic_complete_read_bounds_and_one_lookahead(tmp_path,resource,cap,over):
    name='list_shared_negative_keyword_lists' if resource=='shared_set' else 'get_shared_negative_keyword_list'
    server,provider=setup(tmp_path,[name],read_only=True);data=provider.data[h.CUSTOMER_ID];count=cap+int(over)
    if resource=='shared_set':data[resource]=[shared_set(i+401,member_count=0,reference_count=0) for i in range(count)]
    elif resource=='shared_criterion':
        data[resource]=[member(i+601) for i in range(count)];data['shared_set'][0]['shared_set']['member_count']=count
    else:
        data[resource]=[link(i+701) for i in range(count)]
        data['campaign']=[campaign(i+701) for i in range(count)]
        data['shared_set'][0]['shared_set']['reference_count']=count
    args={} if name.startswith('list_') else {'shared_set_id':'401'}
    if over:rejected(server,provider,name,args)
    else:
        result=h.expect_ok(h.call(server,name,args))
        assert len(result['lists' if resource=='shared_set' else 'keywords' if resource=='shared_criterion' else 'campaigns'])==cap
    assert provider.pulls[resource]<=cap+1
    for call in provider.searches:
        if h._FROM_RE.search(call.query).group(1)==resource:
            assert re.search(r'\bLIMIT\s+'+str(cap+1)+r'\s*$',call.query,re.I)


@pytest.mark.parametrize('tool,resource,cap',[('add_shared_negative_keywords','shared_criterion',5000),('attach_shared_negative_keyword_list','campaign_shared_set',1000)])
def test_proposed_membership_and_link_limits_reject_overflow(tmp_path,tool,resource,cap):
    server,provider=setup(tmp_path,[tool]);data=provider.data[h.CUSTOMER_ID]
    if resource=='shared_criterion':
        data[resource]=[member(i+1000) for i in range(cap)];data['shared_set'][0]['shared_set']['member_count']=cap
    else:
        data[resource]=[link(i+1000) for i in range(cap)];data['campaign'] += [campaign(i+1000) for i in range(cap)]
        data['shared_set'][0]['shared_set']['reference_count']=cap
    rejected(server,provider,tool,SHARED_ARGS[tool])


def test_singular_identity_lookahead_and_state_byte_bound(tmp_path):
    tool='get_shared_negative_keyword_list';server,provider=setup(tmp_path,[tool],read_only=True)
    h.expect_ok(h.call(server,tool,{'shared_set_id':'401'}))
    singles=[call for call in provider.searches if h._FROM_RE.search(call.query).group(1)=='shared_set']
    assert singles and all(re.search(r'\bLIMIT\s+2\s*$',call.query,re.I) for call in singles)
    provider.data[h.CUSTOMER_ID]['shared_criterion'][0]['shared_criterion']['keyword']['text']='x'*(16*1024*1024+1)
    rejected(server,provider,tool,{'shared_set_id':'401'})


@pytest.mark.parametrize('resource,field,value',[
    ('shared_set','name','A new name'),('shared_criterion','negative',True),
    ('campaign_shared_set','status','REMOVED'),('campaign','name','A changed campaign'),
    ('campaign','status','PAUSED'),('campaign','advertising_channel_type','SHOPPING')])
def test_all_relevant_state_is_rechecked_before_live_write(tmp_path,resource,field,value):
    tool='add_shared_negative_keywords';server,provider=setup(tmp_path,[tool]);plan=stage(server,tool,SHARED_ARGS[tool])
    provider.data[h.CUSTOMER_ID][resource][0][resource][field]=value
    assert_stale(server,provider,plan)


def test_reordering_complete_rows_does_not_stale(tmp_path):
    tool='add_shared_negative_keywords';server,provider=setup(tmp_path,[tool]);plan=stage(server,tool,SHARED_ARGS[tool])
    for value in provider.data[h.CUSTOMER_ID].values():
        if isinstance(value,list):value.reverse()
    checked_apply(server,provider,plan,'SharedCriterionService','mutate_shared_criteria','MutateSharedCriteriaRequest')


@pytest.mark.parametrize('tool',sorted(SHARED_WRITES))
@pytest.mark.parametrize('case',['preview','expiry','replay','lost_response','provider_rejection','stage_audit','pre_audit','terminal_audit','concurrent'])
def test_each_shared_plan_preserves_lifecycle_and_no_retry(tmp_path,tool,case):
    safety(tmp_path,tool,SHARED_ARGS[tool],case)


@pytest.mark.parametrize('resource,field',[('shared_set','shared_set.type_'),('shared_set','shared_set.status'),
    ('shared_criterion','shared_criterion.type_'),('shared_criterion','shared_criterion.keyword.match_type'),
    ('campaign_shared_set','campaign_shared_set.status'),('campaign','campaign.status'),
    ('campaign','campaign.advertising_channel_type'),('campaign','campaign.advertising_channel_sub_type')])
def test_installed_unknown_enums_refuse_without_sdk_stderr(tmp_path,monkeypatch,resource,field):
    import test_auth_cause_contract as process
    setup(tmp_path,['add_shared_negative_keywords'])
    monkeypatch.setattr(process,'INJECTION',INSTALLED_INJECTION)
    with process.InstalledServer(tmp_path/'installed') as installed:
        installed.mode(corruption=[resource,field,123456])
        result=installed.call('add_shared_negative_keywords',SHARED_ARGS['add_shared_negative_keywords'])
        assert h.error_of(result)['code']!='INTERNAL' and not installed.events('targeting mutation')
    assert not installed.stderr


@pytest.mark.parametrize('resource',['shared_criterion','campaign_shared_set'])
def test_same_count_duplicate_identity_cannot_hide_behind_matching_counters(tmp_path,resource):
    server,provider=setup(tmp_path,['add_shared_negative_keywords'])
    provider.corrupt[resource]=lambda rows:[deepcopy(rows[0]),deepcopy(rows[0])]
    rejected(server,provider,'add_shared_negative_keywords',SHARED_ARGS['add_shared_negative_keywords'])


@pytest.mark.parametrize('name',sorted(SHARED_READS))
def test_catalog_and_detail_unknown_state_refuse_on_actual_read_boundary(tmp_path,name,capfd):
    server,provider=setup(tmp_path,[name],read_only=True)
    corrupt_field(provider,'shared_set','shared_set.status',123456)
    rejected(server,provider,name,{} if name.startswith('list_') else {'shared_set_id':'401'})
    assert not capfd.readouterr().err


def test_removed_and_unrelated_list_types_do_not_collide_with_active_negative_names(tmp_path):
    server,provider=setup(tmp_path,['create_shared_negative_keyword_list'])
    data=provider.data[h.CUSTOMER_ID]
    data['shared_set'] += [shared_set(403,name='Available name',status='REMOVED',member_count=0,reference_count=0),
        shared_set(404,name='Available name',type_='NEGATIVE_PLACEMENTS',member_count=0,reference_count=0)]
    plan=stage(server,'create_shared_negative_keyword_list',{'name':'Available name'})
    checked_apply(server,provider,plan,'SharedSetService','mutate_shared_sets','MutateSharedSetsRequest')


def test_canonical_maximum_signed_id_is_not_rejected_as_a_number(tmp_path):
    name='get_shared_negative_keyword_list';server,provider=setup(tmp_path,[name],read_only=True)
    ident=9223372036854775807
    provider.data[h.CUSTOMER_ID]['shared_set']=[shared_set(ident,member_count=0,reference_count=0)]
    provider.data[h.CUSTOMER_ID]['shared_criterion']=[];provider.data[h.CUSTOMER_ID]['campaign_shared_set']=[]
    payload=h.expect_ok(h.call(server,name,{'shared_set_id':str(ident),'customer_id':None}))
    assert payload['shared_set']['shared_set_id']==str(ident)


def test_omitted_optional_member_negative_flag_is_a_valid_container_member(tmp_path):
    server,provider=setup(tmp_path,['get_shared_negative_keyword_list','add_shared_negative_keywords'])
    provider.data[h.CUSTOMER_ID]['shared_criterion'][0]['shared_criterion'].pop('negative')
    payload=h.expect_ok(h.call(server,'get_shared_negative_keyword_list',{'shared_set_id':'401'}))
    assert next(item for item in payload['keywords'] if item['criterion_id']=='601')['negative'] is False
    plan=stage(server,'add_shared_negative_keywords',SHARED_ARGS['add_shared_negative_keywords'])
    request,_=checked_apply(server,provider,plan,'SharedCriterionService','mutate_shared_criteria','MutateSharedCriteriaRequest')
    assert not request.operations[0].create._pb.HasField('negative')
