"""F066: forbidden declarations in reachable map value schemas."""
from copy import deepcopy

import pytest

from capability_oracle import (
    BUILTIN_FORBIDDEN, DETAIL, cli, empty, failure, record, requirement, success,
)


def object_schema(**properties):
    return {"type": "object", "properties": properties}


def map_schema(value):
    return {"type": "object", "additionalProperties": value}


def array_schema(item):
    return {"type": "array", "items": item}


def located_map(location, leaf):
    """Synthetic declarations, with explicit diagnostic expectations below."""
    if location == "root":
        return map_schema(leaf)
    if location == "property":
        return object_schema(mapping=map_schema(leaf))
    if location == "nested_maps":
        return object_schema(mapping=map_schema(map_schema(leaf)))
    if location == "named_nested_map":
        return object_schema(mapping=map_schema(object_schema(options=map_schema(leaf))))
    if location == "array_value":
        return object_schema(mapping=map_schema(array_schema(leaf)))
    if location == "map_in_array":
        return object_schema(rows=array_schema(object_schema(mapping=map_schema(leaf))))
    if location == "named_ancestor":
        return object_schema(wrapper=object_schema(mapping=map_schema(leaf)))
    if location == "scalar_or_object_value":
        return object_schema(mapping=map_schema({"anyOf": [{"type": "string"}, leaf]}))
    raise AssertionError(location)


MAP_LOCATIONS = [
    ("root", "*."),
    ("property", "mapping.*."),
    ("nested_maps", "mapping.*.*."),
    ("named_nested_map", "mapping.*.options.*."),
    ("array_value", "mapping.*.[]."),
    ("map_in_array", "rows.[].mapping.*."),
    ("named_ancestor", "wrapper.mapping.*."),
    ("scalar_or_object_value", "mapping.*."),
]


@pytest.mark.parametrize("location,prefix", MAP_LOCATIONS, ids=[item[0] for item in MAP_LOCATIONS])
@pytest.mark.parametrize("name", BUILTIN_FORBIDDEN + ["custom_override"])
@pytest.mark.parametrize("tool", ["example", "unexpected"])
def test_cli_finds_each_forbidden_map_location_in_requested_and_unrequested_tools(
    tmp_path, location, prefix, name, tool,
):
    schema = located_map(location, object_schema(**{name: {"type": "boolean"}}))
    actual = [record(schema, tool)]
    if tool != "example":
        actual.append(record())
    result, observed = cli(
        tmp_path, value=requirement(forbidden=["custom_override"]), actual=actual,
    )
    assert "actual metadata injected" in observed
    success(result, empty(forbidden_parameters=[tool + "." + prefix + name]))


@pytest.mark.parametrize("combinator", ["allOf", "anyOf", "oneOf"])
@pytest.mark.parametrize("placement", ["map_parent", "map_value"])
def test_cli_preserves_conjunctions_and_alternatives_around_maps(tmp_path, combinator, placement):
    branches = [object_schema(custom_override={}), object_schema(confirmed_twice={})]
    if placement == "map_parent":
        schema = {combinator: [object_schema(mapping=map_schema(branch)) for branch in branches]}
    else:
        schema = object_schema(mapping=map_schema({combinator: branches}))
    result, observed = cli(
        tmp_path, value=requirement(forbidden=["custom_override"]),
        actual=[record(), record(schema, "unexpected")],
    )
    assert "actual metadata injected" in observed
    success(result, empty(forbidden_parameters=[
        "unexpected.mapping.*.confirmed_twice",
        "unexpected.mapping.*.custom_override",
    ]))


@pytest.mark.parametrize("reverse", [False, True])
def test_cli_reports_all_effective_reference_and_sibling_locations_once_in_sorted_order(tmp_path, reverse):
    schema = {
        "$defs": {
            "a/b~c": object_schema(confirmed_twice={}),
            "map": map_schema({"$ref": "#/$defs/a~1b~0c"}),
            "unused": object_schema(unused_override={}),
        },
        "properties": {
            "z_map": {
                "$ref": "#/$defs/map",
                "additionalProperties": object_schema(custom_override={}),
            },
            "rows": array_schema({"$ref": "#/$defs/map"}),
            "plain": object_schema(confirmed_twice={}),
            "a_map": map_schema({
                "$ref": "#/$defs/a~1b~0c",
                "properties": {"bypass_require_dry_run": {}},
                "anyOf": [object_schema(confirmed_twice={}), object_schema(confirmed_twice={})],
            }),
        },
    }
    actual = [record(deepcopy(schema), "unexpected"), record(deepcopy(schema))]
    if reverse:
        actual.reverse()
        for tool in actual:
            properties = tool["inputSchema"]["properties"]
            tool["inputSchema"]["properties"] = dict(reversed(list(properties.items())))
    result, observed = cli(
        tmp_path, value=requirement(forbidden=["custom_override", "unused_override"]), actual=actual,
    )
    assert "actual metadata injected" in observed
    success(result, empty(forbidden_parameters=[
        "example.a_map.*.bypass_require_dry_run",
        "example.a_map.*.confirmed_twice",
        "example.plain.confirmed_twice",
        "example.rows.[].*.confirmed_twice",
        "example.z_map.*.confirmed_twice",
        "example.z_map.*.custom_override",
        "unexpected.a_map.*.bypass_require_dry_run",
        "unexpected.a_map.*.confirmed_twice",
        "unexpected.plain.confirmed_twice",
        "unexpected.rows.[].*.confirmed_twice",
        "unexpected.z_map.*.confirmed_twice",
        "unexpected.z_map.*.custom_override",
    ]))


@pytest.mark.parametrize("location,prefix", MAP_LOCATIONS, ids=[item[0] for item in MAP_LOCATIONS])
def test_cli_preserves_harmless_maps_at_each_supported_location(tmp_path, location, prefix):
    leaf = object_schema(safe={
        "type": "string", "enum": ["confirmed_twice", "custom_override"],
        "description": "bypass_require_dry_run is text, not a declared property.",
    })
    result, observed = cli(
        tmp_path, value=requirement(forbidden=["custom_override"]),
        actual=[record(located_map(location, leaf)), record(located_map(location, leaf), "unexpected")],
    )
    assert "actual metadata injected" in observed
    success(result, empty())


@pytest.mark.parametrize("value_schema", [True, False, {}, {"type": "string"}],
                         ids=["open", "closed", "empty_schema", "scalar_schema"])
def test_cli_preserves_boolean_and_unconstrained_map_values(tmp_path, value_schema):
    schema = map_schema(deepcopy(value_schema))
    schema["properties"] = {"mapping": map_schema(deepcopy(value_schema))}
    result, observed = cli(
        tmp_path, value=requirement(parameters=["mapping"]),
        actual=[record(schema), record(deepcopy(schema), "unexpected")],
    )
    assert "actual metadata injected" in observed
    success(result, empty())


@pytest.mark.parametrize("definitions_key", ["$defs", "definitions"])
def test_cli_does_not_report_unreferenced_definitions_as_reachable_parameters(tmp_path, definitions_key):
    schema = object_schema(mapping=map_schema({
        definitions_key: {"unused_nested": object_schema(confirmed_twice={})},
        "properties": {"safe": {"type": "string"}},
    }))
    schema[definitions_key] = {
        "unused": map_schema(object_schema(custom_override={}, bypass_require_dry_run={})),
    }
    result, observed = cli(
        tmp_path, value=requirement(forbidden=["custom_override"]),
        actual=[record(schema), record(deepcopy(schema), "unexpected")],
    )
    assert "actual metadata injected" in observed
    success(result, empty())


@pytest.mark.parametrize("declared,required", [(False, False), (True, False), (True, True)])
def test_cli_map_values_do_not_supply_named_path_presence_requiredness_or_domains(tmp_path, declared, required):
    mapping = map_schema({
        "properties": {"leaf": {"const": "MAP"}}, "required": ["leaf"],
    })
    if declared:
        mapping["properties"] = {"leaf": {"const": "NAMED"}}
    if required:
        mapping["required"] = ["leaf"]
    schema = {"properties": {"mapping": mapping}, "required": ["mapping"]}
    value = requirement(
        parameters=["mapping.leaf"], required=["mapping.leaf"],
        values={"mapping.leaf": ["MAP", "NAMED"]},
    )
    if not declared:
        expected = empty(missing_parameters=["example.mapping.leaf"])
    else:
        expected = empty(
            missing_required=[] if required else ["example.mapping.leaf"],
            missing_values=['example.mapping.leaf="MAP"'],
        )
    result, observed = cli(tmp_path, value=value, actual=[record(schema)])
    assert "actual metadata injected" in observed
    success(result, expected)


@pytest.mark.parametrize("path", ["*.leaf", "mapping.*.leaf", "mapping.*.*.leaf", "rows.[].*.leaf"])
@pytest.mark.parametrize("use", ["parameter", "required", "values"])
def test_cli_rejects_wildcard_requirements_before_metadata_initialization(tmp_path, path, use):
    value = requirement(
        parameters=[path], required=[path] if use == "required" else [],
        values={path: ["example"]} if use == "values" else {},
    )
    result, observed = cli(tmp_path, value=value, actual=[record(map_schema(object_schema(leaf={})))])
    failure(result, observed, private_path=tmp_path)


BAD_MAP_VALUES = [
    pytest.param(None, id="null"),
    pytest.param([], id="array"),
    pytest.param(7, id="number"),
    pytest.param(DETAIL, id="private_string"),
    pytest.param({"properties": []}, id="properties_shape"),
    pytest.param({"properties": {"leaf": True}}, id="boolean_property_schema"),
    pytest.param({"items": []}, id="tuple_items"),
    pytest.param({"items": False}, id="boolean_items"),
    pytest.param({"prefixItems": [{}]}, id="prefix_items"),
    pytest.param({"patternProperties": {DETAIL: {}}}, id="pattern_properties"),
    pytest.param({"if": {}, "then": {}}, id="conditional"),
    pytest.param({"dependentSchemas": {DETAIL: {}}}, id="dependent_schema"),
    pytest.param({"not": {}}, id="negation"),
    pytest.param({"allOf": []}, id="empty_conjunction"),
    pytest.param({"anyOf": [True]}, id="boolean_alternative"),
    pytest.param({"oneOf": DETAIL}, id="alternatives_shape"),
    pytest.param({"enum": [{}]}, id="nonscalar_enum"),
    pytest.param({"required": ["leaf", "leaf"]}, id="duplicate_required"),
    pytest.param({"$ref": "https://example.invalid/" + DETAIL}, id="external_reference"),
    pytest.param({"$ref": "#/$defs/" + DETAIL}, id="missing_reference"),
    pytest.param({"$ref": "#/properties/mapping"}, id="cyclic_reference"),
    pytest.param({"additionalProperties": {"properties": {"leaf": None}}}, id="nested_map"),
    pytest.param({"$defs": {"unused": {"not": {}}}}, id="unsupported_unused_definition"),
    pytest.param({"definitions": {"unused": False}}, id="malformed_unused_definition"),
]


@pytest.mark.parametrize("value_schema", BAD_MAP_VALUES)
@pytest.mark.parametrize("tool", ["example", "unexpected"])
def test_cli_contains_malformed_or_unsupported_map_metadata_without_partial_results(tmp_path, value_schema, tool):
    schema = object_schema(mapping=map_schema(deepcopy(value_schema)))
    schema["description"] = DETAIL
    actual = [record(object_schema(confirmed_twice={}), "known_difference"), record(schema, tool)]
    if tool != "example":
        actual.append(record())
    result, observed = cli(tmp_path, value=requirement(), actual=actual)
    assert "actual metadata injected" in observed
    failure(result, observed, code="METADATA_ERROR", before_init=False, private_path=tmp_path)
