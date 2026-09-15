"""F064: exporter-owned root names are inventory conflicts, before output.

Each case is a separate ordinary Git repository made with add/commit and
verified by strict fsck. No raw objects, empty trees, scanner exemptions or
host-specific interpreter paths are needed.
"""
import json
import re

import pytest

import harness as h
from test_export_identity_privacy_contract import (
    commit_files, git, git_env, init, invoke, safe_diagnostic, successful,
)
from test_release_source_export_contract import fingerprint


FINAL = "source-manifest.json"
TEMPORARY = ".source-manifest.incomplete"
CHILD = "synthetic-sensitive-name.txt"
CONTENTS = b"SYNTHETIC_USER_BYTES_DO_NOT_ECHO\n"
OBSERVE_CREATION = r'''
import os
from pathlib import Path
import sys
output = os.environ["ORACLE_RESERVED_OUTPUT"]
marker = Path(os.environ["ORACLE_RESERVED_MARKER"])
def observe(event, args):
    if event in ("socket.connect", "socket.bind", "socket.getaddrinfo", "socket.sendto"):
        raise OSError("synthetic export contract forbids network")
    if event == "os.mkdir" and os.path.abspath(args[0]) == output:
        marker.write_text("destination creation attempted")
sys.addaudithook(observe)
'''


def observed_export(repo, env, commit, output):
    injection = output.parent / "creation-observer"
    injection.mkdir()
    (injection / "sitecustomize.py").write_text(OBSERVE_CREATION)
    marker = output.parent / "creation-observed"
    result = invoke(repo, {**env, "PYTHONPATH": str(injection),
        "ORACLE_RESERVED_OUTPUT": str(output), "ORACLE_RESERVED_MARKER": str(marker)}, commit, output)
    assert "Error in sitecustomize" not in result.stderr
    return result, marker.exists()


def ordinary_repository(tmp_path, env, files):
    repo = tmp_path / ("source-" + h.FAKE_CLIENT_SECRET)
    init(repo, env)
    commit = commit_files(repo, env, files)
    # Prove these exact paths are committed, through normal Git operations.
    listed = git(repo, env, "ls-tree", "-rz", "--name-only", commit).split(b"\0")
    assert set(listed) - {b""} == {name.encode() for name in files}
    git(repo, env, "fsck", "--full", "--strict")
    # The selected immutable commit must not consume or change staged,
    # unstaged or unrelated untracked material. Fingerprint includes .git.
    (repo / "staged.txt").write_bytes(b"synthetic staged difference\n")
    git(repo, env, "add", "staged.txt")
    (repo / "README.md").write_bytes(b"synthetic unstaged difference\n")
    (repo / "untracked.txt").write_bytes(b"synthetic untracked data\n")
    user = tmp_path / "existing-user-directory"
    user.mkdir()
    (user / "keep.txt").write_bytes(CONTENTS)
    (user / "keep.txt").chmod(0o600)
    return repo, commit, user


def named_private_refusal(result, repo, output):
    safe_diagnostic(result, repo, output, h.FAKE_CLIENT_SECRET, CHILD, CONTENTS.decode().strip())
    text = result.stdout + result.stderr
    assert result.returncode != 0, "reserved root namespace was exported as successful source"
    assert not result.stdout.strip(), "refusal must not claim a ready source snapshot"
    assert re.search(r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b", result.stderr), (
        "a stable named refusal category is required, without pinning a new code spelling")
    assert len(text.strip().splitlines()) == 1, "refusal needs concise one-line recovery"


@pytest.mark.parametrize("root", [
    FINAL, FINAL.upper(), "\u017fource-manifest.json",
    TEMPORARY, TEMPORARY.upper(), ".\u017fource-manifest.incomplete",
], ids=["final", "final-case", "final-casefold", "temporary", "temporary-case", "temporary-casefold"])
@pytest.mark.parametrize("directory", [False, True], ids=["file", "directory"])
def test_reserved_root_names_refuse_before_creating_output(tmp_path, git_env, root, directory):
    # Both owned basenames are ASCII: NFD has no distinct canonical spelling.
    # Long-s exercises the existing supported Unicode casefold alias policy;
    # unrelated accented names below remain valid, not compatibility-normalized.
    name = root + "/" + CHILD if directory else root
    files = {"README.md": b"public synthetic fixture\n", name: CONTENTS}
    repo, commit, user = ordinary_repository(tmp_path, git_env, files)
    before = fingerprint(repo), fingerprint(user)
    output = tmp_path / "fresh-output"
    result, attempted_creation = observed_export(repo, git_env, commit, output)
    assert (fingerprint(repo), fingerprint(user)) == before
    named_private_refusal(result, repo, output)
    assert not attempted_creation and not output.exists(), (
        "predictable exporter-owned name collision created incomplete output before refusal")
    text = result.stderr.lower()
    assert any(word in text for word in ("collision", "collid", "conflict", "reserved")), (
        "diagnostic must identify a source-name collision, not an ordinary later write failure")
    assert any(word in text for word in ("rename", "resolve", "remove", "clean commit", "change")), (
        "recovery must fix the source collision; another fresh destination alone cannot help")
    assert any(word in text for word in ("source", "tracked", "commit", "manifest"))


def test_nested_reserved_names_and_safe_neighbors_export_exactly(tmp_path, git_env):
    files = {
        "README.md": b"public synthetic source\n",
        "nested/" + FINAL: b"ordinary nested final basename\n",
        "nested/" + TEMPORARY: b"ordinary nested temporary basename\n",
        "directories/" + FINAL + "/child.txt": b"nested final directory\n",
        "directories/" + TEMPORARY + "/child.txt": b"nested temporary directory\n",
        "aliases/" + FINAL.upper(): b"nested final alias\n",
        "aliases/" + TEMPORARY.upper() + "/child.txt": b"nested temporary alias\n",
        FINAL + ".backup/child.txt": b"similar final prefix\n",
        TEMPORARY + ".backup": b"similar temporary prefix\n",
        "copy-" + FINAL: b"contained basename\n",
        "copy-" + TEMPORARY: b"contained temporary basename\n",
        "source-manife\u015bt.json": b"distinct accented name\n",
        "caf\u00e9/child.txt": b"safe accented directory\n",
        "\u65e5\u672c\u8a9e/\u8cc7\u6599.txt": b"safe non-ASCII source name\n",
    }
    repo, commit, user = ordinary_repository(tmp_path, git_env, files)
    before = fingerprint(repo), fingerprint(user)
    output = tmp_path / "fresh-output"
    result, attempted_creation = observed_export(repo, git_env, commit, output)
    assert (fingerprint(repo), fingerprint(user)) == before
    safe_diagnostic(result, repo, output, h.FAKE_CLIENT_SECRET)
    successful(result, output, commit, files)
    assert attempted_creation, "positive export did not reach the passive OS creation observer"
    assert "SOURCE_SNAPSHOT_READY" in result.stdout and not result.stderr
    manifest = json.loads((output / FINAL).read_text())
    assert set(manifest) == {"schema_version", "commit", "tree", "files", "scan", "human_review_required"}
    assert manifest["schema_version"] == 1 and manifest["human_review_required"] is True
    assert manifest["scan"]["status"] == "no_unresolved_pattern_findings"
    assert manifest["scan"]["exceptions"] == []
    assert not (output / TEMPORARY).exists()
