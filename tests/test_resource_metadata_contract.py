"""F088: get_resource_metadata, a GoogleAdsFieldService-backed field catalogue read.

Only the field service is synthetic (tests/harness.py stub, genuine v25
GoogleAdsField messages from fixtures/resource_metadata_fields_v25.json).
Expected payloads are computed from that fixture by resource_metadata_oracle.

Public contract pinned here (the acceptance text leaves these names open):
- Success payloads are ``{"name", "fields", "next_page_token"}``; a resource
  name adds ``attribute_resources``, ``segments`` and ``metrics`` (sorted names,
  identical on every page). ``fields`` holds detail entries sorted by name and
  is the only paginated list; ``next_page_token`` is null on the last page.
- Each detail entry has exactly name, category, data_type, selectable,
  filterable, sortable, is_repeated, enum_values (first 100 in provider order)
  and enum_values_truncated; enum names are their symbolic strings.
- Refusals reuse existing codes: INVALID_ARGUMENT (name rule, too-broad names),
  INVALID_PAGE_SIZE, INVALID_PAGE_TOKEN; unknown names are NOT_FOUND with
  ``error.close_matches`` (at most five names, best first).
- Any cache belongs to one server and its age is measured on that server's
  injected clock.
"""
from copy import deepcopy
import asyncio
import difflib
import json
import re
import socket
import subprocess
import sys
import time

import pytest
from google.api_core.exceptions import DeadlineExceeded, ServiceUnavailable
from google.auth.exceptions import GoogleAuthError, RefreshError
from google.ads.googleads.errors import GoogleAdsException
from google.protobuf.json_format import ParseDict

import harness as h
import resource_metadata_oracle as rm
import test_auth_cause_contract as plumbing
from capability_oracle import DEFAULT, cli, empty, success
from offline_contract import load_script
from test_pmax_experiment_workflow_contract import EXPECTED_ALL, EXPECTED_READS
from tool_catalog import METADATA_TOOLS, READ_TOOLS

FINAL_ALL = EXPECTED_ALL | METADATA_TOOLS
FINAL_READS = EXPECTED_READS | METADATA_TOOLS
FAST_RETRY = {"ADS_MCP_RETRY_BASE_SECONDS": "0.001"}


def server_for(tmp_path, client=None, *, read_only=True, env=None, clock=None):
    tmp_path.mkdir(parents=True, exist_ok=True)
    client = client if client is not None else rm.provider()
    merged = {**FAST_RETRY, **(env or {})}
    if read_only:
        server = h.build_server(tmp_path, client=client, env={"ADS_MCP_AUDIT_LOG": str(h.audit_file(tmp_path)), **merged}, clock=clock)
    else:
        server = h.build_rw_server(tmp_path, client=client, env=merged, clock=clock)
    return server, client


def refused(payload, code, client, start=0):
    error = h.error_of(payload)
    assert error["code"] == code, error
    assert client.field_searches[start:] == [], "a refusal must precede any field-service request"
    assert not client.searches and not client.mutations
    text = json.dumps(payload)
    assert "Traceback" not in text
    h.assert_no_secrets(text)
    return error


def assert_sanitized(message, supplied):
    assert all(ch.isprintable() for ch in message), "control characters echoed into the message"
    if isinstance(supplied, str):
        for start in range(0, max(len(supplied) - 64, 0) + 1):
            window = supplied[start:start + 65]
            if len(window) == 65:
                assert window not in message, "more than 64 characters of input echoed"


def walk(server, name, page_size=None):
    pages, token = [], None
    while True:
        args = {"name": name}
        if page_size is not None:
            args["page_size"] = page_size
        if token is not None:
            args["page_token"] = token
        payload = h.expect_ok(rm.metadata(server, args))
        pages.append(payload)
        token = payload["next_page_token"]
        assert len(pages) <= 200, "pagination did not terminate"
        if token is None:
            return pages
        assert isinstance(token, str) and token


def test_catalog_has_exactly_85_operations_and_35_reads_with_strict_metadata_schema(tmp_path):
    from ads_mcp.tools.registry import all_tool_specs

    assert rm.TOOL in METADATA_TOOLS and rm.TOOL not in READ_TOOLS
    write, _ = server_for(tmp_path / "write", read_only=False)
    names = h.tool_names(write)
    assert rm.TOOL in names, f"{rm.TOOL} is not registered in write mode"
    assert names == FINAL_ALL and len(names) == 85
    readonly, _ = server_for(tmp_path / "read")
    assert h.tool_names(readonly) == FINAL_READS and len(FINAL_READS) == 35
    specs = all_tool_specs()
    assert len(specs) == len({spec.name for spec in specs}) == 85
    assert {spec.name for spec in specs if spec.kind == "read"} == FINAL_READS
    for server in (write, readonly):
        schema = h.tool_map(server)[rm.TOOL].input_schema
        parameters, required = rm.SIGNATURE
        assert set(schema["properties"]) == set(parameters) and "customer_id" not in schema["properties"]
        assert schema.get("required", []) == required
        assert schema.get("additionalProperties") is False


@pytest.mark.parametrize("extra", [
    {"customer_id": h.CUSTOMER_ID}, {"login_customer_id": h.LOGIN_CUSTOMER_ID},
    {"query": "SELECT name WHERE name LIKE '%'"}, {"fields": ["name"]}, {"resource": "campaign"},
])
def test_unexpected_arguments_refused_by_the_strict_schema_before_provider_access(tmp_path, extra):
    server, client = server_for(tmp_path)
    result = rm.call_result(server, {"name": "campaign.status", **extra})
    assert result.is_error, "an undeclared argument must be refused, not ignored"
    text = h.result_text(result)
    assert next(iter(extra)) in text
    for value in extra.values():
        if isinstance(value, str) and len(value) > 3:
            assert value not in text
    assert "Traceback" not in text
    assert client.field_searches == [] and not client.searches


INVALID_NAMES = [
    "", "Campaign", "campaign.Status", "CAMPAIGN", "campaign status", " campaign", "campaign ",
    "campaign.", ".campaign", "campaign..status", "1campaign", "campaign.1status", "_campaign",
    "campaign-status", "campaign.status'", "'campaign'", '"campaign"', "campaign;DROP",
    "campaign.status = 'ENABLED'", "SELECT campaign.id FROM campaign", "campaign.*", "campaign%",
    "campaign.stat_s%", "campaign\n", "campaign\x00", "campaign\t.status", "ċampaign", "campaign​",
    "metrics.clicks)", "segments.date OR 1=1", "x" * 129, ("a." * 64) + "a",
    "campaign." + "z" * 300 + "\x1b[31m",
]


@pytest.mark.parametrize("name", INVALID_NAMES, ids=lambda value: repr(value)[:40])
def test_name_rule_violations_are_stable_sanitized_validation_errors(tmp_path, name):
    server, client = server_for(tmp_path)
    error = refused(rm.metadata(server, {"name": name}), "INVALID_ARGUMENT", client)
    message = error["message"]
    assert "128" in message and ("a-z" in message or "lowercase" in message.lower()), (
        f"the validation error must name the rule: {message!r}")
    assert_sanitized(message, name)
    assert len(message) <= 400
    repeat = refused(rm.metadata(server, {"name": name}), "INVALID_ARGUMENT", client)
    assert repeat == error


@pytest.mark.parametrize("name", ["a" * 128, "a", "a" + ".b" * 63, "campaign.oracle_attribute_1000"])
def test_name_rule_boundaries_are_accepted_and_reach_a_bounded_lookup(tmp_path, name):
    assert rm.passes_name_rule(name) and len(name) <= 128
    server, client = server_for(tmp_path)
    error = h.error_of(rm.metadata(server, {"name": name}))
    assert error["code"] == "NOT_FOUND", error
    rm.assert_bounded_requests(client, 0, [name.split(".")[0]])


@pytest.mark.parametrize("name", ["metrics", "segments"])
def test_bare_metrics_or_segments_is_refused_as_too_broad_with_guidance(tmp_path, name):
    server, client = server_for(tmp_path)
    detail = h.expect_ok(rm.metadata(server, {"name": name + ".clicks" if name == "metrics" else name + ".date"}))
    assert detail["fields"] == rm.expected_fields(detail["name"])
    start = len(client.field_searches)
    error = h.error_of(rm.metadata(server, {"name": name}))
    assert error["code"] == "INVALID_ARGUMENT", error
    assert client.field_searches[start:] == []
    assert "broad" in error["message"].lower()
    assert (name + ".") in error["message"] or "resource" in error["message"].lower()


def test_resource_returns_attribute_details_and_selectable_names_only(tmp_path):
    server, client = server_for(tmp_path)
    first = h.expect_ok(rm.metadata(server, {"name": "campaign"}))
    calls = rm.assert_bounded_requests(client, 0, ["campaign"])
    assert len(calls) >= 2, "the resource row and its attribute fields are separate prefix-limited reads"
    expected = rm.expected_fields("campaign")
    names = rm.expected_names("campaign")
    assert len(expected) == 130 and set(first) == rm.RESOURCE_KEYS
    assert first["name"] == "campaign" and first["fields"] == expected[:100]
    assert {key: first[key] for key in names} == names
    assert names["segments"] and names["metrics"] and names["attribute_resources"]
    rm.assert_detail_shape(first["fields"])
    token = first["next_page_token"]
    assert isinstance(token, str) and token
    start = len(client.field_searches)
    second = h.expect_ok(rm.metadata(server, {"name": "campaign", "page_token": token}))
    assert len(client.field_searches) - start <= rm.MAX_REQUESTS
    assert set(second) == rm.RESOURCE_KEYS and second["next_page_token"] is None
    assert second["fields"] == expected[100:]
    assert {key: second[key] for key in names} == names
    detailed = {entry["name"] for entry in first["fields"] + second["fields"]}
    assert not detailed & set(names["segments"] + names["metrics"]), "segments/metrics are names only"


@pytest.mark.parametrize("name", [
    "campaign.status", "campaign.primary_status_reasons", "campaign.labels", "campaign.url_custom_parameters",
    "metrics.clicks", "segments.date", "ad_group.status", "customer.currency_code",
])
def test_full_field_name_returns_exactly_that_field(tmp_path, name):
    server, client = server_for(tmp_path)
    payload = h.expect_ok(rm.metadata(server, {"name": name}))
    assert payload == {"name": name, "fields": rm.expected_fields(name), "next_page_token": None}
    assert len(payload["fields"]) == 1
    rm.assert_bounded_requests(client, 0, [name.split(".")[0]])


def test_non_leaf_prefix_returns_fields_under_it_at_a_dot_boundary(tmp_path):
    server, client = server_for(tmp_path)
    payload = h.expect_ok(rm.metadata(server, {"name": "campaign.network_settings"}))
    assert payload == json.loads(rm.GOLDEN_PATH.read_text())["golden"]
    assert [entry["name"] for entry in payload["fields"]] == [
        "campaign.network_settings.target_content_network", "campaign.network_settings.target_google_search",
        "campaign.network_settings.target_partner_search_network", "campaign.network_settings.target_search_network"]
    assert "campaign.network_settings_extra" not in json.dumps(payload)
    rm.assert_bounded_requests(client, 0, ["campaign"])


@pytest.mark.parametrize("name,decoy", [("ad_group", "adxgroup"), ("campaign_budget", "campaignxbudget"),
                                        ("ad_group.status", "adxgroup.status")])
def test_prefix_queries_match_underscores_literally(tmp_path, name, decoy):
    server, client = server_for(tmp_path)
    payload = h.expect_ok(rm.metadata(server, {"name": name}))
    assert decoy not in json.dumps(payload)
    assert payload["fields"] == rm.expected_fields(name)
    # The query itself may not reach the decoy family: _ is a LIKE wildcard.
    rm.assert_bounded_requests(client, 0, [name.split(".")[0]])


@pytest.mark.parametrize("name,decoy", [("ad_group", "adxgroup"), ("campaign_budget", "campaignxbudget"),
                                        ("campaign", "campaign_budget"), ("campaign.network_settings", "network_settings_extra")])
def test_results_are_filtered_to_the_exact_prefix_when_the_provider_over_returns(tmp_path, name, decoy):
    client = rm.provider()
    client.field_like_honors_escapes = False
    server, _ = server_for(tmp_path, client)
    pages = walk(server, name)
    entries = [entry for page in pages for entry in page["fields"]]
    assert entries == rm.expected_fields(name)
    assert all(not entry["name"].startswith(decoy) and decoy + "." not in entry["name"] for entry in entries)
    assert len(client.field_searches) <= rm.MAX_REQUESTS * len(pages)


@pytest.mark.parametrize("name,count,truncated", [
    ("campaign.oracle_enum_100", 100, False), ("campaign.oracle_enum_101", 100, True),
    ("campaign.oracle_enum_150", 100, True), ("campaign.status", 5, False), ("campaign.name", 0, False),
])
def test_enum_values_keep_provider_order_and_truncate_after_100(tmp_path, name, count, truncated):
    server, _ = server_for(tmp_path)
    entry = h.expect_ok(rm.metadata(server, {"name": name}))["fields"][0]
    source = rm.by_name()[name].get("enum_values", [])
    assert entry["enum_values"] == source[:100] and len(entry["enum_values"]) == count
    assert entry["enum_values_truncated"] is truncated


def test_unknown_field_returns_not_found_with_close_matches_from_its_own_resource(tmp_path):
    server, client = server_for(tmp_path)
    error = h.error_of(rm.metadata(server, {"name": "ad_group.stauts"}))
    assert error["code"] == "NOT_FOUND", error
    matches = error.get("close_matches")
    assert isinstance(matches, list) and matches[0] == "ad_group.status", error
    assert set(matches) == {"ad_group.status", "ad_group.name", "ad_group", "ad_group.id"}
    assert "adxgroup" not in json.dumps(error), "close matches must come from the same top-level resource"
    assert "ad_group.cpc_bid_micros" not in matches, "ratio below 0.6"
    rm.assert_bounded_requests(client, 0, ["ad_group"])


def test_close_matches_are_capped_at_five_above_the_ratio_cutoff(tmp_path):
    server, client = server_for(tmp_path)
    error = h.error_of(rm.metadata(server, {"name": "campaign.stauts"}))
    assert error["code"] == "NOT_FOUND", error
    matches = error.get("close_matches")
    candidates = rm.close_candidates("campaign.stauts", "campaign")
    assert len(candidates) > 5
    assert isinstance(matches, list) and len(matches) == len(set(matches)) == 5, error
    assert matches[0] == "campaign.status" and set(matches) <= candidates
    assert {"campaign.status", "campaign.start_date", "campaign.serving_status", "campaign.primary_status"} <= set(matches)
    assert set(difflib.get_close_matches("campaign.stauts", sorted(candidates), n=6, cutoff=0.6)) >= set(matches) - {"campaign.labels", "campaign.name"}
    rm.assert_bounded_requests(client, 0, ["campaign"])


@pytest.mark.parametrize("name", ["campain", "campaign.no_such_prefix.leaf", "metrics.zzzzzz"])
def test_unknown_names_without_close_matches_are_not_found(tmp_path, name):
    server, client = server_for(tmp_path)
    error = h.error_of(rm.metadata(server, {"name": name}))
    assert error["code"] == "NOT_FOUND", error
    assert error.get("close_matches", []) == [] or set(error["close_matches"]) <= rm.close_candidates(name, name.split(".")[0])
    assert "campaign_budget" not in json.dumps(error) and "adxgroup" not in json.dumps(error)
    rm.assert_bounded_requests(client, 0, [name.split(".")[0]])


def test_pagination_defaults_to_100_caps_at_1000_and_never_duplicates_or_omits(tmp_path):
    server, client = server_for(tmp_path)
    expected = rm.expected_fields("campaign")
    default = walk(server, "campaign")
    assert [len(page["fields"]) for page in default] == [100, 30]
    for size, shape in ((7, [7] * 18 + [4]), (1, None), (130, [130]), (1000, [130])):
        pages = walk(server, "campaign", size)
        sizes = [len(page["fields"]) for page in pages]
        if shape is not None:
            assert sizes == shape
        else:
            assert sizes == [1] * 130
        names = [entry["name"] for page in pages for entry in page["fields"]]
        assert len(names) == len(set(names)) and [e for p in pages for e in p["fields"]] == expected
    start = len(client.field_searches)
    for bad in (0, 1001, -1, 10**9):
        error = h.error_of(rm.metadata(server, {"name": "campaign", "page_size": bad}))
        assert error["code"] == "INVALID_PAGE_SIZE" and "page_size" in error["message"], error
    for bad in (True, 1.5, "10", [10]):
        assert rm.call_result(server, {"name": "campaign", "page_size": bad}).is_error
    assert client.field_searches[start:] == []


def test_page_tokens_are_opaque_and_bound_to_name_page_size_and_tool(tmp_path):
    from ads_mcp.tools.registry import _RetainedReadMetadata

    server, client = server_for(tmp_path)
    first = h.expect_ok(rm.metadata(server, {"name": "campaign", "page_size": 7}))
    token = first["next_page_token"]
    assert isinstance(token, str) and token
    tool = server._tool_manager.get_tool(rm.TOOL)
    assert not isinstance(tool.fn_metadata, _RetainedReadMetadata), "metadata must not use retained snapshots"
    ok = h.expect_ok(rm.metadata(server, {"name": "campaign", "page_size": 7, "page_token": token}))
    assert ok["fields"] == rm.expected_fields("campaign")[7:14]
    client.stub("campaign", [{"campaign": {"id": n, "name": f"Synthetic {n}"}} for n in range(1, 4)])
    gaql = h.expect_ok(h.call(server, "run_gaql", {"query": "SELECT campaign.id FROM campaign", "page_size": 1}))
    foreign = gaql.get("next_page_token") or gaql.get("page_token")
    assert isinstance(foreign, str) and foreign
    cases = [
        {"name": "ad_group", "page_size": 7, "page_token": token},
        {"name": "campaign", "page_size": 8, "page_token": token},
        {"name": "campaign", "page_token": token},
        {"name": "campaign", "page_size": 7, "page_token": foreign},
        {"name": "campaign", "page_size": 7, "page_token": token[:-2]},
        {"name": "campaign", "page_size": 7, "page_token": token + "A"},
        {"name": "campaign", "page_size": 7, "page_token": ""},
        {"name": "campaign", "page_size": 7, "page_token": "synthetic-DO-NOT-ECHO-" + "x" * 300},
        {"name": "campaign", "page_size": 7, "page_token": "\x00‮" + token},
    ]
    for args in cases:
        error = h.error_of(rm.metadata(server, args))
        assert error["code"] == "INVALID_PAGE_TOKEN", (args, error)
        assert args["page_token"] not in error["message"] or not args["page_token"]
        assert "DO-NOT-ECHO" not in error["message"]
    refusal = h.error_of(h.call(server, "run_gaql", {"query": "SELECT campaign.id FROM campaign",
                                                       "page_size": 1, "page_token": token}))
    assert refusal["code"] == "INVALID_PAGE_TOKEN"


def test_each_call_is_three_prefix_limited_requests_under_one_ten_second_deadline(tmp_path):
    client = rm.provider()
    seen = []

    def slow_first(call):
        seen.append(time.monotonic())
        if len(seen) == 1:
            time.sleep(1.2)

    client.field_hook = slow_first
    server, _ = server_for(tmp_path, client)
    h.expect_ok(rm.metadata(server, {"name": "campaign"}))
    calls = rm.assert_bounded_requests(client, 0, ["campaign"])
    assert len(calls) >= 2
    for call in calls[1:]:
        assert call.timeout <= rm.DEADLINE_SECONDS - 1.15, (
            f"a later request kept a fresh {call.timeout!r} s budget; the deadline covers the whole call")


def test_cache_age_is_bounded_to_one_hour_on_the_server_clock(tmp_path):
    clock = h.FakeClock()
    fields = rm.catalogue()
    client = rm.provider(fields)
    server, _ = server_for(tmp_path, client, clock=clock)
    before = h.expect_ok(rm.metadata(server, {"name": "campaign.status"}))["fields"][0]
    assert before["enum_values"][-1] == "REMOVED"
    changed = deepcopy(fields)
    rm.by_name(changed)["campaign.status"]["enum_values"].append("ORACLE_LATER_VALUE")
    client.stub_fields(changed)
    clock.advance(3601)
    after = h.expect_ok(rm.metadata(server, {"name": "campaign.status"}))["fields"][0]
    assert after["enum_values"][-1] == "ORACLE_LATER_VALUE", "a cached entry outlived one hour"


def _failure(error_code, message):
    failure = h.get_ads_type("GoogleAdsFailure")
    ParseDict({"errors": [{"error_code": error_code, "message": message}]}, failure._pb)
    return GoogleAdsException(None, None, failure, "synthetic-field-request")


PLANTED = "provider diagnostic " + h.FAKE_REFRESH_TOKEN + " " + h.FAKE_CLIENT_SECRET


@pytest.mark.parametrize("fault,code", [
    (lambda: _failure({"request_error": "INVALID_ENUM_VALUE"}, PLANTED), "ADS_API_REJECTED"),
    (lambda: _failure({"authorization_error": "CLOUD_PROJECT_NOT_APPROVED_FOR_PRODUCTION"}, PLANTED),
     "ADS_CLOUD_PROJECT_NOT_APPROVED"),
    (lambda: RefreshError("invalid_grant " + PLANTED), "AUTH_TOKEN_REVOKED"),
    (lambda: GoogleAuthError(PLANTED), "AUTH_FAILED"),
    (lambda: ServiceUnavailable(PLANTED), "TRANSPORT_FAILED"),
    (lambda: DeadlineExceeded(PLANTED), "TRANSPORT_FAILED"),
], ids=["rejected", "project", "revoked", "auth", "unavailable", "deadline"])
def test_provider_failures_map_to_existing_structured_codes_without_secrets(tmp_path, fault, code):
    client = rm.provider()
    client.stub_field_error(fault())
    server, _ = server_for(tmp_path, client, read_only=False)
    result = rm.call_result(server, {"name": "campaign.status"})
    payload = h.payload_of(result)
    assert h.error_of(payload)["code"] == code, payload
    text = h.result_text(result)
    if h.audit_file(tmp_path).exists():
        text += h.audit_file(tmp_path).read_text()
    h.assert_no_secrets(text)
    assert "Traceback" not in text and "googleAdsFields/" not in text
    assert 1 <= len(client.field_searches) <= rm.MAX_REQUESTS
    assert not client.searches and not client.mutations
    if code == "AUTH_TOKEN_REVOKED":
        records = h.read_audit_records(tmp_path)
        assert any(r.get("event") == "auth_failure" and r.get("tool") == rm.TOOL for r in records)


@pytest.mark.parametrize("read_only", [True, False], ids=["readonly", "write-mode"])
def test_two_accounts_get_identical_account_free_metadata(tmp_path, read_only):
    payloads = []
    for customer in (h.CUSTOMER_ID_DASHED, "555-666-7777"):
        root = tmp_path / customer
        server, client = server_for(root, read_only=read_only, env={"GOOGLE_ADS_CUSTOMER_ID": customer})
        payloads.append(h.expect_ok(rm.metadata(server, {"name": "campaign.status"})))
        assert not client.searches, "field metadata must not read account data"
        for call in client.field_searches:
            assert h.CUSTOMER_ID not in call.query and h.OTHER_CUSTOMER_ID not in call.query
    assert payloads[0] == payloads[1] == {"name": "campaign.status", "fields": rm.expected_fields("campaign.status"),
                                          "next_page_token": None}


def test_output_carries_no_raw_provider_payload_or_credentials(tmp_path):
    server, _ = server_for(tmp_path)
    result = rm.call_result(server, {"name": "campaign"})
    text = h.result_text(result)
    h.expect_ok(h.payload_of(result))
    assert "googleAdsFields/" not in text and "google.ads.googleads.v25.resources" not in text
    assert "type_url" not in text
    h.assert_no_secrets(text)


@pytest.fixture
def installed(monkeypatch, tmp_path):
    def deny(*args, **kwargs):
        raise AssertionError("F088 parent forbids network")
    for name in ("connect", "connect_ex", "bind", "sendto"):
        monkeypatch.setattr(socket.socket, name, deny)
    monkeypatch.setattr(socket, "getaddrinfo", deny)
    monkeypatch.setattr(plumbing, "INJECTION", plumbing.INJECTION + rm.INSTALLED_FIELDS_INJECTION)
    servers = []

    def start(root, **kwargs):
        env = {"ORACLE_FIELDS": str(rm.CATALOGUE_PATH), **kwargs.pop("env", {})}
        server = plumbing.InstalledServer(root, env=env, **kwargs)
        servers.append(server)
        return server

    yield start
    for server in servers:
        if server.process.poll() is None:
            server.process.stdin.close()
            try:
                server.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.process.kill()
        for stream in (server.process.stdout, server.process.stderr):
            if stream:
                stream.close()


def _events(server, kind):
    if not server.marker.exists():
        return []
    return [json.loads(line) for line in server.marker.read_text().splitlines()
            if line.strip() and json.loads(line)["event"] == kind]


@pytest.mark.parametrize("read_only,customer", [(True, h.CUSTOMER_ID), (False, h.OTHER_CUSTOMER_ID)],
                         ids=["readonly", "write-other-account"])
def test_installed_console_serves_metadata_over_real_mcp(installed, tmp_path, read_only, customer):
    server = installed(tmp_path / "installed", customer=customer,
                       env={"ADS_MCP_READ_ONLY": str(read_only).lower(), **FAST_RETRY})
    listing = server.receive(server.send("tools/list", {}))["result"]["tools"]
    tools = {tool["name"]: tool for tool in listing}
    assert rm.TOOL in tools, f"installed {rm.TOOL} is not listed"
    assert set(tools) == (FINAL_READS if read_only else FINAL_ALL)
    schema = tools[rm.TOOL]["inputSchema"]
    assert set(schema["properties"]) == set(rm.SIGNATURE[0]) and schema.get("required") == rm.SIGNATURE[1]
    golden = json.loads(rm.GOLDEN_PATH.read_text())
    assert server.call(rm.TOOL, golden["args"]) == golden["golden"]
    walked = [server.call(rm.TOOL, {"name": "campaign"})]
    walked.append(server.call(rm.TOOL, {"name": "campaign", "page_token": walked[0]["next_page_token"]}))
    assert [e for page in walked for e in page["fields"]] == rm.expected_fields("campaign")
    error = server.receive(server.send("tools/call", {"name": rm.TOOL, "arguments": {"name": "Campaign Status"}}))
    assert server.payload(error)["error"]["code"] == "INVALID_ARGUMENT"
    fields = _events(server, "field")
    assert fields and all(event["method"] == "search_google_ads_fields" for event in fields)
    assert all(isinstance(event["timeout"], (int, float)) and 0 < event["timeout"] <= 10 for event in fields)
    assert not _events(server, "search") and not _events(server, "network attempted")
    public = "".join(server.public + server.stderr)
    assert "Traceback" not in public
    h.assert_no_secrets(public)


def test_inventories_docs_capabilities_parity_and_archives_agree_exactly(tmp_path):
    server, _ = server_for(tmp_path, read_only=False)
    assert rm.TOOL in h.tool_names(server), f"{rm.TOOL} is not registered"
    installed = load_script("check_installed")
    assert rm.TOOL in installed.EXPECTED_READS and len(installed.EXPECTED_READS) == 35
    assert installed.EXPECTED_WRITE_MODE == FINAL_ALL
    assert set(asyncio.run(installed.catalog(True))) == FINAL_READS
    assert set(asyncio.run(installed.catalog(False))) == FINAL_ALL
    records = [tool for capability in json.loads(DEFAULT.read_text())["capabilities"] for tool in capability["tools"]]
    by_name = {record["name"]: record for record in records}
    assert len(records) == len(by_name) == 85 and set(by_name) == FINAL_ALL
    assert by_name[rm.TOOL] == {"name": rm.TOOL, "parameters": rm.SIGNATURE[0],
                                "required": rm.SIGNATURE[1], "values": {}}
    result, _ = cli(tmp_path, default=True)
    success(result, empty())
    parity = load_script("parity")
    paths = parity.fixture_paths(None)
    assert rm.GOLDEN_PATH in paths and len(paths) == 35
    report = subprocess.run([sys.executable, str(rm.ROOT / "scripts/parity.py"), "--report", "-"],
                            cwd=tmp_path, env=h.scrubbed_env(), capture_output=True, text=True, timeout=120)
    assert report.returncode == 0 and not report.stderr, report.stdout + report.stderr
    matched = re.findall(r"(?m)^(\w+)\s+MATCH\s*$", report.stdout)
    assert len(matched) == len(set(matched)) == 35 and set(matched) == FINAL_READS
    docs = (rm.ROOT / "docs/tools.md").read_text()
    reads_section = docs.split("## Read tools", 1)[1].split("\n## ", 1)[0]
    assert f"### `{rm.TOOL}`" in reads_section
    generated = subprocess.run([sys.executable, str(rm.ROOT / "scripts/gen_tools_md.py"), "--stdout"],
                               cwd=rm.ROOT, capture_output=True, text=True, timeout=60)
    assert generated.returncode == 0 and generated.stdout == docs
    checker = load_script("check_release_archives")
    assert set(rm.NEW_SOURCE) <= set(checker.REQUIRED_SOURCE)
    assert all((rm.ROOT / path).is_file() for path in rm.NEW_SOURCE)
    for missing in rm.NEW_SOURCE:
        with pytest.raises(ValueError):
            checker.check_inventory(sorted(set(checker.REQUIRED_SOURCE) - {missing}), source_archive=True)


def _normalized(path):
    return re.sub(r"\s+", " ", path.read_text())


def test_readme_and_guides_state_85_operations_and_35_reads_and_no_stale_counts():
    readme = _normalized(rm.ROOT / "README.md")
    assert re.search(r"\b35 read(?: tools|s)?\b", readme), "README must state 35 reads"
    assert re.search(r"\b85 (?:total )?operations\b", readme), "README must state 85 operations"
    stale = re.compile(r"\b84 (?:total )?operations\b|\b(?:all )?34 (?:read tools|reads|read fixtures|current reads)\b")
    for path in [rm.ROOT / "README.md", *sorted((rm.ROOT / "docs").glob("*.md"))]:
        text = _normalized(path)
        assert not stale.search(text), f"{path.name} still states a pre-metadata count"
    for name in ("installation-validation.md", "migration.md", "shared-targeting.md"):
        text = _normalized(rm.ROOT / "docs" / name)
        assert "85" in text and "35" in text, name
