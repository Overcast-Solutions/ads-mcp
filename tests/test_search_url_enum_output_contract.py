"""Search URL enum refusals preserve normal installed-console diagnostics.

Only the Google Ads client factory returns a synthetic transport. Console
dispatch, protobuf enum conversion, URL plans, audit and stderr remain real.
"""
from copy import deepcopy
import hashlib
import json
from pathlib import Path

import pytest

import harness as h
import test_auth_cause_contract as process_plumbing
from offline_contract import load_script
from search_url_oracle import AFTER_FINAL, BEFORE_FINAL, KINDS, ROOT, args


UNKNOWN_ENUM = 918273
CONTRACT_MEMBER = "tests/test_search_url_enum_output_contract.py"
PARENT_ENUMS = [
    ("campaign", "campaign.status"),
    ("campaign", "campaign.advertising_channel_type"),
    ("ad_group", "ad_group.status"),
    ("ad_group", "ad_group.type_"),
]
ENUM_CASES = [
    *[(kind, resource, path) for kind in ("ad", "keyword") for resource, path in PARENT_ENUMS],
    ("ad", "ad_group_ad", "ad_group_ad.status"),
    ("ad", "ad_group_ad", "ad_group_ad.ad.type_"),
    ("ad", "ad_group_ad", "ad_group_ad.ad.responsive_search_ad.headlines.0.pinned_field"),
    ("ad", "ad_group_ad", "ad_group_ad.ad.responsive_search_ad.descriptions.0.pinned_field"),
    ("keyword", "ad_group_criterion", "ad_group_criterion.status"),
    ("keyword", "ad_group_criterion", "ad_group_criterion.type_"),
    ("keyword", "ad_group_criterion", "ad_group_criterion.keyword.match_type"),
]

# The subprocess imports the installed product before adding the test helper
# directory. No repository package path is inserted into its import search.
INJECTION = r'''
import atexit
from copy import deepcopy
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import sys
import threading
import warnings

import mcp
from google.ads.googleads.client import GoogleAdsClient
from google.protobuf.json_format import MessageToDict

def warning_configuration():
    return (warnings.filters, list(warnings.filters), warnings.showwarning,
            warnings.warn, warnings.filterwarnings, warnings.simplefilter)
def same_warning_configuration(before):
    after = warning_configuration()
    return after[0] is before[0] and after[1:] == before[1:]

dependency_warning_configuration = warning_configuration()
import ads_mcp
import ads_mcp.server

sys.path.insert(0, os.environ["BOUNDARY_TESTS"])
import harness as h
from search_url_oracle import SearchClient, corrupt_path, set_path

control = Path(os.environ["BOUNDARY_CONTROL"])
marker = Path(os.environ["BOUNDARY_MARKER"])
lock = threading.Lock()
def mark(event, **fields):
    with lock, marker.open("a") as stream:
        stream.write(json.dumps({"event": event, **fields}) + "\n")

mark("loaded", integer_limit=sys.get_int_max_str_digits())
def deny(event, arguments):
    if event == "socket.bind" and arguments[1] == ("::1", 0):
        raise OSError("offline IPv6 probe")
    if event in ("socket.connect", "socket.bind", "socket.getaddrinfo"):
        mark("network attempted", operation=event)
        raise OSError("synthetic URL transport forbids network")
sys.addaudithook(deny)

class EnumTransport(SearchClient):
    def __init__(self):
        super().__init__()
        self.settings = None
        self.diagnostics = set()
        self.warning_state = None

    def observe_warnings(self):
        if self.warning_state is None:
            self.warning_state = warning_configuration()
        mark("warning configuration", unchanged=same_warning_configuration(self.warning_state))

    def _do_search(self, service, method, arguments, kwargs):
        self.observe_warnings()
        mode = json.loads(control.read_text())
        if mode != self.settings:
            self.corrupt = {}
            for resource, path, value in mode.get("corrupt", []):
                corrupt_path(self, resource, path, value)
            for customer, path, value in mode.get("changes", []):
                set_path(self.data[customer], path, value)
            self.settings = deepcopy(mode)
        diagnostic = mode.get("diagnostic")
        if diagnostic and diagnostic not in self.diagnostics:
            self.diagnostics.add(diagnostic)
            warnings.warn("Synthetic URL transport diagnostic " + diagnostic, UserWarning)
        mark("query", customer_id=str(h._req_field(arguments, kwargs, "customer_id")),
             query=str(h._req_field(arguments, kwargs, "query")))
        for row in super()._do_search(service, method, arguments, kwargs):
            # Raw protobuf reflection records injected enum numbers without
            # reading the proto-plus property that the product must handle.
            for resource, path, value in mode.get("corrupt", []):
                if resource != h._FROM_RE.search(str(h._req_field(arguments, kwargs, "query"))).group(1):
                    continue
                raw = row._pb
                for part in path.split("."):
                    if part.isdecimal():
                        raw = raw[int(part)]
                    else:
                        field = part if part in raw.DESCRIPTOR.fields_by_name else part.rstrip("_")
                        raw = getattr(raw, field)
                mark("enum row", path=path, value=raw, expected=value,
                     proto=row._pb.DESCRIPTOR.full_name)
            yield row
            self.observe_warnings()

    def _do_mutation(self, service, method, arguments, kwargs):
        before = deepcopy(self.data)
        response = super()._do_mutation(service, method, arguments, kwargs)
        call = self.mutations[-1]
        mark("mutation", service=service, method=method,
             proto=call.request._pb.DESCRIPTOR.full_name,
             request=MessageToDict(call.request._pb, preserving_proto_field_name=True),
             validate_only=call.validate_only, before=before, after=self.data)
        return response

transport = EnumTransport()
def factory(*arguments, **kwargs):
    mark("client factory")
    return transport
GoogleAdsClient.load_from_dict = factory

def finished():
    transport.observe_warnings()
    files = {}
    for name, module in tuple(sys.modules.items()):
        if name == "ads_mcp" or name.startswith("ads_mcp."):
            path = Path(module.__file__).resolve()
            files[name] = {"path": str(path), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    distribution = importlib.metadata.distribution("ads-mcp")
    mark("product binding", package=str(Path(ads_mcp.__file__).resolve().parent),
         files=files, executable=sys.argv[0], prefix=sys.prefix,
         warning_configuration_unchanged=same_warning_configuration(dependency_warning_configuration),
         entrypoints={entry.name: entry.value for entry in distribution.entry_points
                      if entry.group == "console_scripts"})
    mark("finished", module_file=ads_mcp.server.__file__,
         integer_limit=sys.get_int_max_str_digits())
atexit.register(finished)
'''


class EnumConsole(process_plumbing.InstalledServer):
    """Reuse declared JSON-RPC plumbing and reap startup failures too."""
    def __init__(self, root):
        try:
            super().__init__(root, env={"ADS_MCP_REQUIRE_DRY_RUN": "true"})
        except BaseException:
            self.reap()
            raise

    def reap(self):
        process = getattr(self, "process", None)
        if process is None:
            return
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)
        for reader in getattr(self, "readers", []):
            reader.join(timeout=3)
        for pipe in (process.stdin, process.stdout, process.stderr):
            if pipe is not None:
                pipe.close()

    def __exit__(self, *exception):
        try:
            return super().__exit__(*exception)
        finally:
            self.reap()
            (self.root / "stdout.jsonl").write_text("".join(self.public))
            (self.root / "stderr.log").write_text("".join(self.stderr))


@pytest.fixture
def console(tmp_path, monkeypatch):
    monkeypatch.setattr(process_plumbing, "INJECTION", INJECTION)
    # Ambient warning policy must not silence the channel under test.
    monkeypatch.delenv("PYTHONWARNINGS", raising=False)
    monkeypatch.delenv("PYTHONHOME", raising=False)
    return EnumConsole(tmp_path / "console")


def assert_product_binding(console):
    binding, = console.events("product binding")
    package = Path(binding["package"])
    assert package == ROOT / "ads_mcp" or Path(binding["prefix"]) in package.parents
    assert Path(binding["executable"]).resolve() == h.console_script().resolve()
    assert binding["entrypoints"]["ads-mcp"] == "ads_mcp.server:main"
    assert binding["warning_configuration_unchanged"], "Product import must preserve the warning policy"
    assert {"ads_mcp.server", "ads_mcp.search_urls", "ads_mcp.tools.registry"} <= binding["files"].keys()
    for evidence in binding["files"].values():
        relative = Path(evidence["path"]).relative_to(package)
        assert evidence["sha256"] == hashlib.sha256((ROOT / "ads_mcp" / relative).read_bytes()).hexdigest()
    assert all(event["unchanged"] for event in console.events("warning configuration"))


def stage(console, kind):
    return h.expect_ok(console.call(KINDS[kind]["write"], args(kind, final_urls=AFTER_FINAL)))["plan"]


def preview(console, plan):
    result = h.expect_ok(console.call("confirm_and_apply", {"plan_id": plan["id"], "dry_run": True}))
    assert result["applied"] is False and result["plan"]["operations"] == plan["operations"]
    assert not console.events("mutation")


@pytest.mark.parametrize("kind,resource,path", ENUM_CASES,
                         ids=[kind + ":" + path for kind, _, path in ENUM_CASES])
@pytest.mark.parametrize("phase", ["inspect", "stage", "apply"])
def test_unknown_consumed_enum_refuses_without_console_warning(console, kind, resource, path, phase):
    with console:
        if phase == "apply":
            plan = stage(console, kind)
            preview(console, plan)
        console.mode(corrupt=[(resource, path, UNKNOWN_ENUM)])
        if phase == "apply":
            result = console.call("confirm_and_apply", {"plan_id": plan["id"], "dry_run": False})
        else:
            tool = KINDS[kind]["read" if phase == "inspect" else "write"]
            result = console.call(tool, args(kind, **({"final_urls": AFTER_FINAL} if phase == "stage" else {})))
        expected = "STALE_PLAN" if phase == "apply" else "SEARCH_URL_STATE_UNVERIFIED"
        assert h.error_of(result)["code"] == expected
        assert not console.events("mutation")
        rows = console.events("enum row")
        assert rows and all(row["value"] == UNKNOWN_ENUM and row["expected"] == UNKNOWN_ENUM
                            and row["proto"] == "google.ads.googleads.v25.services.GoogleAdsRow" for row in rows)
        public = json.dumps(result) + json.dumps(console.audit())
        assert str(UNKNOWN_ENUM) not in public and "Traceback" not in public
        h.assert_no_secrets(public)
    assert_product_binding(console)
    assert "".join(console.stderr) == "", "Unknown provider enum emitted an installed-console diagnostic"


AD_CONTROLS = [
    ("ENABLED", "HEADLINE_1", "DESCRIPTION_1"),
    ("PAUSED", "HEADLINE_2", "DESCRIPTION_2"),
    ("ENABLED", "HEADLINE_3", "UNSPECIFIED"),
    ("PAUSED", "UNSPECIFIED", "UNSPECIFIED"),
]
VALID_CASES = [("ad", status, headline, description) for status, headline, description in AD_CONTROLS]
VALID_CASES += [("keyword", status, match, None) for status in ("ENABLED", "PAUSED")
                for match in ("EXACT", "PHRASE", "BROAD")]


@pytest.mark.parametrize("kind,status,enum_value,description", VALID_CASES,
                         ids=["-".join(str(value) for value in case if value is not None) for case in VALID_CASES])
def test_valid_enums_keep_precise_inspection_preview_and_application(console, kind, status, enum_value, description):
    spec = KINDS[kind]
    changes = [(h.CUSTOMER_ID, "campaign.0.campaign.status", status),
               (h.CUSTOMER_ID, "ad_group.0.ad_group.status", status),
               (h.CUSTOMER_ID, spec["resource"] + ".0." + spec["resource"] + ".status", status)]
    if kind == "ad":
        creative = "ad_group_ad.0.ad_group_ad.ad.responsive_search_ad."
        changes += [(h.CUSTOMER_ID, creative + "headlines.0.pinned_field", enum_value),
                    (h.CUSTOMER_ID, creative + "descriptions.0.pinned_field", description)]
    else:
        changes.append((h.CUSTOMER_ID, "ad_group_criterion.0.ad_group_criterion.keyword.match_type", enum_value))
    with console:
        console.mode(changes=changes)
        inspected = h.expect_ok(console.call(spec["read"], args(kind)))
        assert inspected[spec["entity"]]["final_urls"] == BEFORE_FINAL
        assert inspected["campaign"]["status"] == inspected["ad_group"]["status"] == status
        assert inspected["campaign"]["advertising_channel_type"] == "SEARCH"
        assert inspected["ad_group"]["type"] == "SEARCH_STANDARD"
        assert inspected[spec["entity"]]["status"] == status
        if kind == "ad":
            creative = inspected["ad"]["responsive_search_ad"]
            assert [asset["pinned_field"] for asset in creative["headlines"]] == [enum_value, "UNSPECIFIED", "UNSPECIFIED"]
            assert [asset["pinned_field"] for asset in creative["descriptions"]] == [description, "UNSPECIFIED"]
        else:
            assert inspected["keyword"]["match_type"] == enum_value
        plan = stage(console, kind)
        preview(console, plan)
        assert h.expect_ok(console.call("confirm_and_apply", {"plan_id": plan["id"], "dry_run": False}))["applied"]
        mutation, = console.events("mutation")
        identity = f"customers/{h.CUSTOMER_ID}/{spec['path']}/" + ("601" if kind == "ad" else "801~601")
        assert (mutation["service"], mutation["method"]) == (spec["service"], spec["method"])
        assert mutation["proto"] == "google.ads.googleads.v25.services." + spec["request"]
        assert not mutation["validate_only"]
        assert mutation["request"] == {"customer_id": h.CUSTOMER_ID, "operations": [{
            "update": {"resource_name": identity, "final_urls": AFTER_FINAL}, "update_mask": "finalUrls"}]}
        expected_data = deepcopy(mutation["before"])
        target = expected_data[h.CUSTOMER_ID][spec["resource"]][0][spec["resource"]]
        (target["ad"] if kind == "ad" else target)["final_urls"] = AFTER_FINAL
        assert mutation["after"] == expected_data, "Only the selected destination may change"
        reread = h.expect_ok(console.call(spec["read"], args(kind)))
        expected_state = deepcopy(inspected)
        expected_state[spec["entity"]]["final_urls"] = AFTER_FINAL
        assert reread == expected_state
    assert_product_binding(console)
    assert "".join(console.stderr) == ""


@pytest.mark.parametrize("kind", ["ad", "keyword"])
def test_unrelated_provider_warnings_remain_visible_during_and_after_refusal(console, kind):
    with console:
        console.mode(corrupt=[("campaign", "campaign.status", UNKNOWN_ENUM)], diagnostic="during refusal")
        assert h.error_of(console.call(KINDS[kind]["read"], args(kind)))["code"] == "SEARCH_URL_STATE_UNVERIFIED"
        console.mode(diagnostic="after refusal")
        h.expect_ok(console.call(KINDS[kind]["read"], args(kind)))
        assert not console.events("mutation")
    assert_product_binding(console)
    stderr = "".join(console.stderr)
    for marker in ("Synthetic URL transport diagnostic during refusal", "Synthetic URL transport diagnostic after refusal"):
        assert marker in stderr, "The repair must retain unrelated warning diagnostics"


def test_source_archive_admission_requires_enum_output_regression():
    checker = load_script("check_release_archives")
    inventory = set(checker.REQUIRED_SOURCE) | {CONTRACT_MEMBER}
    assert all((ROOT / name).is_file() for name in inventory)
    checker.check_inventory(inventory, source_archive=True)
    with pytest.raises(ValueError, match="source inventory"):
        checker.check_inventory(inventory - {CONTRACT_MEMBER}, source_archive=True)


def test_source_archive_inventory_valid_control_accepts_complete_source():
    checker = load_script("check_release_archives")
    required = set(checker.REQUIRED_SOURCE) | {CONTRACT_MEMBER}
    assert all((ROOT / name).is_file() for name in required)
    checker.check_inventory(required, source_archive=True)
