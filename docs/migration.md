# Integration and migration

ads-mcp 0.1.0 registers 27 read tools by default and 67 operations when
experimental writes are enabled. Integrate against the actual
[tool catalog](tools.md) and this project's expected-output fixtures. Review
representative workflows and adapt consumers before changing a deployment.

## Evaluate an installation

1. Configure your own project, OAuth client and authorized login using the
   [setup guide](../README.md#oauth-setup-one-time). Existing credentials are
   reusable only if they belong to your organization and are authorized for
   this installation. No credentials are supplied by the package.
2. Register a separate read-only server. Verify `health_check`, account identity
   and account time zone before comparing reports.
3. Compare campaigns, ads, keywords, search terms and geography over the same
   explicit completed dates. Walk every continuation page, inspect truncation
   warnings, and normalize documented field names and lifecycle filters before
   comparing. Matching aggregate clicks/impressions is useful but does not
   establish row-by-row equality or equivalent costs/conversions.
4. Exercise your actual consumers with zero results, multiple pages, missing
   windows, unsupported fields and provider refusals. Keep customer payloads
   local; retain sanitized outcomes rather than raw report rows in issues.
5. Decide whether workflows need capabilities outside the documented catalog
   before switching. Shared negative-list management, demographic changes and
   in-place RSA/keyword final-URL edits are not provided by this release.
6. Keep writes disabled until account authority, host approval behavior, audit
   storage and a controlled write-acceptance plan have been reviewed. Read-only
   success does not authorize a write or demonstrate its provider acceptance.

Do not expose an existing organization's credentials to independent users to
avoid their setup. Internal organizational use and third-party distribution
have different authentication and disclosure responsibilities.

| Boundary | Consumer migration needed |
| --- | --- |
| Output schemas | Read account/report fields and nesting from recorded examples. Geography returns `locations`. Policy defaults to a summary. Raw GAQL preserves every selected field in JSON, CSV and table output, including zero and null values. |
| Dates | Metric reports require a valid explicit window or `last_n_days`; omitting both refuses `INVALID_WINDOW`. Relative windows use the requested account's verified time zone. Change history accepts the 30 calendar dates from D-29 through today D, including the complete end date; the query retains `LIMIT 1000` and warns when full. |
| Relative-day precedence | When `last_n_days` is supplied it takes precedence over explicit start/end dates, even malformed explicit dates. Supply only one window form to avoid surprises. |
| Queries and pagination | Campaign/ad reports can include removed entities. Negative-keyword output omits other negative criteria. Follow opaque continuation tokens using identical arguments and inspect truncation warnings on every page. Token exhaustion means the retained prefix ended, not that Google has no more rows. |
| Forecast meaning | Keyword forecasts call Google's nonpersisting forecast service and require Planner access. They are estimates for a future window, not historical performance reports. No persistent Planner resources are created. |
| Forecast values | Both `keywords` and `keyword_texts` are accepted and must agree if both supplied. Default dates are the next 30 complete account-local days; explicit windows must be future and within the one-year horizon. Impressions are `null` because the API supplies none; other absent estimates are `null`, explicit zeros stay zero, clicks may be fractional, and money uses account-currency decimal strings. |
| Creation inputs | Campaign/ad-group/RSA creation defaults to paused. `REMOVED` is a declared lifecycle value but is refused for creation. PMax needs explicit image assets, creative and political-advertising declarations. Review geo and bidding defaults; only documented creation graphs are supported. |
| Input strictness | Unknown top-level and nested mutation keys refuse before staging. Use `cpc_bid_micros`, not an invented dollar-CPC key; keyword `status`, schedule `bid_modifier` and sitelink start dates are not supported inputs. |
| Confirmation | Mutation tools return a plan. Only `confirm_and_apply` executes it, with preview required by default. Irreversible changes also need plan-bound `confirm_irreversible`; that acknowledgement does not bypass preview. No per-call preview bypass or generic confirmation flag is accepted. |

Budget and CPC guardrails are **per operation**. They do not cap aggregate
account spend, sum separate budgets, constrain CPA/ROAS targets or guarantee
that enabling an existing entity cannot increase spend. A verified reduction
may exceed a configured daily-budget ceiling; an unset budget ceiling refuses
budget changes. Bid increases use freshly verified account baselines. First
bids require an absolute configured ceiling. Experimental writes always target
the configured account, use expiring single-use plans, and never retry an
uncertain provider write automatically. A client calling confirmation is not
proof of human authorization. See [operator responsibilities](operator-guide.md).

## Read-only rollout

Stdio clients must keep the input pipe open until the response arrives.
Sending requests and immediately closing stdin (EOF) is unsupported: the MCP
SDK may cancel in-flight responses when the input stream closes. The stdio
boot regression holds the pipe open through initialization and `tools/list`.

Start with read-only parallel operation: register this server alongside your
current one without enabling mutations. All 27 read tools are available.
Compare representative outputs and confirm that your API access permits the
services you need. `scripts/parity.py --all-fixtures` replays all 27 read fixtures offline;
`--live` performs a read sweep using the supplied fixture arguments. For a live
sweep, provide fixtures with your real resource IDs and suitable dates through
`--fixtures`. A successful sweep is separate from comparing another server's
actual output.

The default fixture sweep retains the original 21 fixtures. `--all-fixtures`
combines them with four PMax fixtures and the responsive search ad URL fixture.
An explicit `--fixtures` directory may contain the complete original
21-read set, the complete 25-read PMax set or all 27 current reads; partial
extensions are refused.
Original fixtures retain their output contracts.

## Performance Max workflows

The [PMax guide](pmax.md) covers four added reads and seven added staging tools,
plus asset-group pause/enable through the existing lifecycle tools. Consumers
can inspect groups, signals, existing audiences and URL settings in read-only
mode. Writes use the same account binding and preview/apply flow. Follow the
guide's returned-ID rules, audience scope prerequisites and supported flat
Item-ID tree shapes; no audience creation or nested-tree editing is implied.

Review delivery effects as well as budgets: signal, URL and product changes
can alter eligible traffic under an existing budget. Removals and full product
tree replacement require irreversible acknowledgement. Changed relevant state
refuses `STALE_PLAN`; inspect again and stage a fresh plan. Local theme/input
limits and genuine SDK request tests do not establish live Google acceptance.

## Workflow requirements

`python scripts/check_capabilities.py` inspects actual MCP metadata offline
against the independently authored `tests/fixtures/capability_requirements.json`.
The default covers every public operation grouped by purpose, with only the
structural obligations promised for those workflows. It does not demand every
optional parameter. Nested dictionary validation and provider behavior remain
covered by executable scenarios; the fixture must not be generated from the
runtime schema.

Custom contracts use `--requirements PATH` and the same exact version 1 shape:

```json
{
  "version": 1,
  "capabilities": [{
    "id": "account_access",
    "purpose": "Read the authenticated account identity before reporting.",
    "tools": [{
      "name": "get_account_info",
      "parameters": ["customer_id"],
      "required": [],
      "values": {}
    }]
  }],
  "forbidden_parameters": ["bypass_require_dry_run", "confirmed_twice"]
}
```

The root, capability and tool objects accept exactly the illustrated keys.
Capabilities, tools and forbidden names must be nonempty lists. Parameters,
required arguments and values may be empty. IDs, tool names and property names
use ASCII letters, digits and underscores, starting with a letter or underscore.
IDs and tool names are globally unique; keys, paths and values must not repeat.
Purposes are nonblank original text. Required paths and value-domain keys must
also be listed under parameters. Domains contain one or more finite JSON
scalars, with booleans distinct from numbers and equal numbers treated alike.

Paths use dotted properties and an explicit separate array-item segment:
`keywords.[].match_type`. A path starts and ends with a property. It exists
when fully declared in at least one alternative. Requiredness needs every named
ancestor and the leaf to be required in every alternative; array-item rules
apply to every item if present and do not require a nonempty array. Local JSON
pointer references, homogeneous array items, object properties, `allOf`,
`anyOf` and `oneOf` are supported. References combine with sibling constraints.
`allOf` merges object declarations and required lists.

Declared values come from scalar `enum`, `const` or `type: null`. Alternatives
union finite domains only when every relevant branch is bounded; conjunctions
intersect finite constraints. Defaults, descriptions and bare string types do
not prove enumerated support. Missing tools suppress descendant differences;
missing paths suppress value and requiredness differences. Extra operations
are allowed, but every tool is inspected for forbidden property names. The two
built-in forbidden names always apply, even when omitted from a custom list.
Inspection follows reachable schema-valued `additionalProperties`, including
local references, alternatives, nested maps and array items. Boolean
`additionalProperties` values remain supported. Unreferenced `$defs` and
`definitions` do not declare reachable input parameters, but their schema
shapes and unsupported constructs are still validated.

The result contains sorted unique `missing_tools`, `missing_parameters`,
`missing_values`, `missing_required` and `forbidden_parameters` lists. Paths
are reported as `tool.path`; values as `tool.path=<compact JSON scalar>`.
Forbidden declarations in map values use a separate `*` diagnostic segment,
such as `unexpected.mapping.*.confirmed_twice`. This notation is output only:
requirements reject wildcard segments, and map schemas do not supply named-path
presence, requiredness or finite-value evidence.
Exit 0 means satisfied structure; exit 1 means unmet requirements; exit 2 means
`REQUIREMENTS_INPUT_ERROR` or `METADATA_ERROR`. Invalid input is refused before
server initialization. Unsupported metadata, including cyclic/external refs,
boolean schemas, tuple arrays, pattern/conditional/dependent schemas and `not`,
fails without a success result. This bounded inspection is not a complete JSON
Schema validator and does not establish general API compatibility.

To see a truthful negative result, copy the default requirements, rename one
required operation to `future_operation`, then run the checker with that copy.
It reports the missing operation and exits 1. Editing requirements cannot
enable server bypasses or relax runtime safety controls.

`scripts/parity.py` separately replays this project's recorded read fixtures.
Its optional live mode is an operator-run read sweep using API quota; it does
not compare another package or prove equivalent outputs. Local checks do not
authorize writes, establish Google acceptance or approve a deployment.
