"""Independent provider/fixture controls; these intentionally pass before features."""
from copy import deepcopy
import inspect
import json
from types import SimpleNamespace

import pytest
from google.api_core.operation import Operation
from google.ads.googleads.v25.services.services.experiment_service import ExperimentServiceClient
from google.ads.googleads.v25.services.services.experiment_arm_service import ExperimentArmServiceClient
from google.ads.googleads.v25.services.services.experiment_service.pagers import ListExperimentAsyncErrorsPager
from google.ads.googleads.v25.services.types import PromoteExperimentMetadata
from google.protobuf import empty_pb2
from google.protobuf.json_format import MessageToDict

import harness as h
from pmax_experiment_oracle import (FACTS,FIXTURES,READS,METRIC_NAMES,TYPE,EXPANSION,TEXT,HANDLE,
    ExperimentClient,assert_queries,rn,standard_data,raw_operation,future,metric_row)


def test_every_synthetic_row_parses_as_genuine_v25_and_accounts_have_siblings():
    for customer in (h.CUSTOMER_ID,h.OTHER_CUSTOMER_ID):
        data=standard_data(customer)
        assert len(data['experiment'])==2 and len(data['experiment_arm'])==4 and len(data['campaign'])==3
        for key,rows in data.items():
            if key=='untouched':continue
            for raw in rows:
                row=h.make_row(raw)
                assert row._pb.DESCRIPTOR.full_name=='google.ads.googleads.v25.services.GoogleAdsRow'
        assert all(r['experiment']['resource_name'].startswith('customers/'+customer+'/') for r in data['experiment'])


def test_four_goldens_are_independent_complete_v25_resources():
    paths=sorted(FIXTURES.glob('*.json'))
    assert {json.loads(p.read_text())['tool'] for p in paths}==READS
    for path in paths:
        fixture=h.load_contract_fixture(path)
        assert fixture['golden']['customer_id']==h.CUSTOMER_ID
        for rows in fixture['gaql'].values():
            for row in rows:h.make_row(row)


@pytest.mark.parametrize('field',METRIC_NAMES)
def test_official_metric_type_and_optional_presence_distinguish_absence_zero_and_value(field):
    fact=json.loads(FACTS.read_text())['metric_presence'][field]
    metrics=h.get_ads_type('Metrics');desc=metrics._pb.DESCRIPTOR.fields_by_name[field]
    assert desc.has_presence is fact['has_presence'] is True
    assert desc.type==({'int64':3,'double':1}[fact['protobuf_type']])
    assert not metrics._pb.HasField(field)
    setattr(metrics,field,0)
    assert metrics._pb.HasField(field) and getattr(metrics,field)==0
    metrics._pb.ClearField(field)
    assert not metrics._pb.HasField(field)


def test_official_field_flags_are_pinned_independently_of_query_implementation():
    facts=json.loads(FACTS.read_text())
    assert facts['fields']['experiment.long_running_operation']=={
        'selectable':True,'filterable':False,'sortable':False,'repeated':False,'data_type':'STRING'}
    assert facts['fields']['experiment_arm.campaigns']['repeated'] is True
    assert facts['fields']['experiment_arm.resource_name']['sortable'] is False
    assert facts['fields']['campaign.asset_automation_settings']['filterable'] is False
    assert facts['fields']['segments.date']['filterable'] is True
    campaign=h.get_ads_type('Campaign')._pb.DESCRIPTOR.fields_by_name
    assert 'start_date_time' in campaign and 'end_date_time' in campaign
    assert 'start_date' not in campaign and 'end_date' not in campaign
    assert 'asset_automation_settings' in campaign and 'url_expansion_opt_out' not in campaign
    for field in facts['fields']:
        owner,name=field.split('.')
        message=h.get_ads_type({'experiment':'Experiment','experiment_arm':'ExperimentArm','campaign':'Campaign',
                               'customer':'Customer','metrics':'Metrics','segments':'Segments'}[owner])
        assert name in message._pb.DESCRIPTOR.fields_by_name or name+'_' in message._pb.DESCRIPTOR.fields_by_name


@pytest.mark.parametrize('query',[
 "SELECT experiment.resource_name, experiment.experiment_id, experiment.long_running_operation FROM experiment WHERE experiment.experiment_id = 301",
 "SELECT experiment_arm.resource_name, experiment_arm.control, experiment_arm.campaigns FROM experiment_arm WHERE experiment_arm.experiment = 'customers/9876543210/experiments/301'",
 "SELECT experiment.resource_name, metrics.clicks, metrics.control_clicks, metrics.conversions_absolute_change_p_value FROM experiment WHERE experiment.experiment_id = 301 AND segments.date BETWEEN '2026-09-01' AND '2026-09-15'",
 "SELECT campaign.resource_name, campaign.start_date_time, campaign.asset_automation_settings FROM campaign WHERE campaign.id = 703",
])
def test_query_permission_positive_controls(query):
    assert_queries([SimpleNamespace(query=query)])


@pytest.mark.parametrize('query',[
 "SELECT experiment.resource_name FROM experiment WHERE experiment.long_running_operation = 'opaque'",
 "SELECT experiment_arm.resource_name FROM experiment_arm ORDER BY experiment_arm.resource_name",
 "SELECT experiment.resource_name, metrics.control_clicks FROM campaign WHERE campaign.id = 701",
 "SELECT experiment.resource_name, metrics.control_clicks FROM experiment WHERE experiment.experiment_id = 301",
 "SELECT experiment.resource_name, metrics.control_clicks, segments.date FROM experiment WHERE experiment.experiment_id = 301 AND segments.date BETWEEN '2026-09-01' AND '2026-09-15'",
 "SELECT campaign.resource_name FROM campaign WHERE campaign.asset_automation_settings = 'x'",
 "SELECT campaign.start_date FROM campaign",
])
def test_query_permission_negative_controls_discriminate_real_provider_incompatibility(query):
    with pytest.raises(AssertionError):assert_queries([SimpleNamespace(query=query)])


def test_projected_transport_does_not_leak_unselected_fields_or_other_account():
    client=ExperimentClient()
    query="SELECT experiment.resource_name, experiment.experiment_id FROM experiment WHERE experiment.experiment_id = 301"
    rows=list(client.get_service('GoogleAdsService').search(customer_id=h.OTHER_CUSTOMER_ID,query=query))
    assert len(rows)==1
    raw=MessageToDict(rows[0]._pb,preserving_proto_field_name=True)
    assert raw=={'experiment':{'resource_name':rn('experiments',301,h.OTHER_CUSTOMER_ID),'experiment_id':'301'}}
    assert client.pulls['experiment']==1


def test_sdk_request_shapes_support_validation_and_exact_repeated_list_mask():
    for name,field in (('EndExperimentRequest','experiment'),('PromoteExperimentRequest','resource_name')):
        request=h.get_ads_type(name);setattr(request,field,rn('experiments',301));request.validate_only=True
        assert set(MessageToDict(request._pb,preserving_proto_field_name=True))=={field,'validate_only'}
    request=h.get_ads_type('MutateGoogleAdsRequest');request.customer_id=h.CUSTOMER_ID;request.validate_only=True
    operation=h.get_ads_type('MutateOperation')
    operation.experiment_operation.create.resource_name=rn('experiments',-1)
    operation.experiment_operation.create.name='Synthetic trial'
    operation.experiment_operation.create.type_=TYPE
    operation.experiment_operation.create.start_date='2026-09-15';operation.experiment_operation.create.end_date='2026-10-15'
    request.mutate_operations.append(operation)
    fields={field.name for field,_ in operation.experiment_operation.create._pb.ListFields()}
    assert fields=={'resource_name','name','type_','start_date','end_date'} and not request.partial_failure
    update=h.get_ads_type('CampaignOperation');update.update_mask.paths.append('asset_automation_settings')
    assert list(update.update_mask.paths)==['asset_automation_settings']
    assert ExperimentArmServiceClient.experiment_arm_path(h.CUSTOMER_ID,'-1','-2')==rn('experimentArms','-1~-2')


def test_sdk_async_enum_mapping_is_explicit():
    enum=h.FakeGoogleAdsClient().enums.AsyncActionStatusEnum
    assert {value.name:value.value for value in enum}=={'UNSPECIFIED':0,'UNKNOWN':1,'NOT_STARTED':2,
        'IN_PROGRESS':3,'COMPLETED':4,'FAILED':5,'COMPLETED_WITH_WARNING':6}


@pytest.mark.parametrize('done,result',[(False,None),(True,'response'),(True,'error')])
def test_sdk_future_preserves_raw_operation_typed_metadata_and_immediate_terminal_states(done,result):
    raw=raw_operation(done=done,result=result);wrapped=future(raw)
    assert isinstance(wrapped,Operation) and wrapped.operation==raw
    metadata=PromoteExperimentMetadata();assert raw.metadata.Unpack(metadata._pb)
    assert metadata.experiment==rn('experiments',301)
    assert raw.WhichOneof('result')==result
    if result=='response':assert raw.response.Unpack(empty_pb2.Empty())
    if not done:
        with pytest.raises(AssertionError,match='hidden polling'):wrapped.done()
    source=inspect.getsource(ExperimentServiceClient.promote_experiment)
    assert 'operation.from_gapic(' in source and 'metadata_type=experiment_service.PromoteExperimentMetadata' in source


def test_end_sdk_none_return_and_async_pager_are_not_raw_wire_messages():
    annotation=inspect.signature(ExperimentServiceClient.end_experiment).return_annotation
    assert annotation in (None,type(None),'None')
    client=ExperimentClient();request=h.get_ads_type('EndExperimentRequest');request.experiment=rn('experiments',301);request.validate_only=True
    assert client.get_service('ExperimentService').end_experiment(request=request,retry=None) is None
    errors=h.get_ads_type('ListExperimentAsyncErrorsRequest');errors.resource_name=rn('experiments',301);errors.page_size=100
    pager=client.get_service('ExperimentService').list_experiment_async_errors(request=errors,retry=None)
    assert isinstance(pager,ListExperimentAsyncErrorsPager)
    first=next(pager.pages);assert first._pb.DESCRIPTOR.name=='ListExperimentAsyncErrorsResponse' and first.errors[0].code==13


def test_opaque_operation_poll_is_bound_to_original_manager_transport():
    client=ExperimentClient();service=client.get_service('ExperimentService')
    assert service.transport.operations_client is service
    observed=service.transport.operations_client.get_operation(name=HANDLE,retry=None,timeout=15)
    assert observed==client.operation and client.polls[0]['login_customer_id']==h.LOGIN_CUSTOMER_ID


def test_genuine_pager_fetches_individual_pages_with_explicit_disabled_rpc_retry():
    client=ExperimentClient();client.error_pages=[{'errors':[{'code':7}],'next_page_token':'1'}, {'errors':[{'code':13}]}]
    request=h.get_ads_type('ListExperimentAsyncErrorsRequest');request.resource_name=rn('experiments',301);request.page_size=100
    pager=client.experiment_service.list_experiment_async_errors(request=request,retry=None,timeout=15)
    iterator=pager.pages
    assert next(iterator).errors[0].code==7 and len(client.error_calls)==1
    assert next(iterator).errors[0].code==13 and len(client.error_calls)==2
    with pytest.raises(StopIteration):next(iterator)


def test_provider_substitute_creates_real_four_result_receipt_and_projectable_state():
    client=ExperimentClient();request=h.get_ads_type('MutateGoogleAdsRequest');request.customer_id=h.CUSTOMER_ID
    for kind in ('experiment','experiment_arm','experiment_arm','campaign'):
        operation=h.get_ads_type('MutateOperation')
        if kind=='experiment':
            entity=operation.experiment_operation.create
            entity.resource_name=rn('experiments',-1);entity.name='Provider control';entity.type_=TYPE
            entity.start_date='2026-09-15';entity.end_date='2026-10-15'
        elif kind=='experiment_arm':
            number=len(request.mutate_operations)
            entity=operation.experiment_arm_operation.create;entity.resource_name=rn('experimentArms',f'-1~-{number+1}')
            entity.experiment=rn('experiments',-1);entity.control=number==1;entity.traffic_split=50
            entity.name='Control' if number==1 else 'Treatment';entity.campaigns.append(rn('campaigns',703))
        else:
            entity=operation.campaign_operation.update;entity.resource_name=rn('campaigns',703)
            entity.asset_automation_settings.append({'asset_automation_type':EXPANSION,'asset_automation_status':'OPTED_IN'})
            operation.campaign_operation.update_mask.paths.append('asset_automation_settings')
        request.mutate_operations.append(operation)
    before=deepcopy(client.data)
    request.validate_only=True
    validated=client.get_service('GoogleAdsService').mutate(request=request,retry=None)
    assert validated._pb.DESCRIPTOR.name=='MutateGoogleAdsResponse' and not validated.mutate_operation_responses and client.data==before
    request.validate_only=False
    response=client.get_service('GoogleAdsService').mutate(request=request,retry=None)
    assert [r._pb.WhichOneof('response') for r in response.mutate_operation_responses]==[
        'experiment_result','experiment_arm_result','experiment_arm_result','campaign_result']
    assert response.mutate_operation_responses[0].experiment_result.resource_name==rn('experiments',901)
    assert client.data[h.OTHER_CUSTOMER_ID]==before[h.OTHER_CUSTOMER_ID]
    for key in ('experiment','experiment_arm','campaign'):
        for row in client.data[h.CUSTOMER_ID][key]:h.make_row(row)
