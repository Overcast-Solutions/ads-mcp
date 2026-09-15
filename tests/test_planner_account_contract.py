"""F032: real SDK provider pagination and requested/root account attribution."""
import pytest
from google.api_core.exceptions import PermissionDenied
from google.ads.googleads.errors import GoogleAdsException
from google.ads.googleads.v25.services.services.keyword_plan_idea_service.pagers import GenerateKeywordIdeasPager
from google.ads.googleads.v25.services.types.keyword_plan_idea_service import GenerateKeywordIdeasRequest, GenerateKeywordIdeaResponse
from google.protobuf.json_format import ParseDict

import harness as h
from offline_contract import ProjectedClient


def authorization_error():
    failure = h.get_ads_type("GoogleAdsFailure")
    ParseDict({"errors": [{"error_code": {"authorization_error": "USER_PERMISSION_DENIED"}, "message": "synthetic access denied"}]}, failure._pb)
    return GoogleAdsException(None, None, failure, "synthetic-permission")


class PagedIdeas:
    def __init__(self, pages, *, error=None, fail_page=None):
        self.pages, self.error, self.fail_page = pages, error, fail_page
        self.requests = []

    def generate_keyword_ideas(self, request):
        assert isinstance(request, GenerateKeywordIdeasRequest)
        def page(req, **kwargs):
            index = int(req.page_token or "0")
            self.requests.append((req.customer_id, index))
            if self.error is not None and index == (self.fail_page or 0):
                raise self.error
            return GenerateKeywordIdeaResponse(results=[{"text": text, "keyword_idea_metrics": {"avg_monthly_searches": 20, "competition": "LOW"}}
                for text in self.pages[index]], next_page_token=str(index + 1) if index + 1 < len(self.pages) else "", total_size=sum(map(len, self.pages)))
        return GenerateKeywordIdeasPager(page, request, page(request))

    def generate_keyword_forecast_metrics(self, request):
        self.requests.append((request.customer_id, "forecast"))
        if self.error:
            raise self.error
        return h.get_ads_type("GenerateKeywordForecastMetricsResponse")


@pytest.mark.parametrize("pages", [[["one", "two"], [], ["three", "four", "five"]], [[], ["one"], [], ["two", "three", "four", "five"]]])
def test_real_sdk_ideas_walk_all_provider_pages_without_loss(tmp_path, account_client, pages):
    service = PagedIdeas(pages)
    account_client._services["KeywordPlanIdeaService"] = service
    server = h.build_server(tmp_path, client=account_client, env={"ADS_MCP_ROW_LIMIT": "2"})
    args = {"seed_keywords": ["synthetic"], "customer_id": h.OTHER_CUSTOMER_ID}
    seen, tokens = [], set()
    for _ in range(6):
        payload = h.expect_ok(h.call(server, "discover_keywords", args))
        assert payload["customer_id"] == h.OTHER_CUSTOMER_ID
        assert len(payload["ideas"]) <= 2
        seen += [idea["keyword"] for idea in payload["ideas"]]
        token = payload.get("next_page_token")
        if not token:
            break
        assert token not in tokens
        tokens.add(token)
        args["page_token"] = token
    assert seen == [item for page in pages for item in page]
    assert all(cid == h.OTHER_CUSTOMER_ID for cid, _ in service.requests)
    assert account_client.login_customer_id == h.LOGIN_CUSTOMER_ID
    assert not account_client.mutations


@pytest.mark.parametrize("token", ["garbage", "-1", "1.5", "1 OR 1=1"])
def test_invalid_idea_token_refuses_before_provider(tmp_path, account_client, token):
    service = PagedIdeas([["one"]])
    account_client._services["KeywordPlanIdeaService"] = service
    server = h.build_server(tmp_path, client=account_client)
    h.error_of(h.call(server, "discover_keywords", {"seed_keywords": ["synthetic"], "page_token": token}))
    assert not service.requests and not account_client.searches


@pytest.mark.parametrize("tool,args", [("discover_keywords", {"seed_keywords": ["synthetic"]}), ("get_keyword_forecasts", {"keywords": ["synthetic"]})])
@pytest.mark.parametrize("error", [authorization_error(), PermissionDenied("synthetic account denied")])
def test_both_planner_surfaces_name_requested_account_and_manager(tmp_path, account_client, tool, args, error):
    service = PagedIdeas([["one"]], error=error)
    account_client._services["KeywordPlanIdeaService"] = service
    server = h.build_server(tmp_path, client=account_client)
    err = h.error_of(h.call(server, tool, {**args, "customer_id": h.OTHER_CUSTOMER_ID}))
    assert err["code"] == "ACCOUNT_NOT_ACCESSIBLE"
    assert h.OTHER_CUSTOMER_ID in err["message"] and h.LOGIN_CUSTOMER_ID in err["message"]
    assert len(service.requests) == 1 and service.requests[0][0] == h.OTHER_CUSTOMER_ID
    assert not account_client.mutations


def test_later_provider_page_permission_failure_is_not_partial_success(tmp_path, account_client):
    service = PagedIdeas([["one"], ["two"]], error=authorization_error(), fail_page=1)
    account_client._services["KeywordPlanIdeaService"] = service
    server = h.build_server(tmp_path, client=account_client)
    err = h.error_of(h.call(server, "discover_keywords", {"seed_keywords": ["synthetic"], "customer_id": h.OTHER_CUSTOMER_ID}))
    assert err["code"] == "ACCOUNT_NOT_ACCESSIBLE"
    assert h.OTHER_CUSTOMER_ID in err["message"] and h.LOGIN_CUSTOMER_ID in err["message"]
    assert service.requests == [(h.OTHER_CUSTOMER_ID, 0), (h.OTHER_CUSTOMER_ID, 1)]


@pytest.mark.parametrize("tool,args", [("discover_keywords", {"seed_keywords": ["synthetic"]}), ("get_keyword_forecasts", {"keywords": ["synthetic"]})])
@pytest.mark.parametrize("cid", ["", " ", "bad", "123"])
def test_malformed_explicit_planner_account_never_contacts_transport(tmp_path, account_client, tool, args, cid):
    service = PagedIdeas([["one"]])
    account_client._services["KeywordPlanIdeaService"] = service
    server = h.build_server(tmp_path, client=account_client)
    h.expect_error(server, tool, {**args, "customer_id": cid}, code="INVALID_CUSTOMER_ID")
    assert not service.requests and not account_client.searches


@pytest.mark.parametrize("manager", [h.LOGIN_CUSTOMER_ID, ""])
def test_account_discovery_queries_configured_root_with_two_siblings(tmp_path, manager):
    client = ProjectedClient()
    rows = [{"customer_client": {"id": int(cid), "descriptive_name": label, "currency_code": "EUR", "status": "ENABLED", "manager": False}}
            for cid, label in [(h.CUSTOMER_ID, "First child"), (h.OTHER_CUSTOMER_ID, "Second child")]]
    client.stub("customer_client", rows)
    root = manager or h.CUSTOMER_ID
    client.filters["customer_client"] = lambda items, call: items if call.customer_id == root else items[:1]
    server = h.build_server(tmp_path, client=client, env={"GOOGLE_ADS_LOGIN_CUSTOMER_ID": manager})
    payload = h.expect_ok(h.call(server, "list_accounts"))
    assert client.searches and {s.customer_id for s in client.searches} == {root}
    assert payload["customer_id"] == root
    assert {a["customer_id"] for a in payload["accounts"]} == {h.CUSTOMER_ID, h.OTHER_CUSTOMER_ID}
    if manager:
        assert client.login_customer_id == manager


def test_inaccessible_manager_names_manager_root(tmp_path, account_client):
    account_client.stub_error(authorization_error())
    server = h.build_server(tmp_path, client=account_client)
    err = h.expect_error(server, "list_accounts", code="ACCOUNT_NOT_ACCESSIBLE")
    assert account_client.searches[0].customer_id == h.LOGIN_CUSTOMER_ID
    assert h.LOGIN_CUSTOMER_ID in err["message"]
