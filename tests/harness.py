"""Shared oracle harness for the ads-mcp locked test suite.

CONTRACT NOTES (binding on the implementation, not just the tests):

- Every tool returns exactly ONE JSON object payload (dict). Domain failures
  are RETURNED as ``{"error": {"code": "<STABLE_CODE>", "message": "..."}}``
  with no other top-level keys required — never raised as exceptions, so the
  MCP result is machine-parseable for an autonomous agent either way.
- ``ads_mcp.server.create_server(config, *, client=None, plan_store=None,
  clock=None)`` returns an ``mcp.server.mcpserver.MCPServer``. ``client`` is
  a GoogleAdsClient-compatible object (this file's FakeGoogleAdsClient in
  tests), ``plan_store`` an ``ads_mcp.guardrails.PlanStore``, ``clock`` a
  zero-arg callable returning epoch seconds (UTC).
- Ids in curated tool payloads are strings; ``run_gaql`` json rows keep the
  native proto JSON types (ints for int64 within JS-safe range).
- The fake client parses fixture rows into REAL v25 ``GoogleAdsRow`` protos
  and hands out REAL proto types/enums, so the implementation must work
  against genuine Google Ads API message semantics.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from types import SimpleNamespace

import mcp
from google.ads.googleads.errors import GoogleAdsException
from google.ads.googleads.util import proto_copy_from
from google.protobuf import json_format
from proto.enums import ProtoEnumMeta

import ads_mcp.config as config_mod
import ads_mcp.server as server_mod

API_VERSION = "v25"  # google-ads 31.x default; fixtures parse against it

FIXED_NOW = datetime(2026, 8, 2, 12, 0, 0, tzinfo=timezone.utc)

CUSTOMER_ID = "9876543210"
CUSTOMER_ID_DASHED = "987-654-3210"
LOGIN_CUSTOMER_ID = "1112223333"
LOGIN_CUSTOMER_ID_DASHED = "111-222-3333"
OTHER_CUSTOMER_ID = "5556667777"

# Planted secret material: these strings must NEVER appear in any tool
# response, log line, or audit record.
FAKE_CLIENT_SECRET = "GOCSPX-oracle-planted-secret-4242"
FAKE_REFRESH_TOKEN = "1//oracle-planted-refresh-token-XYZZY"
FAKE_DEVELOPER_TOKEN = "dEvT0ken-oracle-planted"

CONTRACT_DIR = Path(__file__).parent / "fixtures" / "contract"


# ---------------------------------------------------------------------------
# Real proto plumbing


@lru_cache(maxsize=None)
def _version_module(kind: str):
    return importlib.import_module(f"google.ads.googleads.{API_VERSION}.{kind}")


def get_ads_type(name: str):
    """Mirror GoogleAdsClient.get_type against the pinned API version."""
    for kind in ("common", "enums", "errors", "resources"):
        cls = getattr(_version_module(kind), name, None)
        if cls is not None:
            return cls()
    cls = getattr(_version_module("services").types, name, None)
    if cls is not None:
        return cls()
    raise ValueError(f"unknown Google Ads type {name!r} in {API_VERSION}")


def make_row(row_dict: dict):
    """Parse a recorded-fixture dict into a REAL GoogleAdsRow proto."""
    row = get_ads_type("GoogleAdsRow")
    try:
        json_format.ParseDict(row_dict, row._pb)
    except Exception as exc:  # surface the offending fixture clearly
        raise AssertionError(f"fixture row does not parse as GoogleAdsRow: {exc}\n{row_dict}")
    return row


def make_google_ads_exception(messages, request_id="req-oracle-1"):
    """A constructed GoogleAdsException carrying the given error messages."""
    failure = get_ads_type("GoogleAdsFailure")
    json_format.ParseDict({"errors": [{"message": m} for m in messages]}, failure._pb)
    return GoogleAdsException(None, None, failure, request_id)


class _FakeEnums:
    """Replicates GoogleAdsClient.enums: XxxEnum -> the inner enum class."""

    def __getattr__(self, name):
        wrapper = getattr(_version_module("enums"), name, None)
        if wrapper is None:
            raise AttributeError(name)
        for attr in dir(wrapper):
            val = getattr(wrapper, attr)
            if isinstance(val, ProtoEnumMeta):
                return val
        raise AttributeError(name)


# ---------------------------------------------------------------------------
# Fake GoogleAdsClient (recorded-fixture transport)


@dataclass
class SearchCall:
    service: str
    method: str
    customer_id: str
    query: str
    page_token: str
    page_size: int


@dataclass
class MutateCall:
    service: str
    method: str
    request: object
    kwargs: dict
    validate_only: bool


_FROM_RE = re.compile(r"\bFROM\s+([A-Za-z_][A-Za-z0-9_.]*)", re.IGNORECASE)

_MUTATION_METHOD_RE = re.compile(
    r"^(mutate|apply_recommendation|dismiss_recommendation|upload_)"
)


def _req_field(args, kwargs, name, default=""):
    if name in kwargs and kwargs[name] not in (None, ""):
        return kwargs[name]
    req = kwargs.get("request")
    if req is None and args:
        req = args[0]
    if req is not None:
        val = getattr(req, name, None)
        if val not in (None, ""):
            return val
    return default


class _FakeService:
    def __init__(self, owner, name):
        self._owner = owner
        self._name = name

    def __getattr__(self, method):
        owner, service = self._owner, self._name

        def _call(*args, **kwargs):
            if method in ("search", "search_stream"):
                rows = owner._do_search(service, method, args, kwargs)
                if method == "search_stream":
                    return [SimpleNamespace(results=rows)]
                return rows
            if service == "KeywordPlanIdeaService":
                owner._planner_calls.append((method, args, kwargs))
                if method in owner._planner:
                    return owner._planner[method]
                return SimpleNamespace(results=[])
            if _MUTATION_METHOD_RE.match(method):
                return owner._do_mutation(service, method, args, kwargs)
            # Anything else (path helpers, etc.): benign namespace.
            owner.other_calls.append((service, method, args, kwargs))
            return SimpleNamespace()

        # Resource-name helpers like campaign_path(customer, id).
        if method.endswith("_path"):
            entity = method[: -len("_path")]

            def _path(*parts):
                return f"customers/{parts[0]}/{entity}s/{'~'.join(str(p) for p in parts[1:])}"

            return _path
        return _call


class FakeGoogleAdsClient:
    """GoogleAdsClient stand-in: real types/enums, recorded fixture rows."""

    def __init__(self):
        self.version = API_VERSION
        self.use_proto_plus = True
        self.login_customer_id = None
        self.enums = _FakeEnums()
        self._responses: dict[str, list] = {}
        self._default_rows: list | None = None
        self._search_error: Exception | None = None
        self._search_error_budget: int | None = None
        self.searches: list[SearchCall] = []
        self.mutations: list[MutateCall] = []
        self.other_calls: list = []
        self._planner: dict = {}
        self._planner_calls: list = []
        self._services: dict[str, _FakeService] = {}

    # -- construction-compatible surface
    def get_service(self, name, version=None):
        return self._services.setdefault(name, _FakeService(self, name))

    def get_type(self, name, version=None):
        return get_ads_type(name)

    @staticmethod
    def copy_from(destination, origin):
        return proto_copy_from(destination, origin)

    # -- fixture configuration
    def stub(self, resource: str, row_dicts: list[dict]):
        self._responses[resource] = [make_row(d) for d in row_dicts]
        return self

    def stub_rows(self, resource: str, rows: list):
        self._responses[resource] = list(rows)
        return self

    def stub_planner(self, method: str, response):
        """Stub a KeywordPlanIdeaService response (not a GAQL surface)."""
        self._planner[method] = response
        return self

    def planner_calls(self) -> list:
        return list(self._planner_calls)

    def stub_error(self, exc: Exception, times: int | None = None):
        """Every search raises ``exc`` (or only the first ``times`` calls)."""
        self._search_error = exc
        self._search_error_budget = times
        return self

    # -- recording accessors
    def live_mutations(self) -> list[MutateCall]:
        return [m for m in self.mutations if not m.validate_only]

    def queries(self) -> list[str]:
        return [s.query for s in self.searches]

    # -- transport behavior
    def _do_search(self, service, method, args, kwargs):
        self.searches.append(
            SearchCall(
                service=service,
                method=method,
                customer_id=str(_req_field(args, kwargs, "customer_id")),
                query=str(_req_field(args, kwargs, "query")),
                page_token=str(_req_field(args, kwargs, "page_token")),
                page_size=int(_req_field(args, kwargs, "page_size", 0) or 0),
            )
        )
        if self._search_error is not None:
            if self._search_error_budget is None:
                raise self._search_error
            if self._search_error_budget > 0:
                self._search_error_budget -= 1
                raise self._search_error
        query = str(_req_field(args, kwargs, "query"))
        match = _FROM_RE.search(query)
        resource = match.group(1) if match else ""
        rows = self._responses.get(resource, self._default_rows)
        if resource == "asset" and rows:
            # Asset prerequisites must be selected explicitly; keep real proto
            # metadata and respect either public GAQL identity projection.
            ids = re.search(r"asset\.id\s*(?:=\s*(\d+)|IN\s*\(([^)]+)\))", query, re.I)
            names = re.search(r"asset\.resource_name\s*(?:=\s*('[^']+'|\"[^\"]+\")|IN\s*\(([^)]+)\))", query, re.I)
            if ids:
                wanted = set(re.findall(r"\d+", ids.group(1) or ids.group(2)))
                rows = [r for r in rows if str(r.asset.id) in wanted]
            if names:
                wanted = set(re.findall(r"customers/\d+/assets/\d+", names.group(1) or names.group(2)))
                rows = [r for r in rows if r.asset.resource_name in wanted]
        return list(rows) if rows else []

    def _do_mutation(self, service, method, args, kwargs):
        req = kwargs.get("request")
        if req is None and args:
            req = args[0]
        validate_only = bool(kwargs.get("validate_only", False))
        if req is not None and getattr(req, "validate_only", False):
            validate_only = True
        self.mutations.append(
            MutateCall(
                service=service,
                method=method,
                request=req,
                kwargs=kwargs,
                validate_only=validate_only,
            )
        )
        if service == "GoogleAdsService" and method == "mutate":
            # Aggregate responses use a result oneof, not `.results`. Model
            # the genuine SDK shape so campaign selection cannot accidentally
            # return the budget or the final asset-link result.
            response = get_ads_type("MutateGoogleAdsResponse")
            paths = {"campaign_budget": "campaignBudgets", "campaign": "campaigns",
                     "asset": "assets", "asset_group": "assetGroups",
                     "asset_group_asset": "assetGroupAssets",
                     "campaign_criterion": "campaignCriteria"}
            for index, operation in enumerate(req.mutate_operations, 1):
                field = operation._pb.WhichOneof("operation")
                kind = field.removesuffix("_operation")
                item = get_ads_type("MutateOperationResponse")
                getattr(item, kind + "_result").resource_name = (
                    f"customers/{req.customer_id}/{paths[kind]}/{1000 + index}"
                )
                response.mutate_operation_responses.append(item)
            return response
        n = 1
        try:
            n = max(1, len(req.operations))
        except Exception:
            pass
        results = [
            SimpleNamespace(resource_name=f"customers/{CUSTOMER_ID}/mocked/{i}")
            for i in range(n)
        ]
        return SimpleNamespace(results=results, partial_failure_error=None)


def mutation_mask_paths(mutate_call: MutateCall) -> list[str]:
    """Best-effort extraction of update_mask paths from a recorded mutation."""
    req = mutate_call.request
    if req is None:
        return []
    ops = getattr(req, "operations", None) or []
    paths: list[str] = []
    for op in ops:
        mask = getattr(op, "update_mask", None)
        if mask is not None:
            paths.extend(list(getattr(mask, "paths", [])))
    return paths


# ---------------------------------------------------------------------------
# Clock


class FakeClock:
    def __init__(self, start: float | None = None):
        self.now = FIXED_NOW.timestamp() if start is None else float(start)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float):
        self.now += seconds
        return self.now


# ---------------------------------------------------------------------------
# MCP drivers (in-memory client against the server object)


def run(coro):
    return asyncio.run(coro)


def session_run(server, script):
    """Run ``async script(client)`` against an in-memory MCP connection."""

    async def _go():
        async with mcp.Client(server) as client:
            return await script(client)

    return asyncio.run(_go())


def call_result(server, name, args=None):
    async def _script(client):
        return await client.call_tool(name, args or {})

    return session_run(server, _script)


def payload_of(result):
    """Extract the single JSON object payload from a CallToolResult."""
    sc = result.structured_content
    if isinstance(sc, dict):
        if set(sc) == {"result"} and isinstance(sc["result"], dict):
            return sc["result"]
        return sc
    text = "".join(getattr(c, "text", "") for c in (result.content or []))
    try:
        parsed = json.loads(text)
    except Exception:
        raise AssertionError(
            "tool response is not a JSON object payload "
            f"(is_error={result.is_error}): {text[:400]!r}"
        )
    assert isinstance(parsed, dict), f"tool payload must be a JSON object: {parsed!r}"
    return parsed


def call(server, name, args=None) -> dict:
    return payload_of(call_result(server, name, args))


def error_of(payload: dict) -> dict:
    assert isinstance(payload, dict) and "error" in payload, (
        f"expected an error payload, got: {json.dumps(payload)[:400]}"
    )
    err = payload["error"]
    assert isinstance(err.get("code"), str) and err["code"], f"error without code: {err}"
    assert isinstance(err.get("message"), str) and err["message"], f"error without message: {err}"
    return err


def expect_error(server, name, args=None, code=None) -> dict:
    err = error_of(call(server, name, args))
    if code is not None:
        assert err["code"] == code, f"expected code {code}, got {err['code']}: {err['message']}"
    return err


def expect_ok(payload: dict) -> dict:
    assert "error" not in payload, f"unexpected error payload: {payload.get('error')}"
    return payload


def tool_map(server) -> dict:
    async def _script(client):
        listed = await client.list_tools()
        return {t.name: t for t in listed.tools}

    return session_run(server, _script)


def tool_names(server) -> set[str]:
    return set(tool_map(server))


# ---------------------------------------------------------------------------
# Environment and server builders


def write_oauth_files(tmp_path) -> tuple[Path, Path]:
    cred = Path(tmp_path) / "oauth_client.json"
    tok = Path(tmp_path) / "oauth_refresh.json"
    cred.write_text(
        json.dumps(
            {
                "installed": {
                    "client_id": "oracle.apps.googleusercontent.com",
                    "client_secret": FAKE_CLIENT_SECRET,
                    "token_uri": "https://oauth2.googleapis.com/token",
                }
            }
        )
    )
    tok.write_text(
        json.dumps(
            {
                "refresh_token": FAKE_REFRESH_TOKEN,
                "client_id": "oracle.apps.googleusercontent.com",
                "client_secret": FAKE_CLIENT_SECRET,
            }
        )
    )
    return cred, tok


def google_ads_env(tmp_path) -> dict:
    """Standard Google Ads environment variables with synthetic credentials."""
    cred, tok = write_oauth_files(tmp_path)
    return {
        "GOOGLE_ADS_DEVELOPER_TOKEN": FAKE_DEVELOPER_TOKEN,
        "GOOGLE_ADS_CUSTOMER_ID": CUSTOMER_ID_DASHED,
        "GOOGLE_ADS_LOGIN_CUSTOMER_ID": LOGIN_CUSTOMER_ID_DASHED,
        "GOOGLE_ADS_CREDENTIALS_PATH": str(cred),
        "GOOGLE_ADS_TOKEN_PATH": str(tok),
    }


def audit_file(tmp_path) -> Path:
    return Path(tmp_path) / "audit.jsonl"


def build_config(tmp_path, env=None):
    merged = google_ads_env(tmp_path)
    merged.update(env or {})
    merged = {k: v for k, v in merged.items() if v is not None}
    return config_mod.load_config(merged)


def build_server(tmp_path, *, client, env=None, plan_store=None, clock=None):
    cfg = build_config(tmp_path, env=env)
    return server_mod.create_server(
        cfg,
        client=client,
        plan_store=plan_store,
        clock=clock if clock is not None else FakeClock(),
    )


def rw_env(tmp_path, **overrides) -> dict:
    """Write-mode env: mutations on, caps set, dry-run relaxed unless a test
    exercises it, audit into tmp. Tests override per-scenario."""
    env = {
        "ADS_MCP_READ_ONLY": "false",
        "ADS_MCP_REQUIRE_DRY_RUN": "false",
        "ADS_MCP_MAX_DAILY_BUDGET": "100",
        "ADS_MCP_MAX_BID_INCREASE_PCT": "100",
        "ADS_MCP_MAX_FIRST_BID": "10",
        "ADS_MCP_AUDIT_LOG": str(audit_file(tmp_path)),
    }
    env.update(overrides)
    return env


def build_rw_server(tmp_path, *, client, env=None, plan_store=None, clock=None):
    merged = rw_env(tmp_path)
    merged.update(env or {})
    return build_server(
        tmp_path, client=client, env=merged, plan_store=plan_store, clock=clock
    )


# ---------------------------------------------------------------------------
# Standard fixture account (mirrors the contract fixtures)


def stub_standard_account(client: FakeGoogleAdsClient) -> FakeGoogleAdsClient:
    client.stub("asset", [
        {"asset": {"id": asset_id,
                   "resource_name": f"customers/{CUSTOMER_ID}/assets/{asset_id}",
                   "type_": "IMAGE", "image_asset": {
                       "file_size": 5120000,
                       "full_size": {"width_pixels": width, "height_pixels": height}}}}
        for asset_id, width, height in ((801, 1200, 628), (802, 300, 300), (803, 128, 128))
    ])
    cur = {"customer": {"id": 9876543210, "currency_code": "USD"}}
    client.stub(
        "customer",
        [
            {
                "customer": {
                    "id": 9876543210,
                    "descriptive_name": "ACME",
                    "currency_code": "USD",
                    "time_zone": "America/Los_Angeles",
                    "auto_tagging_enabled": True,
                    "manager": False,
                    "test_account": False,
                }
            }
        ],
    )
    client.stub(
        "customer_client",
        [
            {
                "customer_client": {
                    "id": 9876543210,
                    "descriptive_name": "ACME",
                    "manager": False,
                    "level": 0,
                    "status": "ENABLED",
                }
            }
        ],
    )
    client.stub(
        "campaign",
        [
            {
                "campaign": {
                    "id": 111,
                    "resource_name": f"customers/{CUSTOMER_ID}/campaigns/111",
                    "campaign_budget": f"customers/{CUSTOMER_ID}/campaignBudgets/311",
                    "name": "ACME - PMax - Retail",
                    "status": "ENABLED",
                    "serving_status": "SERVING",
                    "primary_status": "ELIGIBLE",
                    "primary_status_reasons": [],
                    "advertising_channel_type": "PERFORMANCE_MAX",
                    "bidding_strategy_type": "MAXIMIZE_CONVERSION_VALUE",
                    "maximize_conversion_value": {"target_roas": 3.5},
                    "shopping_setting": {"merchant_id": 555111},
                },
                "campaign_budget": {"id": 311, "resource_name": f"customers/{CUSTOMER_ID}/campaignBudgets/311", "amount_micros": 50000000},
                "metrics": {
                    "impressions": 1200,
                    "clicks": 40,
                    "cost_micros": 52400000,
                    "conversions": 2.0,
                    "conversions_value": 180.0,
                    "ctr": 0.05,
                    "average_cpc": 1310000,
                },
                **cur,
            },
            {
                "campaign": {
                    "id": 222,
                    "resource_name": f"customers/{CUSTOMER_ID}/campaigns/222",
                    "campaign_budget": f"customers/{CUSTOMER_ID}/campaignBudgets/322",
                    "name": "Brand - Search",
                    "status": "PAUSED",
                    "serving_status": "PENDING",
                    "primary_status": "PAUSED",
                    "primary_status_reasons": ["CAMPAIGN_PAUSED"],
                    "advertising_channel_type": "SEARCH",
                    "bidding_strategy_type": "MANUAL_CPC",
                },
                "campaign_budget": {"id": 322, "resource_name": f"customers/{CUSTOMER_ID}/campaignBudgets/322", "amount_micros": 20000000},
                "metrics": {
                    "impressions": 300,
                    "clicks": 12,
                    "cost_micros": 8100000,
                    "conversions": 0.0,
                    "conversions_value": 0.0,
                    "ctr": 0.04,
                    "average_cpc": 675000,
                },
                **cur,
            },
            {
                "campaign": {
                    "id": 333,
                    "resource_name": f"customers/{CUSTOMER_ID}/campaigns/333",
                    "campaign_budget": f"customers/{CUSTOMER_ID}/campaignBudgets/333311",
                    "name": "Search - tCPA",
                    "status": "ENABLED",
                    "serving_status": "SERVING",
                    "primary_status": "ELIGIBLE",
                    "primary_status_reasons": [],
                    "advertising_channel_type": "SEARCH",
                    "bidding_strategy_type": "MAXIMIZE_CONVERSIONS",
                    "maximize_conversions": {"target_cpa_micros": 8000000},
                },
                "campaign_budget": {"id": 333311, "resource_name": f"customers/{CUSTOMER_ID}/campaignBudgets/333311", "amount_micros": 30000000},
                "metrics": {
                    "impressions": 90,
                    "clicks": 5,
                    "cost_micros": 4000000,
                    "conversions": 1.0,
                    "conversions_value": 45.0,
                    "ctr": 0.055,
                    "average_cpc": 800000,
                },
                **cur,
            },
        ],
    )
    client.stub(
        "ad_group",
        [
            {
                "ad_group": {
                    "id": 201,
                    "name": "Brand core",
                    "status": "ENABLED",
                    "cpc_bid_micros": 1000000,
                },
                "campaign": {"id": 222},
                **cur,
            }
        ],
    )
    client.stub(
        "ad_group_ad",
        [
            {
                "ad_group_ad": {
                    "status": "ENABLED",
                    "ad": {"id": 901, "type_": "RESPONSIVE_SEARCH_AD"},
                },
                "ad_group": {"id": 201, "name": "Brand core"},
                "campaign": {"id": 222, "name": "Brand - Search"},
                "metrics": {
                    "impressions": 280,
                    "clicks": 11,
                    "cost_micros": 7600000,
                    "conversions": 0.0,
                    "ctr": 0.039,
                },
                **cur,
            }
        ],
    )
    client.stub(
        "ad_group_criterion",
        [
            {
                "ad_group_criterion": {
                    "criterion_id": 401,
                    "status": "ENABLED",
                    "keyword": {"text": "acme widgets", "match_type": "EXACT"},
                    "cpc_bid_micros": 1200000,
                    "quality_info": {"quality_score": 7},
                },
                "ad_group": {"id": 201},
                "campaign": {"id": 222},
                "metrics": {
                    "impressions": 250,
                    "clicks": 10,
                    "cost_micros": 7000000,
                    "conversions": 0.0,
                },
                **cur,
            }
        ],
    )
    client.stub(
        "campaign_criterion",
        [
            {
                "campaign_criterion": {
                    "criterion_id": 444,
                    "negative": True,
                    "keyword": {"text": "cheap", "match_type": "PHRASE"},
                },
                "campaign": {"id": 222},
                **cur,
            }
        ],
    )
    client.stub(
        "recommendation",
        [
            {
                "recommendation": {
                    "resource_name": f"customers/{CUSTOMER_ID}/recommendations/777",
                    "type_": "CAMPAIGN_BUDGET",
                    "dismissed": False,
                    "campaign": f"customers/{CUSTOMER_ID}/campaigns/111",
                    "impact": {
                        "base_metrics": {
                            "impressions": 1200,
                            "clicks": 40,
                            "cost_micros": 52400000,
                            "conversions": 2.0,
                        },
                        "potential_metrics": {
                            "impressions": 2600,
                            "clicks": 70,
                            "cost_micros": 90000000,
                            "conversions": 4.5,
                        },
                    },
                    "campaign_budget_recommendation": {
                        "current_budget_amount_micros": 50000000,
                        "recommended_budget_amount_micros": 65000000,
                    },
                },
                **cur,
            }
        ],
    )
    client.stub(
        "conversion_action",
        [
            {
                "conversion_action": {
                    "id": 555,
                    "name": "Signup",
                    "category": "SIGNUP",
                    "type_": "UPLOAD_CLICKS",
                    "status": "ENABLED",
                    "counting_type": "ONE_PER_CLICK",
                    "primary_for_goal": True,
                },
                **cur,
            }
        ],
    )
    client.stub(
        "geo_target_constant",
        [
            {
                "geo_target_constant": {
                    "id": 2840,
                    "name": "United States",
                    "canonical_name": "United States",
                    "country_code": "US",
                    "target_type": "Country",
                    "status": "ENABLED",
                }
            }
        ],
    )
    return client


# ---------------------------------------------------------------------------
# Console / stdio drivers (the artifact users actually run)


def console_script(name: str = "ads-mcp") -> Path:
    path = Path(sys.executable).with_name(name)
    assert path.exists(), (
        f"console script {name!r} is not installed next to {sys.executable}; "
        "run a dev install (uv pip install -e '.[dev]')"
    )
    return path


def scrubbed_env(overlay: dict | None = None) -> dict:
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("GOOGLE_ADS_", "ADS_MCP_"))
    }
    env.update(overlay or {})
    return env


def run_console(name, args=(), env_overlay=None, input_text="", timeout=40):
    return subprocess.run(
        [str(console_script(name)), *args],
        input=input_text,
        env=scrubbed_env(env_overlay),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _run_console_holding_pipe(name, env_overlay=None, input_text="", timeout=40,
                              settle=None):
    """Drain both pipes while waiting for all requested JSON-RPC responses.

    ``settle`` is retained for call compatibility only. A monotonic deadline
    covers replies and shutdown. On every outcome the child is reaped and all
    three pipes close; invalid/missing protocol output is an observer failure.
    """
    import queue
    import threading
    import time

    requested = {frame["id"] for line in input_text.splitlines() if line.strip()
                 for frame in [json.loads(line)] if "id" in frame}
    deadline = time.monotonic() + timeout
    proc = subprocess.Popen(
        [str(console_script(name))],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        env=scrubbed_env(env_overlay), text=True, bufsize=1,
    )
    events = queue.Queue()
    output, errors = [], []

    def drain(pipe, chunks, report):
        try:
            for line in pipe:
                chunks.append(line)
                if report:
                    events.put(line)
        finally:
            if report:
                events.put(None)

    readers = [threading.Thread(target=drain, args=(proc.stdout, output, True)),
               threading.Thread(target=drain, args=(proc.stderr, errors, False))]
    for reader in readers:
        reader.start()

    def remaining():
        seconds = deadline - time.monotonic()
        if seconds <= 0:
            raise subprocess.TimeoutExpired(proc.args, timeout)
        return seconds

    seen = set()

    def validate(line):
        if not line.strip():
            return
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            raise AssertionError("malformed JSON-RPC response: invalid JSON") from None
        assert isinstance(message, dict) and message.get("jsonrpc") == "2.0", (
            "malformed JSON-RPC response envelope"
        )
        if "id" in message:
            assert ("result" in message) != ("error" in message), (
                "malformed JSON-RPC response: expected exactly one result or error"
            )
            assert message["id"] not in seen, "duplicate JSON-RPC response"
            seen.add(message["id"])
        else:
            assert isinstance(message.get("method"), str), "malformed JSON-RPC notification"

    try:
        proc.stdin.write(input_text)
        proc.stdin.flush()
        while requested - seen:
            try:
                line = events.get(timeout=remaining())
            except queue.Empty:
                raise subprocess.TimeoutExpired(proc.args, timeout) from None
            assert line is not None, "missing requested JSON-RPC response before stdout closed"
            validate(line)
        proc.stdin.close()
        proc.wait(timeout=remaining())
        for reader in readers:
            reader.join(timeout=remaining())
        while not events.empty():
            line = events.get_nowait()
            if line is not None:
                validate(line)
        return subprocess.CompletedProcess(proc.args, proc.returncode,
                                           "".join(output), "".join(errors))
    finally:
        if not proc.stdin.closed:
            proc.stdin.close()
        if proc.poll() is None:
            proc.kill()
        proc.wait()
        for reader in readers:
            reader.join(timeout=2)
        proc.stdout.close()
        proc.stderr.close()


def stdio_tools_list(env_overlay, timeout=40):
    """Drive the installed ads-mcp binary over real stdio JSON-RPC.

    Sends initialize -> initialized -> tools/list, closes stdin, and returns
    (CompletedProcess, parsed-JSON stdout messages).
    """
    frames = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "oracle", "version": "0"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    ]
    # Hold the pipe open while the server answers. Closing stdin immediately
    # makes the SDK cancel in-flight responses, which produced a ~20%
    # intermittent failure here; no real MCP client uses that pattern.
    proc = _run_console_holding_pipe(
        "ads-mcp",
        env_overlay=env_overlay,
        input_text="".join(json.dumps(f) + "\n" for f in frames),
        timeout=timeout,
    )
    messages = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            messages.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return proc, messages


# ---------------------------------------------------------------------------
# Contract fixtures (F016)


def planner_response(spec: dict):
    """Build a KeywordPlanIdeaService-shaped response from fixture JSON."""
    def _node(d):
        return SimpleNamespace(**{
            k: (_node(v) if isinstance(v, dict) else v) for k, v in d.items()
        })

    if "results" in spec:
        return SimpleNamespace(results=[_node(r) for r in spec["results"]])
    return _node(spec)


def contract_fixture_files() -> list[Path]:
    return sorted(CONTRACT_DIR.glob("*.json"))


def load_contract_fixture(path: Path) -> dict:
    data = json.loads(path.read_text())
    for key in ("tool", "args", "golden"):
        assert key in data, f"{path.name}: contract fixture missing {key!r}"
    assert "gaql" in data or "planner" in data, (
        f"{path.name}: contract fixture needs a gaql or planner section"
    )
    data.setdefault("gaql", {})
    for resource, rows in data["gaql"].items():
        for row in rows:
            make_row(row)  # fail loudly if a recorded row stops parsing
    return data


def substitute_placeholders(node, mapping: dict):
    """Replace exact "${NAME}" string values in a golden payload."""
    if isinstance(node, dict):
        return {k: substitute_placeholders(v, mapping) for k, v in node.items()}
    if isinstance(node, list):
        return [substitute_placeholders(v, mapping) for v in node]
    if isinstance(node, str) and node.startswith("${") and node.endswith("}"):
        name = node[2:-1]
        assert name in mapping, f"unknown golden placeholder {node}"
        return mapping[name]
    return node


# ---------------------------------------------------------------------------
# Assertions shared across features


MONEY_RE = re.compile(r"^\d+\.\d{2} [A-Z]{3}$")


def assert_money(value, currency="USD"):
    assert isinstance(value, str) and MONEY_RE.match(value), (
        f"expected micros converted to decimal money string like '52.40 {currency}', got {value!r}"
    )
    assert value.endswith(f" {currency}"), f"expected currency {currency} in {value!r}"


def assert_no_secrets(text: str):
    for secret in (FAKE_REFRESH_TOKEN, FAKE_CLIENT_SECRET, FAKE_DEVELOPER_TOKEN):
        assert secret not in text, f"secret material leaked: {secret[:12]}..."


def result_text(result) -> str:
    """Every byte of a CallToolResult as text, for secret scans."""
    parts = [json.dumps(result.structured_content or {})]
    for c in result.content or []:
        parts.append(getattr(c, "text", ""))
    return "\n".join(parts)


def parse_iso_utc(value: str) -> datetime:
    assert isinstance(value, str), f"expected ISO-8601 UTC string, got {value!r}"
    dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    assert dt.tzinfo is not None, f"timestamp must be timezone-aware UTC: {value!r}"
    return dt.astimezone(timezone.utc)


def read_audit_records(tmp_path) -> list[dict]:
    path = audit_file(tmp_path)
    assert path.exists(), f"audit log {path} was not created"
    records = []
    for line in path.read_text().splitlines():
        if not line.strip():
            continue
        rec = json.loads(line)
        assert isinstance(rec, dict), f"audit line is not a JSON object: {line!r}"
        records.append(rec)
    return records
