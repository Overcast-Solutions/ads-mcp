# Operator guide: Google access, data and release responsibilities

**Assessment date: 2026-09-13.** This is a source-grounded deployment guide,
not legal advice, Google approval or certification. Recheck the linked primary
sources before deployment. Documentation update dates are not necessarily
contract effective dates; your accepted agreements and notices still matter.

## Choose the deployment and access model

Use this downloaded stdio server with **your own Google Cloud project, Google
Ads access and OAuth credentials**. Google's programmatic-use policy expressly
preserves downloaded own-credential tools and an entity's own automation.
Agency internal use can fit when clients do not access the tool. Shared OAuth
across independent businesses or a hosted service re-exposing Ads calls needs
a separate policy assessment; customer-supplied tokens alone do not settle it.
Google requires direct entity authentication and describes manual sign-in for
changes. The permitted interval for unattended downstream writes is unresolved;
obtain clarification for the proposed use case. Secondary-interface review
includes advertiser-bound immutable records and independent assessments such
as SOC 2 Type II. This is conditional review, not a universal SOC 2 requirement
for downloaded tools. JSONL append behavior does not meet it by itself.
[Google Ads Developer Policies, Programmatic use and Secondary Interface Review](https://support.google.com/adspolicy/answer/6169371?hl=en).

The supported distribution model is software-only: maintainers supply no
shared client ID, client secret, refresh token, access grant or hosted proxy.
Each independent operator completes the [OAuth setup](../README.md#oauth-setup-one-time)
with their own project and authorized Google identity. An organization's own
internal installation may use that organization's credentials; those files
must not be distributed to independent users. The server requires explicit
local credential paths and does not fall back to a maintainer account.
Successful authentication establishes access, not proof of credential ownership
or advertiser consent. Do not offer delegated access through the maintainer's
project as an alternative to setup.

Since September 9, 2026, access attaches to the OAuth owner's Cloud project;
developer-token headers are currently ignored and a future major API version
will reject them. This server omits them. Operators must verify migrated
access and current project contacts; the package cannot certify your approval.
[Migration guide](https://developers.google.com/google-ads/api/docs/api-policy/developer-token).

| Cloud access | Current daily operation limits |
| --- | --- |
| Test | 15,000, test accounts only |
| Explorer | 2,880 production; 15,000 test; planning services excluded |
| Basic | 15,000, test and production |
| Standard | No daily operation ceiling; service/rate restrictions still apply |

These are project limits over a sliding 24-hour window, separate from account
roles and local spending safeguards. The detailed four-tier table supersedes
the access page's stale generated summary.
[Access levels](https://developers.google.com/google-ads/api/docs/api-policy/access-levels).

New Basic applications require brand verification. The Ads-specific process
requires External/In production settings even when generic Internal/Testing
guidance would suggest otherwise. Existing migrated access is treated separately.
[Brand verification](https://developers.google.com/google-ads/api/docs/api-policy/brand-verification).
Google now classifies `adwords` as a restricted OAuth scope. Assess verification
and any required security assessment for the actual audience; personal/internal
exceptions do not waive all data duties.
[Ads credential security](https://developers.google.com/google-ads/api/docs/productionize/secure-credentials).

RMF is conditional on Standard access and tool classification: internal-only
tools are exempt; external reporting tools have reporting duties; full-service
tools have creation, management and reporting duties. Planning and
recommendation features need their own coverage review. Read-only mode or
arbitrary GAQL support does not prove required fields are displayed. Evaluate
the MCP host's presentation as well as the server, and ask Google when the
classification is uncertain.
[Required minimum functionality](https://developers.google.com/google-ads/api/docs/api-policy/rmf).

Keep usage within the intended use described in your access application. Submit
the Google Ads API Tool Change Form when that use changes, such as adding
management to a reporting tool. Significant functionality changes can trigger
RMF reclassification. If Google requests a demo account, the responsible
operator must provide it within **seven days of the request**, using the live
tool or a demo with equivalent functionality that accurately represents it.
Keep project contacts current and assign someone to respond. This conditional
duty does not assert that Google has requested a demo for this project.
[Developer Policies, Appropriate Access, Demo account and Contact information](https://support.google.com/adspolicy/answer/6169371?hl=en).

Where RMF applies, implement newly required features by their published due
dates. Send screenshots or mockups of material proposed tool changes through
the Tool Change Form at least **two weeks before** they take effect; after
that period Google does not require waiting for a response. Evaluate these
duties against the access level and tool category above, including the
internal-only exemption. They do not establish a universal approval requirement
for every self-hosted installation or replace separate secondary-interface
review where applicable.
[RMF, Feature implementation](https://developers.google.com/google-ads/api/docs/api-policy/rmf#feature_implementation).

## Account and action authorization

OAuth's Ads scope covers both reads and writes; account roles and manager
relationships determine effective access. A customer ID is not authorization.
Use the least-privileged Ads role appropriate to the task and retain the
server's read-only default during evaluation.
[Google Ads access model](https://developers.google.com/google-ads/api/docs/oauth/access-model).

Project recommendation: document advertiser authority, the intended account,
allowed operations and who may approve them. Experimental writes stage plans,
but an agent can also call the confirmation tool; confirmation alone does not
authenticate a human. Keep a trusted approval boundary in the MCP host and
review the [per-operation cap limits](migration.md). Never automate policy
evasion. Google's rules prohibit cloaking, replacement-account abuse and
verification circumvention.
[Circumventing systems](https://support.google.com/adspolicy/answer/15938075?hl=en).

## Disclose model-provider use

The MCP host receives Ads outputs and may send them to a model provider.
Local stdio does not limit that downstream transfer. Before enabling a cloud
model, identify the endpoint, subprocessors, regions, retention, human-access
practices and training terms. Minimize fields and obtain necessary authorization.
Account names, keywords, URLs and change records can be confidential or personal.

The general user-data policy requires prominent, timely disclosures and limits
covered data use and transfers to permitted purposes. Its additional
specific-scope requirements have an own-domain exception; that does not waive
the whole policy. Self-hosting is not synonymous with own-domain-only use.
Limited Use reaches derived and anonymized data. Its advertising-use language
and Ads-specific permitted management uses require careful applicability
review, not a blanket exemption or blanket ban.
[User Data Policy](https://developers.google.com/terms/api-services-user-data-policy).

Project recommendation: prefer explicitly contracted no-training inference
for a disclosed user-facing feature. Inference and provider training are
different uses; no-training does not mean zero retention. Customer fine-tuning,
cross-customer training or benchmarking needs a separate rights, purpose and
consent analysis. The Workspace-specific training restriction is not imported
solely because this Ads-only app uses OAuth; adding Workspace data changes
that analysis. [Workspace policy scope](https://developers.google.com/workspace/workspace-api-user-data-developer-policy).

OAuth account consent does not itself authorize every onward disclosure.
General API terms require explicit opt-in for exposing nonpublic content to
third parties. Retention/cache and export obligations also depend on owner
permission and applicable law; there is no universal audit-retention period
established here. [Google APIs Terms, section 5](https://developers.google.com/terms).

## Storage, lifecycle and incidents

**Operator-managed encrypted storage is a deployment prerequisite.** Cover
credentials, tokens, audit logs, transcripts, exports and backups; protect
encryption keys separately. POSIX permissions restrict access but do not
encrypt files. This package supplies no encryption/keychain system and does
not certify volumes, backups or providers. OAuth policy requires encryption
of tokens at rest, then revocation and permanent deletion when access is no
longer needed. [OAuth policies, Handle user tokens securely](https://developers.google.com/identity/protocols/oauth2/policies).

Project procedure: inventory copies and owners; choose and document a justified
retention period; restrict parent directories; stop the server before controlled
rotation; verify the new audit destination before re-enabling writes. On exit,
stop the MCP host's jobs, revoke the Google grant, delete unneeded local tokens
and data, and request deletion of provider/backup copies under the applicable
retention policy. Revocation does not erase existing copies. Do not delete the
advertiser's campaigns or unrelated account access. Logs are neither immutable
evidence nor an entitlement to retain data indefinitely.

For applicable third-party deployments, Google requires written client consent
for account-data disclosure, data-management help, and disassociation within
three business days. Its incident rules require prompt Google notification of
known or suspected unauthorized access and notification before public statements.
Assign an incident owner and separately handle legal notices.
[Developer Policies, disclosures, opt-out and security](https://support.google.com/adspolicy/answer/6169371?hl=en).

Agencies must distinguish Google charges from management fees, disclose fees,
provide customer IDs on request, and assess the conditional small-advertiser
notice and reporting duties. Do not present estimated or mixed-platform totals
as exact Google charges.
[Transparency requirements](https://support.google.com/adspolicy/answer/16489093?hl=en).

The MCP host owns installation consent, sandboxing, executable trust and trusted
tool approval. Direct stdio avoids an MCP HTTP authentication layer; adding a
remote wrapper requires separate authorization, audience, session and proxy
controls. [MCP security guidance](https://modelcontextprotocol.io/docs/2025-11-25/tutorials/security/security_best_practices).

## Licensing and unresolved review

[Apache License 2.0](../LICENSE) licenses this software; it grants no rights to Google accounts or returned
data. Retain applicable license notices for any dependencies or material you
redistribute, and review the provenance of bundled assets. The Ads API
agreement defines development/distribution as API use, so publishers cannot
assume that open source eliminates contractual duties. Review accepted terms,
change notices, indemnities, data-processing roles and termination obligations
with qualified counsel before external commitments.
[Ads API Terms, sections 1, 7, 8, 11, 14 and 15](https://developers.google.com/google-ads/api/docs/api-policy/terms).

The release reviewer should record the exact source/manifest, intended audience,
Cloud access, RMF/verification applicability, authority for writes, data flows,
encrypted-storage evidence and retention/incident owners. Keep unresolved
hosted-proxy, unattended-write, Limited Use and contract questions explicit.
This guide neither establishes Google endorsement nor clears a downstream
deployment. Publication and account cutover remain separate decisions.
