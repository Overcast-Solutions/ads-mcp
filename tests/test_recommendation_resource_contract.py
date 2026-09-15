"""F044: recommendation grammar through MCP and genuine generated SDK RPCs."""
import socket

import pytest
from google.ads.googleads.v25.services.services.recommendation_service import RecommendationServiceClient
from google.ads.googleads.v25.services.services.recommendation_service.transports.base import RecommendationServiceTransport
from google.auth.credentials import AnonymousCredentials

import harness as h
from offline_contract import ProjectedClient


ACCOUNTS = (h.CUSTOMER_ID, h.OTHER_CUSTOMER_ID)
TOOLS = ("apply_recommendation", "dismiss_recommendation")
OPAQUE_IDS = ("777", "000777", "Rec_A-09")


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("synthetic recommendation oracle forbids network")
    for name in ("connect", "connect_ex", "bind"):
        monkeypatch.setattr(socket.socket, name, denied)
    monkeypatch.setattr(socket, "getaddrinfo", denied)


class RecommendationTransport(RecommendationServiceTransport):
    """Only the RPC replies are synthetic; generated SDK methods run intact."""
    def __init__(self, owner):
        super().__init__(credentials=AnonymousCredentials())
        self.owner = owner
        self._apply = lambda request, **kwargs: self.reply("apply_recommendation", request, kwargs)
        self._dismiss = lambda request, **kwargs: self.reply("dismiss_recommendation", request, kwargs)
        self._wrapped_methods = {self._apply: self._apply, self._dismiss: self._dismiss}

    @property
    def apply_recommendation(self):
        return self._apply

    @property
    def dismiss_recommendation(self):
        return self._dismiss

    def reply(self, method, request, kwargs):
        self.owner.mutations.append(h.MutateCall("RecommendationService", method, request, kwargs, False))
        name = "ApplyRecommendationResponse" if method == "apply_recommendation" else "DismissRecommendationResponse"
        response = h.get_ads_type(name)
        for operation in request.operations:
            response.results.append({"resource_name": operation.resource_name})
        return response


class TwoAccountClient(ProjectedClient):
    def __init__(self, *, rec_type="CALLOUT_ASSET", budget=None):
        super().__init__()
        rows = []
        for account in ACCOUNTS:
            for identifier in OPAQUE_IDS:
                recommendation = {"resource_name": f"customers/{account}/recommendations/{identifier}",
                    "type_": rec_type, "campaign": f"customers/{account}/campaigns/111"}
                if budget is not None:
                    recommendation["campaign_budget_recommendation"] = {"recommended_budget_amount_micros": budget}
                rows.append({"recommendation": recommendation, "customer": {"id": int(account), "currency_code": "EUR"}})
        self.stub("recommendation", rows)
        self.filters["recommendation"] = lambda rows, call: [row for row in rows if str(row.customer.id) == call.customer_id]
        self._services["RecommendationService"] = RecommendationServiceClient(transport=RecommendationTransport(self))


def server_for(tmp_path, account, *, rec_type="CALLOUT_ASSET", budget=None):
    client = TwoAccountClient(rec_type=rec_type, budget=budget)
    server = h.build_rw_server(tmp_path, client=client, env={
        "GOOGLE_ADS_CUSTOMER_ID": account, "ADS_MCP_REQUIRE_DRY_RUN": "true"})
    return client, server


def refused_before_account_access(tmp_path, client, payload, code):
    error = h.error_of(payload)
    assert error["code"] == code, payload
    assert not client.searches and not client.mutations and not client.planner_calls()
    assert "plan" not in payload
    assert not any(row.get("event") == "plan_created" for row in h.read_audit_records(tmp_path))


MALFORMED = (
    "", " ", "bad/id", "777/", "/777", "Re c", "Re\tc", "Re\nc",
    "Rec?query", "Rec#fragment", "Rec\x00x", "Rec\x1fx", "Rec\x7fx",
    "customers/{account}/recommendations/", "customers/{account}/recommendations",
    "customers/{account}/recommendations/777/extra",
    "customers/{account}/campaigns/1/recommendations/777",
    "customers/{account}/campaigns/777", "customers//recommendations/777",
    "customers/not-an-account/recommendations/777",
    "customers/{account}/recommendations/Rec A",
    "customers/{account}/recommendations/Rec?query",
    "customers/{account}/recommendations/Rec#fragment",
    "customers/{account}/recommendations/Rec\x00x",
)


@pytest.mark.parametrize("account", ACCOUNTS)
@pytest.mark.parametrize("tool", TOOLS)
@pytest.mark.parametrize("supplied", MALFORMED)
def test_malformed_recommendations_refuse_before_reads_plans_or_writes(tmp_path, account, tool, supplied):
    client, server = server_for(tmp_path, account)
    payload = h.call(server, tool, {"recommendation_id": supplied.format(account=account)})
    refused_before_account_access(tmp_path, client, payload, "INVALID_RECOMMENDATION_ID")


@pytest.mark.parametrize("account", ACCOUNTS)
@pytest.mark.parametrize("tool", TOOLS)
@pytest.mark.parametrize("suffix", ["", "777/extra", "Rec?query", "Rec\x00x"])
def test_foreign_but_malformed_names_have_grammar_error_before_customer_mismatch(tmp_path, account, tool, suffix):
    client, server = server_for(tmp_path, account)
    other = next(value for value in ACCOUNTS if value != account)
    payload = h.call(server, tool, {"recommendation_id": f"customers/{other}/recommendations/{suffix}"})
    refused_before_account_access(tmp_path, client, payload, "INVALID_RECOMMENDATION_ID")


@pytest.mark.parametrize("account", ACCOUNTS)
@pytest.mark.parametrize("tool", TOOLS)
def test_well_formed_foreign_account_still_refuses_before_calls(tmp_path, account, tool):
    client, server = server_for(tmp_path, account)
    other = next(value for value in ACCOUNTS if value != account)
    payload = h.call(server, tool, {"recommendation_id": f" customers/{other}/recommendations/Rec_A-09 "})
    refused_before_account_access(tmp_path, client, payload, "PLAN_CUSTOMER_MISMATCH")


@pytest.mark.parametrize("account", ACCOUNTS)
@pytest.mark.parametrize("tool", TOOLS)
@pytest.mark.parametrize("identifier", OPAQUE_IDS)
def test_bare_and_full_opaque_ids_preserve_identity_in_preview_and_sdk_request(tmp_path, account, tool, identifier):
    client, server = server_for(tmp_path, account)
    resource = f"customers/{account}/recommendations/{identifier}"
    for supplied in (f" \t{identifier}\n ", f" \t{resource}\n "):
        plan = h.expect_ok(h.call(server, tool, {"recommendation_id": supplied}))["plan"]
        assert plan["operations"] == [{"type": tool, "resource": resource}]
        before = len(client.mutations)
        h.expect_error(server, "confirm_and_apply", {"plan_id": plan["id"], "dry_run": False}, code="DRY_RUN_REQUIRED")
        assert len(client.mutations) == before
        preview = h.expect_ok(h.call(server, "confirm_and_apply", {"plan_id": plan["id"], "dry_run": True}))
        assert preview["plan"]["operations"] == plan["operations"]
        assert len(client.mutations) == before
        result = h.expect_ok(h.call(server, "confirm_and_apply", {"plan_id": plan["id"], "dry_run": False}))
        assert result["applied"] and len(client.mutations) == before + 1
        call = client.mutations[-1]
        request_name = "ApplyRecommendationRequest" if tool == "apply_recommendation" else "DismissRecommendationRequest"
        assert call.request._pb.DESCRIPTOR.full_name.endswith("." + request_name)
        assert call.method == tool and call.request.customer_id == account
        assert [operation.resource_name for operation in call.request.operations] == [resource]
        assert dict(call.kwargs["metadata"])["x-goog-request-params"] == f"customer_id={account}"
        parsed = RecommendationServiceClient.parse_recommendation_path(resource)
        assert parsed == {"customer_id": account, "recommendation_id": identifier}
        h.expect_error(server, "confirm_and_apply", {"plan_id": plan["id"], "dry_run": False}, code="PLAN_CONSUMED")
        assert len(client.mutations) == before + 1
    assert all(call.customer_id == account for call in client.searches)


@pytest.mark.parametrize("rec_type,budget,code", [
    ("KEYWORD", None, "SPEND_IMPACT_UNBOUNDED"),
    ("CAMPAIGN_BUDGET", 101_000_000, "BUDGET_CAP_EXCEEDED"),
])
def test_valid_opaque_id_cannot_bypass_existing_spend_or_type_checks(tmp_path, rec_type, budget, code):
    client, server = server_for(tmp_path, ACCOUNTS[0], rec_type=rec_type, budget=budget)
    h.expect_error(server, "apply_recommendation", {"recommendation_id": " Rec_A-09 "}, code=code)
    assert client.searches and not client.mutations
    assert not any(row.get("event") == "plan_created" for row in h.read_audit_records(tmp_path))
