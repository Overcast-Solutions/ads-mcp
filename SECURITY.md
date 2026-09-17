# Security and support

The public source supports CPython 3.12, 3.13 and 3.14 on Linux and macOS.
Native Windows server operation, including read-only mode, mutation audit
storage and the token helper, is unsupported. No Windows ACL implementation
or successful native Windows validation is claimed. Writes remain experimental.

The source repository is [Overcast-Solutions/ads-mcp](https://github.com/Overcast-Solutions/ads-mcp).
The designated reporting route is
[GitHub private vulnerability reporting](https://github.com/Overcast-Solutions/ads-mcp/security/advisories/new).
Use that private form for suspected vulnerabilities, not a public issue.
Do not include live credentials or account data; provide a sanitized reproduction.

Private vulnerability reporting is enabled for this public repository.
Maintainers must check outside-account form access and security-report
notification delivery as part of maintaining that route. If the form is
unavailable, do not post exploitable details publicly. Ordinary sanitized bugs can use the
[issue tracker](https://github.com/Overcast-Solutions/ads-mcp/issues).

The release maintainer owns advisory triage, affected-version assessment,
remediation, coordinated disclosure and release decisions. Review incoming
security reports promptly, prioritize credential exposure and unsafe writes,
and target an initial response within three business days. Deployment operators
own revocation, containment, Google
notifications and their data; see the [operator guide](docs/operator-guide.md).

The software ships no shared Google credentials or authentication service.
Every independent operator must configure their own project, OAuth client and
authorized account login. Never send credential files or tokens to maintainers
as part of setup, support or a vulnerability report.

## Dependency maintenance

CI scans resolved runtime versions on changes and weekly on Monday, retains
machine-readable package and advisory results, and fails on advisories,
unavailable scans or incomplete coverage. Scanner tooling is isolated from
the runtime environment. No advisory is silently ignored. The project is
reviewed as source; pip/setuptools are installation tools rather
than runtime inputs to this scan.

Maintainers review dependencies weekly, evaluate compatible updates at least
monthly, and rerun minimum/current and archive checks before every release.
For an advisory, identify exposure and a fixed version, remediate and rerun
the scan. If a temporary exception is ever necessary, record the advisory ID,
affected exact versions, rationale, compensating controls, responsible owner,
independent reviewer, approval date and near-term expiry. Reassess before
expiry; no permanent exception or silent suppression is allowed. The current
workflow has no exception bypass and will stay red until remediation or an
explicitly reviewed workflow change.

Minimum direct versions are google-ads 31.4.0, MCP 2.0.0 and httpx2/httpcore2
2.12.0, with major ceilings 32/3/3/3 respectively; pytest stays 9.x. The
2026-09-13 comparison found advisories in httpx2/httpcore2 2.9.1 and zero known
findings in its recorded 2.12.0 clean environment. This motivated the floors;
it is neither proof of an exploitable application path nor a permanent
vulnerability-free claim. See [reproducible release checks](docs/releasing.md).
