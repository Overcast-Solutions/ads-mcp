"""Pre-lock maintenance: protocol observation, independent of product startup.

Synthetic executables deliberately isolate pipe timing from MCP/server behavior.
The same tests are run against the original and amended harness.
"""
import json
import os
import subprocess
import sys
import textwrap
import time

import pytest

import harness as h


REQUEST = json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}) + "\n"
REPLY = {"jsonrpc": "2.0", "id": 2, "result": {"tools": [{"name": "synthetic"}]}}


def fixture_console(tmp_path, monkeypatch, body):
    script = tmp_path / "synthetic-console"
    script.write_text(
        "#!" + sys.executable + "\n"
        "import sys, os, json, time, select\n"
        "from pathlib import Path\n"
        + textwrap.dedent(body)
    )
    script.chmod(0o700)
    monkeypatch.setattr(h, "console_script", lambda name: script)
    processes = []
    real_popen = subprocess.Popen

    def observe(*args, **kwargs):
        proc = real_popen(*args, **kwargs)
        processes.append(proc)
        return proc

    monkeypatch.setattr(subprocess, "Popen", observe)
    return processes


def assert_reaped(processes):
    assert len(processes) == 1
    proc = processes[0]
    assert proc.returncode is not None, "observer did not reap its child"
    with pytest.raises(ProcessLookupError):
        os.kill(proc.pid, 0)
    assert all(pipe.closed for pipe in (proc.stdin, proc.stdout, proc.stderr))


def test_delayed_response_keeps_stdin_open(tmp_path, monkeypatch):
    processes = fixture_console(tmp_path, monkeypatch, f'''
        request = sys.stdin.readline()
        time.sleep(2.35)
        if select.select([sys.stdin], [], [], 0)[0]:
            if sys.stdin.read() == "":
                sys.exit(7)
        print({json.dumps(REPLY)!r}, flush=True)
        sys.stdin.read()
    ''')
    result = h._run_console_holding_pipe("synthetic", input_text=REQUEST, timeout=6)
    assert result.returncode == 0, "observer closed stdin before the delayed response"
    assert json.loads(result.stdout) == REPLY
    assert_reaped(processes)


def test_concurrently_drains_both_large_output_pipes(tmp_path, monkeypatch):
    processes = fixture_console(tmp_path, monkeypatch, f'''
        sys.stdin.readline()
        sys.stderr.write("diagnostic-" * 40000)
        sys.stderr.flush()
        print(json.dumps({{"jsonrpc": "2.0", "method": "notifications/message",
                          "params": {{"data": "x" * 400000}}}}), flush=True)
        print({json.dumps(REPLY)!r}, flush=True)
        sys.stdin.read()
    ''')
    result = h._run_console_holding_pipe("synthetic", input_text=REQUEST, timeout=5)
    assert result.returncode == 0, "pipe pressure prevented the requested response"
    assert json.loads(result.stdout.splitlines()[-1]) == REPLY
    assert result.stderr == "diagnostic-" * 40000
    assert_reaped(processes)


@pytest.mark.parametrize("wire", [
    "not json\n", "[]\n",
    json.dumps({"jsonrpc": "2.0", "id": 2}) + "\n",
    json.dumps({"jsonrpc": "2.0", "id": 2, "result": {}, "error": {}}) + "\n",
    json.dumps(REPLY) + "\nnot json after response\n",
])
def test_malformed_responses_are_observer_failures(tmp_path, monkeypatch, wire):
    processes = fixture_console(tmp_path, monkeypatch, f'''
        sys.stdin.readline()
        sys.stdout.write({wire!r})
        sys.stdout.flush()
        sys.stdin.read()
    ''')
    with pytest.raises(AssertionError, match="(?i)(protocol|json|malformed|response)"):
        h._run_console_holding_pipe("synthetic", input_text=REQUEST, timeout=4)
    assert_reaped(processes)


@pytest.mark.parametrize("wire", ["", json.dumps({"jsonrpc": "2.0", "id": 99, "result": {}}) + "\n"])
def test_exit_without_requested_response_is_not_success(tmp_path, monkeypatch, wire):
    processes = fixture_console(tmp_path, monkeypatch, f'''
        sys.stdin.readline()
        sys.stdout.write({wire!r})
        sys.stdout.flush()
    ''')
    with pytest.raises(AssertionError, match="(?i)(missing|response)"):
        h._run_console_holding_pipe("synthetic", input_text=REQUEST, timeout=4)
    assert_reaped(processes)


@pytest.mark.parametrize("reply_first", [False, True])
def test_timeout_kills_reaps_and_closes_pipes(tmp_path, monkeypatch, reply_first):
    processes = fixture_console(tmp_path, monkeypatch, f'''
        sys.stdin.readline()
        if {reply_first!r}:
            print({json.dumps(REPLY)!r}, flush=True)
        time.sleep(60)
    ''')
    start = time.monotonic()
    with pytest.raises(subprocess.TimeoutExpired):
        h._run_console_holding_pipe("synthetic", input_text=REQUEST, timeout=0.5)
    assert time.monotonic() - start < 3
    assert_reaped(processes)
