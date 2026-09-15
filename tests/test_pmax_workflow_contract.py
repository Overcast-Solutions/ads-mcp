"""F071: complete public PMax catalog, installed workflows and release obligations."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from pathlib import Path
import re
import shutil
import subprocess
import sys

import pytest

import harness as h
import test_auth_cause_contract as process_plumbing
from capability_oracle import DEFAULT, cli, empty, success
from offline_contract import load_script
from pmax_oracle import (PMAX_ADDITIONS, PMAX_ARGS, PMAX_FIXTURES, PMAX_IRREVERSIBLE, PMAX_MUTATIONS, PMAX_READS,
    ROOT, SAFETY_CASES, SEARCH_URL_ADDITIONS, SEARCH_URL_READS, SEARCH_URL_MUTATIONS, expected_pending, apply, assert_catalog, checked_apply, preview, rejected, require, safeguard, setup, stage)
from tool_catalog import ALL_WRITE_MODE_TOOLS, MUTATION_TOOLS, READ_TOOLS

# These expectations were authored from the approved workflow contract,
# independently of registry metadata or generated documentation.
OBLIGATIONS = {
    "get_asset_groups": (["campaign_id", "customer_id", "page_token"], ["campaign_id"], {}),
    "get_asset_group_signals": (["asset_group_id", "customer_id", "page_token"], ["asset_group_id"], {}),
    "list_audiences": (["customer_id", "page_token"], [], {}),
    "get_pmax_url_settings": (["campaign_id", "customer_id", "page_token"], ["campaign_id"], {}),
    "add_asset_group_search_themes": (["asset_group_id", "themes", "customer_id"], ["asset_group_id", "themes"], {}),
    "add_asset_group_audience_signal": (["asset_group_id", "audience_id", "customer_id"], ["asset_group_id", "audience_id"], {}),
    "remove_asset_group_signals": (["asset_group_id", "signal_ids", "customer_id"], ["asset_group_id", "signal_ids"], {}),
    "set_pmax_final_url_expansion": (["campaign_id", "enabled", "customer_id"], ["campaign_id", "enabled"], {}),
    "add_pmax_url_exclusion": (["campaign_id", "url", "match_type", "customer_id"], ["campaign_id", "url"], {"match_type": ["EXACT", "CONTAINS"]}),
    "remove_pmax_url_exclusions": (["campaign_id", "criterion_ids", "customer_id"], ["campaign_id", "criterion_ids"], {}),
    "set_asset_group_product_selection": (["asset_group_id", "item_ids", "customer_id"], ["asset_group_id", "item_ids"], {}),
}
READ_ARGS = {"get_asset_groups": {"campaign_id": "701"}, "get_asset_group_signals": {"asset_group_id": "801"},
             "list_audiences": {}, "get_pmax_url_settings": {"campaign_id": "701"}}


def final_server(tmp_path, *, read_only=False):
    server, provider = setup(tmp_path, PMAX_READS if read_only else PMAX_ADDITIONS, read_only=read_only)
    assert_catalog(h.tool_names(server), read_only=read_only, final=True)
    return server, provider


def test_final_contract_requires_all_63_operations_and_all_25_reads(tmp_path):
    from ads_mcp.tools.registry import all_tool_specs
    server, provider = final_server(tmp_path)
    assert 63 <= len(h.tool_names(server)) <= 67
    ro, _ = final_server(tmp_path, read_only=True)
    assert 25 <= len(h.tool_names(ro)) <= 27
    specs = all_tool_specs()
    assert 63 <= len({spec.name for spec in specs}) == len(specs) <= 67
    assert_catalog({s.name for s in specs if s.kind == "read"}, kind="read", final=True)
    assert_catalog({s.name for s in specs if s.kind == "mutation"}, kind="mutation", final=True)
    assert {s.name for s in specs if s.kind == "apply"} == {"confirm_and_apply"}
    assert all(s.description.strip() for s in specs)


def test_authored_requirements_and_declared_domains_are_complete(tmp_path):
    server, _ = final_server(tmp_path)
    contract = json.loads(DEFAULT.read_text())
    tools = [t for cap in contract["capabilities"] for t in cap["tools"]]
    by_name = {t["name"]: t for t in tools}
    assert len(tools) == len(by_name) == 67 and set(by_name) == ALL_WRITE_MODE_TOOLS | PMAX_ADDITIONS | SEARCH_URL_ADDITIONS
    assert set(OBLIGATIONS) == PMAX_ADDITIONS
    for name, (parameters, required, values) in OBLIGATIONS.items():
        assert by_name[name] == {"name": name, "parameters": parameters, "required": required, "values": values}
    assert set(contract["forbidden_parameters"]) == {"bypass_require_dry_run", "confirmed_twice"}
    for name in ("pause_entity", "enable_entity"):
        assert "asset_group" in by_name[name]["values"]["entity_type"]
    metadata = h.tool_map(server)
    for name in ("pause_entity", "enable_entity"):
        schema = metadata[name].input_schema["properties"]["entity_type"]
        encoded = json.dumps(schema)
        assert all('"' + kind + '"' in encoded for kind in ("campaign", "ad_group", "ad", "keyword", "asset_group"))
    enabled = metadata["set_pmax_final_url_expansion"].input_schema["properties"]["enabled"]
    assert enabled.get("type") == "boolean"
    # PMax remains required while declared Search URL additions may be pending.
    result, _ = cli(tmp_path, default=True)
    success(result, expected_pending(server))


@pytest.mark.parametrize("tool", sorted(PMAX_MUTATIONS))
@pytest.mark.parametrize("case", SAFETY_CASES)
def test_final_exhaustive_guardrail_walk_includes_every_new_operation(tmp_path, monkeypatch, tool, case):
    final_server(tmp_path)
    safeguard(tmp_path, monkeypatch, tool, case)


@pytest.mark.parametrize("tool", sorted(PMAX_READS))
def test_final_new_reads_reject_unknown_arguments_and_keep_ro_rw_parity(tmp_path, tool):
    rw, client = final_server(tmp_path)
    ro, ro_client = final_server(tmp_path, read_only=True)
    assert h.call(rw, tool, READ_ARGS[tool]) == h.call(ro, tool, READ_ARGS[tool])
    rejected(rw, client, tool, {**READ_ARGS[tool], "unexpected_option": True}, local=True)


@pytest.mark.parametrize("tool", sorted(PMAX_MUTATIONS))
def test_final_all_new_writes_are_unregistered_readonly(tmp_path, tool):
    server, client = final_server(tmp_path, read_only=True)
    result = h.call_result(server, tool, PMAX_ARGS[tool])
    assert result.is_error and "unknown tool" in h.result_text(result).lower()
    assert not client.searches and not client.mutations


def test_final_concurrent_confirmations_write_tree_once(tmp_path):
    server, provider = final_server(tmp_path)
    plan = stage(server, "set_asset_group_product_selection")
    preview(server, plan)
    import threading
    barrier = threading.Barrier(4)
    def attempt(_):
        barrier.wait(timeout=5)
        return apply(server, plan)
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(attempt, range(4)))
    assert sum(r.get("applied") is True for r in results) == 1
    assert [h.error_of(r)["code"] for r in results if "error" in r] == ["PLAN_CONSUMED"] * 3
    assert len(provider.live_mutations()) == 1


INSTALLED_INJECTION = process_plumbing.INJECTION + r'''
from pmax_oracle import PMaxClient
class InstalledPMaxTransport(PMaxClient):
    def _do_search(self, service, method, args, kwargs):
        mode = json.loads(control.read_text())
        if "group_status" in mode:
            self.data[customer]["asset_group"][0]["asset_group"]["status"] = mode["group_status"]
        return super()._do_search(service, method, args, kwargs)
    def _do_mutation(self, service, method, args, kwargs):
        result = super()._do_mutation(service, method, args, kwargs)
        request = self.mutations[-1].request
        mark("pmax mutation", service=service, method=method,
             proto=request._pb.DESCRIPTOR.name,
             request=MessageToDict(request._pb, preserving_proto_field_name=True))
        return result
transport = InstalledPMaxTransport()
'''


@pytest.mark.parametrize("customer", [h.CUSTOMER_ID, h.OTHER_CUSTOMER_ID])
def test_installed_entrypoint_exposes_and_drives_every_new_workflow(tmp_path, monkeypatch, customer):
    # Availability is asserted first so the initial red is missing behavior,
    # never a process bootstrap or input-plumbing error.
    final_server(tmp_path)
    monkeypatch.setattr(process_plumbing, "INJECTION", INSTALLED_INJECTION)
    with process_plumbing.InstalledServer(tmp_path / "installed", customer=customer,
             env={"ADS_MCP_REQUIRE_DRY_RUN": "true"}) as installed:
        listing = installed.receive(installed.send("tools/list", {}))["result"]["tools"]
        assert_catalog({tool["name"] for tool in listing}, final=True)
        for name, args in READ_ARGS.items():
            payload = h.expect_ok(installed.call(name, args))
            assert payload["customer_id"] == customer
        actions = {**PMAX_ARGS, "pause_entity": {"entity_type": "asset_group", "entity_id": "801"},
            "enable_entity": {"entity_type": "asset_group", "entity_id": "801"}}
        for name in sorted(actions):
            installed.mode(group_status="PAUSED" if name == "enable_entity" else "ENABLED")
            plan = h.expect_ok(installed.call(name, deepcopy(actions[name])))["plan"]
            assert h.error_of(installed.call("confirm_and_apply", {"plan_id": plan["id"], "dry_run": False}))["code"] == "DRY_RUN_REQUIRED"
            assert h.expect_ok(installed.call("confirm_and_apply", {"plan_id": plan["id"], "dry_run": True}))["applied"] is False
            result = h.expect_ok(installed.call("confirm_and_apply", {"plan_id": plan["id"], "dry_run": False,
                "confirm_irreversible": name in PMAX_IRREVERSIBLE}))
            assert result["applied"] is True
            assert h.error_of(installed.call("confirm_and_apply", {"plan_id": plan["id"], "dry_run": False,
                "confirm_irreversible": True}))["code"] == "PLAN_CONSUMED"
        writes = installed.events("pmax mutation")
        assert len(writes) == 9 and all(event["request"]["customer_id"] == customer for event in writes)
        assert {event["proto"] for event in writes} == {"MutateAssetGroupSignalsRequest", "MutateCampaignsRequest",
            "MutateCampaignCriteriaRequest", "MutateAssetGroupListingGroupFiltersRequest", "MutateAssetGroupsRequest"}


def test_all_read_goldens_are_owned_and_final_parity_checks_the_merged_inventory(tmp_path):
    final_server(tmp_path)
    directory = tmp_path / "all-fixtures"
    directory.mkdir()
    fixtures = list(h.CONTRACT_DIR.glob("*.json")) + list(PMAX_FIXTURES.glob("*.json")) + list((ROOT / "tests/fixtures/search_urls").glob("*.json"))
    names = [h.load_contract_fixture(path)["tool"] for path in fixtures]
    assert len(names) == len(set(names)) == 27 and set(names) == READ_TOOLS | PMAX_READS | SEARCH_URL_READS
    server, _ = final_server(tmp_path)
    names = [name for name in names if name in h.tool_names(server)]
    for path in fixtures:
        if h.load_contract_fixture(path)["tool"] in names:
            shutil.copy2(path, directory / path.name)
    result = subprocess.run([sys.executable, str(ROOT / "scripts/parity.py"), "--fixtures", str(directory), "--report", "-"],
        capture_output=True, text=True, timeout=60, env=h.scrubbed_env(), cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    for name in names:
        assert re.search(r"(?m)^" + re.escape(name) + r"\s+MATCH\s*$", result.stdout), result.stdout


def test_source_archive_inventory_requires_new_oracles_goldens_and_guide(tmp_path):
    final_server(tmp_path)
    checker = load_script("check_release_archives")
    required = ["LICENSE", "SECURITY.md", "CONTRIBUTING.md", "tests/fixtures/capability_requirements.json",
        "tests/pmax_oracle.py", "docs/pmax.md", *["tests/" + name for name in (
            "test_pmax_lifecycle_contract.py", "test_pmax_signals_contract.py", "test_pmax_url_controls_contract.py",
            "test_pmax_product_selection_contract.py", "test_pmax_workflow_contract.py")],
        "tests/test_pmax_provider_boundary_contract.py", "tests/fixtures/pmax_provider_fields_v25.json",
        *["tests/fixtures/pmax/" + name + ".json" for name in sorted(PMAX_READS)]]
    complete = [*required, "tests/search_url_oracle.py", "tests/test_search_ad_urls_contract.py",
        "tests/test_keyword_urls_contract.py", "tests/test_search_url_workflow_contract.py",
        "tests/test_search_url_oracle_controls.py", "tests/fixtures/search_url_provider_fields_v25.json",
        "docs/search-urls.md", *["tests/fixtures/search_urls/" + name + ".json" for name in sorted(SEARCH_URL_READS)]]
    checker.check_inventory(complete, source_archive=True)
    for name in required:
        with pytest.raises(ValueError):
            checker.check_inventory([path for path in complete if path != name], source_archive=True)


def test_public_pmax_guide_is_linked_truthful_and_generated_reference_current(tmp_path):
    final_server(tmp_path)
    guide = ROOT / "docs/pmax.md"
    assert guide.is_file(), "Add the public PMax workflow guide"
    text = guide.read_text()
    lower = text.lower()
    for name in sorted(PMAX_ADDITIONS | {"pause_entity", "enable_entity", "confirm_and_apply", "get_listing_groups"}):
        assert name in text
    for concepts in (("50", "80"), ("998", "1000", "128"), ("preview", "dry_run"), ("stale",),
        ("irreversible", "confirm_irreversible"), ("spend", "delivery"), ("audience", "scope"),
        ("conflict", "documentation"), ("optimization", "targeting"), ("merchant center", "final url"),
        ("text", "customization"), ("item", "case"), ("nested",), ("live", "acceptance")):
        assert all(word in lower for word in concepts), concepts
    readme = (ROOT / "README.md").read_text()
    assert any(total in readme and reads in readme for total, reads in (("63", "25"), ("65", "26"), ("67", "27")))
    assert "docs/pmax.md" in (ROOT / "README.md").read_text()
    migration = (ROOT / "docs/migration.md").read_text()
    assert any(total in migration and reads in migration for total, reads in (("63", "25"), ("65", "26"), ("67", "27"))) and "pmax" in migration.lower()
    assert "unreleased" in (ROOT / "CHANGELOG.md").read_text().lower() and "pmax" in (ROOT / "CHANGELOG.md").read_text().lower()
    for filename in ("configuration.md", "migration.md", "tools.md"):
        assert (ROOT / "docs" / filename).is_file()
    generated = subprocess.run([sys.executable, str(ROOT / "scripts/gen_tools_md.py"), "--stdout"],
        capture_output=True, text=True, timeout=30, env=h.scrubbed_env())
    assert generated.returncode == 0 and generated.stdout.strip() == (ROOT / "docs/tools.md").read_text().strip()
    assert all(name in generated.stdout for name in PMAX_ADDITIONS)
    # Mechanical layout/link checks complement the required rendered inspection.
    assert sum(line.startswith("```") for line in text.splitlines()) % 2 == 0
    for target in re.findall(r"\[[^\]]+\]\(([^)]+)\)", text):
        if "://" in target or target.startswith("#"):
            continue
        relative, _, anchor = target.partition("#")
        linked = guide.parent / relative
        assert linked.is_file(), target
        if anchor:
            headings = [re.sub(r"[^a-z0-9 _-]", "", line.lstrip("# ").lower()).replace(" ", "-")
                        for line in linked.read_text().splitlines() if line.startswith("#")]
            assert anchor in headings, target
