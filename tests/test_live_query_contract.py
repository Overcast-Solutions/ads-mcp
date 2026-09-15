"""F023: queries accepted by Google's captured v25 field metadata.

This bounded transport validates the affected SELECT field families and
projects real GoogleAdsRow protos to the selected fields. A tool cannot pass
by deleting the invalid selections and receiving unselected fixture data.
The historical brand/recommendation golden payloads remain authoritative.
"""

import copy
import json
import re
from pathlib import Path

import pytest
from google.protobuf import json_format

import harness


METADATA = json.loads((Path(__file__).parent / "fixtures" / "google_ads_fields_v25.json").read_text())
SELECTABLE = {f["name"] for f in METADATA["fields"] if f["selectable"]}
BOUNDED_FAMILIES = (
    "recommendation.impact",
    "recommendation.campaign_budget_recommendation",
    "asset_group_listing_group_filter.case_value",
)
REPORT_RESOURCES = {"recommendation", "asset_group_listing_group_filter"}
SELECT_RE = re.compile(r"\bSELECT\s+(.*?)\s+FROM\s+", re.I | re.S)
PROTO_FIELD_ALIASES = {
    "recommendation.type": "recommendation.type_",
    "asset_group_listing_group_filter.type": "asset_group_listing_group_filter.type_",
}


def _select_fields(query):
    match = SELECT_RE.search(query)
    assert match, f"report did not issue a SELECT query: {query}"
    return [field.strip() for field in match.group(1).split(",")]


def _project_row(row, fields):
    source = json_format.MessageToDict(row._pb, preserving_proto_field_name=True)
    projected = {}
    for field in fields:
        # GAQL uses the public name; proto JSON preserves the SDK spelling.
        parts = PROTO_FIELD_ALIASES.get(field, field).split(".")
        value = source
        for part in parts:
            if not isinstance(value, dict) or part not in value:
                break
            value = value[part]
        else:
            dest = projected
            for part in parts[:-1]:
                dest = dest.setdefault(part, {})
            dest[parts[-1]] = copy.deepcopy(value)
    return harness.make_row(projected)


@pytest.mark.parametrize("resource,values", [
    ("recommendation", {
        "type_": "CAMPAIGN_BUDGET",
        "impact": {"base_metrics": {"clicks": 40}},
    }),
    ("asset_group_listing_group_filter", {
        "type_": "UNIT_INCLUDED",
        "case_value": {"product_type": {"value": "Outdoor equipment", "level": "LEVEL3"}},
    }),
])
def test_projection_preserves_selected_public_type_alias_only(resource, values):
    row = harness.make_row({resource: values, "customer": {"currency_code": "USD"}})
    projected = _project_row(row, [f"{resource}.type"])
    expected = harness.make_row({resource: {"type_": values["type_"]}})
    assert projected._pb == expected._pb


@pytest.mark.parametrize("resource,values,selected,expected", [
    ("recommendation", {
        "type_": "CAMPAIGN_BUDGET",
        "impact": {
            "base_metrics": {"clicks": 40, "impressions": 1200},
            "potential_metrics": {"clicks": 70, "conversions": 4.5},
        },
    }, ["impact.base_metrics.clicks", "impact.potential_metrics.conversions"], {
        "impact": {
            "base_metrics": {"clicks": 40},
            "potential_metrics": {"conversions": 4.5},
        },
    }),
    ("asset_group_listing_group_filter", {
        "type_": "UNIT_INCLUDED",
        "id": 9002,
        "case_value": {"product_type": {"value": "Outdoor equipment", "level": "LEVEL3"}},
    }, ["case_value.product_type.value"], {
        "case_value": {"product_type": {"value": "Outdoor equipment"}},
    }),
])
def test_projection_keeps_nested_selections_without_unselected_fields(
        resource, values, selected, expected):
    row = harness.make_row({resource: values, "customer": {"currency_code": "USD"}})
    projected = _project_row(row, [f"{resource}.{field}" for field in selected])
    assert projected._pb == harness.make_row({resource: expected})._pb


class MetadataGoogleAdsClient(harness.FakeGoogleAdsClient):
    """Validate captured families; unrelated GAQL remains outside this fixture."""

    def __init__(self):
        super().__init__()
        self.rejected_fields = []

    def _do_search(self, service, method, args, kwargs):
        rows = super()._do_search(service, method, args, kwargs)
        query = self.searches[-1].query
        resource = harness._FROM_RE.search(query).group(1)
        if resource not in REPORT_RESOURCES:
            return rows
        fields = _select_fields(query)
        unsupported = [f for f in fields
                       if "*" in f or (
                           any(f == family or f.startswith(family + ".")
                               or family.startswith(f + ".")
                               for family in BOUNDED_FAMILIES)
                           and f not in SELECTABLE)]
        self.rejected_fields.extend(unsupported)
        if unsupported:
            raise harness.make_google_ads_exception([
                "Unsupported SELECT fields in captured Google Ads v25 metadata: "
                + ", ".join(unsupported)])
        return [_project_row(row, fields) for row in rows]


def _read(tmp_path, fixture):
    assert METADATA["api_version"] == harness.API_VERSION, (
        "API changed: recapture field metadata before relying on this oracle")
    client = MetadataGoogleAdsClient()
    for resource, rows in fixture["gaql"].items():
        client.stub(resource, rows)
    server = harness.build_server(tmp_path, client=client)
    payload = harness.call(server, fixture["tool"], fixture["args"])
    assert not client.rejected_fields, (
        f"{fixture['tool']} selected fields rejected by Google's v25 metadata: "
        f"{client.rejected_fields}")
    harness.expect_ok(payload)
    assert client.searches, "read tool returned data without querying the account"
    assert client.live_mutations() == []
    return payload


@pytest.mark.parametrize("tool", ["list_recommendations", "get_listing_groups"])
def test_supported_queries_preserve_existing_golden_payload(tmp_path, tool):
    fixture = harness.load_contract_fixture(harness.CONTRACT_DIR / f"{tool}.json")
    payload = _read(tmp_path, fixture)
    assert payload == fixture["golden"], (
        "fixing SELECT compatibility must preserve the original typed payload")


# Real v25 protobuf dimension values, entirely synthetic. Multi-field
# dimensions must retain their discriminator as well as their value.
DIMENSIONS = [
    ("product_category", {"category_id": 1234, "level": "LEVEL2"}),
    ("product_type", {"value": "Outdoor equipment", "level": "LEVEL3"}),
    ("product_condition", {"condition": "NEW"}),
    ("product_channel", {"channel": "ONLINE"}),
    ("product_custom_attribute", {"value": "seasonal", "index": "INDEX2"}),
]


@pytest.mark.parametrize("kind,details", DIMENSIONS, ids=[d[0] for d in DIMENSIONS])
def test_nonbrand_listing_dimensions_retain_values_and_discriminators(tmp_path, kind, details):
    fixture = copy.deepcopy(harness.load_contract_fixture(
        harness.CONTRACT_DIR / "get_listing_groups.json"))
    fixture["gaql"]["asset_group_listing_group_filter"][1][
        "asset_group_listing_group_filter"]["case_value"] = {kind: details}
    payload = _read(tmp_path, fixture)
    assert payload["customer_id"] == harness.CUSTOMER_ID
    assert payload["campaign_id"] == "111"
    groups = payload["asset_groups"]
    assert len(groups) == 1 and groups[0]["asset_group_id"] == "501"
    nodes = {n["filter_id"]: n for n in groups[0]["nodes"]}
    assert nodes["9001"]["dimension"] is None
    assert nodes["9002"]["parent_filter_id"] == "9001"
    assert nodes["9002"]["type"] == "UNIT_INCLUDED"
    dimension = nodes["9002"]["dimension"]
    assert isinstance(dimension, dict) and kind in dimension, (
        f"{kind} dimension was dropped: {dimension}")
    actual = dimension[kind]
    # Single-value dimensions may follow the historical flat brand shape.
    # For multiple fields the named value and qualifier must both survive.
    if len(details) == 1 and not isinstance(actual, dict):
        assert actual == next(iter(details.values()))
    else:
        assert isinstance(actual, dict), f"{kind} lost its qualifiers: {actual}"
        for key, expected in details.items():
            assert key in actual, f"{kind}.{key} omitted: {actual}"
            if key == "category_id":
                assert str(actual[key]) == str(expected)
            else:
                assert actual[key] == expected
