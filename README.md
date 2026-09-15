# ads-mcp

A Google Ads MCP server for agents, with 21 read tools and a management
surface that stages changes for review before applying them. Read-only mode
is the default. Version 0.1.0 is unreleased; write support is experimental.

- **Authenticated health checks.** `health_check` makes an API read and
  distinguishes authentication, transport, and configuration failures.
- **Retries for reads.** Transient read failures use bounded backoff.
  Mutations are never retried automatically: a lost response can leave the
  outcome uncertain, and repeating the request could duplicate a change.
- **One execution path.** Mutation tools return plans. `confirm_and_apply`
  checks customer binding, expiry, single use, configured preview requirements,
  spend limits, and acknowledgement of irreversible operations.
- **Budget and CPC limits.** Configuration controls daily-budget, bid-increase,
  and first-bid ceilings. Account reads establish existing bid baselines,
  and application checks them again. These are per-operation limits,
  not an account-wide spending ceiling; see the scope below.
- **Reporting fidelity.** Curated reports provide typed metrics and bounded
  responses. GAQL projection preserves selected fields, including zeros and
  explicit nulls. Reads can target accessible accounts under a manager login.
- **Audit records.** Plan and execution events are recorded in an append-only
  JSONL log. Partial failures, uncertain transport outcomes, and audit failures
  are surfaced to the caller. Write mode requires an audit destination.

See the [tool catalog](docs/tools.md) for parameters and defaults.

`pause_entity`, `enable_entity` and `remove_entity` require an `entity_id`
matching the `entity_type`: `campaign` and `ad_group` use one numeric ID;
`ad` uses the composite `ad_group_id~ad_id`, and `keyword` uses
`ad_group_id~criterion_id`. For example, `201~901` identifies ad 901 under
ad group 201. Surrounding whitespace and leading zeroes in each segment
normalize to the same ID in the preview and request. Missing or extra
segments and malformed IDs return `INVALID_ID` before account calls.

Report `campaign_id` filters use one numeric ID, normalized consistently in
queries, returned scope and local pagination. An omitted optional filter or
explicit `null` selects all campaigns; an explicit blank or malformed value
returns `INVALID_ID` before account calls. Shopping performance, listing
groups and product status still require a campaign ID.

## Install

First-release server support is CPython 3.12, 3.13 and 3.14 on Linux and macOS.
Native Windows is unsupported, including read-only operation and mutation
audit storage. Google Ads SDK 31.4.0 or later within major 31 is required.
From a source checkout:

```bash
uv venv --python 3.12
uv pip install -e ".[dev]"
uv run --no-sync ads-mcp --version
```

Use the absolute path to the installed executable when registering a desktop
MCP client. A virtual environment's executable is normally `.venv/bin/ads-mcp`
on macOS/Linux. This package has
not been published to PyPI.

## OAuth setup (one-time)

**Bring your own Google credentials.** Each person or organization installing
ads-mcp must configure a Google Cloud project and OAuth client they control,
then sign in with a Google account authorized for the Ads accounts they use.
The package provides no maintainer credentials, shared OAuth client, hosted
authentication service or fallback access. Never request or distribute another
organization's credential files or refresh tokens.

An organization may use its own credentials for its authorized internal
installation. This does not grant independent users access through that
organization's project. Run separate installations for independent operators;
do not expose this stdio server as a shared credential-backed proxy.

Google sunset developer tokens on **September 9, 2026**. API access now belongs
to the Cloud project that owns your OAuth client. This downloaded server uses
the operator's own project and OAuth credentials; it supplies no shared project
or credentials. Existing credentials can be reused only when they belong to
the operator and are authorized for this installation.
Legacy `GOOGLE_ADS_DEVELOPER_TOKEN` is optional and ignored for API access;
the server uses the SDK's supported `use_cloud_org_for_api_access` option to
omit the header from unary and streaming requests. See Google's
[migration guide](https://developers.google.com/google-ads/api/docs/api-policy/developer-token).

1. In Google Cloud Console: create (or reuse) your own project, enable the
   **Google Ads API**, and create an OAuth client of type **Desktop app**.
   Download its JSON — this is your `GOOGLE_ADS_CREDENTIALS_PATH` file.
2. Configure the OAuth consent screen for the intended users. External apps
   left in Testing normally receive refresh tokens that expire after seven
   days. See Google's [OAuth error guidance](https://developers.google.com/google-ads/api/docs/get-started/common-errors)
   before choosing the app's publishing status.
3. Set `GOOGLE_ADS_CREDENTIALS_PATH` to the downloaded client JSON and
   `GOOGLE_ADS_TOKEN_PATH` to the token output path, then generate the refresh
   token:

```bash
export GOOGLE_ADS_CREDENTIALS_PATH="/absolute/private/path/oauth_client.json"
export GOOGLE_ADS_TOKEN_PATH="/absolute/private/path/token.json"
uv run --no-sync ads-mcp-generate-token   # opens the consent screen
```

Replace both paths with locations on your own encrypted storage. Complete
Google's consent flow yourself; the helper uses your client JSON and stores
your refresh token locally. Do not send either file to the maintainers or
paste their contents into an issue, chat or model prompt.

4. Configure your `GOOGLE_ADS_CUSTOMER_ID` and, for a manager login, your
   `GOOGLE_ADS_LOGIN_CUSTOMER_ID`. Register the installed command with the
   [MCP environment example](#claude-code--mcp-registration), using your own
   paths and account IDs. Start read-only and call `health_check` to verify
   authenticated account access before requesting reports.

Missing required environment settings refuse startup. Unreadable files or
incomplete OAuth material refuse authenticated operations; there is no
automatic selection of maintainer credentials. The
server can validate credential structure and Google access, but cannot prove
who legally owns a supplied credential. Ownership and permission remain the
operator's responsibility.

`AUTH_TOKEN_REVOKED` identifies credentials that need to be renewed.

The token helper requires a platform and filesystem that enforce POSIX private
file permissions (mode `0600`) and support nonblocking opens (`os.O_NONBLOCK`)
and descriptor-based regular-file checks. It also needs `os.fchmod` when an
existing file's permissions must be tightened; secure creation and an already
private file do not require that call. If a required API is unavailable, the
helper exits with `AUTH_TOKEN_WRITE_FAILED` before writing new token bytes,
preserving existing content. Run it on a system with these capabilities and
use a regular file as the token destination.

The helper does not implement Windows ACL storage; native Windows helper
support is unclaimed and operation has not been validated.

Use operator-managed encrypted storage for credentials, token files, audit
records, transcripts and backups. Private permissions alone are not encryption.
Review the [operator guide](docs/operator-guide.md) before connecting account
data to an MCP host or model provider. This release certifies neither downstream
storage nor providers.

Review the existing OAuth project's access in the Google Ads API Overview in
Cloud Console, including any migrated access level. Apply there if the project
lacks the access you need. Check that project owner/editor contacts are current
for Google's administrative notices. The
[migration guide](https://developers.google.com/google-ads/api/docs/api-policy/developer-token)
describes these operator responsibilities.

Enabling the API initially grants Test access, which cannot access production
accounts. Explorer permits production reads but excludes Keyword Planner;
Planner needs Basic or Standard access. Account permissions and manager login
still apply independently of project approval. Check the services and quotas
you need against Google's
[access levels](https://developers.google.com/google-ads/api/docs/api-policy/access-levels).

New Basic and Standard applications require brand verification. Existing access
holders are not required to repeat it solely for this migration. For a Basic
application, Google's Ads-specific guidance requires an External audience and
In production publishing status, even for an internal-use app. Follow the
[brand verification guide](https://developers.google.com/google-ads/api/docs/api-policy/brand-verification)
and the [migration requirements](https://developers.google.com/google-ads/api/docs/api-policy/developer-token).
Brand verification is separate from restricted-scope OAuth verification:
review Google's [credential security and verification guidance](https://developers.google.com/google-ads/api/docs/productionize/secure-credentials)
for your deployment and any applicable exceptions. Publishing alone does not
establish verification or grant Ads account access.

## Configuration (environment)

Configure account access with the `GOOGLE_ADS_*` variables below. The server
starts read-only unless you explicitly enable experimental writes.

| Variable | Required | Default | Meaning |
|---|---|---|---|
| `GOOGLE_ADS_DEVELOPER_TOKEN` | no | — | optional legacy value, ignored for API access and never transmitted |
| `GOOGLE_ADS_CUSTOMER_ID` | yes | — | target account, `123-456-7890` or bare |
| `GOOGLE_ADS_LOGIN_CUSTOMER_ID` | no | — | MCC/manager login id |
| `GOOGLE_ADS_CREDENTIALS_PATH` | yes | — | OAuth Desktop-app client JSON |
| `GOOGLE_ADS_TOKEN_PATH` | yes | — | refresh-token JSON (generate-token writes it) |
| `ADS_MCP_READ_ONLY` | no | **true** | `false`, `0`, `no`, or `off` (case-insensitive) registers mutation tools; unknown or empty values keep read-only enabled |
| `ADS_MCP_REQUIRE_DRY_RUN` | no | **true** | apply requires a prior dry-run preview of the same plan |
| `ADS_MCP_MAX_DAILY_BUDGET` | no | **unset = refuse** | cap (account currency) on any staged budget |
| `ADS_MCP_MAX_BID_INCREASE_PCT` | no | **unset = refuse increases** | max % bid increase vs the account's own or inherited bid; inclusive ceiling |
| `ADS_MCP_MAX_FIRST_BID` | no | **unset = refuse** | absolute ceiling for setting a bid where none exists (a percentage of zero is meaningless) |
| `ADS_MCP_AUDIT_LOG` | **required in write mode** | — | append-only JSONL audit path; applies fail closed if it is not writable |
| `ADS_MCP_PLAN_TTL_SECONDS` | no | 900 | staged-plan expiry |
| `ADS_MCP_ROW_LIMIT` | no | 1000 | row bound before pagination tokens |
| `ADS_MCP_RETRY_BASE_SECONDS` | no | 1.0 | transport backoff base |

Every `ADS_MCP_*` variable is optional with a fail-safe default: the required
Google Ads environment alone boots read-only, with caps refusing. The exception
is deliberate — enabling mutations (`ADS_MCP_READ_ONLY=false`) without
`ADS_MCP_AUDIT_LOG` is refused at startup: a write path nobody can audit is
not a write path worth having.

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

Spend guardrails apply at plan creation and again at application:

- Budget mutations require `ADS_MCP_MAX_DAILY_BUDGET`. Bid increases require
  `ADS_MCP_MAX_BID_INCREASE_PCT`; verified bid decreases remain allowed with
  that cap unset.
- Bidding strategy changes and CPA/ROAS targets are **uncapped**: this model
  bounds daily budgets and explicit CPC bids. A CPA target is not a daily
  budget. These changes still use the plan and application flow. Enabling an
  existing entity or changing portfolio settings can also affect spend without
  supplying a new budget or CPC. These caps do not enforce aggregate account
  spend; use independent account controls for that purpose.
- Account reads establish a keyword's own bid, or its inherited ad-group bid
  when it has none. Ad-group updates read that group's bid. The compatibility
  parameter `current_bid` has no effect on the baseline or limit. A 1.20
  account bid with a 50% cap allows a bid of 1.80, inclusively.
- A verified absence of any applicable bid uses `ADS_MCP_MAX_FIRST_BID`, an
  absolute amount in account currency. Unset means first bids are refused.
  `create_ad_group.cpc_bid_micros` and keyword dictionaries in `draft_keywords`
  and `draft_campaign` support explicit first bids. Keywords added to an
  existing ad group inherit its bid for the percentage check. A new campaign's
  keywords require `ad_group_name` and use the first-bid ceiling.
- Explicit CPC bids must remain positive after conversion to integer micros.
  Values below one micro-unit refuse `INVALID_BID` before account reads;
  keyword `cpc_bid_micros` values must themselves be positive integers.
- Application reads existing baselines again and uses the applying server's
  current caps. Failed, empty, or mismatched reads refuse with
  `BID_BASELINE_UNVERIFIED` and leave an audit record; they never select the
  first-bid ceiling. New entities establish their own absence of prior bids
  by creation, while existing keyword parents still require a successful read.
- `apply_recommendation` applies only recommendations that are either on a
  small, enum-validated spend-neutral allowlist (creative and measurement
  work) or that name a daily budget this server can compare to your cap.
  It reads the supported v25 recommendation messages, including nested
  `options[]` and `budget_options[]`, and compares the **maximum** proposed
  daily budget across all options to `ADS_MCP_MAX_DAILY_BUDGET`. CPA targets,
  ROAS targets, current budgets, and estimated costs are not daily budgets.
  An under-cap recommendation yields a plan; application checks the staged
  amount and freshly read recommendation against the applying server's cap.
  Types without a proposed budget and outside the allowlist are refused with
  `SPEND_IMPACT_UNBOUNDED`, naming the type and recording the refusal in the
  audit log. Apply those deliberately in the Google Ads UI.

Generic `draft_campaign` creation supports only `SEARCH`, `DISPLAY` and the
`PERFORMANCE_MAX` shell. Other known channel enum values refuse with
`UNSUPPORTED_CREATION_CHANNEL` before account calls because this tool cannot
supply their channel-specific prerequisites; configure those channels through
Google Ads with the required settings. Unknown values refuse `INVALID_CHANNEL_TYPE`.
Campaign creation accepts `channel_type`, `target_cpa`/`target_roas`, optional
`ad_group_name` and keywords, and `status` (default `PAUSED`, also used for its
new ad group); keywords are enabled under those parents. Both campaign creation
tools require an explicit boolean `contains_eu_political_advertising` declaration.
The `draft_campaign` channel `PERFORMANCE_MAX` creates only a campaign shell,
with no asset group or serving creative, and rejects ad-group/keyword children.
Use `create_pmax_campaign` for a complete initial non-retail PMax campaign.
Supported creation graphs remain subject to provider/account eligibility,
bidding-strategy compatibility and policy checks. A generic shell does not
establish serving readiness. Any supplied `validate_only` proof applies only
to its exact tested PMax graph, not every channel, strategy or account.

`create_pmax_campaign` requires existing numeric image IDs supplied explicitly:
`landscape_image_asset_ids` (1–20), `square_image_asset_ids` (1–20), and
`logo_asset_ids` (1–5), with no duplicates within a role. It verifies the exact
configured-account resources and IMAGE type at staging and again at apply.
Images need positive integer dimensions and a file size of 1–5,120,000 bytes.
Landscape images must be at least 600×314 with
`abs(width - 1.91 * height) <= 1` pixel, a local rounding allowance that accepts
600×314 and 1200×628, not a claim about Google's tolerance. Square marketing
images must be square and at least 300×300; logos must be square and at least
128×128. No images are selected or downloaded implicitly.

PMax requires 3–15 headlines (30 counted characters each, at least one ≤15),
1–5 long headlines (90), 2–5 descriptions (90, at least one ≤60), a nonblank
business name (25), and nonempty HTTP/HTTPS final URLs. Unicode East Asian Width
W/F characters count as two units; others count as one. One atomic request
creates the non-shared budget, campaign, text assets, asset group, all creative
links and geo criteria. Brand guidelines are explicitly disabled, retaining the
business name and logos on the asset group. `start_paused` defaults to true and
controls the campaign and group. A successful apply returns
`campaign_resource_name`. Local validation and recorded request tests do not
establish Google's final policy acceptance.

`draft_campaign`, `create_ad_group` and `draft_responsive_search_ad` accept
`PAUSED` (the default) or `ENABLED` for creation. `REMOVED` remains in the
lifecycle enum vocabulary, but is not a valid creation status:
these tools refuse `INVALID_CREATION_STATUS` before account reads or writes.
RSA requires
3–15 headlines (30 counted characters each) and 2–4 descriptions (90 each);
display paths `path1`/`path2` are limited to 15 counted characters. The same
Unicode W/F counting rule applies to RSA, sitelinks, callouts and snippets.
Sitelink link text allows 25 counted characters; descriptions are optional but
must be paired, nonblank and at most 35 each. Callouts allow 25, and structured
snippets require 3–10 nonblank values of at most 25 each.
`update_campaign` adds geographic/language criteria without replacing existing
ones, and switching bidding strategy clears previous targets using leaf field
masks. Ad-group rotation supports `OPTIMIZE` and `ROTATE_FOREVER`.

Positive keyword creation requires an explicit `match_type`. Negative keywords
default to `EXACT`; a tool-level match type applies to strings, while a
`{text, match_type}` object can override it per keyword. Both positive and
negative keyword text must be nonblank, at most 80 Unicode codepoints and at
most 10 whitespace-separated words, including keywords created with a campaign.
These keyword limits count each codepoint once regardless of width.
Conversion actions
accept `ONE_PER_CLICK`/`MANY_PER_CLICK` counting and a 1–90 day click-through
lookback window (default 30).

Schedules require integer hours and quarter-hour minutes (0, 15, 30 or 45).
Start must precede end using the complete time, so 08:15–08:30 is valid;
24:00 is allowed only as an end. These validation failures return named errors
before account calls, and mutation refusals are recorded with secrets scrubbed.

`run_gaql` preserves the selected public GAQL field names in JSON, CSV and
table output, including fields such as `recommendation.type`. Its optional
`page_size` must be an integer from 1 to 10,000 and bounds the local response
page, capped by `ADS_MCP_ROW_LIMIT`; omission uses that configured limit.
Continue with `next_page_token` until it is absent. An omitted `customer_id`
uses the configured account; an explicitly empty or malformed ID is refused.
Account permission failures name the requested account and manager login.

Paginated reports, policy detail, history, Keyword Planner ideas and raw GAQL
retain a stable projected result on their first page. Continuations read that
snapshot without another provider request, even if the provider's ordering
changes. Tokens are opaque and bind to this server, account, tool, query,
filters, effective date window, format and effective response-page size. Reuse
the same arguments. Invalid, foreign, expired or evicted tokens refuse before
provider work; restart without a token to obtain a new result, which may differ.

Fixed release limits (not environment settings) are 10,000 projected rows and
16 MiB of compact UTF-8 JSON row bytes per snapshot. Completed retained
snapshots are limited to 16 snapshots and 64 MiB of aggregate row bytes per
server. Serialized construction can temporarily add a bounded 16 MiB projected
prefix alongside the completed store. Total process RSS, SDK buffers and
serialization allocations may be higher than these row-byte figures.
The oldest-created snapshot is evicted first to meet count or aggregate
capacity. Initial reads consume at most one lookahead row beyond the retained
prefix (at most 10,001 rows for an unfiltered larger source). Response-page
limits remain unchanged.

Tokens expire after 300 absolute seconds without renewal on access. Expired
stored rows are reclaimed on subsequent store access; there is no immediate
erasure timer or secure memory wiping guarantee. These mechanics do not set
a deployment's retention policy for logs, client responses or other copies.

Paginated requests wait asynchronously before worker dispatch, so queued reads
leave worker capacity available for fresh authenticated health inspection.
Snapshot construction remains serialized across paginated tools and accounts.
Cancelled waiters do not dispatch later; a provider read already running
finishes before the next queued read starts.

Every capacity-truncated page, including the last, includes
`possibly_truncated: true` and narrowing guidance. An oversized first row
produces a truncated empty result without continuation. Absence of a token
means the retained prefix is exhausted; check truncation metadata before
assuming completeness. Narrow the query, filters or window for more results.
Policy summaries also disclose bounded input. Authenticated health checks,
mutation safety rechecks and authoritative write baselines always read fresh
provider data and never use these snapshots.

`get_change_history` requires strict `YYYY-MM-DD` dates, with start on or
before end and both dates inclusive. The query starts at start-date midnight
and ends just before the next calendar day's midnight after the end date,
including every fractional second of the end date across month, year and
daylight-saving transitions. It verifies the requested account's time
zone and accepts exactly the 30 calendar dates from D-29 through D, where D
is today in that account. Older dates, future dates and longer intervals
return `CHANGE_HISTORY_RANGE_EXCEEDED` with corrective guidance. Missing or
invalid time zones return `ACCOUNT_TIME_ZONE_UNAVAILABLE`; UTC is never a
fallback. This conservative local calendar-day policy is distinct from the
provider's sub-day retention boundary and does not guarantee every event on
the earliest date is still available.

The history query retains `LIMIT 1000`. A full 1000-event result includes
`possibly_truncated: true` and guidance to narrow the date window on every
local page, including the last. Local `next_page_token` values only walk
these capped results; their exhaustion does not establish completeness.
Narrow the dates or resource type to retrieve additional events. More than
1000 events at the same timestamp may require a different query through
`run_gaql`, with narrower filters or a larger explicit limit. The upstream
maximum is `LIMIT 10000`, which is still a cap rather than a completeness
guarantee.

`get_keyword_forecasts` accepts both `keywords` and the
`keyword_texts` alias. If both are supplied, they must agree. Supply both dates
in strict `YYYY-MM-DD` form, or omit both for the next 30 complete days in the
requested account's time zone. The start must be after today and the end may
not exceed today's anniversary next year (February 29 clamps to February 28).
Malformed or globally impossible windows fail before any API request; other
windows are checked after reading the account time zone. Missing or invalid
account time zones return `ACCOUNT_TIME_ZONE_UNAVAILABLE`. Forecasting creates
no persistent Planner resources.

The v25 nonpersisting forecast API does not provide impressions:
`forecast.impressions` is always `null`. Missing clicks, cost and average CPC
estimates are also `null`, including when no forecast metrics are returned.
Explicit zero estimates remain zero, and clicks retain fractional values.
Available cost and average CPC are decimal money strings in the requested
account's currency, including formatted zero amounts when explicitly supplied.

Run `python scripts/check_capabilities.py` offline to inspect actual MCP
metadata against `tests/fixtures/capability_requirements.json`. The authored
contract groups all 52 public operations by operator workflow and requires
essential parameters, declared values and argument requiredness. It is a
structural check; executable tests prove nested input validation and behavior.
Extra supported operations are allowed. Both `bypass_require_dry_run` and
`confirmed_twice` are always forbidden across every tool, even if a custom
contract omits those names. Requirements cannot change server safety rules.

Use `--requirements PATH` for a custom contract. To verify a new requirement,
copy the fixture and change one tool name to `future_operation`, then run the
checker on that copy: it reports `future_operation` in `missing_tools` and
exits 1. Satisfied contracts exit 0. Invalid input or unsupported metadata
exits 2 with a bounded named diagnostic and no success output. Other sorted
differences are `missing_parameters`, `missing_values`, `missing_required`
and `forbidden_parameters`. See [contract semantics](docs/migration.md).

## Error codes an agent should understand

Tool-level application failures use `{"error": {"code": ..., "message": ...}}`.
MCP protocol validation may reject malformed requests before a tool runs.
The main application error codes are:

| Code | Meaning |
|---|---|
| `AUTH_TOKEN_REVOKED` | the refresh token is dead — run `ads-mcp-generate-token` |
| `ADS_CLOUD_PROJECT_NOT_APPROVED` | the API explicitly refused production access for the OAuth Cloud project; review/apply for access in Google Cloud Console's Google Ads API Overview |
| `BUDGET_CAP_EXCEEDED` / `BID_CAP_EXCEEDED` | over a configured cap; not retryable without changing the cap |
| `GUARDRAIL_CAP_UNSET` | the budget change, bid increase, or first bid needs the named cap configured; verified bid cuts remain allowed |
| `FIRST_BID_CAP_EXCEEDED` | a bid without an applicable prior baseline exceeds `ADS_MCP_MAX_FIRST_BID` |
| `BID_BASELINE_UNVERIFIED` | an existing entity or keyword parent could not be verified; restore account access or correct the resource IDs before staging again |
| `SPEND_IMPACT_UNBOUNDED` | a recommendation whose spend effect this server cannot quantify; apply it deliberately in the Ads UI |
| `PLAN_CONSUMED` | this request cannot execute the plan again; an earlier attempt may already have applied it, so check its outcome before re-staging |
| `PLAN_EXPIRED` / `PLAN_EVICTED` / `PLAN_NOT_FOUND` | the plan cannot execute now; earlier changes may have landed. Check prior results, the account and audit log before staging a fresh plan |
| `DRY_RUN_REQUIRED` | preview the plan with `dry_run=true` first |
| `IRREVERSIBLE_CONFIRMATION_REQUIRED` | review the irreversible plan, then apply with `dry_run=false` and `confirm_irreversible=true`; this refusal has not consumed the plan |
| `PLAN_CUSTOMER_MISMATCH` | the call named an account this server is not configured to write to |
| `MUTATION_TRANSPORT_FAILED` | the write was sent and the response was lost. **It is not retried and MAY have landed** — check the account and the audit log before re-planning |
| `AUDIT_WRITE_FAILED` | the audit log could not be written; the mutation was refused, or (if the message says so) it landed and could not be recorded |

`ADS_CLOUD_PROJECT_NOT_APPROVED` identifies the explicit v25
`CLOUD_PROJECT_NOT_APPROVED_FOR_PRODUCTION` authorization error.
`ACTION_NOT_PERMITTED` alone does not establish missing project approval.
Other authorization errors retain their own meaning. Project refusals do not
trigger retries; an attempted mutation still consumes its plan, and lost write
responses retain the existing uncertainty behavior.

The health response's compatibility field `config.developer_token` reports only
whether a nonblank legacy value is present or absent, never whether it grants
access. The value is ignored either way. Health still requires an authenticated
read before reporting success.

`customer_id` is accepted on every write tool, but only to be **refused** if
it names an account other than `GOOGLE_ADS_CUSTOMER_ID`. Writes always land on
the configured account. Account-data read tools can query another accessible
account using `customer_id`; `health_check` and `list_accounts` describe the
configured login.

## Claude Code / MCP registration

Replace the executable placeholder with the absolute path from your install.
Use a separate server name while comparing with an existing MCP server:

```json
{
  "mcpServers": {
    "ads-mcp-readonly": {
      "type": "stdio",
      "command": "/absolute/path/to/ads-mcp/.venv/bin/ads-mcp",
      "env": {
        "GOOGLE_ADS_CUSTOMER_ID": "123-456-7890",
        "GOOGLE_ADS_LOGIN_CUSTOMER_ID": "111-222-3333",
        "GOOGLE_ADS_CREDENTIALS_PATH": "~/.ads-mcp/oauth_client.json",
        "GOOGLE_ADS_TOKEN_PATH": "~/.ads-mcp/token.json"
      }
    }
  }
}
```

## Rollout: read-only first, then the write path

Stdio clients must keep the input pipe open until the response arrives.
Sending requests and immediately closing stdin (EOF) is unsupported: the MCP
SDK may cancel in-flight responses when the input stream closes. The stdio
boot regression holds the pipe open through initialization and `tools/list`.

Start with read-only parallel operation: register this server alongside your
current one without enabling mutations. All 21 read tools are available.
Compare representative outputs and confirm that your API access permits the
services you need. `scripts/parity.py` replays all read fixtures offline;
`--live` performs a read sweep using the supplied fixture arguments. For a live
sweep, provide fixtures with your real resource IDs and suitable dates through
`--fixtures`. A successful sweep is separate from comparing another server's
actual output.

Write support is an explicit opt-in and remains experimental. Before enabling
it, review the cap scope above and the plans your client produces. Configure
`ADS_MCP_AUDIT_LOG`, the needed daily-budget and bid caps (including
`ADS_MCP_MAX_FIRST_BID` for first bids), and then `ADS_MCP_READ_ONLY=false`.
With the default preview requirement, the flow is plan → dry-run preview →
`confirm_and_apply` with `dry_run=false`. Irreversible plans additionally need
`confirm_irreversible=true`. That acknowledgement does not bypass preview and
a refusal leaves the plan available for a properly acknowledged retry.

Within each server context, applications run one at a time from their fresh
account checks through all dependent writes and audit handling. A waiting plan
checks the account state left by the previous application. Previews remain
available during this interval. Other server contexts and external account
writers operate independently; this coordination does not make a sequence of
provider requests atomic. Each plan still permits only one execution attempt,
and writes are never retried automatically.

## Development checks

From the checkout, using its virtual environment:

```bash
.venv/bin/pytest -q
.venv/bin/python scripts/parity.py
.venv/bin/python scripts/check_capabilities.py
.venv/bin/python scripts/verify_apply_surface.py
.venv/bin/python scripts/gen_tools_md.py
```

The source
archive includes fixtures, test helpers, documentation and these scripts. The
wheel provides the runtime and all three console entry points, including
`ads-mcp-export-source`. See [release checks and source export](docs/releasing.md),
[migration differences](docs/migration.md), [security](SECURITY.md) and
[contribution guidance](CONTRIBUTING.md). CI configuration covers six current
Linux/macOS interpreter cells and Linux 3.12 minimum dependencies; unexecuted
hosted/Linux jobs remain missing release evidence, not passing results.

## Tool catalog

See [docs/tools.md](docs/tools.md) — generated from the registry
(`python scripts/gen_tools_md.py`), never hand-edited.

## License

[MIT](LICENSE). Dependencies retain their own licenses; review applicable
obligations when redistributing them or adding bundled material.
