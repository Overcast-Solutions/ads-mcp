"""F029: installed setup artifact failures are concise and unsuccessful."""
import http.server
import io
import json
from pathlib import Path
import re
import runpy
import subprocess
import sys
from types import SimpleNamespace
import urllib.request
import urllib.parse
import webbrowser

import pytest

import harness as h


@pytest.mark.parametrize("kind", ["missing", "malformed", "directory", "array", "missing_keys"])
def test_installed_oauth_helper_configuration_errors(tmp_path, kind):
    credentials = tmp_path / "client.json"
    token = tmp_path / "token.json"
    if kind == "malformed":
        credentials.write_text('{"installed": invalid json}')
    elif kind == "directory":
        credentials.mkdir()
    elif kind == "array":
        credentials.write_text("[]")
    elif kind == "missing_keys":
        credentials.write_text('{"installed": {"client_id": "example.apps.googleusercontent.com"}}')
    proc = h.run_console("ads-mcp-generate-token", ["--credentials", str(credentials), "--token-path", str(token)])
    assert proc.returncode != 0
    assert not token.exists()
    assert "Traceback" not in proc.stderr + proc.stdout
    assert 1 <= len(proc.stderr.strip().splitlines()) <= 3
    assert re.search(r"\b(?:[A-Z][A-Z0-9_]*CONFIG[A-Z0-9_]*|configuration error)\b", proc.stderr), proc.stderr
    assert not proc.stdout.strip()


def run_installed_helper(monkeypatch, arguments):
    monkeypatch.setattr(sys, "argv", ["ads-mcp-generate-token", *arguments])
    try:
        runpy.run_path(str(h.console_script("ads-mcp-generate-token")), run_name="__main__")
    except SystemExit as exc:
        return exc.code
    except Exception as exc:
        pytest.fail(f"installed OAuth helper leaked {type(exc).__name__}: {exc}")
    return 0


@pytest.mark.parametrize("kind", ["missing", "malformed", "unreadable"])
def test_bad_credentials_never_enter_browser_or_network(tmp_path, monkeypatch, capsys, kind):
    credentials = tmp_path / "client.json"
    token = tmp_path / "token.json"
    if kind != "missing":
        credentials.write_text("{bad json")
    if kind == "unreadable":
        credentials.write_text(json.dumps({"installed": {
            "client_id": "example.apps.googleusercontent.com", "client_secret": "synthetic-secret"}}))
        original = Path.read_text
        original_open = Path.open
        def read_text(path, *args, **kwargs):
            if path == credentials:
                raise PermissionError("synthetic unreadable credential file")
            return original(path, *args, **kwargs)
        monkeypatch.setattr(Path, "read_text", read_text)
        def open_path(path, *args, **kwargs):
            if path == credentials:
                raise PermissionError("synthetic unreadable credential file")
            return original_open(path, *args, **kwargs)
        monkeypatch.setattr(Path, "open", open_path)
    calls = []
    def forbidden(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("invalid configuration reached an external seam")
    monkeypatch.setattr(webbrowser, "open", forbidden)
    monkeypatch.setattr(http.server, "HTTPServer", forbidden)
    monkeypatch.setattr(urllib.request, "urlopen", forbidden)
    status = run_installed_helper(monkeypatch, ["--credentials", str(credentials), "--token-path", str(token)])
    output = capsys.readouterr()
    assert status != 0 and not calls and not token.exists()
    assert "Traceback" not in output.err + output.out
    assert re.search(r"\b(?:[A-Z][A-Z0-9_]*CONFIG[A-Z0-9_]*|configuration error)\b", output.err)


def test_documented_valid_helper_flow_still_writes_token(tmp_path, monkeypatch, capsys):
    credentials, _ = h.write_oauth_files(tmp_path)
    output = tmp_path / "new-token.json"
    browser_calls, exchanges = [], []
    monkeypatch.setattr(webbrowser, "open", lambda url: browser_calls.append(url))
    class Loopback:
        def __init__(self, address, handler):
            assert address == ("127.0.0.1", 8085)
            self.handler = handler
        def handle_request(self):
            state = urllib.parse.parse_qs(urllib.parse.urlsplit(browser_calls[-1]).query).get("state", [""])[0]
            path = "/?" + urllib.parse.urlencode({"code": "synthetic-consent-code", "state": state})
            request = SimpleNamespace(path=path, wfile=io.BytesIO(),
                send_response=lambda *a: None, send_header=lambda *a: None, end_headers=lambda: None)
            self.handler.do_GET(request)
        def server_close(self):
            pass
    monkeypatch.setattr(http.server, "HTTPServer", Loopback)
    def exchange(request, *args, **kwargs):
        exchanges.append(request)
        return io.BytesIO(json.dumps({"refresh_token": "synthetic-output-token"}).encode())
    monkeypatch.setattr(urllib.request, "urlopen", exchange)
    assert run_installed_helper(monkeypatch, ["--credentials", str(credentials), "--token-path", str(output)]) == 0
    assert len(browser_calls) == 1 and "accounts.google.com" in browser_calls[0]
    assert len(exchanges) == 1 and exchanges[0].full_url == "https://oauth2.googleapis.com/token"
    assert json.loads(output.read_text()) == {"refresh_token": "synthetic-output-token"}
    assert output.stat().st_mode & 0o777 == 0o600
    captured = capsys.readouterr()
    assert "synthetic-output-token" not in captured.out + captured.err


def test_module_and_console_both_exit_nonzero_for_missing_configuration(tmp_path):
    console = h.run_console("ads-mcp", env_overlay={"GOOGLE_ADS_DEVELOPER_TOKEN": h.FAKE_DEVELOPER_TOKEN})
    module = subprocess.run([sys.executable, "-m", "ads_mcp"], cwd=tmp_path,
        env=h.scrubbed_env({"GOOGLE_ADS_DEVELOPER_TOKEN": h.FAKE_DEVELOPER_TOKEN}), input="", text=True, capture_output=True, timeout=30)
    for proc in (console, module):
        assert proc.returncode != 0, f"configuration failure incorrectly reported success: {proc.stderr}"
        assert "GOOGLE_ADS_CUSTOMER_ID" in proc.stderr
        assert "Traceback" not in proc.stdout + proc.stderr


@pytest.mark.parametrize("flag", ["--help", "--version"])
def test_module_help_and_version_succeed_without_configuration(tmp_path, flag):
    proc = subprocess.run([sys.executable, "-m", "ads_mcp", flag], cwd=tmp_path,
        env=h.scrubbed_env(), capture_output=True, text=True, timeout=30)
    assert proc.returncode == 0 and "ads-mcp" in proc.stdout
    assert "Traceback" not in proc.stderr + proc.stdout
