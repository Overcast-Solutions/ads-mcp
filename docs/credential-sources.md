# Credential sources

`GOOGLE_ADS_CREDENTIAL_SOURCE` selects where the server reads its OAuth client
and refresh token. It is validated before every other setting, so an
unrecognized value refuses startup naming the allowed values; an unset or empty
value means `files`.

| Source | Reads | Choose it when |
| --- | --- | --- |
| `files` (the default) | `GOOGLE_ADS_CREDENTIALS_PATH` and `GOOGLE_ADS_TOKEN_PATH` | you minted a refresh token with `ads-mcp-generate-token` |
| `adc` | an authorized-user Application Default Credentials file | the host is already set up with `gcloud` |
| `google_ads_yaml` | a `google-ads.yaml` configuration file | you are coming from a Google Ads client-library setup |

`GOOGLE_ADS_CUSTOMER_ID` stays required for all three, and all three reach the
same 85 operations (35 of them reads) under the same guardrails. The source
decides where OAuth material comes from and nothing else: the API endpoint,
transport, headers and spend limits always come from this server's own
configuration.

Settings the selected source does not read are ignored and listed by
`health_check` as `config.unused_settings`, so a stale variable is visible
instead of silently in effect. Problems other than the source value itself —
a missing file, an unreadable one, incomplete OAuth fields — surface through
`health_check` and the tools as named `AUTH_CONFIG_*` errors, never as file
contents or credential values. The `adc` and `google_ads_yaml` files are each
read as at most 64 KiB of UTF-8 from a regular file, so a directory, a FIFO or
an oversized file is refused before anything is parsed.

## `files` — token files (default)

The two-file setup from the [README](../README.md#oauth-setup-one-time):
`GOOGLE_ADS_CREDENTIALS_PATH` is your Desktop-app OAuth client JSON and
`GOOGLE_ADS_TOKEN_PATH` the refresh token `ads-mcp-generate-token` wrote.
`GOOGLE_ADS_CONFIGURATION_FILE_PATH` is not read under this source.

## `adc` — Application Default Credentials

Set `GOOGLE_ADS_CREDENTIAL_SOURCE=adc` to authenticate with the ADC file
`gcloud` maintains. The lookup order is:

1. `GOOGLE_APPLICATION_CREDENTIALS`, when it is set to a non-blank value. It
   is then the only candidate — there is no fallback behind an explicit
   setting, so a wrong path is reported instead of being papered over.
2. Otherwise `$CLOUDSDK_CONFIG/application_default_credentials.json`, when
   `CLOUDSDK_CONFIG` names a gcloud configuration directory, and then
3. `~/.config/gcloud/application_default_credentials.json`, the default
   location for the account running the server. The first of the two that
   exists is used.

If no candidate exists, the error names the paths that were tried. No metadata
server, instance identity or ambient cloud credential is ever probed.

Only `type: authorized_user` credentials are accepted. Service accounts,
workload identity federation and impersonation can authenticate, but cannot
authorize a Google Ads API *user*, so they are refused by name rather than
failing later inside a report.

ADC must also be minted with **your own** Desktop OAuth client and the Google
Ads scope, because API access belongs to the Cloud project that owns the
client. `gcloud`'s built-in client cannot carry that scope, so credentials
minted with it are refused:

```bash
gcloud auth application-default login \
  --client-id-file=/absolute/private/path/oauth_client.json \
  --scopes=https://www.googleapis.com/auth/adwords
```

`GOOGLE_ADS_CREDENTIALS_PATH`, `GOOGLE_ADS_TOKEN_PATH` and
`GOOGLE_ADS_CONFIGURATION_FILE_PATH` are not read under this source.
`GOOGLE_ADS_LOGIN_CUSTOMER_ID` still applies as usual. `health_check` reports
`config.credential_source: adc` and a `config.credential_detail` naming the
resolved file, with both path fields null.

## `google_ads_yaml` — a client-library configuration file

Set `GOOGLE_ADS_CREDENTIAL_SOURCE=google_ads_yaml` to read an existing
`google-ads.yaml`. The file comes from `GOOGLE_ADS_CONFIGURATION_FILE_PATH`,
else `google-ads.yaml` in the home directory of the account running the server.
It is parsed with a safe loader; YAML anchors, aliases and duplicate keys are
refused, because each of them lets the file mean something other than it reads.

**Read:** `client_id`, `client_secret` and `refresh_token` (each a non-blank
string), plus `login_customer_id`. An explicit `GOOGLE_ADS_LOGIN_CUSTOMER_ID`
overrides the file's value.

Quote the login id. An unquoted one is a YAML 1.1 number, and only a plain
decimal literal of exactly ten digits with no leading zero is the account it
reads as; every other numeric form resolves to some other integer and gets
the `AUTH_CONFIG_INCOMPLETE` refusal that asks for quotes, before any
credential is used. The forms that read like an id and are not one: a leading
zero is octal (`010000000000`), a `0x` prefix is hexadecimal (`0x499602D2`),
digit groups may carry underscores (`1_234_567_890`), a colon makes it
sexagesimal (`342935:31:30`), and a sign (`+1234567890`) or a decimal point
(`1234567890.0`) makes it yet another literal.

Quoted string forms keep working exactly as before: write
`login_customer_id: '123-456-7890'` and the value keeps the ordinary
customer-id rule, dashes and a leading zero included.

**Ignored, never applied:** every other key, including `developer_token`,
`endpoint`, `http_proxy`, `logging`, `json_key_file_path`,
`impersonated_email` and `use_proto_plus`. This server's destination, transport
and headers come from its own configuration, so a configuration file cannot
redirect it, add a proxy, turn on library logging or reintroduce a
developer-token header. A legacy `GOOGLE_ADS_DEVELOPER_TOKEN` is likewise
ignored for API access; see [authentication](authentication.md).

`GOOGLE_ADS_CREDENTIALS_PATH` and `GOOGLE_ADS_TOKEN_PATH` are not read under
this source.

## Where to go next

- [Configuration](configuration.md) for the full variable table and audit
  storage.
- [Authentication](authentication.md) for your Cloud project, consent screen,
  access levels and token lifetimes.
- [Errors](errors.md) for the `AUTH_CONFIG_*` and `AUTH_TOKEN_*` refusals.
