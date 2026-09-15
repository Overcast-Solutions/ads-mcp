"""Shared synthetic transport for the F030-F038 independent oracle.

No provider or credential access. Selected projections become genuine SDK rows;
unselected values cannot authorize a repair accidentally. Public v25 metadata
is captured separately in fixtures/offline_repair_fields_v25.json.
"""
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import re

from google.protobuf.json_format import MessageToDict

import harness as h

ROOT = Path(__file__).resolve().parents[1]


def selected(query):
    return [x.strip() for x in re.search(r"SELECT\s+(.*?)\s+FROM", query, re.I | re.S).group(1).split(",")]


def project(row, fields):
    raw = MessageToDict(row._pb, preserving_proto_field_name=True)
    out = {}
    for field in fields:
        src, dest = raw, out
        parts = field.split(".")
        for index, segment in enumerate(parts):
            key = segment if segment in src else segment + "_"
            if key not in src:
                break
            if index == len(parts) - 1:
                dest[key] = deepcopy(src[key])
            elif isinstance(src[key], dict):
                src, dest = src[key], dest.setdefault(key, {})
            else:
                break
    return h.make_row(out)


class ProjectedClient(h.FakeGoogleAdsClient):
    """GAQL projection with optional resource-scoped server filtering."""
    def __init__(self):
        super().__init__()
        self.filters = {}

    def _do_search(self, service, method, args, kwargs):
        rows = super()._do_search(service, method, args, kwargs)
        query = self.searches[-1].query
        resource = h._FROM_RE.search(query).group(1)
        if resource in self.filters:
            rows = self.filters[resource](rows, self.searches[-1])
        limit = re.search(r"\bLIMIT\s+(\d+)", query, re.I)
        if limit:
            rows = rows[:int(limit.group(1))]
        return [project(row, selected(query)) for row in rows]


def campaign(client, family="MAXIMIZE_CONVERSION_VALUE", *, cid="111", portfolio="", target=3.5):
    fields = {
        "TARGET_CPA": ("target_cpa", "target_cpa_micros"),
        "TARGET_ROAS": ("target_roas", "target_roas"),
        "MAXIMIZE_CONVERSIONS": ("maximize_conversions", "target_cpa_micros"),
        "MAXIMIZE_CONVERSION_VALUE": ("maximize_conversion_value", "target_roas"),
    }
    entity = {"id": int(cid), "resource_name": f"customers/{h.CUSTOMER_ID}/campaigns/{cid}",
              "name": "Synthetic campaign", "status": "ENABLED", "bidding_strategy_type": family,
              "campaign_budget": f"customers/{h.CUSTOMER_ID}/campaignBudgets/311"}
    if portfolio:
        entity["bidding_strategy"] = portfolio
    elif family in fields:
        parent, leaf = fields[family]
        entity[parent] = {leaf: int(target * 1_000_000) if "cpa" in leaf else target}
    client.stub("campaign", [{"campaign": entity,
        "campaign_budget": {"id": 311, "resource_name": f"customers/{h.CUSTOMER_ID}/campaignBudgets/311", "amount_micros": 50_000_000},
        "customer": {"id": int(h.CUSTOMER_ID), "currency_code": "EUR"}}])
    return client


def stage(server, tool, args):
    return h.expect_ok(h.call(server, tool, args))["plan"]


def apply(server, plan, **kwargs):
    return h.call(server, "confirm_and_apply", {"plan_id": plan["id"], "dry_run": False,
                                               "confirm_irreversible": True, **kwargs})


def refusal(payload, client=None, *, no_reads=False):
    error = h.error_of(payload)
    assert re.fullmatch(r"[A-Z][A-Z0-9_]+", error["code"])
    assert error["code"] != "INTERNAL" and "Traceback" not in json.dumps(payload)
    h.assert_no_secrets(json.dumps(payload))
    if client is not None:
        assert not client.mutations
        if no_reads:
            assert not client.searches and not client.planner_calls()
    return error


def audited(tmp_path, error, **fields):
    records = h.read_audit_records(tmp_path)
    assert any(r["event"] == "refused" and r["outcome"] == error["code"]
               and all(r.get(k) == v for k, v in fields.items()) for r in records)


def wire(client):
    return [MessageToDict(c.request._pb, preserving_proto_field_name=True) for c in client.mutations]


def load_script(name):
    spec = importlib.util.spec_from_file_location("offline_" + name, ROOT / "scripts" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
