"""F040: reject unusable destinations before staging; preserve real SDK URLs."""
from copy import deepcopy
import json
import re
import socket

import pytest
from google.protobuf.json_format import MessageToDict

import harness as h
from tool_catalog import MUTATION_ARGS


TOOLS = ["draft_responsive_search_ad", "draft_sitelinks"]
VALID = "https://example.org/catalog?a=one%20two&b=3#details"


@pytest.fixture(autouse=True)
def no_destination_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("creative URL validation must never fetch a destination")
    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)


def arguments(tool, url):
    args = deepcopy(MUTATION_ARGS[tool])
    if tool == "draft_responsive_search_ad":
        args["final_url"] = url
    else:
        args["sitelinks"] = [{"link_text": "First destination", "final_url": VALID},
            {"link_text": "Second destination", "final_url": url}]
    return args


def refuse(server, client, tmp_path, tool, url):
    payload = h.call(server, tool, arguments(tool, url))
    error = h.error_of(payload)
    assert re.fullmatch(r"[A-Z][A-Z0-9_]+", error["code"]) and error["code"] != "INTERNAL"
    assert error["message"] and any(word in error["message"].lower() for word in ("url", "http", "destination"))
    assert "plan" not in payload
    assert not client.searches and not client.mutations and not client.planner_calls()
    records = h.read_audit_records(tmp_path)
    assert not any(row.get("event") == "plan_created" for row in records)
    assert "Traceback" not in json.dumps(payload)
    h.assert_no_secrets(json.dumps(payload))


@pytest.mark.parametrize("tool", TOOLS)
@pytest.mark.parametrize("url", ["", "   ", "/relative/path", "example.org/path", "not-a-url",
    "ftp://example.org/file", "javascript:alert(1)", "mailto:person@example.org",
    "https://", "http:///missing-host", "https:///path", "//example.org/path"])
def test_invalid_string_destinations_never_offer_a_plan(tmp_path, account_client, tool, url):
    server = h.build_rw_server(tmp_path, client=account_client)
    refuse(server, account_client, tmp_path, tool, url)


@pytest.mark.parametrize("url", [42, 0, True, {"url": VALID}, [VALID], None])
def test_mcp_nested_sitelink_values_are_validated_as_strings(tmp_path, account_client, url):
    server = h.build_rw_server(tmp_path, client=account_client)
    # list[dict] admits these values through the real MCP schema.
    refuse(server, account_client, tmp_path, "draft_sitelinks", url)


@pytest.mark.parametrize("tool", TOOLS)
@pytest.mark.parametrize("url", [VALID, "http://example.org:8080/path", "HTTPS://EXAMPLE.ORG/Case?x=%2F"])
def test_valid_destinations_survive_preview_and_genuine_sdk_request(tmp_path, account_client, tool, url):
    server = h.build_rw_server(tmp_path, client=account_client)
    args = arguments(tool, url)
    plan = h.expect_ok(h.call(server, tool, args))["plan"]
    assert url in json.dumps(plan["operations"])
    assert not account_client.mutations
    preview = h.expect_ok(h.call(server, "confirm_and_apply", {"plan_id": plan["id"], "dry_run": True}))
    assert preview["plan"]["operations"] == plan["operations"]
    applied = h.expect_ok(h.call(server, "confirm_and_apply", {"plan_id": plan["id"], "dry_run": False}))
    assert applied["applied"]
    urls = []
    for call in account_client.live_mutations():
        assert call.request.customer_id == h.CUSTOMER_ID
        # Conversion requires a genuine protobuf, not a namespace stand-in.
        MessageToDict(call.request._pb, preserving_proto_field_name=True)
        for op in call.request.operations:
            if tool == "draft_responsive_search_ad":
                urls.extend(op.create.ad.final_urls)
            elif call.service == "AssetService":
                urls.extend(op.create.final_urls)
    assert urls == ([url] if tool == "draft_responsive_search_ad" else [VALID, url])
    h.expect_error(server, "confirm_and_apply", {"plan_id": plan["id"], "dry_run": False}, code="PLAN_CONSUMED")


@pytest.mark.parametrize("tool", TOOLS)
def test_valid_url_keeps_text_and_account_safeguards(tmp_path, account_client, tool):
    server = h.build_rw_server(tmp_path, client=account_client)
    args = arguments(tool, VALID)
    args["customer_id"] = h.OTHER_CUSTOMER_ID
    h.error_of(h.call(server, tool, args))
    assert not account_client.searches and not account_client.mutations
    args.pop("customer_id")
    if tool == "draft_responsive_search_ad":
        args["headlines"] = ["too short a collection"]
    else:
        args["sitelinks"][0]["link_text"] = "x" * 26
    h.error_of(h.call(server, tool, args))
    assert not account_client.searches and not account_client.mutations
