"""Requirements input diagnostics and actual MCP stdio version metadata."""
from copy import deepcopy
import importlib.metadata
import json

import pytest

import harness as h
from pmax_oracle import assert_catalog
from capability_oracle import (
    DETAIL, cli, empty, events, failure, offline_env, requirement, success,
    tool_requirement,
)
from tool_catalog import ALL_WRITE_MODE_TOOLS, READ_TOOLS


def invalid_requirements():
    cases = [({}, "empty-object"), ([], "array"), (None, "null"), (True, "boolean"), (17, "number"), (DETAIL, "string")]
    for version in (0, -1, 2, True, 1.0, "1", None):
        value = requirement()
        value["version"] = version
        cases.append((value, "version-" + repr(version)))
    for level in ("root", "capability", "tool"):
        value = requirement()
        parent = value if level == "root" else value["capabilities"][0] if level == "capability" else tool_requirement(value)
        parent["unexpected"] = DETAIL
        cases.append((value, level + "-unknown-key"))
        for key in list(parent):
            if key == "unexpected":
                continue
            value = requirement()
            parent = value if level == "root" else value["capabilities"][0] if level == "capability" else tool_requirement(value)
            del parent[key]
            cases.append((value, level + "-missing-" + key))
    for key in ("capabilities", "forbidden_parameters"):
        for wrong in ([], {}, None, DETAIL, True):
            value = requirement()
            value[key] = wrong
            cases.append((value, key + "-shape-" + repr(wrong)))
    for wrong in (None, {}, DETAIL, 7):
        value = requirement()
        value["capabilities"] = [wrong]
        cases.append((value, "capability-item-" + repr(wrong)))
    for field, wrongs in {
        "id": ["", " ", "bad-id", "2bad", "é", [], None],
        "purpose": ["", " \t", [], None, 3],
        "tools": [[], {}, None, DETAIL, [None], [{}]],
    }.items():
        for wrong in wrongs:
            value = requirement()
            value["capabilities"][0][field] = wrong
            cases.append((value, "capability-" + field + "-" + repr(wrong)))
    for field, wrongs in {
        "name": ["", " ", "bad.name", "2bad", "é", 3, None],
        "parameters": [None, {}, DETAIL, [None], [3]],
        "required": [None, {}, DETAIL, [3]],
        "values": [None, [], DETAIL, {"x": []}, {"x": "A"}, {"x": [None]}],
    }.items():
        for wrong in wrongs:
            value = requirement()
            tool_requirement(value)[field] = wrong
            cases.append((value, "tool-" + field + "-" + repr(wrong)))
    for bad_path in ("", " ", ".x", "x.", "x..y", "[].x", "x.[]", "x[].y", "x.[0].y", "x.*.y", "x/y", "x-y", "2x", "é", "x. y"):
        value = requirement(parameters=[bad_path])
        cases.append((value, "invalid-path-" + repr(bad_path)))
    for forbidden in (["x", "x"], [""], ["x.y"], ["x[]"], ["é"], [None], [7]):
        cases.append((requirement(forbidden=forbidden), "invalid-forbidden-" + repr(forbidden)))
    value = requirement(parameters=["x", "x"])
    cases.append((value, "duplicate-parameter"))
    cases.append((requirement(parameters=["x"], required=["x", "x"]), "duplicate-required"))
    cases.append((requirement(required=["undeclared"]), "undeclared-required"))
    cases.append((requirement(values={"undeclared": [1]}), "undeclared-value-path"))
    value = requirement()
    value["capabilities"].append(deepcopy(value["capabilities"][0]))
    value["capabilities"][1]["tools"][0]["name"] = "distinct_tool"
    cases.append((value, "duplicate-capability-id"))
    value = requirement()
    value["capabilities"][0]["tools"].append(deepcopy(tool_requirement(value)))
    cases.append((value, "duplicate-tool-within-capability"))
    value = requirement()
    value["capabilities"].append(deepcopy(value["capabilities"][0]))
    value["capabilities"][1]["id"] = "distinct_workflow"
    cases.append((value, "duplicate-tool-across-capabilities"))
    for domain in ([], [1, 1.0], [0, -0.0], [True, True], [None, None], ["x", "x"], [{}], [[]], [float("nan")], [float("inf")], [-float("inf")]):
        cases.append((requirement(parameters=["x"], values={"x": domain}), "invalid-domain-" + repr(domain)))
    return cases


@pytest.mark.parametrize("value,label", invalid_requirements(), ids=lambda item: item if isinstance(item, str) else None)
def test_invalid_requirements_fail_cleanly_before_initialization(tmp_path, value, label):
    result, observed = cli(tmp_path, value=value, actual=[])
    failure(result, observed, private_path=tmp_path)


@pytest.mark.parametrize("special", ["missing", "directory", "unreadable"])
def test_unreadable_requirements_fail_cleanly_before_initialization(tmp_path, special):
    result, observed = cli(tmp_path, value=requirement(), special=special, actual=[])
    failure(result, observed, private_path=tmp_path)
    if special == "unreadable":
        assert "input permission failure" in observed


@pytest.mark.parametrize("raw", ["{" + DETAIL, b"\xff\xfe", "[", "", "{} {}", "{" + DETAIL * 1000],
                         ids=["malformed", "invalid-encoding", "unclosed", "empty", "trailing-document", "long-input"])
def test_invalid_json_is_a_bounded_private_input_error(tmp_path, raw):
    result, observed = cli(tmp_path, raw=raw, actual=[])
    failure(result, observed, private_path=tmp_path)


@pytest.mark.parametrize("key,replacement", [
    ('"version": 1', '"version": 1, "version": 1'),
    ('"id": "workflow"', '"id": "workflow", "id": "workflow"'),
    ('"name": "example"', '"name": "example", "name": "example"'),
    ('"values": {}', '"values": {"x": [1], "x": [2]}'),
])
def test_duplicate_json_keys_at_every_contract_level_are_rejected(tmp_path, key, replacement):
    raw = json.dumps(requirement(parameters=["x"]))
    assert key in raw
    result, observed = cli(tmp_path, raw=raw.replace(key, replacement, 1), actual=[])
    failure(result, observed, private_path=tmp_path)


@pytest.mark.parametrize("value", [
    requirement(name="get_account_info"),
    requirement(name="run_gaql", parameters=["query"], required=["query"]),
    requirement(name="get_account_info", forbidden=["custom_guardrail_override"]),
])
def test_nonempty_custom_subsets_allow_empty_tool_requirements(tmp_path, value):
    result, _ = cli(tmp_path, value=value)
    success(result, empty())


@pytest.mark.parametrize("field,difference", [
    ("tool", "future_synthetic_tool"),
    ("parameter", "get_account_info.future_argument"),
    ("value", 'get_account_info.customer_id="future_account"'),
])
def test_custom_requirements_against_real_metadata_report_truthful_differences(tmp_path, field, difference):
    value = requirement(name="get_account_info")
    expected = empty()
    tool = tool_requirement(value)
    if field == "tool":
        tool["name"] = difference
        expected["missing_tools"] = [difference]
    elif field == "parameter":
        tool["parameters"] = ["future_argument"]
        expected["missing_parameters"] = [difference]
    else:
        tool["parameters"] = ["customer_id"]
        tool["values"] = {"customer_id": ["future_account"]}
        expected["missing_values"] = [difference]
    result, _ = cli(tmp_path, value=value)
    success(result, expected)


def test_help_is_available_without_initialization(tmp_path):
    result, observed = cli(tmp_path, help=True)
    assert result.returncode == 0 and "--requirements" in result.stdout and not result.stderr
    assert not any(event.startswith("initialized:") or event == "metadata collection" for event in observed)


@pytest.mark.parametrize("arguments", [["--requirements"], ["--unknown", DETAIL], [DETAIL], ["--requirements", "--unknown=" + DETAIL]])
def test_cli_argument_admission_is_named_private_and_initialization_free(tmp_path, arguments):
    result, observed = cli(tmp_path, arguments=arguments, actual=[])
    failure(result, observed, private_path=tmp_path)


@pytest.mark.parametrize("write_enabled", [False, True])
@pytest.mark.parametrize("no_metadata", [False, True])
def test_installed_stdio_version_matches_distribution_cli_and_tool_inventory(tmp_path, write_enabled, no_metadata):
    env, marker = offline_env(tmp_path, no_metadata=no_metadata)
    env.update(h.google_ads_env(tmp_path))
    if write_enabled:
        env.update(h.rw_env(tmp_path))
    expected = "unknown" if no_metadata else importlib.metadata.version("ads-mcp")
    cli = h.run_console("ads-mcp", ["--version"], env_overlay=env)
    assert cli.returncode == 0 and cli.stdout.strip() == f"ads-mcp {expected}" and not cli.stderr
    result, messages = h.stdio_tools_list(env)
    observed = events(marker)
    assert result.returncode == 0 and "Traceback" not in result.stderr, result.stderr
    responses = {message.get("id"): message for message in messages if "id" in message}
    assert 1 in responses and 2 in responses, messages
    assert "error" not in responses[1] and "error" not in responses[2], messages
    info = responses[1]["result"]["serverInfo"]
    tools = responses[2]["result"]["tools"]
    actual_names = {tool["name"] for tool in tools}
    assert_catalog(actual_names, read_only=not write_enabled)
    assert len(tools) == len(actual_names)
    assert info["name"] == "ads-mcp" and info.get("version") == expected, info
    if no_metadata:
        assert "metadata unavailable" in observed
