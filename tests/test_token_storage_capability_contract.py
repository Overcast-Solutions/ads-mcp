"""Installed helper recovery when a required secure-storage API is absent.

This is explicit missing-capability injection on the current host, not a native
Windows or ACL test. The inspected raw-HTTP/consent/exchange adapter and real
file-write observer are reused; no product function or write is replaced.
"""
import base64
import hashlib
import json
import re
import socket
import stat
import urllib.parse

import pytest

import test_local_auth_io_contract as local


STORAGE_OBSERVER = r'''
# Track opened output descriptors without requiring any particular write or
# replacement algorithm. A descriptor still naming that file at process exit
# is a leak; unrelated later reuse of the same descriptor number is not.
tracked_outputs = {}
storage_open = os.open
def track_output_open(path, flags, *args, **kwargs):
    fd = storage_open(path, flags, *args, **kwargs)
    if (isinstance(path, (str, bytes, os.PathLike))
            and Path(os.fsdecode(path)).absolute().is_relative_to(output.parent)
            and flags & (os.O_WRONLY | os.O_RDWR)):
        info = os.fstat(fd)
        tracked_outputs[fd] = (info.st_dev, info.st_ino)
    return fd
os.open = track_output_open

def descriptor_cleanup():
    leaks = []
    for fd, identity in tracked_outputs.items():
        try:
            info = os.fstat(fd)
        except OSError:
            continue
        if (info.st_dev, info.st_ino) == identity:
            leaks.append(fd)
    mark("descriptor cleanup", leaked=len(leaks))
atexit.register(descriptor_cleanup)

def extra_network_guard(event, args):
    if event in ("socket.gethostbyname", "socket.gethostbyaddr"):
        mark("network attempted", operation=event)
        raise OSError("offline storage contract forbids name resolution")
sys.addaudithook(extra_network_guard)
'''


@pytest.fixture(autouse=True)
def offline_parent(monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError("storage capability parent forbids network")
    for name in ("connect", "connect_ex", "bind", "sendto"):
        monkeypatch.setattr(socket.socket, name, deny)
    for name in ("getaddrinfo", "gethostbyname", "gethostbyname_ex", "gethostbyaddr"):
        monkeypatch.setattr(socket, name, deny)


def launch(tmp_path, monkeypatch, *, destination, missing=None, private=False):
    injection = local.INJECTION + STORAGE_OBSERVER
    if private:
        # Test preparation, before removing any API or invoking the console.
        injection += '\noutput.chmod(0o600)\n'
    if missing:
        injection += (
            f'\nassert hasattr(os, {missing!r}), "fault injection requires the native API"\n'
            f'delattr(os, {missing!r})\n'
            f'mark("capability removed", name={missing!r}, absent=not hasattr(os, {missing!r}))\n'
        )
    monkeypatch.setattr(local, "INJECTION", injection)
    # This driver invokes the actual adjacent installed console, has explicit
    # neutral HOME/TMPDIR/env, and kills/drains children on its bounded timeout.
    attempt = local.launch(tmp_path, destination=destination)
    assert attempt.prompt, "installed helper exceeded deadline; child killed and drained"
    assert attempt.events("finished"), "installed helper did not finish its entry path"
    assert attempt.events("descriptor cleanup") == [{"event": "descriptor cleanup", "leaked": 0}]
    assert len(attempt.events("closed")) == len(attempt.events("listener"))
    assert "Error in sitecustomize" not in attempt.result.stderr, "fault adapter failed to import"
    if missing:
        assert attempt.events("capability removed") == [
            {"event": "capability removed", "name": missing, "absent": True}]
    return attempt


def public_output(attempt):
    # The issued public consent URL intentionally carries state. It is the
    # only exception; state must not be reflected in HTTP/diagnostic text.
    stdout = attempt.result.stdout
    for row in attempt.events("browser"):
        stdout = stdout.replace(row["url"], "<issued consent URL>")
    return stdout + attempt.result.stderr + "".join(row["raw"] for row in attempt.events("response"))


def confidentiality_problems(attempt):
    public = public_output(attempt)
    values = [local.TOKEN, local.SECRET, local.PRIVATE, local.CODE]
    values.extend(row["state"] for row in attempt.events("accepted"))
    for row in attempt.events("exchange"):
        values.extend(urllib.parse.parse_qs(row["body"]).get("code_verifier", []))
    problems = []
    if any(value in public or urllib.parse.quote(value, safe="") in public for value in values):
        problems.append("private callback, credential, or token material escaped")
    if "Traceback" in public or "AttributeError:" in public:
        problems.append("uncaught exception details escaped")
    return problems


def valid_exchange(attempt):
    assert len(attempt.events("listener")) == len(attempt.events("accepted")) == 1
    assert len(attempt.events("exchange")) == len(attempt.events("closed")) == 1
    response = attempt.events("response")
    assert len(response) == 1 and response[0]["raw"].split("\r\n", 1)[0].split()[1] == "200"
    consent = urllib.parse.parse_qs(urllib.parse.urlsplit(attempt.events("browser")[0]["url"]).query)
    exchange = urllib.parse.parse_qs(attempt.events("exchange")[0]["body"])
    verifier = exchange["code_verifier"][0]
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).decode().rstrip("=")
    assert consent["code_challenge"] == [challenge]
    assert consent["code_challenge_method"] == ["S256"]
    assert attempt.events("accepted")[0]["state"] == consent["state"][0]
    assert exchange["code"] == [local.CODE] and exchange["client_secret"] == [local.SECRET]
    assert 0 < attempt.events("exchange")[0]["timeout"] <= 30


def private_success(attempt):
    assert attempt.result.returncode == 0, attempt.result.stderr
    assert json.loads(attempt.output.read_text()) == {"refresh_token": local.TOKEN}
    info = attempt.output.stat()
    assert stat.S_ISREG(info.st_mode) and stat.S_IMODE(info.st_mode) == 0o600
    writes = attempt.events("token write")
    assert writes, "real descriptor observer did not see token bytes"
    assert all(row["regular"] and row["mode"] == 0o600 for row in writes)
    valid_exchange(attempt)
    assert not confidentiality_problems(attempt)


@pytest.mark.parametrize("missing,destination", [
    ("O_NONBLOCK", "new"),
    ("O_NONBLOCK", "existing"),
    ("fchmod", "existing"),
], ids=["missing-nonblock-new", "missing-nonblock-existing", "missing-fchmod-tightening"])
def test_unavailable_storage_capability_is_a_safe_recoverable_failure(tmp_path, monkeypatch, missing, destination):
    attempt = launch(tmp_path, monkeypatch, destination=destination, missing=missing)
    # Check side effects independently of the intended diagnostic red.
    assert attempt.result.returncode != 0
    assert not attempt.events("token write")
    if destination == "existing":
        assert attempt.output.is_file() and attempt.output.read_bytes() == local.OLD
    elif attempt.output.exists():
        assert attempt.output.is_file()
        assert local.TOKEN.encode() not in attempt.output.read_bytes()
    if attempt.events("exchange"):
        valid_exchange(attempt)
    # Capability preflight may refuse before or after listener construction,
    # without consent/exchange. Any constructed listener must still close;
    # launch checks that independently of the location of the preflight.
    lines = [line for line in attempt.result.stderr.splitlines() if line.strip()]
    problems = confidentiality_problems(attempt)
    if not (len(lines) == 1 and len(lines[0]) <= 600
            and lines[0].startswith("AUTH_TOKEN_WRITE_FAILED:")):
        problems.append("missing concise AUTH_TOKEN_WRITE_FAILED diagnostic")
    else:
        guidance = lines[0].partition(":")[2].lower()
        # Semantic alternatives leave the wording to the implementation. Merely
        # advising a writable directory misdiagnoses an unavailable OS facility.
        if not any(word in guidance for word in (
                "capabil", "support", "unavailable", "not available", "missing", "require",
                "undefined", "lacks", "not implemented", "operating system", "platform", "posix", "unix")):
            problems.append("guidance does not explain the storage capability limitation")
        if not any(word in guidance for word in ("secur", "privat", "permission", "nonblock", "fchmod", "safe", "protect")):
            problems.append("guidance does not identify secure storage")
        if not any(word in guidance for word in ("use ", "using ", "choose", "run ", "retry", "try again", "install", "configure")):
            problems.append("guidance gives no recovery action")
    # Match affirmative status lines, allowing truthful recovery such as
    # "No token was saved." Strip HTML tags to include callback status text.
    status_text = re.sub(r"<[^>]+>", "", public_output(attempt))
    if re.search(r"^\s*(?:(?:(?:the|your)\s+)?(?:refresh\s+)?token\s+"
                 r"(?:(?:was|has been)\s+)?(?:written|saved)|"
                 r"successfully\s+(?:saved|written|authorized)|authorization\s+(?:complete|successful))\b",
                 status_text, re.I | re.M):
        problems.append("failed storage claimed success")
    assert not problems, f"{missing} unavailable for {destination}: " + "; ".join(problems)


@pytest.mark.parametrize("destination", ["new", "existing"])
def test_native_regular_file_storage_is_private_from_first_token_byte(tmp_path, monkeypatch, destination):
    private_success(launch(tmp_path, monkeypatch, destination=destination))


@pytest.mark.parametrize("destination", ["new", "existing"])
def test_missing_fchmod_is_harmless_when_no_tightening_is_needed(tmp_path, monkeypatch, destination):
    attempt = launch(tmp_path, monkeypatch, destination=destination, missing="fchmod",
                     private=destination == "existing")
    private_success(attempt)
