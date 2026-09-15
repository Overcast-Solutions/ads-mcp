"""F028: public mutation validation, boundary controls, and sanitized refusals."""
from copy import deepcopy
import json
import re

import pytest

import harness as h
from tool_catalog import MUTATION_ARGS


def send(server, tool, changes):
    return h.call(server, tool, {**deepcopy(MUTATION_ARGS[tool]), **changes})


def refusal(result, tmp_path, client, code=None):
    error = h.error_of(result)
    if code:
        assert error["code"] == code
    assert re.fullmatch(r"[A-Z][A-Z0-9_]+", error["code"]) and error["code"] != "INTERNAL"
    assert error["message"] and "Traceback" not in json.dumps(result)
    assert not client.searches and not client.mutations and not client.planner_calls()
    audit = h.read_audit_records(tmp_path)
    assert any(r["event"] == "refused" and r["outcome"] == error["code"] for r in audit)
    h.assert_no_secrets(json.dumps(result) + json.dumps(audit))


def applied(server, client, tool, changes):
    plan = h.expect_ok(send(server, tool, changes))["plan"]
    assert not client.mutations
    preview = h.expect_ok(h.call(server, "confirm_and_apply", {"plan_id": plan["id"], "dry_run": True}))
    assert preview["plan"]["operations"] == plan["operations"]
    result = h.expect_ok(h.call(server, "confirm_and_apply", {"plan_id": plan["id"], "dry_run": False}))
    assert result["applied"] and client.live_mutations()
    return [op.create if op._pb.WhichOneof("operation") == "create" else op.update
            for call in client.mutations for op in call.request.operations]


@pytest.mark.parametrize("changes", [
    {"headlines": ["h"] * 2}, {"headlines": ["h"] * 16},
    {"descriptions": ["d"]}, {"descriptions": ["d"] * 5},
    {"headlines": ["界" * 15 + "a", "Short", "Third"]},
    {"descriptions": ["界" * 45 + "a", "Short"]},
    {"path1": "界" * 8}, {"path2": "Ａ" * 7 + "ab"},
])
def test_rsa_counts_and_double_width_fields_refuse_before_calls(tmp_path, account_client, changes):
    server = h.build_rw_server(tmp_path, client=account_client)
    refusal(send(server, "draft_responsive_search_ad", changes), tmp_path, account_client)


def test_rsa_maximum_counts_and_widths_reach_real_request(tmp_path, account_client):
    server = h.build_rw_server(tmp_path, client=account_client)
    changes = {"headlines": ["界" * 14 + f"{i:02}" for i in range(15)],
               "descriptions": ["界" * 44 + f"{i:02}" for i in range(4)],
               "path1": "界" * 7 + "a", "path2": "p" * 15}
    messages = applied(server, account_client, "draft_responsive_search_ad", changes)
    ad = messages[0].ad.responsive_search_ad
    assert [a.text for a in ad.headlines] == changes["headlines"]
    assert [a.text for a in ad.descriptions] == changes["descriptions"]
    assert ad.path1 == changes["path1"] and ad.path2 == changes["path2"]


@pytest.mark.parametrize("tool", ["draft_keywords", "add_negative_keywords", "draft_campaign"])
@pytest.mark.parametrize("text", [" ", "x" * 81, " ".join(["word"] * 11)])
def test_keyword_text_codepoint_and_word_limits(tmp_path, account_client, tool, text):
    server = h.build_rw_server(tmp_path, client=account_client)
    changes = {"keywords": [{"text": text, "match_type": "EXACT"}]}
    if tool == "draft_campaign":
        changes["ad_group_name"] = "Example group"
    refusal(send(server, tool, changes), tmp_path, account_client)


@pytest.mark.parametrize("tool", ["draft_keywords", "add_negative_keywords"])
def test_keywords_keep_eighty_codepoints_and_ten_words(tmp_path, account_client, tool):
    server = h.build_rw_server(tmp_path, client=account_client)
    texts = ["界" * 80, " ".join(["word"] * 10)]
    messages = applied(server, account_client, tool, {"keywords": [{"text": text, "match_type": "EXACT"} for text in texts]})
    assert [m.keyword.text for m in messages] == texts
    assert all(m.keyword.match_type.name == "EXACT" for m in messages)


@pytest.mark.parametrize("detail", [
    {"description1": "first"}, {"description2": "second"},
    {"description1": "", "description2": "second"},
    {"description1": "first", "description2": " "},
    {"description1": "x" * 36, "description2": "second"},
    {"description1": "first", "description2": "x" * 36},
    {"description1": "界" * 18, "description2": "second"},
    {"link_text": "界" * 13},
])
def test_sitelink_description_pair_and_width_limits(tmp_path, account_client, detail):
    server = h.build_rw_server(tmp_path, client=account_client)
    link = {"link_text": "Example", "final_url": "https://example.org", **detail}
    refusal(send(server, "draft_sitelinks", {"sitelinks": [link]}), tmp_path, account_client)


@pytest.mark.parametrize("paired", [False, True])
def test_sitelink_absent_or_at_limit_descriptions_apply(tmp_path, account_client, paired):
    server = h.build_rw_server(tmp_path, client=account_client)
    link = {"link_text": "界" * 12 + "a", "final_url": "https://example.org"}
    if paired:
        link.update(description1="界" * 17 + "a", description2="d" * 35)
    messages = applied(server, account_client, "draft_sitelinks", {"sitelinks": [link]})
    asset = next(m for m in messages if m._pb.DESCRIPTOR.name == "Asset").sitelink_asset
    assert asset.link_text == link["link_text"]
    assert asset.description1 == link.get("description1", "")
    assert asset.description2 == link.get("description2", "")


@pytest.mark.parametrize("values", [["a", "b"], ["a"] * 11, ["a", "b", ""], ["a", "b", " "], ["a", "b", "x" * 26], ["a", "b", "界" * 13]])
def test_snippet_counts_nonblank_values_and_widths(tmp_path, account_client, values):
    server = h.build_rw_server(tmp_path, client=account_client)
    refusal(send(server, "create_structured_snippets", {"values": values}), tmp_path, account_client)


@pytest.mark.parametrize("count", [3, 10])
def test_snippet_boundary_values_apply(tmp_path, account_client, count):
    server = h.build_rw_server(tmp_path, client=account_client)
    values = ["界" * 12 + chr(65 + i) for i in range(count)]
    messages = applied(server, account_client, "create_structured_snippets", {"values": values})
    asset = next(m for m in messages if m._pb.DESCRIPTOR.name == "Asset")
    assert list(asset.structured_snippet_asset.values) == values


def test_callout_double_width_limit_is_audited(tmp_path, account_client):
    server = h.build_rw_server(tmp_path, client=account_client)
    refusal(send(server, "create_callouts", {"callouts": ["界" * 13]}), tmp_path, account_client)


def test_callout_at_width_limit_applies(tmp_path, account_client):
    server = h.build_rw_server(tmp_path, client=account_client)
    messages = applied(server, account_client, "create_callouts", {"callouts": ["界" * 12 + "a"]})
    asset = next(m for m in messages if m._pb.DESCRIPTOR.name == "Asset")
    assert asset.callout_asset.callout_text == "界" * 12 + "a"


@pytest.mark.parametrize("change", [
    {"start_hour": 8.9}, {"start_hour": True}, {"end_hour": False},
    {"start_hour": "garbage"}, {"start_hour": None},
    {"start_minute": 15.5}, {"end_minute": True}, {"start_minute": "garbage"},
    {"start_hour": -1}, {"end_hour": 25}, {"start_minute": 1},
    {"start_hour": 8, "start_minute": 15, "end_hour": 8, "end_minute": 15},
    {"start_hour": 8, "start_minute": 30, "end_hour": 8, "end_minute": 15},
    {"start_hour": 24, "end_hour": 24}, {"end_hour": 24, "end_minute": 15},
])
def test_schedule_quantities_and_complete_interval_are_validated(tmp_path, account_client, change):
    server = h.build_rw_server(tmp_path, client=account_client)
    schedule = {"day_of_week": "MONDAY", "start_hour": 8, "start_minute": 0, "end_hour": 10, "end_minute": 0, **change}
    refusal(send(server, "set_campaign_schedule", {"schedules": [schedule]}), tmp_path, account_client, "INVALID_SCHEDULE")


@pytest.mark.parametrize("start_hour,start_minute,end_hour,end_minute", [(8, 15, 8, 30), (0, 0, 24, 0), (23, 45, 24, 0)])
def test_valid_quarter_hour_and_day_end_schedules_apply(tmp_path, account_client, start_hour, start_minute, end_hour, end_minute):
    server = h.build_rw_server(tmp_path, client=account_client)
    schedule = {"day_of_week": "MONDAY", "start_hour": start_hour, "start_minute": start_minute, "end_hour": end_hour, "end_minute": end_minute}
    messages = applied(server, account_client, "set_campaign_schedule", {"schedules": [schedule]})
    sent = messages[0].ad_schedule
    assert sent.start_hour == start_hour and sent.end_hour == end_hour
    names = {0: "ZERO", 15: "FIFTEEN", 30: "THIRTY", 45: "FORTY_FIVE"}
    assert sent.start_minute.name == names[start_minute] and sent.end_minute.name == names[end_minute]


def bid_args(tool, value):
    if tool == "update_keyword_bid":
        return {"ad_group_id": "201", "criterion_id": "401", "current_bid": 1.2, "new_bid": value}
    if tool in {"create_ad_group", "update_ad_group"}:
        return {"cpc_bid_micros": value}
    changes = {"keywords": [{"text": "widgets", "match_type": "EXACT", "cpc_bid_micros": value}]}
    if tool == "draft_campaign":
        changes["ad_group_name"] = "Example group"
    return changes


@pytest.mark.parametrize("tool,value", [
    ("update_keyword_bid", 1e-8), ("update_keyword_bid", 0.0000009),
    ("draft_keywords", 0.5), ("draft_campaign", 0.5),
    ("create_ad_group", 0), ("update_ad_group", 0),
    ("update_keyword_bid", 0), ("update_keyword_bid", -1),
    ("draft_keywords", -1), ("draft_campaign", -1),
    ("draft_keywords", "NaN"), ("draft_keywords", "Infinity"), ("draft_campaign", "-Infinity"),
])
def test_nonpositive_wire_bids_are_named_audited_before_reads(tmp_path, account_client, tool, value):
    server = h.build_rw_server(tmp_path, client=account_client)
    refusal(send(server, tool, bid_args(tool, value)), tmp_path, account_client, "INVALID_BID")


@pytest.mark.parametrize("tool", ["create_ad_group", "update_ad_group", "update_keyword_bid", "draft_keywords", "draft_campaign"])
def test_one_micro_bid_reaches_every_wire_path(tmp_path, account_client, tool):
    server = h.build_rw_server(tmp_path, client=account_client)
    value = 0.000001 if tool == "update_keyword_bid" else 1
    messages = applied(server, account_client, tool, bid_args(tool, value))
    bids = [m.cpc_bid_micros for m in messages if "cpc_bid_micros" in m._pb.DESCRIPTOR.fields_by_name and m.cpc_bid_micros]
    assert bids == [1]


def test_audit_failure_blocks_an_otherwise_valid_mutation(tmp_path, account_client):
    blocked = tmp_path / "audit-directory"
    blocked.mkdir()
    server = h.build_rw_server(tmp_path, client=account_client, env={"ADS_MCP_AUDIT_LOG": str(blocked)})
    result = send(server, "create_callouts", {"callouts": ["At limit"]})
    assert h.error_of(result)["code"] == "AUDIT_WRITE_FAILED"
    assert not account_client.mutations
