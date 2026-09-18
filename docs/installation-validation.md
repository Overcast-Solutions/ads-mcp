# Source installation and validation

The project is already available as public source on GitHub. The package version
is **0.1.0**; PyPI packages and version tags have not been published. It supports
CPython 3.12, 3.13 and 3.14 on Linux and macOS; native Windows is unsupported.
The source and package metadata use [Apache License 2.0](../LICENSE).

The server provides 84 operations, including 34 reads. Read-only mode is the
default; write support remains experimental. The [changelog](../CHANGELOG.md)
describes the workflows and protocol repair. Invalid request IDs now receive a
generic refusal before dispatch, and the same connection accepts valid requests
afterward. This does not add advertising operations or change credential setup.

## Install an exact source revision

Choose a reviewed commit from the public repository and record its full SHA.
`REPLACE_WITH_FULL_COMMIT_SHA` below is an explicit placeholder,
not a tag or executable example value. Replace it before running the commands.

Use a fresh checkout and virtual environment:

```bash
git clone https://github.com/Overcast-Solutions/ads-mcp.git
cd ads-mcp
git checkout --detach REPLACE_WITH_FULL_COMMIT_SHA
git rev-parse HEAD
git status --porcelain
python3.12 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/python -m pip check
.venv/bin/python -m pip freeze --all
.venv/bin/ads-mcp --version
```

Use `python3.13` or `python3.14` instead for another supported interpreter.
Check that `HEAD` equals the selected SHA, the checkout is clean and the command
reports `ads-mcp 0.1.0`. This is a noneditable install. Pinning source does not
pin transitive dependencies: retain the interpreter version and resolved package
inventory with validation results. The [release checks](releasing.md) describe
separate minimum/current dependency checks and fresh wheel/sdist installations.

If installing supplied artifacts, their handoff should identify the exact source
commit/tree, source-manifest hash, wheel and sdist hashes, checksum inventory and
the independently reviewed source
disposition. Verify any supplied archive against that inventory before using it.
Metadata, the installed version command and both archives must agree on 0.1.0.
Evidence from another commit does not validate the selected revision.

## Validate reads with your credentials

Each installer supplies their own Google Cloud project, Desktop app OAuth client
and authorized Google account. Follow [authentication](authentication.md) and
the [README setup](../README.md#oauth-setup-one-time); no shared credentials or
hosted authentication service are supplied. Keep credentials, tokens, account
data and audit records in private operator-managed storage.

1. Register the installed `.venv/bin/ads-mcp` command in your MCP host with
   `ADS_MCP_READ_ONLY=true` and your credential/token paths. Check that the host
   points to this installation's environment, then restart its server connection.
2. Call `health_check`. It performs an authenticated read; version output alone
   does not establish Google access. Resolve any named configuration or access
   refusal using the [error guide](errors.md).
3. Call `get_account_info` and verify the selected customer, currency and time
   zone against the account you intend to inspect. Check manager-login settings
   and account authority before querying other accounts.
4. Inspect the read-only catalog and make a small report request with an explicit
   date window. Compare returned account identity, units and available data with
   Google Ads. Review [reporting](reporting.md) for limits and pagination.
5. Record the commit, interpreter/dependency inventory, host configuration without
   secrets, checks performed and unresolved refusals. Keep account results private;
   share only redacted, synthetic reproductions when reporting problems.

Live Google Ads validation belongs to the operator and remains outstanding until
performed for the intended account and workflow. Offline tests use synthetic
provider state and genuine SDK messages; they do not establish live acceptance,
account eligibility or policy approval. A test account does not support every
Google Ads feature. Choose checks supported by the account, project access and
provider rules; a refusal is not permission to bypass them. Review the
[operator guide](operator-guide.md) before exposing account data to a host/model.

## Optional experimental write validation

Enable writes only after deliberate, separate approval for the exact account
and change. Follow the [write guide](writes.md), configure protected audit storage
and appropriate caps, then restart with write mode explicitly enabled. Use a
provider-eligible workflow and review every affected resource and serving/spend
impact. Some staging workflows already call provider validate-only.

Stage one approved change, inspect its account-bound expiring plan and complete
the required dry-run preview. Only then deliberately apply it with the required
acknowledgements. Read back the account state and inspect the audit result.
Plans are single-use; uncertain writes must be reconciled before any new plan,
never automatically retried. Budget/CPC caps apply per operation, not to aggregate
account spend, CPA/ROAS targets or every possible serving effect.

## Package release checks

For a future package release, evidence must bind to the final clean commit:
full supported dependency checks, fresh installed archives, independent installed review, public source
and history review, rendered changed documentation, exact-head hosted CI and
the dependency advisory scan. Retain missing checks and accepted limitations
explicitly; the existence of this guide does not claim those checks passed.

The source is already public. Preparing distribution artifacts or validating an
installation does not publish a PyPI package, push a version tag, merge a pull
request or authorize account changes. Those remain separate actions.
