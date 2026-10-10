# Configuration

Configure account access with the `GOOGLE_ADS_*` variables below. The server
starts read-only unless you explicitly enable experimental writes.

| Variable | Required | Default | Meaning |
|---|---|---|---|
| `GOOGLE_ADS_DEVELOPER_TOKEN` | no | — | optional legacy value, ignored for API access and never transmitted |
| `GOOGLE_ADS_CUSTOMER_ID` | yes | — | target account, `123-456-7890` or bare |
| `GOOGLE_ADS_LOGIN_CUSTOMER_ID` | no | — | MCC/manager login id; overrides a `google-ads.yaml` `login_customer_id` |
| `GOOGLE_ADS_CREDENTIAL_SOURCE` | no | **files** | where OAuth material comes from: `files`, `adc`, or `google_ads_yaml`; any other value is refused at startup |
| `GOOGLE_ADS_CREDENTIALS_PATH` | `files` only | — | OAuth Desktop-app client JSON |
| `GOOGLE_ADS_TOKEN_PATH` | `files` only | — | refresh-token JSON (generate-token writes it) |
| `GOOGLE_ADS_CONFIGURATION_FILE_PATH` | no | `$HOME/google-ads.yaml` | `google_ads_yaml` source only: the configuration file to read `client_id`, `client_secret`, `refresh_token` and `login_customer_id` from |
| `ADS_MCP_READ_ONLY` | no | **true** | `false`, `0`, `no`, or `off` (case-insensitive) registers mutation tools; unknown or empty values keep read-only enabled |
| `ADS_MCP_REQUIRE_DRY_RUN` | no | **true** | apply requires a prior dry-run preview of the same plan |
| `ADS_MCP_MAX_DAILY_BUDGET` | no | **unset = refuse** | cap (account currency) on any staged budget |
| `ADS_MCP_MAX_BID_INCREASE_PCT` | no | **unset = refuse increases** | max % bid increase vs the account's own or inherited bid; inclusive ceiling |
| `ADS_MCP_MAX_FIRST_BID` | no | **unset = refuse** | absolute ceiling for setting a bid where none exists (a percentage of zero is meaningless) |
| `ADS_MCP_AUDIT_LOG` | **required in write mode** | — | append-only JSONL audit path; applies fail closed if it is not writable |
| `ADS_MCP_PLAN_TTL_SECONDS` | no | 900 | staged-plan expiry |
| `ADS_MCP_ROW_LIMIT` | no | 1000 | row bound before pagination tokens |
| `ADS_MCP_RETRY_BASE_SECONDS` | no | 1.0 | transport backoff base |

## Credential sources

`GOOGLE_ADS_CREDENTIAL_SOURCE` selects where the OAuth client and refresh
token come from. It is validated before every other variable, and
`GOOGLE_ADS_CUSTOMER_ID` stays required for all three:

- `files` (the default) reads `GOOGLE_ADS_CREDENTIALS_PATH` and
  `GOOGLE_ADS_TOKEN_PATH`.
- `adc` reads the authorized-user Application Default Credentials file named
  by `GOOGLE_APPLICATION_CREDENTIALS`, else
  `$CLOUDSDK_CONFIG/application_default_credentials.json`, else
  `~/.config/gcloud/application_default_credentials.json`. Only
  `authorized_user` credentials are accepted, and they must come from your own
  Desktop OAuth client: `gcloud auth application-default login
  --client-id-file=<your client JSON>
  --scopes=https://www.googleapis.com/auth/adwords`.
- `google_ads_yaml` reads `client_id`, `client_secret`, `refresh_token` and
  `login_customer_id` from `GOOGLE_ADS_CONFIGURATION_FILE_PATH`, else
  `$HOME/google-ads.yaml`. Every other key in that file — `endpoint`,
  `http_proxy`, `logging`, `developer_token`, `json_key_file_path` and the rest
  — is ignored, never applied. Quote `login_customer_id` so a leading zero
  survives YAML's number rules.

Settings the selected source does not read are ignored and listed in
`health_check` as `config.unused_settings`. Credential problems other than the
source value itself surface through `health_check` and the tools as named
`AUTH_CONFIG_*` errors, never as file contents or credential values.

Every `ADS_MCP_*` variable is optional with a fail-safe default: the required
Google Ads environment alone boots read-only, with caps refusing. The exception
is deliberate — enabling mutations (`ADS_MCP_READ_ONLY=false`) without
`ADS_MCP_AUDIT_LOG` is refused at startup: a write path nobody can audit is
not a write path worth having.

## Audit storage

Audit records use a local regular file created with mode `0600` from the first
record. Existing regular files are tightened through the opened descriptor
before append; symlinks and special files are refused without writing. Secure
no-follow opens, nonblocking opens and descriptor permission changes must be
available; otherwise auditing fails closed for plans and execution, while read
observations remain best-effort. Native platforms without these operations
(including native Windows) cannot use this secure audit writer for mutations.
Use a local filesystem with atomic append support for concurrent writers.

Protect the audit directory and all its parent directories against replacement
by other users; file permissions alone do not secure the directory, encrypt
records, revoke already-open handles, or make the log tamper-evident. Operators
own retention periods, access controls, backups, rotation and deletion. Stop
writers before rotating or removing logs, preserve records required for outcome
reconciliation, and secure archived copies as carefully as the active file.
There is no automatic rotation or deletion. Failed pre-write setup preserves
existing bytes; a disk failure during append can leave a partial final record
that needs operator reconciliation before resuming writes.

See [experimental writes](writes.md) for cap scope and the plan, preview and apply flow.
