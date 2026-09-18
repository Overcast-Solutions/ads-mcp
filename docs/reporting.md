# Reporting

Curated reports provide typed metrics and bounded responses. Raw GAQL preserves selected fields, including zeros and explicit nulls.

Metric reports require an explicit date window or `last_n_days`; see [window and migration semantics](migration.md) and the [tool catalog](tools.md) for individual parameters.

`get_campaign_performance` includes all four optional `network_settings`
booleans for Search and non-Search campaigns. Explicit provider `false` remains
`false`; an absent value is `null`. Metrics and pagination are unchanged. See
[campaign networks](campaign-networks.md) for field meanings and guarded updates.

## Campaign filters

Report `campaign_id` filters use one numeric ID, normalized consistently in
queries, returned scope and local pagination. An omitted optional filter or
explicit `null` selects all campaigns; an explicit blank or malformed value
returns `INVALID_ID` before account calls. Shopping performance, listing
groups and product status still require a campaign ID.

## GAQL results

`run_gaql` preserves the selected public GAQL field names in JSON, CSV and
table output, including fields such as `recommendation.type`. Its optional
`page_size` must be an integer from 1 to 10,000 and bounds the local response
page, capped by `ADS_MCP_ROW_LIMIT`; omission uses that configured limit.
Continue with `next_page_token` until it is absent. An omitted `customer_id`
uses the configured account; an explicitly empty or malformed ID is refused.
Account permission failures name the requested account and manager login.

## Pagination and retained snapshots

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

## Change history

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

## Keyword forecasts

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

Keyword Planner requires Basic or Standard project access; see [authentication and access levels](authentication.md#project-approval-and-access-levels).
