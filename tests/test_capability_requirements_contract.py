"""F065: independent workflow requirements and bounded structural inspection."""
from copy import deepcopy
import json
import math
import re
import tomllib

import pytest

from capability_oracle import (
    BUILTIN_FORBIDDEN, DEFAULT, DETAIL, ROOT, cli, compare, empty, failure,
    record, requirement, success, tool_requirement,
)
from offline_contract import load_script
from tool_catalog import ALL_WRITE_MODE_TOOLS, OPTIONAL_ARGS


def test_authored_default_covers_the_accepted_workflows_and_declared_obligations():
    contract = json.loads(DEFAULT.read_text())
    assert set(contract) == {"version", "capabilities", "forbidden_parameters"}
    assert type(contract["version"]) is int and contract["version"] == 1
    capabilities = contract["capabilities"]
    assert isinstance(capabilities, list) and capabilities
    assert len({item["id"] for item in capabilities}) == len(capabilities)
    tools = []
    for capability in capabilities:
        assert set(capability) == {"id", "purpose", "tools"}
        assert re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", capability["id"])
        assert isinstance(capability["purpose"], str) and len(capability["purpose"].strip()) >= 30
        assert capability["tools"]
        tools.extend(capability["tools"])
    by_name = {tool["name"]: tool for tool in tools}
    assert len(by_name) == len(tools) and set(by_name) == ALL_WRITE_MODE_TOOLS
    for tool in tools:
        assert set(tool) == {"name", "parameters", "required", "values"}
        assert len(tool["parameters"]) == len(set(tool["parameters"]))
        assert len(tool["required"]) == len(set(tool["required"]))
        assert set(tool["required"]) | set(tool["values"]) <= set(tool["parameters"])
    assert set(contract["forbidden_parameters"]) == set(BUILTIN_FORBIDDEN)
    for name, arguments in OPTIONAL_ARGS.items():
        assert set(arguments) <= set(by_name[name]["parameters"])
    assert {"query"} <= set(by_name["run_gaql"]["required"])
    assert {"plan_id"} <= set(by_name["confirm_and_apply"]["required"])
    assert {"dry_run", "confirm_irreversible"} <= set(by_name["confirm_and_apply"]["parameters"])
    for name in ("draft_campaign", "create_ad_group", "draft_responsive_search_ad"):
        assert {"ENABLED", "PAUSED", "REMOVED"} <= set(by_name[name]["values"]["status"])
    assert {"keywords", "keyword_texts"} <= set(by_name["get_keyword_forecasts"]["parameters"])
    assert not {"keywords", "keyword_texts"} & set(by_name["get_keyword_forecasts"]["required"])


def test_source_tree_and_archive_checker_use_the_owned_contract():
    retired = ["tests/fixtures/incumbent_catalog.json", "scripts/parity_catalog.py", "THIRD_PARTY_NOTICES.md"]
    assert DEFAULT.is_file()
    assert not [name for name in retired if (ROOT / name).exists()]
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    assert project["license"] == "MIT"
    assert "LICENSE" in project["license-files"]
    assert "THIRD_PARTY_NOTICES.md" not in project["license-files"]
    archive_check = (ROOT / "scripts" / "check_release_archives.py").read_text()
    assert "capability_requirements.json" in archive_check


@pytest.mark.parametrize("parameters,required,values,schema,expected", [
    ([], [], {}, {}, empty()),
    (["customer_id"], [], {}, {"properties": {"customer_id": {}}}, empty()),
    (["customer_id"], [], {}, {}, empty(missing_parameters=["example.customer_id"])),
    (["customer_id"], ["customer_id"], {"customer_id": ["x"]}, {}, empty(missing_parameters=["example.customer_id"])),
    (["x"], ["x"], {}, {"properties": {"x": {}}}, empty(missing_required=["example.x"])),
    (["x"], ["x"], {}, {"properties": {"x": {}}, "required": ["x"]}, empty()),
    (["x"], [], {"x": ["EXACT"]}, {"properties": {"x": {"type": "string"}}}, empty(missing_values=['example.x="EXACT"'])),
    (["x"], [], {"x": ["EXACT"]}, {"properties": {"x": {"enum": ["EXACT", "PHRASE"]}}}, empty()),
])
def test_scalar_presence_requiredness_and_values_are_distinct(parameters, required, values, schema, expected):
    assert compare(requirement(parameters=parameters, required=required, values=values), [record(schema)]) == expected


def test_missing_tool_suppresses_descendant_differences_and_extra_tools_are_allowed():
    requested = requirement(parameters=["x"], required=["x"], values={"x": ["v"]})
    assert compare(requested, []) == empty(missing_tools=["example"])
    assert compare(requirement(), [record(), record(name="additional_tool")]) == empty()


def test_requiredness_can_be_merged_across_allof_object_declarations():
    schema = {"allOf": [
        {"properties": {"parent": {"properties": {"leaf": {"type": "string"}}}}},
        {"required": ["parent"], "properties": {"parent": {"required": ["leaf"]}}},
    ]}
    assert compare(requirement(parameters=["parent.leaf"], required=["parent.leaf"]), [record(schema)]) == empty()


@pytest.mark.parametrize("combinator", ["anyOf", "oneOf"])
@pytest.mark.parametrize("second", ["required", "optional", "absent"])
def test_alternative_path_presence_and_requiredness_have_different_quantifiers(combinator, second):
    one = {"properties": {"parent": {"properties": {"leaf": {"enum": ["A"]}}, "required": ["leaf"]}}, "required": ["parent"]}
    two = deepcopy(one)
    if second == "optional":
        two["required"] = []
    elif second == "absent":
        two = {"type": "object"}
    expected = empty() if second == "required" else empty(missing_required=["example.parent.leaf"])
    value = requirement(parameters=["parent.leaf"], required=["parent.leaf"], values={"parent.leaf": ["A"]})
    assert compare(value, [record({combinator: [one, two]})]) == expected


@pytest.mark.parametrize("parent_required,leaf_required", [(True, True), (True, False), (False, True), (False, False)])
def test_array_item_requiredness_includes_named_ancestors_without_requiring_nonempty_arrays(parent_required, leaf_required):
    schema = {"properties": {"keywords": {"type": "array", "minItems": 0, "items": {
        "type": "object", "properties": {"match_type": {"enum": ["EXACT", "PHRASE"]}},
        "required": ["match_type"] if leaf_required else []}}}, "required": ["keywords"] if parent_required else []}
    value = requirement(parameters=["keywords.[].match_type"], required=["keywords.[].match_type"], values={"keywords.[].match_type": ["EXACT"]})
    expected = empty() if parent_required and leaf_required else empty(missing_required=["example.keywords.[].match_type"])
    assert compare(value, [record(schema)]) == expected


def test_array_items_can_have_scalar_and_object_alternatives():
    item = {"anyOf": [{"type": "string"}, {"properties": {"text": {"const": "value"}}, "required": ["text"]}]}
    actual = [record({"properties": {"entries": {"type": "array", "items": item}}, "required": ["entries"]})]
    requested = requirement(parameters=["entries.[].text"], required=["entries.[].text"], values={"entries.[].text": ["value"]})
    assert compare(requested, actual) == empty(missing_required=["example.entries.[].text"])


@pytest.mark.parametrize("path", ["keywords.match_type", "plain.[].match_type"])
def test_paths_do_not_skip_array_or_object_boundaries(path):
    schema = {"properties": {
        "keywords": {"type": "array", "items": {"properties": {"match_type": {"enum": ["EXACT"]}}}},
        "plain": {"properties": {"match_type": {"enum": ["EXACT"]}}},
    }}
    assert compare(requirement(parameters=[path], required=[path], values={path: ["EXACT"]}), [record(schema)]) == empty(missing_parameters=["example." + path])


def test_literal_property_text_and_descriptions_cannot_fake_a_nested_path():
    schema = {"properties": {"parent.leaf": {}, "parent": {"description": "leaf enum EXACT required"}}}
    assert compare(requirement(parameters=["parent.leaf"]), [record(schema)]) == empty(missing_parameters=["example.parent.leaf"])


def test_nested_arrays_follow_each_explicit_item_segment():
    schema = {"properties": {"groups": {"items": {"properties": {"children": {"items": {
        "properties": {"id": {"const": 7}}, "required": ["id"]}}}, "required": ["children"]}}}, "required": ["groups"]}
    path = "groups.[].children.[].id"
    assert compare(requirement(parameters=[path], required=[path], values={path: [7.0]}), [record(schema)]) == empty()


@pytest.mark.parametrize("schema,wanted,missing", [
    ({"enum": ["A", "B"]}, ["A", "C"], ['"C"']),
    ({"const": "A"}, ["A", "B"], ['"B"']),
    ({"type": "null"}, [None, "null"], ['"null"']),
    ({"anyOf": [{"const": "A"}, {"enum": ["B", "C"]}]}, ["A", "B", "D"], ['"D"']),
    ({"oneOf": [{"const": "A"}, {"type": "null"}]}, ["A", None], []),
    ({"anyOf": [{"const": "A"}, {"type": "string"}]}, ["A"], ['"A"']),
    ({"oneOf": [{"enum": [1]}, {}]}, [1], ['1']),
    ({"allOf": [{"enum": ["A", "B"]}, {"enum": ["B", "C"]}]}, ["A", "B", "C"], ['"A"', '"C"']),
    ({"allOf": [{"enum": ["A"]}, {"type": "string"}]}, ["A", "B"], ['"B"']),
    ({"enum": ["A", "B"], "anyOf": [{"enum": ["B", "C"]}, {"const": "D"}]}, ["A", "B", "C"], ['"A"', '"C"']),
    ({"enum": ["A"], "const": "B"}, ["A", "B"], ['"A"', '"B"']),
    ({"enum": [True, 2, None]}, [1, True, 2.0, None], ['1']),
    ({"enum": [1, 0]}, [True, False, 1.0, 0.0], ['true', 'false']),
    ({"enum": [0.0]}, [-0.0], []),
    ({"type": "string", "default": "A", "examples": ["A"], "description": "A"}, ["A"], ['"A"']),
    ({"allOf": [{"enum": ["A"]}, {"anyOf": [{"const": "A"}, {}]}]}, ["A"], []),
])
def test_finite_scalar_domains_follow_bounded_union_and_intersection(schema, wanted, missing):
    expected = empty(missing_values=sorted("example.x=" + value for value in missing))
    assert compare(requirement(parameters=["x"], values={"x": wanted}), [record({"properties": {"x": schema}})]) == expected


@pytest.mark.parametrize("unbounded", [False, True])
@pytest.mark.parametrize("combinator", ["anyOf", "oneOf"])
def test_domains_union_complete_leaf_paths_across_alternatives(combinator, unbounded):
    branches = [{"properties": {"x": {"enum": ["A"]}}}, {"properties": {"x": {} if unbounded else {"const": "B"}}}]
    expected = empty(missing_values=['example.x="A"', 'example.x="B"']) if unbounded else empty()
    assert compare(requirement(parameters=["x"], values={"x": ["A", "B"]}), [record({combinator: branches})]) == expected


def test_allof_merges_leaf_constraints_before_comparing_values():
    schema = {"allOf": [{"properties": {"x": {"enum": ["A", "B"]}}}, {"properties": {"x": {"enum": ["B", "C"]}}}]}
    value = requirement(parameters=["x"], values={"x": ["A", "B", "C"]})
    assert compare(value, [record(schema)]) == empty(missing_values=['example.x="A"', 'example.x="C"'])


def test_local_json_pointer_escapes_reference_siblings_and_reuse_are_resolved():
    schema = {"$defs": {"a/b~c": {"properties": {"leaf": {"enum": ["A", "B"]}}, "required": ["leaf"]}},
        "properties": {"parent": {"$ref": "#/$defs/a~1b~0c", "properties": {"leaf": {"const": "B"}}},
                       "other": {"$ref": "#/$defs/a~1b~0c"}}, "required": ["parent", "other"]}
    value = requirement(parameters=["parent.leaf", "other.leaf"], required=["parent.leaf", "other.leaf"], values={"parent.leaf": ["A", "B"], "other.leaf": ["A"]})
    assert compare(value, [record(schema)]) == empty(missing_values=['example.parent.leaf="A"'])


def test_reference_sibling_required_lists_combine_instead_of_overwriting():
    schema = {"$defs": {"body": {"properties": {"a": {}, "b": {}}, "required": ["a"]}},
        "$ref": "#/$defs/body", "required": ["b"]}
    assert compare(requirement(parameters=["a", "b"], required=["a", "b"]), [record(schema)]) == empty()


def test_referenced_object_can_be_traversed_inside_an_array():
    schema = {"$defs": {"item": {"properties": {"id": {"type": "null"}}, "required": ["id"]}},
        "properties": {"rows": {"type": "array", "items": {"$ref": "#/$defs/item"}}}, "required": ["rows"]}
    requested = requirement(parameters=["rows.[].id"], required=["rows.[].id"], values={"rows.[].id": [None]})
    assert compare(requested, [record(schema)]) == empty()


def test_descriptive_validation_metadata_cannot_create_properties_or_finite_domains():
    schema = {"title": "Synthetic object", "description": "This is not a property declaration.", "type": "object",
        "additionalProperties": {"type": "string"}, "minProperties": 0, "maxProperties": 20,
        "properties": {"x": {"type": "number", "minimum": 0, "exclusiveMaximum": 10, "multipleOf": 0.5,
            "default": 1, "examples": [1], "format": "float", "readOnly": True, "deprecated": False}}}
    expected = empty(missing_parameters=["example.invented"], missing_values=["example.x=1"])
    assert compare(requirement(parameters=["x", "invented"], values={"x": [1]}), [record(schema)]) == expected


@pytest.mark.parametrize("name", BUILTIN_FORBIDDEN + ["custom_override"])
@pytest.mark.parametrize("location", ["top", "object", "array", "alternative", "reference"])
def test_forbidden_names_are_global_and_recursive_in_unrequested_tools(name, location):
    bad = {"properties": {name: {"type": "boolean"}}}
    path = name
    if location == "object":
        bad, path = {"properties": {"options": bad}}, "options." + name
    elif location == "array":
        bad, path = {"properties": {"options": {"type": "array", "items": bad}}}, "options.[]." + name
    elif location == "alternative":
        bad = {"anyOf": [{}, bad]}
    elif location == "reference":
        bad = {"$defs": {"option": bad}, "$ref": "#/$defs/option"}
    value = requirement(forbidden=["custom_override"])
    assert compare(value, [record(), record(bad, "extra")]) == empty(forbidden_parameters=["extra." + path])


def test_repeated_forbidden_declarations_and_differences_are_unique_and_sorted():
    value = requirement(parameters=["z", "a"], required=["z", "a"], values={"a": ["Z", "A"]})
    schema = {"properties": {"a": {}}, "anyOf": [{"properties": {"confirmed_twice": {}}}, {"properties": {"confirmed_twice": {}}}]}
    value["capabilities"].append({"id": "additional", "purpose": "Check another missing operation.", "tools": [{"name": "z_tool", "parameters": [], "required": [], "values": {}}]})
    expected = empty(missing_tools=["z_tool"], missing_parameters=["example.z"], missing_required=["example.a"],
        missing_values=['example.a="A"', 'example.a="Z"'], forbidden_parameters=["example.confirmed_twice"])
    assert compare(value, [record(schema)]) == expected
    reversed_value = deepcopy(value)
    reversed_value["capabilities"].reverse()
    assert compare(reversed_value, [record(schema)]) == expected


BAD_METADATA = [
    None, [], True, 7, DETAIL,
    {"properties": None}, {"properties": []}, {"properties": {"x": None}}, {"properties": {"x": True}},
    {"required": None}, {"required": "x"}, {"required": [7]}, {"required": ["x", "x"]},
    {"items": []}, {"items": True}, {"prefixItems": []}, {"patternProperties": {}},
    {"if": {}}, {"then": {}}, {"else": {}}, {"dependentSchemas": {}}, {"dependentRequired": {}}, {"dependencies": {}}, {"not": {}},
    {"anyOf": None}, {"oneOf": DETAIL}, {"allOf": [None]}, {"anyOf": []}, {"oneOf": []}, {"allOf": []},
    {"enum": None}, {"enum": []}, {"enum": [math.nan]}, {"enum": [math.inf]}, {"enum": [{}]}, {"const": []},
    {"$ref": 7}, {"$ref": "#/missing"}, {"$ref": "https://example.invalid/" + DETAIL},
    {"$defs": {"loop": {"$ref": "#/$defs/loop"}}, "$ref": "#/$defs/loop"},
    {"$defs": {"a": {"$ref": "#/$defs/b"}, "b": {"$ref": "#/$defs/a"}}, "$ref": "#/$defs/a"},
    {"$defs": {"scalar": 1}, "$ref": "#/$defs/scalar"}, {"$defs": []},
    {"$ref": "#"}, {"$ref": "#/$defs/bad~2escape", "$defs": {"bad~2escape": {}}},
    {"properties": {"unused": {"not": {}}}},
]


@pytest.mark.parametrize("schema", BAD_METADATA)
def test_unusable_actual_metadata_is_a_named_cli_failure_even_for_an_unrequested_tool(tmp_path, schema):
    result, observed = cli(tmp_path, value=requirement(), actual=[record(), {"name": "unrequested", "inputSchema": schema}])
    failure(result, observed, code="METADATA_ERROR", before_init=False, private_path=tmp_path)
    assert "actual metadata injected" in observed


@pytest.mark.parametrize("actual", [
    {}, "bad", [None], [{}], [{"name": "example"}],
    [{"name": "", "inputSchema": {}}], [{"name": 4, "inputSchema": {}}],
    [record(), record()],
])
def test_invalid_metadata_inventory_is_contained(tmp_path, actual):
    result, observed = cli(tmp_path, value=requirement(), actual=actual)
    failure(result, observed, code="METADATA_ERROR", before_init=False, private_path=tmp_path)
    assert "actual metadata injected" in observed


@pytest.mark.parametrize("schema", BAD_METADATA)
def test_comparison_seam_reports_metadata_failure_without_returning_partial_success(schema):
    module = load_script("check_capabilities")
    with pytest.raises(module.MetadataError):
        module.compare_requirements(requirement(), [record(), {"name": "unrequested", "inputSchema": schema}])


def test_default_cli_uses_actual_offline_metadata_and_is_not_bound_to_working_directory(tmp_path):
    result, observed = cli(tmp_path, default=True)
    success(result, empty())
    assert "metadata collection" in observed
    assert any(event.startswith("initialized:") for event in observed)


@pytest.mark.parametrize("difference", ["tool", "parameter", "value", "required", "forbidden"])
def test_real_cli_is_sensitive_to_each_difference_category(tmp_path, difference):
    value = requirement(parameters=["x"], required=["x"], values={"x": ["A"]})
    schema = {"properties": {"x": {"enum": ["A"]}}, "required": ["x"]}
    actual, expected = [record(schema)], empty()
    if difference == "tool":
        actual, expected = [], empty(missing_tools=["example"])
    elif difference == "parameter":
        schema["properties"] = {}
        schema["required"] = []
        expected = empty(missing_parameters=["example.x"])
    elif difference == "value":
        schema["properties"]["x"] = {"enum": ["B"]}
        expected = empty(missing_values=['example.x="A"'])
    elif difference == "required":
        schema["required"] = []
        expected = empty(missing_required=["example.x"])
    else:
        actual.append(record({"properties": {"bypass_require_dry_run": {}}}, "extra"))
        expected = empty(forbidden_parameters=["extra.bypass_require_dry_run"])
    result, observed = cli(tmp_path, value=value, actual=actual)
    success(result, expected)
    assert "actual metadata injected" in observed


def test_real_cli_positive_control_and_compact_scalar_spelling(tmp_path):
    requested = requirement(parameters=["x"], values={"x": ['line\n"quoted"', True, None, 1]})
    actual = [record({"properties": {"x": {"enum": [True, None, 1.0]}}})]
    result, observed = cli(tmp_path, value=requested, actual=actual)
    success(result, empty(missing_values=['example.x="line\\n\\"quoted\\""']))
    assert "actual metadata injected" in observed
    tool_requirement(requested)["values"]["x"] = [True, None, 1]
    next_dir = tmp_path / "satisfied"
    next_dir.mkdir()
    result, _ = cli(next_dir, value=requested, actual=actual)
    success(result, empty())


def test_comparison_does_not_mutate_the_authored_contract_or_actual_metadata():
    requested = requirement(parameters=["x"], values={"x": ["B", "A"]})
    actual = [record({"properties": {"x": {"enum": ["A", "B"]}}})]
    originals = deepcopy((requested, actual))
    module = load_script("check_capabilities")
    assert module.compare_requirements(requested, actual) == empty()
    assert (requested, actual) == originals
