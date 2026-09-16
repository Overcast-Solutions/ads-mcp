"""Green controls for original fixture types, provider projection and inventories."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest
from google.protobuf.json_format import MessageToDict

import harness as h
from shared_targeting_oracle import (TargetingClient,CATEGORIES,NUMERIC,FACTS,ADDITIONS,READS,SHARED_READS,SHARED_WRITES,
    DEMO_READS,DEMO_WRITES,SIGNATURES,rn,demographic,assert_queries,assert_expansion)


def test_independent_categories_and_all_synthetic_accounts_parse_as_v25():
    provider=TargetingClient()
    assert len(provider.data)==2
    for data in provider.data.values():
        for rows in data.values():
            if isinstance(rows,list):
                for row in rows:assert h.make_row(row)._pb.DESCRIPTOR.full_name=='google.ads.googleads.v25.services.GoogleAdsRow'
    for dimension,values in CATEGORIES.items():
        assert len(values)==len(set(values))
        for value in values:
            for level in ('ad_group','campaign'):
                row=h.make_row(demographic(dimension,value,level=level))
                assert getattr(getattr(row,level+'_criterion'),dimension.lower()).type_.name==value


@pytest.mark.parametrize('field',NUMERIC)
def test_numeric_presence_is_preserved_by_genuine_projection(field):
    provider=TargetingClient();entity=provider.data[h.CUSTOMER_ID]['ad_group_criterion'][0]['ad_group_criterion']
    query=f'SELECT ad_group_criterion.resource_name, ad_group_criterion.{field} FROM ad_group_criterion WHERE ad_group_criterion.ad_group = "{rn("adGroups",801)}" LIMIT 101'
    rows=list(provider.get_service('GoogleAdsService').search(customer_id=h.CUSTOMER_ID,query=query))
    assert len(rows)==1 and not rows[0].ad_group_criterion._pb.HasField(field)
    entity[field]=0
    rows=list(provider.get_service('GoogleAdsService').search(customer_id=h.CUSTOMER_ID,query=query))
    assert rows[0].ad_group_criterion._pb.HasField(field) and getattr(rows[0].ad_group_criterion,field)==0
    assert not rows[0].ad_group_criterion._pb.HasField('criterion_id'),'Unselected data must stay absent'


def test_transport_filters_account_and_sibling_before_projection():
    provider=TargetingClient()
    for customer in (h.CUSTOMER_ID,h.OTHER_CUSTOMER_ID):
        query=f'SELECT shared_criterion.resource_name, shared_criterion.negative FROM shared_criterion WHERE shared_criterion.shared_set = "{rn("sharedSets",401,customer)}" LIMIT 5001'
        rows=list(provider.get_service('GoogleAdsService').search(customer_id=customer,query=query))
        assert {r.shared_criterion.resource_name for r in rows}=={rn('sharedCriteria','401~601',customer),rn('sharedCriteria','401~602',customer)}
        assert all(r.shared_criterion.negative is False for r in rows)
        assert all(not r.shared_criterion.keyword.text for r in rows)


def test_query_permissions_reject_nonselectable_parent_message_and_unfilterable_leaf():
    for query in ('SELECT ad_group.targeting_setting FROM ad_group',
                  'SELECT ad_group.id FROM ad_group WHERE ad_group.targeting_setting.target_restrictions = TRUE',
                  'SELECT ad_group_criterion.resource_name FROM ad_group_criterion ORDER BY ad_group_criterion.url_custom_parameters'):
        with pytest.raises(AssertionError):assert_queries([SimpleNamespace(query=query)])
    facts=json.loads(FACTS.read_text())
    assert facts['numeric_presence']==dict.fromkeys(NUMERIC,True)
    assert facts['resources']['ad_group']['fields']['ad_group.targeting_setting.target_restrictions']==[True,False,False]


@pytest.mark.parametrize('service,request_name,method,create',[
    ('SharedSetService','MutateSharedSetsRequest','mutate_shared_sets',{'name':'Original synthetic list','type_':'NEGATIVE_KEYWORDS'}),
    ('SharedCriterionService','MutateSharedCriteriaRequest','mutate_shared_criteria',{'shared_set':rn('sharedSets',401),'keyword':{'text':'new synthetic','match_type':'EXACT'}}),
    ('CampaignSharedSetService','MutateCampaignSharedSetsRequest','mutate_campaign_shared_sets',{'campaign':rn('campaigns',703),'shared_set':rn('sharedSets',401)}),
    ('AdGroupCriterionService','MutateAdGroupCriteriaRequest','mutate_ad_group_criteria',{'ad_group':rn('adGroups',801),'negative':True,'gender':{'type_':'FEMALE'}})])
def test_genuine_requests_preview_without_changes_then_apply_only_named_resource(service,request_name,method,create):
    from google.protobuf.json_format import ParseDict
    provider=TargetingClient();before=deepcopy(provider.data);request=h.get_ads_type(request_name)
    ParseDict({'customer_id':h.CUSTOMER_ID,'validate_only':True,'partial_failure':False,'operations':[{'create':create}]},request._pb)
    result=getattr(provider.get_service(service),method)(request=request)
    assert result._pb.DESCRIPTOR.full_name.startswith('google.ads.googleads.v25.services.') and provider.data==before
    request.validate_only=False;getattr(provider.get_service(service),method)(request=request)
    assert provider.data[h.OTHER_CUSTOMER_ID]==before[h.OTHER_CUSTOMER_ID] and provider.data[h.CUSTOMER_ID]!=before[h.CUSTOMER_ID]
    assert len(provider.live_mutations())==1
    if service=='SharedCriterionService':assert not request.operations[0].create._pb.HasField('negative')


def test_fixed_incremental_inventory_cannot_admit_substitution_or_delete_prior_names():
    from tool_catalog import ALL_WRITE_MODE_TOOLS
    from pmax_oracle import PMAX_ADDITIONS,SEARCH_URL_ADDITIONS
    prior=ALL_WRITE_MODE_TOOLS|PMAX_ADDITIONS|SEARCH_URL_ADDITIONS
    assert len(prior)==67 and len(ADDITIONS)==9 and len(READS)==3
    assert set(SIGNATURES)==ADDITIONS and SHARED_READS|SHARED_WRITES|DEMO_READS|DEMO_WRITES==ADDITIONS
    for extra in (set(),SHARED_READS|SHARED_WRITES,ADDITIONS):assert_expansion(prior|extra,prior,ADDITIONS)
    with pytest.raises(AssertionError):assert_expansion((prior-{'health_check'})|{'unapproved'},prior,ADDITIONS)
    with pytest.raises(AssertionError):assert_expansion(prior|{'unapproved'},prior,ADDITIONS)


def test_installed_driver_closes_process_and_readers_without_network(tmp_path,monkeypatch):
    import test_auth_cause_contract as process
    from shared_targeting_oracle import INSTALLED_INJECTION
    monkeypatch.setattr(process,'INJECTION',INSTALLED_INJECTION)
    with process.InstalledServer(tmp_path/'installed') as installed:
        listing=installed.receive(installed.send('tools/list',{}))['result']['tools']
        assert any(tool['name']=='health_check' for tool in listing)
    assert installed.process.poll()==0 and all(not thread.is_alive() for thread in installed.readers)
    assert not installed.stderr and not installed.events('network attempted')


@pytest.mark.parametrize('name',sorted(READS))
def test_new_golden_attributed_inputs_agree_with_plain_parity_transport(name):
    from offline_contract import project
    from shared_targeting_oracle import FIXTURES
    fixture=h.load_contract_fixture(FIXTURES/(name+'.json'))
    provider=TargetingClient();provider.data[h.CUSTOMER_ID]=deepcopy(fixture['gaql'])
    parity=h.FakeGoogleAdsClient()
    for resource,rows in fixture['gaql'].items():parity.stub(resource,rows)
    facts=json.loads(FACTS.read_text())['resources']
    for resource in fixture['gaql']:
        fields=[field for owner in [resource,*facts[resource]['attributed_resources']] for field,permissions in facts[owner]['fields'].items() if permissions[0]]
        query='SELECT '+', '.join(fields)+' FROM '+resource
        projected=list(provider.get_service('GoogleAdsService').search(customer_id=h.CUSTOMER_ID,query=query))
        plain=parity.get_service('GoogleAdsService').search(customer_id=h.CUSTOMER_ID,query=query)
        serial=lambda rows:[MessageToDict(row._pb,preserving_proto_field_name=True) for row in rows]
        assert serial(projected)==serial([project(row,fields) for row in plain]),(name,resource)
