"""F042: actual console/script entry paths and offline output-file failures.

The subprocess injection replaces consent and exchange, never the entry point
or token serialization. Permission/write injection is restricted to the named
output path and raises at its OSError boundary; no chmod simulation depends on
the user running pytest without elevated filesystem privileges.
"""
import json
from pathlib import Path
import re
import subprocess
import sys

import pytest

import harness as h


ROOT = Path(__file__).resolve().parents[1]
DETAIL = "synthetic-private-os-detail-DO-NOT-DISCLOSE"
TOKEN = "synthetic-output-refresh-token-DO-NOT-DISCLOSE"
SECRET = "synthetic-output-client-secret-DO-NOT-DISCLOSE"
INJECTION = r'''
import builtins
import http.server
import io
import json
import os
from pathlib import Path
import sys
import urllib.request
import urllib.parse
import webbrowser

marker = Path(os.environ["ORACLE_MARKER"])
def mark(text):
    with marker.open("a") as stream:
        stream.write(text + "\n")
mark("injection loaded")

def deny_network(event, args):
    # urllib3 probes IPv6 availability by binding ::1:0 during import. Refuse
    # that capability probe as an ordinary unavailable socket, without letting
    # it become a false provider-contact alarm or changing any product seam.
    if event == "socket.bind" and args[1] == ("::1", 0):
        mark("IPv6 capability probe blocked")
        raise OSError("offline capability probe")
    if event in ("socket.connect", "socket.bind", "socket.getaddrinfo"):
        mark("network attempted")
        raise AssertionError("offline oracle forbids network")
sys.addaudithook(deny_network)

if os.environ["ORACLE_MODE"] == "oauth":
    issued_state = ""
    def browser(url):
        global issued_state
        issued_state = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query).get("state", [""])[0]
        mark("synthetic browser")
        return True
    class Loopback:
        def __init__(self, address, handler):
            self.handler = handler
        def handle_request(self):
            mark("synthetic consent")
            handler = self.handler.__new__(self.handler)
            handler.path = "/?" + urllib.parse.urlencode({"code": "synthetic-authorization-code", "state": issued_state})
            handler.wfile = io.BytesIO()
            handler.send_response = lambda *a: None
            handler.send_header = lambda *a: None
            handler.end_headers = lambda: None
            handler.do_GET()
        def server_close(self):
            pass
    def exchange(request, *args, **kwargs):
        mark("synthetic exchange")
        return io.BytesIO(json.dumps({"refresh_token": os.environ["ORACLE_TOKEN"]}).encode())
    webbrowser.open = browser
    http.server.HTTPServer = Loopback
    urllib.request.urlopen = exchange

if os.environ.get("ORACLE_FORBID_EXECUTION") == "1":
    def profile(frame, event, arg):
        if event == "call" and frame.f_code.co_name in (
            "offline_report", "live_report", "load_contract_fixture",
            "load_config", "load_oauth_material", "create_server", "build_server",
        ):
            mark("report execution attempted")
            raise AssertionError("output preflight must precede report execution")
    sys.setprofile(profile)

failure = os.environ.get("ORACLE_FAILURE")
target = os.environ.get("ORACLE_OUTPUT")
def matches(path):
    try:
        return os.fspath(path) == target
    except TypeError:
        return False
class FailedWriter:
    def __init__(self, stream):
        self.stream = stream
    def write(self, value):
        mark("synthetic write failure")
        raise OSError(os.environ["ORACLE_DETAIL"])
    def __enter__(self):
        return self
    def __exit__(self, *args):
        self.stream.close()
    def __getattr__(self, name):
        return getattr(self.stream, name)
def wrap(opener):
    def opened(file, *args, **kwargs):
        if matches(file) and failure == "permission":
            mark("synthetic permission failure")
            raise PermissionError(os.environ["ORACLE_DETAIL"])
        stream = opener(file, *args, **kwargs)
        return FailedWriter(stream) if matches(file) and failure == "write" else stream
    return opened
if failure:
    builtins.open = wrap(builtins.open)
    io.open = wrap(io.open)
'''


def launch(tmp_path, mode, output, *, failure=None, forbid=False, extra=(), malformed=False):
    injection = tmp_path / "injection"
    injection.mkdir(exist_ok=True)
    (injection / "sitecustomize.py").write_text(INJECTION)
    marker = tmp_path / "steps.txt"
    marker.unlink(missing_ok=True)
    env = h.scrubbed_env({"PYTHONPATH": str(injection), "PYTHONDONTWRITEBYTECODE": "1",
        "ORACLE_MODE": mode, "ORACLE_MARKER": str(marker), "ORACLE_OUTPUT": str(output),
        "ORACLE_TOKEN": TOKEN, "ORACLE_DETAIL": DETAIL,
        "ORACLE_FAILURE": failure or "", "ORACLE_FORBID_EXECUTION": "1" if forbid else "0"})
    if mode == "oauth":
        credentials = tmp_path / "synthetic-client.json"
        credentials.write_text(json.dumps([] if malformed else {"installed": {
            "client_id": "synthetic-client.apps.example.org", "client_secret": SECRET}}))
        cmd = [str(h.console_script("ads-mcp-generate-token")), "--credentials", str(credentials), "--token-path", str(output)]
    else:
        cmd = [sys.executable, str(ROOT / "scripts" / "parity.py"), "--report", str(output)]
    result = subprocess.run([*cmd, *extra], cwd=tmp_path, env=env, text=True, capture_output=True, timeout=40)
    steps = marker.read_text()
    assert "injection loaded" in steps, "the installed console process must load deterministic offline injection"
    assert "network attempted" not in steps
    return result, steps


def clean_failure(result):
    assert result.returncode != 0
    assert "Traceback" not in result.stdout + result.stderr
    lines = [line for line in result.stderr.splitlines() if line.strip()]
    assert len(lines) == 1, result.stderr
    assert re.match(r"(?:[a-zA-Z0-9_.-]+: )?[A-Z][A-Z0-9_]+:", lines[0]), result.stderr
    assert len(lines[0]) <= 600
    assert any(word in lines[0].lower() for word in ("choose", "check", "writable", "permission", "directory", "parent", "path"))
    for planted in (TOKEN, SECRET, DETAIL):
        assert planted not in result.stdout + result.stderr


@pytest.mark.parametrize("case", ["directory", "permission"])
def test_token_output_oserror_is_clean_at_installed_console_boundary(tmp_path, case):
    output = tmp_path / "token-output"
    if case == "directory":
        output.mkdir()
    result, steps = launch(tmp_path, "oauth", output, failure="permission" if case == "permission" else None)
    clean_failure(result)
    assert "synthetic exchange" in steps, "exercise the actual post-exchange token write"
    if case == "permission":
        assert "synthetic permission failure" in steps and not output.exists()
    else:
        assert output.is_dir() and list(output.iterdir()) == []


def test_successful_installed_token_helper_writes_only_refresh_with_private_mode(tmp_path):
    output = tmp_path / "token.json"
    result, steps = launch(tmp_path, "oauth", output)
    assert result.returncode == 0, result.stderr
    assert json.loads(output.read_text()) == {"refresh_token": TOKEN}
    assert output.stat().st_mode & 0o777 == 0o600
    assert "synthetic browser" in steps and "synthetic consent" in steps and "synthetic exchange" in steps
    assert TOKEN not in result.stdout + result.stderr and SECRET not in result.stdout + result.stderr


def test_malformed_credentials_still_stop_before_consent_or_output(tmp_path):
    output = tmp_path / "token.json"
    result, steps = launch(tmp_path, "oauth", output, malformed=True)
    clean_failure(result)
    assert "AUTH_CONFIG_" in result.stderr
    assert "synthetic browser" not in steps and "synthetic exchange" not in steps
    assert not output.exists()


@pytest.mark.parametrize("case", ["missing-parent", "directory"])
@pytest.mark.parametrize("live", [False, True])
def test_parity_output_open_failure_precedes_fixture_or_live_execution(tmp_path, case, live):
    output = tmp_path / "missing" / "report.txt" if case == "missing-parent" else tmp_path / "directory"
    if case == "directory":
        output.mkdir()
    result, steps = launch(tmp_path, "parity", output, forbid=True, extra=("--live",) if live else ())
    clean_failure(result)
    assert "report execution attempted" not in steps
    assert not result.stdout


def test_parity_report_write_oserror_is_clean_after_successful_open(tmp_path):
    output = tmp_path / "report.txt"
    result, steps = launch(tmp_path, "parity", output, failure="write")
    clean_failure(result)
    assert "synthetic write failure" in steps
    assert output.exists() and output.read_text() == ""


@pytest.mark.parametrize("destination", ["stdout", "file"])
def test_complete_default_fixture_report_still_runs_to_completion(tmp_path, destination):
    output = "-" if destination == "stdout" else tmp_path / "report.txt"
    result, _ = launch(tmp_path, "parity", output)
    text = result.stdout if destination == "stdout" else output.read_text()
    # Content correctness is F016/F018's oracle. This output-boundary feature
    # independently requires complete fixture execution and honest exit status,
    # without importing F018's intentionally red forecast golden dependency.
    from tool_catalog import READ_TOOLS
    rows = re.findall(r"^(\w+)\s+(MATCH|DRIFT)$", text, re.M)
    assert {name for name, _ in rows} == set(READ_TOOLS) and len(rows) == len(READ_TOOLS)
    assert f"across {len(READ_TOOLS)} read-tool fixtures" in text
    assert result.returncode == (1 if any(status == "DRIFT" for _, status in rows) else 0)
    assert not result.stderr and "Traceback" not in text
