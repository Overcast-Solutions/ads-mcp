"""F089: run_gaql query-error guidance from GoogleAdsFieldService metadata.

Provider substitution only: genuine v25 GoogleAdsFailure/QueryError messages on
the search path and the harness field-service stub (resource_metadata_oracle).
Hint content is checked against the synthetic catalogue, never product output.

Contract pinned here: listed query errors keep ``error.code`` GAQL_ERROR and the
unchanged ``error.message``, and add ``error.provider_error`` (the enum name)
and ``error.hints`` (1-5 objects with exactly kind, message and fields; message
at most 200 characters; fields are catalogue names). With no identifiable
field, or a failed/empty lookup, ``hints`` is absent or empty.
"""
import json
import re
import socket
import subprocess

import pytest
from google.api_core.exceptions import DeadlineExceeded, ServiceUnavailable
from google.auth.exceptions import RefreshError
from google.ads.googleads.errors import GoogleAdsException
from google.protobuf.json_format import ParseDict

import harness as h
import resource_metadata_oracle as rm
import test_auth_cause_contract as plumbing

FAST_RETRY = {"ADS_MCP_RETRY_BASE_SECONDS": "0.001"}
UNRECOGNIZED = ["UNRECOGNIZED_FIELD", "BAD_FIELD_NAME"]
REQUIRED = ["EXPECTED_REFERENCED_FIELD_IN_SELECT_CLAUSE", "EXPECTED_DIMENSION_FIELD_IN_SELECT_CLAUSE",
            "REQUIRED_SEGMENT_FIELD_MISSING"]
PROHIBITED = ["PROHIBITED_FIELD_IN_SELECT_CLAUSE", "PROHIBITED_RESOURCE_TYPE_IN_SELECT_CLAUSE",
              "PROHIBITED_METRIC_IN_SELECT_OR_WHERE_CLAUSE", "PROHIBITED_SEGMENT_IN_SELECT_OR_WHERE_CLAUSE",
              "PROHIBITED_SEGMENT_WITH_METRIC_IN_SELECT_OR_WHERE_CLAUSE"]
OPERATOR = ["OPERATOR_FIELD_MISMATCH", "BAD_OPERATOR"]
LISTED = UNRECOGNIZED + REQUIRED + PROHIBITED + OPERATOR
REQUEST_ID = "synthetic-request-id-DO-NOT-ECHO"


def query_failure(enum, message, trigger=None, *, extra=()):
    error = {"error_code": {"query_error": enum}, "message": message}
    if trigger is not None:
        error["trigger"] = {"string_value": trigger}
    failure = h.get_ads_type("GoogleAdsFailure")
    ParseDict({"errors": [error, *extra]}, failure._pb)
    return GoogleAdsException(None, None, failure, REQUEST_ID)


def server_for(tmp_path, client=None, *, env=None, read_only=True):
    tmp_path.mkdir(parents=True, exist_ok=True)
    client = client if client is not None else rm.provider()
    merged = {**FAST_RETRY, "ADS_MCP_AUDIT_LOG": str(h.audit_file(tmp_path)), **(env or {})}
    if read_only:
        return h.build_server(tmp_path, client=client, env=merged), client
    return h.build_rw_server(tmp_path, client=client, env=merged), client


def gaql_error(server, query, customer_id=None):
    args = {"query": query}
    if customer_id is not None:
        args["customer_id"] = customer_id
    return h.error_of(h.call(server, "run_gaql", args))


def hints_of(error, enum):
    assert error["code"] == "GAQL_ERROR", error
    assert error.get("provider_error") == enum, f"provider_error missing or wrong: {error}"
    hints = error.get("hints")
    assert isinstance(hints, list) and 1 <= len(hints) <= 5, f"expected 1-5 hints: {error}"
    for hint in hints:
        assert set(hint) == {"kind", "message", "fields"}, hint
        assert isinstance(hint["kind"], str) and hint["kind"] and len(hint["kind"]) <= 64
        assert isinstance(hint["message"], str) and 0 < len(hint["message"]) <= 200
        assert isinstance(hint["fields"], list) and all(rm.passes_name_rule(f) for f in hint["fields"])
    return hints


def no_hints(error):
    assert error["code"] == "GAQL_ERROR", error
    assert error.get("hints", []) == [], f"hints must be omitted here: {error}"


def hinted_fields(hints):
    return {name for hint in hints for name in hint["fields"]}


def assert_unrewritten(client, query, searches=1):
    assert [call.query for call in client.searches] == [query] * searches, (
        "a query error must never rewrite or resubmit the query")


FACT_NEG = r"\b(?:not|non|un)[- ]?{0}|{0}\W{{0,3}}(?:false|no)\b"


def assert_fact(message, word, value):
    lowered = message.lower()
    assert word in lowered, f"hint must state {word}: {message!r}"
    negative = re.search(FACT_NEG.format(word), lowered) is not None
    assert negative is (not value), f"hint misstates {word}={value}: {message!r}"


# ---------------------------------------------------------------------------
# One positive fixture per family, for every listed enum.


@pytest.mark.parametrize("enum", UNRECOGNIZED)
@pytest.mark.parametrize("field,top,best", [("campaign.stauts", "campaign", "campaign.status"),
                                            ("ad_group.stauts", "ad_group", "ad_group.status")])
def test_unrecognized_field_hints_name_close_catalogue_fields(tmp_path, enum, field, top, best):
    query = f"SELECT {field} FROM {top}"
    message = f"Unrecognized field in the query: '{field}'."
    server, client = server_for(tmp_path, rm.provider().stub_error(query_failure(enum, message, field)))
    error = gaql_error(server, query)
    hints = hints_of(error, enum)
    assert error["message"] == message
    named = hinted_fields(hints)
    candidates = rm.close_candidates(field, top)
    assert best in named and named <= candidates, {"named": sorted(named), "allowed": sorted(candidates)}
    assert len(named) <= 5 and not any("adxgroup" in name for name in named)
    assert_unrewritten(client, query)
    rm.assert_bounded_requests(client, 0, [top])


@pytest.mark.parametrize("enum", REQUIRED)
@pytest.mark.parametrize("field", ["campaign.name", "segments.date"])
def test_required_select_field_hint_names_the_exact_field_to_add(tmp_path, enum, field):
    query = "SELECT metrics.clicks FROM campaign WHERE segments.date DURING LAST_7_DAYS ORDER BY campaign.name"
    message = f"The following field must be present in SELECT clause: '{field}'."
    server, client = server_for(tmp_path, rm.provider().stub_error(query_failure(enum, message, field)))
    error = gaql_error(server, query)
    hints = hints_of(error, enum)
    assert error["message"] == message
    assert any(hint["fields"] == [field] for hint in hints), hints
    assert hinted_fields(hints) <= set(rm.by_name()), "hints may only name catalogue fields"
    assert_unrewritten(client, query)
    assert len(client.field_searches) <= rm.MAX_REQUESTS


@pytest.mark.parametrize("enum", PROHIBITED)
@pytest.mark.parametrize("field,expected", [("metrics.ctr", {"campaign", "customer"}),
                                            ("ad_group", {"campaign", "customer"}),
                                            ("segments.hour", {"ad_group", "campaign", "customer"})])
def test_prohibited_field_hint_lists_resources_it_is_selectable_with(tmp_path, enum, field, expected):
    query = f"SELECT {field}, keyword_view.resource_name FROM keyword_view"
    message = f"The field '{field}' cannot be selected with the resource in the FROM clause."
    server, client = server_for(tmp_path, rm.provider().stub_error(query_failure(enum, message, field)))
    error = gaql_error(server, query)
    hints = hints_of(error, enum)
    assert error["message"] == message
    assert any(set(hint["fields"]) == expected for hint in hints), hints
    assert_unrewritten(client, query)
    rm.assert_bounded_requests(client, 0, [field.split(".")[0]])


@pytest.mark.parametrize("enum", PROHIBITED)
def test_selectable_with_hint_is_capped_at_five_resources(tmp_path, enum):
    field = "segments.date"
    resources = {name for name in rm.by_name()[field]["selectable_with"] if "." not in name}
    assert len(resources) > 5
    query = "SELECT segments.date FROM change_event"
    message = f"The field '{field}' cannot be selected with the resource in the FROM clause."
    server, client = server_for(tmp_path, rm.provider().stub_error(query_failure(enum, message, field)))
    hints = hints_of(gaql_error(server, query), enum)
    listing = [hint for hint in hints if hint["fields"] and set(hint["fields"]) <= resources]
    assert listing and all(len(hint["fields"]) == len(set(hint["fields"])) <= 5 for hint in listing)
    assert any(len(hint["fields"]) == 5 for hint in listing)


@pytest.mark.parametrize("enum", OPERATOR)
@pytest.mark.parametrize("field", ["campaign.status", "campaign.labels", "campaign.url_custom_parameters",
                                   "metrics.clicks", "campaign.primary_status_reasons"])
def test_operator_hint_states_filterable_type_repeated_and_enum_facts(tmp_path, enum, field):
    query = f"SELECT campaign.id FROM campaign WHERE {field} LIKE '%x%'"
    message = f"Invalid operator for field '{field}'."
    server, client = server_for(tmp_path, rm.provider().stub_error(query_failure(enum, message, field)))
    error = gaql_error(server, query)
    hints = hints_of(error, enum)
    assert error["message"] == message
    facts = [hint for hint in hints if hint["fields"] == [field]]
    assert facts, hints
    source = rm.by_name()[field]
    text = " ".join(hint["message"] for hint in facts)
    assert source["data_type"] in text
    assert_fact(text, "filterable", source["filterable"])
    assert_fact(text, "repeated", source["is_repeated"])
    if source["data_type"] == "ENUM":
        useful = [value for value in source["enum_values"] if value not in ("UNSPECIFIED", "UNKNOWN")]
        assert any(value in text for value in useful), "enum facts must list values"
    assert_unrewritten(client, query)
    rm.assert_bounded_requests(client, 0, [field.split(".")[0]])


# ---------------------------------------------------------------------------
# Identification of the offending field.


def test_valid_trigger_takes_precedence_over_the_message(tmp_path):
    message = "Unrecognized field in the query: 'campaign.nme'."
    exc = query_failure("UNRECOGNIZED_FIELD", message, "ad_group.stauts")
    server, client = server_for(tmp_path, rm.provider().stub_error(exc))
    hints = hints_of(gaql_error(server, "SELECT ad_group.stauts, campaign.nme FROM ad_group"), "UNRECOGNIZED_FIELD")
    named = hinted_fields(hints)
    assert "ad_group.status" in named and "campaign.name" not in named
    rm.assert_bounded_requests(client, 0, ["ad_group"])


@pytest.mark.parametrize("trigger", [None, "", "SELECT", "Campaign.Status", "campaign status",
                                     "campaign.stauts'", "x" * 129, "campaign..stauts", "1=1"])
def test_invalid_or_missing_trigger_falls_back_to_first_valid_quoted_dotted_name(tmp_path, trigger):
    message = ("Unrecognized field in the query: 'Campaign.Stauts' near 'campaign' "
               "and 'campaign.stauts' and 'ad_group.stauts'.")
    exc = query_failure("BAD_FIELD_NAME", message, trigger)
    server, client = server_for(tmp_path, rm.provider().stub_error(exc))
    error = gaql_error(server, "SELECT campaign.stauts FROM campaign")
    hints = hints_of(error, "BAD_FIELD_NAME")
    named = hinted_fields(hints)
    assert "campaign.status" in named and "ad_group.status" not in named
    assert error["message"] == message
    rm.assert_bounded_requests(client, 0, ["campaign"])


@pytest.mark.parametrize("message", [
    "Unrecognized field in the query.",
    "Unrecognized field in the query: campaign.stauts (unquoted).",
    "Unrecognized field in the query: 'Campaign.Stauts'.",
    "Unrecognized field in the query: 'campaign'.",
    "Unrecognized field in the query: \"campaign.stauts\".",
])
def test_no_identifiable_field_means_no_hints_and_no_lookup(tmp_path, message):
    exc = query_failure("UNRECOGNIZED_FIELD", message, "Not A Field")
    server, client = server_for(tmp_path, rm.provider().stub_error(exc))
    error = gaql_error(server, "SELECT campaign.stauts FROM campaign")
    assert error.get("provider_error") == "UNRECOGNIZED_FIELD", error
    no_hints(error)
    assert error["message"] == message
    assert client.field_searches == []
    # Control: the same failure with a valid trigger does produce guidance.
    control, control_client = server_for(tmp_path / "control", rm.provider().stub_error(
        query_failure("UNRECOGNIZED_FIELD", message, "campaign.stauts")))
    assert hints_of(gaql_error(control, "SELECT campaign.stauts FROM campaign"), "UNRECOGNIZED_FIELD")


# ---------------------------------------------------------------------------
# Never resubmitted; failures leave the original error alone.


@pytest.mark.parametrize("fault", [
    lambda: query_failure("QUERY_ERROR", "synthetic field service failure " + h.FAKE_REFRESH_TOKEN),
    lambda: ServiceUnavailable("synthetic field service unavailable"),
    lambda: DeadlineExceeded("synthetic field service deadline"),
    lambda: RefreshError("invalid_grant " + h.FAKE_REFRESH_TOKEN),
    lambda: RuntimeError("synthetic field service crash"),
], ids=["rejected", "unavailable", "deadline", "revoked", "crash"])
def test_failed_lookup_returns_the_original_error_without_hints_or_a_second_error(tmp_path, fault):
    message = "Unrecognized field in the query: 'campaign.stauts'."
    client = rm.provider().stub_error(query_failure("UNRECOGNIZED_FIELD", message, "campaign.stauts"))
    client.stub_field_error(fault())
    server, _ = server_for(tmp_path, client)
    result = h.call_result(server, "run_gaql", {"query": "SELECT campaign.stauts FROM campaign"})
    error = h.error_of(h.payload_of(result))
    assert error.get("provider_error") == "UNRECOGNIZED_FIELD", error
    no_hints(error)
    assert error["message"] == message
    assert set(error) <= {"code", "message", "provider_error", "hints"}
    assert 1 <= len(client.field_searches) <= rm.MAX_REQUESTS
    assert_unrewritten(client, "SELECT campaign.stauts FROM campaign")
    text = h.result_text(result)
    h.assert_no_secrets(text)
    assert "Traceback" not in text and "AUTH_TOKEN_REVOKED" not in text and "TRANSPORT_FAILED" not in text


def test_empty_lookup_returns_the_original_error_without_hints(tmp_path):
    message = "Unrecognized field in the query: 'campaign.stauts'."
    client = rm.provider(fields=[]).stub_error(query_failure("UNRECOGNIZED_FIELD", message, "campaign.stauts"))
    server, _ = server_for(tmp_path, client)
    error = gaql_error(server, "SELECT campaign.stauts FROM campaign")
    assert error.get("provider_error") == "UNRECOGNIZED_FIELD", error
    no_hints(error)
    assert error["message"] == message
    assert 1 <= len(client.field_searches) <= rm.MAX_REQUESTS


def test_transient_search_retries_stay_f003_retries_and_hints_use_only_the_field_service(tmp_path):
    query = "SELECT campaign.stauts FROM campaign"
    message = "Unrecognized field in the query: 'campaign.stauts'."
    client = rm.provider()
    faults = [ServiceUnavailable("synthetic temporary read failure"),
              query_failure("UNRECOGNIZED_FIELD", message, "campaign.stauts")]
    original = client._do_search

    def search(service, method, args, kwargs):
        original(service, method, args, kwargs)
        raise faults.pop(0) if len(faults) > 1 else faults[0]

    client._do_search = search
    server, _ = server_for(tmp_path, client)
    hints = hints_of(gaql_error(server, query), "UNRECOGNIZED_FIELD")
    assert "campaign.status" in hinted_fields(hints)
    assert [call.query for call in client.searches] == [query, query]
    assert all(call.service == "GoogleAdsService" for call in client.searches)
    rm.assert_bounded_requests(client, 0, ["campaign"])


def test_hint_lookup_shares_one_ten_second_deadline(tmp_path):
    import time

    seen = []

    def slow(call):
        seen.append(call)
        if len(seen) == 1:
            time.sleep(1.2)
            raise ServiceUnavailable("synthetic slow field service")

    client = rm.provider().stub_error(query_failure(
        "UNRECOGNIZED_FIELD", "Unrecognized field in the query: 'campaign.stauts'.", "campaign.stauts"))
    client.field_hook = slow
    server, _ = server_for(tmp_path, client)
    error = gaql_error(server, "SELECT campaign.stauts FROM campaign")
    assert error.get("provider_error") == "UNRECOGNIZED_FIELD", error
    calls = rm.assert_bounded_requests(client, 0, ["campaign"])
    for call in calls[1:]:
        assert call.timeout <= rm.DEADLINE_SECONDS - 1.15


# ---------------------------------------------------------------------------
# Scope: run_gaql only, listed enums only; success unchanged.


@pytest.mark.parametrize("enum", ["QUERY_ERROR", "BAD_LIMIT_VALUE", "DATE_RANGE_TOO_WIDE", "BAD_RESOURCE_TYPE_IN_FROM_CLAUSE",
                                  "PROHIBITED_FIELD_COMBINATION_IN_SELECT_CLAUSE", "INVALID_STRING_VALUE"])
def test_unlisted_query_errors_and_successful_queries_are_unchanged(tmp_path, enum):
    listed, listed_client = server_for(tmp_path / "listed", rm.provider().stub_error(query_failure(
        "UNRECOGNIZED_FIELD", "Unrecognized field in the query: 'campaign.stauts'.", "campaign.stauts")))
    hints_of(gaql_error(listed, "SELECT campaign.stauts FROM campaign"), "UNRECOGNIZED_FIELD")
    message = f"Synthetic {enum.lower()} for 'campaign.status'."
    server, client = server_for(tmp_path / "unlisted", rm.provider().stub_error(query_failure(enum, message, "campaign.status")))
    payload = h.call(server, "run_gaql", {"query": "SELECT campaign.status FROM campaign"})
    assert payload == {"error": {"code": "GAQL_ERROR", "message": message}}
    assert client.field_searches == []
    fixture = h.load_contract_fixture(h.CONTRACT_DIR / "run_gaql.json")
    golden_client = rm.provider(account=False)
    for resource, rows in fixture["gaql"].items():
        golden_client.stub(resource, rows)
    golden_server, _ = server_for(tmp_path / "golden", golden_client, env=fixture.get("env", {}))
    assert h.call(golden_server, "run_gaql", fixture["args"]) == fixture["golden"]
    assert golden_client.field_searches == []


@pytest.mark.parametrize("tool,args", [
    ("get_campaign_performance", {"last_n_days": 7}),
    ("get_keyword_performance", {"last_n_days": 7}),
    ("get_account_info", {}),
    ("get_search_terms", {"last_n_days": 7}),
])
def test_other_tools_never_gain_hints(tmp_path, tool, args):
    listed, _ = server_for(tmp_path / "gaql", rm.provider().stub_error(query_failure(
        "UNRECOGNIZED_FIELD", "Unrecognized field in the query: 'campaign.stauts'.", "campaign.stauts")))
    hints_of(gaql_error(listed, "SELECT campaign.stauts FROM campaign"), "UNRECOGNIZED_FIELD")
    exc = query_failure("UNRECOGNIZED_FIELD", "Unrecognized field in the query: 'campaign.stauts'.", "campaign.stauts")
    server, client = server_for(tmp_path / "other", rm.provider().stub_error(exc))
    error = h.error_of(h.call(server, tool, args))
    assert set(error) == {"code", "message"}, error
    assert client.field_searches == []


# ---------------------------------------------------------------------------
# Hygiene, accounts and the installed console.


def test_hints_carry_no_credentials_account_data_payloads_or_long_query_text(tmp_path):
    marker = "uniqueQueryFragment" + "Q" * 80
    query = f"SELECT campaign.stauts FROM campaign WHERE campaign.name = '{marker}' LIMIT 5"
    message = ("Unrecognized field in the query: 'campaign.stauts'. " + h.FAKE_REFRESH_TOKEN + " "
               + h.FAKE_CLIENT_SECRET)
    server, client = server_for(tmp_path, rm.provider().stub_error(query_failure(
        "UNRECOGNIZED_FIELD", message, "campaign.stauts")), read_only=False)
    error = gaql_error(server, query, customer_id=h.OTHER_CUSTOMER_ID)
    hints = hints_of(error, "UNRECOGNIZED_FIELD")
    text = json.dumps(hints)
    h.assert_no_secrets(json.dumps(error))
    for forbidden in (h.CUSTOMER_ID, h.OTHER_CUSTOMER_ID, h.LOGIN_CUSTOMER_ID, REQUEST_ID,
                      "googleAdsFields/", "google.ads.googleads", "Traceback"):
        assert forbidden not in text, forbidden
    for start in range(len(query) - 64):
        assert query[start:start + 65] not in text, "hints carry more than a 64-character query excerpt"
    assert client.searches[0].customer_id == h.OTHER_CUSTOMER_ID
    if h.audit_file(tmp_path).exists():
        h.assert_no_secrets(h.audit_file(tmp_path).read_text())


def test_two_accounts_receive_identical_account_free_hints(tmp_path):
    results = []
    for customer in (h.CUSTOMER_ID, h.OTHER_CUSTOMER_ID):
        server, client = server_for(tmp_path / customer, rm.provider().stub_error(query_failure(
            "OPERATOR_FIELD_MISMATCH", "Invalid operator for field 'campaign.labels'.", "campaign.labels")))
        error = gaql_error(server, "SELECT campaign.id FROM campaign WHERE campaign.labels = 'x'", customer_id=customer)
        results.append(hints_of(error, "OPERATOR_FIELD_MISMATCH"))
        assert client.searches[0].customer_id == customer
        assert customer not in json.dumps(results[-1])
    assert results[0] == results[1]


QUERY_ERROR_INJECTION = r'''
from google.ads.googleads.errors import GoogleAdsException as _GAE
from google.protobuf.json_format import ParseDict as _parse
_guided_search = Transport._do_search
def _query_error_search(self, service, method, args, kwargs):
    rows = _guided_search(self, service, method, args, kwargs)
    spec = json.loads(control.read_text()).get("query_error")
    if spec:
        failure = h.get_ads_type("GoogleAdsFailure")
        _parse({"errors": [spec]}, failure._pb)
        raise _GAE(None, None, failure, "synthetic-installed-request")
    return rows
Transport._do_search = _query_error_search
'''


@pytest.fixture
def installed(monkeypatch, tmp_path):
    def deny(*args, **kwargs):
        raise AssertionError("F089 parent forbids network")
    for name in ("connect", "connect_ex", "bind", "sendto"):
        monkeypatch.setattr(socket.socket, name, deny)
    monkeypatch.setattr(socket, "getaddrinfo", deny)
    monkeypatch.setattr(plumbing, "INJECTION", plumbing.INJECTION + rm.INSTALLED_FIELDS_INJECTION + QUERY_ERROR_INJECTION)
    servers = []

    def start(root, **kwargs):
        env = {"ORACLE_FIELDS": str(rm.CATALOGUE_PATH), **FAST_RETRY, **kwargs.pop("env", {})}
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
def test_installed_console_returns_hints_over_real_mcp(installed, tmp_path, read_only, customer):
    server = installed(tmp_path / "installed", customer=customer, env={"ADS_MCP_READ_ONLY": str(read_only).lower()})
    message = "Unrecognized field in the query: 'campaign.stauts'."
    server.control.write_text(json.dumps({"query_error": {
        "error_code": {"query_error": "UNRECOGNIZED_FIELD"}, "message": message,
        "trigger": {"string_value": "campaign.stauts"}}}))
    query = "SELECT campaign.stauts FROM campaign"
    error = server.call("run_gaql", {"query": query, "customer_id": customer})["error"]
    hints = hints_of(error, "UNRECOGNIZED_FIELD")
    assert error["message"] == message and "campaign.status" in hinted_fields(hints)
    searches = _events(server, "search")
    assert [event["query"] for event in searches] == [query] and searches[0]["customer_id"] == customer
    fields = _events(server, "field")
    assert 1 <= len(fields) <= rm.MAX_REQUESTS
    assert all(event["method"] == "search_google_ads_fields" and 0 < event["timeout"] <= 10 for event in fields)
    server.control.write_text("{}")
    ok = server.call("run_gaql", {"query": "SELECT customer.id FROM customer"})
    assert "error" not in ok
    public = "".join(server.public + server.stderr)
    assert "Traceback" not in public
    h.assert_no_secrets(public)


def test_archive_inventory_requires_the_guidance_oracle():
    from offline_contract import load_script

    checker = load_script("check_release_archives")
    required = {"tests/test_gaql_field_guidance_contract.py"}
    assert required <= set(checker.REQUIRED_SOURCE), "the guidance oracle must ship in source archives"
    with pytest.raises(ValueError):
        checker.check_inventory(sorted(set(checker.REQUIRED_SOURCE) - required), source_archive=True)
