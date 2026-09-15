"""F018: genuine v25 presence, unavailable impressions and fractional clicks."""
from pathlib import Path

import pytest
from google.ads.googleads.v25.services.types.keyword_plan_idea_service import (
    GenerateKeywordForecastMetricsRequest, GenerateKeywordForecastMetricsResponse,
)

import harness as h


class ForecastService:
    def __init__(self, response):
        self.response = response
        self.requests = []

    def generate_keyword_forecast_metrics(self, request):
        assert isinstance(request, GenerateKeywordForecastMetricsRequest)
        self.requests.append(request)
        return self.response


def forecast(tmp_path, account_client, metrics):
    response = GenerateKeywordForecastMetricsResponse()
    if metrics is not None:
        response.campaign_forecast_metrics = metrics
    # Prove the fixture's presence independently of the product projection.
    assert response._pb.HasField("campaign_forecast_metrics") == (metrics is not None)
    for field in ("clicks", "cost_micros", "average_cpc_micros"):
        assert response.campaign_forecast_metrics._pb.HasField(field) == (field in (metrics or {}))
    assert "impressions" not in response.campaign_forecast_metrics._pb.DESCRIPTOR.fields_by_name
    account_client.stub("customer", [{"customer": {"id": int(h.OTHER_CUSTOMER_ID),
        "currency_code": "EUR", "time_zone": "Etc/UTC"}}])
    service = ForecastService(response)
    account_client._services["KeywordPlanIdeaService"] = service
    server = h.build_server(tmp_path, client=account_client)
    payload = h.expect_ok(h.call(server, "get_keyword_forecasts", {
        "keywords": ["synthetic seed"], "customer_id": h.OTHER_CUSTOMER_ID}))
    assert "plan" not in payload and not account_client.mutations
    assert len(service.requests) == 1
    request = service.requests[0]
    assert request.customer_id == payload["customer_id"] == h.OTHER_CUSTOMER_ID
    assert request.currency_code == "EUR"
    assert account_client.login_customer_id == h.LOGIN_CUSTOMER_ID
    assert {s.customer_id for s in account_client.searches} == {h.OTHER_CUSTOMER_ID}
    assert request.campaign.ad_groups[0].keywords[0].text == "synthetic seed"
    assert "confirm_and_apply" not in h.tool_names(server)
    assert set(payload["forecast"]) == {"impressions", "clicks", "cost", "average_cpc"}
    return payload["forecast"]


@pytest.mark.parametrize("metrics", [None, {}, {"clicks": 2.5},
    {"cost_micros": 1500000}, {"average_cpc_micros": 600000},
    {"clicks": 2.5, "cost_micros": 1500000, "average_cpc_micros": 600000}])
def test_impressions_are_unavailable_in_every_valid_sdk_response(tmp_path, account_client, metrics):
    assert forecast(tmp_path, account_client, metrics)["impressions"] is None


@pytest.mark.parametrize("metrics", [None, {}, {"clicks": 2.5},
    {"cost_micros": 1500000}, {"average_cpc_micros": 600000}])
def test_absent_parent_empty_parent_and_individual_absence_are_null(tmp_path, account_client, metrics):
    result = forecast(tmp_path, account_client, metrics)
    expected = {"clicks": None, "cost": None, "average_cpc": None}
    if metrics and "clicks" in metrics:
        expected["clicks"] = 2.5
    if metrics and "cost_micros" in metrics:
        expected["cost"] = "1.50 EUR"
    if metrics and "average_cpc_micros" in metrics:
        expected["average_cpc"] = "0.60 EUR"
    assert {key: result[key] for key in expected} == expected


@pytest.mark.parametrize("clicks", [0, 0.25, 2.5, 480.0])
def test_explicit_zero_and_fractional_clicks_survive(tmp_path, account_client, clicks):
    result = forecast(tmp_path, account_client, {"clicks": clicks, "cost_micros": 0, "average_cpc_micros": 0})
    assert isinstance(result["clicks"], (int, float)) and not isinstance(result["clicks"], bool)
    assert result["clicks"] == clicks
    assert result["cost"] == result["average_cpc"] == "0.00 EUR"


def test_public_docs_explain_unavailable_impressions(tmp_path, account_client):
    server = h.build_server(tmp_path, client=account_client)
    description = h.tool_map(server)["get_keyword_forecasts"].description.lower()
    readme = (Path(__file__).resolve().parents[1] / "README.md").read_text().lower()
    paragraphs = [p for p in readme.split("\n\n") if "impressions" in p]
    assert any("null" in p and any(word in p for word in ("unavailable", "not provide", "does not", "no impressions"))
               for p in paragraphs), "README must explain unavailable impressions alongside their null value"
    for text in (description,):
        assert "impressions" in text and "null" in text
        assert any(word in text for word in ("unavailable", "not provide", "does not", "no impressions"))
