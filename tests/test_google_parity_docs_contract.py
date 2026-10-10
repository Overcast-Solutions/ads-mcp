"""F092: guarded-writes positioning, Google comparison, new guides, Testing-mode fix.

Only statements that are new in this cycle are asserted; wording already in the
base documentation does not satisfy them. Guides are docs/credential-sources.md
and docs/http-transport.md. Relative links and in-repository anchors across
README.md and docs/ must resolve (GitHub heading slugs).
"""
import re
from pathlib import Path

import pytest

import harness as h

ROOT = Path(__file__).resolve().parents[1]
README = ROOT / "README.md"
GUIDES = {"credentials": ROOT / "docs/credential-sources.md", "http": ROOT / "docs/http-transport.md"}
NEW_SOURCE = ["docs/credential-sources.md", "docs/http-transport.md", "tests/test_google_parity_docs_contract.py"]
LINK = re.compile(r"(?<!!)\[([^\]]+)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
SECRET_SHAPES = re.compile(r"GOCSPX-[A-Za-z0-9_-]{6,}|ya29\.[A-Za-z0-9_-]{6,}|1//0[A-Za-z0-9_-]{6,}|AIza[0-9A-Za-z_-]{20,}")
PLACEHOLDER_IDS = {"123-456-7890", "111-222-3333", "1234567890", "1112223333"}


def normalized(text):
    return re.sub(r"\s+", " ", text)


def paragraphs(text):
    return [normalized(block) for block in re.split(r"\n\s*\n", text) if block.strip()]


def sentences(text):
    return re.split(r"(?<=[.!?])\s+", normalized(text))


def opening(text):
    """README text before the first second-level heading, title excluded."""
    body = text.split("\n", 1)[1] if text.startswith("# ") else text
    return body.split("\n## ", 1)[0]


def section(text, heading_pattern):
    match = re.search(r"^(#{2,4})\s*" + heading_pattern + r".*$", text, re.M | re.I)
    assert match, f"missing section matching {heading_pattern!r}"
    level = len(match.group(1))
    rest = text[match.end():]
    end = re.search(r"^#{2," + str(level) + r"}\s", rest, re.M)
    return rest[:end.start()] if end else rest


def slug(heading):
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", heading.strip().lower())
    return re.sub(r"[^\w\- ]", "", text).replace(" ", "-")


def anchors(path):
    seen, result = {}, set()
    in_fence = False
    for line in path.read_text().splitlines():
        if line.startswith("```"):
            in_fence = not in_fence
        if in_fence:
            continue
        match = re.match(r"^#{1,6}\s+(.*?)\s*#*\s*$", line)
        if match:
            base = slug(match.group(1))
            count = seen.get(base, 0)
            seen[base] = count + 1
            result.add(base if count == 0 else f"{base}-{count}")
    return result


def documents():
    return [README, *sorted((ROOT / "docs").glob("*.md"))]


def assert_links_resolve(paths):
    problems = []
    for path in paths:
        text = re.sub(r"```.*?```", "", path.read_text(), flags=re.S)
        for _, target in LINK.findall(text):
            if re.match(r"[a-z][a-z0-9+.-]*:", target, re.I):
                continue
            file_part, _, fragment = target.partition("#")
            destination = (path.parent / file_part).resolve() if file_part else path
            if not destination.exists():
                problems.append(f"{path.name}: missing {target}")
            elif fragment and destination.suffix == ".md" and fragment not in anchors(destination):
                problems.append(f"{path.name}: missing anchor {target}")
    assert not problems, problems


def assert_public_safe(path):
    text = path.read_text()
    assert not SECRET_SHAPES.search(text), f"{path.name} contains a credential-shaped value"
    for marker in ("/Users/", "/home/", "C:\\Users", "engagements/", ".shipwright", "artifacts/spec"):
        assert marker not in text, f"{path.name} contains an internal path ({marker})"
    for value in re.findall(r"\b\d{3}-\d{3}-\d{4}\b|\b\d{10}\b", text):
        assert value in PLACEHOLDER_IDS, f"{path.name} contains a non-placeholder account id {value}"


# ---------------------------------------------------------------------------
# Positioning: guarded writes are the core capability.


def test_readme_opening_leads_with_guarded_writes_and_the_read_only_start():
    text = README.read_text()
    lead = normalized(opening(text)).lower()
    for term in ("write", "plan", "preview", "audit", "cap"):
        assert term in lead, f"the README opening must lead with guarded writes ({term!r} missing)"
    assert lead.find("write") < lead.find("read-only") if "read-only" in lead else True
    switch = [s for s in sentences(opening(text)) if "ADS_MCP_READ_ONLY=false" in s]
    assert switch, "the opening must say ADS_MCP_READ_ONLY=false enables writes"
    assert any("unset" in s.lower() and "read-only" in s.lower() for s in switch), (
        "one sentence must say the server starts read-only when ADS_MCP_READ_ONLY is unset and that "
        "ADS_MCP_READ_ONLY=false enables writes")


def test_no_readme_or_guide_calls_writes_experimental():
    offenders = []
    for path in documents():
        for number, line in enumerate(path.read_text().splitlines(), start=1):
            if re.search(r"experimental", line, re.I) or re.search(r"#experimental", line, re.I):
                offenders.append(f"{path.relative_to(ROOT)}:{number}")
    assert not offenders, f"writes are described as experimental in: {offenders}"


def test_server_description_matches_the_readme_positioning(tmp_path):
    server = h.build_server(tmp_path, client=h.FakeGoogleAdsClient())
    description = server.description.lower()
    assert "write" in description, description
    assert sum(term in description for term in ("plan", "preview", "audit", "cap")) >= 2, description
    assert "experimental" not in description


# ---------------------------------------------------------------------------
# Coming from Google's server.


def test_readme_maps_googles_three_tools_with_argument_differences():
    part = section(README.read_text(), r"Coming from Google[’']s server")
    rows = [line for line in part.splitlines() if line.strip().startswith("|")]
    cells = [[cell.strip() for cell in row.strip().strip("|").split("|")] for row in rows]
    for theirs, ours in (("list_accessible_customers", "list_accounts"), ("search", "run_gaql"),
                         ("get_resource_metadata", "get_resource_metadata")):
        matches = [row for row in cells if row and re.fullmatch(r"`?" + theirs + r"`?", row[0])
                   and any(ours in cell for cell in row[1:2])]
        assert matches, f"no table row maps {theirs} to {ours}"
        assert len(matches[0]) >= 3 and matches[0][2] and not set(matches[0][2]) <= set("-: "), (
            f"the {theirs} row must state the argument differences")


def test_comparison_states_reads_only_as_of_date_what_is_missing_and_non_affiliation():
    text = README.read_text()
    part = normalized(section(text, r"Coming from Google[’']s server"))
    dated = [s for s in sentences(part) if "2026-10-09" in s]
    assert dated and any("only" in s.lower() and "read" in s.lower() for s in dated), (
        "state that Google's server exposes only read tools (as of 2026-10-09)")
    assert re.search(r"guarded\s+writes?", part, re.I), "state that ads-mcp adds guarded writes"
    lowered = part.lower()
    for missing in ("resources", "login", "oauth proxy", "cloud run"):
        assert missing in lowered, f"name what ads-mcp does not offer: {missing}"
    whole = normalized(text)
    assert re.search(r"not affiliated with or endorsed by Google", whole), "missing the non-affiliation statement"
    assert re.search(r"\bindependent\b", whole, re.I)
    lines = text.splitlines()
    assert len(lines) <= 230, f"README must stay concise ({len(lines)} lines)"
    for guide in ("docs/credential-sources.md", "docs/http-transport.md"):
        assert f"]({guide}" in text, f"README must link {guide}"


# ---------------------------------------------------------------------------
# Guides.


def test_credential_sources_guide_covers_files_adc_and_google_ads_yaml():
    path = GUIDES["credentials"]
    assert path.is_file(), "docs/credential-sources.md is missing"
    text = normalized(path.read_text())
    for term in ("GOOGLE_ADS_CREDENTIAL_SOURCE", "files", "adc", "google_ads_yaml", "--client-id-file",
                 "adwords", "GOOGLE_ADS_CONFIGURATION_FILE_PATH"):
        assert term in text, term
    order = [text.find(term) for term in ("GOOGLE_APPLICATION_CREDENTIALS", "CLOUDSDK_CONFIG",
                                          ".config/gcloud/application_default_credentials.json")]
    assert all(position >= 0 for position in order) and order == sorted(order), "document the ADC lookup order"
    for key in ("client_id", "client_secret", "refresh_token", "login_customer_id", "developer_token", "endpoint"):
        assert key in text, key
    assert re.search(r"ignor", text, re.I), "say which google-ads.yaml keys are ignored"
    assert_public_safe(path)


def test_http_transport_guide_covers_read_only_loopback_protection_and_residual_risk():
    path = GUIDES["http"]
    assert path.is_file(), "docs/http-transport.md is missing"
    text = normalized(path.read_text())
    lowered = text.lower()
    for term in ("--transport http", "read-only", "loopback", "--port 0", "host", "origin", "/mcp"):
        assert term in lowered, term
    assert re.search(r"any local process", lowered) and "account" in lowered, "state the residual local-read risk"
    assert re.search(r"single[- ]user", lowered), "recommend single-user hosts"
    assert_public_safe(path)


def test_authentication_guide_updates_the_proxy_warning_for_http():
    text = (ROOT / "docs/authentication.md").read_text()
    blocks = [block for block in paragraphs(text) if "proxy" in block.lower()]
    assert any("http-transport.md" in block or "HTTP transport" in block for block in blocks), (
        "the shared-proxy warning must address the HTTP transport consistently")


def test_authentication_guide_explains_testing_mode_expiry_and_the_branding_fix():
    text = (ROOT / "docs/authentication.md").read_text()
    blocks = paragraphs(text)
    recognize = [b for b in blocks if "invalid_grant" in b and "AUTH_TOKEN_REVOKED" in b
                 and re.search(r"seven days|7 days", b) and "Testing" in b]
    assert recognize, "explain how to recognize Testing-mode expiry (AUTH_TOKEN_REVOKED / invalid_grant, ~7 days)"
    window = " ".join(blocks[blocks.index(recognize[0]):blocks.index(recognize[0]) + 4]).lower()
    for term in ("branding", "privacy", "authorized domain", "publish"):
        assert term in window, f"the fix must mention {term}"
    assert re.search(r"re-?mint|ads-mcp-generate-token", window) and "once" in window, "re-mint once"


# ---------------------------------------------------------------------------
# Rendering, counts, safety and archives.


def test_changed_docs_resolve_links_and_anchors_and_are_public_safe():
    for path in GUIDES.values():
        assert path.is_file(), f"{path.name} is missing"
    assert_links_resolve(documents())
    for path in (README, *GUIDES.values(), ROOT / "docs/authentication.md", ROOT / "docs/configuration.md"):
        assert_public_safe(path)
    stale = re.compile(r"\b84 (?:total )?operations\b|\b34 (?:read tools|reads)\b")
    assert not [p.name for p in documents() if stale.search(normalized(p.read_text()))]


def test_new_guides_and_this_oracle_ship_in_source_archives():
    from offline_contract import load_script

    checker = load_script("check_release_archives")
    assert set(NEW_SOURCE) <= set(checker.REQUIRED_SOURCE)
    for missing in NEW_SOURCE:
        with pytest.raises(ValueError):
            checker.check_inventory(sorted(set(checker.REQUIRED_SOURCE) - {missing}), source_archive=True)
    assert all((ROOT / path).is_file() for path in NEW_SOURCE)
