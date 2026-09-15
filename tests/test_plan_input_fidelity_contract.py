"""F056: requests cannot approve intent that the genuine v25 builder discards.

Real MCP dispatch, fixture-only providers. All nested dict collections currently
accepted by mutation signatures are enumerated; no new product seam is assumed.
"""
from copy import deepcopy
import json

import pytest
from google.protobuf.json_format import MessageToDict

import harness as h
from pmax_oracle import assert_catalog
from ads_mcp.guardrails import PlanStore
from tool_catalog import ALL_WRITE_MODE_TOOLS, MUTATION_ARGS, READ_TOOLS


READ_ARGS = {
    **{name: {"last_n_days": 7} for name in (
        "get_campaign_performance", "get_ad_performance", "get_keyword_performance",
        "get_search_terms", "get_geo_performance")},
    "run_gaql": {"query": "SELECT customer.id FROM customer"},
    "get_change_history": {"date_range_start": "2026-08-01", "date_range_end": "2026-08-01"},
    "get_shopping_performance": {"campaign_id": "111", "last_n_days": 7},
    "get_listing_groups": {"campaign_id": "111"},
    "get_product_status": {"campaign_id": "111"},
    "search_geo_targets": {"query": "United"},
    "discover_keywords": {"seed_keywords": ["widgets"]},
    "get_keyword_forecasts": {"keywords": ["widgets"]},
}


class ObservedPlanStore(PlanStore):
    """Observe the existing public staging seam; delegate all behavior."""
    def __init__(self):
        super().__init__(clock=h.FakeClock())
        self.created = []

    def create(self, **kwargs):
        entry = super().create(**kwargs)
        self.created.append(entry.id)
        return entry


def rejected(result, field):
    text = h.result_text(result)
    if not result.is_error:
        error = h.error_of(h.payload_of(result))
        assert error["code"] != "INTERNAL"
    assert field.lower() in text.lower(), "refusal must identify the unsupported input"
    assert "Traceback" not in text
    h.assert_no_secrets(text)


def no_staged_audit(tmp_path):
    path = h.audit_file(tmp_path)
    if path.exists():
        assert all(r["event"] != "plan_created" for r in h.read_audit_records(tmp_path))


@pytest.mark.parametrize("name", sorted(ALL_WRITE_MODE_TOOLS))
@pytest.mark.parametrize("unknown", ["customerId", "unexpected_option"])
def test_every_registered_tool_rejects_unknown_argument_before_work(tmp_path, account_client, name, unknown):
    store = ObservedPlanStore()
    server = h.build_rw_server(tmp_path, client=account_client, plan_store=store)
    args = deepcopy(MUTATION_ARGS.get(name, READ_ARGS.get(name, {})))
    if name == "confirm_and_apply":
        # Use a real, previewed plan: ignoring the extra key could execute it.
        plan = h.expect_ok(h.call(server, "pause_entity", MUTATION_ARGS["pause_entity"]))["plan"]
        h.expect_ok(h.call(server, name, {"plan_id": plan["id"], "dry_run": True}))
        args = {"plan_id": plan["id"], "dry_run": False}
    before = (len(account_client.searches), len(account_client.planner_calls()), len(account_client.mutations))
    records = h.read_audit_records(tmp_path) if h.audit_file(tmp_path).exists() else []
    staged = list(store.created)
    rejected(h.call_result(server, name, {**args, unknown: h.OTHER_CUSTOMER_ID}), unknown)
    assert store.created == staged
    assert before == (len(account_client.searches), len(account_client.planner_calls()), len(account_client.mutations))
    after = h.read_audit_records(tmp_path) if h.audit_file(tmp_path).exists() else []
    assert sum(r["event"] == "plan_created" for r in after) == sum(r["event"] == "plan_created" for r in records)


# Separate fields ensure a fix for PAUSED alone cannot hide the ignored CPC.
NESTED = [
    ("draft_keywords", "keywords", {"status": "PAUSED"}),
    ("draft_keywords", "keywords", {"cpc_bid": 0.01}),
    ("draft_keywords", "keywords", {"status": "PAUSED", "cpc_bid": 0.01}),
    ("draft_keywords", "keywords", {"unknown_intent": False}),
    ("draft_campaign", "keywords", {"status": "PAUSED"}),
    ("draft_campaign", "keywords", {"cpc_bid": 0.01}),
    ("add_negative_keywords", "keywords", {"status": "PAUSED"}),
    ("add_negative_keywords", "keywords", {"cpc_bid_micros": 10000}),
    ("set_campaign_schedule", "schedules", {"bid_modifier": 0.1}),
    ("set_campaign_schedule", "schedules", {"unknown_intent": None}),
    ("draft_sitelinks", "sitelinks", {"start_date": "2099-01-01"}),
    ("draft_sitelinks", "sitelinks", {"unknown_intent": ""}),
]


def nested_args(tool, collection, extra):
    args = deepcopy(MUTATION_ARGS[tool])
    if tool in ("draft_campaign", "add_negative_keywords"):
        args[collection] = [{"text": "synthetic", "match_type": "EXACT"}]
        if tool == "draft_campaign":
            args["ad_group_name"] = "Synthetic group"
    # Plant in the second item, after a valid neighbor: validation must be atomic.
    args[collection].append({**deepcopy(args[collection][0]), **extra})
    return args


@pytest.mark.parametrize("tool,collection,extra", NESTED)
def test_nested_unknown_fields_refuse_whole_collection_before_staging(tmp_path, account_client, tool, collection, extra):
    store = ObservedPlanStore()
    server = h.build_rw_server(tmp_path, client=account_client, plan_store=store)
    result = h.call_result(server, tool, nested_args(tool, collection, extra))
    rejected(result, next(iter(extra)))
    assert not store.created
    assert not account_client.searches and not account_client.mutations
    no_staged_audit(tmp_path)


@pytest.mark.parametrize("tool,collection,values", [
    ("draft_keywords", "keywords", [{"text": "synthetic", "match_type": "phrase", "cpc_bid_micros": "1200000"}]),
    ("set_campaign_schedule", "schedules", [{"day_of_week": "monday", "start_hour": 9, "start_minute": 15, "end_hour": 17, "end_minute": 45}]),
    ("draft_sitelinks", "sitelinks", [{"link_text": "Synthetic", "final_url": "https://example.invalid/sale", "description1": "First line", "description2": "Second line"}]),
])
def test_supported_neighbors_agree_in_plan_preview_apply_audit_and_real_request(tmp_path, account_client, tool, collection, values):
    server = h.build_rw_server(tmp_path, client=account_client, env={"ADS_MCP_REQUIRE_DRY_RUN": "true"})
    args = {**deepcopy(MUTATION_ARGS[tool]), collection: values}
    plan = h.expect_ok(h.call(server, tool, args))["plan"]
    h.expect_error(server, "confirm_and_apply", {"plan_id": plan["id"], "dry_run": False}, code="DRY_RUN_REQUIRED")
    preview = h.expect_ok(h.call(server, "confirm_and_apply", {"plan_id": plan["id"], "dry_run": True}))
    assert not account_client.mutations
    applied = h.expect_ok(h.call(server, "confirm_and_apply", {"plan_id": plan["id"], "dry_run": False}))
    assert applied["applied"] is True
    assert preview["plan"]["operations"] == applied["plan"]["operations"] == plan["operations"]
    records = [r for r in h.read_audit_records(tmp_path) if r.get("event") == "applied"]
    assert len(records) == 1 and records[0]["operations"] == plan["operations"]
    canonical = plan["operations"][0]["changes"][collection]["new"][0]
    requests = [MessageToDict(m.request._pb, preserving_proto_field_name=True) for m in account_client.mutations]
    assert all(r["customer_id"] == h.CUSTOMER_ID for r in requests)
    created = requests[0]["operations"][0]["create"]
    if collection == "keywords":
        assert canonical == {"text": "synthetic", "match_type": "PHRASE", "cpc_bid_micros": 1200000}
        assert created["keyword"] == {"text": canonical["text"], "match_type": canonical["match_type"]}
        assert int(created["cpc_bid_micros"]) == canonical["cpc_bid_micros"]
        assert created["status"] == "ENABLED"
    elif collection == "schedules":
        assert canonical == {"day_of_week": "MONDAY", "start_hour": 9, "start_minute": 15, "end_hour": 17, "end_minute": 45}
        assert created["ad_schedule"] == {"day_of_week": "MONDAY", "start_hour": 9, "start_minute": "FIFTEEN", "end_hour": 17, "end_minute": "FORTY_FIVE"}
    else:
        assert canonical == values[0]
        assert created["sitelink_asset"] == {k: canonical[k] for k in ("link_text", "description1", "description2")}
        assert created["final_urls"] == [canonical["final_url"]]
        assert requests[1]["operations"][0]["create"]["field_type"] == "SITELINK"
    before = len(account_client.mutations)
    h.expect_error(server, "confirm_and_apply", {"plan_id": plan["id"], "dry_run": False}, code="PLAN_CONSUMED")
    assert len(account_client.mutations) == before


def test_supported_alias_window_and_account_controls_still_dispatch(tmp_path, account_client):
    server = h.build_rw_server(tmp_path, client=account_client)
    result = h.expect_ok(h.call(server, "get_campaign_performance", {"last_n_days": 7,
        "date_range_start": "invalid", "date_range_end": "invalid", "customer_id": h.OTHER_CUSTOMER_ID}))
    assert result["customer_id"] == h.OTHER_CUSTOMER_ID
    assert result["window"] == {"start": "2026-07-26", "end": "2026-08-01"}
    h.expect_ok(h.call(server, "get_keyword_forecasts", {"keyword_texts": ["synthetic"]}))
    account_client.searches.clear()
    h.expect_error(server, "draft_keywords", {**MUTATION_ARGS["draft_keywords"], "customer_id": h.OTHER_CUSTOMER_ID}, code="PLAN_CUSTOMER_MISMATCH")
    assert not account_client.searches and not account_client.mutations
    readonly = h.build_server(tmp_path, client=account_client)
    assert_catalog(h.tool_names(readonly), read_only=True)


# Adapted after complete reading of root-installed-repro/sitecustomize.py.
# Replace only the existing provider-construction seam. The installed command,
# MCP framing/validation and request builders remain production code.
INSTALLED_PROVIDER = '''
import json, os, socket
from pathlib import Path
from google.ads.googleads.client import GoogleAdsClient
from google.protobuf.json_format import MessageToDict
from types import SimpleNamespace
from ads_mcp import auth
root = Path(os.environ["ORACLE_SYNTHETIC_ROOT"])
def blocked(*args, **kwargs):
    raise AssertionError("ORACLE_NETWORK_FORBIDDEN")
socket.create_connection = blocked
socket.socket.connect = blocked
socket.socket.connect_ex = blocked
sdk = GoogleAdsClient(credentials=None, developer_token="synthetic-only", use_proto_plus=True, version="v25")
def record(value):
    with (root / "provider.jsonl").open("a") as stream:
        stream.write(json.dumps(value) + "\\n")
class Provider:
    enums = sdk.enums
    get_type = sdk.get_type
    login_customer_id = None
    def get_service(self, name):
        if name == "GoogleAdsService":
            return self
        def mutate(request):
            record({"kind": "mutate", "request": MessageToDict(request._pb, preserving_proto_field_name=True)})
            return SimpleNamespace(results=[SimpleNamespace(resource_name="customers/9876543210/assets/999")])
        return SimpleNamespace(mutate_ad_group_criteria=mutate, mutate_campaign_criteria=mutate,
            mutate_assets=mutate, mutate_campaign_assets=mutate)
    def search(self, customer_id, query):
        record({"kind": "search", "customer_id": customer_id, "query": query})
        row = sdk.get_type("GoogleAdsRow")
        row.customer.id = int(customer_id)
        row.ad_group.id = 201
        row.ad_group.cpc_bid_micros = 1000000
        return [row]
auth.build_client = lambda config: Provider()
'''


@pytest.mark.parametrize("kind", ["top_read", "top_write", "keyword_status", "keyword_cpc", "schedule", "sitelink"])
def test_installed_mcp_rejects_recorded_silent_intent_losses(tmp_path, kind):
    (tmp_path / "sitecustomize.py").write_text(INSTALLED_PROVIDER)
    env = h.google_ads_env(tmp_path)
    env.update(h.rw_env(tmp_path))
    env.update(PYTHONPATH=str(tmp_path), ORACLE_SYNTHETIC_ROOT=str(tmp_path), PYTHONDONTWRITEBYTECODE="1")
    if kind.startswith("top"):
        tool = "run_gaql" if kind == "top_read" else "draft_keywords"
        args = {**deepcopy(READ_ARGS.get(tool, MUTATION_ARGS.get(tool))), "customerId": h.OTHER_CUSTOMER_ID}
        field = "customerId"
    else:
        index = {"keyword_status": 0, "keyword_cpc": 1, "schedule": 8, "sitelink": 10}[kind]
        tool, collection, extra = NESTED[index]
        args, field = nested_args(tool, collection, extra), next(iter(extra))
    frames = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "oracle", "version": "1"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": tool, "arguments": args}},
    ]
    proc = h._run_console_holding_pipe("ads-mcp", env_overlay=env,
        input_text="".join(json.dumps(f) + "\n" for f in frames), timeout=30)
    assert proc.returncode == 0 and "Traceback" not in proc.stderr
    response = {r["id"]: r for r in map(json.loads, proc.stdout.splitlines()) if "id" in r}[2]["result"]
    text = "".join(c.get("text", "") for c in response["content"])
    if not response.get("isError"):
        assert "error" in json.loads(text), "installed MCP silently accepted unsupported intent"
    assert field.lower() in text.lower()
    assert not (tmp_path / "provider.jsonl").exists(), "invalid request reached installed provider seam"
    no_staged_audit(tmp_path)
    h.assert_no_secrets(proc.stdout + proc.stderr)
