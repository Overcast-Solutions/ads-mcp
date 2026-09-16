"""Independent synthetic targeting transport and fixed workflow inventories.

The provider substitute stores raw v25 resources, projects only selected fields,
and applies exact service requests. It contains no planner or product validator.
"""
from collections import Counter
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import re

from google.api_core.exceptions import ServiceUnavailable
from google.protobuf.json_format import MessageToDict, ParseDict

import harness as h
from offline_contract import project, selected, refusal

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / 'tests/fixtures/shared_targeting'
FACTS = ROOT / 'tests/fixtures/shared_targeting_provider_v25.json'
SHARED_READS = frozenset({'list_shared_negative_keyword_lists', 'get_shared_negative_keyword_list'})
SHARED_WRITES = frozenset({'create_shared_negative_keyword_list', 'add_shared_negative_keywords',
    'remove_shared_negative_keywords', 'attach_shared_negative_keyword_list', 'detach_shared_negative_keyword_list'})
DEMO_READS = frozenset({'get_demographic_targeting'})
DEMO_WRITES = frozenset({'update_demographic_targeting'})
READS = SHARED_READS | DEMO_READS
WRITES = SHARED_WRITES | DEMO_WRITES
ADDITIONS = READS | WRITES
CATEGORIES = {
    'AGE_RANGE': ['AGE_RANGE_18_24', 'AGE_RANGE_25_34', 'AGE_RANGE_35_44', 'AGE_RANGE_45_54', 'AGE_RANGE_55_64', 'AGE_RANGE_65_UP', 'AGE_RANGE_UNDETERMINED'],
    'GENDER': ['MALE', 'FEMALE', 'UNDETERMINED'],
    'INCOME_RANGE': ['INCOME_RANGE_0_50', 'INCOME_RANGE_50_60', 'INCOME_RANGE_60_70', 'INCOME_RANGE_70_80', 'INCOME_RANGE_80_90', 'INCOME_RANGE_90_UP', 'INCOME_RANGE_UNDETERMINED'],
    'PARENTAL_STATUS': ['PARENT', 'NOT_A_PARENT', 'UNDETERMINED'],
}
NUMERIC = ['bid_modifier', 'cpc_bid_micros', 'cpm_bid_micros', 'cpv_bid_micros', 'percent_cpc_bid_micros']
STRINGS = ['final_url_suffix', 'tracking_url_template']
LISTS = ['final_urls', 'final_mobile_urls', 'url_custom_parameters', 'labels']
CUSTOM = NUMERIC + STRINGS + LISTS
SIGNATURES = {
    'list_shared_negative_keyword_lists': (['customer_id'], []),
    'get_shared_negative_keyword_list': (['shared_set_id','customer_id'], ['shared_set_id']),
    'create_shared_negative_keyword_list': (['name','customer_id'], ['name']),
    'add_shared_negative_keywords': (['shared_set_id','keywords','customer_id'], ['shared_set_id','keywords']),
    'remove_shared_negative_keywords': (['shared_set_id','criterion_ids','customer_id'], ['shared_set_id','criterion_ids']),
    'attach_shared_negative_keyword_list': (['shared_set_id','campaign_ids','customer_id'], ['shared_set_id','campaign_ids']),
    'detach_shared_negative_keyword_list': (['shared_set_id','campaign_ids','customer_id'], ['shared_set_id','campaign_ids']),
    'get_demographic_targeting': (['ad_group_id','customer_id'], ['ad_group_id']),
    'update_demographic_targeting': (['ad_group_id','changes','customer_id'], ['ad_group_id','changes']),
}
SHARED_ARGS = {
    'create_shared_negative_keyword_list': {'name':'Seasonal exclusions'},
    'add_shared_negative_keywords': {'shared_set_id':'401','keywords':[{'text':'sample clearance','match_type':'PHRASE'}]},
    'remove_shared_negative_keywords': {'shared_set_id':'401','criterion_ids':['601']},
    'attach_shared_negative_keyword_list': {'shared_set_id':'401','campaign_ids':['703']},
    'detach_shared_negative_keyword_list': {'shared_set_id':'401','campaign_ids':['701']},
}
BAD_IDS = [None, True, False, 401, 0, -1, 1.5, [], {}, '', '0', '-1', '+1', '01', ' 401', '401 ', '4.01', '4e2', '٤٠١', '４０１', '401~601', '9223372036854775808', '9'*5000, 'customers/5556667777/sharedSets/401']
BAD_CUSTOMERS = [True, False, 9876543210, 1.5, [], {}, '', 'null', 'None', ' 9876543210', '9876543210 ', '9876543210 OR 1=1']
NEW_SOURCE = [
    'tests/shared_targeting_oracle.py','tests/test_shared_targeting_oracle_controls.py',
    'tests/test_shared_negative_lists_contract.py','tests/test_demographic_targeting_contract.py',
    'tests/test_shared_targeting_workflow_contract.py','tests/fixtures/shared_targeting_provider_v25.json',
    'docs/shared-targeting.md', *['tests/fixtures/shared_targeting/'+name+'.json' for name in sorted(READS)],
]


def assert_expansion(actual, baseline, additions):
    actual=set(actual)
    assert set(baseline) <= actual <= set(baseline) | set(additions), {
        'missing_prior':sorted(set(baseline)-actual), 'unapproved':sorted(actual-set(baseline)-set(additions))}


def rn(kind, ident, customer=h.CUSTOMER_ID):
    return f'customers/{customer}/{kind}/{ident}'


def campaign(ident=701, customer=h.CUSTOMER_ID, channel='SEARCH', **extra):
    return {'customer':{'id':int(customer),'currency_code':'USD'}, 'campaign':{
        'id':ident,'resource_name':rn('campaigns',ident,customer),'name':f'Synthetic {channel} campaign {ident}',
        'status':'ENABLED','advertising_channel_type':channel,'advertising_channel_sub_type':'UNSPECIFIED',
        'campaign_budget':rn('campaignBudgets',900+ident,customer), **extra}}


def group(ident=801, customer=h.CUSTOMER_ID, channel='SEARCH', **extra):
    parent=701 if channel=='SEARCH' else 704
    return {**campaign(parent,customer,channel), 'ad_group':{
        'id':ident,'resource_name':rn('adGroups',ident,customer),'name':f'Synthetic {channel} group {ident}',
        'campaign':rn('campaigns',parent,customer),'type_':channel+'_STANDARD','status':'ENABLED',
        'cpc_bid_micros':1300000,'optimized_targeting_enabled':channel=='DISPLAY',
        'targeting_setting':{'target_restrictions':[{'targeting_dimension':'AUDIENCE','bid_only':True}]}, **extra}}


def shared_set(ident=401, customer=h.CUSTOMER_ID, **extra):
    return {'shared_set':{'id':ident,'resource_name':rn('sharedSets',ident,customer),'name':f'Synthetic exclusions {ident}',
        'type_':'NEGATIVE_KEYWORDS','status':'ENABLED','member_count':2,'reference_count':2, **extra}}


def member(ident=601, shared=401, customer=h.CUSTOMER_ID, **extra):
    return {'shared_criterion':{'criterion_id':ident,'resource_name':rn('sharedCriteria',f'{shared}~{ident}',customer),
        'shared_set':rn('sharedSets',shared,customer),'type_':'KEYWORD','negative':False,
        'keyword':{'text':f'clearance item {ident}','match_type':'EXACT'}, **extra}}


def link(campaign_id=701, shared=401, customer=h.CUSTOMER_ID, **extra):
    return {'campaign_shared_set':{'resource_name':rn('campaignSharedSets',f'{campaign_id}~{shared}',customer),
        'campaign':rn('campaigns',campaign_id,customer),'shared_set':rn('sharedSets',shared,customer),'status':'ENABLED', **extra}}


def demographic(dimension='AGE_RANGE', value='AGE_RANGE_25_34', ident=601, group_id=801,
                customer=h.CUSTOMER_ID, *, negative=False, level='ad_group', status='ENABLED', **extra):
    key=level+'_criterion'
    parent=group_id if level=='ad_group' else 701
    path='adGroupCriteria' if level=='ad_group' else 'campaignCriteria'
    owner='adGroups' if level=='ad_group' else 'campaigns'
    return {key:{'criterion_id':ident,'resource_name':rn(path,f'{parent}~{ident}',customer),
        level:rn(owner,parent,customer),'type_':dimension,'negative':negative,'status':status,
        dimension.lower():{'type_':value}, **extra}}


def standard_data(customer):
    return {'customer':[{'customer':{'id':int(customer),'currency_code':'USD'}}],
        'campaign':[campaign(701,customer),campaign(702,customer,'SHOPPING'),campaign(703,customer),campaign(704,customer,'DISPLAY')],
        'ad_group':[group(801,customer),group(802,customer),group(804,customer,'DISPLAY')],
        'shared_set':[shared_set(401,customer),shared_set(402,customer,member_count=1,reference_count=1)],
        'shared_criterion':[member(601,401,customer),member(602,401,customer),member(603,402,customer)],
        'campaign_shared_set':[link(701,401,customer),link(702,401,customer),link(703,402,customer)],
        'ad_group_criterion':[demographic(customer=customer),demographic('GENDER','MALE',602,802,customer),
            demographic('PARENTAL_STATUS','PARENT',604,804,customer,negative=True)],
        'campaign_criterion':[],
        'untouched_budget_state':{'1701':{'amount_micros':53000000},'1702':{'amount_micros':21000000}}}


def value_at(raw, dotted):
    for part in dotted.split('.'):
        if not isinstance(raw,dict):
            return None
        raw=raw.get(part,raw.get(part+'_'))
    return raw


def assert_queries(calls):
    facts=json.loads(FACTS.read_text())['resources']
    assert calls
    for call in calls:
        query=call.query
        resource=h._FROM_RE.search(query).group(1)
        assert resource in facts,resource
        allowed={resource,*facts[resource]['attributed_resources']}
        where=re.search(r'\bWHERE\b(.*?)(?:\bORDER BY\b|\bLIMIT\b|$)',query,re.I|re.S)
        order=re.search(r'\bORDER BY\b(.*?)(?:\bLIMIT\b|$)',query,re.I|re.S)
        uses=[(f,0) for f in selected(query)]
        for clause,index in ((where,1),(order,2)):
            if clause:
                text=re.sub(r"'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\"",'',clause[1])
                uses.extend((f,index) for f in re.findall(r'\b[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+\b',text))
        for field,index in uses:
            owner=field.split('.')[0]
            assert owner in allowed and facts.get(owner,{}).get('fields',{}).get(field,[False]*3)[index], (resource,field,index)


class TargetingClient(h.FakeGoogleAdsClient):
    """Stateful provider-only substitution with bounded lazy iteration."""
    def __init__(self):
        super().__init__()
        self.data={cid:standard_data(cid) for cid in (h.CUSTOMER_ID,h.OTHER_CUSTOMER_ID)}
        self.corrupt={}
        self.fail_after={}
        self.pulls=Counter()
        self.lose_response=False
        self.reject_write=False
        self.next_id=9001
        self.after_write=None

    def _do_search(self,service,method,args,kwargs):
        query=str(h._req_field(args,kwargs,'query'))
        customer=str(h._req_field(args,kwargs,'customer_id'))
        resource=h._FROM_RE.search(query).group(1)
        self._responses[resource]=[]
        super()._do_search(service,method,args,kwargs)
        assert_queries([self.searches[-1]])
        data=deepcopy(self.data.get(customer,{}))
        rows=data.get(resource,[])
        for row in rows:
            entity=row.get(resource,{})
            if resource=='ad_group_criterion':
                row['ad_group']=deepcopy(next((r['ad_group'] for r in data['ad_group'] if r['ad_group']['resource_name']==entity.get('ad_group')),{}))
            parent=entity.get('campaign') or row.get('ad_group',{}).get('campaign')
            if parent:
                row['campaign']=deepcopy(next((r['campaign'] for r in data['campaign'] if r['campaign']['resource_name']==parent),{}))
            if entity.get('shared_set'):
                row['shared_set']=deepcopy(next((r['shared_set'] for r in data['shared_set'] if r['shared_set']['resource_name']==entity['shared_set']),{}))
            row.setdefault('customer',deepcopy(data['customer'][0]['customer']))
        where=re.search(r'\bWHERE\b(.*?)(?:\bORDER BY\b|\bLIMIT\b|$)',query,re.I|re.S)
        if where:
            pattern=r"([a-z][a-z0-9_.]+)\s*(NOT\s+IN|IN|!=|=)\s*(\([^)]*\)|'[^']*'|\"[^\"]*\"|[a-zA-Z_0-9]+)"
            for match in re.finditer(pattern,where[1],re.I):
                field,op,text=match.groups()
                wanted={next(v for v in item if v) for item in re.findall(r"'([^']*)'|\"([^\"]*)\"|([\w]+)",text)}
                def spelling(row):
                    v=value_at(row,field)
                    return str(v).upper() if isinstance(v,bool) else str(v)
                rows=[r for r in rows if (spelling(r) in wanted)!=(op.upper() in ('!=','NOT IN'))]
        if resource in self.corrupt:
            rows=self.corrupt[resource](rows)
        limit=re.search(r'\bLIMIT\s+(\d+)',query,re.I)
        if limit:
            rows=rows[:int(limit[1])]
        def stream():
            for index,row in enumerate(rows):
                if index==self.fail_after.get(resource):
                    raise ServiceUnavailable('Synthetic incomplete provider response')
                self.pulls[resource]+=1
                yield project(h.make_row(row),selected(query))
            if len(rows)==self.fail_after.get(resource):
                raise ServiceUnavailable('Synthetic incomplete provider response')
        return stream()

    def _do_mutation(self,service,method,args,kwargs):
        super()._do_mutation(service,method,args,kwargs)
        call=self.mutations[-1]
        specs={
            'SharedSetService':('shared_set','SharedSet','MutateSharedSetsResponse','sharedSets'),
            'SharedCriterionService':('shared_criterion','SharedCriterion','MutateSharedCriteriaResponse','sharedCriteria'),
            'CampaignSharedSetService':('campaign_shared_set','CampaignSharedSet','MutateCampaignSharedSetsResponse','campaignSharedSets'),
            'AdGroupCriterionService':('ad_group_criterion','AdGroupCriterion','MutateAdGroupCriteriaResponse','adGroupCriteria')}
        assert service in specs,service
        resource,entity_type,response_type,path=specs[service]
        request=call.request
        assert request is not None and hasattr(request,'_pb')
        assert not request.partial_failure
        if self.reject_write and not call.validate_only:
            raise h.make_google_ads_exception(['Synthetic criterion combination not accepted'])
        response=h.get_ads_type(response_type)
        data=self.data[request.customer_id]
        next_data=deepcopy(data)
        results=[]
        for operation in request.operations:
            kind=operation._pb.WhichOneof('operation')
            assert kind in ('create','remove','update')
            if kind=='remove':
                identity=operation.remove
                next_data[resource]=[r for r in next_data[resource] if r[resource]['resource_name']!=identity]
            elif kind=='update':
                raw=MessageToDict(operation.update._pb,preserving_proto_field_name=True)
                identity=operation.update.resource_name
                row=next(r[resource] for r in next_data[resource] if r[resource]['resource_name']==identity)
                for field in operation.update_mask.paths:
                    row[field]=raw.get(field)
            else:
                raw=MessageToDict(operation.create._pb,preserving_proto_field_name=True)
                if resource=='shared_set':
                    identity=rn(path,self.next_id,request.customer_id)
                    raw.update(id=self.next_id,status='ENABLED',type_='NEGATIVE_KEYWORDS',member_count=0,reference_count=0)
                elif resource=='campaign_shared_set':
                    identity=rn(path,raw['campaign'].split('/')[-1]+'~'+raw['shared_set'].split('/')[-1],request.customer_id)
                    raw.update(status='ENABLED')
                else:
                    parent=raw['shared_set' if resource=='shared_criterion' else 'ad_group'].split('/')[-1]
                    identity=rn(path,f'{parent}~{self.next_id}',request.customer_id)
                    raw.update(criterion_id=self.next_id)
                    raw['type_']='KEYWORD' if resource=='shared_criterion' else next(d for d in CATEGORIES if d.lower() in raw)
                    raw.setdefault('negative',False)
                    if resource=='ad_group_criterion':
                        raw.setdefault('status','ENABLED')
                raw['resource_name']=identity
                next_data[resource].append({resource:raw})
                if not call.validate_only:
                    self.next_id+=1
            results.append({'resource_name':identity})
        if not call.validate_only:
            for row in next_data['shared_set']:
                entity=row['shared_set']; identity=entity['resource_name']
                entity['member_count']=sum(r['shared_criterion']['shared_set']==identity for r in next_data['shared_criterion'])
                entity['reference_count']=sum(r['campaign_shared_set']['shared_set']==identity and r['campaign_shared_set']['status']!='REMOVED' for r in next_data['campaign_shared_set'])
            self.data[request.customer_id]=next_data
            if self.after_write:
                self.after_write()
            if self.lose_response:
                raise ServiceUnavailable('Synthetic uncertain mutation response')
        ParseDict({'results':results},response._pb)
        return response


def require(server,names):
    missing=set(names)-h.tool_names(server)
    assert not missing, 'Missing approved shared targeting behavior: '+', '.join(sorted(missing))


def setup(tmp_path,names,*,read_only=False,clock=None,env=None):
    provider=TargetingClient()
    server=(h.build_server if read_only else h.build_rw_server)(tmp_path,client=provider,clock=clock,
        env={'ADS_MCP_REQUIRE_DRY_RUN':'true',**(env or {})})
    require(server,names)
    return server,provider


def rejected(server,provider,tool,arguments,*,local=False):
    count=len(provider.searches); before=deepcopy(provider.data)
    result=h.call_result(server,tool,arguments)
    text=h.result_text(result)
    assert 'unknown tool' not in text.lower() and 'Traceback' not in text and 'INTERNAL' not in text
    h.assert_no_secrets(text)
    assert 'synthetic-private' not in text
    if not result.is_error:
        refusal(h.payload_of(result),provider)
    assert not provider.mutations and provider.data==before
    if local:
        assert len(provider.searches)==count
    return result


def stage(server,tool,arguments):
    payload=h.expect_ok(h.call(server,tool,arguments)); plan=payload['plan']
    assert plan['tool']==tool and plan['operations']
    h.parse_iso_utc(plan['expires_at'])
    return plan


def preview(server,plan):
    return h.expect_ok(h.call(server,'confirm_and_apply',{'plan_id':plan['id'],'dry_run':True,'confirm_irreversible':True}))


def apply(server,plan,*,ack=True):
    return h.call(server,'confirm_and_apply',{'plan_id':plan['id'],'dry_run':False,'confirm_irreversible':ack})


def checked_apply(server,provider,plan,service,method,request_type):
    before=deepcopy(provider.data); previous=len(provider.live_mutations())
    preview(server,plan)
    assert provider.data==before and len(provider.live_mutations())==previous
    queries=len(provider.searches)
    assert h.expect_ok(apply(server,plan))['applied'] is True
    assert len(provider.searches)>queries
    assert len(provider.live_mutations())==previous+1
    call=provider.live_mutations()[-1]
    assert (call.service,call.method)==(service,method)
    assert call.request._pb.DESCRIPTOR.full_name=='google.ads.googleads.v25.services.'+request_type
    assert call.request.customer_id==h.CUSTOMER_ID and not call.request.partial_failure and not call.request.validate_only
    assert provider.data[h.OTHER_CUSTOMER_ID]==before[h.OTHER_CUSTOMER_ID]
    assert provider.data[h.CUSTOMER_ID]['untouched_budget_state']==before[h.CUSTOMER_ID]['untouched_budget_state']
    assert_queries(provider.searches)
    return call.request,before


def require_projection(provider,resource,fields):
    covered=set()
    for call in provider.searches:
        if h._FROM_RE.search(call.query).group(1)==resource:
            covered.update(selected(call.query))
    assert {resource+'.'+field for field in fields}<=covered,(resource,sorted(covered))


def corrupt_field(provider,resource,field,value):
    def change(rows):
        rows=deepcopy(rows)
        if rows:
            parent=rows[0]
            parts=field.split('.')
            for part in parts[:-1]:
                parent=parent.setdefault(part,{})
            parent[parts[-1]]=value
        return rows
    provider.corrupt[resource]=change


def assert_stale(server,provider,plan):
    preview(server,plan)
    before=deepcopy(provider.data)
    assert refusal(apply(server,plan),provider)['code']=='STALE_PLAN'
    assert provider.data==before and not provider.live_mutations()


def golden(tmp_path,name):
    server,provider=setup(tmp_path,[name],read_only=True)
    fixture=h.load_contract_fixture(FIXTURES/(name+'.json'))
    provider.data[h.CUSTOMER_ID]={**provider.data[h.CUSTOMER_ID],**deepcopy(fixture['gaql'])}
    assert h.call(server,name,fixture['args'])==fixture['golden']
    assert_queries(provider.searches)
    assert not provider.mutations


def break_audit(path):
    path=Path(path)
    if path.exists() and not path.is_dir():
        path.rename(path.with_suffix('.preserved'))
    path.mkdir(exist_ok=True)


def safety(tmp_path,tool,arguments,case):
    clock=h.FakeClock();server,provider=setup(tmp_path,[tool],clock=clock)
    if case=='stage_audit':
        break_audit(h.audit_file(tmp_path))
        assert refusal(h.call(server,tool,arguments),provider)['code']=='AUDIT_WRITE_FAILED'
        return
    plan=stage(server,tool,arguments)
    original_state=deepcopy(provider.data)
    if case=='preview':
        assert refusal(apply(server,plan),provider)['code']=='DRY_RUN_REQUIRED'
        preview(server,plan)
        assert not provider.live_mutations()
    elif case=='expiry':
        clock.advance(901)
        assert refusal(apply(server,plan),provider)['code']=='PLAN_EXPIRED'
    else:
        preview(server,plan)
        if case=='pre_audit':
            break_audit(h.audit_file(tmp_path))
            assert refusal(apply(server,plan),provider)['code']=='AUDIT_WRITE_FAILED'
            return
        if case=='lost_response':
            provider.lose_response=True
        if case=='provider_rejection':
            provider.reject_write=True
        if case=='terminal_audit':
            provider.after_write=lambda:break_audit(h.audit_file(tmp_path))
        if case=='concurrent':
            with ThreadPoolExecutor(max_workers=2) as pool:
                results=list(pool.map(lambda _:apply(server,plan),range(2)))
            assert sum(result.get('applied') is True for result in results)==1
            assert sorted(h.error_of(r)['code'] for r in results if 'error' in r)==['PLAN_CONSUMED']
        else:
            result=apply(server,plan)
            if case in ('lost_response','provider_rejection'):
                assert h.error_of(result)['code']!='INTERNAL'
            else:
                assert h.expect_ok(result)['applied'] is True
                if case=='terminal_audit':
                    assert result.get('audit_warning')
        assert len(provider.live_mutations())==1
        if case=='provider_rejection':
            assert provider.data==original_state, 'An atomic rejected request cannot partially change resources'
        assert h.error_of(apply(server,plan))['code']=='PLAN_CONSUMED'
        assert len(provider.live_mutations())==1
    if case not in ('stage_audit','pre_audit','terminal_audit'):
        records=h.read_audit_records(tmp_path)
        assert any(r.get('plan_id')==plan['id'] for r in records)
        h.assert_no_secrets(json.dumps(records))


INSTALLED_INJECTION = r'''
import atexit,json,os,sys,threading,warnings
from pathlib import Path
sys.path.insert(0,os.environ['BOUNDARY_TESTS'])
import harness as h
from shared_targeting_oracle import TargetingClient,corrupt_field,break_audit
from google.ads.googleads.client import GoogleAdsClient
from google.protobuf.json_format import MessageToDict
marker=Path(os.environ['BOUNDARY_MARKER']);control=Path(os.environ['BOUNDARY_CONTROL'])
lock=threading.Lock()
def mark(event,**values):
    with lock,marker.open('a') as stream:
        stream.write(json.dumps({'event':event,**values})+'\n')
mark('loaded',integer_limit=sys.get_int_max_str_digits())
def deny(event,args):
    if event=='socket.bind' and args[1]==('::1',0):
        raise OSError('offline capability probe')
    if event in ('socket.connect','socket.bind','socket.getaddrinfo'):
        mark('network attempted')
        raise OSError('offline targeting contract')
sys.addaudithook(deny)
class Observed(TargetingClient):
    def _do_search(self,service,method,args,kwargs):
        mode=json.loads(control.read_text())
        if mode.get('corruption'):
            corrupt_field(self,*mode['corruption'])
        if mode.get('drift'):
            self.data[h.CUSTOMER_ID]['campaign'][0]['campaign']['name']='Changed campaign'
        if mode.get('unrelated_warning'):
            warnings.warn('Synthetic unrelated diagnostic',UserWarning)
        mark('targeting query',customer_id=str(h._req_field(args,kwargs,'customer_id')),query=str(h._req_field(args,kwargs,'query')))
        return super()._do_search(service,method,args,kwargs)
    def _do_mutation(self,service,method,args,kwargs):
        mode=json.loads(control.read_text())
        self.lose_response=mode.get('uncertain',False)
        if mode.get('terminal_audit'):
            self.after_write=lambda:break_audit(os.environ['ADS_MCP_AUDIT_LOG'])
        try:
            return super()._do_mutation(service,method,args,kwargs)
        finally:
            call=self.mutations[-1]
            mark('targeting mutation',service=service,method=method,proto=call.request._pb.DESCRIPTOR.name,
                 request=MessageToDict(call.request._pb,preserving_proto_field_name=True),validate_only=call.validate_only)
transport=Observed()
mark('initial state',data=transport.data)
def factory(*args,**kwargs):
    return transport
GoogleAdsClient.load_from_dict=factory
def finished():
    import ads_mcp.server
    mark('final state',data=transport.data)
    mark('finished',module_file=ads_mcp.server.__file__,integer_limit=sys.get_int_max_str_digits())
atexit.register(finished)
'''
