"""Synthetic experiment transport and independently authored public contracts.

Only Google service calls are substituted. Rows, requests, operation futures,
metadata, Empty results and async-error pagers use the actual v25 SDK types.
"""
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import re
from types import SimpleNamespace

from google.api_core.exceptions import ServiceUnavailable
from google.api_core.operation import Operation
from google.longrunning import operations_pb2
from google.protobuf import empty_pb2
from google.protobuf.json_format import MessageToDict, ParseDict
from google.rpc import status_pb2
from google.ads.googleads.v25.services.types import PromoteExperimentMetadata
from google.ads.googleads.v25.services.services.experiment_service.pagers import ListExperimentAsyncErrorsPager

import harness as h
from offline_contract import project, selected

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT/'tests/fixtures/pmax_experiments'
FACTS = ROOT/'tests/fixtures/pmax_experiment_provider_v25.json'
INSPECTION_READS = frozenset({'list_pmax_url_experiments','get_pmax_url_experiment','get_pmax_url_experiment_results'})
OPERATION_READ = 'get_pmax_url_experiment_operation'
READS = INSPECTION_READS | {OPERATION_READ}
CREATE = 'create_pmax_url_experiment'
END = 'end_pmax_url_experiment'
PROMOTE = 'promote_pmax_url_experiment'
WRITES = frozenset({CREATE,END,PROMOTE})
ADDITIONS = READS | WRITES
TYPE = 'PMAX_TEXT_CUSTOMIZATION_FINAL_URL_EXPANSION'
EXPANSION = 'FINAL_URL_EXPANSION_TEXT_ASSET_AUTOMATION'
TEXT = 'TEXT_ASSET_AUTOMATION'
PRIVATE = 'synthetic-provider-detail-do-not-disclose'
HANDLE = 'opaque-region/operations/url-trial-A:7'
NOW = datetime(2026,9,16,0,30,tzinfo=timezone.utc).timestamp()
TODAY = '2026-09-15'  # America/Denver, deliberately different from UTC date.
SIGNATURES = {
 'list_pmax_url_experiments': (['customer_id'],[]),
 'get_pmax_url_experiment': (['experiment_id','customer_id'],['experiment_id']),
 'get_pmax_url_experiment_results': (['experiment_id','date_start','date_end','customer_id'],['experiment_id','date_start','date_end']),
 CREATE: (['campaign_id','name','date_start','date_end','customer_id'],['campaign_id','name','date_start','date_end']),
 END: (['experiment_id','customer_id'],['experiment_id']),
 PROMOTE: (['experiment_id','customer_id'],['experiment_id']),
 OPERATION_READ: (['experiment_id','operation_name','customer_id'],['experiment_id','operation_name']),
}
CREATE_ARGS = {'campaign_id':'703','name':'Autumn landing trial','date_start':TODAY,'date_end':'2026-10-15'}
RESULT_ARGS = {'experiment_id':'301','date_start':'2026-09-01','date_end':'2026-09-15'}
ARGS = {'list_pmax_url_experiments':{},'get_pmax_url_experiment':{'experiment_id':'301'},
 'get_pmax_url_experiment_results':RESULT_ARGS,CREATE:CREATE_ARGS,END:{'experiment_id':'301'},
 PROMOTE:{'experiment_id':'301'},OPERATION_READ:{'experiment_id':'301','operation_name':HANDLE}}
BAD_IDS = [None,True,301,1.5,[],{},'', 'null','{}','[]','0','-1','+1','01',' 301','301 ', '3e2','٣٠١','9223372036854775808','9'*5000,'301 OR 1=1','\ud800']
BAD_CUSTOMERS = [True,9876543210,[],{},'', 'null',' 9876543210','9876543210 ','9876543210 OR 1=1','\ud800']
METRIC_NAMES = ['clicks','control_clicks','impressions','control_impressions','cost_micros','control_cost_micros',
 'conversions','control_conversions','conversions_value','control_conversion_value','clicks_point_estimate',
 'clicks_margin_of_error','clicks_p_value','conversions_absolute_change_point_estimate',
 'conversions_absolute_change_margin_of_error','conversions_absolute_change_p_value']
NEW_SOURCE = ['tests/pmax_experiment_oracle.py','tests/test_pmax_experiment_oracle_controls.py',
 *['tests/test_pmax_experiment_'+name+'_contract.py' for name in ('reads','create','lifecycle','workflow')],
 'tests/fixtures/pmax_experiment_provider_v25.json','docs/pmax-experiments.md',
 *['tests/fixtures/pmax_experiments/'+name+'.json' for name in sorted(READS)]]


def rn(kind, ident, customer=h.CUSTOMER_ID):
    return f'customers/{customer}/{kind}/{ident}'


def account(customer=h.CUSTOMER_ID):
    return {'id':int(customer),'resource_name':f'customers/{customer}',
            'currency_code':'EUR' if customer==h.OTHER_CUSTOMER_ID else 'USD','time_zone':'America/Denver'}


def settings(enabled=False):
    return [{'asset_automation_type':TEXT,'asset_automation_status':'OPTED_IN'},
            {'asset_automation_type':EXPANSION,'asset_automation_status':'OPTED_IN' if enabled else 'OPTED_OUT'},
            {'asset_automation_type':'GENERATE_IMAGE_ENHANCEMENT','asset_automation_status':'OPTED_OUT'}]


def campaign(ident=701, customer=h.CUSTOMER_ID, **extra):
    return {'customer':account(customer),'campaign':{'id':ident,'resource_name':rn('campaigns',ident,customer),
        'name':f'Synthetic retail {ident}','status':'ENABLED','advertising_channel_type':'PERFORMANCE_MAX',
        'start_date_time':'2020-01-01 00:00:00','end_date_time':'2037-12-31 23:59:59','asset_automation_settings':settings(), **extra}}


def experiment(ident=301, customer=h.CUSTOMER_ID, **extra):
    return {'customer':account(customer),'experiment':{'experiment_id':ident,
        'resource_name':rn('experiments',ident,customer),'name':f'Synthetic URL trial {ident}','type_':TYPE,
        'status':'ENABLED','start_date':'2026-09-01','end_date':'2026-10-31','promote_status':'NOT_STARTED',
        'long_running_operation':HANDLE if ident==301 else 'opaque/operations/sibling', **extra}}


def arm(experiment_id=301, ident=1, campaign_id=701, customer=h.CUSTOMER_ID, **extra):
    return {'customer':account(customer),'experiment':experiment(experiment_id,customer)['experiment'],
        'experiment_arm':{'resource_name':rn('experimentArms',f'{experiment_id}~{ident}',customer),
        'experiment':rn('experiments',experiment_id,customer),'name':'Control' if ident==1 else 'Treatment',
        'control':ident==1,'traffic_split':50,'campaigns':[rn('campaigns',campaign_id,customer)], **extra}}


def metric_row(ident=301, customer=h.CUSTOMER_ID):
    return {**experiment(ident,customer),'metrics':{'clicks':0,'control_clicks':11,'impressions':200,
        'control_impressions':210,'cost_micros':9007199254740993,'control_cost_micros':0,
        'conversions':-0.5,'control_conversions':1.25,'conversions_value':-7.5,'control_conversion_value':2.0,
        'clicks_point_estimate':-0.2,'clicks_margin_of_error':0.1,'clicks_p_value':1.0,
        'conversions_absolute_change_point_estimate':-1.75,
        'conversions_absolute_change_margin_of_error':0.25,'conversions_absolute_change_p_value':0.0}}


def standard_data(customer):
    return {'customer':[{'customer':account(customer)}],
        'campaign':[campaign(i,customer) for i in (701,702,703)],
        'experiment':[experiment(301,customer),experiment(302,customer,status='HALTED')],
        'experiment_arm':[arm(e,a,c,customer) for e,c in ((301,701),(302,702)) for a in (1,2)],
        'report':[metric_row(301,customer),metric_row(302,customer)],
        'untouched':{'budget_micros':81000000,'exclusions':['https://example.invalid/archive'],'asset_groups':['801','802']}}


def value_at(row, field):
    for part in field.split('.'):
        if not isinstance(row,dict): return None
        row=row.get(part,row.get(part+'_'))
    return row


def assert_queries(calls):
    facts=json.loads(FACTS.read_text())
    for call in calls:
        query=call.query; resource=h._FROM_RE.search(query)[1]
        assert resource in facts['from_resources'], resource
        where=re.search(r'\bWHERE\b(.*?)(?:\bORDER BY\b|\bLIMIT\b|$)',query,re.I|re.S)
        order=re.search(r'\bORDER BY\b(.*?)(?:\bLIMIT\b|$)',query,re.I|re.S)
        uses=[(f,'selectable') for f in selected(query)]
        for clause,permission in ((where,'filterable'),(order,'sortable')):
            if clause:
                text=re.sub(r"'[^']*'|\"[^\"]*\"",'',clause[1])
                uses.extend((f,permission) for f in re.findall(r'\b[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+\b',text))
        for field,permission in uses:
            assert field.split('.')[0] in facts['from_resources'][resource], (resource,field)
            assert field in facts['fields'] and facts['fields'][field][permission], (field,permission)
        if any(f.startswith('metrics.') for f in selected(query)):
            assert resource=='experiment' and where, query
            assert re.search(r'experiment\.(?:experiment_id|resource_name)\s*=',where[1]), query
            assert re.search(r"segments.date\s+BETWEEN\s+'\d{4}-\d{2}-\d{2}'\s+AND\s+'\d{4}-\d{2}-\d{2}'",where[1],re.I) or (
                re.search(r"segments.date\s*>=\s*'\d{4}-\d{2}-\d{2}'",where[1]) and re.search(r"segments.date\s*<=\s*'\d{4}-\d{2}-\d{2}'",where[1])),query
            assert not any(f.startswith('segments.') for f in selected(query)), 'Report must remain one aggregate row'


def raw_operation(*, customer=h.CUSTOMER_ID, done=False, result=None, code=13,
                  name=HANDLE, metadata='valid', experiment_id=301):
    operation=operations_pb2.Operation(name=name,done=done)
    if metadata=='valid': operation.metadata.Pack(PromoteExperimentMetadata(experiment=rn('experiments',experiment_id,customer))._pb)
    elif metadata=='wrong_type': operation.metadata.Pack(empty_pb2.Empty())
    elif metadata=='malformed':
        operation.metadata.type_url='type.googleapis.com/google.ads.googleads.v25.services.PromoteExperimentMetadata'
        operation.metadata.value=b'\xff'
    if result=='response': operation.response.Pack(empty_pb2.Empty())
    elif result=='wrong_response': operation.response.Pack(status_pb2.Status(code=1,message=PRIVATE))
    elif result=='error': operation.error.CopyFrom(status_pb2.Status(code=code,message=PRIVATE))
    return operation


def future(operation):
    def no_poll(*args,**kwargs): raise AssertionError('SDK future must be inspected without hidden polling or waiting')
    return Operation(operation,no_poll,no_poll,empty_pb2.Empty,metadata_type=PromoteExperimentMetadata)


class ExperimentService:
    def __init__(self,owner):
        self.owner=owner
        self.transport=SimpleNamespace(operations_client=self)
        self._transport=self.transport

    def end_experiment(self,*args,**kwargs): return self.owner.action('end_experiment',args,kwargs)
    def promote_experiment(self,*args,**kwargs): return self.owner.action('promote_experiment',args,kwargs)

    def get_operation(self,*args,**kwargs):
        owner=self.owner
        name=kwargs.get('name',args[0] if args else '')
        owner.polls.append({'name':name,**kwargs,'login_customer_id':owner.login_customer_id})
        assert kwargs.get('retry','unset') is None
        assert isinstance(kwargs.get('timeout'),(float,int)) and 0<kwargs['timeout']<=120
        if owner.poll_failures:
            owner.poll_failures-=1
            raise ServiceUnavailable(PRIVATE)
        return deepcopy(owner.operation)

    def list_experiment_async_errors(self,*args,**kwargs):
        owner=self.owner
        request=kwargs.get('request',args[0] if args else None)
        assert request is not None and request._pb.DESCRIPTOR.name=='ListExperimentAsyncErrorsRequest'
        assert request.page_size==100 and kwargs.get('retry','unset') is None
        def page(request,**paging):
            assert paging.get('retry') is None
            owner.error_calls.append(deepcopy(request))
            if owner.detail_failure: raise ServiceUnavailable(PRIVATE)
            number=int(request.page_token or '0')
            result=h.get_ads_type('ListExperimentAsyncErrorsResponse')
            raw=owner.error_pages[min(number,len(owner.error_pages)-1)]
            ParseDict(raw,result._pb)
            return result
        return ListExperimentAsyncErrorsPager(page,request,page(request),metadata=(),retry=None,timeout=kwargs.get('timeout'))


class ExperimentClient(h.FakeGoogleAdsClient):
    def __init__(self):
        super().__init__()
        self.login_customer_id=h.LOGIN_CUSTOMER_ID
        self.data={cid:standard_data(cid) for cid in (h.CUSTOMER_ID,h.OTHER_CUSTOMER_ID)}
        self.corrupt={};self.pulls=Counter();self.fail_after={}
        self.actions=[];self.polls=[];self.error_calls=[];self.poll_failures=0
        self.operation=raw_operation();self.promote_response=None
        self.error_pages=[{'errors':[{'code':13,'message':PRIVATE}]}]
        self.detail_failure=False;self.reject_validation=False;self.lose_response=False
        self.receipt_fault=None;self.after_action=None
        self.experiment_service=ExperimentService(self)

    def get_service(self,name,version=None):
        if name=='ExperimentService': return self.experiment_service
        return super().get_service(name,version)

    def _do_search(self,service,method,args,kwargs):
        self._responses={}
        super()._do_search(service,method,args,kwargs)
        call=self.searches[-1];assert_queries([call])
        resource=h._FROM_RE.search(call.query)[1]
        report=any(f.startswith('metrics.') for f in selected(call.query))
        key='report' if report else resource
        data=self.data.get(call.customer_id,{})
        rows=deepcopy(data.get(key,[]))
        for row in rows:
            row.setdefault('customer',deepcopy(data['customer'][0]['customer']))
            if resource=='experiment_arm':
                entity=row['experiment_arm']
                parent=next((r['experiment'] for r in data['experiment'] if r['experiment']['resource_name']==entity['experiment']),{})
                row['experiment']=deepcopy(parent)
        where=re.search(r'\bWHERE\b(.*?)(?:\bORDER BY\b|\bLIMIT\b|$)',call.query,re.I|re.S)
        if where:
            for field,operator,literal in re.findall(r"([a-z][a-z0-9_.]+)\s*(NOT\s+IN|IN|!=|=|CONTAINS\s+ANY)\s*(\([^)]*\)|'[^']*'|\"[^\"]*\"|[a-zA-Z_0-9]+)",where[1],re.I):
                wanted={next(x for x in item if x) for item in re.findall(r"'([^']*)'|\"([^\"]*)\"|([\w]+)",literal)}
                def matched(row):
                    value=value_at(row,field)
                    return bool(set(map(str,value))&wanted) if isinstance(value,list) else str(value) in wanted
                rows=[r for r in rows if matched(r)!=(operator.upper() in ('!=','NOT IN'))]
        if key in self.corrupt: rows=self.corrupt[key](rows)
        limit=re.search(r'\bLIMIT\s+(\d+)',call.query,re.I)
        if limit: rows=rows[:int(limit[1])]
        def stream():
            for index,row in enumerate(rows):
                if index==self.fail_after.get(key): raise ServiceUnavailable(PRIVATE)
                self.pulls[key]+=1
                yield project(h.make_row(row),selected(call.query))
            if len(rows)==self.fail_after.get(key): raise ServiceUnavailable(PRIVATE)
        return stream()

    def _do_mutation(self,service,method,args,kwargs):
        request=kwargs.get('request',args[0] if args else None)
        assert service=='GoogleAdsService' and method=='mutate'
        assert request is not None and request._pb.DESCRIPTOR.name=='MutateGoogleAdsRequest'
        self.mutations.append(h.MutateCall(service,method,deepcopy(request),kwargs,bool(request.validate_only)))
        assert kwargs.get('retry','unset') is None, 'Mutations must explicitly disable SDK retries'
        assert not request.partial_failure
        if request.validate_only and self.reject_validation: raise h.make_google_ads_exception([PRIVATE])
        response=h.get_ads_type('MutateGoogleAdsResponse')
        if request.validate_only:return response
        for index,operation in enumerate(request.mutate_operations):
            field=operation._pb.WhichOneof('operation');kind=field.removesuffix('_operation')
            assert kind in ('experiment','experiment_arm','campaign')
            item=h.get_ads_type('MutateOperationResponse')
            ident=rn('experiments',901,request.customer_id) if kind=='experiment' else (
                rn('experimentArms',f'901~{index}',request.customer_id) if kind=='experiment_arm' else operation.campaign_operation.update.resource_name)
            getattr(item,kind+'_result').resource_name=ident
            response.mutate_operation_responses.append(item)
        if not request.validate_only:
            data=self.data[request.customer_id]
            for index,operation in enumerate(request.mutate_operations):
                field=operation._pb.WhichOneof('operation')
                if field=='experiment_operation':
                    raw=MessageToDict(operation.experiment_operation.create._pb,preserving_proto_field_name=True)
                    raw.update(resource_name=rn('experiments',901,request.customer_id),experiment_id=901,status='SETUP',promote_status='NOT_STARTED')
                    data['experiment'].append({'experiment':raw})
                elif field=='experiment_arm_operation':
                    raw=MessageToDict(operation.experiment_arm_operation.create._pb,preserving_proto_field_name=True)
                    raw.update(resource_name=rn('experimentArms',f'901~{index}',request.customer_id),experiment=rn('experiments',901,request.customer_id))
                    raw.setdefault('control',False)
                    data['experiment_arm'].append({'experiment_arm':raw})
                else:
                    update=operation.campaign_operation.update
                    target=next(r['campaign'] for r in data['campaign'] if r['campaign']['resource_name']==update.resource_name)
                    target['asset_automation_settings']=MessageToDict(update._pb,preserving_proto_field_name=True)['asset_automation_settings']
            if self.after_action: self.after_action()
            if self.lose_response: raise ServiceUnavailable(PRIVATE)
            if self.receipt_fault: response=self.receipt_fault(response)
        return response

    def action(self,method,args,kwargs):
        request=kwargs.get('request',args[0] if args else None)
        expected='EndExperimentRequest' if method=='end_experiment' else 'PromoteExperimentRequest'
        assert request is not None and request._pb.DESCRIPTOR.name==expected
        assert kwargs.get('retry','unset') is None
        self.actions.append({'method':method,'request':deepcopy(request),'kwargs':kwargs})
        if request.validate_only:
            if self.reject_validation: raise h.make_google_ads_exception([PRIVATE])
            return None if method=='end_experiment' else future(raw_operation())
        identity=request.experiment if method=='end_experiment' else request.resource_name
        customer=identity.split('/')[1]
        entity=next(r['experiment'] for r in self.data[customer]['experiment'] if r['experiment']['resource_name']==identity)
        if method=='end_experiment': entity['end_date']=TODAY  # Does not fabricate HALTED.
        else: entity['long_running_operation']=self.operation.name;entity['promote_status']='IN_PROGRESS'
        if self.after_action: self.after_action()
        if self.lose_response: raise ServiceUnavailable(PRIVATE)
        return None if method=='end_experiment' else future(self.promote_response or self.operation)


def require(server,names):
    missing=set(names)-h.tool_names(server)
    assert not missing,'Missing approved experiment behavior: '+', '.join(sorted(missing))


def setup(tmp_path,names,*,read_only=False,clock=None,env=None):
    client=ExperimentClient()
    server=(h.build_server if read_only else h.build_rw_server)(tmp_path,client=client,
        clock=clock or h.FakeClock(NOW),env={'ADS_MCP_REQUIRE_DRY_RUN':'true',**(env or {})})
    require(server,names)
    return server,client


def quiet(payload):
    text=json.dumps(payload,ensure_ascii=True)
    h.assert_no_secrets(text)
    assert PRIVATE not in text and 'Traceback' not in text and len(text)<100000


def rejected(server,client,tool,args,*,local=False,code=None):
    count=len(client.searches);before=deepcopy(client.data)
    result=h.call_result(server,tool,args)
    text=h.result_text(result)
    assert 'unknown tool' not in text.lower() and 'Traceback' not in text and PRIVATE not in text
    h.assert_no_secrets(text)
    if not result.is_error:
        error=h.error_of(h.payload_of(result));assert error['code']!='INTERNAL'
        if code: assert error['code']==code
    assert not client.live_mutations() and not [c for c in client.actions if not c['request'].validate_only]
    assert client.data==before
    if local: assert len(client.searches)==count and not client.mutations and not client.actions and not client.polls
    return result


def stage(server,tool=CREATE,args=None):
    payload=h.expect_ok(h.call(server,tool,args or ARGS[tool]));plan=payload['plan']
    assert plan['tool']==tool and plan['operations']
    assert plan['irreversible'] is (tool in (END,PROMOTE))
    return plan


def preview(server,plan):
    result=h.expect_ok(h.call(server,'confirm_and_apply',{'plan_id':plan['id'],'dry_run':True}))
    assert result['applied'] is False
    return result


def apply(server,plan,*,ack=True):
    return h.call(server,'confirm_and_apply',{'plan_id':plan['id'],'dry_run':False,'confirm_irreversible':ack})


def live_calls(client):
    return client.live_mutations()+[c for c in client.actions if not c['request'].validate_only]


def break_audit(path):
    path.unlink(missing_ok=True);path.mkdir()


def complete_promotion(client,customer=h.CUSTOMER_ID,status='COMPLETED'):
    client.operation=raw_operation(customer=customer,done=True,result='response')
    client.data[customer]['experiment'][0]['experiment']['promote_status']=status
    client.data[customer]['campaign'][0]['campaign']['asset_automation_settings']=settings(True)


INSTALLED_INJECTION = r'''
import atexit,json,os,sys,threading
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
sys.path.insert(0,os.environ['BOUNDARY_TESTS'])
import harness as h
from pmax_experiment_oracle import ExperimentClient,raw_operation,rn,metric_row,settings,break_audit
from google.ads.googleads.client import GoogleAdsClient
from google.protobuf.json_format import MessageToDict
marker=Path(os.environ['BOUNDARY_MARKER']);control=Path(os.environ['BOUNDARY_CONTROL']);lock=threading.Lock()
def mark(event,**values):
    with lock,marker.open('a') as stream:stream.write(json.dumps({'event':event,**values})+'\n')
mark('loaded',integer_limit=sys.get_int_max_str_digits())
def deny(event,args):
    if event=='socket.bind' and args[1]==('::1',0):raise OSError('offline capability probe')
    if event in ('socket.connect','socket.bind','socket.getaddrinfo'):
        mark('network attempted');raise OSError('offline experiment contract')
sys.addaudithook(deny)
class Observed(ExperimentClient):
    def configure(self):
        mode=json.loads(control.read_text());customer=os.environ['GOOGLE_ADS_CUSTOMER_ID'];data=self.data[customer]
        self.lose_response=mode.get('uncertain',False);self.reject_validation=mode.get('reject_validation',False)
        if mode.get('activate_created'):
            for row in data['experiment']:
                if row['experiment']['experiment_id']==901:row['experiment']['status']='ENABLED'
            if not any(r['experiment']['experiment_id']==901 for r in data['report']):data['report'].append(metric_row(901,customer))
        if mode.get('drift'):data['experiment'][0]['experiment']['name']='Changed experiment'
        if mode.get('terminal_audit'):self.after_action=lambda:break_audit(Path(os.environ['ADS_MCP_AUDIT_LOG']))
        if mode.get('readback_failure'):self.after_action=lambda:self.fail_after.update(experiment=0)
        if mode.get('complete'):
            ident=mode.get('experiment_id',301);self.operation=raw_operation(customer=customer,experiment_id=ident,done=True,result='response')
            entity=next(r['experiment'] for r in data['experiment'] if r['experiment']['experiment_id']==ident)
            entity['promote_status']='COMPLETED';entity['long_running_operation']=self.operation.name
            parent=next(r['experiment_arm']['campaigns'][0] for r in data['experiment_arm'] if r['experiment_arm']['experiment']==entity['resource_name'])
            next(r['campaign'] for r in data['campaign'] if r['campaign']['resource_name']==parent)['asset_automation_settings']=settings(True)
        if 'promotion_status' in mode:
            data['experiment'][0]['experiment']['promote_status']=mode['promotion_status']
            data['campaign'][0]['campaign']['asset_automation_settings']=settings(True)
            self.operation=raw_operation(customer=customer,done=True,result='response')
        if mode.get('operation_case'):
            case=mode['operation_case']
            self.operation=raw_operation(customer=(h.OTHER_CUSTOMER_ID if customer==h.CUSTOMER_ID else h.CUSTOMER_ID) if case=='foreign' else customer,
                done=case=='failed',result='error' if case=='failed' else None,metadata='wrong_type' if case=='wrong_metadata' else 'valid')
        return mode
    def _do_search(self,service,method,args,kwargs):
        self.configure()
        mark('experiment query',customer_id=str(h._req_field(args,kwargs,'customer_id')),query=str(h._req_field(args,kwargs,'query')))
        return super()._do_search(service,method,args,kwargs)
    def _do_mutation(self,service,method,args,kwargs):
        self.configure()
        try:return super()._do_mutation(service,method,args,kwargs)
        finally:
            call=self.mutations[-1]
            mark('experiment mutation',service=service,method=method,proto=call.request._pb.DESCRIPTOR.name,
                request=MessageToDict(call.request._pb,preserving_proto_field_name=True),validate_only=call.validate_only)
    def action(self,method,args,kwargs):
        mode=self.configure();request=kwargs.get('request',args[0] if args else None)
        identity=request.experiment if method=='end_experiment' else request.resource_name
        customer=identity.split('/')[1];ident=int(identity.split('/')[-1])
        self.operation=raw_operation(customer=customer,experiment_id=ident)
        try:
            result=super().action(method,args,kwargs)
            if method=='end_experiment' and not request.validate_only:
                next(r['experiment'] for r in self.data[customer]['experiment'] if r['experiment']['resource_name']==identity)['end_date']=datetime.now(ZoneInfo('America/Denver')).date().isoformat()
            return result
        finally:mark('experiment action',method=method,proto=request._pb.DESCRIPTOR.name,
            request=MessageToDict(request._pb,preserving_proto_field_name=True),validate_only=bool(request.validate_only))
transport=Observed();mark('initial state',data=transport.data)
def factory(*args,**kwargs):return transport
GoogleAdsClient.load_from_dict=factory
def finished():
    import ads_mcp.server
    mark('final state',data=transport.data)
    mark('finished',module_file=ads_mcp.server.__file__,integer_limit=sys.get_int_max_str_digits())
atexit.register(finished)
'''
