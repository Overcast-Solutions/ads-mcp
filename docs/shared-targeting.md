# Shared negative lists and demographic targeting

Inspect reusable negative-keyword lists and explicit demographic criteria, then
review a plan before changing them. Read-only is the default. Experimental write
mode adds six staging tools to these three reads; the complete server has 84
operations, including 34 reads. See the separate [PMax experiment guide](pmax-experiments.md)
for URL expansion experiments.

## Set up your account

Use your own credentials, OAuth client and authorized Google Ads account as
described in [authentication](authentication.md) and the
[installation guide](../README.md#oauth-setup-one-time). The package supplies no
shared credentials. Start with `ADS_MCP_READ_ONLY=true`, call `health_check` and
`get_account_info`, and confirm which account you are inspecting.

Reads accept an optional `customer_id` for another accessible account. Writes
are restricted to the configured `GOOGLE_ADS_CUSTOMER_ID`; manager login does
not enable cross-account writes or manager-shared lists. Before opting into
`ADS_MCP_READ_ONLY=false`, configure private audit storage and review the
[write flow](writes.md#plan-preview-apply) and [operator guide](operator-guide.md).
Targeting changes can alter traffic and spend within existing budgets.

Examples below use synthetic IDs and names for a fictional site,
`https://example.invalid/`. Replace IDs with values returned from your account;
never guess a resource ID or reuse a plan ID from another server session.
Tool names identify MCP calls; JSON blocks contain their arguments.

## Shared list scope and inspection

Only active, same-account `NEGATIVE_KEYWORDS` lists are supported. Every linked
campaign must be an enabled or paused standard Search or Shopping campaign.
Display, Performance Max, specialized campaign subtypes, account-level lists
and manager-owned sharing are outside this workflow. An unsupported existing
link prevents list editing rather than disappearing from its impact preview.
There are no list rename or delete tools.

Call `list_shared_negative_keyword_lists` with `{}` to get list IDs, names and
counts. Call `get_shared_negative_keyword_list` with a returned ID:

```json
{"shared_set_id": "401"}
```

The detail contains the complete membership and all linked campaigns, including
their IDs, names, status and channel. A membership change affects **all linked
campaigns**, not just the campaign that prompted the change. Google's
[shared-set model](https://developers.google.com/google-ads/api/docs/targeting/shared-sets)
uses a list, its members and campaign associations as separate resources.
The negative-keyword container establishes the workflow's exclusion semantics.
The observed member `negative` flag is retained as metadata; a default/false
flag in this container does not make the member a positive keyword.

## Create and maintain a list

Each staging call below returns its own plan. Complete the
[preview and apply steps](#preview-apply-and-re-read) for that plan before
continuing to a dependent call. Creation, membership and association changes
are separate operations, with no transaction spanning the whole sequence.

1. Stage an empty list with `create_shared_negative_keyword_list`:

   ```json
   {"name": "Seasonal exclusions"}
   ```

   After applying, re-read the catalog and use the newly returned `shared_set_id`.
   The remaining examples assume it is `401`.
2. Stage membership with `add_shared_negative_keywords`:

   ```json
   {"shared_set_id": "401", "keywords": [{"text": "sample clearance", "match_type": "PHRASE"}]}
   ```

   Text and match type are separate inputs. Use `BROAD`, `PHRASE` or `EXACT`;
   quotes or brackets in text do not select a match type. Apply, then inspect
   the list to obtain the member's `criterion_id`.
3. Stage associations with `attach_shared_negative_keyword_list`:

   ```json
   {"shared_set_id": "401", "campaign_ids": ["701", "702"]}
   ```

   Review all affected campaigns, apply, then re-read the detail to verify links.
4. To remove the inspected member, call `remove_shared_negative_keywords`:

   ```json
   {"shared_set_id": "401", "criterion_ids": ["601"]}
   ```

   Use the actual member ID returned by inspection. Preview, acknowledge the
   removal, apply and re-read; exclusions change across all linked campaigns.
5. To remove an association, call `detach_shared_negative_keyword_list`:

   ```json
   {"shared_set_id": "401", "campaign_ids": ["701"]}
   ```

   Preview and acknowledge before applying. Detachment retains the list and
   its members. A surviving campaign and list can later be reattached using
   the same composite association identity; detachment can still change serving.

Names require 1–255 UTF-8 bytes in original NFC form. Names and keyword text
must be nonblank with no edge whitespace, control characters or surrogates.
Keyword text is limited locally to 80 codepoints and 10 words. Original text
is preserved; NFC/casefold comparison detects duplicate names and text/match
pairs without rewriting them. IDs must be canonical positive decimal strings,
without leading zeros, whitespace or full resource-name prefixes. Supply actual
JSON arrays, not strings containing JSON. Each shared mutation batch accepts
1–100 distinct entries. Duplicate additions, missing removals and already
satisfied link requests refuse instead of staging a no-op.

## Demographic inspection and changes

Call `get_demographic_targeting` with `{"ad_group_id": "801"}`. Supported parent
types are standard Search and standard Display campaigns and ad groups.
Inspection reports explicit criteria, their status and direct settings,
campaign exclusions, supported category values, targeting restrictions and
Display optimized-targeting settings.

| Dimension | Search | Display | Example category |
| --- | --- | --- | --- |
| `AGE_RANGE` | Yes | Yes | `AGE_RANGE_25_34` |
| `GENDER` | Yes | Yes | `FEMALE` |
| `INCOME_RANGE` | Subject to country availability | Subject to country availability | `INCOME_RANGE_0_50` |
| `PARENTAL_STATUS` | No | Yes | `PARENT` |

Use the exact category names returned by inspection. Detailed demographic
audience segments and demographic bid changes are outside this interface.
Google documents [demographic targeting](https://support.google.com/google-ads/answer/2580383)
and [its setup and availability](https://support.google.com/google-ads/answer/2580282).

An absent or removed criterion means default/unconfigured at this level; it
does not establish an exclusion. A positive row, including its enabled or
paused status, does not by itself establish effective eligibility. Campaign
exclusions, targeting restrictions, optimized targeting, geography and policy
also matter. The inspection response is configuration evidence, not a prediction
of who will see an ad.

For a Search ad group, stage an exclusion with `update_demographic_targeting`:

```json
{"ad_group_id": "801", "changes": [{"dimension": "AGE_RANGE", "value": "AGE_RANGE_25_34", "action": "EXCLUDE"}]}
```

For a Display ad group, an inclusion uses the same tool:

```json
{"ad_group_id": "804", "changes": [{"dimension": "PARENTAL_STATUS", "value": "PARENT", "action": "INCLUDE"}]}
```

Supply 1–20 distinct category changes, each with exactly `dimension`, `value`
and `action`. Actions are `INCLUDE` and `EXCLUDE`. Inspect the plan's actual
operations before preview and application, then re-read the same ad group.

Inclusion creates an enabled positive criterion when none exists, or enables
a paused positive criterion with a status-only update. A polarity switch must
remove the old criterion and recreate its replacement because the provider's
[criterion polarity is immutable](https://developers.google.com/google-ads/api/reference/rpc/v25/AdGroupCriterion).
The replacement gets a new criterion identity and requires
`confirm_irreversible=true`. Exclusion creates a negative criterion or replaces
a positive one. A wholly unchanged result refuses as a no-op.

Replacement refuses if it would lose direct customization: bid modifier, CPC,
CPM, CPV or percent-CPC bid; final/mobile URLs; tracking template or suffix;
URL custom parameters; or labels. Present direct numeric values count even
when zero or a neutral modifier of one. Output-only effective bids are not
direct customization. A positive status-only update preserves identity and all
these settings. The tools preserve campaign exclusions, parent restrictions
and optimized targeting; an inclusion conflicting with a verified campaign
exclusion refuses and requires a separate review of that parent setting.

For each changed dimension, a local conservative check combines the whole batch
with existing active ad-group and campaign exclusions. It refuses when no
category, or only the unknown/undetermined category, would remain unexcluded.
At least one known category must remain. This supported-subset check does not
derive eligibility from positive rows or paused status, and is not a universal
statement of Google policy. Google's
[criterion errors](https://developers.google.com/google-ads/api/reference/rpc/v25/CriterionErrorEnum.CriterionError)
describe some provider rejections; country and campaign policy rules remain
separate. Check the current
[restricted targeting policy](https://support.google.com/adspolicy/answer/143465?hl=en)
for your campaign, including housing, employment and consumer-finance advertising
in the US and Canada. Google's
[policy FAQ](https://support.google.com/adspolicy/answer/9997418?hl=en)
describes a context-specific unknown-only exception; this tool does not implement
policy classification, exceptions or a compliance certification.

## Preview, apply and re-read

Review the returned plan and its affected resources. With the default preview
requirement, call `confirm_and_apply` with that exact `plan.id` and `dry_run=true`.
For a plan containing removal, include `confirm_irreversible=true`:

```json
{"plan_id": "<returned plan.id>", "dry_run": true, "confirm_irreversible": true}
```

After a successful preview and approval through your host's workflow, call
`confirm_and_apply` with the same arguments and `dry_run=false`. Acknowledgement
does not bypass preview. Re-read the list or ad group after application. Separate
plans are bound to an account, expire and permit only one execution attempt.

Complete relevant state is read at staging and rechecked immediately before
application. Each apply uses one service request with partial failure disabled;
demographic removals and creations stay together. Google's
[shared-criterion request](https://developers.google.com/google-ads/api/reference/rpc/v25/MutateSharedCriteriaRequest)
and [ad-group-criterion request](https://developers.google.com/google-ads/api/reference/rpc/v25/MutateAdGroupCriteriaRequest)
document request-level atomicity. This is neither a cross-service transaction
nor protection from another writer changing state after the final read.

## Bounds and recovery

The local completeness limits are 100 active lists, 5000 members per list,
1000 campaign links per list, and 16 MiB of verified state. Demographic scans
allow 100 nonremoved criteria per level and 16 MiB. Bounded lookahead detects
oversized results; provider counts must agree with shared inventories. Missing,
foreign, malformed or duplicate records, unknown enums and incomplete reads
refuse rather than returning a usable partial snapshot. These local limits
are not promises of provider capacity: Google's
[negative-list guidance](https://support.google.com/google-ads/answer/2453983)
describes 20 lists and 5000 members, while its
[bulk-application guidance](https://support.google.com/google-ads/answer/7102995)
discusses selecting up to 1000 campaigns in that UI.

- **Stale or unverifiable state:** inspect current resources and supported
  parents, then stage a fresh plan. Do not infer empty state from a failed read.
- **Expired or consumed plan:** a new plan requires a fresh inspection and
  review. Replaying the old plan cannot apply it again.
- **Uncertain write:** there is no retry of the application request. Re-read
  provider state and reconcile the audit record before deciding on further work;
  an error is not proof that Google made no change.
- **Audit failure:** staging or pre-execution failures block the write. If the
  write succeeded but one or more audit records could not be saved, the result
  can report `applied=true` with an audit warning. Repair audit storage and reconcile; do
  not repeat the write to obtain a cleaner log.

The [error guide](errors.md) covers shared plan and transport errors. Installed
console tests use synthetic account state and genuine SDK requests with provider
transport substitution. They establish local workflow behavior, not live Google
acceptance, serving results, authorization to change an account or policy
compliance. Package/archive checks and live acceptance remain separate evidence.
