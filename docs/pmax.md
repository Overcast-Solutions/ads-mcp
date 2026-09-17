# Performance Max management

Inspect existing Performance Max (PMax) campaigns in the default read-only
installation. Experimental writes can manage asset-group status, optimization
signals, URL controls and product selection. Use your own authorized credentials
and review [authentication](authentication.md), [configuration](configuration.md)
and the [write flow and limits](writes.md#plan-preview-apply) before enabling
writes. All writes target the configured account; reads can take an explicit
`customer_id` for another accessible account.

For a controlled same-campaign 50/50 test of text customization and final URL
expansion, use the [PMax experiment guide](pmax-experiments.md). It covers
creation, direct arm reporting, ending and asynchronous promotion observation.

The examples below are MCP `tools/call` parameter objects. Replace synthetic
IDs, URLs and Item IDs with values you have inspected in your account. A call
to a staging tool returns a plan; it does not apply the change.

## Inspect before staging

Start with `health_check` and `get_account_info` to verify access and account
identity. Inspect the campaign's groups, then the selected group's signals:

```json
{"name": "get_asset_groups", "arguments": {"campaign_id": "701"}}
```

```json
{"name": "get_asset_group_signals", "arguments": {"asset_group_id": "801"}}
```

```json
{"name": "list_audiences", "arguments": {}}
```

```json
{"name": "get_pmax_url_settings", "arguments": {"campaign_id": "701"}}
```

```json
{"name": "get_listing_groups", "arguments": {"campaign_id": "701"}}
```

`get_asset_groups` reports IDs, status, serving-related primary status and final
URLs. Signal reads identify the kind, support status and available policy
details. Audience reads include status, scope and asset-group binding. Listing
groups are campaign-scoped: inspect the rows belonging to your chosen asset
group. Follow every continuation token with identical arguments and inspect
truncation warnings; see [pagination](reporting.md). A retained read may expose
only a bounded prefix. Staging independently requires complete relevant state
and refuses ambiguous or oversized results.

## Stage, preview, apply and re-read

For example, stage pausing one existing group:

```json
{
  "name": "pause_entity",
  "arguments": {"entity_type": "asset_group", "entity_id": "801"}
}
```

Review the returned plan's account, parent campaign, old status and proposed
status. Copy its `plan.id` into the following preview call:

```json
{
  "name": "confirm_and_apply",
  "arguments": {"plan_id": "<returned-plan-id>", "dry_run": true}
}
```

After reviewing that preview and obtaining the approval your host requires,
apply the same plan:

```json
{
  "name": "confirm_and_apply",
  "arguments": {"plan_id": "<returned-plan-id>", "dry_run": false}
}
```

Then call `get_asset_groups` again without an old continuation token and verify
the group's status. To enable a paused group, stage `enable_entity` with the
same `entity_type` and `entity_id`, then follow the same flow. Enabling may
resume delivery and spend under existing budgets. Asset-group removal is not
supported by `remove_entity`.

Every staging example below uses this same preview/apply sequence. Signal
removal, exclusion removal and product-tree replacement are **irreversible**:
their apply call also requires `"confirm_irreversible": true` in `arguments`.
This acknowledges the reviewed plan and does not bypass preview. There is no
automatic undo. Plans expire, are account-bound and permit one execution
attempt. Re-read the affected surface after each apply before planning the
next change.

## Search themes and existing audience signals

```json
{
  "name": "add_asset_group_search_themes",
  "arguments": {"asset_group_id": "801", "themes": ["trail shoes"]}
}
```

The local ceiling is **50 resulting themes per group**, including existing
themes. Each new theme must be nonblank, at most **80 Unicode codepoints**
after trimming, and free of control characters. Exact duplicates after trimming
and already-present themes are refused. Google documentation conflicts on the
count: the [Google Ads Help instructions](https://support.google.com/google-ads/answer/14767319)
say 50, while the [API enhancement guide](https://developers.google.com/google-ads/api/performance-max/upgrade-enhancements)
still says 25. The local ceiling does not resolve that conflict or promise
acceptance of 50 themes by Google. Provider eligibility and policy refusals
remain authoritative.

```json
{
  "name": "add_asset_group_audience_signal",
  "arguments": {"asset_group_id": "801", "audience_id": "901"}
}
```

Use an existing, enabled `Audience` from `list_audiences` in the same customer.
A `CUSTOMER` scope audience is reusable there; an `ASSET_GROUP` scope audience
must be bound to the selected group. Google documents these
[audience scope rules](https://developers.google.com/google-ads/api/performance-max/asset-group-signals).
This tool attaches that resource; it does not create audiences, edit their
composition or upload customer lists. A user-list or segment ID is not an
Audience ID. Signals guide optimization rather than impose hard targeting;
ads may reach people beyond those signals, as described in Google's
[PMax enhancement guide](https://developers.google.com/google-ads/api/performance-max/upgrade-enhancements).

```json
{
  "name": "remove_asset_group_signals",
  "arguments": {"asset_group_id": "801", "signal_ids": ["501"]}
}
```

If inspection returns `signal_id: "801~501"`, pass only the numeric child
`"501"` in `signal_ids`, together with group `"801"`. Only existing search-theme
and audience signals can be removed. Other kinds, including travel inventory
rules, remain visibly unsupported and cannot be removed here. Signal changes
use create/remove operations, consistent with Google's
[signal API](https://developers.google.com/google-ads/api/performance-max/asset-group-signals);
editing a theme requires separately reviewed removal and addition. After each
apply, re-read `get_asset_group_signals` and inspect policy details.

## Final URL expansion and exclusions

```json
{
  "name": "set_pmax_final_url_expansion",
  "arguments": {"campaign_id": "701", "enabled": false}
}
```

Supply a boolean. `get_pmax_url_settings` distinguishes an explicit automation
entry from an absent one: `explicit: false` with `UNSPECIFIED` does not mean
disabled. Google documents expansion as enabled by default for PMax.
The setter changes `FINAL_URL_EXPANSION_TEXT_ASSET_AUTOMATION`, preserving
unrelated entries. Enabling it permits different landing pages and generated
text for those pages; disabling it does not disable independent
`TEXT_ASSET_AUTOMATION` text customization. Review Google's
[asset automation settings](https://developers.google.com/google-ads/api/docs/assets/asset-automation-settings).

```json
{
  "name": "add_pmax_url_exclusion",
  "arguments": {
    "campaign_id": "701",
    "url": "https://example.invalid/careers",
    "match_type": "EXACT"
  }
}
```

`EXACT` is the default and requires an HTTP(S) URL without user information.
For a substring rule, use `"url": "/careers", "match_type": "CONTAINS"`.
Each addition creates one negative webpage criterion with one URL condition.
Blank rules, whitespace/control characters and exact duplicates are refused.
This interface does not add page feeds, label rules or compound exclusions.

```json
{
  "name": "remove_pmax_url_exclusions",
  "arguments": {"campaign_id": "701", "criterion_ids": ["601"]}
}
```

Use numeric child criterion IDs returned by `get_pmax_url_settings`. Removal
requires an existing negative webpage URL criterion under the campaign, shows
all its conditions in the plan, and cannot remove unrelated criteria. Re-read
`get_pmax_url_settings` after applying either kind of URL change.

Exclusions have limits: the campaign's supplied final URL can still receive
traffic, and an excluded homepage can still serve for some Merchant Center
inventory, including Shopping ads on Gmail. They are not a universal block on
destinations. Review Google's [exclusion exceptions](https://support.google.com/google-ads/answer/14337773).
Changes to landing pages or exclusions can alter delivery and spend even when
the budget stays unchanged.

## Replace product selection by Item ID

```json
{
  "name": "set_asset_group_product_selection",
  "arguments": {"asset_group_id": "801", "item_ids": ["SKU-A", "sku-b"]}
}
```

The group must belong to a feed-linked PMax campaign. This replaces its entire
listing tree with included Item-ID leaves and an excluded everything-else leaf.
It changes inventory eligibility and may affect delivery and spend. Confirm
the whole replacement, not just the new IDs, before acknowledging it as
irreversible.

| Local bound | Supported input/state |
| --- | --- |
| Selected Item IDs | 1–998 distinct IDs; case is preserved |
| Item-ID text | At most 128 Unicode codepoints after trimming; no controls or internal whitespace |
| Existing tree | At most 1000 nodes and 16 MiB of serialized state |
| Source shape | Empty, a single included/excluded all-products root, or a flat Item-ID subdivision with exactly one catch-all |

The 998-ID ceiling reserves two of the 1000 nodes for the root and catch-all.
`SKU-A` and `sku-a` remain distinct. Empty selections, nested trees, other
dimensions, duplicate siblings and inconsistent identities are refused;
unsupported trees are never silently flattened. Inspect or restructure those
trees deliberately in Google Ads before staging here.

The plan contains every old and new node. One dedicated SDK request removes
children before the old root, then creates the root before its new leaves.
Google's [listing-group request](https://developers.google.com/google-ads/api/reference/rpc/v25/MutateAssetGroupListingGroupFiltersRequest)
validates the resulting tree atomically and has no partial-failure option.
This does not verify that every supplied Item ID is an eligible product.
After applying with irreversible acknowledgement, re-read
`get_listing_groups` for the campaign and inspect the selected group's rows.

## Refusals and evidence boundaries

Staging verifies resource ownership, parent campaign and relevant existing
state. Application reads that state again. A changed group, signal/audience,
URL configuration or product tree returns `STALE_PLAN`: inspect the new state
and stage a fresh plan. External writers can still race after the recheck.
URL and signal writes require complete state within local 10,000-row and
16 MiB bounds; product trees use the stricter bounds above.

Provider writes are never automatically retried. An uncertain outcome requires
account inspection before further action; see [error recovery](errors.md).
Budget/CPC caps apply per operation and do not cap aggregate spend or eliminate
delivery effects from these workflows. Approval must come from the operator's
trusted process; a model's confirmation call is not proof of human approval.

Offline fixtures, installed-entrypoint tests and genuine v25 SDK request
assertions establish implemented behavior. They do not establish **live Google
acceptance**, account eligibility, policy approval or a completed deployment.
See the [tool reference](tools.md) for all parameters and
[operator responsibilities](operator-guide.md) for deployment review.
