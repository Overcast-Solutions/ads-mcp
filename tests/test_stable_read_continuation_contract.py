"""F057: stable, scoped, bounded retained read results via public tool calls.

Provider iterators generate real v25 rows lazily. Pull counts measure client
consumption, not Google network buffers or total Python RSS. No cache class,
private eviction hook, or representation of tokens is prescribed.
"""
from collections import Counter
import csv
from datetime import datetime, timezone
import io
import json
import re
from types import SimpleNamespace

import pytest
from google.ads.googleads.v25.services.services.keyword_plan_idea_service.pagers import GenerateKeywordIdeasPager

import harness as h
from offline_contract import project, selected


MIB = 1024 * 1024
SNAPSHOT_BYTES = 16 * MIB
ROW_CAP = 10000
QUERY = "SELECT campaign.id, campaign.name, metrics.clicks, campaign.maximize_conversions.target_cpa_micros FROM campaign"
RAW = {"query": QUERY, "page_size": 2}
WINDOW = {"last_n_days": 7}
HISTORY = {"date_range_start": "2026-08-01", "date_range_end": "2026-08-01"}
# Every public tool whose current schema contains page_token, including Planner.
CASES = [
    ("run_gaql", "campaign", RAW, "rows", "campaign"),
    ("get_campaign_performance", "campaign", WINDOW, "campaigns", "campaign_id"),
    ("get_ad_performance", "ad_group_ad", WINDOW, "ads", "ad_id"),
    ("get_keyword_performance", "keyword_view", WINDOW, "keywords", "criterion_id"),
    ("get_search_terms", "search_term_view", WINDOW, "search_terms", "search_term"),
    ("get_geo_performance", "geographic_view", WINDOW, "locations", "country_id"),
    ("get_shopping_performance", "shopping_performance_view", {**WINDOW, "campaign_id": "111"}, "products", "item_id"),
    ("get_policy_issues", "ad_group_ad", {"mode": "full"}, "issues", "ad_id"),
    ("get_policy_issues", "asset_group_asset", {"mode": "full"}, "issues", "asset"),
    ("get_policy_issues", "campaign_asset", {"mode": "full"}, "issues", "asset"),
    ("get_change_history", "change_event", HISTORY, "changes", "actor"),
    ("discover_keywords", "ideas", {"seed_keywords": ["synthetic"]}, "ideas", "keyword"),
]


def row_for(resource, cid, index):
    label = f"{cid}-{index}"
    summary = {"approval_status": "DISAPPROVED", "review_status": "REVIEWED",
               "policy_topic_entries": [{"topic": "SYNTHETIC", "type_": "PROHIBITED"}]}
    row = {"customer": {"id": int(cid), "currency_code": "EUR", "time_zone": "Etc/UTC"},
           "campaign": {"id": index if resource == "campaign" else 111, "name": label, "status": "ENABLED"},
           "ad_group": {"id": 201}, "metrics": {"clicks": 0, "impressions": index}}
    if resource == "ad_group_ad":
        row[resource] = {"ad": {"id": index}, "status": "ENABLED", "policy_summary": summary}
    elif resource == "keyword_view":
        row["ad_group_criterion"] = {"criterion_id": index, "status": "ENABLED", "keyword": {"text": label, "match_type": "EXACT"}}
    elif resource == "search_term_view":
        row[resource] = {"search_term": label}
    elif resource == "geographic_view":
        row[resource] = {"country_criterion_id": index}
    elif resource == "shopping_performance_view":
        row["segments"] = {"product_item_id": label, "product_title": "Synthetic product"}
    elif resource == "asset_group_asset":
        row[resource] = {"asset": f"customers/{cid}/assets/{index}", "asset_group": f"customers/{cid}/assetGroups/201",
                         "status": "ENABLED", "field_type": "HEADLINE", "policy_summary": summary}
    elif resource == "campaign_asset":
        row[resource] = {"campaign": f"customers/{cid}/campaigns/111", "asset": f"customers/{cid}/assets/{index}",
                         "status": "ENABLED", "field_type": "SITELINK"}
        row["asset"] = {"policy_summary": summary}
    elif resource == "change_event":
        row[resource] = {"change_date_time": "2026-08-01 12:00:00", "user_email": label + "@example.invalid",
            "client_type": "GOOGLE_ADS_WEB_CLIENT", "change_resource_type": "CAMPAIGN",
            "resource_change_operation": "UPDATE", "campaign": f"customers/{cid}/campaigns/111",
            "changed_fields": "name", "old_resource": {"campaign": {"name": "Before"}},
            "new_resource": {"campaign": {"name": label}}}
    return h.make_row(row)


class LazyProvider(h.FakeGoogleAdsClient):
    def __init__(self, resource="campaign", count=3, names=None):
        super().__init__()
        self.resource, self.count, self.names = resource, count, names
        self.pulls = Counter()
        self.requests = []
        self.generations = Counter()
        self.reorder = True
        self._services["KeywordPlanIdeaService"] = SimpleNamespace(generate_keyword_ideas=self.ideas)

    def order(self, key, count):
        self.generations[key] += 1
        # Same set, distinct order on every repeat, even with identical ties.
        turn = (self.generations[key] - 1) % max(count, 1) if self.reorder else 0
        return (1 + (i + turn) % count for i in range(count))

    def _do_search(self, service, method, args, kwargs):
        cid = str(h._req_field(args, kwargs, "customer_id"))
        query = str(h._req_field(args, kwargs, "query"))
        resource = h._FROM_RE.search(query).group(1)
        self.requests.append((resource, cid, query))
        if resource == "customer":
            self.pulls[resource] += 1
            return [project(row_for("customer", cid, 1), selected(query))]
        if resource != self.resource:
            return iter(())
        count = self.count
        # Emulate the real provider's hard history LIMIT, not a product seam.
        if resource == "change_event":
            match = re.search(r"\bLIMIT\s+(\d+)", query)
            assert match, "history omitted its provider limit"
            count = min(count, int(match.group(1)))
        order = self.order((cid, query), count)
        def rows():
            for index in order:
                self.pulls[resource] += 1
                if self.names is None:
                    row = row_for(resource, cid, index)
                else:
                    row = h.make_row({"campaign": {"name": self.names[index - 1]}})
                yield project(row, selected(query))
        return rows()

    def ideas(self, request):
        cid = request.customer_id
        self.requests.append(("ideas", cid, str(request)))
        order = list(self.order((cid, tuple(request.keyword_seed.keywords), request.url_seed.url), self.count))
        owner = self
        # Genuine SDK pagination; each upstream page contains two ideas.
        def response(offset):
            result = h.get_ads_type("GenerateKeywordIdeaResponse")
            for i in order[offset:offset + 2]:
                item = h.get_ads_type("GenerateKeywordIdeaResult")
                item.text = f"{cid}-{i}"
                item.keyword_idea_metrics.avg_monthly_searches = i
                result.results.append(item)
            if offset + 2 < len(order):
                result.next_page_token = str(offset + 2)
            return result
        def next_page(request, **kwargs):
            owner.requests.append(("ideas_page", cid, request.page_token))
            return response(int(request.page_token))
        class ObservedPager(GenerateKeywordIdeasPager):
            def __iter__(self):
                for item in super().__iter__():
                    owner.pulls["ideas"] += 1
                    yield item
        return ObservedPager(method=next_page, request=request, response=response(0))


def setup(tmp_path, resource="campaign", count=3, size=2, clock=None, names=None):
    provider = LazyProvider(resource, count, names)
    server = h.build_server(tmp_path, client=provider, clock=clock,
        env={"ADS_MCP_ROW_LIMIT": str(size)})
    return server, provider


def footprint(provider):
    return dict(provider.pulls), len(provider.requests)


def denied_before_work(server, provider, tool, args):
    before = footprint(provider)
    error = h.error_of(h.call(server, tool, args))
    assert error["code"] != "INTERNAL" and re.fullmatch(r"[A-Z][A-Z0-9_]+", error["code"])
    assert footprint(provider) == before, "invalid continuation reached provider work"
    assert any(word in error["message"].lower() for word in ("restart", "first page", "new query", "again", "fresh")), (
        "invalid continuation needs recovery guidance"
    )
    return error


def truncation(payload, key):
    assert payload.get("possibly_truncated") is True or payload.get("truncated") is True, (
        "capacity-limited results must disclose truncation on every page"
    )
    meta = json.dumps({k: v for k, v in payload.items() if k != key}).lower()
    assert "narrow" in meta, "truncation must explain narrowing the query/window"


def test_inventory_covers_every_current_paginated_tool(tmp_path, fake_client):
    tools = h.tool_map(h.build_server(tmp_path, client=fake_client))
    actual = {name for name, t in tools.items() if "page_token" in t.input_schema.get("properties", {})}
    assert actual == {case[0] for case in CASES}


@pytest.mark.parametrize("tool,resource,args,key,identity", CASES)
def test_every_category_walk_is_stable_and_continuations_pull_nothing(tmp_path, tool, resource, args, key, identity):
    server, provider = setup(tmp_path, resource)
    first = h.expect_ok(h.call(server, tool, args))
    assert len(first[key]) == 2
    assert first.get("next_page_token"), "fixture must produce a continuation"
    before = footprint(provider)
    second = h.expect_ok(h.call(server, tool, {**args, "page_token": first["next_page_token"]}))
    assert len(second[key]) == 1 and not second.get("next_page_token")
    combined = first[key] + second[key]
    identities = [json.dumps(row[identity], sort_keys=True) for row in combined]
    assert len(set(identities)) == 3, "reordered provider data caused duplication/omission"
    assert footprint(provider) == before, "continuation refetched or pulled provider rows"
    assert all(p["customer_id"] == h.CUSTOMER_ID for p in (first, second))
    if key == "keywords":
        assert [r["quality_score"] for r in combined] == [None] * 3
    if key == "changes":
        assert combined[-1]["changes"]["campaign.name"] == {"old": "Before", "new": f"{h.CUSTOMER_ID}-3"}
    if key in ("campaigns", "ads", "keywords", "search_terms", "locations", "products"):
        assert all(r["clicks"] == 0 and r["cost"] == "0.00 EUR" for r in combined)


@pytest.mark.parametrize("tool,resource,args,key,identity", CASES)
@pytest.mark.parametrize("scope", ["account", "tool", "filter"])
def test_every_category_token_binds_account_tool_and_effective_filter(tmp_path, tool, resource, args, key, identity, scope):
    server, provider = setup(tmp_path, resource)
    token = h.expect_ok(h.call(server, tool, args))["next_page_token"]
    foreign = {**args, "page_token": token}
    if scope == "account":
        foreign["customer_id"] = h.OTHER_CUSTOMER_ID
    elif scope == "tool":
        tool = "get_search_terms" if tool != "get_search_terms" else "get_keyword_performance"
        foreign = {"last_n_days": 7, "page_token": token}
    elif tool == "run_gaql":
        foreign["query"] = QUERY + " WHERE campaign.id = 111"
    elif tool == "discover_keywords":
        foreign["seed_keywords"] = ["different"]
    elif tool == "get_policy_issues":
        foreign["topic"] = "DIFFERENT"
    elif tool == "get_change_history":
        foreign["resource_type"] = "AD_GROUP"
    elif tool == "get_shopping_performance":
        foreign["campaign_id"] = "222"
    else:
        foreign["last_n_days"] = 6
    denied_before_work(server, provider, tool, foreign)


@pytest.mark.parametrize("tool,resource,args,key,identity", CASES)
@pytest.mark.parametrize("token", ["garbage", "1", "-1", "0", "999999999999999999999999", "a.b.c"])
def test_malformed_or_unissued_tokens_never_read_before_refusal(tmp_path, tool, resource, args, key, identity, token):
    server, provider = setup(tmp_path, resource)
    denied_before_work(server, provider, tool, {**args, "page_token": token})


@pytest.mark.parametrize("fmt", ["json", "table", "csv"])
def test_raw_formats_walk_2575_rows_without_field_loss_or_refetch(tmp_path, fmt):
    server, provider = setup(tmp_path, count=2575, size=1000)
    args = {"query": QUERY, "format": fmt}
    seen, tokens = [], set()
    for _ in range(4):
        payload = h.expect_ok(h.call(server, "run_gaql", args))
        if fmt == "json":
            assert payload["fields"] == selected(QUERY)
            rows = payload["rows"]
            assert all(r["metrics"]["clicks"] == 0 and r["campaign"]["maximize_conversions"]["target_cpa_micros"] is None for r in rows)
            ids = [r["campaign"]["id"] for r in rows]
        elif fmt == "table":
            assert payload["columns"] == selected(QUERY)
            assert all(r[2:] == [0, None] for r in payload["rows"])
            ids = [r[0] for r in payload["rows"]]
        else:
            rows = list(csv.reader(io.StringIO(payload["csv"])))
            assert rows[0] == selected(QUERY)
            assert all(r[2:] == ["0", ""] for r in rows[1:])
            ids = [int(r[0]) for r in rows[1:]]
        assert len(ids) == min(1000, 2575 - len(seen))
        seen.extend(ids)
        token = payload.get("next_page_token")
        if not token:
            break
        assert token not in tokens
        tokens.add(token)
        args["page_token"] = token
    assert seen == list(range(1, 2576))
    assert provider.pulls["campaign"] == 2575 and len(provider.requests) == 1


def test_keyword_report_complete_2575_row_walk(tmp_path):
    server, provider = setup(tmp_path, "keyword_view", 2575, size=1000)
    args, seen = dict(WINDOW), []
    for _ in range(4):
        result = h.expect_ok(h.call(server, "get_keyword_performance", args))
        seen += [r["criterion_id"] for r in result["keywords"]]
        if not result.get("next_page_token"):
            break
        args["page_token"] = result["next_page_token"]
    assert seen == [str(i) for i in range(1, 2576)]
    assert provider.pulls["keyword_view"] == 2575 and len(provider.requests) == 1


@pytest.mark.parametrize("count", [9999, 10000, 10001, 20000])
def test_row_capacity_boundary_and_single_lookahead_budget(tmp_path, count):
    server, provider = setup(tmp_path, count=count, size=1000)
    args, seen = {"query": QUERY}, []
    for _ in range(11):
        result = h.expect_ok(h.call(server, "run_gaql", args))
        assert provider.pulls["campaign"] <= min(count, ROW_CAP + 1), "initial retention/continuation exceeded the pull budget"
        if count > ROW_CAP:
            truncation(result, "rows")
        else:
            assert not result.get("possibly_truncated") and not result.get("truncated")
        seen += [r["campaign"]["id"] for r in result["rows"]]
        assert provider.pulls["campaign"] <= min(count, ROW_CAP + 1)
        if not result.get("next_page_token"):
            break
        args["page_token"] = result["next_page_token"]
    assert seen == list(range(1, min(count, ROW_CAP) + 1))
    assert not result.get("next_page_token")
    assert len(provider.requests) == 1


def name_bytes(size, unicode=False):
    """An exact compact UTF-8 JSON projected row size, independently computed."""
    overhead = len(json.dumps({"campaign": {"name": ""}}, separators=(",", ":")).encode())
    available = size - overhead
    value = ("é" * (available // 2) + "x" * (available % 2)) if unicode else "x" * available
    assert len(json.dumps({"campaign": {"name": value}}, ensure_ascii=False, separators=(",", ":")).encode()) == size
    return value


@pytest.mark.parametrize("delta", [-1, 0, 1])
@pytest.mark.parametrize("unicode", [False, True])
def test_exact_projected_byte_boundary_and_one_lookahead(tmp_path, delta, unicode):
    names = [name_bytes(8 * MIB, unicode), name_bytes(8 * MIB + delta, unicode), "tail"]
    server, provider = setup(tmp_path, count=3, size=1, names=names)
    args = {"query": "SELECT campaign.name FROM campaign"}
    first = h.expect_ok(h.call(server, "run_gaql", args))
    assert first["rows"] == [{"campaign": {"name": names[0]}}]
    truncation(first, "rows")
    assert provider.pulls["campaign"] == (2 if delta > 0 else 3), "provider must stop at first unretained row"
    if delta > 0:
        assert not first.get("next_page_token")
    else:
        second = h.expect_ok(h.call(server, "run_gaql", {**args, "page_token": first["next_page_token"]}))
        assert second["rows"] == [{"campaign": {"name": names[1]}}]
        assert not second.get("next_page_token")
        truncation(second, "rows")
        assert provider.pulls["campaign"] == 3


def test_oversized_first_row_is_truncated_empty_and_cannot_loop(tmp_path):
    server, provider = setup(tmp_path, count=2, size=1,
        names=[name_bytes(SNAPSHOT_BYTES + 1), "small"])
    result = h.expect_ok(h.call(server, "run_gaql", {"query": "SELECT campaign.name FROM campaign"}))
    assert result["rows"] == [] and not result.get("next_page_token")
    truncation(result, "rows")
    assert provider.pulls["campaign"] == 1, "oversized first row establishes the bound immediately"


def test_count_capacity_is_16_with_oldest_created_eviction_not_lru(tmp_path):
    clock = h.FakeClock()
    server, provider = setup(tmp_path, count=6, clock=clock)
    starts = []
    for i in range(16):
        args = {**RAW, "query": QUERY + f" WHERE campaign.id > {i}"}
        starts.append((args, h.expect_ok(h.call(server, "run_gaql", args))["next_page_token"]))
        clock.advance(1)
    # All 16 survive at capacity; revisiting the oldest must not make it newest.
    for index in reversed(range(len(starts))):
        args, token = starts[index]
        page = h.expect_ok(h.call(server, "run_gaql", {**args, "page_token": token}))
        starts[index] = (args, page["next_page_token"])
    h.expect_ok(h.call(server, "run_gaql", {**RAW, "query": QUERY + " WHERE campaign.id > 99"}))
    denied_before_work(server, provider, "run_gaql", {**starts[0][0], "page_token": starts[0][1]})
    for args, token in starts[1:]:
        h.expect_ok(h.call(server, "run_gaql", {**args, "page_token": token}))


@pytest.mark.parametrize("snapshots", [4, 5])
def test_aggregate_64_mib_evicts_oldest_snapshot_through_public_tokens(tmp_path, snapshots):
    # Tiny first page leaves >64 MiB unconsumed across five snapshots, even
    # if consumed rows are discarded. The count bound cannot explain eviction.
    clock = h.FakeClock()
    small = len(json.dumps({"campaign": {"name": "first"}}, separators=(",", ":")).encode())
    names = ["first", name_bytes(8 * MIB), name_bytes(8 * MIB - small)]
    server, provider = setup(tmp_path, count=3, size=1, names=names, clock=clock)
    starts = []
    for i in range(snapshots):
        args = {"query": f"SELECT campaign.name FROM campaign WHERE campaign.id > {i}"}
        result = h.expect_ok(h.call(server, "run_gaql", args))
        starts.append((args, result["next_page_token"]))
        clock.advance(1)
    if snapshots == 5:
        denied_before_work(server, provider, "run_gaql", {**starts[0][0], "page_token": starts[0][1]})
    before = footprint(provider)
    for args, token in starts[1 if snapshots == 5 else 0:]:
        page = h.expect_ok(h.call(server, "run_gaql", {**args, "page_token": token}))
        assert page["rows"] == [{"campaign": {"name": names[1]}}]
    assert footprint(provider) == before


@pytest.mark.parametrize("elapsed,expired", [(299.999, False), (300, True), (301, True)])
def test_expiry_is_absolute_300_seconds_and_access_does_not_refresh_it(tmp_path, elapsed, expired):
    clock = h.FakeClock()
    server, provider = setup(tmp_path, count=6, clock=clock)
    first = h.expect_ok(h.call(server, "run_gaql", RAW))
    clock.advance(150)
    args = {**RAW, "page_token": first["next_page_token"]}
    middle = h.expect_ok(h.call(server, "run_gaql", args))
    args["page_token"] = middle["next_page_token"]
    clock.advance(elapsed - 150)
    if expired:
        denied_before_work(server, provider, "run_gaql", args)
    else:
        before = footprint(provider)
        h.expect_ok(h.call(server, "run_gaql", args))
        assert footprint(provider) == before


@pytest.mark.parametrize("change", ["size", "window", "enabled", "policy_mode", "planner_url"])
def test_additional_effective_scope_binding(tmp_path, change):
    clock = h.FakeClock(datetime(2026, 8, 2, 23, 59, tzinfo=timezone.utc).timestamp())
    tool, resource, args = "run_gaql", "campaign", dict(RAW)
    if change in ("window", "enabled"):
        tool, args = "get_campaign_performance", dict(WINDOW)
    elif change == "policy_mode":
        tool, resource, args = "get_policy_issues", "ad_group_ad", {"mode": "full"}
    elif change == "planner_url":
        tool, resource, args = "discover_keywords", "ideas", {"seed_keywords": ["synthetic"]}
    server, provider = setup(tmp_path, resource, clock=clock)
    args["page_token"] = h.expect_ok(h.call(server, tool, args))["next_page_token"]
    if change == "size":
        args["page_size"] = 1
    elif change == "window":
        clock.advance(120)  # Same relative spelling, new effective UTC dates.
    elif change == "enabled":
        args["enabled_only"] = True
    elif change == "policy_mode":
        args["mode"] = "summary"
    else:
        args["page_url"] = "https://example.invalid/"
    denied_before_work(server, provider, tool, args)


def test_interleaved_accounts_and_queries_retain_independent_walks(tmp_path):
    server, provider = setup(tmp_path)
    walks = [{**RAW, "customer_id": cid, "query": QUERY + suffix}
             for cid in (h.CUSTOMER_ID, h.OTHER_CUSTOMER_ID)
             for suffix in ("", " WHERE campaign.status = 'ENABLED'")]
    first = [h.expect_ok(h.call(server, "run_gaql", args)) for args in walks]
    assert len({p["next_page_token"] for p in first}) == 4, "tokens cannot alias across accounts/queries"
    before = footprint(provider)
    for args, head in zip(reversed(walks), reversed(first)):
        tail = h.expect_ok(h.call(server, "run_gaql", {**args, "page_token": head["next_page_token"]}))
        rows = head["rows"] + tail["rows"]
        assert [r["campaign"]["name"] for r in rows] == [f"{args['customer_id']}-{i}" for i in (1, 2, 3)]
    assert footprint(provider) == before


def test_snapshot_does_not_cache_authenticated_health_or_budget_rechecks(tmp_path, account_client):
    server = h.build_rw_server(tmp_path, client=account_client, env={"ADS_MCP_ROW_LIMIT": "1"})
    query = "SELECT campaign.id, campaign.campaign_budget, campaign_budget.amount_micros FROM campaign"
    h.expect_ok(h.call(server, "run_gaql", {"query": query}))
    before = len(account_client.searches)
    h.expect_ok(h.call(server, "health_check"))
    h.expect_ok(h.call(server, "health_check"))
    assert len(account_client.searches) >= before + 2
    plan = h.expect_ok(h.call(server, "update_campaign", {"campaign_id": "111", "daily_budget": 40.0}))["plan"]
    for rows in account_client._responses.values():
        for row in rows:
            if row.campaign.id == 111:
                row.campaign.campaign_budget = f"customers/{h.CUSTOMER_ID}/campaignBudgets/999"
    before = len(account_client.searches)
    error = h.error_of(h.call(server, "confirm_and_apply", {"plan_id": plan["id"], "dry_run": False}))
    assert error["code"] != "INTERNAL"
    assert len(account_client.searches) > before and not account_client.mutations


@pytest.mark.parametrize("count", [999, 1000, 1001])
def test_history_stricter_cap_warning_persists_without_continuation_refetch(tmp_path, count):
    server, provider = setup(tmp_path, "change_event", count, size=333)
    args, seen = dict(HISTORY), []
    for _ in range(5):
        payload = h.expect_ok(h.call(server, "get_change_history", args))
        if count >= 1000:
            assert payload.get("possibly_truncated") is True
            assert "narrow" in json.dumps({k: v for k, v in payload.items() if k != "changes"}).lower()
        seen += [r["actor"] for r in payload["changes"]]
        if not payload.get("next_page_token"):
            break
        args["page_token"] = payload["next_page_token"]
    assert seen == [f"{h.CUSTOMER_ID}-{i}@example.invalid" for i in range(1, min(1000, count) + 1)]
    assert provider.pulls["change_event"] == min(1000, count)
    assert all(re.search(r"LIMIT\s+1000", q) for r, c, q in provider.requests if r == "change_event")


@pytest.mark.parametrize("delta", [-1, 0, 1])
def test_byte_limit_does_not_falsely_mark_complete_prefix_truncated(tmp_path, delta):
    names = [name_bytes(8 * MIB), name_bytes(8 * MIB + delta)]
    server, provider = setup(tmp_path, count=2, size=1, names=names)
    args = {"query": "SELECT campaign.name FROM campaign"}
    first = h.expect_ok(h.call(server, "run_gaql", args))
    assert first["rows"] == [{"campaign": {"name": names[0]}}]
    if delta > 0:
        truncation(first, "rows")
        assert not first.get("next_page_token")
    else:
        assert not first.get("possibly_truncated") and not first.get("truncated")
        second = h.expect_ok(h.call(server, "run_gaql", {**args, "page_token": first["next_page_token"]}))
        assert second["rows"] == [{"campaign": {"name": names[1]}}]
        assert not second.get("possibly_truncated") and not second.get("truncated")
        assert not second.get("next_page_token")
    assert provider.pulls["campaign"] == 2 and len(provider.requests) == 1


def test_policy_expansion_is_bounded_by_projected_issues_not_provider_row_count(tmp_path):
    provider = LazyProvider("ad_group_ad", 1)
    original = provider._do_search
    row = row_for("ad_group_ad", h.CUSTOMER_ID, 1)
    row.ad_group_ad.policy_summary.policy_topic_entries.clear()
    for i in range(10002):
        entry = h.get_ads_type("PolicyTopicEntry")
        entry.topic = f"synthetic-topic-{i}"
        entry.type_ = "PROHIBITED"
        row.ad_group_ad.policy_summary.policy_topic_entries.append(entry)
    def search(service, method, args, kwargs):
        query = str(h._req_field(args, kwargs, "query"))
        if "FROM ad_group_ad" in query:
            provider.requests.append(("ad_group_ad", h.CUSTOMER_ID, query))
            provider.pulls["ad_group_ad"] += 1
            return iter([project(row, selected(query))])
        return original(service, method, args, kwargs)
    provider._do_search = search
    server = h.build_server(tmp_path, client=provider, env={"ADS_MCP_ROW_LIMIT": "1000"})
    args, seen = {"mode": "full"}, []
    for _ in range(11):
        result = h.expect_ok(h.call(server, "get_policy_issues", args))
        truncation(result, "issues")
        seen += [r["topic"] for r in result["issues"]]
        if not result.get("next_page_token"):
            break
        args["page_token"] = result["next_page_token"]
    assert seen == [f"synthetic-topic-{i}" for i in range(10000)]
    assert provider.pulls["ad_group_ad"] == 1


def test_retained_reads_do_not_authorize_stale_keyword_bid_baselines(tmp_path, account_client):
    server = h.build_rw_server(tmp_path, client=account_client, env={"ADS_MCP_MAX_BID_INCREASE_PCT": "100"})
    query = "SELECT ad_group.id, ad_group.cpc_bid_micros FROM ad_group WHERE ad_group.id = 201"
    h.expect_ok(h.call(server, "run_gaql", {"query": query}))
    plan = h.expect_ok(h.call(server, "draft_keywords", {"ad_group_id": "201", "keywords": [
        {"text": "synthetic", "match_type": "EXACT", "cpc_bid_micros": 1500000}]}))["plan"]
    for row in account_client._responses["ad_group"]:
        row.ad_group.cpc_bid_micros = 100000
    before = len(account_client.searches)
    error = h.error_of(h.call(server, "confirm_and_apply", {"plan_id": plan["id"], "dry_run": False}))
    assert error["code"] == "BID_CAP_EXCEEDED"
    assert len(account_client.searches) > before and not account_client.mutations


def test_continuations_are_local_to_originating_server_and_detect_tampering(tmp_path):
    one, provider_one = setup(tmp_path)
    two, provider_two = setup(tmp_path)
    token = h.expect_ok(h.call(one, "run_gaql", RAW))["next_page_token"]
    denied_before_work(two, provider_two, "run_gaql", {**RAW, "page_token": token})
    denied_before_work(one, provider_one, "run_gaql", {**RAW, "page_token": token[:-1] + "!"})


def test_simultaneous_account_query_walks_share_no_rows_or_continuations(tmp_path):
    import asyncio
    import mcp
    server, provider = setup(tmp_path)
    walks = [{**RAW, "customer_id": cid, "query": QUERY + suffix}
             for cid in (h.CUSTOMER_ID, h.OTHER_CUSTOMER_ID)
             for suffix in ("", " WHERE campaign.status = 'ENABLED'")]
    async def drive():
        async with mcp.Client(server) as client:
            heads = [h.expect_ok(h.payload_of(r)) for r in await asyncio.gather(
                *(client.call_tool("run_gaql", args) for args in walks))]
            before = footprint(provider)
            tails = [h.expect_ok(h.payload_of(r)) for r in await asyncio.gather(
                *(client.call_tool("run_gaql", {**args, "page_token": head["next_page_token"]})
                  for args, head in zip(walks, heads)))]
            for args, head, tail in zip(walks, heads, tails):
                assert [r["campaign"]["name"] for r in head["rows"] + tail["rows"]] == [f"{args['customer_id']}-{i}" for i in (1, 2, 3)]
            assert footprint(provider) == before
    h.run(drive())
