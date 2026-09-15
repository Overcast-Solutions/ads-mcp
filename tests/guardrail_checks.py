#!/usr/bin/env python3
"""Guardrail checks for complete application, batches and concurrency.

Exercise every mutation executor, multiple ids per batch and concurrent
confirmations with an offline fixture transport and synthetic credentials.
"""

from __future__ import annotations

import sys
import tempfile
import threading
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "tests"))

import harness  # noqa: E402
from tool_catalog import IRREVERSIBLE_TOOLS, MUTATION_ARGS, MUTATION_TOOLS  # noqa: E402

# Multi-element batches. The locked catalog passes ONE id/item per batch
# tool, which makes "applied only the first" indistinguishable from correct.
BATCH_ARGS = {
    "remove_keywords": {"ad_group_id": "201", "criterion_ids": ["401", "402", "403"]},
    "remove_negative_keywords": {"campaign_id": "222", "criterion_ids": ["444", "445", "446"]},
    "draft_keywords": {
        "ad_group_id": "201",
        "keywords": [
            {"text": "alpha", "match_type": "EXACT"},
            {"text": "beta", "match_type": "PHRASE"},
            {"text": "gamma", "match_type": "BROAD"},
        ],
    },
    "add_negative_keywords": {"campaign_id": "222", "keywords": ["free", "cheap", "torrent"]},
    "create_callouts": {"campaign_id": "222", "callouts": ["Free ship", "24/7", "No fees"]},
    "set_campaign_schedule": {
        "campaign_id": "222",
        "schedules": [
            {"day_of_week": day, "start_hour": 9, "start_minute": 0,
             "end_hour": 17, "end_minute": 30}
            for day in ("MONDAY", "TUESDAY", "WEDNESDAY")
        ],
    },
    "create_structured_snippets": {
        "campaign_id": "222", "header": "Brands", "values": ["ACME", "Pro", "Elite"],
    },
    "draft_sitelinks": {
        "campaign_id": "222",
        "sitelinks": [
            {"link_text": f"Link {i}", "final_url": f"https://example.com/{i}",
             "description1": "One", "description2": "Two"}
            for i in range(3)
        ],
    },
}
BATCH_EXPECTED = {
    "remove_keywords": 3, "remove_negative_keywords": 3, "draft_keywords": 3,
    "add_negative_keywords": 3, "create_callouts": 3, "draft_sitelinks": 3,
    "set_campaign_schedule": 3,
}
# Tools whose N inputs become ONE operation carrying N values, rather than N
# operations — counted differently, but still verified against the plan.
SINGLE_OP_BATCHES = {"create_structured_snippets": 3}

FAILS: list[str] = []


def fail(msg: str):
    FAILS.append(msg)
    print(f"  FAIL: {msg}")


def _args(tool: str) -> dict:
    return dict(BATCH_ARGS.get(tool, MUTATION_ARGS[tool]))


def _wire_operations(request):
    """Count the same semantic operations on the aggregate PMax SDK path."""
    if request._pb.DESCRIPTOR.name == "MutateGoogleAdsRequest":
        return request.mutate_operations
    return request.operations


def check_every_tool_applies():
    """Every mutation tool must send a real, non-empty request — and a batch
    tool must send an operation for EVERY item, not just the first."""
    print("\n== apply walk (multi-element batches) ==")
    with tempfile.TemporaryDirectory() as tmp:
        for tool in sorted(MUTATION_TOOLS):
            client = harness.stub_standard_account(harness.FakeGoogleAdsClient())
            server = harness.build_rw_server(tmp, client=client)
            staged = harness.call(server, tool, _args(tool))
            if "error" in staged:
                fail(f"{tool}: plan refused its valid args: {staged['error']}")
                continue
            plan = staged["plan"]
            harness.call(server, "confirm_and_apply", {"plan_id": plan["id"], "dry_run": True})
            if client.live_mutations():
                fail(f"{tool}: dry-run sent a live mutation")
                continue
            applied = harness.call(
                server, "confirm_and_apply", {"plan_id": plan["id"], "dry_run": False,
                                               "confirm_irreversible": tool in IRREVERSIBLE_TOOLS}
            )
            if "error" in applied:
                fail(f"{tool}: APPLY FAILED: {applied['error']}")
                continue
            live = client.live_mutations()
            ops = sum(len(_wire_operations(m.request)) for m in live)
            if not live or ops == 0:
                fail(f"{tool}: reported success but sent no operations")
                continue
            expected = BATCH_EXPECTED.get(tool)
            if expected is not None:
                got = max(
                    len(getattr(m.request, "operations", []) or []) for m in live
                )
                if got != expected:
                    fail(f"{tool}: batch of {expected} sent {got} operations")
                    continue
            if tool in SINGLE_OP_BATCHES:
                values = 0
                for m in live:
                    for op in getattr(m.request, "operations", []):
                        create = getattr(op, "create", None)
                        snippet = getattr(create, "structured_snippet_asset", None)
                        values = max(values, len(getattr(snippet, "values", []) or []))
                if values != SINGLE_OP_BATCHES[tool]:
                    fail(f"{tool}: {SINGLE_OP_BATCHES[tool]} values sent as {values}")
                    continue
            # An operation that exists but points somewhere else is worse than
            # no operation: the plan said one thing and the API got another.
            planned = {
                op.get("resource") for op in plan["operations"] if op.get("resource")
            }
            if planned:
                sent_text = " ".join(
                    str(_wire_operations(m.request)) for m in live
                )
                missing = [
                    r for r in planned
                    if r.rsplit("/", 1)[-1] not in sent_text
                ]
                if missing:
                    fail(f"{tool}: planned resources absent from the request: {missing}")
                    continue
            if tool in IRREVERSIBLE_TOOLS and plan.get("irreversible") is not True:
                fail(f"{tool}: removal not flagged irreversible")
                continue
            records = harness.read_audit_records(tmp)
            mine = [r for r in records if r.get("plan_id") == plan["id"]]
            if not any(r["event"] == "applied" and r.get("outcome") == "success" for r in mine):
                fail(f"{tool}: no terminal success record: {[r['event'] for r in mine]}")
                continue
            if not any(r["event"] == "step_applied" for r in mine):
                fail(f"{tool}: no per-step record of what reached the API")
                continue
            print(f"  {tool:38s} ok ({ops} ops)")


def check_single_use_under_concurrency():
    """One approved plan, many parallel applies: exactly one may win."""
    print("\n== single-use under concurrency ==")
    for threads in (4, 8):
        with tempfile.TemporaryDirectory() as tmp:
            client = harness.stub_standard_account(harness.FakeGoogleAdsClient())
            server = harness.build_rw_server(tmp, client=client)
            plan = harness.call(
                server, "draft_campaign",
                {"campaign_name": "Race", "daily_budget": 25.0, "contains_eu_political_advertising": False,
                 "bidding_strategy": "MAXIMIZE_CONVERSIONS",
                 "geo_target_ids": ["2840"], "language_ids": ["1000"]},
            )["plan"]
            harness.call(server, "confirm_and_apply", {"plan_id": plan["id"], "dry_run": True})

            results, barrier = [], threading.Barrier(threads)

            def apply_once():
                barrier.wait()
                results.append(
                    harness.call(
                        server, "confirm_and_apply",
                        {"plan_id": plan["id"], "dry_run": False},
                    )
                )

            workers = [threading.Thread(target=apply_once) for _ in range(threads)]
            for w in workers:
                w.start()
            for w in workers:
                w.join()

            wins = [r for r in results if r.get("applied") is True]
            campaigns = [
                m for m in client.live_mutations() if m.method == "mutate_campaigns"
            ]
            budgets = [
                m for m in client.live_mutations()
                if m.method == "mutate_campaign_budgets"
            ]
            if len(wins) != 1:
                fail(f"{threads} threads: {len(wins)} applies succeeded, expected 1")
            elif len(campaigns) != 1 or len(budgets) != 1:
                fail(
                    f"{threads} threads: created {len(campaigns)} campaigns / "
                    f"{len(budgets)} budgets from one plan"
                )
            else:
                print(f"  {threads} threads: 1 winner, 1 campaign, 1 budget")


def _zero_bid_account():
    """The DEFAULT shape under every automated bidding strategy: neither the
    keyword nor its ad group carries an explicit CPC bid."""
    client = harness.stub_standard_account(harness.FakeGoogleAdsClient())
    cur = {"customer": {"id": 9876543210, "currency_code": "USD"}}
    client.stub("ad_group_criterion", [{
        "ad_group_criterion": {"criterion_id": 401, "status": "ENABLED",
                               "keyword": {"text": "kw", "match_type": "EXACT"},
                               "cpc_bid_micros": 0},
        "ad_group": {"id": 201}, "campaign": {"id": 222}, **cur,
    }])
    client.stub("ad_group", [{
        "ad_group": {"id": 201, "name": "Auto-bid", "status": "ENABLED",
                     "cpc_bid_micros": 0},
        "campaign": {"id": 222}, **cur,
    }])
    return client


def check_bid_cap_with_no_baseline_anywhere():
    """The caller's claim must not become the ceiling when NOTHING on the
    account carries a bid — the state that defeated the cap twice."""
    print("\n== bid cap when no baseline exists at all ==")
    with tempfile.TemporaryDirectory() as tmp:
        for claimed, new_bid in ((100000.0, 99999.0), (1e6, 999999.0)):
            client = _zero_bid_account()
            server = harness.build_rw_server(
                tmp, client=client, env={"ADS_MCP_MAX_BID_INCREASE_PCT": "100"}
            )
            r = harness.call(
                server, "update_keyword_bid",
                {"ad_group_id": "201", "criterion_id": "401",
                 "current_bid": claimed, "new_bid": new_bid},
            )
            if "error" not in r:
                fail(
                    f"claimed current_bid={claimed:g} licensed new_bid="
                    f"{new_bid:g} with no account baseline: {r['plan']['summary']}"
                )
            else:
                print(f"  claimed={claimed:<10g} new={new_bid:<10g} {r['error']['code']}")


def check_customer_id_refused_on_every_write_tool():
    """A foreign customer_id must never be silently dropped on a write."""
    print("\n== foreign customer_id on write tools ==")
    foreign = "1234567890"
    leaked = []
    with tempfile.TemporaryDirectory() as tmp:
        for tool in sorted(MUTATION_TOOLS):
            client = harness.stub_standard_account(harness.FakeGoogleAdsClient())
            server = harness.build_rw_server(tmp, client=client)
            r = harness.call(
                server, tool, {**_args(tool), "customer_id": foreign}
            )
            if "error" not in r:
                leaked.append(tool)
    if leaked:
        fail(
            f"{len(leaked)} write tools accepted a foreign customer_id and "
            f"targeted the configured account instead: {leaked[:6]}"
            + (" ..." if len(leaked) > 6 else "")
        )
    else:
        print(f"  all {len(MUTATION_TOOLS)} write tools refuse a foreign customer_id")


def check_bid_cap_cannot_be_defined_by_caller():
    """current_bid is caller-supplied; it must not set the ceiling."""
    print("\n== bid cap vs a caller-supplied baseline ==")
    with tempfile.TemporaryDirectory() as tmp:
        client = harness.stub_standard_account(harness.FakeGoogleAdsClient())
        server = harness.build_rw_server(
            tmp, client=client, env={"ADS_MCP_MAX_BID_INCREASE_PCT": "100"}
        )
        # Account bid for criterion 401 is $1.20. A caller claiming a huge
        # "current" must not license a huge new bid.
        for claimed, new_bid, must_refuse in (
            (100000.0, 99999.0, True),
            (0.0, 1e9, True),
            (-5.0, 500.0, True),
            (1.0, 1.5, False),
        ):
            r = harness.call(
                server, "update_keyword_bid",
                {"ad_group_id": "201", "criterion_id": "401",
                 "current_bid": claimed, "new_bid": new_bid},
            )
            refused = "error" in r
            if refused != must_refuse:
                fail(
                    f"current_bid={claimed} new_bid={new_bid}: "
                    f"{'allowed' if not refused else 'refused'}, expected "
                    f"{'refusal' if must_refuse else 'acceptance'}"
                )
            else:
                verdict = r["error"]["code"] if refused else "staged"
                print(f"  current={claimed:<10g} new={new_bid:<10g} {verdict}")


def check_partial_apply_is_recorded():
    """A multi-step apply that fails midway must still record what landed."""
    print("\n== partial multi-step apply ==")
    from ads_mcp import executors

    with tempfile.TemporaryDirectory() as tmp:
        client = harness.stub_standard_account(harness.FakeGoogleAdsClient())
        server = harness.build_rw_server(tmp, client=client)
        plan = harness.call(
            server, "draft_campaign",
            {"campaign_name": "Partial", "daily_budget": 30.0, "contains_eu_political_advertising": False,
             "bidding_strategy": "MAXIMIZE_CONVERSIONS"},
        )["plan"]
        harness.call(server, "confirm_and_apply", {"plan_id": plan["id"], "dry_run": True})

        real_send = executors._send
        state = {"calls": 0}

        def failing_send(ctx, client_, service, method, req_name, ops):
            state["calls"] += 1
            if state["calls"] == 2:  # budget lands, campaign step fails
                raise RuntimeError("synthetic API rejection on step 2")
            return real_send(ctx, client_, service, method, req_name, ops)

        executors._send = failing_send
        try:
            result = harness.call(
                server, "confirm_and_apply", {"plan_id": plan["id"], "dry_run": False}
            )
        finally:
            executors._send = real_send

        records = harness.read_audit_records(tmp)
        mine = [r for r in records if r.get("plan_id") == plan["id"]]
        events = [r["event"] for r in mine]
        landed = [r for r in mine if r["event"] == "step_applied"]
        failed = [r for r in mine if r["event"] == "apply_failed"]
        if "error" not in result:
            fail("a failed multi-step apply reported success")
        elif not landed:
            fail(f"step 1 reached the API but left no record: {events}")
        elif not failed:
            fail(f"no apply_failed record: {events}")
        elif not failed[0].get("partial_changes_possible"):
            fail("apply_failed does not warn that earlier steps may have landed")
        else:
            print(f"  budget landed and is recorded; failure flagged partial ({events})")


def check_audit_honesty_on_executor_failure():
    print("\n== audit honesty on whole-executor failure ==")
    from ads_mcp.guardrails import PlanStore

    with tempfile.TemporaryDirectory() as tmp:
        client = harness.stub_standard_account(harness.FakeGoogleAdsClient())
        store = PlanStore(clock=harness.FakeClock())
        server = harness.build_rw_server(tmp, client=client, plan_store=store)
        plan = harness.call(
            server, "update_campaign", {"campaign_id": "111", "daily_budget": 45.0}
        )["plan"]
        store._plans[plan["id"]].execute = lambda _c: (_ for _ in ()).throw(
            RuntimeError("synthetic executor failure")
        )
        harness.call(server, "confirm_and_apply", {"plan_id": plan["id"], "dry_run": True})
        result = harness.call(
            server, "confirm_and_apply", {"plan_id": plan["id"], "dry_run": False}
        )
        events = [
            r["event"] for r in harness.read_audit_records(tmp)
            if r.get("plan_id") == plan["id"]
        ]
        if "error" not in result:
            fail("failing executor reported success")
        elif "applied" in events:
            fail(f"phantom applied record: {events}")
        elif client.live_mutations():
            fail("failed apply still reached a live mutate call")
        else:
            print(f"  {events}")


def check_mutations_are_not_retried():
    """Retrying a non-idempotent mutate duplicates it.

    The fault is injected at the transport boundary (the service call itself),
    not at our own helper, so the check exercises the real retry decision.
    """
    print("\n== mutations are never retried ==")
    from google.api_core import exceptions as core_exceptions

    with tempfile.TemporaryDirectory() as tmp:
        client = harness.stub_standard_account(harness.FakeGoogleAdsClient())
        attempts = {"n": 0}

        def counting_mutation(service, method, args, kwargs):
            attempts["n"] += 1
            # The classic lost-response fault: Google applied it, we never
            # heard back. A retry here would create a second copy.
            raise core_exceptions.DeadlineExceeded("response lost in flight")

        client._do_mutation = counting_mutation

        server = harness.build_rw_server(
            tmp, client=client, env={"ADS_MCP_RETRY_BASE_SECONDS": "0.01"}
        )
        plan = harness.call(
            server, "add_negative_keywords",
            {"campaign_id": "222", "keywords": ["free"]},
        )["plan"]
        harness.call(server, "confirm_and_apply", {"plan_id": plan["id"], "dry_run": True})
        harness.call(
            server, "confirm_and_apply", {"plan_id": plan["id"], "dry_run": False}
        )

        if attempts["n"] != 1:
            fail(
                f"a mutate reached the API {attempts['n']} times; writes carry "
                "no idempotency key and must not be retried"
            )
        else:
            print("  one attempt, no duplicate write")


def check_ad_group_bid_applies():
    """Ad-group bid increases and decreases must stage and apply consistently."""
    print("\n== update_ad_group bid: stage AND apply ==")
    with tempfile.TemporaryDirectory() as tmp:
        for label, micros in (("increase", 1500000), ("cut", 500000)):
            client = harness.stub_standard_account(harness.FakeGoogleAdsClient())
            server = harness.build_rw_server(tmp, client=client)
            staged = harness.call(
                server, "update_ad_group",
                {"ad_group_id": "201", "cpc_bid_micros": micros},
            )
            if "error" in staged:
                fail(f"update_ad_group {label}: refused at plan time: {staged['error']}")
                continue
            pid = staged["plan"]["id"]
            harness.call(server, "confirm_and_apply", {"plan_id": pid, "dry_run": True})
            applied = harness.call(
                server, "confirm_and_apply", {"plan_id": pid, "dry_run": False}
            )
            if "error" in applied:
                fail(
                    f"update_ad_group {label}: staged and previewed, then "
                    f"REFUSED at apply: {applied['error']['code']}"
                )
            elif not client.live_mutations():
                fail(f"update_ad_group {label}: applied but sent nothing")
            else:
                print(f"  {label:9s} {micros} micros applied")


def check_recommendation_spend_gate():
    """The gate must compare a DAILY BUDGET to the budget cap — never some
    other micros field that happens to be readable."""
    print("\n== recommendation spend gate ==")
    cur = {"customer": {"id": 9876543210, "currency_code": "USD"}}

    def rec(rec_type, detail):
        return {
            "recommendation": {
                "resource_name": f"customers/{harness.CUSTOMER_ID}/recommendations/42",
                "type_": rec_type, "dismissed": False,
                "campaign": f"customers/{harness.CUSTOMER_ID}/campaigns/111",
                **detail,
            },
            **cur,
        }

    cases = [
        ("TARGET_CPA_OPT_IN requiring $5000/day",
         rec("TARGET_CPA_OPT_IN", {"target_cpa_opt_in_recommendation": {
             "recommended_target_cpa_micros": 20000000}}), True),
        ("CAMPAIGN_BUDGET at $5000/day",
         rec("CAMPAIGN_BUDGET", {"campaign_budget_recommendation": {
             "recommended_budget_amount_micros": 5000000000}}), True),
        ("CAMPAIGN_BUDGET at $65/day",
         rec("CAMPAIGN_BUDGET", {"campaign_budget_recommendation": {
             "recommended_budget_amount_micros": 65000000}}), False),
    ]
    with tempfile.TemporaryDirectory() as tmp:
        for label, row, must_refuse in cases:
            client = harness.stub_standard_account(harness.FakeGoogleAdsClient())
            client.stub("recommendation", [row])
            server = harness.build_rw_server(
                tmp, client=client, env={"ADS_MCP_MAX_DAILY_BUDGET": "100"}
            )
            r = harness.call(server, "apply_recommendation", {"recommendation_id": "42"})
            refused = "error" in r
            if refused != must_refuse:
                fail(
                    f"{label}: {'refused' if refused else 'ALLOWED'}, expected "
                    f"{'refusal' if must_refuse else 'acceptance'}"
                )
            else:
                print(f"  {label:42s} {r['error']['code'] if refused else 'staged'}")


def check_apply_time_recheck_runs():
    """F011 requires the spend cap to be re-checked AT APPLY, not just at plan
    time. Tighten the cap between preview and apply: the apply must refuse."""
    print("\n== apply-time spend re-check ==")
    from ads_mcp.guardrails import PlanStore

    with tempfile.TemporaryDirectory() as tmp:
        client = harness.stub_standard_account(harness.FakeGoogleAdsClient())
        store = PlanStore(clock=harness.FakeClock())
        loose = harness.build_rw_server(
            tmp, client=client, plan_store=store,
            env={"ADS_MCP_MAX_DAILY_BUDGET": "1000"},
        )
        plan = harness.call(
            loose, "update_campaign", {"campaign_id": "111", "daily_budget": 150.0}
        )["plan"]
        harness.call(loose, "confirm_and_apply", {"plan_id": plan["id"], "dry_run": True})
        tight = harness.build_rw_server(
            tmp, client=client, plan_store=store,
            env={"ADS_MCP_MAX_DAILY_BUDGET": "100"},
        )
        result = harness.call(
            tight, "confirm_and_apply", {"plan_id": plan["id"], "dry_run": False}
        )
        if "error" not in result or result["error"]["code"] != "BUDGET_CAP_EXCEEDED":
            fail(f"apply-time cap re-check did not refuse: {result}")
        elif client.live_mutations():
            fail("a plan refused at apply still reached the API")
        else:
            print("  tightened cap refused the staged plan at apply")


def check_composite_entity_ids():
    """Ads and keywords are addressed by {parentId}~{childId}; refusing the
    tilde makes them unreachable."""
    print("\n== composite entity ids ==")
    with tempfile.TemporaryDirectory() as tmp:
        for entity_type, entity_id in (("ad", "201~901"), ("keyword", "201~401")):
            client = harness.stub_standard_account(harness.FakeGoogleAdsClient())
            server = harness.build_rw_server(tmp, client=client)
            r = harness.call(
                server, "pause_entity",
                {"entity_type": entity_type, "entity_id": entity_id},
            )
            if "error" in r:
                fail(f"pause_entity({entity_type}, {entity_id}): {r['error']}")
            elif entity_id not in str(r["plan"]["operations"]):
                fail(f"pause_entity({entity_type}): composite id lost")
            else:
                print(f"  {entity_type:8s} {entity_id} addressable")
        # and a genuinely malformed id is still refused
        client = harness.stub_standard_account(harness.FakeGoogleAdsClient())
        server = harness.build_rw_server(tmp, client=client)
        r = harness.call(
            server, "pause_entity",
            {"entity_type": "ad", "entity_id": "201/../../evil"},
        )
        if "error" not in r:
            fail("a path-traversal entity id was accepted")
        else:
            print(f"  path traversal refused ({r['error']['code']})")


def check_decreases_are_never_blocked():
    """Configured caps bound increases and must allow verified decreases."""
    print("\n== decreases are never blocked ==")
    with tempfile.TemporaryDirectory() as tmp:
        # Keyword with no explicit bid: inherits the ad group's.
        client = harness.stub_standard_account(harness.FakeGoogleAdsClient())
        client.stub("ad_group_criterion", [{
            "ad_group_criterion": {"criterion_id": 401, "status": "ENABLED",
                                   "keyword": {"text": "kw", "match_type": "EXACT"},
                                   "cpc_bid_micros": 0},
            "ad_group": {"id": 201}, "campaign": {"id": 222},
            "customer": {"id": 9876543210, "currency_code": "USD"},
        }])
        server = harness.build_rw_server(tmp, client=client)
        r = harness.call(
            server, "update_keyword_bid",
            {"ad_group_id": "201", "criterion_id": "401",
             "current_bid": 5.0, "new_bid": 0.10},
        )
        if "error" in r:
            fail(f"a 90% inherited bid CUT was refused: {r['error']}")
        else:
            print("  bid cut allowed on a keyword with an inherited bid")

        # Budget cut that still exceeds a tightened cap.
        client = harness.stub_standard_account(harness.FakeGoogleAdsClient())
        server = harness.build_rw_server(
            tmp, client=client, env={"ADS_MCP_MAX_DAILY_BUDGET": "20"}
        )
        r = harness.call(
            server, "update_campaign", {"campaign_id": "111", "daily_budget": 30.0}
        )
        if "error" in r:
            fail(f"a budget cut from 50.00 to 30.00 was refused: {r['error']}")
        else:
            print("  budget cut allowed even though 30 > cap 20")


if __name__ == "__main__":
    check_every_tool_applies()
    check_single_use_under_concurrency()
    check_bid_cap_cannot_be_defined_by_caller()
    check_bid_cap_with_no_baseline_anywhere()
    check_customer_id_refused_on_every_write_tool()
    check_partial_apply_is_recorded()
    check_audit_honesty_on_executor_failure()
    check_mutations_are_not_retried()
    check_apply_time_recheck_runs()
    check_ad_group_bid_applies()
    check_recommendation_spend_gate()
    check_composite_entity_ids()
    check_decreases_are_never_blocked()
    print()
    if FAILS:
        print(f"{len(FAILS)} FAILURES")
        raise SystemExit(1)
    print("all guardrail regression checks passed")
