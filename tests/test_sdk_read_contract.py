"""F026: public GAQL aliases and genuine generated SDK argument binding.

The recommendation fields are selectable in the captured v25 field metadata.
The nested ad alias/path is selectable in the public v25 ad_group_ad reference:
https://developers.google.com/google-ads/api/fields/v25/ad_group_ad
Zero metrics retain their existing F005 coverage; nonselectable recommendation
impact leaves must not be substituted into these public-query fixtures.
"""
import csv
import inspect
import io
from types import SimpleNamespace

import pytest
from google.ads.googleads.errors import GoogleAdsException
from google.ads.googleads.v25.services.services.google_ads_service import GoogleAdsServiceClient
from google.protobuf.json_format import ParseDict

import harness as h


def bind_sdk_search(client):
    """Use the real generated method signature, never a permissive fake seam.

    No generated method body or network transport is entered. Request objects
    additionally cannot carry the deprecated page_size field.
    """
    signature = inspect.signature(GoogleAdsServiceClient.search)
    seen = []
    def search(*args, **kwargs):
        signature.bind(object(), *args, **kwargs)
        request = kwargs.get("request") or (args[0] if args else None)
        if request is not None:
            page_size = request.get("page_size", 0) if isinstance(request, dict) else request.page_size
            assert not page_size, "page_size must be a local bound, never an SDK request field"
        seen.append((args, kwargs))
        return client._do_search("GoogleAdsService", "search", args, kwargs)
    client._services["GoogleAdsService"] = SimpleNamespace(search=search)
    return seen


@pytest.mark.parametrize("fmt", ["json", "csv", "table"])
@pytest.mark.parametrize("query,resource,fixture,fields,values", [
    ("SELECT recommendation.type, recommendation.campaign FROM recommendation",
     "recommendation", {"recommendation": {"type_": "CAMPAIGN_BUDGET", "campaign": ""}},
     ["recommendation.type", "recommendation.campaign"], ["CAMPAIGN_BUDGET", ""]),
    ("SELECT ad_group_ad.ad.type, ad_group_ad.ad.responsive_search_ad.path1 FROM ad_group_ad",
     "ad_group_ad", {"ad_group_ad": {"ad": {"type_": "RESPONSIVE_SEARCH_AD"}}},
     ["ad_group_ad.ad.type", "ad_group_ad.ad.responsive_search_ad.path1"], ["RESPONSIVE_SEARCH_AD", None]),
])
def test_public_aliases_keep_selected_keys_and_values(tmp_path, fake_client, fmt, query, resource, fixture, fields, values):
    fake_client.stub(resource, [fixture])
    bind_sdk_search(fake_client)
    server = h.build_server(tmp_path, client=fake_client)
    payload = h.expect_ok(h.call(server, "run_gaql", {"query": query, "format": fmt}))
    if fmt == "table":
        assert payload["columns"] == fields and payload["rows"] == [values]
    elif fmt == "csv":
        rows = list(csv.reader(io.StringIO(payload["csv"])))
        assert rows == [fields, ["" if v is None else str(v) for v in values]]
    else:
        assert payload["fields"] == fields
        assert len(payload["rows"]) == 1
        for field, expected in zip(fields, values):
            node = payload["rows"][0]
            for segment in field.split("."):
                node = node[segment]
            assert node == expected
        assert "type_" not in str(payload["rows"])


@pytest.mark.parametrize("fmt", ["json", "table", "csv"])
@pytest.mark.parametrize("size,expected_size", [(1, 1), (2, 2), (10000, 3), (None, 3)])
def test_local_pagination_walks_all_rows_with_real_sdk_binding(tmp_path, fake_client, fmt, size, expected_size):
    fake_client.stub("customer", [{"customer": {"id": i}} for i in range(1, 8)])
    seen = bind_sdk_search(fake_client)
    server = h.build_server(tmp_path, client=fake_client, env={"ADS_MCP_ROW_LIMIT": "3"})
    args = {"query": "SELECT customer.id FROM customer", "format": fmt}
    if size is not None:
        args["page_size"] = size
    all_ids, tokens = [], set()
    for _ in range(8):
        payload = h.expect_ok(h.call(server, "run_gaql", args))
        if fmt == "json":
            batch = [r["customer"]["id"] for r in payload["rows"]]
        elif fmt == "table":
            assert payload["columns"] == ["customer.id"]
            batch = [r[0] for r in payload["rows"]]
        else:
            rows = list(csv.reader(io.StringIO(payload["csv"])))
            assert rows[0] == ["customer.id"]
            batch = [int(r[0]) for r in rows[1:]]
        assert len(batch) == min(expected_size, 7 - len(all_ids))
        all_ids.extend(batch)
        token = payload.get("next_page_token")
        if not token:
            break
        assert token not in tokens
        tokens.add(token)
        assert isinstance(token, str) and token, "continuation must be a nonempty token"
        args["page_token"] = token
    assert all_ids == list(range(1, 8)) and seen
    assert all(call.page_size == 0 for call in fake_client.searches)


@pytest.mark.parametrize("page_size", [0, -1, 10001])
def test_invalid_page_size_is_named_before_transport(tmp_path, account_client, page_size):
    server = h.build_server(tmp_path, client=account_client)
    h.expect_error(server, "run_gaql", {"query": "SELECT customer.id FROM customer", "page_size": page_size}, code="INVALID_PAGE_SIZE")
    assert not account_client.searches


@pytest.mark.parametrize("page_size", [0.5, "not-an-integer"])
def test_wrong_typed_page_sizes_remain_framework_validation_errors(tmp_path, account_client, page_size):
    # Decision 0006 preserves the typed MCP schema. These never enter the
    # structured tool envelope; no widening to accept arbitrary input types.
    server = h.build_server(tmp_path, client=account_client)
    result = h.call_result(server, "run_gaql", {"query": "SELECT customer.id FROM customer", "page_size": page_size})
    assert result.is_error
    assert not account_client.searches


@pytest.mark.parametrize("customer", ["", " ", "junk", "123", "9876543210 OR 1=1"])
def test_explicit_invalid_customer_never_falls_back(tmp_path, account_client, customer):
    server = h.build_server(tmp_path, client=account_client)
    h.expect_error(server, "run_gaql", {"query": "SELECT customer.id FROM customer", "customer_id": customer}, code="INVALID_CUSTOMER_ID")
    assert not account_client.searches


@pytest.mark.parametrize("customer,expected", [(None, h.CUSTOMER_ID), (h.OTHER_CUSTOMER_ID, h.OTHER_CUSTOMER_ID), (h.CUSTOMER_ID_DASHED, h.CUSTOMER_ID)])
def test_omission_and_valid_customer_select_exact_account(tmp_path, account_client, customer, expected):
    server = h.build_server(tmp_path, client=account_client)
    args = {"query": "SELECT customer.id FROM customer"}
    if customer is not None:
        args["customer_id"] = customer
    result = h.expect_ok(h.call(server, "run_gaql", args))
    assert result["customer_id"] == expected
    assert account_client.searches[-1].customer_id == expected


@pytest.mark.parametrize("tool,args", [("get_account_info", {}), ("run_gaql", {"query": "SELECT customer.id FROM customer"})])
@pytest.mark.parametrize("authorization", ["USER_PERMISSION_DENIED", "CUSTOMER_NOT_ENABLED"])
def test_sdk_authorization_error_names_requested_account_and_manager(tmp_path, account_client, tool, args, authorization):
    failure = h.get_ads_type("GoogleAdsFailure")
    ParseDict({"errors": [{"error_code": {"authorization_error": authorization}, "message": "synthetic account inaccessible"}]}, failure._pb)
    account_client.stub_error(GoogleAdsException(None, None, failure, "synthetic-request"))
    server = h.build_server(tmp_path, client=account_client)
    payload = h.call(server, tool, {**args, "customer_id": h.OTHER_CUSTOMER_ID})
    error = h.error_of(payload)
    assert error["code"] == "ACCOUNT_NOT_ACCESSIBLE"
    assert h.OTHER_CUSTOMER_ID in error["message"] and h.LOGIN_CUSTOMER_ID in error["message"]
    assert len(account_client.searches) == 1


def test_other_google_ads_failures_retain_query_error(tmp_path, account_client):
    failure = h.get_ads_type("GoogleAdsFailure")
    ParseDict({"errors": [{"error_code": {"query_error": "UNRECOGNIZED_FIELD"}, "message": "synthetic unknown GAQL field"}]}, failure._pb)
    account_client.stub_error(GoogleAdsException(None, None, failure, "synthetic-query"))
    server = h.build_server(tmp_path, client=account_client)
    error = h.error_of(h.call(server, "run_gaql", {"query": "SELECT customer.nonexistent FROM customer"}))
    assert error["code"] == "GAQL_ERROR" and "synthetic unknown GAQL field" in error["message"]


def test_unknown_projection_field_is_still_named(tmp_path, account_client):
    server = h.build_server(tmp_path, client=account_client)
    error = h.error_of(h.call(server, "run_gaql", {"query": "SELECT recommendation.not_real FROM recommendation"}))
    assert error["code"] == "INVALID_QUERY" and "not_real" in error["message"]
