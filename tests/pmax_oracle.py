"""Independent synthetic PMax contract: genuine SDK rows and bounded transport.

Public read payloads are authored in fixtures/pmax, not captured from product
output. Existing harness and its reviewed credential fixtures stay unchanged.
"""
from collections import Counter
from copy import deepcopy
import json
from pathlib import Path
import re

import pytest
from google.api_core.exceptions import ServiceUnavailable

import harness as h
from offline_contract import project, selected, refusal

ROOT = Path(__file__).resolve().parents[1]
PMAX_FIXTURES = ROOT / "tests/fixtures/pmax"
PMAX_READS = frozenset({"get_asset_groups", "get_asset_group_signals", "list_audiences", "get_pmax_url_settings"})
PMAX_MUTATIONS = frozenset({"add_asset_group_search_themes", "add_asset_group_audience_signal",
    "remove_asset_group_signals", "set_pmax_final_url_expansion", "add_pmax_url_exclusion",
    "remove_pmax_url_exclusions", "set_asset_group_product_selection"})
PMAX_ADDITIONS = PMAX_READS | PMAX_MUTATIONS
SEARCH_URL_READS = frozenset({"get_responsive_search_ad_urls", "get_keyword_urls"})
SEARCH_URL_MUTATIONS = frozenset({"update_responsive_search_ad_urls", "update_keyword_urls"})
SEARCH_URL_ADDITIONS = SEARCH_URL_READS | SEARCH_URL_MUTATIONS
PMAX_IRREVERSIBLE = frozenset({"remove_asset_group_signals", "remove_pmax_url_exclusions", "set_asset_group_product_selection"})
PMAX_ARGS = {
    "add_asset_group_search_themes": {"asset_group_id": "801", "themes": ["New season", "Trail equipment"]},
    "add_asset_group_audience_signal": {"asset_group_id": "801", "audience_id": "902"},
    "remove_asset_group_signals": {"asset_group_id": "801", "signal_ids": ["701", "702"]},
    "set_pmax_final_url_expansion": {"campaign_id": "701", "enabled": False},
    "add_pmax_url_exclusion": {"campaign_id": "701", "url": "https://example.invalid/private", "match_type": "EXACT"},
    "remove_pmax_url_exclusions": {"campaign_id": "701", "criterion_ids": ["601", "602"]},
    "set_asset_group_product_selection": {"asset_group_id": "801", "item_ids": ["New-A", "new-a"]},
}
assert set(PMAX_ARGS) == PMAX_MUTATIONS
EXPANSION = "FINAL_URL_EXPANSION_TEXT_ASSET_AUTOMATION"
BAD_IDS = [None, [], {}, True, False, 0, -1, 1.5, "", "0", "-2", "1.0", "1e3", "701 OR 1=1", "801~701", "customers/9876543210/assetGroups/801", "²", "9" * 100]
BAD_TEXT = [None, True, 3, {}, [], "", "   ", "a\x00b", "a\nb", "a\rb", "a\tb"]


def assert_catalog(actual, *, read_only=False, kind=None, final=False):
    """All original names remain required; only this fixed addition list may appear."""
    from tool_catalog import READ_TOOLS, MUTATION_TOOLS, ALL_WRITE_MODE_TOOLS
    base, allowed = (READ_TOOLS | PMAX_READS, SEARCH_URL_READS) if read_only or kind == "read" else (
        (MUTATION_TOOLS | PMAX_MUTATIONS, SEARCH_URL_MUTATIONS) if kind == "mutation" else (ALL_WRITE_MODE_TOOLS | PMAX_ADDITIONS, SEARCH_URL_ADDITIONS))
    actual = set(actual)
    assert base <= actual <= base | allowed, {"missing_baseline": sorted(base - actual), "unapproved": sorted(actual - base - allowed)}
    if final:
        assert base <= actual <= base | allowed, {"missing_approved": sorted(base - actual)}


def expected_pending(server):
    """Independent expected checker output: only absent approved additions may differ.

    Existing lifecycle kinds remain mandatory; asset_group is a newly declared
    domain value whose absence is a real pending requirement, never success.
    """
    from capability_oracle import empty
    tools = h.tool_map(server)
    missing = sorted((PMAX_ADDITIONS | SEARCH_URL_ADDITIONS) - set(tools))
    missing_values = []
    def admits(node):
        if isinstance(node, dict):
            return "asset_group" in node.get("enum", []) or node.get("const") == "asset_group" or any(admits(v) for v in node.values())
        return isinstance(node, list) and any(admits(v) for v in node)
    for name in ("pause_entity", "enable_entity"):
        if not admits(tools[name].input_schema["properties"]["entity_type"]):
            missing_values.append(name + '.entity_type="asset_group"')
    return empty(missing_tools=missing, missing_values=sorted(missing_values))


def rn(kind, ident, customer=h.CUSTOMER_ID):
    return f"customers/{customer}/{kind}/{ident}"


def campaign_row(ident=701, customer=h.CUSTOMER_ID, **extra):
    return {"customer": {"id": int(customer), "currency_code": "USD"}, "campaign": {
        "id": ident, "resource_name": rn("campaigns", ident, customer), "name": f"Synthetic retail {ident}",
        "status": "ENABLED", "advertising_channel_type": "PERFORMANCE_MAX", "shopping_setting": {"merchant_id": 12345},
        "asset_automation_settings": [
            {"asset_automation_type": "TEXT_ASSET_AUTOMATION", "asset_automation_status": "OPTED_OUT"},
            {"asset_automation_type": EXPANSION, "asset_automation_status": "OPTED_IN"},
            {"asset_automation_type": "GENERATE_IMAGE_ENHANCEMENT", "asset_automation_status": "OPTED_IN"}], **extra}}


def group_row(ident=801, campaign=701, customer=h.CUSTOMER_ID, **extra):
    return {"customer": {"id": int(customer)}, "campaign": campaign_row(campaign, customer)["campaign"],
        "asset_group": {"id": ident, "resource_name": rn("assetGroups", ident, customer),
            "campaign": rn("campaigns", campaign, customer), "name": f"Synthetic group {customer}-{ident}",
            "status": "ENABLED", "primary_status": "ELIGIBLE", "final_urls": [f"https://example.invalid/{ident}"],
            "final_mobile_urls": [], **extra}}


def signal_row(ident=701, group=801, customer=h.CUSTOMER_ID, kind="search_theme", value="Trail footwear"):
    signal = {"resource_name": rn("assetGroupSignals", f"{group}~{ident}", customer),
        "asset_group": rn("assetGroups", group, customer), "approval_status": "APPROVED", "disapproval_reasons": []}
    if kind == "search_theme":
        signal[kind] = {"text": value}
    elif kind == "audience":
        signal[kind] = {"audience": rn("audiences", value, customer)}
    elif kind == "local_services_id":
        signal[kind] = {"service_id": "synthetic-service"}
    elif kind == "vertical_ads_item_group_rule_list":
        signal[kind] = {"shared_set": rn("sharedSets", "404", customer)}
    return {**group_row(group, 701, customer), "asset_group_signal": signal}


def audience_row(ident=901, customer=h.CUSTOMER_ID, scope="CUSTOMER", group=None, **extra):
    entity = {"id": ident, "resource_name": rn("audiences", ident, customer), "name": f"Synthetic audience {customer}-{ident}",
        "status": "ENABLED", "scope": scope, **extra}
    if group is not None:
        entity["asset_group"] = rn("assetGroups", group, customer)
    return {"customer": {"id": int(customer)}, "audience": entity}


def criterion_row(ident=601, campaign=701, customer=h.CUSTOMER_ID, conditions=None, **extra):
    return {**campaign_row(campaign, customer), "campaign_criterion": {
        "criterion_id": ident, "resource_name": rn("campaignCriteria", f"{campaign}~{ident}", customer),
        "campaign": rn("campaigns", campaign, customer), "status": "ENABLED", "negative": True, "type_": "WEBPAGE",
        "webpage": {"criterion_name": f"Synthetic rule {ident}", "conditions": conditions or [
            {"operand": "URL", "operator": "EQUALS", "argument": f"https://example.invalid/exclude/{ident}"}]}, **extra}}


def tree_row(ident=1000, group=801, customer=h.CUSTOMER_ID, parent=None, item=None, type_="SUBDIVISION", **extra):
    node = {"id": ident, "resource_name": rn("assetGroupListingGroupFilters", f"{group}~{ident}", customer),
        "asset_group": rn("assetGroups", group, customer), "type_": type_, "listing_source": "SHOPPING", **extra}
    if parent is not None:
        node["parent_listing_group_filter"] = rn("assetGroupListingGroupFilters", f"{group}~{parent}", customer)
    if item is not None:
        node["case_value"] = {"product_item_id": {"value": item}}
    elif parent is not None:
        node["case_value"] = {"product_item_id": {}}
    return {**group_row(group, 701, customer), "asset_group_listing_group_filter": node}


def standard_data(customer):
    return {
        "customer": [{"customer": {"id": int(customer), "currency_code": "USD", "descriptive_name": "Synthetic retailer"}}],
        "campaign": [campaign_row(701, customer), campaign_row(702, customer), campaign_row(703, customer, advertising_channel_type="SEARCH")],
        "asset_group": [group_row(801, 701, customer), group_row(802, 701, customer), group_row(803, 702, customer)],
        "asset_group_signal": [signal_row(customer=customer), signal_row(702, customer=customer, kind="audience", value="901"),
                               signal_row(703, 802, customer, value="Sibling theme")],
        "audience": [audience_row(customer=customer), audience_row(902, customer, "ASSET_GROUP", 801), audience_row(903, customer, "ASSET_GROUP", 802)],
        "campaign_criterion": [criterion_row(customer=customer), criterion_row(602, customer=customer, conditions=[
            {"operand": "URL", "operator": "CONTAINS", "argument": "/archive/"},
            {"operand": "PAGE_TITLE", "operator": "EQUALS", "argument": "Archived"}]), criterion_row(604, 702, customer)],
        "asset_group_listing_group_filter": [tree_row(customer=customer),
            tree_row(1001, customer=customer, parent=1000, item="Old-A", type_="UNIT_INCLUDED"),
            tree_row(1002, customer=customer, parent=1000, item="Old-B", type_="UNIT_EXCLUDED"),
            tree_row(1003, customer=customer, parent=1000, type_="UNIT_EXCLUDED"),
            tree_row(2000, 802, customer, type_="UNIT_INCLUDED")],
    }


class PMaxClient(h.FakeGoogleAdsClient):
    """Account-scoped GAQL, selected-field projection and counted lazy iteration.

    Corruption is injected AFTER ordinary provider filtering to test consumers'
    identity/completeness checks. No business behavior is supplied by this fake.
    """
    def __init__(self):
        super().__init__()
        self.data = {cid: standard_data(cid) for cid in (h.CUSTOMER_ID, h.OTHER_CUSTOMER_ID)}
        self.corrupt = {}
        self.pulls = Counter()
        self.fail_after = {}
        self.lose_response = False

    def _do_search(self, service, method, args, kwargs):
        customer = str(h._req_field(args, kwargs, "customer_id"))
        data = deepcopy(self.data.get(customer, {}))
        campaigns = {row["campaign"]["resource_name"]: row["campaign"] for row in data.get("campaign", [])}
        groups = {row["asset_group"]["resource_name"]: row["asset_group"] for row in data.get("asset_group", [])}
        # Joins expose the current authoritative parent/group state, so an
        # implementation may verify a parent in a joined SELECT or separately.
        for resource_name, entries in data.items():
            for row in entries:
                if resource_name != "asset_group" and "asset_group" in row:
                    group_name = row["asset_group"].get("resource_name")
                    row["asset_group"] = deepcopy(groups.get(group_name, {}))
                if resource_name != "campaign" and "campaign" in row:
                    parent_name = row.get("asset_group", {}).get("campaign") or row.get("campaign_criterion", {}).get("campaign") or row["campaign"].get("resource_name")
                    row["campaign"] = deepcopy(campaigns.get(parent_name, {}))
        self._responses = {key: [h.make_row(r) for r in rows] for key, rows in data.items()}
        rows = super()._do_search(service, method, args, kwargs)
        query = self.searches[-1].query
        resource = h._FROM_RE.search(query).group(1)
        # Equality/IN identity constraints are the API filtering supported by
        # these fixtures; unscoped queries retain siblings to expose leakage.
        for field in ("campaign.id", "campaign.resource_name", "asset_group.id", "asset_group.resource_name",
                      "asset_group.campaign", "asset_group_signal.asset_group", "asset_group_signal.resource_name",
                      "audience.id", "audience.resource_name", "asset_group_listing_group_filter.asset_group",
                      "campaign_criterion.campaign", "campaign_criterion.criterion_id", "campaign_criterion.resource_name"):
            match = re.search(r"(?<![\w.])" + re.escape(field) + r"\s*(?:=\s*('[^']*'|\"[^\"]*\"|\d+)|IN\s*\(([^)]+)\))", query, re.I)
            if match:
                values = re.findall(r"'([^']*)'|\"([^\"]*)\"|(\d+)", match.group(1) or match.group(2))
                wanted = {next(v for v in parts if v) for parts in values}
                def field_value(row):
                    value = row
                    for part in field.split("."):
                        value = getattr(value, part)
                    return str(value)
                rows = [r for r in rows if field_value(r) in wanted]
        if resource in self.corrupt:
            rows = self.corrupt[resource](rows)
        # Apply a parent fault to a joined verification too. A query that never
        # selected the parent cannot accidentally satisfy parent verification.
        fields = selected(query)
        for parent in ("campaign", "asset_group"):
            if parent != resource and parent in self.corrupt and any(field.startswith(parent + ".") for field in fields):
                rows = self.corrupt[parent](rows)
        limit = re.search(r"\bLIMIT\s+(\d+)", query, re.I)
        if limit:
            rows = rows[:int(limit.group(1))]
        def stream():
            for index, row in enumerate(rows):
                if index == self.fail_after.get(resource):
                    raise ServiceUnavailable("Synthetic incomplete read")
                self.pulls[resource] += 1
                yield project(row, selected(query))
        return stream()

    def _do_mutation(self, service, method, args, kwargs):
        response = super()._do_mutation(service, method, args, kwargs)
        if self.lose_response and not self.mutations[-1].validate_only:
            raise ServiceUnavailable("Synthetic write response lost")
        return response


def setup(tmp_path, tools=(), *, read_only=False, env=None, clock=None):
    provider = PMaxClient()
    build = h.build_server if read_only else h.build_rw_server
    server = build(tmp_path, client=provider, clock=clock, env={"ADS_MCP_REQUIRE_DRY_RUN": "true", **(env or {})})
    require(server, tools)
    return server, provider


def require(server, names):
    missing = set(names) - h.tool_names(server)
    assert not missing, f"Missing approved PMax behavior: {sorted(missing)}"


def stage(server, tool, args=None):
    payload = h.expect_ok(h.call(server, tool, deepcopy(PMAX_ARGS[tool] if args is None else args)))
    plan = payload["plan"]
    assert plan["tool"] == tool and plan["id"] and plan["operations"]
    h.parse_iso_utc(plan["expires_at"])
    return plan


def preview(server, plan):
    result = h.expect_ok(h.call(server, "confirm_and_apply", {"plan_id": plan["id"], "dry_run": True}))
    assert result["applied"] is False
    return result


def apply(server, plan, *, acknowledge=True):
    return h.call(server, "confirm_and_apply", {"plan_id": plan["id"], "dry_run": False, "confirm_irreversible": acknowledge})


def checked_apply(server, provider, plan):
    assert not provider.mutations, "staging cannot write"
    preview(server, plan)
    assert not provider.live_mutations(), "preview cannot write"
    result = h.expect_ok(apply(server, plan))
    assert result["applied"] is True
    assert len(provider.live_mutations()) == 1, "one logical PMax operation must be one atomic request"
    return provider.live_mutations()[0]


def rejected(server, provider, tool, args, *, local=False, code=None):
    before = len(provider.searches)
    result = h.call_result(server, tool, args)
    text = h.result_text(result)
    assert "Traceback" not in text and "INTERNAL" not in text
    h.assert_no_secrets(text)
    # MCP may reject malformed caller types before domain dispatch.
    if result.is_error:
        assert "unknown tool" not in text.lower()
    else:
        error = refusal(h.payload_of(result), provider)
        if code is not None:
            assert error["code"] == code
    assert not provider.mutations
    if local:
        assert len(provider.searches) == before, "locally invalid input reached the provider"


def changed(row, field, value):
    copy = h.make_row(json.loads(type(row).to_json(row)))
    target = copy
    parts = field.split(".")
    for key in parts[:-1]:
        target = getattr(target, key)
    setattr(target, parts[-1], value)
    return copy


def stale(server, provider, plan):
    before = len(provider.searches)
    preview(server, plan)
    error = refusal(apply(server, plan), provider)
    assert error["code"] == "STALE_PLAN"
    assert len(provider.searches) > before


def golden(tmp_path, name):
    path = PMAX_FIXTURES / (name + ".json")
    fixture = h.load_contract_fixture(path)
    provider = h.FakeGoogleAdsClient()
    for resource, rows in fixture["gaql"].items():
        provider.stub(resource, rows)
    server = h.build_server(tmp_path, client=provider)
    require(server, [name])
    assert h.call(server, name, fixture["args"]) == fixture["golden"]
    assert not provider.mutations


def safeguard(tmp_path, monkeypatch, tool, case, *, args=None, prerequisite=()):
    from ads_mcp.audit import AuditLog
    from ads_mcp.errors import ToolError
    clock = h.FakeClock()
    server, provider = setup(tmp_path, [tool, *prerequisite], clock=clock)
    if tool == "enable_entity":
        provider.data[h.CUSTOMER_ID]["asset_group"][0]["asset_group"]["status"] = "PAUSED"
    arguments = deepcopy(PMAX_ARGS[tool] if args is None else args)
    if case == "foreign_account":
        rejected(server, provider, tool, {**arguments, "customer_id": h.OTHER_CUSTOMER_ID}, local=True, code="PLAN_CUSTOMER_MISMATCH")
        return
    if case == "unknown_parameter":
        rejected(server, provider, tool, {**arguments, "bypass_require_dry_run": True}, local=True)
        return
    if case == "staging_audit":
        original = AuditLog.write
        def reject_staging(self, record, **kwargs):
            if record["event"] == "plan_created":
                raise ToolError("AUDIT_WRITE_FAILED", "Synthetic staging audit unavailable")
            return original(self, record, **kwargs)
        monkeypatch.setattr(AuditLog, "write", reject_staging)
        result = h.call(server, tool, arguments)
        assert refusal(result, provider)["code"] == "AUDIT_WRITE_FAILED"
        assert "plan" not in result
        return
    plan = stage(server, tool, arguments)
    assert not provider.mutations
    assert plan["irreversible"] is (tool in PMAX_IRREVERSIBLE)
    if case == "preview":
        assert refusal(apply(server, plan), provider)["code"] == "DRY_RUN_REQUIRED"
        checked_apply(server, provider, plan)
    elif case == "expiry":
        clock.advance(901)
        assert refusal(apply(server, plan), provider)["code"] == "PLAN_EXPIRED"
    elif case == "replay":
        checked_apply(server, provider, plan)
        before = len(provider.mutations)
        assert h.error_of(apply(server, plan))["code"] == "PLAN_CONSUMED"
        assert len(provider.mutations) == before
    elif case == "acknowledgement":
        assert tool in PMAX_IRREVERSIBLE
        preview(server, plan)
        refusal(apply(server, plan, acknowledge=False), provider)
        checked_apply(server, provider, plan)
    elif case == "lost_response":
        preview(server, plan)
        provider.lose_response = True
        result = apply(server, plan)
        error = h.error_of(result)
        assert error["code"] != "INTERNAL"
        assert len(provider.live_mutations()) == 1, "uncertain write was retried"
        assert h.error_of(apply(server, plan))["code"] == "PLAN_CONSUMED"
        assert len(provider.live_mutations()) == 1
    elif case in ("pre_audit", "terminal_audit"):
        original = AuditLog.write
        def write(self, record, **kwargs):
            if record.get("plan_id") == plan["id"]:
                if case == "pre_audit" and record["event"] == "apply_started":
                    raise ToolError("AUDIT_WRITE_FAILED", "Synthetic audit unavailable")
                if case == "terminal_audit" and record["event"] == "applied":
                    return False
            return original(self, record, **kwargs)
        monkeypatch.setattr(AuditLog, "write", write)
        preview(server, plan)
        result = apply(server, plan)
        if case == "pre_audit":
            assert refusal(result, provider)["code"] == "AUDIT_WRITE_FAILED"
        else:
            assert h.expect_ok(result)["applied"] is True and result.get("audit_warning")
            assert len(provider.live_mutations()) == 1
    else:
        raise AssertionError(case)
    records = h.read_audit_records(tmp_path)
    mine = [r for r in records if r.get("plan_id") == plan["id"]]
    assert mine and all(r.get("customer_id") == h.CUSTOMER_ID for r in mine)
    h.assert_no_secrets(json.dumps(records))


SAFETY_CASES = ["staging_audit", "foreign_account", "unknown_parameter", "preview", "expiry", "replay", "lost_response", "pre_audit", "terminal_audit"]


def read_walk(tmp_path, tool, args, key, resource, rows, *, case="walk"):
    clock = h.FakeClock()
    server, provider = setup(tmp_path, [tool], read_only=True, env={"ADS_MCP_ROW_LIMIT": "1"}, clock=clock)
    for customer in provider.data:
        provider.data[customer][resource] = rows(customer)
    first = h.expect_ok(h.call(server, tool, args))
    assert first["customer_id"] == h.CUSTOMER_ID and len(first[key]) == 1
    token = first.get("next_page_token")
    assert isinstance(token, str) and token
    before = len(provider.searches)
    if case == "walk":
        # Provider ordering/data change cannot alter the retained continuation.
        for customer in provider.data:
            provider.data[customer][resource] = []
        seen = list(first[key])
        for _ in range(5):
            result = h.expect_ok(h.call(server, tool, {**args, "page_token": token}))
            seen += result[key]
            token = result.get("next_page_token")
            if not token:
                break
        assert len(seen) == 3 and len({json.dumps(r, sort_keys=True) for r in seen}) == 3
        assert len(provider.searches) == before, "continuation re-read live state"
    else:
        changed_args = {**args, "page_token": token}
        if case == "account":
            changed_args["customer_id"] = h.OTHER_CUSTOMER_ID
        elif case == "filter":
            field = "campaign_id" if "campaign_id" in args else "asset_group_id"
            changed_args[field] = "702" if field == "campaign_id" else "802"
        elif case == "tamper":
            changed_args["page_token"] += "!"
        elif case == "expiry":
            clock.advance(300)
        elif case == "tool":
            tool, changed_args = "run_gaql", {"query": "SELECT campaign.id FROM campaign", "page_token": token}
        rejected(server, provider, tool, changed_args, local=True)


def read_bound(tmp_path, tool, args, key, resource, rows):
    server, provider = setup(tmp_path, [tool], read_only=True, env={"ADS_MCP_ROW_LIMIT": "1000"})
    provider.data[h.CUSTOMER_ID][resource] = rows(h.CUSTOMER_ID)
    payload = h.expect_ok(h.call(server, tool, args))
    assert len(payload[key]) == 1000
    assert payload.get("possibly_truncated") is True or payload.get("truncated") is True
    assert provider.pulls[resource] <= 10001, "bounded read consumed beyond one lookahead"
    assert payload.get("next_page_token")
    count = len(payload[key])
    before = len(provider.searches)
    for _ in range(11):
        token = payload.get("next_page_token")
        if not token:
            break
        payload = h.expect_ok(h.call(server, tool, {**args, "page_token": token}))
        count += len(payload[key])
        assert payload.get("possibly_truncated") is True or payload.get("truncated") is True
    assert count == 10000 and not payload.get("next_page_token")
    assert len(provider.searches) == before
