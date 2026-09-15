# Authentication and Google API access

**Bring your own Google credentials.** Each person or organization installing
ads-mcp must configure a Google Cloud project and OAuth client they control,
then sign in with a Google account authorized for the Ads accounts they use.
The package provides no maintainer credentials, shared OAuth client, hosted
authentication service or fallback access. Never request or distribute another
organization's credential files or refresh tokens.

An organization may use its own credentials for its authorized internal
installation. This does not grant independent users access through that
organization's project. Run separate installations for independent operators;
do not expose this stdio server as a shared credential-backed proxy.

Google sunset developer tokens on **September 9, 2026**. API access now belongs
to the Cloud project that owns your OAuth client. This downloaded server uses
the operator's own project and OAuth credentials; it supplies no shared project
or credentials. Existing credentials can be reused only when they belong to
the operator and are authorized for this installation.
Legacy `GOOGLE_ADS_DEVELOPER_TOKEN` is optional and ignored for API access;
the server uses the SDK's supported `use_cloud_org_for_api_access` option to
omit the header from unary and streaming requests. See Google's
[migration guide](https://developers.google.com/google-ads/api/docs/api-policy/developer-token).

1. In Google Cloud Console: create (or reuse) your own project, enable the
   **Google Ads API**, and create an OAuth client of type **Desktop app**.
   Download its JSON — this is your `GOOGLE_ADS_CREDENTIALS_PATH` file.
2. Configure the OAuth consent screen for the intended users. External apps
   left in Testing normally receive refresh tokens that expire after seven
   days. See Google's [OAuth error guidance](https://developers.google.com/google-ads/api/docs/get-started/common-errors)
   before choosing the app's publishing status.
3. Set `GOOGLE_ADS_CREDENTIALS_PATH` to the downloaded client JSON and
   `GOOGLE_ADS_TOKEN_PATH` to the token output path, then generate the refresh
   token:

```bash
export GOOGLE_ADS_CREDENTIALS_PATH="/absolute/private/path/oauth_client.json"
export GOOGLE_ADS_TOKEN_PATH="/absolute/private/path/token.json"
.venv/bin/ads-mcp-generate-token   # opens the consent screen
```

Replace both paths with locations on your own encrypted storage. Complete
Google's consent flow yourself; the helper uses your client JSON and stores
your refresh token locally. Do not send either file to the maintainers or
paste their contents into an issue, chat or model prompt.

4. Configure your `GOOGLE_ADS_CUSTOMER_ID` and, for a manager login, your
   `GOOGLE_ADS_LOGIN_CUSTOMER_ID`. Register the installed command with the
   [MCP environment example](../README.md#claude-code--mcp-registration), using your own
   paths and account IDs. Start read-only and call `health_check` to verify
   authenticated account access before requesting reports.

Missing required environment settings refuse startup. Unreadable files or
incomplete OAuth material refuse authenticated operations; there is no
automatic selection of maintainer credentials. The
server can validate credential structure and Google access, but cannot prove
who legally owns a supplied credential. Ownership and permission remain the
operator's responsibility.

`AUTH_TOKEN_REVOKED` identifies credentials that need to be renewed.

## Token storage

The token helper requires a platform and filesystem that enforce POSIX private
file permissions (mode `0600`) and support nonblocking opens (`os.O_NONBLOCK`)
and descriptor-based regular-file checks. It also needs `os.fchmod` when an
existing file's permissions must be tightened; secure creation and an already
private file do not require that call. If a required API is unavailable, the
helper exits with `AUTH_TOKEN_WRITE_FAILED` before writing new token bytes,
preserving existing content. Run it on a system with these capabilities and
use a regular file as the token destination.

The helper does not implement Windows ACL storage; native Windows helper
support is unclaimed and operation has not been validated.

Use operator-managed encrypted storage for credentials, token files, audit
records, transcripts and backups. Private permissions alone are not encryption.
Review the [operator guide](operator-guide.md) before connecting account
data to an MCP host or model provider. This release certifies neither downstream
storage nor providers.

## Project approval and access levels

Review the existing OAuth project's access in the Google Ads API Overview in
Cloud Console, including any migrated access level. Apply there if the project
lacks the access you need. Check that project owner/editor contacts are current
for Google's administrative notices. The
[migration guide](https://developers.google.com/google-ads/api/docs/api-policy/developer-token)
describes these operator responsibilities.

Enabling the API initially grants Test access, which cannot access production
accounts. Explorer permits production reads but excludes Keyword Planner;
Planner needs Basic or Standard access. Account permissions and manager login
still apply independently of project approval. Check the services and quotas
you need against Google's
[access levels](https://developers.google.com/google-ads/api/docs/api-policy/access-levels).

New Basic and Standard applications require brand verification. Existing access
holders are not required to repeat it solely for this migration. For a Basic
application, Google's Ads-specific guidance requires an External audience and
In production publishing status, even for an internal-use app. Follow the
[brand verification guide](https://developers.google.com/google-ads/api/docs/api-policy/brand-verification)
and the [migration requirements](https://developers.google.com/google-ads/api/docs/api-policy/developer-token).
Brand verification is separate from restricted-scope OAuth verification:
review Google's [credential security and verification guidance](https://developers.google.com/google-ads/api/docs/productionize/secure-credentials)
for your deployment and any applicable exceptions. Publishing alone does not
establish verification or grant Ads account access.
