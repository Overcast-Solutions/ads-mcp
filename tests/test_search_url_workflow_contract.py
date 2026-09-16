"""Complete Search URL catalog, installed workflows, and distribution contract."""
from copy import deepcopy
import json
import re
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

import harness as h
import test_auth_cause_contract as process_plumbing
from capability_oracle import DEFAULT, cli, empty, success
from offline_contract import load_script
from pmax_oracle import PMAX_ADDITIONS, PMAX_READS, PMAX_MUTATIONS
from search_url_oracle import (ADDITIONS, AFTER_FINAL, BEFORE_FINAL, FACTS_PATH, FIXTURES, KINDS, READS, ROOT,
    WRITES, args, assert_provider_queries, require, setup)
from tool_catalog import ALL_WRITE_MODE_TOOLS, MUTATION_TOOLS, READ_TOOLS

EXPECTED_ALL = ALL_WRITE_MODE_TOOLS | PMAX_ADDITIONS | ADDITIONS
EXPECTED_READS = READ_TOOLS | PMAX_READS | READS
EXPECTED_WRITES = MUTATION_TOOLS | PMAX_MUTATIONS | WRITES
OBLIGATIONS = {
    "get_responsive_search_ad_urls": {"name": "get_responsive_search_ad_urls", "parameters": ["ad_group_id", "ad_id", "customer_id"], "required": ["ad_group_id", "ad_id"], "values": {}},
    "update_responsive_search_ad_urls": {"name": "update_responsive_search_ad_urls", "parameters": ["ad_group_id", "ad_id", "final_urls", "final_mobile_urls", "customer_id"], "required": ["ad_group_id", "ad_id"], "values": {}},
    "get_keyword_urls": {"name": "get_keyword_urls", "parameters": ["ad_group_id", "criterion_id", "customer_id"], "required": ["ad_group_id", "criterion_id"], "values": {}},
    "update_keyword_urls": {"name": "update_keyword_urls", "parameters": ["ad_group_id", "criterion_id", "final_urls", "final_mobile_urls", "customer_id"], "required": ["ad_group_id", "criterion_id"], "values": {}},
}
REQUIRED_SOURCE = [
    "LICENSE", "SECURITY.md", "CONTRIBUTING.md", "tests/fixtures/capability_requirements.json",
    "tests/pmax_oracle.py", "docs/pmax.md", "tests/test_pmax_lifecycle_contract.py",
    "tests/test_pmax_signals_contract.py", "tests/test_pmax_url_controls_contract.py",
    "tests/test_pmax_product_selection_contract.py", "tests/test_pmax_workflow_contract.py",
    "tests/test_pmax_provider_boundary_contract.py", "tests/fixtures/pmax_provider_fields_v25.json",
    *["tests/fixtures/pmax/" + name + ".json" for name in sorted(PMAX_READS)],
    "tests/search_url_oracle.py", "tests/test_search_ad_urls_contract.py", "tests/test_keyword_urls_contract.py",
    "tests/test_search_url_enum_output_contract.py",
    "tests/test_search_url_workflow_contract.py", "tests/test_search_url_oracle_controls.py",
    "tests/fixtures/search_url_provider_fields_v25.json", "docs/search-urls.md",
    *["tests/fixtures/search_urls/" + name + ".json" for name in sorted(READS)],
]


def final_server(tmp_path):
    server, provider = setup(tmp_path, "ad")
    require(server, ADDITIONS)
    return server, provider


def test_final_catalog_requires_all_67_operations_and_27_reads(tmp_path):
    from ads_mcp.tools.registry import all_tool_specs
    server, _ = final_server(tmp_path)
    assert len(EXPECTED_ALL) == 67 and len(EXPECTED_READS) == 27
    assert h.tool_names(server) == EXPECTED_ALL
    readonly, _ = setup(tmp_path, "ad", read_only=True)
    assert h.tool_names(readonly) == EXPECTED_READS
    specs = all_tool_specs()
    assert len(specs) == len({spec.name for spec in specs}) == 67
    assert {spec.name for spec in specs if spec.kind == "read"} == EXPECTED_READS
    assert {spec.name for spec in specs if spec.kind == "mutation"} == EXPECTED_WRITES
    assert {spec.name for spec in specs if spec.kind == "apply"} == {"confirm_and_apply"}
    metadata = h.tool_map(server)
    for name, requirement in OBLIGATIONS.items():
        schema = metadata[name].input_schema
        assert set(schema["properties"]) == set(requirement["parameters"])
        assert set(schema["required"]) == set(requirement["required"])
        assert metadata[name].description.strip()


def test_independent_requirements_are_complete_and_cli_has_no_pending_additions(tmp_path):
    final_server(tmp_path)
    tools = [tool for cap in json.loads(DEFAULT.read_text())["capabilities"] for tool in cap["tools"]]
    by_name = {tool["name"]: tool for tool in tools}
    assert len(tools) == len(by_name) == 67 and set(by_name) == EXPECTED_ALL
    assert {name: by_name[name] for name in OBLIGATIONS} == OBLIGATIONS
    result, _ = cli(tmp_path, default=True)
    success(result, empty())


def test_default_parity_report_requires_and_exercises_every_authored_read(tmp_path):
    final_server(tmp_path)
    result = subprocess.run([sys.executable, str(ROOT / "scripts/parity.py"), "--report", "-"],
                            cwd=tmp_path, env=h.scrubbed_env(), capture_output=True, text=True, timeout=90)
    assert result.returncode == 0, result.stdout + result.stderr
    matches = re.findall(r"(?m)^(\w+)\s+MATCH\s*$", result.stdout)
    assert len(matches) == len(set(matches)) == 27 and set(matches) == EXPECTED_READS
    module = load_script("parity")
    paths = module.fixture_paths(None)
    assert {h.load_contract_fixture(path)["tool"] for path in paths} == EXPECTED_READS
    assert len(paths) == 27


@pytest.mark.parametrize("missing", sorted(READS))
def test_final_parity_inventory_refuses_missing_search_inspection_golden(tmp_path, missing):
    final_server(tmp_path)
    directory = tmp_path / "fixtures"
    directory.mkdir()
    paths = [*h.CONTRACT_DIR.glob("*.json"), *(ROOT / "tests/fixtures/pmax").glob("*.json"), *FIXTURES.glob("*.json")]
    for path in paths:
        if h.load_contract_fixture(path)["tool"] != missing:
            shutil.copy2(path, directory / path.name)
    with pytest.raises(ValueError, match=missing):
        load_script("parity").validate_fixture_inventory(directory)


def test_source_archive_inventory_requires_every_original_and_new_member(tmp_path):
    final_server(tmp_path)
    checker = load_script("check_release_archives")
    assert set(REQUIRED_SOURCE) <= set(checker.REQUIRED_SOURCE)
    checker.check_inventory(REQUIRED_SOURCE, source_archive=True)
    for missing in REQUIRED_SOURCE:
        with pytest.raises(ValueError):
            checker.check_inventory([name for name in REQUIRED_SOURCE if name != missing], source_archive=True)
    for path in REQUIRED_SOURCE:
        assert (ROOT / path).is_file(), path


@pytest.mark.parametrize("read_only", [True, False])
def test_installed_archive_checker_requires_exact_tool_names_not_just_counts(tmp_path, monkeypatch, read_only):
    final_server(tmp_path)
    module = load_script("check_installed")
    expected = EXPECTED_READS if read_only else EXPECTED_ALL
    class CatalogClient:
        def __init__(self, server):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *unused):
            pass
        async def list_tools(self):
            return SimpleNamespace(tools=[SimpleNamespace(name=name) for name in names])
    monkeypatch.setattr(module.mcp, "Client", CatalogClient)
    import asyncio
    names = sorted(expected)
    assert set(asyncio.run(module.catalog(read_only))) == expected
    names = sorted((expected - {next(iter(sorted(expected)))}) | {"unapproved_tool"})
    with pytest.raises(AssertionError):
        asyncio.run(module.catalog(read_only))


INSTALLED_INJECTION = process_plumbing.INJECTION + r'''
from search_url_oracle import SearchClient, entity, KINDS
from ads_mcp.audit import AuditLog
from ads_mcp.errors import ToolError
class InstalledSearchTransport(SearchClient):
    def _do_search(self, service, method, args, kwargs):
        mode = json.loads(control.read_text())
        if mode.get("drift"):
            entity(self, mode["kind"], customer)["final_url_suffix"] = "edition=changed"
        mark("search-url query", query=str(h._req_field(args, kwargs, "query")),
             customer_id=str(h._req_field(args, kwargs, "customer_id")))
        return super()._do_search(service, method, args, kwargs)
    def _do_mutation(self, service, method, args, kwargs):
        self.lose_response = json.loads(control.read_text()).get("uncertain", False)
        try:
            return super()._do_mutation(service, method, args, kwargs)
        finally:
            call = self.mutations[-1]
            mark("search-url mutation", service=service, method=method, proto=call.request._pb.DESCRIPTOR.name,
                 request=MessageToDict(call.request._pb, preserving_proto_field_name=True),
                 validate_only=call.validate_only)
transport = InstalledSearchTransport()
original_write = AuditLog.write
def observed_write(self, record, **kwargs):
    event = json.loads(control.read_text()).get("audit_event")
    if record["event"] == event:
        if event == "applied":
            return False
        raise ToolError("AUDIT_WRITE_FAILED", "Synthetic audit destination unavailable")
    return original_write(self, record, **kwargs)
AuditLog.write = observed_write
'''


@pytest.mark.parametrize("kind", ["ad", "keyword"])
@pytest.mark.parametrize("customer", [h.CUSTOMER_ID, h.OTHER_CUSTOMER_ID])
def test_installed_console_drives_inspect_preview_apply_and_reread(tmp_path, monkeypatch, kind, customer):
    final_server(tmp_path)
    monkeypatch.setattr(process_plumbing, "INJECTION", INSTALLED_INJECTION)
    spec = KINDS[kind]
    with process_plumbing.InstalledServer(tmp_path / "installed", customer=customer,
            env={"ADS_MCP_REQUIRE_DRY_RUN": "true"}) as installed:
        listing = installed.receive(installed.send("tools/list", {}))["result"]["tools"]
        assert len(listing) == 67 and {item["name"] for item in listing} == EXPECTED_ALL
        inspected = h.expect_ok(installed.call(spec["read"], args(kind)))
        assert inspected["customer_id"] == customer
        assert inspected[spec["entity"]]["final_urls"] == BEFORE_FINAL
        plan = h.expect_ok(installed.call(spec["write"], args(kind, final_urls=AFTER_FINAL)))["plan"]
        assert not plan["irreversible"] and not installed.events("search-url mutation")
        confirm = {"plan_id": plan["id"], "dry_run": False}
        assert h.error_of(installed.call("confirm_and_apply", confirm))["code"] == "DRY_RUN_REQUIRED"
        assert h.expect_ok(installed.call("confirm_and_apply", {**confirm, "dry_run": True}))["applied"] is False
        assert not [event for event in installed.events("search-url mutation") if not event["validate_only"]]
        assert h.expect_ok(installed.call("confirm_and_apply", confirm))["applied"] is True
        writes = [event for event in installed.events("search-url mutation") if not event["validate_only"]]
        assert len(writes) == 1 and writes[0]["proto"] == spec["request"] and writes[0]["service"] == spec["service"]
        request = writes[0]["request"]
        assert request["customer_id"] == customer and len(request["operations"]) == 1
        operation = request["operations"][0]
        assert operation["update"]["final_urls"] == AFTER_FINAL and operation["update_mask"] == "finalUrls"
        assert set(operation["update"]) == {"resource_name", "final_urls"}
        identity = "601" if kind == "ad" else "801~601"
        assert operation["update"]["resource_name"] == f"customers/{customer}/{spec['path']}/{identity}"
        assert h.expect_ok(installed.call(spec["read"], args(kind)))[spec["entity"]]["final_urls"] == AFTER_FINAL
        assert h.error_of(installed.call("confirm_and_apply", confirm))["code"] == "PLAN_CONSUMED"
        assert len([event for event in installed.events("search-url mutation") if not event["validate_only"]]) == 1
        assert_provider_queries([SimpleNamespace(**event) for event in installed.events("search-url query")])
        records = [record for record in installed.audit() if record.get("plan_id") == plan["id"]]
        assert records and all(record.get("customer_id") == customer for record in records)
        assert any(record["event"] == "applied" for record in records)


@pytest.mark.parametrize("kind", ["ad", "keyword"])
@pytest.mark.parametrize("fault", ["stale", "uncertain", "staging_audit", "pre_audit", "terminal_audit"])
def test_installed_errors_preserve_no_retry_and_audit_outcomes(tmp_path, monkeypatch, kind, fault):
    final_server(tmp_path)
    monkeypatch.setattr(process_plumbing, "INJECTION", INSTALLED_INJECTION)
    spec = KINDS[kind]
    with process_plumbing.InstalledServer(tmp_path / "installed", env={"ADS_MCP_REQUIRE_DRY_RUN": "true"}) as installed:
        if fault == "staging_audit":
            installed.mode(audit_event="plan_created")
            result = installed.call(spec["write"], args(kind, final_urls=AFTER_FINAL))
            assert h.error_of(result)["code"] == "AUDIT_WRITE_FAILED" and "plan" not in result
            assert not installed.events("search-url mutation")
            return
        plan = h.expect_ok(installed.call(spec["write"], args(kind, final_urls=AFTER_FINAL)))["plan"]
        confirm = {"plan_id": plan["id"], "dry_run": False}
        h.expect_ok(installed.call("confirm_and_apply", {**confirm, "dry_run": True}))
        settings = {"stale": {"drift": True, "kind": kind}, "uncertain": {"uncertain": True},
                    "pre_audit": {"audit_event": "apply_started"}, "terminal_audit": {"audit_event": "applied"}}
        installed.mode(**settings[fault])
        result = installed.call("confirm_and_apply", confirm)
        writes = [event for event in installed.events("search-url mutation") if not event["validate_only"]]
        if fault in ("stale", "pre_audit"):
            assert h.error_of(result)["code"] == ("STALE_PLAN" if fault == "stale" else "AUDIT_WRITE_FAILED")
            assert not writes
        elif fault == "uncertain":
            assert h.error_of(result)["code"] != "INTERNAL" and len(writes) == 1
            assert h.error_of(installed.call("confirm_and_apply", confirm))["code"] == "PLAN_CONSUMED"
            assert len([event for event in installed.events("search-url mutation") if not event["validate_only"]]) == 1
            assert any(record["event"] == "apply_failed" for record in installed.audit())
        else:
            assert h.expect_ok(result)["applied"] is True and result.get("audit_warning") and len(writes) == 1
        assert_provider_queries([SimpleNamespace(**event) for event in installed.events("search-url query")])
        text = json.dumps(installed.audit()) + json.dumps(result)
        h.assert_no_secrets(text)
        assert "Traceback" not in text and "synthetic-private-provider" not in text


def test_public_guide_and_generated_tool_docs_cover_the_complete_workflow(tmp_path):
    final_server(tmp_path)
    guide = ROOT / "docs/search-urls.md"
    assert guide.is_file(), "Add the Search destination workflow guide"
    text = guide.read_text()
    lower = text.lower()
    for name in ADDITIONS | {"confirm_and_apply"}:
        assert name in text
    for concepts in (("inspect", "preview", "apply"), ("re-read",), ("null", "omitted", "[]"),
            ("10", "2048", "local"), ("tracking", "mobile", "suffix"), ("search_standard",),
            ("stale", "uncertain"), ("policy", "provider"), ("keyword", "fallback"),
            ("read-only",), ("creative", "status")):
        assert all(concept in lower for concept in concepts), concepts
    assert "example.invalid" in text
    assert "no retry" in lower or "not retry" in lower or "never retry" in lower
    readme = (ROOT / "README.md").read_text()
    assert "67" in readme and "27" in readme and "docs/search-urls.md" in readme
    migration = (ROOT / "docs/migration.md").read_text()
    assert "67" in migration and "27" in migration
    changelog = (ROOT / "CHANGELOG.md").read_text().lower()
    assert "unreleased" in changelog and "keyword" in changelog and "responsive search ad" in changelog
    generated = subprocess.run([sys.executable, str(ROOT / "scripts/gen_tools_md.py"), "--stdout"],
                              capture_output=True, text=True, timeout=30, env=h.scrubbed_env())
    assert generated.returncode == 0 and generated.stdout.strip() == (ROOT / "docs/tools.md").read_text().strip()
    assert all(name in generated.stdout for name in ADDITIONS)
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
