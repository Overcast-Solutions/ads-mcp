# Experimental writes

Review [operator responsibilities](operator-guide.md) and [configuration](configuration.md) before enabling writes.

## Plan, preview, apply

Write support is an explicit opt-in and remains experimental. Before enabling
it, review the [spend limits](#spend-limits) and the plans your client produces. Configure
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

Plan and execution events use an append-only JSONL audit log. Partial failures,
uncertain transport outcomes and audit failures are surfaced to the caller.

## Account scope

`customer_id` is accepted on every write tool, but only to be **refused** if
it names an account other than `GOOGLE_ADS_CUSTOMER_ID`. Writes always land on
the configured account. Account-data read tools can query another accessible
account using `customer_id`; `health_check` and `list_accounts` describe the
configured login.

## Resource identifiers

`pause_entity`, `enable_entity` and `remove_entity` require an `entity_id`
matching the `entity_type`: `campaign` and `ad_group` use one numeric ID;
`ad` uses the composite `ad_group_id~ad_id`, and `keyword` uses
`ad_group_id~criterion_id`. For example, `201~901` identifies ad 901 under
ad group 201. Surrounding whitespace and leading zeroes in each segment
normalize to the same ID in the preview and request. Missing or extra
segments and malformed IDs return `INVALID_ID` before account calls.

## Spend limits

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

## Campaign creation

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

## Performance Max

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

### URL expansion and exclusions

Read `get_pmax_url_settings(campaign_id)` before changing destinations. Its
`final_url_expansion.explicit` flag distinguishes a returned setting from an
absent entry. `UNSPECIFIED` is not an opt-out. Google documents expansion as
enabled by default for PMax. `set_pmax_final_url_expansion` takes a strict
boolean and preserves unrelated automation entries and their order. Enabling
expansion allows different landing pages and text generated for those pages;
disabling it does not disable independent text customization. See Google's
[automation settings](https://developers.google.com/google-ads/api/docs/assets/asset-automation-settings).

`add_pmax_url_exclusion` creates one negative webpage criterion with one URL
condition. The default `EXACT` mode requires an HTTP(S) URL without user info;
`CONTAINS` accepts a nonblank URL fragment. Neither permits whitespace or
control characters. Existing exact duplicate rules are refused.
`remove_pmax_url_exclusions` takes distinct numeric child criterion IDs and
previews the complete conditions of each selected rule. Removal is irreversible
and requires `confirm_irreversible=true` after preview.

These exclusions are not universal destination blocks. Explicitly supplied
final URLs can still receive traffic, and an excluded homepage can still serve
on some Merchant Center inventory, including Shopping ads on Gmail. Review
Google's [exclusion exceptions](https://support.google.com/google-ads/answer/14337773).

The read uses retained pagination; writes require complete state within the
local 10,000-row and 16 MiB bounds. Unknown or ambiguous state refuses a plan.
Apply reads the campaign and relevant URL state again; changed state returns
`STALE_PLAN` and requires a fresh plan. An external writer can still race after
that recheck. Each application uses one provider request without automatic
write retries. Local request checks do not establish live serving acceptance.

## Ads and assets

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

## Keywords and conversions

Positive keyword creation requires an explicit `match_type`. Negative keywords
default to `EXACT`; a tool-level match type applies to strings, while a
`{text, match_type}` object can override it per keyword. Both positive and
negative keyword text must be nonblank, at most 80 Unicode codepoints and at
most 10 whitespace-separated words, including keywords created with a campaign.
These keyword limits count each codepoint once regardless of width.
Conversion actions
accept `ONE_PER_CLICK`/`MANY_PER_CLICK` counting and a 1–90 day click-through
lookback window (default 30).

## Schedules

Schedules require integer hours and quarter-hour minutes (0, 15, 30 or 45).
Start must precede end using the complete time, so 08:15–08:30 is valid;
24:00 is allowed only as an end. These validation failures return named errors
before account calls, and mutation refusals are recorded with secrets scrubbed.
