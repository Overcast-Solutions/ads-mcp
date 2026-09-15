"""Synthetic Search destination acceptance with real MCP and v25 messages.

Expected payloads and provider reporting permissions are separately authored.
The transport filters and projects rows; it does not implement tool behavior.
"""
from collections import Counter
from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import re
import threading

from google.api_core.exceptions import ServiceUnavailable
from google.protobuf.json_format import MessageToDict

import harness as h
from offline_contract import project, selected, refusal

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests/fixtures/search_urls"
FACTS_PATH = ROOT / "tests/fixtures/search_url_provider_fields_v25.json"
READS = frozenset({"get_responsive_search_ad_urls", "get_keyword_urls"})
WRITES = frozenset({"update_responsive_search_ad_urls", "update_keyword_urls"})
ADDITIONS = READS | WRITES
KINDS = {
    "ad": {"read": "get_responsive_search_ad_urls", "write": "update_responsive_search_ad_urls",
           "id": "ad_id", "resource": "ad_group_ad", "service": "AdService", "method": "mutate_ads",
           "request": "MutateAdsRequest", "path": "ads", "entity": "ad"},
    "keyword": {"read": "get_keyword_urls", "write": "update_keyword_urls",
                "id": "criterion_id", "resource": "ad_group_criterion", "service": "AdGroupCriterionService",
                "method": "mutate_ad_group_criteria", "request": "MutateAdGroupCriteriaRequest",
                "path": "adGroupCriteria", "entity": "keyword"},
}
BEFORE_FINAL = ["https://example.invalid/Original?edition=One#Details"]
BEFORE_MOBILE = ["https://m.example.invalid/Original"]
AFTER_FINAL = ["HTTPS://Example.invalid/Trail?Campaign={campaignid}&Q=A%2FB#Details", "https://example.invalid/trail?edition=two"]
AFTER_MOBILE = ["https://m.example.invalid/Trail?device={device}"]
TRACKING = {"tracking_url_template": "https://track.example.invalid/click?u={lpurl}&v={_edition}",
            "final_url_suffix": "edition=one&source=search", "url_custom_parameters": [{"key": "edition", "value": "One"}]}
BAD_IDS = [None, True, False, 0, -1, 601, 1.25, [], {}, "", "0", "-1", "+601", " 601", "601 ", "01", "٦٠١", "６０１", "601~801", "1e3", "9223372036854775808", "9" * 5000, "customers/9876543210/ads/601"]
BAD_LISTS = [True, False, 0, 1.5, {}, "https://example.invalid/", [None], [True], [3], [[]], [{}],
             [""], [" https://example.invalid/"], ["https://example.invalid/ "], ["ftp://example.invalid/"],
             ["//example.invalid/"], ["https:///missing"], ["https://user:pass@example.invalid/"],
             ["https://@example.invalid/"], ["https://example.invalid:bad/"], ["https://example.invalid:65536/"],
             ["https://example.invalid:-1/"], ["https://example.invalid/a b"],
             ["https://example.invalid/a", "https://example.invalid/a"],
             ["https://example.invalid/" + "a" * 2025],
             [f"https://example.invalid/{i}" for i in range(11)],
             *[["https://example.invalid/a" + char + "b"] for char in ("\x00", "\n", "\r", "\t", "\x1f", "\x7f", "\x85", "\x9f", "\u00a0", "\u2028")]]
SAFETY_CASES = ["foreign_account", "unknown_parameter", "staging_audit", "preview", "expiry", "replay",
                "lost_response", "pre_audit", "terminal_audit", "concurrent"]


def rn(kind, ident, customer=h.CUSTOMER_ID):
    return f"customers/{customer}/{kind}/{ident}"


def args(kind, **extra):
    return {"ad_group_id": "801", KINDS[kind]["id"]: "601", **extra}


def campaign_row(ident=701, customer=h.CUSTOMER_ID):
    return {"customer": {"id": int(customer), "currency_code": "USD"}, "campaign": {
        "id": ident, "resource_name": rn("campaigns", ident, customer), "name": f"Search campaign {ident}",
        "status": "ENABLED", "advertising_channel_type": "SEARCH"}}


def group_row(ident=801, campaign=701, customer=h.CUSTOMER_ID):
    return {**campaign_row(campaign, customer), "ad_group": {"id": ident,
        "resource_name": rn("adGroups", ident, customer), "campaign": rn("campaigns", campaign, customer),
        "name": f"Search group {ident}", "status": "ENABLED", "type_": "SEARCH_STANDARD", "cpc_bid_micros": 1200000}}


def ad_row(ident=601, group=801, customer=h.CUSTOMER_ID):
    return {**group_row(group, 701, customer), "ad_group_ad": {
        "resource_name": rn("adGroupAds", f"{group}~{ident}", customer), "ad_group": rn("adGroups", group, customer),
        "status": "ENABLED", "ad": {"id": ident, "resource_name": rn("ads", ident, customer),
        "type_": "RESPONSIVE_SEARCH_AD", "final_urls": deepcopy(BEFORE_FINAL), "final_mobile_urls": deepcopy(BEFORE_MOBILE),
        **deepcopy(TRACKING), "responsive_search_ad": {"headlines": [
            {"text": "Trail equipment", "pinned_field": "HEADLINE_1"}, {"text": "Explore the collection"},
            {"text": "Prepare for the outdoors"}], "descriptions": [
            {"text": "Browse durable equipment for your next trail.", "pinned_field": "DESCRIPTION_1"},
            {"text": "Explore the seasonal collection online."}], "path1": "trail", "path2": "equipment"}}}}


def keyword_row(ident=601, group=801, customer=h.CUSTOMER_ID):
    return {**group_row(group, 701, customer), "ad_group_criterion": {
        "criterion_id": ident, "resource_name": rn("adGroupCriteria", f"{group}~{ident}", customer),
        "ad_group": rn("adGroups", group, customer), "status": "ENABLED", "type_": "KEYWORD", "negative": False,
        "keyword": {"text": "trail equipment", "match_type": "PHRASE"}, "cpc_bid_micros": 900000,
        "final_urls": deepcopy(BEFORE_FINAL), "final_mobile_urls": deepcopy(BEFORE_MOBILE), **deepcopy(TRACKING)}}


def standard_data(customer):
    return {"customer": [{"customer": {"id": int(customer), "currency_code": "USD"}}],
            "campaign": [campaign_row(701, customer), campaign_row(702, customer)],
            "ad_group": [group_row(801, 701, customer), group_row(802, 701, customer), group_row(803, 702, customer)],
            "ad_group_ad": [ad_row(601, 801, customer), ad_row(602, 801, customer), ad_row(603, 802, customer)],
            "ad_group_criterion": [keyword_row(601, 801, customer), keyword_row(602, 801, customer), keyword_row(603, 802, customer)]}


def entity(provider, kind, customer=h.CUSTOMER_ID, index=0):
    raw = provider.data[customer][KINDS[kind]["resource"]][index]
    return raw["ad_group_ad"]["ad"] if kind == "ad" else raw["ad_group_criterion"]


def field_uses(query):
    clean = re.sub(r"'(?:\\.|[^'\\])*'|\"(?:\\.|[^\"\\])*\"", "''", query)
    parsed = re.fullmatch(r"\s*SELECT\s+(?P<select>.+?)\s+FROM\s+(?P<resource>\w+)"
        r"(?:\s+WHERE\s+(?P<where>.+?))?(?:\s+ORDER\s+BY\s+(?P<order>.+?))?"
        r"(?:\s+LIMIT\s+\d+)?\s*", clean, re.I | re.S)
    assert parsed, f"Unparsed GAQL: {query}"
    token = r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+"
    uses = []
    for name in parsed["select"].split(","):
        name = name.strip()
        assert re.fullmatch(token, name), name
        uses.append((name, 0))
    uses += [(name, 1) for name in re.findall(token, parsed["where"] or "")]
    for ordering in (parsed["order"] or "").split(","):
        if not ordering:
            continue
        match = re.fullmatch(rf"\s*({token})(?:\s+(?:ASC|DESC))?\s*", ordering, re.I)
        assert match, ordering
        uses.append((match[1], 2))
    return parsed["resource"], uses


def assert_provider_queries(searches):
    facts = json.loads(FACTS_PATH.read_text())["resources"]
    assert searches, "A fresh verification query is required"
    failures = []
    for call in searches:
        resource, uses = field_uses(call.query)
        assert resource in facts, resource
        allowed = {resource, *facts[resource]["attributed_resources"]}
        for name, use in uses:
            owner = name.split(".")[0]
            flags = facts.get(owner, {}).get("fields", {}).get(name)
            if owner not in allowed or flags is None or not flags[use]:
                failures.append((resource, name, ["SELECT", "WHERE", "ORDER BY"][use]))
    assert not failures, f"Unsupported v25 reporting field uses: {failures}"


def _get(raw, field):
    for part in field.split("."):
        if not isinstance(raw, dict):
            return None
        raw = raw.get(part, raw.get(part + "_"))
    return raw


class SearchClient(h.FakeGoogleAdsClient):
    """Scoped and projected rows with lazy-read and mutation observation."""
    def __init__(self):
        super().__init__()
        self.data = {cid: standard_data(cid) for cid in (h.CUSTOMER_ID, h.OTHER_CUSTOMER_ID)}
        self.corrupt = {}
        self.fail_after = {}
        self.pulls = Counter()
        self.lose_response = False

    def _do_search(self, service, method, args, kwargs):
        customer = str(h._req_field(args, kwargs, "customer_id"))
        query = str(h._req_field(args, kwargs, "query"))
        resource = h._FROM_RE.search(query).group(1)
        data = deepcopy(self.data.get(customer, {}))
        campaigns = {r["campaign"]["resource_name"]: r["campaign"] for r in data.get("campaign", [])}
        groups = {r["ad_group"]["resource_name"]: r["ad_group"] for r in data.get("ad_group", [])}
        rows = data.get(resource, [])
        for row in rows:
            if resource in ("ad_group_ad", "ad_group_criterion"):
                row["ad_group"] = deepcopy(groups.get(row[resource].get("ad_group"), {}))
            if resource not in ("campaign", "customer"):
                row["campaign"] = deepcopy(campaigns.get(row.get("ad_group", {}).get("campaign"), {}))
        self._responses[resource] = []
        super()._do_search(service, method, args, kwargs)
        # Filters are applied before fault injection to make foreign and duplicate
        # rows observable even when an otherwise correct query scopes identity.
        where = re.search(r"\bWHERE\b(.*?)(?:\bORDER BY\b|\bLIMIT\b|$)", query, re.I | re.S)
        if where:
            pattern = r"([a-z][a-z0-9_.]+)\s*(=|!=|IN|NOT\s+IN)\s*(\([^)]*\)|'[^']*'|\"[^\"]*\"|[a-zA-Z_0-9]+)"
            for match in re.finditer(pattern, where[1], re.I):
                field, operator, value = match.groups()
                values = re.findall(r"'([^']*)'|\"([^\"]*)\"|([\w]+)", value)
                wanted = {next(v for v in group if v) for group in values}
                negate = operator.upper() in ("!=", "NOT IN")
                def spelling(row):
                    value = _get(row, field)
                    return str(value).upper() if isinstance(value, bool) else str(value)
                rows = [row for row in rows if ((spelling(row) in wanted) != negate)]
        for name in (resource, "campaign", "ad_group", "customer"):
            if name in self.corrupt and (name == resource or any(f.startswith(name + ".") for f in selected(query))):
                rows = self.corrupt[name](rows)
        limit = re.search(r"\bLIMIT\s+(\d+)", query, re.I)
        if limit:
            rows = rows[:int(limit[1])]
        def stream():
            for index, row in enumerate(rows):
                if index == self.fail_after.get(resource):
                    raise ServiceUnavailable("Synthetic incomplete inspection")
                self.pulls[resource] += 1
                yield project(h.make_row(row), selected(query))
            if len(rows) == self.fail_after.get(resource):
                raise ServiceUnavailable("Synthetic incomplete inspection")
        return stream()

    def _do_mutation(self, service, method, args, kwargs):
        response = super()._do_mutation(service, method, args, kwargs)
        call = self.mutations[-1]
        if not call.validate_only:
            for kind, spec in KINDS.items():
                if service != spec["service"]:
                    continue
                for operation in call.request.operations:
                    if operation._pb.WhichOneof("operation") != "update":
                        continue
                    update = operation.update
                    for index in range(len(self.data[call.request.customer_id][spec["resource"]])):
                        target = entity(self, kind, call.request.customer_id, index)
                        if target["resource_name"] == update.resource_name:
                            for field in operation.update_mask.paths:
                                target[field] = list(getattr(update, field))
            if self.lose_response:
                raise ServiceUnavailable("Synthetic update response unavailable")
        return response


def require(server, names):
    missing = set(names) - h.tool_names(server)
    assert not missing, f"Missing approved Search URL behavior: {sorted(missing)}"


def setup(tmp_path, kind, *, read_only=False, env=None, clock=None):
    provider = SearchClient()
    build = h.build_server if read_only else h.build_rw_server
    server = build(tmp_path, client=provider, clock=clock, env={"ADS_MCP_REQUIRE_DRY_RUN": "true", **(env or {})})
    spec = KINDS[kind]
    require(server, [spec["read"]] if read_only else [spec["read"], spec["write"]])
    return server, provider


def stage(server, kind, values=None):
    values = {"final_urls": deepcopy(AFTER_FINAL)} if values is None else values
    payload = h.expect_ok(h.call(server, KINDS[kind]["write"], args(kind, **values)))
    plan = payload["plan"]
    assert plan["tool"] == KINDS[kind]["write"] and plan["operations"] and not plan["irreversible"]
    h.parse_iso_utc(plan["expires_at"])
    return plan


def preview(server, plan):
    result = h.expect_ok(h.call(server, "confirm_and_apply", {"plan_id": plan["id"], "dry_run": True}))
    assert result["applied"] is False
    return result


def apply(server, plan):
    return h.call(server, "confirm_and_apply", {"plan_id": plan["id"], "dry_run": False})


def rejected(server, provider, tool, arguments, *, local=False):
    before = len(provider.searches)
    result = h.call_result(server, tool, arguments)
    text = h.result_text(result)
    assert "Traceback" not in text and "INTERNAL" not in text and "unknown tool" not in text.lower()
    h.assert_no_secrets(text)
    assert "synthetic-private-provider" not in text
    if not result.is_error:
        refusal(h.payload_of(result), provider)
    assert not provider.mutations
    if local:
        assert len(provider.searches) == before
    return result


def values_named(value, name):
    if isinstance(value, dict):
        for key, child in value.items():
            if key == name:
                yield child
            yield from values_named(child, name)
    elif isinstance(value, list):
        for child in value:
            yield from values_named(child, name)


def assert_plan(plan, kind, before, after, mask):
    encoded = json.dumps(plan)
    for name, expected in (("before", before), ("after", after)):
        assert any(isinstance(value, dict) and all(value.get(k) == v for k, v in expected.items())
                   for value in values_named(plan, name)), (name, expected)
    masks = list(values_named(plan, "update_mask"))
    assert masks and any(set(value) == set(mask) for value in masks), masks
    for identity in (rn("campaigns", "701"), rn("adGroups", "801")):
        assert identity in encoded
    for value in TRACKING.values():
        if isinstance(value, str):
            assert value in encoded
        else:
            assert value in list(values_named(plan, "url_custom_parameters"))
    assert "SEARCH" in encoded and "SEARCH_STANDARD" in encoded and "ENABLED" in encoded


def checked_apply(server, provider, kind, plan, values, mask):
    assert not provider.mutations
    snapshot = deepcopy(provider.data)
    preview(server, plan)
    assert not provider.live_mutations()
    before_reads = len(provider.searches)
    assert h.expect_ok(apply(server, plan))["applied"] is True
    assert len(provider.live_mutations()) == 1
    assert len(provider.searches) > before_reads
    assert_provider_queries(provider.searches)
    spec = KINDS[kind]
    call = provider.live_mutations()[0]
    request = call.request
    assert (call.service, call.method) == (spec["service"], spec["method"])
    assert request._pb.DESCRIPTOR.full_name == "google.ads.googleads.v25.services." + spec["request"]
    assert request.customer_id == h.CUSTOMER_ID and not request.partial_failure and not request.validate_only
    assert len(request.operations) == 1
    operation = request.operations[0]
    assert operation._pb.WhichOneof("operation") == "update"
    assert list(operation.update_mask.paths) == list(dict.fromkeys(operation.update_mask.paths))
    assert set(operation.update_mask.paths) == set(mask)
    identity = rn(spec["path"], "601" if kind == "ad" else "801~601")
    assert operation.update.resource_name == identity
    raw = MessageToDict(operation.update._pb, preserving_proto_field_name=True)
    assert set(raw) == {"resource_name", *[field for field in mask if values[field]]}, raw
    for field in mask:
        assert list(getattr(operation.update, field)) == values[field]
    expected = deepcopy(snapshot)
    for row in expected[h.CUSTOMER_ID][spec["resource"]]:
        expected_entity = row["ad_group_ad"]["ad"] if kind == "ad" else row["ad_group_criterion"]
        if expected_entity["resource_name"] == identity:
            for field in mask:
                expected_entity[field] = values[field]
    assert provider.data == expected, "An untouched field, sibling, or other account changed"
    return call


def golden(tmp_path, kind):
    spec = KINDS[kind]
    server, provider = setup(tmp_path, kind, read_only=True)
    fixture = h.load_contract_fixture(FIXTURES / (spec["read"] + ".json"))
    provider.data[h.CUSTOMER_ID] = fixture["gaql"]
    assert h.call(server, spec["read"], fixture["args"]) == fixture["golden"]
    assert_provider_queries(provider.searches)
    assert not provider.mutations


def set_path(row, path, value):
    parts = path.split(".")
    for part in parts[:-1]:
        row = row[int(part)] if isinstance(row, list) else row[part]
    if isinstance(row, list):
        row[int(parts[-1])] = value
    else:
        row[parts[-1]] = value


def corrupt_path(provider, resource, path, value):
    def change(rows):
        rows = deepcopy(rows)
        if rows:
            set_path(rows[0], path, value)
        return rows
    provider.corrupt[resource] = change


def stale(server, provider, plan):
    preview(server, plan)
    before = len(provider.searches)
    error = refusal(apply(server, plan), provider)
    assert error["code"] == "STALE_PLAN"
    assert len(provider.searches) > before
    assert_provider_queries(provider.searches)


def safeguard(tmp_path, monkeypatch, kind, case):
    from ads_mcp.audit import AuditLog
    from ads_mcp.errors import ToolError
    clock = h.FakeClock()
    server, provider = setup(tmp_path, kind, clock=clock)
    spec = KINDS[kind]
    if case in ("foreign_account", "unknown_parameter"):
        extra = {"customer_id": h.OTHER_CUSTOMER_ID} if case == "foreign_account" else {"bypass_require_dry_run": True}
        rejected(server, provider, spec["write"], args(kind, final_urls=AFTER_FINAL, **extra), local=True)
        return
    original = AuditLog.write
    def fail(self, record, **kwargs):
        event = {"staging_audit": "plan_created", "pre_audit": "apply_started", "terminal_audit": "applied"}.get(case)
        if record["event"] == event:
            if case == "terminal_audit":
                return False
            raise ToolError("AUDIT_WRITE_FAILED", "Synthetic audit destination unavailable")
        return original(self, record, **kwargs)
    if case == "staging_audit":
        monkeypatch.setattr(AuditLog, "write", fail)
        result = h.call(server, spec["write"], args(kind, final_urls=AFTER_FINAL))
        assert refusal(result, provider)["code"] == "AUDIT_WRITE_FAILED" and "plan" not in result
        return
    plan = stage(server, kind)
    assert not provider.mutations
    if case == "preview":
        assert refusal(apply(server, plan), provider)["code"] == "DRY_RUN_REQUIRED"
        checked_apply(server, provider, kind, plan, {"final_urls": AFTER_FINAL}, ["final_urls"])
    elif case == "expiry":
        clock.advance(901)
        assert refusal(apply(server, plan), provider)["code"] == "PLAN_EXPIRED"
    elif case == "replay":
        checked_apply(server, provider, kind, plan, {"final_urls": AFTER_FINAL}, ["final_urls"])
        count = len(provider.mutations)
        assert h.error_of(apply(server, plan))["code"] == "PLAN_CONSUMED"
        assert len(provider.mutations) == count
    elif case == "lost_response":
        preview(server, plan)
        provider.lose_response = True
        error = h.error_of(apply(server, plan))
        assert error["code"] != "INTERNAL"
        assert len(provider.live_mutations()) == 1
        assert h.error_of(apply(server, plan))["code"] == "PLAN_CONSUMED"
        assert len(provider.live_mutations()) == 1
    elif case in ("pre_audit", "terminal_audit"):
        preview(server, plan)
        monkeypatch.setattr(AuditLog, "write", fail)
        result = apply(server, plan)
        if case == "pre_audit":
            assert refusal(result, provider)["code"] == "AUDIT_WRITE_FAILED"
        else:
            assert h.expect_ok(result)["applied"] is True and result.get("audit_warning")
            assert len(provider.live_mutations()) == 1
    elif case == "concurrent":
        preview(server, plan)
        barrier = threading.Barrier(4)
        def attempt(_):
            barrier.wait(timeout=5)
            return apply(server, plan)
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(attempt, range(4)))
        assert sum(result.get("applied") is True for result in results) == 1
        assert [h.error_of(result)["code"] for result in results if "error" in result] == ["PLAN_CONSUMED"] * 3
        assert len(provider.live_mutations()) == 1
    else:
        raise AssertionError(case)
    records = h.read_audit_records(tmp_path)
    mine = [record for record in records if record.get("plan_id") == plan["id"]]
    assert mine and all(record.get("customer_id") == h.CUSTOMER_ID for record in mine)
    h.assert_no_secrets(json.dumps(records))
