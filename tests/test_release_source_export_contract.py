"""F059: installed source export of an explicit synthetic committed Git tree.

Only synthetic repositories are created or committed. Source preservation uses
semantic file fingerprints (never unstable directory sizes). Documentation,
license provenance, policy source applicability, CI/platform executions and
manifest-bound human disposition are root/reviewer evidence, not prose tests.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys

import pytest

import harness as h


SECRET = "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424242"
REFRESH = "1//synthetic-export-refresh-token-424242"
PRIVATE = "/Users/synthetic-private-operator/engagements/client-private"
WINDOWS_PRIVATE = r"C:\Users\private-operator\engagements\client-private"
DETAIL = "SYNTHETIC_SUBPROCESS_PRIVATE_DETAIL"


def digest(data):
    return hashlib.sha256(data).hexdigest()


def git(repo, *args, env=None):
    result = subprocess.run(["git", "-C", str(repo), *args], env=env,
        capture_output=True, timeout=20)
    assert result.returncode == 0, f"synthetic Git fixture setup failed: {args!r}: {result.stderr[:300]!r}"
    return result.stdout.decode().strip()


@pytest.fixture
def repository(tmp_path):
    repo = tmp_path / "synthetic-repo"
    repo.mkdir()
    config = tmp_path / "neutral-git-config"
    config.write_text("")
    env = h.scrubbed_env({"GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": str(config),
        "GIT_AUTHOR_NAME": "Synthetic Oracle", "GIT_AUTHOR_EMAIL": "oracle@example.invalid",
        "GIT_COMMITTER_NAME": "Synthetic Oracle", "GIT_COMMITTER_EMAIL": "oracle@example.invalid",
        "GIT_TERMINAL_PROMPT": "0"})
    # Never inherit an ambient alternate index/worktree or injected Git config.
    for key in list(env):
        if key.startswith(("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE", "GIT_CONFIG_COUNT", "GIT_CONFIG_KEY_", "GIT_CONFIG_VALUE_")):
            env.pop(key)
    git(repo, "init", env=env)
    # Keep the synthetic source quiescent for complete semantic fingerprints.
    # Do not ignore disappearing lockfiles or weaken history/index coverage.
    git(repo, "config", "gc.auto", "0", env=env)
    git(repo, "config", "maintenance.auto", "false", env=env)
    (repo / "historical-only.json").write_text(json.dumps({"refresh_token": REFRESH}))
    git(repo, "add", "historical-only.json", env=env)
    git(repo, "commit", "-m", "Synthetic historical fixture", env=env)
    git(repo, "rm", "historical-only.json", env=env)
    files = {"README.md": b"Synthetic source export fixture.\n", "package/module.py": b'VALUE = "neutral"\n',
             "bin/tool": b"synthetic executable bytes\n", "data/unicode.txt": "éclair\n".encode()}
    for name, data in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    (repo / "bin/tool").chmod(0o755)
    git(repo, "add", ".", env=env)
    git(repo, "commit", "-m", "Synthetic clean tree", env=env)
    commit = git(repo, "rev-parse", "HEAD", env=env)
    tree = git(repo, "rev-parse", "HEAD^{tree}", env=env)
    git(repo, "tag", "synthetic-release", env=env)
    # Staged, unstaged and untracked differences must never be exported.
    (repo / "README.md").write_text("Unstaged private difference " + PRIVATE)
    (repo / "staged-only.txt").write_text("Synthetic staged-only file")
    git(repo, "add", "staged-only.txt", env=env)
    for name in (".claude/settings.local.json", ".codex/config.toml", "oauth-private.json", "AGENTS.md"):
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"client_secret": SECRET, "refresh_token": REFRESH, "private": PRIVATE}))
    return repo, env, commit, tree, files


def fingerprint(root):
    """Include history, refs, index, modes, working bytes and untracked files."""
    result = {}
    for path in [root, *sorted(root.rglob("*"))]:
        meta = path.lstat()
        mode = stat.S_IMODE(meta.st_mode)
        name = path.relative_to(root).as_posix()
        if path.is_symlink():
            result[name] = ("link", mode, os.readlink(path))
        elif path.is_dir():
            result[name] = ("directory", mode)
        else:
            result[name] = ("file", mode, digest(path.read_bytes()))
    return result


def export_command():
    command = Path(sys.executable).with_name("ads-mcp-export-source")
    assert command.is_file() and os.access(command, os.X_OK), (
        "F059 missing behavior: installed ads-mcp-export-source executable is required; "
        "the existing installed package has no source-export command"
    )
    return str(command)


def invoke(repo, env, ref, output, *, extra_env=None):
    command = export_command()
    before = fingerprint(repo) if repo.exists() else None
    result = subprocess.run([command, "--repo", str(repo), "--ref", ref, "--output", str(output)],
        cwd=output.parent, env={**env, **(extra_env or {})}, capture_output=True, text=True, timeout=30)
    if before is not None:
        assert fingerprint(repo) == before, "export changed source files, index, refs, history or untracked integration"
    return result


def all_values(value):
    if isinstance(value, dict):
        for item in value.values():
            yield from all_values(item)
    elif isinstance(value, list):
        for item in value:
            yield from all_values(item)
    else:
        yield value


def manifests(output):
    candidates = []
    if output.exists() and output.is_dir():
        for path in output.rglob("*.json"):
            if "manifest" in path.name.lower() and path.is_file() and not path.is_symlink():
                try:
                    value = json.loads(path.read_text())
                except (ValueError, UnicodeError):
                    continue
                if isinstance(value, dict):
                    candidates.append((path, value))
    return candidates


def keyed(value, word):
    if isinstance(value, dict):
        for key, item in value.items():
            if word in key.lower():
                yield item
            yield from keyed(item, word)
    elif isinstance(value, list):
        for item in value:
            yield from keyed(item, word)


def file_entries(manifest):
    # Permit either a path->hash mapping or sorted {path,sha256} records. The
    # signed contract specifies content and ordering, not one manifest layout.
    for candidate in keyed(manifest, "files"):
        if isinstance(candidate, dict):
            entries = []
            for path, value in candidate.items():
                sha = value.get("sha256") if isinstance(value, dict) else value
                entries.append((path, sha))
            return entries
        if isinstance(candidate, list) and all(isinstance(x, dict) for x in candidate):
            return [(x.get("path"), x.get("sha256")) for x in candidate]
    pytest.fail("manifest lacks the required sorted source file SHA-256 inventory")


def assert_manifest(output, commit, tree, files):
    found = manifests(output)
    assert len(found) == 1, "successful export must emit one reviewable JSON manifest"
    path, manifest = found[0]
    assert commit in list(all_values(list(keyed(manifest, "commit"))))
    assert tree in list(all_values(list(keyed(manifest, "tree"))))
    entries = file_entries(manifest)
    assert [p for p, sha in entries] == sorted(files)
    assert dict(entries) == {p: digest(data) for p, data in files.items()}
    assert list(keyed(manifest, "scan")), "manifest must record scan policy and results"
    assert any("policy" in k for obj in keyed(manifest, "scan") if isinstance(obj, dict) for k in obj) or list(keyed(manifest, "scan_policy"))
    scans = list(keyed(manifest, "scan"))
    assert any(list(keyed(scan, word)) for scan in scans for word in ("result", "status", "finding")), "manifest must retain scan results, not only a policy label"
    review = list(keyed(manifest, "review"))
    required = any(item is True or (isinstance(item, str) and item.lower() in ("required", "human_review_required")) for item in review)
    required = required or any(value is True for item in review for value in keyed(item, "required"))
    assert required, "manifest must say human review is required, not completed or optional"
    assert not any(str(value).lower() in ("privacy certified", "legally approved", "review complete") for value in all_values(manifest))
    actual = {p.relative_to(output).as_posix() for p in output.rglob("*") if p.is_file()}
    assert actual == set(files) | {path.relative_to(output).as_posix()}
    assert not (output / ".git").exists()
    assert not any(p.is_symlink() for p in output.rglob("*"))
    for name, data in files.items():
        assert (output / name).read_bytes() == data
    assert os.access(output / "bin/tool", os.X_OK), "committed executable mode was lost"
    return digest(path.read_bytes())


def assert_failure(result, output):
    text = result.stdout + result.stderr
    assert result.returncode != 0, "unsafe or incomplete export incorrectly reported success"
    assert "Traceback" not in text
    # Ordinary JSON and repr diagnostics double the same path backslashes.
    assert all(secret not in text for secret in (
        SECRET, REFRESH, PRIVATE, WINDOWS_PRIVATE, json.dumps(WINDOWS_PRIVATE)[1:-1], DETAIL
    ))
    assert re.search(r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+\b", text), "export failure needs a stable named diagnostic"
    assert 1 <= len(text.strip().splitlines()) <= 8, "failure should have concise recovery guidance"
    assert any(word in text.lower() for word in ("check", "choose", "provide", "retry", "fresh", "review", "inspect", "valid", "writable"))
    assert not re.search(r"export (?:complete|succeeded)|successfully exported|review complete", text, re.I)
    for path, value in manifests(output):
        # An incomplete marker may remain, but never a successful manifest.
        assert any(v in ("incomplete", "failed") for v in all_values(value)), "partial output contains a successful manifest"


@pytest.mark.parametrize("ref_kind", ["commit", "tag", "head"])
def test_installed_export_resolves_committed_tree_exactly_and_preserves_original(repository, tmp_path, ref_kind):
    repo, env, commit, tree, files = repository
    later_files = {**files, "package/module.py": b'VALUE = "later"\n'}
    (repo / "package/module.py").write_bytes(later_files["package/module.py"])
    # Advance only this tracked path, preserving the staged-only fixture.
    git(repo, "commit", "--only", "-m", "Synthetic later clean tree",
        "--", "package/module.py", env=env)
    later_commit = git(repo, "rev-parse", "HEAD", env=env)
    later_tree = git(repo, "rev-parse", "HEAD^{tree}", env=env)
    assert later_commit != commit and later_tree != tree
    expected = (later_commit, later_tree, later_files) if ref_kind == "head" else (commit, tree, files)
    ref = {"commit": commit, "tag": "synthetic-release", "head": "HEAD"}[ref_kind]
    output = tmp_path / "fresh-export"
    result = invoke(repo, env, ref, output)
    assert result.returncode == 0, f"neutral committed source export failed: {result.stderr[:500]}"
    manifest_hash = assert_manifest(output, *expected)
    assert re.fullmatch("[0-9a-f]{64}", manifest_hash)
    assert all(secret not in result.stdout + result.stderr for secret in (SECRET, REFRESH, PRIVATE))


@pytest.mark.parametrize("contents,path", [
    (json.dumps({"client_secret": SECRET}), "settings.json"),
    (json.dumps({"refresh_token": REFRESH}), "data/token.json"),
    ("GOOGLE_ADS_DEVELOPER_TOKEN=" + SECRET, "environment.txt"),
    ("operator workspace " + PRIVATE, "notes.md"),
    ("operator workspace " + WINDOWS_PRIVATE, "windows-notes.md"),
    ("neutral local integration", ".claude/settings.json"),
    ("neutral local integration", ".codex/config.toml"),
])
def test_tracked_private_material_is_rejected_without_echoing_matches(repository, tmp_path, contents, path):
    repo, env, commit, tree, files = repository
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(contents)
    git(repo, "add", path, env=env)
    # Commit only the planted path, leaving the existing staged fixture intact.
    git(repo, "commit", "--only", "-m", "Synthetic tracked privacy fixture", "--", path, env=env)
    output = tmp_path / "rejected-export"
    result = invoke(repo, env, "HEAD", output)
    assert_failure(result, output)


@pytest.mark.parametrize("kind", ["symlink", "submodule"])
def test_git_links_are_rejected_without_following_them(repository, tmp_path, kind):
    repo, env, commit, tree, files = repository
    victim = tmp_path / "synthetic-outside-secret"
    victim.write_text(SECRET)
    if kind == "symlink":
        (repo / "unsafe-link").symlink_to(victim)
        git(repo, "add", "unsafe-link", env=env)
        git(repo, "commit", "--only", "-m", "Synthetic symlink", "--", "unsafe-link", env=env)
    else:
        git(repo, "update-index", "--add", "--cacheinfo", f"160000,{commit},unsafe-module", env=env)
        git(repo, "commit", "-m", "Synthetic gitlink", env=env)
    before = fingerprint(tmp_path / "synthetic-repo")
    result = invoke(repo, env, "HEAD", tmp_path / "rejected-export")
    assert_failure(result, tmp_path / "rejected-export")
    assert victim.read_text() == SECRET and fingerprint(repo) == before


@pytest.mark.parametrize("kind", ["nonempty", "symlink", "file", "inside-source"])
def test_unsafe_destinations_preserve_existing_user_data(repository, tmp_path, kind):
    repo, env, commit, tree, files = repository
    output = tmp_path / "occupied"
    victim = tmp_path / "existing-directory"
    victim.mkdir()
    (victim / "keep").write_text("existing synthetic user data")
    if kind == "nonempty":
        output.mkdir()
        (output / "keep").write_text("existing synthetic user data")
    elif kind == "symlink":
        output.symlink_to(victim, target_is_directory=True)
    elif kind == "file":
        output.write_text("existing synthetic user data")
    else:
        output = repo / "export-must-not-change-source"
    before = fingerprint(victim)
    old = fingerprint(output) if output.is_dir() and not output.is_symlink() else None
    result = invoke(repo, env, commit, output)
    assert_failure(result, output)
    assert fingerprint(victim) == before
    if old is not None:
        assert fingerprint(output) == old
    if kind == "file":
        assert output.read_text() == "existing synthetic user data"
    if kind == "symlink":
        assert output.is_symlink() and os.readlink(output) == str(victim)


@pytest.mark.parametrize("ref", ["does-not-exist", "--all", "HEAD:README.md"])
def test_invalid_or_noncommit_refs_have_named_secret_safe_failures(repository, tmp_path, ref):
    repo, env, commit, tree, files = repository
    output = tmp_path / "bad-ref-export"
    assert_failure(invoke(repo, env, ref, output), output)


def test_missing_repository_has_named_recovery(repository, tmp_path):
    repo, env, commit, tree, files = repository
    output = tmp_path / "missing-repo-export"
    assert_failure(invoke(tmp_path / "missing-repo", env, commit, output), output)


@pytest.mark.parametrize("kind", ["nonzero_git", "missing_git"])
def test_git_execution_failure_does_not_expose_subprocess_detail(repository, tmp_path, kind):
    repo, env, commit, tree, files = repository
    tools = tmp_path / "synthetic-tools"
    tools.mkdir()
    if kind == "nonzero_git":
        fake = tools / "git"
        fake.write_text("#!" + sys.executable + "\nimport sys\nprint(" + repr(DETAIL + SECRET + PRIVATE) + ", file=sys.stderr)\nsys.exit(42)\n")
        fake.chmod(0o700)
    output = tmp_path / "git-failed-export"
    result = invoke(repo, env, commit, output, extra_env={"PATH": str(tools)})
    assert_failure(result, output)


FAULT_INJECTION = '''
import builtins, errno, io, os
from pathlib import Path
root = Path(os.environ["ORACLE_EXPORT_OUTPUT"])
source = Path(os.environ["ORACLE_EXPORT_REPO"])
kind = os.environ["ORACLE_EXPORT_FAULT"]
marker = Path(os.environ["ORACLE_EXPORT_MARKER"])
real_open, real_io_open, real_os_open, real_mkdir = builtins.open, io.open, os.open, os.mkdir
real_replace, real_rename = os.replace, os.rename
def target(path):
    try:
        p = Path(path).absolute()
        if p == source or source in p.parents:
            return False
        if kind == "source_file":
            return "module.py" in str(p)
        if kind == "manifest":
            return "manifest" in str(p).lower()
        return p == root or root in p.parents
    except TypeError:
        return False
def fail(path):
    if target(path) and (kind == "unwritable" or
        (kind == "manifest" and "manifest" in str(path).lower()) or
        (kind == "source_file" and str(path).endswith("module.py"))):
        with real_open(marker, "w") as f:
            f.write("observed")
        raise OSError(errno.EACCES if kind == "unwritable" else errno.ENOSPC,
                      "SYNTHETIC_SUBPROCESS_PRIVATE_DETAIL")
def open_file(path, mode="r", *args, **kwargs):
    if any(c in mode for c in "wax+"):
        fail(path)
    return real_io_open(path, mode, *args, **kwargs)
def open_fd(path, flags, *args, **kwargs):
    if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT):
        fail(path)
    return real_os_open(path, flags, *args, **kwargs)
def mkdir(path, *args, **kwargs):
    if kind == "unwritable":
        fail(path)
    return real_mkdir(path, *args, **kwargs)
def replace(src, dst, *args, **kwargs):
    fail(dst)
    return real_replace(src, dst, *args, **kwargs)
def rename(src, dst, *args, **kwargs):
    fail(dst)
    return real_rename(src, dst, *args, **kwargs)
builtins.open = open_file
io.open = open_file
os.open = open_fd
os.mkdir = mkdir
os.replace = replace
os.rename = rename
'''


@pytest.mark.parametrize("kind", ["unwritable", "source_file", "manifest"])
def test_installed_partial_write_failures_never_claim_success(repository, tmp_path, kind):
    repo, env, commit, tree, files = repository
    injection = tmp_path / "fault-injection"
    injection.mkdir()
    (injection / "sitecustomize.py").write_text(FAULT_INJECTION)
    output, marker = tmp_path / "partial-export", tmp_path / "fault-observed"
    result = invoke(repo, env, commit, output, extra_env={"PYTHONPATH": str(injection),
        "PYTHONDONTWRITEBYTECODE": "1", "ORACLE_EXPORT_OUTPUT": str(output),
        "ORACLE_EXPORT_MARKER": str(marker), "ORACLE_EXPORT_FAULT": kind, "ORACLE_EXPORT_REPO": str(repo)})
    assert_failure(result, output)
    assert marker.read_text() == "observed", "installed exporter did not reach the intended OS failure boundary"


PARTIAL_WRITE_INJECTION = FAULT_INJECTION + '''
fds = {}
real_os_write = os.write
def partial_target(path):
    if isinstance(path, int):
        return fds.get(path, False)
    try:
        p = Path(path).absolute()
        if p == source or source in p.parents:
            return False
        return ("module.py" in str(p) if kind == "partial_source" else "manifest" in str(p).lower())
    except TypeError:
        return False
def record_partial(count):
    with real_open(marker, "w") as file:
        file.write(str(count))
    raise OSError(errno.ENOSPC, "SYNTHETIC_SUBPROCESS_PRIVATE_DETAIL")
class PartialFile:
    def __init__(self, file):
        self.file = file
    def __getattr__(self, name):
        return getattr(self.file, name)
    def __enter__(self):
        return self
    def __exit__(self, *args):
        return self.file.__exit__(*args)
    def write(self, data):
        if data:
            count = self.file.write(data[:max(1, len(data) // 2)])
            self.file.flush()
            record_partial(count)
        return self.file.write(data)
    def writelines(self, lines):
        for line in lines:
            self.write(line)
def partial_open(path, mode="r", *args, **kwargs):
    file = real_io_open(path, mode, *args, **kwargs)
    if any(c in mode for c in "wax+") and partial_target(path):
        return PartialFile(file)
    return file
def partial_fd(path, flags, *args, **kwargs):
    fd = real_os_open(path, flags, *args, **kwargs)
    if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT):
        fds[fd] = partial_target(path)
    return fd
def partial_write(fd, data):
    if fds.get(fd) and data:
        count = real_os_write(fd, data[:max(1, len(data) // 2)])
        record_partial(count)
    return real_os_write(fd, data)
builtins.open = partial_open
io.open = partial_open
os.open = partial_fd
os.write = partial_write
'''


@pytest.mark.parametrize("kind", ["partial_source", "partial_manifest"])
def test_interrupted_writes_leave_no_success_manifest(repository, tmp_path, kind):
    repo, env, commit, tree, files = repository
    injection = tmp_path / "partial-write-injection"
    injection.mkdir()
    (injection / "sitecustomize.py").write_text(PARTIAL_WRITE_INJECTION)
    output, marker = tmp_path / "partial-export", tmp_path / "partial-byte-count"
    result = invoke(repo, env, commit, output, extra_env={"PYTHONPATH": str(injection),
        "PYTHONDONTWRITEBYTECODE": "1", "ORACLE_EXPORT_OUTPUT": str(output),
        "ORACLE_EXPORT_MARKER": str(marker), "ORACLE_EXPORT_FAULT": kind, "ORACLE_EXPORT_REPO": str(repo)})
    assert_failure(result, output)
    assert marker.exists() and int(marker.read_text()) > 0, "fixture must interrupt after writing actual output bytes"


def test_unreadable_repository_is_a_named_failure_without_source_changes(repository, tmp_path):
    repo, env, commit, tree, files = repository
    command = export_command()
    before, mode = fingerprint(repo), stat.S_IMODE(repo.stat().st_mode)
    output = tmp_path / "unreadable-export"
    try:
        repo.chmod(0)
        result = subprocess.run([command, "--repo", str(repo), "--ref", commit, "--output", str(output)],
            cwd=tmp_path, env=env, capture_output=True, text=True, timeout=20)
    finally:
        repo.chmod(mode)
    assert_failure(result, output)
    assert fingerprint(repo) == before
