"""Synthetic network state and console provider transport for contract tests."""
from copy import deepcopy
from pathlib import Path

import harness as h
from search_url_oracle import SearchClient

ROOT = Path(__file__).resolve().parents[1]
FIELDS = ('target_google_search', 'target_search_network',
          'target_partner_search_network', 'target_content_network')
DEFAULTS = dict(zip(FIELDS, (True, False, False, False)))
TOOL = 'set_campaign_networks'
NEW_SOURCE = ['tests/test_campaign_networks_contract.py',
              'tests/test_mutation_receipts_contract.py',
              'tests/campaign_networks_oracle.py',
              'tests/mutation_receipts_oracle.py',
              'tests/mutation_response_oracle.py',
              'tests/test_mutation_response_oracle_controls.py',
              'docs/campaign-networks.md']


class NetworkClient(SearchClient):
    def __init__(self):
        super().__init__()
        for account, data in self.data.items():
            for row in data['campaign']:
                row['campaign']['network_settings'] = deepcopy(DEFAULTS)
                row['campaign']['advertising_channel_sub_type'] = 'UNSPECIFIED'
        self.responses = []
        self.response_fault = None

    def _do_mutation(self, service, method, args, kwargs):
        response = super()._do_mutation(service, method, args, kwargs)
        self.responses.append(deepcopy(response))
        if self.response_fault:
            from mutation_response_oracle import corrupt_response
            return corrupt_response(response, self.response_fault)
        return response


def require(server):
    assert TOOL in h.tool_names(server), 'The approved guarded network update tool is missing'


def setup(tmp_path, *, read_only=False, clock=None, plan_store=None, env=None):
    provider = NetworkClient()
    builder = h.build_server if read_only else h.build_rw_server
    server = builder(tmp_path, client=provider, clock=clock, plan_store=plan_store,
                     env={'ADS_MCP_REQUIRE_DRY_RUN': 'true', **(env or {})})
    return server, provider


def campaign(provider, account=h.CUSTOMER_ID):
    return provider.data[account]['campaign'][0]['campaign']


def stage(server, values=None):
    require(server)
    return h.expect_ok(h.call(server, TOOL, {'campaign_id': '701', **(values or {'target_content_network': True})}))['plan']


def preview(server, plan):
    result = h.expect_ok(h.call(server, 'confirm_and_apply', {'plan_id': plan['id'], 'dry_run': True}))
    assert result['applied'] is False
    assert not result.get('created')
    return result


def apply(server, plan):
    return h.call(server, 'confirm_and_apply', {'plan_id': plan['id'], 'dry_run': False, 'confirm_irreversible': True})


def reject(server, provider, name, args, *, local=False):
    before = len(provider.searches)
    result = h.call_result(server, name, args)
    text = h.result_text(result)
    assert 'unknown tool' not in text.lower() and 'Traceback' not in text and 'INTERNAL' not in text
    if not result.is_error:
        assert h.error_of(h.payload_of(result))['code']
    assert not provider.mutations
    if local:
        assert len(provider.searches) == before
    h.assert_no_secrets(text)


INJECTION = r'''
import atexit
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import sys
import ads_mcp
import ads_mcp.server
from google.ads.googleads.client import GoogleAdsClient
from google.protobuf.json_format import MessageToDict
sys.path.insert(0, os.environ['BOUNDARY_TESTS'])
import harness as h
from campaign_networks_oracle import NetworkClient
from mutation_response_oracle import response_names
control = Path(os.environ['BOUNDARY_CONTROL'])
marker = Path(os.environ['BOUNDARY_MARKER'])
def mark(event, **fields):
    with marker.open('a') as stream:
        stream.write(json.dumps({'event': event, **fields}) + '\n')
mark('loaded', integer_limit=sys.get_int_max_str_digits())
def deny(event, args):
    if event == 'socket.bind' and args[1] == ('::1', 0):
        raise OSError('Offline IPv6 probe')
    if event in ('socket.connect', 'socket.bind', 'socket.getaddrinfo'):
        mark('network attempted')
        raise OSError('Synthetic provider forbids network')
sys.addaudithook(deny)
class Observed(NetworkClient):
    def _do_search(self, service, method, args, kwargs):
        mode = json.loads(control.read_text())
        for customer, fields in mode.get('campaigns', {}).items():
            self.data[customer]['campaign'][0]['campaign'].update(fields)
        return super()._do_search(service, method, args, kwargs)
    def _do_mutation(self, service, method, args, kwargs):
        self.response_fault = json.loads(control.read_text()).get('receipt_fault')
        response = super()._do_mutation(service, method, args, kwargs)
        call = self.mutations[-1]
        mark('mutation', service=service, method=method, validate_only=call.validate_only,
             request=MessageToDict(call.request._pb, preserving_proto_field_name=True),
             response_names=response_names(response))
        return response
transport = Observed()
def factory(*args, **kwargs): return transport
GoogleAdsClient.load_from_dict = factory
def finished():
    package = Path(ads_mcp.__file__).resolve().parent
    files = {str(path.relative_to(package)): hashlib.sha256(path.read_bytes()).hexdigest()
             for path in package.rglob('*.py')}
    mark('binding', package=str(package), prefix=sys.prefix, files=files, executable=sys.argv[0])
    mark('finished', module_file=ads_mcp.server.__file__, integer_limit=sys.get_int_max_str_digits())
atexit.register(finished)
'''


def assert_console_binding(console):
    binding, = console.events('binding')
    package = Path(binding['package'])
    assert package != ROOT / 'ads_mcp', 'Console oracle must execute a noneditable installation'
    assert Path(binding['prefix']) in package.parents
    assert Path(binding['executable']).resolve() == h.console_script().resolve()
    import hashlib
    expected = {str(p.relative_to(ROOT / 'ads_mcp')): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in (ROOT / 'ads_mcp').rglob('*.py')}
    assert binding['files'] == expected, 'Installed console differs from the tree under test'
    assert not console.stderr
