"""F096 repair: comparison accuracy, changelog coverage and positioning wording.

Reproduces the verify findings S7 (the README's Google comparison misstated
the argument differences), S8 (the CHANGELOG's Unreleased section left out
credential sources, the HTTP transport, the new dependencies and the
positioning), N11 (the dry-run preview described as unconditional), N12
(list_accounts' account source) and the N14 page_size default, plus the guide
statements that follow from F094 and F095.

Only statements that are new in this round are asserted; wording already
present at HEAD does not satisfy them. Section, link and public-safety helpers
are the F092 ones (tests/test_google_parity_docs_contract.py), so the same
GitHub slug rules and placeholder-id allow-list apply.
"""
import re
import subprocess
import sys

import pytest

import harness as h
from test_google_parity_docs_contract import (
    GUIDES,
    README,
    ROOT,
    assert_links_resolve,
    assert_public_safe,
    normalized,
    opening,
    paragraphs,
    section,
    sentences,
)

CHANGELOG = ROOT / "CHANGELOG.md"
COMPARISON = r"Coming from Google[’']s server"
DEPENDENCY_FLOORS = (("PyYAML", "6.0.3"), ("google-auth", "2.61.0"), ("starlette", "1.7.0"), ("uvicorn", "0.54.0"))


def comparison_units():
    """Table rows and prose sentences of the README comparison section."""
    part = section(README.read_text(), COMPARISON)
    rows = [normalized(line) for line in part.splitlines() if line.strip().startswith("|")]
    prose = "\n".join(line for line in part.splitlines() if not line.strip().startswith("|"))
    return rows + sentences(prose)


def units_naming(tool):
    units = [unit for unit in comparison_units() if f"`{tool}`" in unit]
    assert units, f"the comparison never names `{tool}`"
    return units


def assert_some_unit(units, checks, what):
    missing_by_unit = []
    for unit in units:
        missing = [label for label, pattern in checks if not re.search(pattern, unit, re.I)]
        if not missing:
            return unit
        missing_by_unit.append(missing)
    pytest.fail(f"{what}: no row or sentence states all of it; missing per candidate: {missing_by_unit}")


# ---------------------------------------------------------------------------
# S7 and N12: the comparison states the real argument differences.


def test_comparison_states_that_googles_search_takes_structured_arguments_and_run_gaql_one_query():
    assert_some_unit(units_naming("search"), [
        ("customer_id", r"`customer_id`"), ("required", r"\brequire[sd]?\b"), ("fields", r"`fields`"),
        ("resource", r"`resource`"), ("conditions", r"`conditions`"), ("orderings", r"`orderings`"),
        ("limit", r"`limit`"), ("login_customer_id", r"`login_customer_id`"), ("optional", r"\boptional\b"),
        ("GAQL", r"\bGAQL\b"), ("query string", r"\bstring\b"), ("run_gaql", r"`run_gaql`|\brun_gaql\b"),
    ], "Google's search takes customer_id (required), fields, resource, conditions, orderings, limit and an "
       "optional login_customer_id, while run_gaql takes one GAQL query string")


def test_comparison_states_the_metadata_argument_rename_from_resource_name_to_name():
    assert_some_unit(units_naming("get_resource_metadata"), [
        ("resource_name", r"`resource_name`"), ("name", r"`name`"),
    ], "Google's get_resource_metadata argument is resource_name while ads-mcp's is name")


def test_comparison_states_where_each_server_finds_its_accounts():
    assert_some_unit(units_naming("list_accessible_customers") + units_naming("list_accounts"), [
        ("directly accessible", r"\bdirect(?:ly)?\b"), ("hierarchy", r"\bhierarch"),
        ("login customer id", r"login customer id|`?GOOGLE_ADS_LOGIN_CUSTOMER_ID`?|login_customer_id"),
        ("otherwise", r"\botherwise\b"),
        ("configured customer id", r"`?GOOGLE_ADS_CUSTOMER_ID`?|configured customer id"),
        ("google-ads.yaml override", r"google-ads\.yaml"), ("override", r"\boverrid"),
    ], "list_accessible_customers lists directly accessible accounts while list_accounts walks the hierarchy "
       "under the login customer id, otherwise the configured customer id, with GOOGLE_ADS_LOGIN_CUSTOMER_ID "
       "overriding a google-ads.yaml login id")


# ---------------------------------------------------------------------------
# S8: the Unreleased section covers the whole cycle.


def test_changelog_unreleased_covers_the_whole_cycle_and_this_rounds_repairs():
    text = normalized(section(CHANGELOG.read_text(), r"Unreleased"))
    required = [
        ("field metadata", r"`get_resource_metadata`"), ("GAQL hints", r"\bhints\b"),
        ("credential sources", r"GOOGLE_ADS_CREDENTIAL_SOURCE"), ("ADC", r"\badc\b|Application Default Credentials"),
        ("google-ads.yaml", r"google-ads\.yaml|google_ads_yaml"),
        ("HTTP transport", r"--transport http"), ("new listening surface", r"\blisten"),
        ("read-only HTTP", r"read-only"), ("guarded-writes positioning", r"guarded writes"),
        ("F093 deadline repair", r"\bdeadline\b"), ("F094 login id repair", r"login_customer_id"),
        ("F095 body timeouts", r"time[sd]? out|timeouts?\b"),
    ]
    missing = [label for label, pattern in required if not re.search(pattern, text, re.I)]
    floors = [f"{name} {version}" for name, version in DEPENDENCY_FLOORS
              if not re.search(re.escape(name) + r"[^.\n]{0,24}?" + re.escape(version), text, re.I)]
    assert not missing and not floors, (
        f"CHANGELOG 'Unreleased' must cover the whole cycle; missing: {missing}; dependency floors: {floors}")
    assert_links_resolve([CHANGELOG])
    assert_public_safe(CHANGELOG)


# ---------------------------------------------------------------------------
# N11: the dry-run preview is required by default, not unconditionally.


def test_readme_lead_says_the_dry_run_preview_is_required_by_default():
    lead = opening(README.read_text())
    stated = [s for s in sentences(lead) if re.search(r"dry[- ]run", s, re.I)]
    assert stated, "the README lead must mention the dry-run preview"
    assert any("ADS_MCP_REQUIRE_DRY_RUN" in s and re.search(r"\bdefault\b", s, re.I) for s in stated), (
        f"the lead must say the preview is required by default (ADS_MCP_REQUIRE_DRY_RUN): {stated}")


def test_server_description_says_the_dry_run_preview_is_required_by_default(tmp_path):
    description = h.build_server(tmp_path, client=h.FakeGoogleAdsClient()).description
    assert "ADS_MCP_REQUIRE_DRY_RUN" in description and re.search(r"\bdefault\b", description, re.I), (
        f"the server description must say the preview is required by default: {description!r}")


# ---------------------------------------------------------------------------
# N14: the page_size default lives in the schema; tools.md is generated from it.


def test_metadata_schema_declares_page_size_default_100_and_tools_md_is_generated_from_it(tmp_path):
    schema = h.tool_map(h.build_server(tmp_path, client=h.FakeGoogleAdsClient()))["get_resource_metadata"].input_schema
    assert schema["properties"]["page_size"].get("default") == 100, schema["properties"]["page_size"]
    docs = (ROOT / "docs/tools.md").read_text()
    entry = docs.split("### `get_resource_metadata`", 1)[1].split("\n### ", 1)[0]
    row = [line for line in entry.splitlines() if line.startswith("| `page_size` |")]
    assert row and row[0].rstrip().endswith("| `100` |"), row
    generated = subprocess.run([sys.executable, str(ROOT / "scripts/gen_tools_md.py"), "--stdout"],
                               cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert generated.returncode == 0 and generated.stdout == docs, (
        "docs/tools.md must be regenerated, never hand-edited")


# ---------------------------------------------------------------------------
# Guides follow the F094 and F095 behavior.


def test_http_guide_states_the_repaired_host_rule_url_methods_timeouts_and_drain():
    path = GUIDES["http"]
    text = path.read_text()
    units = [normalized(line) for line in text.splitlines() if line.strip().startswith("|")]
    units += sentences(text) + paragraphs(text)
    checks = {
        "Host/Origin accept the --host literal": [r"\bHost\b", r"--host"],
        "host comparison is case-insensitive": [r"\bHost\b", r"case[- ]insensitiv|any (?:letter )?case|\bcase\b"],
        "localhost prints http://127.0.0.1": [r"`?localhost`?", r"http://127\.0\.0\.1:"],
        "GET and DELETE answer 405": [r"\bGET\b", r"\bDELETE\b", r"\b405\b"],
        "body timeouts": [r"\b408\b|time[sd]? out|timeout", r"\b10\b", r"\b30\b", r"\bidle\b|no bytes|silen"],
        "413 drain": [r"\b413\b", r"8 MiB", r"drain|keeps? reading|reads? the rest|finish(?:es)? sending"],
    }
    missing = [label for label, patterns in checks.items()
               if not any(all(re.search(p, unit, re.I if p != r"\bHost\b" else 0) for p in patterns) for unit in units)]
    assert not missing, f"docs/http-transport.md does not state: {missing}"
    assert_public_safe(path)


def test_credential_guide_states_the_unquoted_number_rule_and_that_string_forms_keep_working():
    path = GUIDES["credentials"]
    text = path.read_text()
    blocks = paragraphs(text)
    forms = [("octal", r"\boctal\b"), ("hex", r"\bhex"), ("underscores", r"underscore"),
             ("sexagesimal", r"sexagesimal|colon"), ("sign", r"\bsign"),
             ("decimal point", r"decimal point|float|fraction|\.0\b")]
    rule = [block for block in blocks if re.search(r"\bunquoted\b", block, re.I)
            and re.search(r"\bten\b|\b10\b", block, re.I) and re.search(r"digit", block, re.I)]
    assert rule, "state the rule for an unquoted login_customer_id that YAML reads as a number"
    best = min(([label for label, pattern in forms if not re.search(pattern, block, re.I)] for block in rule), key=len)
    assert not best, f"the unquoted-number rule must name the refused literal forms; missing: {best}"
    kept = [s for s in sentences(text) if re.search(r"\bstring", s, re.I) and "123-456-7890" in s]
    assert kept, "state, with the placeholder id 123-456-7890, that string forms keep working"
    assert_public_safe(path)


# ---------------------------------------------------------------------------
# Rendering.


def test_changed_docs_resolve_links_and_stay_public_safe():
    assert_links_resolve([README, CHANGELOG, *GUIDES.values()])
    for path in (README, *GUIDES.values()):
        assert_public_safe(path)
