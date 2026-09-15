#!/usr/bin/env python3
"""Inspect declared MCP structure against independently authored workflows.

This offline check proves structural obligations only. Executable tests remain
responsible for input validation, safety and provider behavior.
"""
from __future__ import annotations

import argparse
import asyncio
import itertools
import json
import math
from pathlib import Path
import re
import sys

REPO = Path(__file__).resolve().parents[1]
DEFAULT_REQUIREMENTS = REPO / "tests" / "fixtures" / "capability_requirements.json"
IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
BUILTIN_FORBIDDEN = {"bypass_require_dry_run", "confirmed_twice"}
DIFFERENCES = ("missing_tools", "missing_parameters", "missing_values",
               "missing_required", "forbidden_parameters")


class RequirementsInputError(ValueError):
    """The supplied requirements cannot be inspected."""


class MetadataError(ValueError):
    """The actual tool metadata cannot be inspected safely."""


def _identifier(value):
    return isinstance(value, str) and IDENTIFIER.fullmatch(value) is not None


def _path(value):
    if not isinstance(value, str):
        return False
    parts = value.split(".")
    return (_identifier(parts[0]) and _identifier(parts[-1])
            and all(part == "[]" or _identifier(part) for part in parts))


def _scalar_key(value, error):
    # JSON numbers share equality, while booleans are a separate domain.
    if value is None or type(value) in (str, bool):
        return (type(value).__name__, value)
    if type(value) in (int, float) and (type(value) is int or math.isfinite(value)):
        return ("number", value)
    raise error()


def _unique_list(value, predicate, error, *, nonempty=False):
    if (not isinstance(value, list) or (nonempty and not value)
            or not all(predicate(item) for item in value)):
        raise error()
    if len(set(value)) != len(value):
        raise error()


def _shape(value, keys):
    if not isinstance(value, dict) or set(value) != set(keys):
        raise RequirementsInputError()


def _validate_requirements(value):
    _shape(value, ("version", "capabilities", "forbidden_parameters"))
    if type(value["version"]) is not int or value["version"] != 1:
        raise RequirementsInputError()
    _unique_list(value["forbidden_parameters"], _identifier,
                 RequirementsInputError, nonempty=True)
    capabilities = value["capabilities"]
    if not isinstance(capabilities, list) or not capabilities:
        raise RequirementsInputError()
    ids, names = set(), set()
    for capability in capabilities:
        _shape(capability, ("id", "purpose", "tools"))
        if not _identifier(capability["id"]) or capability["id"] in ids:
            raise RequirementsInputError()
        ids.add(capability["id"])
        if not isinstance(capability["purpose"], str) or not capability["purpose"].strip():
            raise RequirementsInputError()
        if not isinstance(capability["tools"], list) or not capability["tools"]:
            raise RequirementsInputError()
        for tool in capability["tools"]:
            _shape(tool, ("name", "parameters", "required", "values"))
            if not _identifier(tool["name"]) or tool["name"] in names:
                raise RequirementsInputError()
            names.add(tool["name"])
            for key in ("parameters", "required"):
                _unique_list(tool[key], _path, RequirementsInputError)
            values = tool["values"]
            if (not isinstance(values, dict)
                    or not set(tool["required"]).union(values) <= set(tool["parameters"])):
                raise RequirementsInputError()
            for domain in values.values():
                if not isinstance(domain, list) or not domain:
                    raise RequirementsInputError()
                keys = [_scalar_key(item, RequirementsInputError) for item in domain]
                if len(set(keys)) != len(keys):
                    raise RequirementsInputError()
    return value


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise RequirementsInputError()
        result[key] = value
    return result


def load_requirements(path):
    """Admit the complete contract before importing any server code."""
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"),
                           object_pairs_hook=_unique_object)
        return _validate_requirements(value)
    except (OSError, ValueError, TypeError, RecursionError, OverflowError):
        raise RequirementsInputError() from None


def collect_actual_tools():
    """Return actual MCP records offline, preserving duplicate tool names."""
    sys.path.insert(0, str(REPO))
    import mcp
    from ads_mcp.server import create_server
    from scripts.gen_tools_md import _config

    async def collect():
        server = create_server(_config(False), client=object(), clock=lambda: 0.0)
        async with mcp.Client(server) as client:
            listed = await client.list_tools()
            return [{"name": tool.name, "inputSchema": tool.input_schema}
                    for tool in listed.tools]

    return asyncio.run(collect())


class _Schema:
    """Inspect supported structure as conjunctions of alternative branches.

    Child declarations merge within a conjunction. Independent alternatives
    cannot invent a path by contributing separate pieces of its ancestry.
    """

    UNSUPPORTED = {"prefixItems", "patternProperties", "if", "then", "else",
                   "dependentSchemas", "dependentRequired", "dependencies", "not",
                   "contains", "additionalItems", "unevaluatedItems",
                   "unevaluatedProperties", "$dynamicRef", "$recursiveRef"}
    TYPES = {"object", "array", "string", "number", "integer", "boolean", "null"}
    MAX_BRANCHES = 4096

    def __init__(self, root):
        self.root = root
        self._validated = set()
        self._validate(root, set())

    def _reference(self, reference):
        if not isinstance(reference, str) or not (reference == "#" or reference.startswith("#/")):
            raise MetadataError()
        node = self.root
        for part in reference[2:].split("/") if reference != "#" else ():
            if re.search(r"~(?![01])", part):
                raise MetadataError()
            part = part.replace("~1", "/").replace("~0", "~")
            if isinstance(node, dict) and part in node:
                node = node[part]
            elif isinstance(node, list) and re.fullmatch(r"0|[1-9][0-9]*", part):
                try:
                    node = node[int(part)]
                except (IndexError, ValueError):
                    raise MetadataError() from None
            else:
                raise MetadataError()
        if not isinstance(node, dict):
            raise MetadataError()
        return node

    def _validate(self, node, active):
        if not isinstance(node, dict) or id(node) in active:
            raise MetadataError()
        if id(node) in self._validated:
            return
        if not all(isinstance(key, str) for key in node) or self.UNSUPPORTED.intersection(node):
            raise MetadataError()
        active = active | {id(node)}
        for key in ("properties", "$defs", "definitions"):
            if key in node:
                if not isinstance(node[key], dict):
                    raise MetadataError()
                for name, child in node[key].items():
                    if not isinstance(name, str):
                        raise MetadataError()
                    self._validate(child, active)
        if "required" in node:
            _unique_list(node["required"], lambda item: isinstance(item, str), MetadataError)
        if "type" in node:
            kinds = node["type"] if isinstance(node["type"], list) else [node["type"]]
            _unique_list(kinds, lambda item: isinstance(item, str) and item in self.TYPES,
                         MetadataError, nonempty=True)
        for key in ("allOf", "anyOf", "oneOf"):
            if key in node:
                if not isinstance(node[key], list) or not node[key]:
                    raise MetadataError()
                for child in node[key]:
                    self._validate(child, active)
        if "items" in node:
            self._validate(node["items"], active)
        if "additionalProperties" in node and type(node["additionalProperties"]) is not bool:
            self._validate(node["additionalProperties"], active)
        if "enum" in node:
            domain = node["enum"]
            if not isinstance(domain, list) or not domain:
                raise MetadataError()
            keys = [_scalar_key(item, MetadataError) for item in domain]
            if len(set(keys)) != len(keys):
                raise MetadataError()
        if "const" in node:
            _scalar_key(node["const"], MetadataError)
        if "$ref" in node:
            self._validate(self._reference(node["$ref"]), active)
        self._validated.add(id(node))

    def branches(self, nodes):
        result = [()]
        for node in nodes:
            choices = [(node,)]
            conjuncts = list(node.get("allOf", ()))
            if "$ref" in node:
                conjuncts.append(self._reference(node["$ref"]))
            if conjuncts:
                choices = self._product(choices, self.branches(conjuncts))
            for key in ("anyOf", "oneOf"):
                if key in node:
                    alternatives = []
                    for alternative in node[key]:
                        alternatives.extend(self.branches([alternative]))
                        if len(alternatives) > self.MAX_BRANCHES:
                            raise MetadataError()
                    choices = self._product(choices, alternatives)
            result = self._product(result, choices)
        return result

    def _product(self, left, right):
        if len(left) * len(right) > self.MAX_BRANCHES:
            raise MetadataError()
        return [a + b for a, b in itertools.product(left, right)]

    def path(self, nodes, parts):
        """Return declared leaf branches and universal ancestor requiredness."""
        leaves, required = [], True
        for branch in self.branches(nodes):
            part = parts[0]
            if part == "[]":
                children = [node["items"] for node in branch if "items" in node]
                locally_required = True  # every item if present, even an empty array
            else:
                children = [node["properties"][part] for node in branch
                            if part in node.get("properties", {})]
                locally_required = any(part in node.get("required", ()) for node in branch)
            if not children:
                required = False
                continue
            if len(parts) == 1:
                leaves.extend(self.branches(children))
                required = required and locally_required
            else:
                nested, nested_required = self.path(children, parts[1:])
                leaves.extend(nested)
                required = required and locally_required and nested_required
        return leaves, required

    @staticmethod
    def domain(leaves):
        result = set()
        for branch in leaves:
            domain = None
            for node in branch:
                constraints = []
                if "enum" in node:
                    constraints.append({_scalar_key(item, MetadataError) for item in node["enum"]})
                if "const" in node:
                    constraints.append({_scalar_key(node["const"], MetadataError)})
                if node.get("type") == "null" or node.get("type") == ["null"]:
                    constraints.append({_scalar_key(None, MetadataError)})
                for constraint in constraints:
                    domain = constraint if domain is None else domain & constraint
            if domain is None:
                return None  # an unbounded branch prevents finite evidence
            result.update(domain)
        return result

    def forbidden(self, names, nodes=None, prefix=()):
        result = set()
        for branch in self.branches([self.root] if nodes is None else nodes):
            properties = {}
            items = []
            map_values = []
            for node in branch:
                for name, child in node.get("properties", {}).items():
                    properties.setdefault(name, []).append(child)
                if "items" in node:
                    items.append(node["items"])
                if isinstance(node.get("additionalProperties"), dict):
                    map_values.append(node["additionalProperties"])
            for name, children in properties.items():
                path = prefix + (name,)
                if name in names:
                    result.add(".".join(path))
                result.update(self.forbidden(names, children, path))
            if items:
                result.update(self.forbidden(names, items, prefix + ("[]",)))
            if map_values:
                result.update(self.forbidden(names, map_values, prefix + ("*",)))
        return result


def compare_requirements(requirements, actual_tools):
    """Report truthful sorted differences without modifying either input."""
    _validate_requirements(requirements)
    try:
        if not isinstance(actual_tools, list):
            raise MetadataError()
        actual = {}
        for tool in actual_tools:
            if (not isinstance(tool, dict) or not _identifier(tool.get("name"))
                    or "inputSchema" not in tool or tool["name"] in actual):
                raise MetadataError()
            actual[tool["name"]] = _Schema(tool["inputSchema"])
        differences = {key: set() for key in DIFFERENCES}
        forbidden = BUILTIN_FORBIDDEN | set(requirements["forbidden_parameters"])
        for name, schema in actual.items():
            differences["forbidden_parameters"].update(
                name + "." + path for path in schema.forbidden(forbidden))
        for capability in requirements["capabilities"]:
            for tool in capability["tools"]:
                name = tool["name"]
                if name not in actual:
                    differences["missing_tools"].add(name)
                    continue
                schema = actual[name]
                for path in tool["parameters"]:
                    entry = name + "." + path
                    leaves, required = schema.path([schema.root], path.split("."))
                    if not leaves:
                        differences["missing_parameters"].add(entry)
                        continue
                    if path in tool["required"] and not required:
                        differences["missing_required"].add(entry)
                    domain = schema.domain(leaves)
                    for value in tool["values"].get(path, ()):
                        if domain is None or _scalar_key(value, MetadataError) not in domain:
                            differences["missing_values"].add(
                                entry + "=" + json.dumps(value, separators=(",", ":")))
        return {key: sorted(values) for key, values in differences.items()}
    except (RecursionError, OverflowError):
        raise MetadataError() from None


class _Parser(argparse.ArgumentParser):
    def error(self, message):
        # Default argparse errors echo supplied arguments and private paths.
        raise RequirementsInputError()


def main(argv=None):
    parser = _Parser(description=__doc__, allow_abbrev=False)
    parser.add_argument("--requirements", type=Path, default=DEFAULT_REQUIREMENTS)
    try:
        args = parser.parse_args(argv)
        requirements = load_requirements(args.requirements)
    except RequirementsInputError:
        print("REQUIREMENTS_INPUT_ERROR: Supply a readable UTF-8 version 1 workflow "
              "requirements file with valid unique fields; see --help.", file=sys.stderr)
        return 2
    try:
        result = compare_requirements(requirements, collect_actual_tools())
    except Exception:
        # Library errors must not disclose environment, paths or descriptions,
        # nor produce a partial successful comparison.
        print("METADATA_ERROR: Unable to inspect the complete offline tool metadata. "
              "Check the installation and supported schema structures, then retry.", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2))
    return 1 if any(result.values()) else 0


if __name__ == "__main__":
    raise SystemExit(main())
