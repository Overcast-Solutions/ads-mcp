"""Independent synthetic field-metadata oracle for get_resource_metadata and run_gaql hints.

The provider substitute is tests/harness.py's GoogleAdsFieldService stub: it
serves genuine v25 GoogleAdsField messages from the public-safe synthetic
catalogue in fixtures/resource_metadata_fields_v25.json, projects only the
selected attributes, and records every request. Expected payloads here are
computed from the fixture alone, never from product output.
"""
from __future__ import annotations

import difflib
import json
from pathlib import Path
import re

import harness as h

ROOT = Path(__file__).resolve().parents[1]
CATALOGUE_PATH = ROOT / "tests/fixtures/resource_metadata_fields_v25.json"
GOLDEN_PATH = ROOT / "tests/fixtures/resource_metadata/get_resource_metadata.json"
TOOL = "get_resource_metadata"
SIGNATURE = (["name", "page_size", "page_token"], ["name"])
NAME_RULE = re.compile(r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*")
DETAIL_KEYS = {"name", "category", "data_type", "selectable", "filterable", "sortable",
               "is_repeated", "enum_values", "enum_values_truncated"}
RESOURCE_KEYS = {"name", "fields", "attribute_resources", "segments", "metrics", "next_page_token"}
FIELD_KEYS = {"name", "fields", "next_page_token"}
MAX_REQUESTS = 3
DEADLINE_SECONDS = 10.0
NEW_SOURCE = ["tests/test_resource_metadata_contract.py", "tests/resource_metadata_oracle.py",
              "tests/fixtures/resource_metadata_fields_v25.json",
              "tests/fixtures/resource_metadata/get_resource_metadata.json"]


def catalogue() -> list[dict]:
    return json.loads(CATALOGUE_PATH.read_text())["fields"]


def by_name(fields=None) -> dict:
    return {item["name"]: item for item in (fields if fields is not None else catalogue())}


def passes_name_rule(value) -> bool:
    return isinstance(value, str) and 1 <= len(value) <= 128 and NAME_RULE.fullmatch(value) is not None


def family(top: str, names) -> list[str]:
    return [name for name in names if name == top or name.startswith(top + ".")]


def detail(item: dict) -> dict:
    values = list(item.get("enum_values", []))
    return {
        "name": item["name"], "category": item["category"], "data_type": item["data_type"],
        "selectable": bool(item.get("selectable", False)), "filterable": bool(item.get("filterable", False)),
        "sortable": bool(item.get("sortable", False)), "is_repeated": bool(item.get("is_repeated", False)),
        "enum_values": values[:100], "enum_values_truncated": len(values) > 100,
    }


def expected_fields(name: str, fields=None) -> list[dict]:
    """Detail entries a lookup of ``name`` must return, sorted by name."""
    table = by_name(fields)
    item = table.get(name)
    if item is not None and item["category"] == "RESOURCE":
        chosen = [f for n, f in table.items() if n.startswith(name + ".") and f["category"] == "ATTRIBUTE"]
    elif item is not None:
        chosen = [item]
    else:
        chosen = [f for n, f in table.items() if n.startswith(name + ".")]
    return [detail(f) for f in sorted(chosen, key=lambda f: f["name"])]


def expected_names(resource: str, fields=None) -> dict:
    item = by_name(fields)[resource]
    selectable_with = list(item.get("selectable_with", []))
    return {
        "attribute_resources": sorted(item.get("attribute_resources", [])),
        "segments": sorted(n for n in selectable_with if n.startswith("segments.")),
        "metrics": sorted(n for n in selectable_with if n.startswith("metrics.")),
    }


def close_candidates(word: str, top: str, fields=None) -> set[str]:
    """Names from the same top-level family with ratio >= 0.6 in either order."""
    names = family(top, by_name(fields))
    return {n for n in names if max(difflib.SequenceMatcher(None, n, word).ratio(),
                                    difflib.SequenceMatcher(None, word, n).ratio()) >= 0.6}


def provider(fields=None, *, account=True):
    client = h.FakeGoogleAdsClient()
    if account:
        h.stub_standard_account(client)
    return client.stub_fields(fields if fields is not None else catalogue())


def call_result(server, args):
    result = h.call_result(server, TOOL, args)
    text = h.result_text(result)
    assert not (result.is_error and "Unknown tool" in text), (
        f"{TOOL} is not registered on this server")
    return result


def metadata(server, args) -> dict:
    return h.payload_of(call_result(server, args))


def requests_since(client, start=0):
    return client.field_searches[start:]


def assert_bounded_requests(client, start, tops, *, universe=None, at_most=MAX_REQUESTS):
    """Every field request is a prefix-limited SearchGoogleAdsFields with a deadline."""
    calls = requests_since(client, start)
    assert not client.unsupported_field_queries, (
        "the synthetic field service could not evaluate these queries: "
        f"{client.unsupported_field_queries}")
    assert 1 <= len(calls) <= at_most, f"expected 1..{at_most} field-service requests, saw {len(calls)}"
    fields = [h.make_field(item) for item in (universe if universe is not None else catalogue())]
    allowed = {name for top in tops for name in family(top, [f.name for f in fields])}
    for call in calls:
        assert call.method == "search_google_ads_fields", f"unexpected field-service method {call.method}"
        assert isinstance(call.timeout, (int, float)) and not isinstance(call.timeout, bool), (
            f"field request has no numeric deadline: {call.timeout!r}")
        assert 0 < call.timeout <= DEADLINE_SECONDS, f"field request timeout {call.timeout!r} exceeds 10 s"
        matched = {f.name for f in h.field_query_matches(call.query, fields)}
        assert matched <= allowed, {
            "query": call.query, "outside_requested_prefix": sorted(matched - allowed)[:10]}
    return calls


def assert_detail_shape(entries):
    for entry in entries:
        assert set(entry) == DETAIL_KEYS, sorted(set(entry) ^ DETAIL_KEYS)
        assert passes_name_rule(entry["name"])
        assert isinstance(entry["enum_values"], list) and len(entry["enum_values"]) <= 100
        assert type(entry["enum_values_truncated"]) is bool


# Appended to test_auth_cause_contract.INJECTION: the installed console keeps
# its real configuration, MCP dispatch and tool code; only the SDK client
# factory is synthetic, now also serving the field catalogue.
INSTALLED_FIELDS_INJECTION = r'''
transport.stub_fields(json.loads(Path(os.environ["ORACLE_FIELDS"]).read_text())["fields"])
def _field_hook(call):
    mark("field", method=call.method, query=call.query,
         timeout=call.timeout if isinstance(call.timeout, (int, float)) else repr(call.timeout))
transport.field_hook = _field_hook
'''
