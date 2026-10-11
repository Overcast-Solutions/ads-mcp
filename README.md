# ads-mcp

A Google Ads MCP server for agents. **Guarded writes are the core capability:**
every change is staged as a plan, checked against the configured budget and CPC
caps, and recorded in an append-only audit log before it reaches an account. A
dry-run preview is required by default, and only an operator who sets
`ADS_MCP_REQUIRE_DRY_RUN=false` can waive it. The server starts read-only when
`ADS_MCP_READ_ONLY` is unset, and `ADS_MCP_READ_ONLY=false` enables writes.

The source is publicly available under Apache License 2.0. Install from source;
the package is not published to PyPI. Supports CPython 3.12–3.14 on Linux and
macOS; native Windows is unsupported. Requires Google Ads SDK 31.4.0 or later
within major 31.

- **Guarded changes:** 85 operations once writes are enabled. Write tools stage
  plans; application rechecks account binding, expiry, preview requirements and
  the configured budget/CPC limits.
- **35 read tools:** authenticated health checks, account and PMax inspection,
  performance reports, Keyword Planner, API field metadata and raw GAQL with
  bounded responses.
- **Traceable execution:** write mode requires an audit log. Uncertain writes
  are surfaced and never retried automatically; transient reads use bounded retries.

## Install

Install from source into a virtual environment:

```bash
git clone https://github.com/Overcast-Solutions/ads-mcp.git
cd ads-mcp
python3.12 -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/ads-mcp --version
```

For commit-pinned installation and read-only-first validation, follow the
[installation and validation guide](docs/installation-validation.md).

## OAuth setup (one-time)

**Bring your own credentials.** Each installer must use a Google Cloud project
and OAuth client they control, plus a Google account authorized for their Ads
accounts. The package provides no shared project, maintainer credentials or
hosted authentication service.

1. Enable the Google Ads API in your project and create a **Desktop app** OAuth
   client. Download its JSON to private, encrypted storage.
2. Review [authentication and API access](docs/authentication.md) for project
   approval, consent-screen settings, access levels and token lifetimes.
3. Generate your refresh token using the downloaded client JSON:

```bash
export GOOGLE_ADS_CREDENTIALS_PATH="/absolute/private/path/oauth_client.json"
export GOOGLE_ADS_TOKEN_PATH="/absolute/private/path/token.json"
.venv/bin/ads-mcp-generate-token
```

Complete Google's consent flow yourself. Keep credentials, tokens, account data
and audit records on operator-managed encrypted storage. Never send credential
files or tokens to maintainers or paste them into issues or model prompts.
Internal teams may use their own authorized installation; independent users
must configure their own credentials. Review the [operator guide](docs/operator-guide.md)
before connecting account data to an MCP host or model provider.

If you already have `gcloud` Application Default Credentials or a
`google-ads.yaml`, the [credential sources guide](docs/credential-sources.md)
covers using them instead.

## Claude Code / MCP registration

For clients using `mcpServers` configuration, replace the executable path,
credential paths and account IDs below with your own. Omit
`GOOGLE_ADS_LOGIN_CUSTOMER_ID` if you are not using a manager login.

```json
{
  "mcpServers": {
    "ads-mcp-readonly": {
      "type": "stdio",
      "command": "/absolute/path/to/ads-mcp/.venv/bin/ads-mcp",
      "env": {
        "GOOGLE_ADS_CUSTOMER_ID": "123-456-7890",
        "GOOGLE_ADS_LOGIN_CUSTOMER_ID": "111-222-3333",
        "GOOGLE_ADS_CREDENTIALS_PATH": "/absolute/private/path/oauth_client.json",
        "GOOGLE_ADS_TOKEN_PATH": "/absolute/private/path/token.json",
        "ADS_MCP_READ_ONLY": "true"
      }
    }
  }
}
```

Call `health_check` to verify authenticated access, then `get_account_info`
to confirm the account. Metric reports require an explicit date window or
`last_n_days`; see the [reporting guide](docs/reporting.md).

For a read-only rollout alongside an existing server, follow the
[integration and migration guide](docs/migration.md). A client that cannot
launch a subprocess can use the local read-only
[HTTP transport](docs/http-transport.md).

## Guarded writes

Enabling writes exposes 85 operations. Read the [write guide](docs/writes.md)
before opting in. Writes require audit
storage and the appropriate caps; the default flow is plan → dry-run preview →
`confirm_and_apply`. Irreversible changes require an additional acknowledgement.

**Budget and CPC caps apply per operation.** They do not cap aggregate account
spend or CPA/ROAS targets. Enabling existing entities can also affect spend.
Use independent account controls and review the guide's full limit scope.

## Coming from Google's server

Google's own [google-ads-mcp](https://github.com/googleads/google-ads-mcp)
exposes only read tools (as of 2026-10-09), while ads-mcp adds guarded writes
alongside its 35 reads. Their three tools map across like this:

| Google's server | ads-mcp | Argument differences |
| --- | --- | --- |
| `list_accessible_customers` | `list_accounts` | neither takes arguments, and they answer about different sets: theirs lists the accounts the credentials can access directly, while `list_accounts` walks the account hierarchy under the login customer id if one is configured and otherwise under the configured customer id (`GOOGLE_ADS_LOGIN_CUSTOMER_ID` overrides a `google-ads.yaml` login id); no id can be supplied per call |
| `search` | `run_gaql` | theirs takes the query apart into structured arguments — `customer_id` is required, with `fields`, `resource`, `conditions`, `orderings`, `limit` and an optional `login_customer_id` — while `run_gaql` takes one GAQL query string in `query`, makes `customer_id` optional (it defaults to the configured account), and adds `format` (`json`, `table`, `csv`), `page_size` and `page_token` for bounded responses |
| `get_resource_metadata` | `get_resource_metadata` | same tool name, renamed argument: theirs takes `resource_name`, ads-mcp takes `name`, which accepts a resource (`campaign`), a field (`campaign.status`) or a field prefix; adds `page_size` (default 100) and `page_token`; takes no account argument, because schema metadata addresses no account |

ads-mcp does not offer several things their deployment path provides: MCP
resources, a per-call login customer id, a hosted OAuth proxy, and a Cloud Run
container. Its own HTTP transport is local and read-only; see the
[HTTP transport guide](docs/http-transport.md). ads-mcp is an independent
project, not affiliated with or endorsed by Google.

## Documentation

| Guide | What it covers |
| --- | --- |
| [Authentication](docs/authentication.md) | Your OAuth client, token storage and Google API access |
| [Credential sources](docs/credential-sources.md) | Token files, `gcloud` ADC and `google-ads.yaml` |
| [Configuration](docs/configuration.md) | Environment variables, defaults and audit storage |
| [HTTP transport](docs/http-transport.md) | Local read-only streamable HTTP, its limits and its risk |
| [Reporting](docs/reporting.md) | GAQL, pagination, date windows, history and forecasts |
| [Writes](docs/writes.md) | Plans, spend limits, creation requirements and validation |
| [Campaign networks](docs/campaign-networks.md) | Search defaults, precise updates and network reporting |
| [Search URLs](docs/search-urls.md) | Inspect and update existing ad and keyword destinations |
| [Shared targeting](docs/shared-targeting.md) | Shared negative lists and explicit ad-group demographics |
| [Performance Max](docs/pmax.md) | Asset groups, signals, URL controls and Item-ID selection |
| [PMax experiments](docs/pmax-experiments.md) | Same-campaign URL tests, results and promotion observation |
| [Errors](docs/errors.md) | Refusals, recovery and uncertain outcomes |
| [Tool catalog](docs/tools.md) | Every tool's parameters and defaults |
| [Operator guide](docs/operator-guide.md) | Account authority, data handling and host responsibilities |
| [Integration and migration](docs/migration.md) | Consumer compatibility and read-only rollout |

For development and redistribution, see [contributing](CONTRIBUTING.md),
[release checks](docs/releasing.md) and [security](SECURITY.md).

## License

[Apache License 2.0](LICENSE). Dependencies retain their own licenses; review
applicable obligations when redistributing them or adding bundled material.
