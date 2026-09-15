"""F062: installed export preserves Git identities and refuses known leaks.

All Git writes below create synthetic tmp_path fixtures. Tree plumbing builds
otherwise unrepresentable names without depending on host filesystem spelling.
Policy prose, final project exceptions and release review remain human gates.
"""
import json
import os
from pathlib import Path
import subprocess

import pytest

from test_release_source_export_contract import (
    assert_failure, digest, export_command, file_entries, fingerprint, manifests,
)


@pytest.fixture
def git_env(tmp_path):
    config = tmp_path / "empty-git-config"
    config.write_text("")
    return {
        "PATH": os.environ.get("PATH", os.defpath),
        "HOME": str(tmp_path), "PYTHONNOUSERSITE": "1",
        "PYTHONDONTWRITEBYTECODE": "1", "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": str(config), "GIT_TERMINAL_PROMPT": "0",
        "GIT_AUTHOR_NAME": "Synthetic Oracle", "GIT_AUTHOR_EMAIL": "oracle@example.invalid",
        "GIT_COMMITTER_NAME": "Synthetic Oracle", "GIT_COMMITTER_EMAIL": "oracle@example.invalid",
    }


def git(repo, env, *args, data=None):
    result = subprocess.run(["git", "-C", str(repo), *args], env=env,
                            input=data, capture_output=True, timeout=20)
    assert result.returncode == 0, result.stderr
    return result.stdout


def init(repo, env):
    repo.mkdir()
    git(repo, env, "init", "--quiet")
    git(repo, env, "config", "gc.auto", "0")
    git(repo, env, "config", "maintenance.auto", "false")


def commit_files(repo, env, files):
    for name, data in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    git(repo, env, "add", "--all")
    git(repo, env, "commit", "--quiet", "-m", "Synthetic fixture")
    return git(repo, env, "rev-parse", "HEAD").decode().rstrip("\n")


def invoke(repo, env, ref, output):
    return subprocess.run([export_command(), "--repo", str(repo), "--ref", ref,
                           "--output", str(output)], env=env, cwd=output.parent,
                          capture_output=True, text=True, timeout=30)


def safe_diagnostic(result, *private):
    text = result.stdout + result.stderr
    spellings = [spelling for value in private for spelling in (
        str(value), json.dumps(str(value))[1:-1], repr(str(value))[1:-1])]
    assert all(spelling not in text for spelling in spellings)
    assert "Traceback" not in text and "fatal:" not in text


def separate_common(source, worktree, common, env):
    (source / ".git").rename(common)
    # '/.' preserves a final newline inside Git's newline-terminated pointer.
    (source / ".git").write_text("gitdir: " + str(common) + "/.\n")
    git(source, env, "worktree", "repair", str(worktree))


def successful(result, output, commit, files):
    assert result.returncode == 0, result.stderr
    found = manifests(output)
    assert len(found) == 1
    path, manifest = found[0]
    assert manifest["commit"] == commit
    assert dict(file_entries(manifest)) == {name: digest(data) for name, data in files.items()}
    assert {p.relative_to(output).as_posix() for p in output.rglob("*") if p.is_file()} == set(files) | {path.name}
    for name, data in files.items():
        assert (output / name).read_bytes() == data


@pytest.mark.parametrize("suffix", ["", " ", "\n"], ids=["ordinary", "space", "newline"])
@pytest.mark.parametrize("location", ["subdirectory", "linked-worktree", "common-git"])
def test_containment_uses_complete_git_path_identity(tmp_path, git_env, suffix, location):
    source = tmp_path / ("source" + suffix)
    init(source, git_env)
    files = {"sub/file.txt": b"public original\n", "root.txt": b"public root\n"}
    commit = commit_files(source, git_env, files)
    worktree = source
    roots = [source]
    if location != "subdirectory":
        worktree = tmp_path / ("linked" + suffix)
        git(source, git_env, "worktree", "add", "--quiet", "--detach", str(worktree), commit)
        roots.append(worktree)
    if location == "common-git":
        # A separate common Git directory can itself end in meaningful whitespace.
        common = tmp_path / ("common.git" + suffix)
        separate_common(source, worktree, common, git_env)
        roots.append(common)
        output = common / "must-refuse"
    else:
        output = worktree / "must-refuse"
    # Preserve index, tracked differences and unrelated untracked bytes too.
    (worktree / "sub/file.txt").write_bytes(b"uncommitted difference\n")
    (worktree / "untracked.txt").write_bytes(b"synthetic untracked\n")
    before = [fingerprint(root) for root in roots]
    result = invoke(worktree / "sub", git_env, commit, output)
    safe_diagnostic(result, source, worktree, output)
    assert_failure(result, output)
    assert not output.exists(), "containment must refuse before creating an output directory"
    assert [fingerprint(root) for root in roots] == before


@pytest.mark.parametrize("suffix", ["", " ", "\n"], ids=["ordinary", "space", "newline"])
@pytest.mark.parametrize("location", ["subdirectory", "linked-worktree", "common-git"])
def test_unusual_readable_sources_still_export_an_immutable_old_ref(tmp_path, git_env, suffix, location):
    source = tmp_path / ("source" + suffix)
    init(source, git_env)
    files = {"sub/file.txt": b"original public bytes\n", "root.txt": b"root bytes\n"}
    old = commit_files(source, git_env, files)
    commit_files(source, git_env, {"root.txt": b"later committed bytes\n"})
    roots, worktree = [source], source
    if location != "subdirectory":
        worktree = tmp_path / ("linked" + suffix)
        git(source, git_env, "worktree", "add", "--quiet", "--detach", str(worktree), "HEAD")
        roots.append(worktree)
    if location == "common-git":
        common = tmp_path / ("common.git" + suffix)
        separate_common(source, worktree, common, git_env)
        roots.append(common)
    (worktree / "untracked.txt").write_bytes(b"not in selected tree\n")
    before = [fingerprint(root) for root in roots]
    output = tmp_path / "external-export"
    result = invoke(worktree / "sub", git_env, old, output)
    successful(result, output, old, files)
    safe_diagnostic(result, source, worktree, output)
    assert [fingerprint(root) for root in roots] == before


def tree_commit(repo, env, files):
    """Git tree objects retain both aliases even on a normalizing filesystem."""
    root = {}
    for name, data in files.items():
        node = root
        parts = name.split("/")
        for part in parts[:-1]:
            node = node.setdefault(part, {})
        node[parts[-1]] = data

    def write(node):
        records = []
        for name, value in node.items():
            if isinstance(value, dict):
                mode, kind, oid = b"040000", b"tree", write(value)
            else:
                mode, kind = b"100644", b"blob"
                oid = git(repo, env, "hash-object", "-w", "--stdin", data=value).rstrip(b"\n")
            records.append(mode + b" " + kind + b" " + oid + b"\t" + name.encode("utf-8") + b"\0")
        return git(repo, env, "mktree", "-z", data=b"".join(records)).rstrip(b"\n")

    return git(repo, env, "commit-tree", write(root).decode(), data=b"Synthetic identity fixture\n").decode().rstrip("\n")


@pytest.mark.parametrize("names", [
    ("e\u0301/a.txt", "\u00e9/b.txt"),
    ("nested/e\u0301.txt", "nested/\u00e9.txt"),
    ("Reports/a.txt", "reports/b.txt"),
    ("Readme.txt", "README.txt"),
    ("STRASSE/a.txt", "Straße/b.txt"),
], ids=["unicode-directories", "unicode-files", "case-directories", "case-files", "casefold-directories"])
def test_distinct_committed_aliases_refuse_before_output_on_supported_hosts(tmp_path, git_env, names):
    repo = tmp_path / "source"
    init(repo, git_env)
    files = dict(zip(names, [b"first distinct blob\n", b"second distinct blob\n"]))
    commit = tree_commit(repo, git_env, files)
    listed = git(repo, git_env, "ls-tree", "-rz", "--name-only", commit).split(b"\0")
    assert {name.encode("utf-8") for name in names} <= set(listed)
    before = fingerprint(repo)
    output = tmp_path / "refused-export"
    result = invoke(repo, git_env, commit, output)
    safe_diagnostic(result, repo, output, *names)
    assert_failure(result, output)
    assert not output.exists(), "tree-visible aliases must refuse before materialization"
    assert fingerprint(repo) == before


def test_unique_non_ascii_and_distinct_ascii_identities_export_unchanged(tmp_path, git_env):
    repo = tmp_path / "source"
    init(repo, git_env)
    files = {"日本語/資料.txt": b"public non-ASCII name\n", "alpha/a.txt": b"a\n", "beta/b.txt": b"b\n"}
    commit = tree_commit(repo, git_env, files)
    before = fingerprint(repo)
    output = tmp_path / "external-export"
    successful(invoke(repo, git_env, commit, output), output, commit, files)
    assert fingerprint(repo) == before


@pytest.mark.parametrize("context", ["prose", "bearer", "assignment"])
@pytest.mark.parametrize("path", ["README.md", "tests/public_example.py"])
def test_common_access_token_shape_is_scanned_in_public_text(tmp_path, git_env, context, path):
    repo = tmp_path / "source"
    init(repo, git_env)
    # Deliberately invalid and public. No scanner policy or exception is patched.
    token = "ya29." + "SYNTHETIC_ONLY_NEVER_VALID_" * 5
    contents = {
        "prose": "Example transcript token: " + token + "\n",
        "bearer": "Authorization: Bearer " + token + "\n",
        "assignment": json.dumps({"access_token": token}) + "\n",
    }[context].encode()
    commit = commit_files(repo, git_env, {path: contents})
    before = fingerprint(repo)
    output = tmp_path / "refused-export"
    result = invoke(repo, git_env, commit, output)
    safe_diagnostic(result, repo, output, token)
    assert_failure(result, output)
    assert "EXPORT_SCAN_REJECTED" in result.stderr
    assert not manifests(output)
    assert fingerprint(repo) == before


def test_ordinary_authorization_explanation_remains_exportable(tmp_path, git_env):
    repo = tmp_path / "source"
    init(repo, git_env)
    files = {"README.md": b"Authorization: Bearer <access token supplied at runtime>\n"}
    commit = commit_files(repo, git_env, files)
    output = tmp_path / "external-export"
    successful(invoke(repo, git_env, commit, output), output, commit, files)
