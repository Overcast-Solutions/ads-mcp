"""Atomic, provider-validated creation with complete eligibility and readback."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import re

import pytest
from google.protobuf.json_format import MessageToDict

import harness as h
from pmax_experiment_oracle import (CREATE,CREATE_ARGS,BAD_IDS,BAD_CUSTOMERS,TYPE,TEXT,EXPANSION,NOW,TODAY,
    setup,rn,experiment,arm,settings,stage,preview,apply,rejected,live_calls,break_audit,quiet)


MIDNIGHT_ROLLOVER_NOW = datetime(2026,9,16,5,59,30,tzinfo=timezone.utc).timestamp()
MIDNIGHT_ROLLOVER_SECONDS = 60


def assert_create_request(request,before):
    assert request._pb.DESCRIPTOR.full_name=='google.ads.googleads.v25.services.MutateGoogleAdsRequest'
    assert request.customer_id==h.CUSTOMER_ID and not request.partial_failure
    operations=request.mutate_operations
    assert [op._pb.WhichOneof('operation') for op in operations]==[
        'experiment_operation','experiment_arm_operation','experiment_arm_operation','campaign_operation']
    experiment_create=operations[0].experiment_operation.create
    raw=MessageToDict(experiment_create._pb,preserving_proto_field_name=True)
    assert set(raw)=={'resource_name','name','type_','start_date','end_date'}
    assert raw['type_']==TYPE and raw['name']==CREATE_ARGS['name']
    assert raw['start_date']==CREATE_ARGS['date_start'] and raw['end_date']==CREATE_ARGS['date_end']
    assert re.fullmatch(r'customers/'+h.CUSTOMER_ID+r'/experiments/-[1-9][0-9]*',raw['resource_name'])
    arms=[operations[index].experiment_arm_operation.create for index in (1,2)]
    assert {a.control for a in arms}=={True,False} and [a.traffic_split for a in arms]==[50,50]
    assert len({a.resource_name for a in arms})==2
    for a in arms:
        assert a.experiment==raw['resource_name'] and list(a.campaigns)==[rn('campaigns',703)]
        assert re.fullmatch(r'customers/'+h.CUSTOMER_ID+r'/experimentArms/-[1-9][0-9]*~-[1-9][0-9]*',a.resource_name)
        assert a.resource_name.split('/')[-1].split('~')[0]==raw['resource_name'].split('/')[-1]
        assert set(MessageToDict(a._pb,preserving_proto_field_name=True)) <= {'resource_name','experiment','name','control','traffic_split','campaigns'}
    update=operations[-1].campaign_operation
    assert list(update.update_mask.paths)==['asset_automation_settings']
    updated=MessageToDict(update.update._pb,preserving_proto_field_name=True)
    assert set(updated)=={'resource_name','asset_automation_settings'} and update.update.resource_name==rn('campaigns',703)
    expected={x['asset_automation_type']:x['asset_automation_status'] for x in before}
    expected.update({TEXT:'OPTED_IN',EXPANSION:'OPTED_IN'})
    assert {x.asset_automation_type.name:x.asset_automation_status.name for x in update.update.asset_automation_settings}==expected
    assert len(update.update.asset_automation_settings)==len(expected)


def test_provider_validate_only_is_distinct_from_preview_and_real_atomic_apply(tmp_path):
    server,client=setup(tmp_path,{CREATE});before=deepcopy(client.data)
    plan=stage(server)
    assert len(client.mutations)==1 and client.mutations[0].validate_only and not client.live_mutations()
    assert_create_request(client.mutations[0].request,before[h.CUSTOMER_ID]['campaign'][2]['campaign']['asset_automation_settings'])
    assert client.data==before and 'validat' in json.dumps(plan).lower()
    assert h.error_of(apply(server,plan))['code']=='DRY_RUN_REQUIRED'
    preview(server,plan)
    assert len(client.mutations)==1
    reads=len(client.searches)
    result=h.expect_ok(apply(server,plan,ack=False))
    assert result['submitted'] is True and result['applied'] is True
    assert result['experiment_id']=='901' and result['resource_name']==rn('experiments',901)
    assert len(client.live_mutations())==1 and len(client.searches)>reads
    assert_create_request(client.live_mutations()[0].request,before[h.CUSTOMER_ID]['campaign'][2]['campaign']['asset_automation_settings'])
    assert client.data[h.OTHER_CUSTOMER_ID]==before[h.OTHER_CUSTOMER_ID]
    assert client.data[h.CUSTOMER_ID]['untouched']==before[h.CUSTOMER_ID]['untouched']
    assert client.data[h.CUSTOMER_ID]['campaign'][:2]==before[h.CUSTOMER_ID]['campaign'][:2]
    assert result['observed']['status']=='SETUP' and result.get('running') is not True
    assert h.error_of(apply(server,plan))['code']=='PLAN_CONSUMED' and len(client.live_mutations())==1


@pytest.mark.parametrize('bad',BAD_IDS)
def test_creation_campaign_id_preserves_original_json_types(tmp_path,bad):
    server,client=setup(tmp_path,{CREATE});rejected(server,client,CREATE,{**CREATE_ARGS,'campaign_id':bad},local=True)


@pytest.mark.parametrize('bad',[*BAD_CUSTOMERS,h.OTHER_CUSTOMER_ID])
def test_creation_other_account_and_invalid_customer_refuse_before_reads(tmp_path,bad):
    server,client=setup(tmp_path,{CREATE});rejected(server,client,CREATE,{**CREATE_ARGS,'customer_id':bad},local=True)


@pytest.mark.parametrize('name',[None,True,4,[],{},'', 'null',' ',' trial','trial ','e\u0301','a\nname','a\x00name','\ud800','x'*256,'é'*128])
def test_name_is_exact_nfc_and_bounded_in_utf8_bytes(tmp_path,name):
    server,client=setup(tmp_path,{CREATE});rejected(server,client,CREATE,{**CREATE_ARGS,'name':name},local=True)


@pytest.mark.parametrize('name',['x','x'*255,'é'*127+'x'])
def test_valid_name_neighbors_are_not_trimmed_or_ascii_only(tmp_path,name):
    server,client=setup(tmp_path,{CREATE});stage(server,args={**CREATE_ARGS,'name':name})
    assert client.mutations[0].request.mutate_operations[0].experiment_operation.create.name==name


@pytest.mark.parametrize('changes',[
 {'date_start':None},{'date_start':True},{'date_start':20260915},{'date_start':'2026-9-15'},
 {'date_start':'2026-02-30'},{'date_start':'2026-09-15 '},{'date_start':'\ud800'},
 {'date_end':'2026-09-14'},{'date_start':'2026-09-14'},{'date_start':'2027-09-16','date_end':'2027-09-17'},
 {'date_start':'2026-09-15','date_end':'2027-09-16'}, {'unexpected':True},
])
def test_creation_date_and_argument_boundaries_refuse_without_validation_rpc(tmp_path,changes):
    server,client=setup(tmp_path,{CREATE});rejected(server,client,CREATE,{**CREATE_ARGS,**changes})
    assert not client.mutations


@pytest.mark.parametrize('start,end',[(TODAY,TODAY),('2027-09-15','2028-09-14'),(TODAY,'2027-09-15')])
def test_creation_allows_local_today_365_day_start_and_366_day_duration_neighbors(tmp_path,start,end):
    server,client=setup(tmp_path,{CREATE});stage(server,args={**CREATE_ARGS,'date_start':start,'date_end':end})
    assert len(client.mutations)==1 and client.mutations[0].validate_only


@pytest.mark.parametrize('fault',['paused','search','missing_expansion','unknown_expansion','duplicate_setting','unknown_setting',
    'campaign_starts_later','campaign_ends_earlier','invalid_campaign_date','missing_currency','invalid_zone'])
def test_creation_eligibility_uses_verified_actual_settings_dates_and_account(tmp_path,fault):
    server,client=setup(tmp_path,{CREATE});data=client.data[h.CUSTOMER_ID];campaign=data['campaign'][2]['campaign']
    if fault=='paused':campaign['status']='PAUSED'
    elif fault=='search':campaign['advertising_channel_type']='SEARCH'
    elif fault=='missing_expansion':campaign['asset_automation_settings']=[s for s in settings() if s['asset_automation_type']!=EXPANSION]
    elif fault=='unknown_expansion':campaign['asset_automation_settings'][1]['asset_automation_status']='UNSPECIFIED'
    elif fault=='duplicate_setting':campaign['asset_automation_settings'].append(deepcopy(settings()[0]))
    elif fault=='unknown_setting':campaign['asset_automation_settings'][2]['asset_automation_type']=999
    elif fault=='campaign_starts_later':campaign['start_date_time']='2026-09-16 00:00:00'
    elif fault=='campaign_ends_earlier':campaign['end_date_time']='2026-10-14 23:59:59'
    elif fault=='invalid_campaign_date':campaign['end_date_time']='bad-date'
    elif fault=='missing_currency':data['customer'][0]['customer']['currency_code']=''
    elif fault=='invalid_zone':data['customer'][0]['customer']['time_zone']='invalid-zone'
    rejected(server,client,CREATE,CREATE_ARGS);assert not client.mutations


@pytest.mark.parametrize('kind',['same_campaign','name_casefold','name_nfc','ended','unknown_type','dangling_arm','duplicate_experiment','duplicate_arm','foreign_arm'])
def test_collision_inventory_includes_other_types_and_ended_nonremoved_experiments(tmp_path,kind):
    server,client=setup(tmp_path,{CREATE});data=client.data[h.CUSTOMER_ID]
    if kind=='same_campaign':data['experiment_arm'][0]['experiment_arm']['campaigns']=[rn('campaigns',703)]
    elif kind in ('name_casefold','name_nfc'):
        data['experiment'][0]['experiment']['name']=CREATE_ARGS['name'].upper() if kind=='name_casefold' else 'CAFE\u0301'
    elif kind=='ended':
        data['experiment'][0]['experiment'].update(status='HALTED',end_date='2026-01-01')
        data['experiment_arm'][0]['experiment_arm']['campaigns']=[rn('campaigns',703)]
    elif kind=='unknown_type':
        data['experiment'][0]['experiment']['type_']=999
        data['experiment_arm'][0]['experiment_arm']['campaigns']=[rn('campaigns',703)]
    elif kind=='dangling_arm':data['experiment_arm'][0]['experiment_arm']['experiment']=rn('experiments',999)
    elif kind=='duplicate_experiment':data['experiment'].append(deepcopy(data['experiment'][0]))
    elif kind=='duplicate_arm':data['experiment_arm'].append(deepcopy(data['experiment_arm'][0]))
    elif kind=='foreign_arm':data['experiment_arm'][0]['experiment_arm']['resource_name']=rn('experimentArms','301~1',h.OTHER_CUSTOMER_ID)
    args={**CREATE_ARGS,'name':'café'} if kind=='name_nfc' else CREATE_ARGS
    rejected(server,client,CREATE,args);assert not client.mutations


def test_removed_experiment_does_not_block_reuse_and_inventory_order_is_semantic(tmp_path):
    server,client=setup(tmp_path,{CREATE});data=client.data[h.CUSTOMER_ID]
    data['experiment'][0]['experiment'].update(status='REMOVED',name=CREATE_ARGS['name'])
    data['experiment_arm'][0]['experiment_arm']['campaigns']=[rn('campaigns',703)]
    plan=stage(server);preview(server,plan)
    for key in ('experiment','experiment_arm','campaign'):data[key].reverse()
    next(c for c in data['campaign'] if c['campaign']['id']==703)['campaign']['asset_automation_settings'].reverse()
    assert h.expect_ok(apply(server,plan))['applied'] is True


@pytest.mark.parametrize('resource,count',[('experiment',1000),('experiment',1001),('experiment_arm',2000),('experiment_arm',2001)])
def test_collision_inventory_caps_use_bounded_complete_reads(tmp_path,resource,count):
    server,client=setup(tmp_path,{CREATE});data=client.data[h.CUSTOMER_ID]
    total=count if resource=='experiment' else 1000
    data['experiment']=[experiment(1000+i,status='REMOVED') for i in range(total)]
    data['experiment_arm']=[arm(1000+i//2,1+i%2,701) for i in range(count if resource=='experiment_arm' else 0)]
    if count in (1000,2000):stage(server)
    else:rejected(server,client,CREATE,CREATE_ARGS);assert not client.mutations
    assert client.pulls[resource] <= (1001 if resource=='experiment' else 2001)


@pytest.mark.parametrize('drift',['name','status','type','date','timestamp_precision','arm','automation','new_collision','today','incomplete'])
def test_recheck_refuses_changed_or_unverifiable_eligibility_as_stale(tmp_path,drift):
    clock=h.FakeClock(MIDNIGHT_ROLLOVER_NOW if drift=='today' else NOW);server,client=setup(tmp_path,{CREATE},clock=clock)
    plan=stage(server);preview(server,plan);data=client.data[h.CUSTOMER_ID]
    if drift=='name':data['experiment'][0]['experiment']['name']='Changed name'
    elif drift=='status':data['experiment'][0]['experiment']['status']='REMOVED'
    elif drift=='type':data['experiment'][0]['experiment']['type_']='SEARCH_CUSTOM'
    elif drift=='date':data['experiment'][0]['experiment']['end_date']='2026-11-01'
    elif drift=='timestamp_precision':data['campaign'][2]['campaign']['end_date_time']='2037-12-31 23:59:58'
    elif drift=='arm':data['experiment_arm'][0]['experiment_arm']['name']='Changed arm'
    elif drift=='automation':data['campaign'][2]['campaign']['asset_automation_settings'][2]['asset_automation_status']='OPTED_IN'
    elif drift=='new_collision':data['experiment_arm'][0]['experiment_arm']['campaigns']=[rn('campaigns',703)]
    elif drift=='today':
        clock.advance(MIDNIGHT_ROLLOVER_SECONDS)
        assert clock()<h.parse_iso_utc(plan['expires_at']).timestamp()
    else:client.fail_after['experiment']=1
    result=apply(server,plan);assert h.error_of(result)['code']=='STALE_PLAN' and not client.live_mutations()


def test_validation_refusal_cannot_stage_a_plan_or_emit_provider_detail(tmp_path):
    server,client=setup(tmp_path,{CREATE});client.reject_validation=True
    result=h.call(server,CREATE,CREATE_ARGS);quiet(result)
    assert h.error_of(result)['code']!='INTERNAL' and 'plan' not in result
    assert len(client.mutations)==1 and client.mutations[0].validate_only


@pytest.mark.parametrize('fault',['missing_experiment','renamed','wrong_dates','wrong_arm','lost_setting','read_error'])
def test_accepted_create_receipt_survives_readback_failure_without_false_application(tmp_path,fault):
    server,client=setup(tmp_path,{CREATE});plan=stage(server);preview(server,plan)
    def after():
        data=client.data[h.CUSTOMER_ID]
        created=data['experiment'][-1]['experiment']
        if fault=='missing_experiment':data['experiment'].pop()
        elif fault=='renamed':created['name']='Provider normalized name'
        elif fault=='wrong_dates':created['end_date']='2026-10-16'
        elif fault=='wrong_arm':data['experiment_arm'][-1]['experiment_arm']['traffic_split']=49
        elif fault=='lost_setting':data['campaign'][2]['campaign']['asset_automation_settings']=settings(True)[:2]
        else:client.fail_after['experiment']=0
    client.after_action=after
    result=h.expect_ok(apply(server,plan));quiet(result)
    assert result['submitted'] is True and result['applied'] is False and result['experiment_id']=='901'
    assert result['resource_name']==rn('experiments',901) and result['observation_error'] and result['verification'] in ('unknown','failed')
    assert len(client.live_mutations())==1
    assert h.error_of(apply(server,plan))['code']=='PLAN_CONSUMED'


def test_readback_transient_failure_retries_read_without_repeating_creation(tmp_path):
    from google.api_core.exceptions import ServiceUnavailable
    server,client=setup(tmp_path,{CREATE});plan=stage(server);preview(server,plan)
    client.after_action=lambda:client.stub_error(ServiceUnavailable('synthetic transient'),times=1)
    result=h.expect_ok(apply(server,plan));assert result['submitted'] is result['applied'] is True
    assert len(client.live_mutations())==1


@pytest.mark.parametrize('fault',['missing_result','extra_result','wrong_kind','foreign','temporary','duplicate_arms','wrong_arm_parent','lost'])
def test_malformed_or_lost_create_receipt_is_uncertain_and_consumed(tmp_path,fault):
    server,client=setup(tmp_path,{CREATE});plan=stage(server);preview(server,plan)
    def malformed(response):
        items=response.mutate_operation_responses
        if fault=='missing_result':del items[-1]
        elif fault=='extra_result':items.append(deepcopy(items[0]))
        elif fault=='wrong_kind':items[0].campaign_result.resource_name=rn('campaigns',901)
        elif fault=='foreign':items[0].experiment_result.resource_name=rn('experiments',901,h.OTHER_CUSTOMER_ID)
        elif fault=='temporary':items[0].experiment_result.resource_name=rn('experiments',-1)
        elif fault=='duplicate_arms':items[2].experiment_arm_result.resource_name=items[1].experiment_arm_result.resource_name
        elif fault=='wrong_arm_parent':items[1].experiment_arm_result.resource_name=rn('experimentArms','902~1')
        return response
    client.receipt_fault=malformed;client.lose_response=fault=='lost'
    result=apply(server,plan);quiet(result)
    assert result.get('applied') is not True
    assert result.get('submitted') is True or 'possible' in json.dumps(result).lower() or 'unknown' in json.dumps(result).lower()
    assert len(client.live_mutations())==1 and h.error_of(apply(server,plan))['code']=='PLAN_CONSUMED'


@pytest.mark.parametrize('moment',['stage','pre_apply','post_acceptance'])
def test_audit_failures_never_erase_or_fabricate_provider_acceptance(tmp_path,moment):
    server,client=setup(tmp_path,{CREATE})
    if moment=='stage':
        break_audit(h.audit_file(tmp_path));result=h.call(server,CREATE,CREATE_ARGS)
        assert h.error_of(result)['code']=='AUDIT_WRITE_FAILED' and 'plan' not in result and not client.live_mutations();return
    plan=stage(server);preview(server,plan)
    if moment=='pre_apply':break_audit(h.audit_file(tmp_path))
    else:client.after_action=lambda:break_audit(h.audit_file(tmp_path))
    result=apply(server,plan)
    if moment=='pre_apply':assert h.error_of(result)['code']=='AUDIT_WRITE_FAILED' and not client.live_mutations()
    else:
        assert result['submitted'] is True and result['experiment_id']=='901' and result['audit_warning']
        assert len(client.live_mutations())==1


def test_expiry_and_concurrent_distinct_plans_preserve_single_account_serialization(tmp_path):
    clock=h.FakeClock(NOW);server,client=setup(tmp_path,{CREATE},clock=clock,env={'ADS_MCP_PLAN_TTL_SECONDS':'30'})
    old=stage(server);clock.advance(31)
    assert h.error_of(apply(server,old))['code']=='PLAN_EXPIRED' and not client.live_mutations()
    plans=[stage(server,args={**CREATE_ARGS,'name':f'Concurrent trial {i}'}) for i in (1,2)]
    for plan in plans:preview(server,plan)
    with ThreadPoolExecutor(max_workers=2) as pool:results=list(pool.map(lambda p:apply(server,p),plans))
    assert sum(r.get('applied') is True for r in results)==1
    assert sum(r.get('error',{}).get('code')=='STALE_PLAN' for r in results)==1
    assert len(client.live_mutations())==1
