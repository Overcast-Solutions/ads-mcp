"""Experiment lifecycle acceptance distinguishes submission, completion and effect."""
from copy import deepcopy
import json
import warnings

import pytest

import harness as h
from pmax_experiment_oracle import (END,PROMOTE,OPERATION_READ,ARGS,HANDLE,NOW,TODAY,PRIVATE,FIXTURES,
    setup,rn,stage,preview,apply,rejected,live_calls,raw_operation,complete_promotion,break_audit,quiet,settings)


@pytest.mark.parametrize('tool',[END,PROMOTE])
def test_dedicated_validate_only_then_one_action_with_irreversible_ack(tmp_path,tool):
    server,client=setup(tmp_path,{tool});before=deepcopy(client.data);plan=stage(server,tool)
    assert len(client.actions)==1 and client.actions[0]['request'].validate_only
    assert not client.mutations and client.data==before
    request=client.actions[0]['request']
    assert request._pb.DESCRIPTOR.name==('EndExperimentRequest' if tool==END else 'PromoteExperimentRequest')
    assert (request.experiment if tool==END else request.resource_name)==rn('experiments',301)
    assert h.error_of(apply(server,plan))['code']=='DRY_RUN_REQUIRED'
    preview(server,plan)
    assert h.error_of(apply(server,plan,ack=False))['code']=='IRREVERSIBLE_CONFIRMATION_REQUIRED'
    count=len(client.searches);result=h.expect_ok(apply(server,plan));quiet(result)
    assert result['submitted'] is True and len(live_calls(client))==1 and len(client.searches)>count
    assert client.data[h.OTHER_CUSTOMER_ID]==before[h.OTHER_CUSTOMER_ID]
    assert client.data[h.CUSTOMER_ID]['campaign']==before[h.CUSTOMER_ID]['campaign']
    assert client.data[h.CUSTOMER_ID]['experiment_arm']==before[h.CUSTOMER_ID]['experiment_arm']
    assert client.data[h.CUSTOMER_ID]['untouched']==before[h.CUSTOMER_ID]['untouched']
    if tool==PROMOTE:
        assert result['applied'] is False and result['state']=='pending' and result['operation_name']==HANDLE
        events=h.read_audit_records(tmp_path)
        assert any(e['event']=='submitted' and e.get('plan_id')==plan['id'] for e in events)
        assert not any(e['event']=='applied' and e.get('plan_id')==plan['id'] for e in events)
    else:
        assert result['observed']['status']=='ENABLED' and result['observed']['end_date']==TODAY
    assert h.error_of(apply(server,plan))['code']=='PLAN_CONSUMED' and len(live_calls(client))==1


@pytest.mark.parametrize('tool',[END,PROMOTE])
@pytest.mark.parametrize('status',['UNSPECIFIED','UNKNOWN','IN_PROGRESS','COMPLETED','FAILED','COMPLETED_WITH_WARNING',999])
def test_every_promotion_state_except_not_started_refuses_new_action(tmp_path,tool,status):
    server,client=setup(tmp_path,{tool});client.data[h.CUSTOMER_ID]['experiment'][0]['experiment']['promote_status']=status
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter('always');rejected(server,client,tool,ARGS[tool])
    assert not caught and not client.actions


@pytest.mark.parametrize('tool',[END,PROMOTE])
@pytest.mark.parametrize('fault',['not_started','ended','not_enabled','unknown_status','wrong_type','bad_arm','foreign','extra','bad_id'])
def test_lifecycle_refuses_ineligible_dates_states_and_original_inputs(tmp_path,tool,fault):
    server,client=setup(tmp_path,{tool});data=client.data[h.CUSTOMER_ID];args=deepcopy(ARGS[tool])
    if fault=='not_started':data['experiment'][0]['experiment']['start_date']='2026-09-16'
    elif fault=='ended':data['experiment'][0]['experiment']['end_date']='2026-09-14'
    elif fault=='not_enabled':data['experiment'][0]['experiment']['status']='HALTED'
    elif fault=='unknown_status':data['experiment'][0]['experiment']['status']=999
    elif fault=='wrong_type':data['experiment'][0]['experiment']['type_']='SEARCH_CUSTOM'
    elif fault=='bad_arm':data['experiment_arm'][0]['experiment_arm']['traffic_split']=75
    elif fault=='foreign':args['customer_id']=h.OTHER_CUSTOMER_ID
    elif fault=='extra':args['unrequested']=True
    else:args['experiment_id']=301
    rejected(server,client,tool,args,local=fault in ('foreign','extra','bad_id'));assert not client.actions


@pytest.mark.parametrize('tool',[END,PROMOTE])
@pytest.mark.parametrize('fault',['end_date','arm','settings','promote_status','time','read_failure'])
def test_lifecycle_recheck_detects_state_and_local_time_drift(tmp_path,tool,fault):
    clock=h.FakeClock(NOW);server,client=setup(tmp_path,{tool},clock=clock)
    plan=stage(server,tool);preview(server,plan);data=client.data[h.CUSTOMER_ID]
    if fault=='end_date':data['experiment'][0]['experiment']['end_date']='2026-10-30'
    elif fault=='arm':data['experiment_arm'][0]['experiment_arm']['name']='Changed'
    elif fault=='settings':data['campaign'][0]['campaign']['asset_automation_settings'][2]['asset_automation_status']='OPTED_IN'
    elif fault=='promote_status':data['experiment'][0]['experiment']['promote_status']='IN_PROGRESS'
    elif fault=='time':
        # Date changes while the plan is still valid: stage 15 seconds before
        # local midnight, with an experiment ending today.
        data['experiment'][0]['experiment']['end_date']=TODAY
        clock.now=NOW+5*3600+29*60+45
        plan=stage(server,tool);preview(server,plan);clock.advance(30)
    else:client.fail_after['experiment']=0
    assert h.error_of(apply(server,plan))['code']=='STALE_PLAN' and not live_calls(client)


@pytest.mark.parametrize('tool',[END,PROMOTE])
def test_provider_validation_failure_stages_nothing_and_never_applies(tmp_path,tool):
    server,client=setup(tmp_path,{tool});client.reject_validation=True
    result=h.call(server,tool,ARGS[tool]);quiet(result)
    assert h.error_of(result)['code']!='INTERNAL' and 'plan' not in result
    assert len(client.actions)==1 and not live_calls(client)


@pytest.mark.parametrize('terminal',['success','warning','error'])
def test_immediate_done_sdk_future_is_classified_without_polling_or_waiting(tmp_path,terminal):
    server,client=setup(tmp_path,{PROMOTE});plan=stage(server,PROMOTE);preview(server,plan)
    client.operation=raw_operation(done=True,result='error' if terminal=='error' else 'response')
    if terminal!='error':client.after_action=lambda:complete_promotion(client,status='COMPLETED_WITH_WARNING' if terminal=='warning' else 'COMPLETED')
    result=h.expect_ok(apply(server,plan));quiet(result)
    assert result['submitted'] is True and result['completed'] is True
    assert result['applied'] is (terminal!='error') and result['state']==('failed' if terminal=='error' else 'completed')
    if terminal=='warning':assert result['warnings'] is True
    assert not client.polls and len(live_calls(client))==1


@pytest.mark.parametrize('fault',['empty_name','wrong_metadata','foreign_metadata','missing_metadata','done_without_result','pending_with_result'])
def test_accepted_malformed_promotion_retains_unknown_submission_and_consumes_plan(tmp_path,fault):
    server,client=setup(tmp_path,{PROMOTE});plan=stage(server,PROMOTE);preview(server,plan)
    kwargs={'name':''} if fault=='empty_name' else {'metadata':'wrong_type'} if fault=='wrong_metadata' else (
        {'customer':h.OTHER_CUSTOMER_ID} if fault=='foreign_metadata' else {'metadata':'absent'} if fault=='missing_metadata' else
        {'done':True} if fault=='done_without_result' else {'done':False,'result':'response'})
    client.promote_response=raw_operation(**kwargs)
    result=apply(server,plan);quiet(result)
    assert result.get('submitted') is True and result.get('applied') is False
    assert result.get('state') in ('unknown','unverified') and result.get('observation_error')
    assert len(live_calls(client))==1 and h.error_of(apply(server,plan))['code']=='PLAN_CONSUMED'


@pytest.mark.parametrize('tool,fault',[(END,'lost'),(PROMOTE,'lost'),(END,'readback'),
    (END,'pre_audit'),(PROMOTE,'pre_audit'),(END,'post_audit'),(PROMOTE,'post_audit')])
def test_lifecycle_failure_keeps_accepted_context_and_never_repeats_rpc(tmp_path,tool,fault):
    server,client=setup(tmp_path,{tool});plan=stage(server,tool);preview(server,plan)
    if fault=='lost':client.lose_response=True
    elif fault=='readback':client.after_action=lambda:client.fail_after.update(experiment=0)
    elif fault=='pre_audit':break_audit(h.audit_file(tmp_path))
    else:client.after_action=lambda:break_audit(h.audit_file(tmp_path))
    result=apply(server,plan);quiet(result)
    if fault=='pre_audit':assert h.error_of(result)['code']=='AUDIT_WRITE_FAILED' and not live_calls(client);return
    assert len(live_calls(client))==1 and result.get('applied') is not True
    if fault!='lost':
        assert result['submitted'] is True
        if fault=='readback':assert result.get('observation_error')
        else:assert result.get('audit_warning')
    assert h.error_of(apply(server,plan))['code']=='PLAN_CONSUMED' and len(live_calls(client))==1


def test_fourth_independent_read_golden_is_pending_and_restart_safe(tmp_path):
    server,client=setup(tmp_path,{OPERATION_READ},read_only=True)
    expected=json.loads((FIXTURES/(OPERATION_READ+'.json')).read_text())
    result=h.expect_ok(h.call(server,OPERATION_READ,expected['args']))
    assert result==expected['golden'] and len(client.polls)==1
    assert client.polls[0]['login_customer_id']==h.LOGIN_CUSTOMER_ID
    assert not live_calls(client)


@pytest.mark.parametrize('customer',[h.CUSTOMER_ID,h.OTHER_CUSTOMER_ID])
def test_polling_uses_selected_account_metadata_binding_and_original_authenticated_transport(tmp_path,customer):
    server,client=setup(tmp_path,{OPERATION_READ},read_only=True)
    client.operation=raw_operation(customer=customer)
    result=h.expect_ok(h.call(server,OPERATION_READ,{**ARGS[OPERATION_READ],'customer_id':customer}))
    assert result['customer_id']==customer and result['state']=='pending'
    assert all(call.customer_id==customer for call in client.searches)
    assert client.polls[0]['name']==HANDLE and client.polls[0]['retry'] is None
    assert not client.actions and not client.mutations


@pytest.mark.parametrize('value',[None,True,10,[],{},'', 'null','a b','a\nb','\ud800','x'*2049])
def test_operation_handle_raw_input_has_bounded_opaque_spelling(tmp_path,value):
    server,client=setup(tmp_path,{OPERATION_READ});rejected(server,client,OPERATION_READ,{**ARGS[OPERATION_READ],'operation_name':value},local=True)


@pytest.mark.parametrize('handle',['operations/a','region-X/service:ads/operations/id~9','https://example.invalid/operations/opaque','x'*2048])
def test_opaque_handle_is_not_restricted_to_an_invented_customer_grammar(tmp_path,handle):
    server,client=setup(tmp_path,{OPERATION_READ})
    client.data[h.CUSTOMER_ID]['experiment'][0]['experiment']['long_running_operation']=handle
    client.operation=raw_operation(name=handle)
    result=h.expect_ok(h.call(server,OPERATION_READ,{**ARGS[OPERATION_READ],'operation_name':handle}))
    assert result['operation_name']==handle and client.polls[0]['name']==handle


@pytest.mark.parametrize('fault',['latest_mismatch','no_latest','wrong_name','wrong_type','malformed_metadata','absent_metadata','foreign_metadata','sibling_metadata'])
def test_poll_refuses_unbound_stale_and_foreign_metadata_without_payload_leak(tmp_path,fault):
    server,client=setup(tmp_path,{OPERATION_READ})
    if fault in ('latest_mismatch','no_latest'):client.data[h.CUSTOMER_ID]['experiment'][0]['experiment']['long_running_operation']='different' if fault=='latest_mismatch' else ''
    elif fault=='wrong_name':client.operation=raw_operation(name='different')
    elif fault=='wrong_type':client.operation=raw_operation(metadata='wrong_type')
    elif fault=='malformed_metadata':client.operation=raw_operation(metadata='malformed')
    elif fault=='absent_metadata':client.operation=raw_operation(metadata='absent')
    elif fault=='foreign_metadata':client.operation=raw_operation(customer=h.OTHER_CUSTOMER_ID)
    else:client.operation=raw_operation(experiment_id=302)
    result=h.call(server,OPERATION_READ,ARGS[OPERATION_READ]);quiet(result)
    assert result.get('applied') is not True and result.get('completed') is not True
    assert result.get('error') or result.get('observation_error')
    if fault in ('latest_mismatch','no_latest'):assert not client.polls


@pytest.mark.parametrize('status',['UNSPECIFIED','UNKNOWN','NOT_STARTED','IN_PROGRESS','COMPLETED','FAILED','COMPLETED_WITH_WARNING'])
def test_completed_empty_operation_and_observed_promotion_state_are_independent(tmp_path,status):
    server,client=setup(tmp_path,{OPERATION_READ});complete_promotion(client,status=status)
    result=h.expect_ok(h.call(server,OPERATION_READ,ARGS[OPERATION_READ]))
    assert result['completed'] is True and result['state']=='completed' and result['promote_status']==status
    assert result['applied'] is (status in ('COMPLETED','COMPLETED_WITH_WARNING'))
    assert result['warnings'] is (status=='COMPLETED_WITH_WARNING')


@pytest.mark.parametrize('fault',['missing_result','wrong_result','pending_result','unknown_code','unknown_promote','settings_out','postread_error','new_latest'])
def test_invalid_or_unverified_terminal_observation_never_claims_application(tmp_path,fault):
    server,client=setup(tmp_path,{OPERATION_READ});complete_promotion(client)
    if fault=='missing_result':client.operation=raw_operation(done=True)
    elif fault=='wrong_result':client.operation=raw_operation(done=True,result='wrong_response')
    elif fault=='pending_result':client.operation=raw_operation(done=False,result='response')
    elif fault=='unknown_code':client.operation=raw_operation(done=True,result='error',code=999)
    elif fault=='unknown_promote':client.data[h.CUSTOMER_ID]['experiment'][0]['experiment']['promote_status']=999
    elif fault=='settings_out':client.data[h.CUSTOMER_ID]['campaign'][0]['campaign']['asset_automation_settings']=settings(False)
    else:
        original=client.experiment_service.get_operation
        def poll(*args,**kwargs):
            result=original(*args,**kwargs)
            if fault=='postread_error':client.fail_after['experiment']=0
            else:client.data[h.CUSTOMER_ID]['experiment'][0]['experiment']['long_running_operation']='next/operations/new'
            return result
        client.experiment_service.get_operation=poll
    result=h.call(server,OPERATION_READ,ARGS[OPERATION_READ]);quiet(result)
    assert result.get('applied') is not True
    if fault in ('settings_out','postread_error','new_latest'):assert result['completed'] is True
    else:assert result.get('error') or result.get('observation_error')


@pytest.mark.parametrize('retryable',[True,False])
def test_poll_transport_errors_are_bounded_unknown_observations_not_experiment_failure(tmp_path,retryable):
    server,client=setup(tmp_path,{OPERATION_READ});client.poll_failures=1 if retryable else 100
    result=h.call(server,OPERATION_READ,ARGS[OPERATION_READ]);quiet(result)
    assert 1<len(client.polls)<=5 and not live_calls(client)
    if retryable:assert h.expect_ok(result)['state']=='pending'
    else:assert result.get('error') or result.get('observation_error');assert result.get('state')!='failed'


@pytest.mark.parametrize('fault',['empty','transport','loop','pages','statuses','bytes','invalid_code','details'])
def test_failed_operation_preserves_failure_with_bounded_sanitized_async_details(tmp_path,fault):
    server,client=setup(tmp_path,{OPERATION_READ});client.operation=raw_operation(done=True,result='error')
    if fault=='empty':client.error_pages=[{'errors':[]}]
    elif fault=='transport':client.detail_failure=True
    elif fault=='loop':client.error_pages=[{'errors':[{'code':13,'message':PRIVATE}],'next_page_token':'1'}]*2
    elif fault=='pages':client.error_pages=[{'errors':[{'code':13,'message':PRIVATE}],'next_page_token':str(i+1)} for i in range(11)]
    elif fault=='statuses':client.error_pages=[{'errors':[{'code':13,'message':PRIVATE}]*1001}]
    elif fault=='bytes':client.error_pages=[{'errors':[{'code':13,'message':PRIVATE*(1024*1024)}]}]
    elif fault=='invalid_code':client.error_pages=[{'errors':[{'code':999,'message':PRIVATE}]}]
    else:client.error_pages=[{'errors':[{'code':7,'message':PRIVATE,'details':[{'@type':'type.googleapis.com/google.protobuf.StringValue','value':PRIVATE}]}]}]
    result=h.expect_ok(h.call(server,OPERATION_READ,ARGS[OPERATION_READ]));quiet(result)
    assert result['state']=='failed' and result['completed'] is True and result['applied'] is False
    details=result['async_errors'];assert details['scope']=='experiment/latest-operation'
    assert details['complete'] is (fault in ('empty','details'))
    if not details['complete']:assert details['reason']
    assert len(client.error_calls)<=10 and len(details['errors'])<=1000
    assert all(call.resource_name==rn('experiments',301) and call.page_size==100 for call in client.error_calls)
    for error in details['errors']:
        assert set(error)<= {'code','name'} and isinstance(error['code'],int) and error['name'].isupper()


def test_async_error_page_boundary_accepts_1000_statuses_without_unbounded_pager_iteration(tmp_path):
    server,client=setup(tmp_path,{OPERATION_READ});client.operation=raw_operation(done=True,result='error',code=7)
    client.error_pages=[{'errors':[{'code':7,'message':PRIVATE}]*100,'next_page_token':str(i+1) if i<9 else ''} for i in range(10)]
    result=h.expect_ok(h.call(server,OPERATION_READ,ARGS[OPERATION_READ]))
    assert result['state']=='failed' and result['async_errors']['complete'] is True
    assert len(result['async_errors']['errors'])==1000 and len(client.error_calls)==10


def test_present_empty_error_oneof_is_terminal_failure_even_without_error_details(tmp_path):
    from google.rpc import status_pb2
    server,client=setup(tmp_path,{OPERATION_READ})
    client.operation=raw_operation(done=True)
    client.operation.error.CopyFrom(status_pb2.Status())
    client.error_pages=[{'errors':[]}]
    result=h.expect_ok(h.call(server,OPERATION_READ,ARGS[OPERATION_READ]))
    assert result['completed'] is True and result['state']=='failed' and result['applied'] is False
