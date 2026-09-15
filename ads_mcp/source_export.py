"""Read-only committed-source export; scanning is a review aid, not clearance."""

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import unicodedata


MANIFEST = "source-manifest.json"
TEMPORARY_MANIFEST = ".source-manifest.incomplete"
POLICY = "ads-mcp-source-v1"
RULES = {
    "credential": re.compile(
        rb"GOCSPX-[A-Za-z0-9_-]+|1//[A-Za-z0-9_-]+|ya29\.[A-Za-z0-9_.-]+|"
        rb"AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{20,}|"
        rb"sk-(?:proj-)?[A-Za-z0-9_-]{20,}|"
        rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----|"
        rb"(?im:\b(?:client_secret|refresh_token|api_key|access_token|"
        rb"GOOGLE_ADS_DEVELOPER_TOKEN)\s*=\s*[A-Za-z0-9_./+-]{16,}[ \t]*$)|"
        rb"(?i:(?:client_secret|refresh_token|api_key|access_token|"
        rb"GOOGLE_ADS_DEVELOPER_TOKEN)[\"']?\s*[:=]\s*[\"'][^\"'\r\n]+[\"'])"
    ),
    "private_home": re.compile(
        rb"/(?:Users|home)/[A-Za-z0-9_.-]+|"
        rb"[A-Za-z]:\\+(?:Users|Documents and Settings)\\+[^\s\"']+"
    ),
    "operational_context": re.compile(
        rb"(?i:engagements[/\\]+|operator "
        rb"workspace|private[-_ ](?:client|account))"
    ),
}
EXCLUDED = {".git", ".claude", ".codex", ".agents", ".engram", ".beads",
            "AGENTS.md", "CLAUDE.md", ".env", ".venv", "node_modules"}


class ExportError(Exception):
    """Only constant, public-safe diagnostic text crosses the CLI boundary."""


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise ExportError(
            "EXPORT_ARGUMENT_INVALID: provide --repo, --ref and --output with valid values."
        )


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def git(repo, *args):
    # No ambient alternate index, injected configuration, credential helper,
    # replacement object, lazy network fetch, or optional maintenance writes.
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
               GIT_OPTIONAL_LOCKS="0", GIT_NO_LAZY_FETCH="1",
               GIT_NO_REPLACE_OBJECTS="1", GIT_TERMINAL_PROMPT="0")
    try:
        result = subprocess.run(
            ["git", "--no-optional-locks", "-C", str(repo), *args],
            env=env, capture_output=True, timeout=30, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        raise ExportError(
            "EXPORT_GIT_FAILED: check Git installation and local repository access; retry."
        ) from None
    if result.returncode:
        raise ExportError(
            "EXPORT_GIT_FAILED: check repository access and provide a valid "
            "locally available commit ref; retry."
        )
    return result.stdout


def findings(data):
    return [{"rule": rule, "start": match.start(), "end": match.end(),
             "match_sha256": sha256(match.group())}
            for rule, pattern in RULES.items() for match in pattern.finditer(data)]


def scan(files):
    # This packaged allowlist is trusted code, never a policy supplied by the
    # repository being exported. Every exception binds full bytes AND matches.
    exceptions = json.loads(Path(__file__).with_name("source_scan_exceptions.json").read_text())
    retained = []
    for name, data, mode in files:
        matches = findings(name.encode()) + findings(data)
        if not matches:
            continue
        approved = exceptions.get(name)
        if not approved or approved["sha256"] != sha256(data) or approved["matches"] != matches:
            raise ExportError(
                "EXPORT_SCAN_REJECTED: review tracked content privately for credentials "
                "or private context; prepare a clean commit and retry."
            )
        retained.append({"path": name, **approved})
    return retained


def committed_files(repo, commit):
    files = []
    seen = set()
    spellings = {}
    for entry in git(repo, "ls-tree", "-rz", "--full-tree", commit).split(b"\0"):
        if not entry:
            continue
        metadata, raw_name = entry.split(b"\t", 1)
        mode, kind, oid = metadata.split()
        name = raw_name.decode("utf-8")
        parts = PurePosixPath(name).parts
        if (mode not in (b"100644", b"100755") or kind != b"blob"
                or not parts or name.startswith("/") or "\\" in name or ":" in name
                or any(p in (".", "..") or p.lower() in {x.lower() for x in EXCLUDED}
                       or p.lower().startswith(".env.") for p in parts)
                or any(ord(c) < 32 or ord(c) == 127 for c in name)
                or name.casefold() in seen
                or name != "/".join(parts)):
            raise ExportError(
                "EXPORT_ENTRY_UNSAFE: review tracked paths, local integrations "
                "and link entries; prepare a clean commit."
            )
        # Only the root namespace belongs to the exporter. Nested basenames
        # and similar names remain source identities, with no renaming.
        if unicodedata.normalize("NFD", parts[0].casefold()) in {
            unicodedata.normalize("NFD", reserved.casefold())
            for reserved in (MANIFEST, TEMPORARY_MANIFEST)
        }:
            raise ExportError(
                "EXPORT_ENTRY_COLLISION: reserved root manifest namespace conflicts "
                "with tracked source; rename the source entry and prepare a clean commit."
            )
        for length in range(1, len(parts) + 1):
            prefix = "/".join(parts[:length])
            # Compare canonical Unicode equivalents and case folds, including
            # directory prefixes. Keep the original spelling for all output;
            # normalization here only detects identities that could alias.
            key = unicodedata.normalize("NFD", prefix.casefold())
            prior = spellings.setdefault(key, prefix)
            if prior != prefix:
                raise ExportError(
                    "EXPORT_ENTRY_COLLISION: review Unicode or case-colliding "
                    "directory/file names and prepare a clean commit."
                )
        seen.add(name.casefold())
        data = git(repo, "cat-file", "blob", oid.decode("ascii"))
        files.append((name, data, 0o755 if mode == b"100755" else 0o644))
    return sorted(files)


def export(repo, ref, output):
    if not ref or ref.startswith("-") or "\0" in ref:
        raise ExportError("EXPORT_REF_INVALID: provide a valid commit or tag ref.")
    try:
        repo = Path(repo).resolve(strict=True)
    except OSError:
        raise ExportError(
            "EXPORT_REPOSITORY_UNREADABLE: check repository location and access; retry."
        ) from None
    if not repo.is_dir() or not os.access(repo, os.R_OK | os.X_OK):
        raise ExportError(
            "EXPORT_REPOSITORY_UNREADABLE: check repository directory access and retry."
        )
    output = Path(output).absolute()
    # Require a missing leaf in an existing parent: no user data is reused.
    # Resolve aliases to detect source containment before creating anything.
    resolved = output.resolve()
    if (output.exists() or output.is_symlink() or not output.parent.is_dir()
            or any(parent.is_symlink() for parent in output.parents)
            or resolved == repo or repo in resolved.parents or resolved in repo.parents):
        raise ExportError(
            "EXPORT_DESTINATION_UNSAFE: choose a fresh absent destination "
            "outside the source repository."
        )
    # --repo may name a subdirectory or linked worktree. Protect the actual
    # worktree and both Git directories, not just the caller's path spelling.
    protected = [repo]
    for option in ("--absolute-git-dir", "--git-common-dir"):
        # Git appends exactly one record terminator. All preceding whitespace
        # belongs to the path, including any newline at the end of its name.
        location = os.fsdecode(git(repo, "rev-parse", option).removesuffix(b"\n"))
        protected.append((repo / location).resolve())
    if git(repo, "rev-parse", "--is-bare-repository").strip() != b"true":
        worktree = os.fsdecode(
            git(repo, "rev-parse", "--show-toplevel").removesuffix(b"\n")
        )
        protected.append(Path(worktree).resolve())
    if any(resolved == path or path in resolved.parents for path in protected):
        raise ExportError(
            "EXPORT_DESTINATION_UNSAFE: choose a fresh path outside the "
            "complete source worktree and Git directories."
        )
    commit = git(
        repo, "rev-parse", "--verify", "--end-of-options", ref + "^{commit}"
    ).decode().strip()
    tree = git(repo, "rev-parse", "--verify", commit + "^{tree}").decode().strip()
    if not all(re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", x) for x in (commit, tree)):
        raise ExportError(
            "EXPORT_GIT_INVALID: check Git returned valid object identities and retry."
        )
    files = committed_files(repo, commit)
    exemptions = scan(files)
    manifest = {
        "schema_version": 1, "commit": commit, "tree": tree,
        "files": [{"path": name, "sha256": sha256(data), "mode": oct(mode)}
                  for name, data, mode in files],
        "scan": {"policy": POLICY, "status": "no_unresolved_pattern_findings",
                 "rules": sorted(RULES), "excluded_components": sorted(EXCLUDED),
                 "exceptions": exemptions,
                 "limitations": "Pattern scanning has false negatives and false positives; "
                                "no legal or privacy clearance."},
        "human_review_required": True,
    }
    # Private creation limits exposure if writing is interrupted. Only this
    # newly created directory is written; never extract Git archive attributes.
    output.mkdir(mode=0o700)
    for name, data, mode in files:
        path = output / name
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("xb") as stream:
            if stream.write(data) != len(data):
                raise OSError("short write")
        path.chmod(mode)
        if sha256(path.read_bytes()) != sha256(data):
            raise OSError("content changed")
    payload = (json.dumps(manifest, indent=2, ensure_ascii=True) + "\n").encode()
    # A partial manifest never has the successful manifest filename or valid
    # JSON. Atomic rename happens only after flush/fsync and complete writing.
    temporary = output / TEMPORARY_MANIFEST
    with temporary.open("xb") as stream:
        if stream.write(payload) != len(payload):
            raise OSError("short write")
        stream.flush()
        os.fsync(stream.fileno())
    temporary.rename(output / MANIFEST)
    return sha256(payload)


def main(argv=None):
    parser = Parser(description="Export a committed source tree for mandatory human review. "
                                "No publication or history changes.")
    parser.add_argument("--repo", required=True)
    parser.add_argument("--ref", required=True)
    parser.add_argument("--output", required=True)
    try:
        args = parser.parse_args(argv)
        identity = export(args.repo, args.ref, args.output)
    except ExportError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except (Exception, KeyboardInterrupt):
        # Never format exceptions: filenames, argv and provider details can all
        # contain private input. Failed fresh output is retained for inspection.
        print("EXPORT_INCOMPLETE: check repository/ref and writable destination; "
              "inspect any incomplete output, then retry with a fresh path.", file=sys.stderr)
        return 1
    print("SOURCE_SNAPSHOT_READY: human review required; manifest SHA-256 " + identity)
    return 0


if __name__ == "__main__":
    sys.exit(main())
