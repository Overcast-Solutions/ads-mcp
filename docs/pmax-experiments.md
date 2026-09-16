# PMax final URL expansion experiments

Compare original settings with text customization and final URL expansion on
the **same Performance Max campaign**, with a fixed **50/50** control/treatment
traffic split. This workflow supports
`PMAX_TEXT_CUSTOMIZATION_FINAL_URL_EXPANSION`. It does not clone campaigns,
change budgets, schedule through a separate RPC, graduate experiments or
choose a winner automatically. Google's
[intra-campaign API guide](https://developers.google.com/google-ads/api/docs/experiments/intra-campaign)
describes the shared-campaign setup and traffic split.

Use your **own credentials**, Cloud project, OAuth client and authorized Ads
login; follow [authentication](authentication.md). The package supplies no
shared credentials. Start read-only with `health_check` and `get_account_info`.
The four experiment reads are available among all 34 reads. Opting into
experimental writes exposes 83 total operations; review [write setup](writes.md)
and configure private audit storage before using the three staging tools.
Account authority and host approval remain the operator's responsibility.

## Inspect before creating

In your MCP client, call the named tool with the JSON arguments shown below.
IDs and dates are examples: replace them with your own returned IDs and dates
that satisfy the eligibility rules. These examples are not records of live
Google Ads acceptance.

Call `list_pmax_url_experiments`:

```json
{}
```

Then `get_pmax_url_experiment` for an existing experiment:

```json
{"experiment_id":"301"}
```

Inspection returns the actual experiment status, separate `promote_status`,
latest operation name, two arms, campaign automation settings, account currency
and timezone. It verifies exactly one control and one treatment arm, each with
50 percent traffic and the same PMax campaign. An observed lifecycle status
alone does not prove that traffic is running. Use `get_pmax_url_settings` with
`{"campaign_id":"703"}` to inspect candidate URL settings and exclusions.

Reads may include `customer_id` for another accessible account. Writes are
bound to the configured account. Omit `customer_id` or use JSON `null` for the
configured default; the string `"null"` is invalid. Account IDs accept ten
digits or the documented `123-456-7890` form. Resource IDs must be positive
canonical integer strings, at most `9223372036854775807`; leading zeroes,
numbers, booleans and full resource names are not substitutes for an ID.

## Eligibility and local limits

Creation requires an enabled PMax campaign with final URL expansion explicitly
`OPTED_OUT`. Text customization may already be enabled. The campaign must not
belong to any experiment that has not been removed, including other experiment
types. This conservative check also blocks an ended experiment that still
references the campaign: inspect and resolve that experiment in Google Ads
before creating another here. Names must be unique among nonremoved experiments
under NFC/case-insensitive comparison.

| Local rule | Required input or complete-state bound |
| --- | --- |
| Name | 1–255 UTF-8 bytes, NFC-normalized; no surrounding whitespace, control characters or invalid Unicode; literal `null` refuses |
| Dates | Explicit real calendar dates in `YYYY-MM-DD` form; end on or after start; both fit the campaign's configured dates |
| Start horizon | Today through 365 days ahead, using the verified customer timezone |
| Duration and reporting window | Each permits 1–366 inclusive calendar days |
| Experiment catalog | At most 100 matching experiments, with complete arms and campaign state |
| Creation collision inventory | At most 1000 experiments across types and 2000 arms |
| State size | At most 16 MiB of projected state; oversized or incomplete scans refuse |
| Operation name | Exact returned opaque string, 1–2048 characters, without whitespace or control characters |
| Failed-operation details | At most 10 pages, 1000 statuses and 16 MiB; incomplete collection is labelled |

These are this server's conservative support limits, not Google's complete
eligibility or transition rules. Scans use bounded lookahead and refuse
missing, duplicate, foreign, malformed or unsupported state. Provider-only
restrictions still apply. Google's
[PMax experiment requirements](https://support.google.com/google-ads/answer/15473042?hl=en)
and API validation remain authoritative.

## Create, preview and confirm

Call `create_pmax_url_experiment` after replacing the example dates with an
eligible window in your account's timezone:

```json
{"campaign_id":"703","name":"Autumn landing trial","date_start":"2026-10-01","date_end":"2026-10-30"}
```

Staging performs local checks and a **Google Ads validate-only request** before
returning a plan. Provider validation is an authenticated API request, but does
not apply the change or guarantee future serving or policy acceptance. A
validation refusal returns no plan and sends no real mutation.

Review the returned plan's original automation settings, dates, both arms and
treatment changes. Call `confirm_and_apply` with its exact returned plan ID:

```json
{"plan_id":"<returned-plan-id>","dry_run":true}
```

This is the separate **local preview** of the stored plan. It is not another
Google validate-only call or a simulation of delivery. After approval, call:

```json
{"plan_id":"<returned-plan-id>","dry_run":false}
```

Plans expire and are single-use. Before submission the server rechecks complete
relevant state and account-local time, including a start date that could become
past at midnight. Drift or unverifiable eligibility refuses `STALE_PLAN` before
the real mutation; inspect and stage anew. The recheck cannot prevent an
external actor from changing state after the final read.

Creation sends the experiment, two arms and campaign automation update together
in one atomic request with partial failure disabled. Treatment enables
`TEXT_ASSET_AUTOMATION` and `FINAL_URL_EXPANSION_TEXT_ASSET_AUTOMATION`; unrelated
automation entries, campaign exclusions, budgets, bids and other campaign
configuration remain unchanged. Control retains the original experiment
settings. Delivery and spend can change within the existing budget.

A valid receipt returns `experiment_id` and `resource_name`. Creation reports
`applied: true` with `verification: "verified"` only after readback matches the
requested configuration, arms and complete automation settings. Its observed
status is returned as observed; no default running status is promised. Inspect
the returned ID with `get_pmax_url_experiment` before relying on its state.

Page feeds do not restrict expanded landing pages to URLs in the feed. Review
your existing exclusions and destination policy before enabling expansion;
these experiment tools do not add, remove or rewrite exclusions. See Google's
[PMax experiment guidance](https://support.google.com/google-ads/answer/15473042?hl=en)
and the [PMax URL controls guide](pmax.md).

## Compare the two arms

Call `get_pmax_url_experiment_results` with your returned experiment ID and an
explicit reporting window:

```json
{"experiment_id":"301","date_start":"2026-10-01","date_end":"2026-10-30"}
```

Results come directly from experiment reporting, rather than aggregate campaign
metrics. `control` and `treatment` each expose clicks, impressions, `cost_micros`,
conversions and `conversions_value`, with verified account currency and
`time_zone`. Missing optional metrics remain `null`; measured zero stays `0`.
No rows yields `no_data: true` with null metrics, not invented zero activity.
Duplicate aggregate rows or nonfinite values refuse verification.

Keep cost as exact integer micros: one currency unit is 1,000,000 micros. Use
an integer-preserving JSON parser and decimal arithmetic; large values may
exceed JavaScript's safe integer range. Conversion counts and values may be
fractional or adjusted negative values.

| Statistics group | Meaning |
| --- | --- |
| `statistics.clicks` | `unit: "relative_change"`; click point estimate and margin of error are relative effects; for example `0.2` represents +20% |
| `statistics.conversions` | `unit: "absolute_treatment_minus_control"`; conversion point estimate and margin of error are absolute conversion changes, not percentages |
| Either group's p-value | A separate probability from 0 to 1; may be null, as may either estimate or margin |

The server preserves provider statistics without inferring confidence when
fields are absent or declaring a winner. Review data maturity and the provider's
[experiment reporting definitions](https://developers.google.com/google-ads/api/docs/experiments/reporting)
before deciding whether to keep treatment settings.

## End or promote

Choose the appropriate action after inspecting the current experiment. End and
promotion are alternatives; the examples do not instruct you to promote an
experiment you have just ended.

Both staging tools require observed experiment status `ENABLED`,
`promote_status: "NOT_STARTED"`, and an account-local date from the experiment's
start through its end, inclusive. Other promotion states, including `FAILED`,
refuse a new lifecycle action. Inspect and resolve them in Google Ads. This
local subset is not a universal provider state-transition table.

To stop the test, call `end_pmax_url_experiment`:

```json
{"experiment_id":"301"}
```

To make treatment settings permanent instead, call `promote_pmax_url_experiment`:

```json
{"experiment_id":"301"}
```

Each staging call performs its dedicated provider validate-only request before
returning a plan. Preview and apply that plan with `confirm_and_apply`:

```json
{"plan_id":"<returned-lifecycle-plan-id>","dry_run":true}
```

```json
{"plan_id":"<returned-lifecycle-plan-id>","dry_run":false,"confirm_irreversible":true}
```

The irreversible acknowledgement is required because this workflow cannot
resume an ended experiment or undo promotion. It does not bypass the preview
requirement. Application rechecks complete state, then sends the action once
with provider retries disabled.

End uses Google's dedicated operation to end the experiment immediately. Its
receipt reports `submitted: true`, `applied: false` and separately observed
experiment state when available. It does not promise `HALTED`, manually revert
campaign settings or patch the experiment status. Inspect the observed dates
and status; see the
[EndExperiment API reference](https://developers.google.com/google-ads/api/reference/rpc/v25/ExperimentService/EndExperiment).

## Observe promotion, including after a restart

Save the `experiment_id` and exact `operation_name` returned by promotion.
Call `get_pmax_url_experiment_operation` using those values:

```json
{"experiment_id":"301","operation_name":"<exact-returned-operation-name>"}
```

This read works after a server restart without the original plan. It verifies
the selected account, supported experiment and arms, latest operation name,
returned name and typed metadata. The opaque handle is never treated as a URL
or parsed to guess an account. Polling uses the authenticated ExperimentService
transport, a 60-second timeout per request and the existing bounded read-retry
policy. Call again later while pending; observation never resubmits promotion.

| Outcome | What it establishes |
| --- | --- |
| `submitted: true` | The action was sent; an uncertain response can still carry this flag. It alone proves neither acceptance nor application. |
| `state: "pending"`, `completed: false`, `applied: false` | The bound promotion operation is still pending. Audit records submission, not successful application. |
| `state: "completed"`, `completed: true` | The operation returned a verified typed completion response. Application still needs a separate state read. |
| `applied: true` | Completion and readback verify `COMPLETED` or `COMPLETED_WITH_WARNING` promotion status and both treatment settings enabled. The latter retains `warnings: true`. |
| `state: "failed"` | The operation has a terminal error, even if error details are empty or unavailable. `async_errors.complete` says whether bounded redacted detail collection finished. |

Promotion may complete immediately in its initial response. In either case,
completed and verified application remain distinct. Other observed promotion
states do not establish application. Missing or mismatched handles refuse;
unknown enum numbers are not guessed into success. An observation timeout
means progress is unknown, not that the experiment failed.

## Recover without repeating an uncertain action

- **Lost or unverified creation response:** the mutation may have landed.
  Keep `submitted`, `verification`, `observation_error` and `recovery`. Inspect
  the catalog and campaign before considering another creation. If a new
  experiment identity is known, inspect that identity; do not repeat creation.
- **Creation readback failure or mismatch:** accepted identity remains visible;
  `applied` is false and verification is unknown or failed. Inspect configuration
  and campaign settings in Google Ads and through the read tools.
- **Lost lifecycle response or unverified promotion receipt:** retain the
  experiment ID and inspect its latest operation before another action. An
  uncertain response is never a safe invitation to retry the write.
- **Pending, completed-but-unverified or failed promotion:** use the observation
  read and inspect experiment and campaign settings. A failed operation stays
  failed when detail collection fails, loops or reaches its bound; partial
  detail is explicit and provider messages/payloads are redacted.
- **Audit storage failure:** a failure before submission blocks the action.
  After submission, an `audit_warning` retains the accepted-action context.
  Restore audit storage and reconcile the actual account state; do not repeat
  a mutation to replace a missing audit record. A consumed plan cannot be used
  again, including after an uncertain response.

Provider eligibility, API access and advertising policy remain authoritative.
Local validation and offline installed-console checks with genuine SDK messages
do not demonstrate live Ads acceptance or blanket compliance. This guide does
not grant permission for account changes. Follow the
[operator responsibilities](operator-guide.md) and preserve account data and
audit records in operator-managed storage.
