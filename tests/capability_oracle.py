"""Synthetic requirements and real child-process observations for the checker."""
from copy import deepcopy
import json
from pathlib import Path
import re
import subprocess
import sys

import harness as h
from offline_contract import load_script

ROOT = Path(__file__).resolve().parents[1]
DEFAULT = ROOT / "tests" / "fixtures" / "capability_requirements.json"
DETAIL = "synthetic-requirements-detail-DO-NOT-DISCLOSE"
KEYS = ("missing_tools", "missing_parameters", "missing_values", "missing_required", "forbidden_parameters")
BUILTIN_FORBIDDEN = ["bypass_require_dry_run", "confirmed_twice"]


def empty(**differences):
    return {key: differences.get(key, []) for key in KEYS}


def requirement(name="example", parameters=(), required=(), values=None, *, forbidden=None):
    return {"version": 1, "capabilities": [{"id": "workflow", "purpose": "Inspect declared support for a synthetic workflow.",
        "tools": [{"name": name, "parameters": list(parameters), "required": list(required), "values": values or {}}]}],
        "forbidden_parameters": list(BUILTIN_FORBIDDEN if forbidden is None else forbidden)}


def record(schema=None, name="example"):
    return {"name": name, "inputSchema": {} if schema is None else schema}


def tool_requirement(value):
    return value["capabilities"][0]["tools"][0]


def compare(value, actual):
    module = load_script("check_capabilities")
    return module.compare_requirements(deepcopy(value), deepcopy(actual))


INJECTION = r'''
import builtins
import importlib.metadata
import io
import json
import os
import sys

original_open = builtins.open
def mark(event):
    with original_open(os.environ["ORACLE_MARKER"], "a") as stream:
        stream.write(event + "\n")
mark("loaded")
def deny_network(event, args):
    if event == "socket.bind" and args[1] == ("::1", 0):
        raise OSError("offline IPv6 capability probe")
    if event in ("socket.connect", "socket.bind", "socket.getaddrinfo"):
        mark("network attempted")
        raise OSError("offline oracle denies network")
sys.addaudithook(deny_network)

def injected_tools():
    mark("actual metadata injected")
    with original_open(os.environ["ORACLE_ACTUAL_TOOLS"]) as stream:
        return json.load(stream)

# Change only the metadata provider. Admission, traversal and output execute
# in the actual script; the injected provider must not run on invalid input.
def profile(frame, event, arg):
    if event != "call":
        return
    name = frame.f_code.co_name
    module = frame.f_globals.get("__name__")
    if os.path.basename(frame.f_code.co_filename) == "check_capabilities.py" and name == "main":
        if os.environ.get("ORACLE_ACTUAL_TOOLS"):
            frame.f_globals["collect_actual_tools"] = injected_tools
    if module in ("scripts.gen_tools_md", "ads_mcp.config", "ads_mcp.auth", "ads_mcp.server") and name in (
        "_schemas", "_config", "load_config", "load_oauth_material", "create_server"):
        mark("initialized:" + name)
    if name == "collect_actual_tools":
        mark("metadata collection")
sys.setprofile(profile)

if os.environ.get("ORACLE_UNREADABLE"):
    def wrap(opener):
        def opened(path, *args, **kwargs):
            if not isinstance(path, int) and os.fspath(path) == os.environ["ORACLE_UNREADABLE"]:
                mark("input permission failure")
                raise PermissionError(os.environ["ORACLE_DETAIL"])
            return opener(path, *args, **kwargs)
        return opened
    builtins.open, io.open = wrap(builtins.open), wrap(io.open)

if os.environ.get("ORACLE_NO_METADATA") == "1":
    original_version = importlib.metadata.version
    def version(name):
        if name.replace("_", "-").lower() == "ads-mcp":
            mark("metadata unavailable")
            raise importlib.metadata.PackageNotFoundError(name)
        return original_version(name)
    importlib.metadata.version = version
'''


def offline_env(tmp_path, *, unreadable=None, no_metadata=False):
    injection = tmp_path / "injection"
    injection.mkdir(exist_ok=True)
    (injection / "sitecustomize.py").write_text(INJECTION)
    marker = tmp_path / "process-events.txt"
    return {"PYTHONPATH": str(injection), "PYTHONDONTWRITEBYTECODE": "1",
        "ORACLE_MARKER": str(marker), "ORACLE_DETAIL": DETAIL,
        "ORACLE_UNREADABLE": str(unreadable) if unreadable else "",
        "ORACLE_NO_METADATA": "1" if no_metadata else "0"}, marker


def events(marker):
    observed = marker.read_text().splitlines()
    assert "loaded" in observed, "the actual child process must load offline guards"
    assert "network attempted" not in observed
    return observed


def cli(tmp_path, *, value=None, raw=None, special=None, default=False, help=False, actual=None, arguments=None):
    supplied = tmp_path / (DETAIL + ".json")
    if special == "directory":
        supplied.mkdir()
    elif special != "missing":
        if isinstance(raw, bytes):
            supplied.write_bytes(raw)
        else:
            supplied.write_text(raw if raw is not None else json.dumps(value))
    env, marker = offline_env(tmp_path, unreadable=supplied if special == "unreadable" else None)
    if actual is not None:
        metadata = tmp_path / "actual-tools.json"
        metadata.write_text(json.dumps(actual))
        env["ORACLE_ACTUAL_TOOLS"] = str(metadata)
    args = [sys.executable, str(ROOT / "scripts" / "check_capabilities.py")]
    if arguments is not None:
        args += arguments
    elif help:
        args.append("--help")
    elif not default:
        args += ["--requirements", str(supplied)]
    result = subprocess.run(args, cwd=tmp_path, env=h.scrubbed_env(env),
                            capture_output=True, text=True, timeout=30)
    return result, events(marker)


def failure(result, observed, *, code="REQUIREMENTS_INPUT_ERROR", before_init=True, private_path=None):
    lines = [line for line in result.stderr.splitlines() if line.strip()]
    output = result.stdout + result.stderr
    outcome = {"exit_2": result.returncode == 2,
        "named_one_line": len(lines) == 1 and len(lines[0]) <= 600 and bool(re.fullmatch(code + r": [^\r\n]+", lines[0])),
        "no_traceback": "Traceback" not in output,
        "no_input_disclosure": DETAIL not in output,
        "no_private_path": private_path is None or str(private_path) not in output,
        "no_success_output": not result.stdout.strip()}
    if before_init:
        outcome["no_initialization"] = not any(event.startswith("initialized:") or event in (
            "metadata collection", "actual metadata injected") for event in observed)
    assert all(outcome.values()), {"outcome": outcome, "stderr": result.stderr, "stdout": result.stdout, "events": observed}


def success(result, expected):
    assert result.returncode == (1 if any(expected.values()) else 0) and not result.stderr, result
    payload = json.loads(result.stdout)
    assert payload == expected
    assert set(payload) == set(KEYS)
    assert all(isinstance(items, list) and items == sorted(set(items)) for items in payload.values())
