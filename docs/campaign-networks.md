# Campaign networks

Search campaign creation and network updates use the usual staged
[write flow](writes.md): inspect the plan, preview with `confirm_and_apply`
using `dry_run: true`, then apply the same plan with `dry_run: false`.
Staging and preview do not mutate Google Ads. Enabling a network may change
serving and spend under the campaign's existing budget.

## Creation defaults

`draft_campaign` accepts these optional strict booleans for `SEARCH`:

| Argument | Default | Meaning |
|---|---|---|
| `target_google_search` | `true` | Google Search |
| `target_search_network` | `false` | Ordinary Search Partners |
| `target_partner_search_network` | `false` | Restricted partner network |
| `target_content_network` | `false` | Display Network |

Omission or `null` selects the Search default; explicit `false` is retained.
All four effective values appear in the plan and the campaign create request.
Search Partners requires Google Search. Restricted partner targeting is a
separate provider/account-dependent option, not another name for ordinary
Search Partners. Google Ads determines eligibility and supported combinations;
a staged plan is not provider acceptance or evidence of live delivery.
See Google's [NetworkSettings reference](https://developers.google.com/google-ads/api/reference/rpc/v25/Campaign.NetworkSettings)
for field definitions and provider eligibility restrictions.

Non-null network options are refused for `DISPLAY` and `PERFORMANCE_MAX`.
Omitting them or supplying `null` preserves those channels' existing creation
behavior. Campaigns still default to `PAUSED`, require the political declaration,
and retain the configured budget and dependent-creation checks.

## Existing Search campaigns

`set_campaign_networks` requires a canonical numeric `campaign_id` and at least
one network boolean. It supports enabled or paused standard Search campaigns
(campaign subtype `UNSPECIFIED`) in the configured account. For example:

```json
{"campaign_id": "701", "target_google_search": true, "target_search_network": false}
```

The plan shows the before and after value of each supplied field. Only those
fields enter the update mask: the example updates
`network_settings.target_google_search` and
`network_settings.target_search_network`. `null` means omitted; explicit
`false` is an update. Unspecified fields are retained, never changed implicitly.
The resulting merged settings must keep Search Partners off when Google Search
is off. To disable both, supply both values as `false` in one plan.

Staging verifies singular, complete campaign identity, status, channel, subtype
and all four network settings. Apply reads the same state again and refuses a
stale plan before sending any mutation. Normal account binding, plan expiry,
preview, audit and single-use safeguards apply. Google Ads eligibility checks
still govern the eventual request, including restricted partner access.

## Reporting

`get_campaign_performance` includes `network_settings` with all four fields for
every campaign, including non-Search campaigns. An explicit provider `false`
stays `false`; an absent optional value is `null`. Reporting does not infer
eligibility or effective reach from missing values. Existing metrics, account
scoping and pagination remain available. Mutation staging requires complete
state even though observational reporting allows absent values.
