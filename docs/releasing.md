# Preparing a source release

The installed command exports an **explicit committed tree**, independent of
staged, unstaged and untracked files:

```bash
ads-mcp-export-source --repo /path/to/repository --ref COMMITISH --output /path/to/fresh-source
```

Use a trusted installed exporter and trusted Git executable. The output must
be an absent path in an existing writable parent outside the source repository,
with no symbolic-link parent components. Keep that parent protected from other
writers. The command refuses even existing empty destinations. No Git history,
index, author metadata, untracked files or remotes are copied or changed. Files
come from Git blobs, so archive export-ignore/substitution attributes cannot
silently change the selected content. Git symlinks, submodules, unsafe paths,
case-colliding or canonically equivalent Unicode directory/file identities
and tracked local integrations are refused rather than
followed or silently omitted. Missing local objects fail; fetching is not part
of export. Publication, initializing new public history and choosing a remote
are separate operator actions. Never publish the original history by inference
from a clean snapshot.

Containment uses the complete Git-returned worktree and Git-directory paths,
including meaningful trailing spaces or newlines, even when `--repo` names a
subdirectory or linked worktree. Readable uncommon source locations still
support external exports of old commits. Colliding path components are rejected
from the committed tree before output creation; committed names are never
normalized or renamed to force success. Unique non-ASCII paths remain supported.
These Linux/macOS checks do not certify Windows filenames or add Windows support.

`source-manifest.json` contains resolved commit/tree, sorted file SHA-256 hashes
and executable modes, scan policy/results and `human_review_required: true`.
Its own SHA-256 is printed. The source-file inventory excludes the manifest
itself. File/manifest errors return named, concise nonzero diagnostics without
echoing private input or subprocess detail. A failed fresh directory may retain
partial files or `.source-manifest.incomplete`; it has no successful manifest.
Inspect it privately, preserve any needed evidence, then retry with a fresh path.
Do not treat directory existence or a partial log as successful export.

The scanner checks all tracked filenames and blob bytes for common credential
assignments/token prefixes/private keys, private home paths, operational-context
patterns and excluded local integration components. Google access-token shapes
are checked in ordinary prose and Bearer headers as well as assignments.
It is a bounded pattern
scanner: unknown credentials, encodings, binary/compressed data, names and
business context can evade detection. Ordinary examples can trigger false
positives. A clean result provides **no automatic privacy or legal clearance**.

The installed `source_scan_exceptions.json` records narrow synthetic-test
exceptions, each bound to the exact complete file SHA-256 and each individual
matched span/hash. There is no repository-supplied exemption switch and no
blanket exclusion for tests or Python. Any content change invalidates that
file's exception. The manifest records every used exception for review.
Maintainers must inspect matching source before changing an exception; never
auto-approve new matches just to produce an export. The unchanged reviewed
fixture may export in another repository because approval binds its exact
content, not a private repository location.

An independent reviewer must verify file hashes, read the complete snapshot,
inspect all exemptions, licenses, provenance, examples and policy applicability,
and retain a **separate disposition bound to the exact manifest SHA-256**.
Use a record with `manifest_sha256`, reviewer identity, date, disposition
(`changes_required` or `approved_for_named_scope`), reviewed scope, findings and
remaining conditions. Retain it outside the snapshot so it cannot change the
manifest inventory. An edited source or manifest requires a new review. A
builder-generated manifest is not a reviewer approval; approval for source
preparation is not approval to publish or operate accounts.

After implementation changes, renew scans of the exact committed source export
and the resulting wheel/sdist archives, review every used synthetic exception,
and obtain a new disposition bound to the final manifest. Include the operator
guide's intended-use, requested-demo and conditional RMF/change-review duties
in that source-applicability review. Prior scans and dispositions cannot approve
changed artifacts.

## Reproduce release checks

The declared support matrix is CPython 3.12, 3.13 and 3.14 on Linux and macOS,
with current compatible dependencies in all six cells, plus minimum direct
dependencies on Linux 3.12. Native Windows is unsupported, including read-only
server use; capability-refusal tests are not Windows support evidence.
The workflow in `.github/workflows/ci.yml` configures these checks. **Workflow
presence is not hosted success.** Retain actual runs before release; unavailable
Linux/hosted runs remain missing evidence. Local macOS runs do not fill them.

In a new, dedicated virtual environment for each interpreter, run:

```bash
python -m pip install -c requirements-min.txt '.[dev]' build
python -m pip check
python -m pip freeze --all
python -m pytest -q
python scripts/check_capabilities.py
python scripts/parity.py
python scripts/check_release_archives.py --dependencies minimum --output /path/to/fresh-evidence
```

For current compatible dependencies, omit `-c requirements-min.txt` and use
`--dependencies current`. Minimum constrains direct runtime dependencies;
transitive versions still resolve compatibly and must be recorded. Use clean
Python 3.12 minimum and Python 3.14 current environments for local release
evidence. Do not upgrade an operator's existing runtime to perform these checks.

The archive script builds wheel and sdist, installs each in its own fresh venv,
runs dependency checks, and exercises all three console commands and both
catalog modes offline. The import probe runs with `-I`, outside the checkout,
and asserts that imports reside in the new environment. The source archive
must retain tests, the authored workflow requirements, the project license
and maintainer guidance. Archive validation rejects retired comparison
assets and their associated notice in both formats and the current tree.
Inspect any new bundled material for applicable obligations; an absence
check is not provenance or legal clearance.
Logs, archive hashes and exact resolved versions are retained in the selected
evidence directory. Full tests run separately in each clean matrix environment;
installed smoke checks are not a replacement for them. Run release builds from
the reviewed clean source snapshot to avoid local build inputs.

The security job resolves a fresh runtime independently of pip-audit tooling,
then `scripts/scan_dependencies.py` scans its exact package inventory and
requires complete advisory coverage. It retains JSON and requirements even on
failure; failed/unavailable scans must never be reported as clean. See the
[maintenance policy](../SECURITY.md) for cadence, ownership and exception rules.
Package metadata links to [Overcast-Solutions/ads-mcp](https://github.com/Overcast-Solutions/ads-mcp).
Before publication, verify those links from an outside account and enable and
verify the private reporting form described in [SECURITY.md](../SECURITY.md).
GitHub private vulnerability reporting requires a public repository; do not
claim the route works during private preparation. Confirm maintainer security
notifications as part of activation.

## Repository controls and public history

CI runs on pull requests, pushes to `main` and `shipwright/initial-source`,
manual dispatch and the weekly advisory schedule. A topic branch gets its
checks through its pull request; it does not start a duplicate push matrix.
Concurrent runs for the same event/ref supersede older runs. Actions are pinned
to reviewed full commit IDs; review upstream release notes and update the pins
alongside their version comments. The seven supported-environment jobs and
`security` must pass before merging. Configure required checks on the default
branch, require it to be up to date, and disallow force pushes and deletion.
If the default branch is renamed to something other than the two listed names,
update the push filter before relying on default-branch validation.

A clean current tree does not remove earlier files or commit metadata. Prepare
the exact public refs in an isolated repository with no object alternates or
development refs. Scan names, identities, messages, historical paths/blobs and
tags, then bind the built archives and review to that source tree. A fresh
root can be prepared locally from a reviewed export without rewriting any
existing remote. Merely adding that root as another branch does not remove
the old history from a repository that is later made public. Any remote
replacement, old-ref removal or history rewrite needs an explicit approved
publication plan; do not use a force push as routine release preparation.

The supported distribution provides code, not Google access. Release archives,
examples and CI must contain no working OAuth client or tokens. Each operator
must follow the [own-credential onboarding](../README.md#oauth-setup-one-time).
Publishing source does not approve an account cutover or experimental writes.
