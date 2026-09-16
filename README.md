# ads-mcp

A Google Ads MCP server for agents. Query accounts and reports, or stage
changes for review before applying them. **Read-only by default.**

Version 0.1.0 is unreleased and is not published to PyPI. Write support is
experimental. Supports CPython 3.12–3.14 on Linux and macOS; native Windows
is unsupported. Requires Google Ads SDK 31.4.0 or later within major 31.

- **27 read tools:** authenticated health checks, account and PMax inspection,
  performance reports, Keyword Planner and raw GAQL with bounded responses.
- **Reviewed changes:** write tools create plans; application checks account
  binding, expiry, preview requirements and configured budget/CPC limits.
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
[integration and migration guide](docs/migration.md).

## Experimental writes

Enabling writes exposes 67 operations. Read the [write guide](docs/writes.md)
before opting in. Writes require audit
storage and the appropriate caps; the default flow is plan → dry-run preview →
`confirm_and_apply`. Irreversible changes require an additional acknowledgement.

**Budget and CPC caps apply per operation.** They do not cap aggregate account
spend or CPA/ROAS targets. Enabling existing entities can also affect spend.
Use independent account controls and review the guide's full limit scope.

## Documentation

| Guide | What it covers |
| --- | --- |
| [Authentication](docs/authentication.md) | Your OAuth client, token storage and Google API access |
| [Configuration](docs/configuration.md) | Environment variables, defaults and audit storage |
| [Reporting](docs/reporting.md) | GAQL, pagination, date windows, history and forecasts |
| [Writes](docs/writes.md) | Plans, spend limits, creation requirements and validation |
| [Performance Max](docs/pmax.md) | Asset groups, signals, URL controls and Item-ID selection |
| [Errors](docs/errors.md) | Refusals, recovery and uncertain outcomes |
| [Tool catalog](docs/tools.md) | Every tool's parameters and defaults |
| [Operator guide](docs/operator-guide.md) | Account authority, data handling and host responsibilities |
| [Integration and migration](docs/migration.md) | Consumer compatibility and read-only rollout |

For development and redistribution, see [contributing](CONTRIBUTING.md),
[release checks](docs/releasing.md) and [security](SECURITY.md).

## License

[Apache License 2.0](LICENSE). Dependencies retain their own licenses; review
applicable obligations when redistributing them or adding bundled material.
