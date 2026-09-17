# Error reference

`health_check` makes an authenticated API read and distinguishes authentication,
transport and configuration failures.

Tool-level application failures use `{"error": {"code": ..., "message": ...}}`.
MCP protocol validation may reject malformed requests before a tool runs.
Malformed JSON or invalid Unicode receives a generic protocol refusal without
echoing the input. Correct the request; the stdio connection remains usable.
Method-bearing requests require a string or integer ID. An explicitly present
object, array, null, boolean or fractional-number ID receives exactly one
`-32600` invalid-request error with a null ID, before any tool or provider access.
IDs are never coerced; JSON `1.0` is not an integer ID. Empty strings, Unicode
strings, zero, negative and large integers retain their exact correlation.
Notifications omit the ID member entirely and receive no response. A null ID
on an SDK error-response envelope is distinct from a null request ID.

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

A refused preview does not consume the plan or grant permission to apply it.
Restore secure audit storage and obtain a successful preview before applying.
A failure at the start of application still consumes the plan; inspect its
outcome before staging another change.

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
