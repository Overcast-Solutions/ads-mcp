# Search ad and keyword URLs

Inspect and update landing destinations on an existing responsive search ad
(RSA) or positive keyword. Updates keep the same resource: they do not replace
the ad or keyword, edit creative content, or change bids, budgets or status.
Destination changes can still affect delivery and policy review.

The two inspection tools are available in read-only mode. To stage updates,
configure your own credentials and audit storage, then explicitly enable
experimental writes as described in the [write guide](writes.md). Reads may
select another accessible `customer_id`; writes always target the configured
account. Omitted or actual JSON `null` selects that configured account. The
string `"null"` is invalid and does not select an account.

## Inspect the current destination

Use `get_responsive_search_ad_urls` with separate ad-group and ad IDs:

```json
{"ad_group_id": "801", "ad_id": "601"}
```

For a keyword, call `get_keyword_urls` with its ad-group and criterion IDs:

```json
{"ad_group_id": "801", "criterion_id": "602"}
```

These examples use synthetic IDs; replace them with your own. Resource IDs
must be positive numeric strings without signs, spaces or leading zeros,
within the signed 64-bit range. Do not supply composite IDs or resource paths
in these arguments.

Inspection returns the resource and parent identities/statuses, final and
mobile URL lists, direct tracking template, final URL suffix and custom
parameters. RSA inspection also shows headlines, descriptions and display
paths; keyword inspection shows text and match type. Empty keyword final URLs
mean fallback to the ad destination. These direct settings do not resolve a
particular serving ad, inherited tracking, redirects or the final served URL.

Only enabled or paused resources under an enabled or paused `SEARCH` campaign
and `SEARCH_STANDARD` ad group are supported. An RSA must have exactly one
nonremoved ad-group association, matching the requested group. Negative
keywords and other criterion/ad types are refused. Missing, ambiguous,
unrecognized or oversized relevant state returns `SEARCH_URL_STATE_UNVERIFIED`.
The inspections require complete singular state within a local 16 MiB bound;
they have no pagination arguments.

## Stage, preview, apply and re-read

1. **Inspect** with the appropriate read tool above and review the account,
   parent status and direct tracking settings.
2. **Stage** with `update_responsive_search_ad_urls`:

   ```json
   {
     "ad_group_id": "801",
     "ad_id": "601",
     "final_urls": ["https://example.invalid/collection?source={network}"]
   }
   ```

   Or stage with `update_keyword_urls`:

   ```json
   {
     "ad_group_id": "801",
     "criterion_id": "602",
     "final_urls": ["https://example.invalid/collection/item"]
   }
   ```

   Use a real, appropriate destination for your account. Staging returns a
   plan and makes no provider mutation. Review its exact before/after lists,
   changed fields and preserved resource context. Save the returned `plan.id`.
3. **Preview** by calling `confirm_and_apply` with that ID:

   ```json
   {"plan_id": "<returned plan.id>", "dry_run": true}
   ```

   Preview does not apply the edit. Preview is required by default through
   `ADS_MCP_REQUIRE_DRY_RUN=true`; an operator may configure that requirement
   at server startup. No tool argument can bypass the configured requirement.
4. **Apply** the reviewed plan with `confirm_and_apply`:

   ```json
   {"plan_id": "<returned plan.id>", "dry_run": false}
   ```

   These URL edits do not require irreversible-removal acknowledgement. Plans
   expire and allow only one execution attempt. The default lifetime is
   900 seconds, configured with `ADS_MCP_PLAN_TTL_SECONDS`.
5. **Re-read** with the same inspection arguments and compare the stored
   destination lists. Review delivery and policy status separately; a stored
   URL is not evidence that the destination is serving.

## Replacing and clearing lists

| URL argument | Meaning |
| --- | --- |
| Omitted or JSON `null` | Preserve the existing list |
| A JSON array of URLs | Replace that entire list in the supplied order |
| `final_mobile_urls: []` | Clear mobile URLs |
| `final_urls: []` on a keyword | Clear the override, subject to the dependencies below |
| `final_urls: []` on an RSA | Refused; the ad must retain final URLs |

Supply at least one URL list. An unchanged resulting pair returns `NO_CHANGES`
without a plan. Use actual JSON arrays and null values: an encoded array
string such as `"[\"https://example.invalid\"]"`, the string `"null"`, a scalar
URL or any other wrong type is refused before account reads.

Each list has a **local** limit of **10 unique URLs**, with at most **2048
Unicode codepoints** per URL. These bounds do not claim Google's maximums.
URLs require HTTP or HTTPS, a host and valid port syntax. User information,
whitespace and control characters are refused. Valid spelling, case, query,
fragment, ValueTrack tokens and list order are preserved without trimming or
normalization. Google may still reject a locally valid URL or token context.

## Keyword clearing and tracking dependencies

A keyword cannot retain mobile URLs when its final URL list is empty. If
mobile URLs exist, clear both lists together with `update_keyword_urls`:

```json
{
  "ad_group_id": "801",
  "criterion_id": "602",
  "final_urls": [],
  "final_mobile_urls": []
}
```

Clearing final URLs also requires the keyword's direct tracking template and
custom parameters to be absent. A conflict returns `KEYWORD_URL_DEPENDENCY`.
If clearing is intended, change those tracking settings separately through an
authorized provider interface, then inspect again and stage a new plan. These
editors never erase tracking implicitly. The final URL suffix is preserved,
as are keyword text, match type, bids and status.

## Changed state and uncertain outcomes

Before applying, the server re-reads relevant URL, tracking, creative or
keyword text/match state and supported parent/type/status state. Drift or an
unverifiable recheck returns `STALE_PLAN` without a provider mutation. Inspect
again and stage a fresh plan. An external writer can still race after this
check; it is not a lock on the Google Ads account.

The request updates only supplied URL fields whose values changed. The
application does not retry provider writes. After an uncertain transport
outcome, the plan is consumed because the provider may have accepted the
request. Re-read the resource and reconcile its state before considering
another plan. An audit warning after a successful write also requires
reconciliation; it does not mean the write failed. See [error recovery](errors.md).

Google remains authoritative for account eligibility, destination validation
and policy review. Local validation, dry-run preview and offline request tests
do not establish live provider acceptance or serving behavior. URL edits can
alter traffic under existing budgets, so review their delivery effects along
with the [operator responsibilities](operator-guide.md).
