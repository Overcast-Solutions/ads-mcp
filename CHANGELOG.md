# Changelog

## 0.1.0 — unreleased

- Inspect, create and maintain same-account shared negative-keyword lists for
  supported Search and Shopping campaigns. Membership previews include all
  linked campaigns; removals and detachments require acknowledgement.
- Inspect and change explicit ad-group demographics on standard Search and
  Display. Immutable polarity changes remove and recreate criteria, refusing
  direct customization loss. Positive status-only updates preserve settings.
  The shared targeting guide explains defaults, inherited exclusions, local
  limits and provider policy boundaries; experiments remain future work.
- Audit loss after a sent mutation remains visible in the structured result,
  without emitting the private audit-log path in a separate stderr diagnostic.
- Inspect and edit final and mobile URLs on existing responsive search ads
  and positive keywords in supported Search ad groups. Plans preserve creative,
  tracking, bids and status, verify relevant state before applying, and update
  the existing resource. The Search URL guide covers clearing, tracking
  dependencies, local input bounds and uncertain outcomes.
- PMax management: inspect asset groups, signals, existing audiences and URL
  settings; stage asset-group pause/enable, search themes, audience signals,
  URL expansion/exclusions and complete flat Item-ID product selection.
  Relevant state is checked again before apply; removals and tree replacement
  require irreversible acknowledgement. The PMax guide documents local limits,
  provider documentation conflicts and delivery effects.
- 76 total operations in write mode, including 30 reads. Default offline
  fixture replay covers the original 21, four PMax, two Search URL and three
  shared targeting reads.
  Installed archive checks require exact tool identities; source archives
  must include the PMax, Search URL and shared targeting contracts, fixtures
  and guides. Installed console workflows exercise synthetic provider state;
  these checks do not establish live Google acceptance.
- Adopt Apache License 2.0 for the current source and distribution metadata.
  Earlier MIT-licensed source remains available under its original license.

- Explicit operator-owned OAuth onboarding, repository metadata and a designated
  private vulnerability-reporting route with public-release activation steps.
- CI actions pinned to supported Node.js 24 releases, with pull-request checks
  and default-branch validation without duplicate topic-branch push matrices.
- Source export preserves whitespace in Git repository identities, rejects
  Unicode/case aliases before creation, and scans Google access-token shapes
  in prose and Bearer headers. Unique non-ASCII paths remain supported.
- Operator guidance covers Google's requested seven-day demo and intended-use
  changes, with conditional RMF deadlines and advance change-review duties.
- Committed-tree source export with fail-closed scanning, content hashes,
  exact synthetic-fixture exceptions and mandatory independent human review.
- Apache-2.0-licensed package, integration and sourced operator guidance, six-cell
  Linux/macOS CI, minimum dependencies and failing advisory scans. Hosted
  execution remains separate release evidence; native Windows is unsupported.

- Cloud-project API access following the September 9, 2026 developer-token
  sunset: legacy configuration is optional and never transmitted. Requires
  Google Ads SDK 31.4.0+, with named production-project approval guidance.
- 30 read tools for account discovery, GAQL, reporting, policy issues,
  recommendations, PMax, Search destinations, shared targeting and shopping data, and
  nonpersistent Keyword Planner requests.
- Account selection and campaign scoping on applicable reads, typed money,
  explicit pagination, supported report-field selections and preserved listing
  dimension values.
- A management surface that stages mutations as expiring, customer-bound,
  single-use plans with configurable preview requirements and explicit
  acknowledgement for irreversible operations.
- Configuration-controlled daily-budget, CPC-increase and first-bid limits.
  Existing bid baselines and applicable recommendation budgets are read from
  the account and checked again at application. CPA/ROAS targets and strategy
  changes remain outside this budget/CPC cap model.
- Campaign, ad-group, keyword, creative, targeting and conversion parameters
  carried through to API requests, with paused creation defaults where
  supported.
- Independently authored workflow requirements and an offline structural
  checker for declared operations, parameters, required paths and finite
  values. Forbidden declarations are inspected in every actual tool, including
  reachable map value schemas under `additionalProperties`; map locations use
  a diagnostic-only `*` segment. Executable scenarios cover runtime behavior.
- Authenticated health checks, bounded read retries, no automatic mutation
  retries, structured application errors and secret scrubbing.
- Required audit configuration in write mode, per-operation audit records,
  and explicit reporting of partial failures or uncertain write outcomes.
- Read-only registration by default. Write support remains experimental.
- Golden read contracts, locked guardrail regressions, OAuth token helper,
  generated tool documentation and complete source-distribution test assets,
  including the project-owned workflow requirements fixture.
