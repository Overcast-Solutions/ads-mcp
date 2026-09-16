"""F078: explicit demographic state and conservative local edit rules."""
from copy import deepcopy
import json
import re

import pytest
from google.protobuf.json_format import MessageToDict

import harness as h
from offline_contract import refusal
from shared_targeting_oracle import (CATEGORIES,CUSTOM,NUMERIC,STRINGS,LISTS,DEMO_READS,DEMO_WRITES,SIGNATURES,BAD_IDS,BAD_CUSTOMERS,
    setup,stage,preview,apply,checked_apply,rejected,golden,rn,demographic,group,campaign,
    require_projection,corrupt_field,assert_stale,safety,TargetingClient,INSTALLED_INJECTION)

READ='get_demographic_targeting'
WRITE='update_demographic_targeting'
CHANNEL_DIMENSIONS=[(channel,dimension) for channel in ('SEARCH','DISPLAY') for dimension in CATEGORIES if channel=='DISPLAY' or dimension!='PARENTAL_STATUS']
LEGAL=[(channel,dimension,value) for channel,dimension in CHANNEL_DIMENSIONS for value in CATEGORIES[dimension]]


def change(dimension='GENDER',value='FEMALE',action='EXCLUDE'):
    return {'dimension':dimension,'value':value,'action':action}


def arguments(changes=None,channel='SEARCH'):
    return {'ad_group_id':'801' if channel=='SEARCH' else '804','changes':[change()] if changes is None else changes}


def configure(provider,channel,rows):
    ident=801 if channel=='SEARCH' else 804
    data=provider.data[h.CUSTOMER_ID]
    data['ad_group_criterion']=[r for r in data['ad_group_criterion'] if r['ad_group_criterion']['ad_group']!=rn('adGroups',ident)]
    data['ad_group_criterion'] += rows


@pytest.mark.parametrize('name',[READ,WRITE])
def test_exact_demographic_signatures_and_read_only_visibility(tmp_path,name):
    server,_=setup(tmp_path,[name]);schema=h.tool_map(server)[name].input_schema
    parameters,required=SIGNATURES[name]
    assert set(schema['properties'])==set(parameters) and set(schema['required'])==set(required)
    ro=h.build_server(tmp_path,client=TargetingClient())
    assert (name in h.tool_names(ro))==(name==READ)


def test_independently_authored_demographic_read_golden(tmp_path):
    golden(tmp_path,READ)


@pytest.mark.parametrize('customer',[None,h.CUSTOMER_ID,h.CUSTOMER_ID_DASHED,h.OTHER_CUSTOMER_ID])
@pytest.mark.parametrize('channel',['SEARCH','DISPLAY'])
def test_read_identity_categories_parents_and_second_account_isolation(tmp_path,customer,channel):
    server,provider=setup(tmp_path,[READ],read_only=True);ident='801' if channel=='SEARCH' else '804'
    before=deepcopy(provider.data)
    payload=h.expect_ok(h.call(server,READ,{'ad_group_id':ident,'customer_id':customer}))
    wanted=h.CUSTOMER_ID if customer is None else customer.replace('-','')
    assert payload['customer_id']==wanted and {call.customer_id for call in provider.searches}=={wanted}
    assert payload['supported_values']=={d:v for d,v in CATEGORIES.items() if channel=='DISPLAY' or d!='PARENTAL_STATUS'}
    text=json.dumps(payload)
    assert rn('adGroups',ident,wanted) in text
    assert rn('adGroupCriteria','802~602',wanted) not in text
    assert 'target_restrictions' in text and 'optimized_targeting_enabled' in text
    assert 'effective' in text.lower() and ('default' in text.lower() or 'unconfigured' in text.lower())
    other=h.OTHER_CUSTOMER_ID if wanted==h.CUSTOMER_ID else h.CUSTOMER_ID
    assert 'customers/'+other+'/' not in text
    assert provider.data==before and not provider.mutations


@pytest.mark.parametrize('channel,dimension,value',LEGAL)
@pytest.mark.parametrize('action',['INCLUDE','EXCLUDE'])
def test_every_supported_category_and_channel_builds_genuine_immutable_create(tmp_path,channel,dimension,value,action):
    server,provider=setup(tmp_path,[WRITE]);ident=801 if channel=='SEARCH' else 804
    configure(provider,channel,[])
    plan=stage(server,WRITE,arguments([change(dimension,value,action)],channel))
    assert plan['irreversible'] is False
    request,before=checked_apply(server,provider,plan,'AdGroupCriterionService','mutate_ad_group_criteria','MutateAdGroupCriteriaRequest')
    assert len(request.operations)==1
    op=request.operations[0];assert op._pb.WhichOneof('operation')=='create'
    raw=MessageToDict(op.create._pb,preserving_proto_field_name=True)
    assert set(raw)<={'ad_group','negative','status',dimension.lower()}
    assert op.create.ad_group==rn('adGroups',ident)
    assert op.create.negative is (action=='EXCLUDE')
    assert getattr(op.create,dimension.lower()).type_.name==value
    if action=='INCLUDE':assert op.create.status.name=='ENABLED'
    assert untouched(before,provider.data,ident)


def untouched(before,after,ident):
    expected=deepcopy(before)
    keep=lambda rows:[r for r in rows if r['ad_group_criterion']['ad_group']!=rn('adGroups',ident)]
    assert keep(after[h.CUSTOMER_ID]['ad_group_criterion'])==keep(before[h.CUSTOMER_ID]['ad_group_criterion'])
    expected[h.CUSTOMER_ID]['ad_group_criterion']=after[h.CUSTOMER_ID]['ad_group_criterion']
    assert expected==after,'Only criteria inside the selected ad group may change'
    return True


@pytest.mark.parametrize('before_negative,before_status,action,expected',[ (False,'PAUSED','INCLUDE','update'),
    (True,'ENABLED','INCLUDE','replace'),(False,'ENABLED','EXCLUDE','replace'),(False,'PAUSED','EXCLUDE','replace'),
    (False,'REMOVED','INCLUDE','create'),(True,'REMOVED','EXCLUDE','create')])
@pytest.mark.parametrize('channel,dimension',CHANNEL_DIMENSIONS)
def test_transition_identity_polarity_masks_and_removal_order(tmp_path,before_negative,before_status,action,expected,channel,dimension):
    server,provider=setup(tmp_path,[WRITE]);ident=801 if channel=='SEARCH' else 804;value=CATEGORIES[dimension][0]
    configure(provider,channel,[demographic(dimension,value,610,ident,negative=before_negative,status=before_status)])
    plan=stage(server,WRITE,arguments([change(dimension,value,action)],channel))
    assert plan['irreversible']==(expected=='replace')
    if expected=='replace':
        preview(server,plan)
        assert refusal(apply(server,plan,ack=False),provider)['code']=='IRREVERSIBLE_CONFIRMATION_REQUIRED'
        assert rn('adGroupCriteria',f'{ident}~610') in json.dumps(plan)
        assert 'before' in json.dumps(plan) and 'after' in json.dumps(plan)
    request,before=checked_apply(server,provider,plan,'AdGroupCriterionService','mutate_ad_group_criteria','MutateAdGroupCriteriaRequest')
    kinds=[op._pb.WhichOneof('operation') for op in request.operations]
    assert kinds==(['remove','create'] if expected=='replace' else [expected])
    if expected=='update':
        op=request.operations[0]
        assert list(op.update_mask.paths)==['status'] and op.update.status.name=='ENABLED'
        assert op.update.resource_name==rn('adGroupCriteria',f'{ident}~610')
        assert set(MessageToDict(op.update._pb,preserving_proto_field_name=True))=={'resource_name','status'}
    else:
        op=request.operations[-1]
        assert op.create.negative is (action=='EXCLUDE')
        assert op.create.ad_group==rn('adGroups',ident)
        if expected=='replace':assert request.operations[0].remove==rn('adGroupCriteria',f'{ident}~610')
    untouched(before,provider.data,ident)


@pytest.mark.parametrize('negative,action',[(False,'INCLUDE'),(True,'EXCLUDE')])
def test_all_noop_refuses_without_creating_plan(tmp_path,negative,action):
    server,provider=setup(tmp_path,[WRITE]);configure(provider,'SEARCH',[demographic('GENDER','FEMALE',negative=negative)])
    rejected(server,provider,WRITE,arguments([change(action=action)]))


@pytest.mark.parametrize('field',CUSTOM)
@pytest.mark.parametrize('action',['INCLUDE','EXCLUDE'])
def test_replacement_refuses_every_direct_customization_and_explicit_numeric_presence(tmp_path,field,action):
    server,provider=setup(tmp_path,[WRITE]);value=(1 if field=='bid_modifier' else 0) if field in NUMERIC else (
        'campaign=original' if field=='final_url_suffix' else 'https://track.example.invalid/?u={lpurl}' if field=='tracking_url_template' else
        [{'key':'season','value':'Spring'}] if field=='url_custom_parameters' else [rn('labels',44)] if field=='labels' else ['https://example.invalid/Original'])
    configure(provider,'SEARCH',[demographic('GENDER','FEMALE',negative=action=='INCLUDE',**{field:value})])
    rejected(server,provider,WRITE,arguments([change(action=action)]))
    require_projection(provider,'ad_group_criterion',CUSTOM)


@pytest.mark.parametrize('field',NUMERIC)
def test_presence_of_zero_numeric_values_is_not_absence(tmp_path,field):
    server,provider=setup(tmp_path,[WRITE]);configure(provider,'SEARCH',[demographic('GENDER','FEMALE',**{field:0})])
    rejected(server,provider,WRITE,arguments())


@pytest.mark.parametrize('field',CUSTOM)
def test_positive_status_only_update_preserves_all_customization(tmp_path,field):
    server,provider=setup(tmp_path,[WRITE]);value=(1 if field=='bid_modifier' else 0) if field in NUMERIC else (
        'retained=Yes' if field in STRINGS else [{'key':'s','value':'v'}] if field=='url_custom_parameters' else [rn('labels',44)] if field=='labels' else ['https://example.invalid/a'])
    row=demographic('GENDER','FEMALE',status='PAUSED',**{field:value})
    configure(provider,'SEARCH',[row]);plan=stage(server,WRITE,arguments([change(action='INCLUDE')]))
    request,before=checked_apply(server,provider,plan,'AdGroupCriterionService','mutate_ad_group_criteria','MutateAdGroupCriteriaRequest')
    assert len(request.operations)==1 and list(request.operations[0].update_mask.paths)==['status']
    expected=deepcopy(before)
    target=next(r['ad_group_criterion'] for r in expected[h.CUSTOMER_ID]['ad_group_criterion'] if r['ad_group_criterion']['resource_name']==rn('adGroupCriteria','801~601'))
    target['status']='ENABLED'
    assert provider.data==expected


def test_empty_strings_lists_and_output_only_effective_bids_allow_replacement(tmp_path):
    server,provider=setup(tmp_path,[WRITE]);extra={**{f:'' for f in STRINGS},**{f:[] for f in LISTS},
        'effective_cpc_bid_micros':1200000,'effective_cpm_bid_micros':0,'approval_status':'APPROVED'}
    configure(provider,'SEARCH',[demographic('GENDER','FEMALE',**extra)])
    plan=stage(server,WRITE,arguments());request,_=checked_apply(server,provider,plan,'AdGroupCriterionService','mutate_ad_group_criteria','MutateAdGroupCriteriaRequest')
    assert [op._pb.WhichOneof('operation') for op in request.operations]==['remove','create']


@pytest.mark.parametrize('field',NUMERIC)
def test_read_snapshots_distinguish_absent_and_explicit_zero(tmp_path,field):
    server,provider=setup(tmp_path,[READ],read_only=True)
    configure(provider,'SEARCH',[demographic('GENDER','FEMALE')])
    before=h.expect_ok(h.call(server,READ,{'ad_group_id':'801'}))
    provider.data[h.CUSTOMER_ID]['ad_group_criterion'][-1]['ad_group_criterion'][field]=0
    after=h.expect_ok(h.call(server,READ,{'ad_group_id':'801'}))
    assert before!=after
    assert after['criteria'][0][field]==0 and before['criteria'][0][field] is None


@pytest.mark.parametrize('channel,dimension',CHANNEL_DIMENSIONS)
@pytest.mark.parametrize('exhaustion',['all','known'])
def test_whole_batch_locally_provable_exclusion_conflicts_refuse_before_reads(tmp_path,channel,dimension,exhaustion):
    server,provider=setup(tmp_path,[WRITE]);values=CATEGORIES[dimension] if exhaustion=='all' else CATEGORIES[dimension][:-1]
    changes=[change(dimension,value) for value in values]
    rejected(server,provider,WRITE,arguments(changes,channel),local=True)


@pytest.mark.parametrize('channel,dimension',CHANNEL_DIMENSIONS)
@pytest.mark.parametrize('level',['group','campaign','combined'])
@pytest.mark.parametrize('phase',['stage','apply'])
def test_existing_plus_batch_exclusions_are_evaluated_as_one_result(tmp_path,channel,dimension,level,phase):
    server,provider=setup(tmp_path,[WRITE]);ident=801 if channel=='SEARCH' else 804;parent=701 if channel=='SEARCH' else 704
    known=CATEGORIES[dimension][:-1];args=arguments([change(dimension,known[-1])],channel)
    configure(provider,channel,[])
    if phase=='apply':plan=stage(server,WRITE,args)
    for i,value in enumerate(known[:-1]):
        campaign_level=level=='campaign' or level=='combined' and i%2==0
        row=demographic(dimension,value,700+i,ident,negative=True,level='campaign' if campaign_level else 'ad_group')
        if campaign_level:
            item=row['campaign_criterion'];item['campaign']=rn('campaigns',parent);item['resource_name']=rn('campaignCriteria',f'{parent}~{700+i}')
        provider.data[h.CUSTOMER_ID]['campaign_criterion' if campaign_level else 'ad_group_criterion'].append(row)
    if phase=='stage':rejected(server,provider,WRITE,args)
    else:assert_stale(server,provider,plan)


@pytest.mark.parametrize('channel,dimension',CHANNEL_DIMENSIONS)
def test_one_known_category_remaining_and_mixed_include_rescue_are_valid(tmp_path,channel,dimension):
    server,provider=setup(tmp_path,[WRITE]);ident=801 if channel=='SEARCH' else 804;known=CATEGORIES[dimension][:-1]
    configure(provider,channel,[demographic(dimension,v,700+i,ident,negative=True) for i,v in enumerate(known[:-1])])
    changes=[change(dimension,known[0],'INCLUDE'),change(dimension,known[-1],'EXCLUDE')]
    plan=stage(server,WRITE,arguments(changes,channel));request,_=checked_apply(server,provider,plan,'AdGroupCriterionService','mutate_ad_group_criteria','MutateAdGroupCriteriaRequest')
    assert sum(op._pb.WhichOneof('operation')=='remove' for op in request.operations)==1
    kinds=[op._pb.WhichOneof('operation') for op in request.operations]
    assert kinds==sorted(kinds,key=lambda value:0 if value=='remove' else 1)


@pytest.mark.parametrize('dimension',['AGE_RANGE','GENDER','INCOME_RANGE'])
def test_campaign_exclusion_blocks_include_with_actionable_guidance(tmp_path,dimension):
    server,provider=setup(tmp_path,[WRITE]);value=CATEGORIES[dimension][0]
    provider.data[h.CUSTOMER_ID]['campaign_criterion']=[demographic(dimension,value,710,negative=True,level='campaign')]
    result=rejected(server,provider,WRITE,arguments([change(dimension,value,'INCLUDE')]))
    assert 'campaign' in h.result_text(result).lower()


@pytest.mark.parametrize('status,negative',[('REMOVED',True),('PAUSED',False)])
def test_removed_and_paused_positive_rows_do_not_count_as_exclusions(tmp_path,status,negative):
    server,provider=setup(tmp_path,[WRITE]);configure(provider,'SEARCH',[demographic('GENDER','MALE',status=status,negative=negative)])
    plan=stage(server,WRITE,arguments());checked_apply(server,provider,plan,'AdGroupCriterionService','mutate_ad_group_criteria','MutateAdGroupCriteriaRequest')


@pytest.mark.parametrize('value',BAD_IDS)
@pytest.mark.parametrize('name',[READ,WRITE])
def test_original_ad_group_ids_are_validated_before_reads(tmp_path,name,value):
    server,provider=setup(tmp_path,[name]);args={'ad_group_id':value}
    if name==WRITE:args['changes']=[change()]
    rejected(server,provider,name,args,local=True)


@pytest.mark.parametrize('name',[READ,WRITE])
@pytest.mark.parametrize('value',BAD_CUSTOMERS)
def test_original_account_types_refuse_before_reads(tmp_path,name,value):
    server,provider=setup(tmp_path,[name]);args={'ad_group_id':'801','customer_id':value}
    if name==WRITE:args['changes']=[change()]
    rejected(server,provider,name,args,local=True)


@pytest.mark.parametrize('extra',[{'unknown':True},{'bypass_require_dry_run':True},dict([(h.FAKE_CLIENT_SECRET,'synthetic-private')]),{'customer_id':h.OTHER_CUSTOMER_ID}])
def test_extra_keys_and_foreign_write_intent_refuse(tmp_path,extra):
    server,provider=setup(tmp_path,[WRITE]);rejected(server,provider,WRITE,{**arguments(),**extra},local=True)

BAD_CHANGES=[None,True,1,{},'[]','null',[],[None],[True],[1],[{}],[{**change(),'extra':True}]]
BAD_CHANGES += [[{k:v for k,v in change().items() if k!=omit}] for omit in ('dimension','value','action')]
BAD_CHANGES += [[{**change(),field:value}] for field in ('dimension','value','action') for value in (None,True,1,{},[],'',' UNKNOWN','UNKNOWN','UNSPECIFIED')]
BAD_CHANGES += [[change('AGE_RANGE','UNDETERMINED')],[change('INCOME_RANGE','UNDETERMINED')],[change(action='exclude')],
    [change(),change()],[change('GENDER','FEMALE','INCLUDE'),change()], [change()]*21]


@pytest.mark.parametrize('changes',BAD_CHANGES)
def test_change_array_original_types_extra_keys_and_finite_values(tmp_path,changes):
    server,provider=setup(tmp_path,[WRITE]);rejected(server,provider,WRITE,arguments(changes),local=True)


@pytest.mark.parametrize('field,value',[('status','REMOVED'),('type_','SEARCH_DYNAMIC_ADS'),('type_','DISPLAY_MOBILE_APP')])
def test_unsupported_ad_group_state_refuses(tmp_path,field,value):
    server,provider=setup(tmp_path,[WRITE]);provider.data[h.CUSTOMER_ID]['ad_group'][0]['ad_group'][field]=value
    rejected(server,provider,WRITE,arguments())


@pytest.mark.parametrize('field,value',[('status','REMOVED'),('advertising_channel_type','SHOPPING'),('advertising_channel_type','PERFORMANCE_MAX'),('advertising_channel_sub_type','SEARCH_MOBILE_APP')])
def test_unsupported_campaign_state_refuses(tmp_path,field,value):
    server,provider=setup(tmp_path,[WRITE]);provider.data[h.CUSTOMER_ID]['campaign'][0]['campaign'][field]=value
    rejected(server,provider,WRITE,arguments())


def test_parental_status_is_display_only(tmp_path):
    server,provider=setup(tmp_path,[WRITE]);rejected(server,provider,WRITE,arguments([change('PARENTAL_STATUS','PARENT')]))


@pytest.mark.parametrize('resource,field,value',[
    ('campaign','campaign.id',999),('campaign','campaign.resource_name',rn('campaigns',701,h.OTHER_CUSTOMER_ID)),
    ('ad_group','ad_group.id',0),('ad_group','ad_group.resource_name',rn('adGroups',802)),
    ('ad_group','ad_group.campaign',rn('campaigns',701,h.OTHER_CUSTOMER_ID)),
    ('ad_group_criterion','ad_group_criterion.resource_name',rn('adGroupCriteria','802~601')),
    ('ad_group_criterion','ad_group_criterion.criterion_id',0),('ad_group_criterion','ad_group_criterion.ad_group',rn('adGroups',802)),
    ('ad_group_criterion','ad_group_criterion.type_','GENDER'),
    ('campaign_criterion','campaign_criterion.resource_name',rn('campaignCriteria','702~710')),
    ('campaign_criterion','campaign_criterion.campaign',rn('campaigns',701,h.OTHER_CUSTOMER_ID)),
    ('campaign_criterion','campaign_criterion.criterion_id',0),('campaign_criterion','campaign_criterion.type_','GENDER')])
def test_foreign_missing_inconsistent_identity_and_oneofs_refuse(tmp_path,resource,field,value):
    server,provider=setup(tmp_path,[WRITE])
    provider.data[h.CUSTOMER_ID]['campaign_criterion']=[demographic('AGE_RANGE','AGE_RANGE_65_UP',710,negative=True,level='campaign')]
    corrupt_field(provider,resource,field,value);rejected(server,provider,WRITE,arguments())


ENUM_FIELDS=[('campaign','campaign.status'),('campaign','campaign.advertising_channel_type'),('campaign','campaign.advertising_channel_sub_type'),
    ('ad_group','ad_group.status'),('ad_group','ad_group.type_'),
    ('ad_group_criterion','ad_group_criterion.status'),('ad_group_criterion','ad_group_criterion.type_'),
    ('campaign_criterion','campaign_criterion.status'),('campaign_criterion','campaign_criterion.type_')]
ENUM_FIELDS += [(resource,resource+'.'+dimension.lower()+'.type_') for resource in ('ad_group_criterion','campaign_criterion') for dimension in CATEGORIES]


@pytest.mark.parametrize('resource,field',ENUM_FIELDS)
def test_each_consumed_enum_rejects_unknown_numbers_without_stderr(tmp_path,resource,field,capfd):
    server,provider=setup(tmp_path,[WRITE]);dimension=next((d for d in CATEGORIES if '.'+d.lower()+'.' in field),'AGE_RANGE')
    provider.data[h.CUSTOMER_ID]['ad_group_criterion']=[demographic(dimension,CATEGORIES[dimension][0],group_id=804)]
    row=demographic(dimension,CATEGORIES[dimension][0],710,negative=True,level='campaign')
    row['campaign_criterion'].update(campaign=rn('campaigns',704),resource_name=rn('campaignCriteria','704~710'))
    provider.data[h.CUSTOMER_ID]['campaign_criterion']=[row]
    corrupt_field(provider,resource,field,123456)
    rejected(server,provider,WRITE,arguments([change('GENDER','FEMALE')],'DISPLAY'))
    captured=capfd.readouterr();assert not captured.err


def test_unknown_target_restriction_enum_refuses_without_hiding_other_warnings(tmp_path,capfd):
    server,provider=setup(tmp_path,[WRITE]);corrupt_field(provider,'ad_group','ad_group.targeting_setting',{
        'target_restrictions':[{'targeting_dimension':123456,'bid_only':False}]})
    rejected(server,provider,WRITE,arguments());assert not capfd.readouterr().err


@pytest.mark.parametrize('resource',['campaign','ad_group','ad_group_criterion','campaign_criterion'])
@pytest.mark.parametrize('fault',['duplicate','partial'])
def test_complete_unique_state_required_at_both_levels(tmp_path,resource,fault):
    server,provider=setup(tmp_path,[WRITE]);provider.data[h.CUSTOMER_ID]['campaign_criterion']=[demographic('AGE_RANGE','AGE_RANGE_65_UP',710,negative=True,level='campaign')]
    if fault=='duplicate':provider.corrupt[resource]=lambda rows:rows+deepcopy(rows[:1])
    else:provider.fail_after[resource]=1
    rejected(server,provider,WRITE,arguments())


@pytest.mark.parametrize('resource',['ad_group_criterion','campaign_criterion'])
def test_nonremoved_demographic_scan_stops_after_101_rows(tmp_path,resource):
    server,provider=setup(tmp_path,[READ],read_only=True)
    provider.data[h.CUSTOMER_ID][resource]=[demographic('AGE_RANGE','AGE_RANGE_25_34',1000+i,level='ad_group' if resource.startswith('ad_group') else 'campaign') for i in range(1000)]
    rejected(server,provider,READ,{'ad_group_id':'801'})
    assert provider.pulls[resource]<=101
    queries=[c.query for c in provider.searches if h._FROM_RE.search(c.query).group(1)==resource]
    assert queries and all(re.search(r'\bLIMIT\s+101\s*$',q,re.I) for q in queries)


def test_projected_state_bytes_are_bounded_and_parent_reads_are_singular(tmp_path):
    server,provider=setup(tmp_path,[READ],read_only=True)
    h.expect_ok(h.call(server,READ,{'ad_group_id':'801'}))
    for source in ('campaign','ad_group'):
        queries=[c.query for c in provider.searches if h._FROM_RE.search(c.query).group(1)==source]
        assert queries and all(re.search(r'\bLIMIT\s+2\s*$',q,re.I) for q in queries)
    provider.data[h.CUSTOMER_ID]['ad_group_criterion'][0]['ad_group_criterion']['final_url_suffix']='x'*(16*1024*1024+1)
    rejected(server,provider,READ,{'ad_group_id':'801'})


@pytest.mark.parametrize('resource,field,value',[
    ('campaign','name','Changed parent'),('campaign','status','PAUSED'),
    ('ad_group','optimized_targeting_enabled',True),('ad_group','targeting_setting',{'target_restrictions':[{'targeting_dimension':'AUDIENCE','bid_only':False}]}),
    ('ad_group_criterion','bid_modifier',1),('ad_group_criterion','status','PAUSED'),
    ('ad_group_criterion','negative',True)])
def test_complete_parent_and_direct_criterion_state_is_fingerprinted(tmp_path,resource,field,value):
    server,provider=setup(tmp_path,[WRITE]);plan=stage(server,WRITE,arguments())
    provider.data[h.CUSTOMER_ID][resource][0][resource][field]=value
    assert_stale(server,provider,plan)


def test_reordered_rows_and_untouched_sibling_changes_do_not_stale(tmp_path):
    server,provider=setup(tmp_path,[WRITE]);plan=stage(server,WRITE,arguments())
    for value in provider.data[h.CUSTOMER_ID].values():
        if isinstance(value,list):value.reverse()
    sibling=next(r['ad_group_criterion'] for r in provider.data[h.CUSTOMER_ID]['ad_group_criterion'] if r['ad_group_criterion']['ad_group']==rn('adGroups',802))
    sibling['status']='PAUSED'
    checked_apply(server,provider,plan,'AdGroupCriterionService','mutate_ad_group_criteria','MutateAdGroupCriteriaRequest')


@pytest.mark.parametrize('case',['preview','expiry','replay','lost_response','provider_rejection','stage_audit','pre_audit','terminal_audit','concurrent'])
def test_demographic_plan_lifecycle_and_no_write_retry(tmp_path,case):
    safety(tmp_path,WRITE,arguments(),case)


@pytest.mark.parametrize('resource,field',[*ENUM_FIELDS,('ad_group','ad_group.targeting_setting')])
def test_installed_unknown_enum_output_is_content_safe(tmp_path,monkeypatch,resource,field):
    import test_auth_cause_contract as process
    setup(tmp_path,[WRITE])
    dimension=next((d for d in CATEGORIES if '.'+d.lower()+'.' in field),'AGE_RANGE')
    # Seed genuine messages in transport only; no product validators are replaced.
    seed="\ntransport.data[h.CUSTOMER_ID]['ad_group_criterion']=["+repr(demographic(dimension,CATEGORIES[dimension][0],group_id=804))+']\n'
    row=demographic(dimension,CATEGORIES[dimension][0],710,negative=True,level='campaign');row['campaign_criterion'].update(campaign=rn('campaigns',704),resource_name=rn('campaignCriteria','704~710'))
    seed+="transport.data[h.CUSTOMER_ID]['campaign_criterion']=["+repr(row)+']\n'
    monkeypatch.setattr(process,'INJECTION',INSTALLED_INJECTION+seed)
    value={'target_restrictions':[{'targeting_dimension':123456,'bid_only':False}]} if field.endswith('targeting_setting') else 123456
    with process.InstalledServer(tmp_path/'installed') as installed:
        installed.mode(corruption=[resource,field,value])
        result=installed.call(WRITE,arguments(channel='DISPLAY'))
        assert h.error_of(result)['code']!='INTERNAL' and not installed.events('targeting mutation')
    assert not installed.stderr


def test_exact_twenty_display_changes_use_one_atomic_request_with_no_unrelated_changes(tmp_path):
    server,provider=setup(tmp_path,[WRITE]);configure(provider,'DISPLAY',[])
    changes=[change(dimension,value,'INCLUDE') for dimension,values in CATEGORIES.items() for value in values]
    assert len(changes)==20
    plan=stage(server,WRITE,arguments(changes,'DISPLAY'))
    request,before=checked_apply(server,provider,plan,'AdGroupCriterionService','mutate_ad_group_criteria','MutateAdGroupCriteriaRequest')
    assert len(request.operations)==20 and all(op._pb.WhichOneof('operation')=='create' for op in request.operations)
    untouched(before,provider.data,804)


def test_mixed_noop_and_real_change_sends_only_the_real_operation(tmp_path):
    server,provider=setup(tmp_path,[WRITE])
    changes=[change('AGE_RANGE','AGE_RANGE_25_34','INCLUDE'),change()]
    plan=stage(server,WRITE,arguments(changes))
    request,_=checked_apply(server,provider,plan,'AdGroupCriterionService','mutate_ad_group_criteria','MutateAdGroupCriteriaRequest')
    assert len(request.operations)==1 and request.operations[0].create.gender.type_.name=='FEMALE'


def test_unknown_enum_on_read_and_freshness_recheck_never_warns(tmp_path,capfd):
    server,provider=setup(tmp_path,[READ,WRITE]);plan=stage(server,WRITE,arguments())
    corrupt_field(provider,'ad_group_criterion','ad_group_criterion.age_range.type_',123456)
    rejected(server,provider,READ,{'ad_group_id':'801'})
    assert_stale(server,provider,plan)
    assert not capfd.readouterr().err


def test_all_demographic_required_reporting_leaves_are_selected(tmp_path):
    server,provider=setup(tmp_path,[WRITE]);stage(server,WRITE,arguments())
    require_projection(provider,'ad_group_criterion',CUSTOM+['criterion_id','resource_name','ad_group','type','status','negative',*[d.lower()+'.type' for d in CATEGORIES]])
    require_projection(provider,'ad_group',['targeting_setting.target_restrictions','optimized_targeting_enabled'])
