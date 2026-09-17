"""Experiment inspection and direct arm reporting through the actual MCP API."""
from copy import deepcopy
import json
import warnings

import pytest

import harness as h
from pmax_experiment_oracle import (INSPECTION_READS,ARGS,RESULT_ARGS,BAD_IDS,BAD_CUSTOMERS,
    FIXTURES,TYPE,METRIC_NAMES,PRIVATE,setup,rn,experiment,arm,campaign,metric_row,rejected,quiet,assert_queries)


@pytest.mark.parametrize('read_only',[True,False])
def test_three_read_schemas_require_exact_arguments_in_both_modes(tmp_path,read_only):
    from pmax_experiment_oracle import SIGNATURES
    server,client=setup(tmp_path,INSPECTION_READS,read_only=read_only)
    for name in INSPECTION_READS:
        schema=h.tool_map(server)[name].input_schema
        parameters,required=SIGNATURES[name]
        assert set(schema['properties'])==set(parameters) and set(schema.get('required',[]))==set(required)
        records=[tool for capability in json.loads((FIXTURES.parent/'capability_requirements.json').read_text())['capabilities'] for tool in capability['tools']]
        assert next(record for record in records if record['name']==name)=={'name':name,'parameters':parameters,'required':required,'values':{}}
    assert not client.searches and not client.mutations


@pytest.mark.parametrize('tool',sorted(INSPECTION_READS))
def test_independent_read_golden_is_exact_with_genuine_projected_sdk_rows(tmp_path,tool):
    server,client=setup(tmp_path,{tool})
    fixture=json.loads((FIXTURES/(tool+'.json')).read_text())
    data=client.data[h.CUSTOMER_ID]
    for key in ('customer','campaign','experiment','experiment_arm'):data[key]=deepcopy(fixture['gaql'][key])
    data['report']=deepcopy(fixture['gaql']['experiment'])
    actual=h.expect_ok(h.call(server,tool,fixture['args']))
    assert actual==fixture['golden']
    assert_queries(client.searches)
    assert not client.mutations


@pytest.mark.parametrize('tool',sorted(INSPECTION_READS))
@pytest.mark.parametrize('customer',[h.CUSTOMER_ID,h.OTHER_CUSTOMER_ID])
def test_all_read_surfaces_isolate_selected_account_and_sibling_experiment(tmp_path,tool,customer):
    server,client=setup(tmp_path,{tool})
    actual=h.expect_ok(h.call(server,tool,{**ARGS[tool],'customer_id':customer}))
    assert actual['customer_id']==customer and actual['currency']==('EUR' if customer==h.OTHER_CUSTOMER_ID else 'USD')
    text=json.dumps(actual)
    other=h.OTHER_CUSTOMER_ID if customer==h.CUSTOMER_ID else h.CUSTOMER_ID
    assert other not in text
    if tool!='list_pmax_url_experiments':assert rn('experiments',302,customer) not in text
    else:assert [e['experiment']['experiment_id'] for e in actual['experiments']]==['301','302']
    assert client.searches and all(c.customer_id==customer for c in client.searches)


@pytest.mark.parametrize('bad',BAD_IDS)
@pytest.mark.parametrize('tool',['get_pmax_url_experiment','get_pmax_url_experiment_results'])
def test_original_json_experiment_ids_refuse_before_reads(tmp_path,tool,bad):
    server,client=setup(tmp_path,{tool});rejected(server,client,tool,{**ARGS[tool],'experiment_id':bad},local=True)


@pytest.mark.parametrize('bad',BAD_CUSTOMERS)
def test_customer_raw_types_and_malformed_spellings_do_not_reach_provider(tmp_path,bad):
    tool='list_pmax_url_experiments';server,client=setup(tmp_path,{tool})
    rejected(server,client,tool,{'customer_id':bad},local=True)


@pytest.mark.parametrize('tool',sorted(INSPECTION_READS))
def test_extra_arguments_are_not_silently_dropped(tmp_path,tool):
    server,client=setup(tmp_path,{tool});rejected(server,client,tool,{**ARGS[tool],'unrequested':PRIVATE},local=True)


@pytest.mark.parametrize('dates',[
 {'date_start':None},{'date_start':True},{'date_start':20260901},{'date_start':'null'},
 {'date_start':'2026-9-01'},{'date_start':'2026-02-29'},{'date_start':'2026-09-01T00:00:00Z'},
 {'date_start':'2026-09-01 '},{'date_start':'\ud800'},{'date_end':'2026-08-31'},
 {'date_start':'2025-01-01','date_end':'2026-01-02'},
])
def test_report_dates_are_explicit_strict_inclusive_and_bounded_before_reads(tmp_path,dates):
    tool='get_pmax_url_experiment_results';server,client=setup(tmp_path,{tool})
    rejected(server,client,tool,{**RESULT_ARGS,**dates},local=True)


@pytest.mark.parametrize('start,end',[('2026-09-01','2026-09-01'),('2024-01-01','2024-12-31')])
def test_report_valid_one_day_and_366_day_neighbors_reach_exact_query(tmp_path,start,end):
    tool='get_pmax_url_experiment_results';server,client=setup(tmp_path,{tool})
    result=h.expect_ok(h.call(server,tool,{**RESULT_ARGS,'date_start':start,'date_end':end}))
    assert result['date_start']==start and result['date_end']==end
    reports=[c.query for c in client.searches if 'metrics.' in c.query]
    assert len(reports)==1 and start in reports[0] and end in reports[0]


CORRUPTIONS=[
 ('experiment','missing'),('experiment','duplicate'),('experiment','foreign'),('experiment','mismatched_id'),
 ('experiment','unsupported'),('experiment','unknown_type'),('experiment','unknown_status'),('experiment','unknown_promote'),
 ('experiment_arm','missing'),('experiment_arm','duplicate'),('experiment_arm','foreign'),('experiment_arm','dangling'),
 ('experiment_arm','both_control'),('experiment_arm','unequal_split'),('experiment_arm','two_campaigns'),('experiment_arm','sibling_campaign'),
 ('campaign','missing'),('campaign','duplicate'),('campaign','foreign'),('campaign','wrong_channel'),
 ('campaign','duplicate_automation'),('campaign','unknown_automation'),
 ('customer','missing'),('customer','duplicate'),('customer','foreign'),('customer','bad_currency'),('customer','bad_zone'),
]


def corrupt_rows(resource,fault,rows):
    rows=deepcopy(rows)
    if fault=='missing':return []
    if fault=='duplicate':return rows+[deepcopy(rows[0])]
    row=rows[0][resource]
    if fault=='foreign':
        if resource=='customer':row['id']=int(h.OTHER_CUSTOMER_ID)
        else:row['resource_name']=row['resource_name'].replace(h.CUSTOMER_ID,h.OTHER_CUSTOMER_ID)
    elif fault=='mismatched_id':row['experiment_id']=999
    elif fault=='unsupported':row['type_']='SEARCH_CUSTOM'
    elif fault=='unknown_type':row['type_']=999
    elif fault=='unknown_status':row['status']=999
    elif fault=='unknown_promote':row['promote_status']=999
    elif fault=='dangling':row['experiment']=rn('experiments',999)
    elif fault=='both_control':rows[1][resource]['control']=True
    elif fault=='unequal_split':row['traffic_split']=49
    elif fault=='two_campaigns':row['campaigns'].append(rn('campaigns',702))
    elif fault=='sibling_campaign':row['campaigns']=[rn('campaigns',702)]
    elif fault=='wrong_channel':row['advertising_channel_type']='SEARCH'
    elif fault=='duplicate_automation':row['asset_automation_settings'].append(deepcopy(row['asset_automation_settings'][0]))
    elif fault=='unknown_automation':row['asset_automation_settings'][0]['asset_automation_status']=999
    elif fault=='bad_currency':row['currency_code']='ZZZ'
    elif fault=='bad_zone':row['time_zone']='Not/A_Zone'
    return rows


@pytest.mark.parametrize('resource,fault',CORRUPTIONS)
def test_complete_graph_verification_refuses_corruption_without_enum_warnings(tmp_path,resource,fault):
    tool='get_pmax_url_experiment';server,client=setup(tmp_path,{tool})
    client.corrupt[resource]=lambda rows:corrupt_rows(resource,fault,rows)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter('always');rejected(server,client,tool,ARGS[tool])
    assert not caught


@pytest.mark.parametrize('status',['ENABLED','HALTED','REMOVED','PROMOTED','SETUP','INITIATED','GRADUATED'])
def test_inspection_exposes_actual_known_inactive_states(tmp_path,status):
    tool='get_pmax_url_experiment';server,client=setup(tmp_path,{tool})
    client.data[h.CUSTOMER_ID]['experiment'][0]['experiment']['status']=status
    result=h.expect_ok(h.call(server,tool,ARGS[tool]))
    assert result['experiment']['status']==status


@pytest.mark.parametrize('count',[100,101])
def test_catalog_limit_requires_complete_bounded_result_and_one_lookahead(tmp_path,count):
    tool='list_pmax_url_experiments';server,client=setup(tmp_path,{tool})
    data=client.data[h.CUSTOMER_ID]
    data['experiment']=[experiment(1000+i) for i in range(count)]
    data['experiment_arm']=[arm(1000+i,a,701) for i in range(count) for a in (1,2)]
    if count==100:
        result=h.expect_ok(h.call(server,tool));assert result['complete'] is True and len(result['experiments'])==100
    else:rejected(server,client,tool,{})
    assert client.pulls['experiment']<=101


@pytest.mark.parametrize('tool',sorted(INSPECTION_READS))
def test_read_retries_never_return_partial_state_or_raw_provider_details(tmp_path,tool):
    server,client=setup(tmp_path,{tool});client.stub_error(__import__('google.api_core.exceptions',fromlist=['ServiceUnavailable']).ServiceUnavailable(PRIVATE),times=1)
    h.expect_ok(h.call(server,tool,ARGS[tool]));assert len(client.searches)>1
    client.fail_after['report' if tool.endswith('_results') else 'experiment']=1
    rejected(server,client,tool,ARGS[tool])


def test_projected_byte_cap_prevents_unbounded_inspection_payload(tmp_path):
    tool='get_pmax_url_experiment';server,client=setup(tmp_path,{tool})
    client.data[h.CUSTOMER_ID]['experiment'][0]['experiment']['name']='x'*(16*1024*1024+1)
    rejected(server,client,tool,ARGS[tool]);assert client.pulls['experiment']<=2


@pytest.mark.parametrize('field',METRIC_NAMES)
def test_optional_metrics_preserve_absence_separately_from_measured_zero(tmp_path,field):
    tool='get_pmax_url_experiment_results';server,client=setup(tmp_path,{tool})
    client.data[h.CUSTOMER_ID]['report'][0]['metrics']={field:0}
    actual=h.expect_ok(h.call(server,tool,RESULT_ARGS))
    expected=json.loads((FIXTURES/(tool+'.json')).read_text())['golden']
    for group in ('treatment','control'):
        for key in expected[group]:
            source=key if group=='treatment' else 'control_'+('conversion_value' if key=='conversions_value' else key)
            assert actual[group][key]==(0 if field==source else None)
    for group in ('clicks','conversions'):
        for key in expected['statistics'][group]:
            if key!='unit':assert actual['statistics'][group][key]==(0 if field==key else None)


@pytest.mark.parametrize('field,value',[
 ('clicks',-1),('control_clicks',-1),('impressions',-1),('control_impressions',-1),
 ('cost_micros',-1),('control_cost_micros',-1),('conversions',float('nan')),('control_conversions',float('inf')),
 ('conversions_value',float('-inf')),('control_conversion_value',float('nan')),
 ('clicks_point_estimate',float('nan')),('clicks_margin_of_error',-0.01),('clicks_p_value',-0.01),('clicks_p_value',1.01),
 ('conversions_absolute_change_point_estimate',float('inf')),('conversions_absolute_change_margin_of_error',-0.01),
 ('conversions_absolute_change_p_value',-0.01),('conversions_absolute_change_p_value',1.01),
])
def test_metric_domains_reject_invalid_numbers_without_rejecting_signed_adjustments(tmp_path,field,value):
    tool='get_pmax_url_experiment_results';server,client=setup(tmp_path,{tool})
    client.data[h.CUSTOMER_ID]['report'][0]['metrics'][field]=value
    rejected(server,client,tool,RESULT_ARGS)


@pytest.mark.parametrize('kind',['empty','duplicate','foreign','sibling'])
def test_aggregate_report_is_not_fabricated_or_merged_across_identities(tmp_path,kind):
    tool='get_pmax_url_experiment_results';server,client=setup(tmp_path,{tool})
    client.corrupt['report']=lambda rows: [] if kind=='empty' else (
        rows+rows if kind=='duplicate' else [metric_row(301,h.OTHER_CUSTOMER_ID)] if kind=='foreign' else [metric_row(302)])
    if kind!='empty':rejected(server,client,tool,RESULT_ARGS);return
    result=h.expect_ok(h.call(server,tool,RESULT_ARGS));assert result['no_data'] is True
    assert all(v is None for group in ('treatment','control') for v in result[group].values())
    quiet(result)


def test_maximum_signed_int64_id_and_hyphenated_selected_account_are_valid_neighbors(tmp_path):
    tool='get_pmax_url_experiment';server,client=setup(tmp_path,{tool})
    ident=9223372036854775807;data=client.data[h.CUSTOMER_ID]
    data['experiment'][0]=experiment(ident)
    data['experiment_arm'][:2]=[arm(ident,a,701) for a in (1,2)]
    result=h.expect_ok(h.call(server,tool,{'experiment_id':str(ident),'customer_id':h.CUSTOMER_ID_DASHED}))
    assert result['experiment']['experiment_id']==str(ident) and result['customer_id']==h.CUSTOMER_ID
