"""F090: GOOGLE_ADS_CREDENTIAL_SOURCE = files | adc | google_ads_yaml.

Provider substitution only: credential_sources_oracle answers Google's OAuth
token endpoint and replaces the gRPC channel; GoogleAdsClient construction,
interceptors, error mapping, MCP dispatch and the installed console are real.
HOME, GOOGLE_APPLICATION_CREDENTIALS and CLOUDSDK_CONFIG are isolated in every
test. All identifiers and credential values are synthetic.

Contract pinned here (names the acceptance leaves open): non-files health adds
``config.credential_source`` and ``config.credential_detail`` and nulls both
path fields; ``config.unused_settings`` (sorted) appears only when non-empty,
for every source. Lazy credential problems use AUTH_CONFIG_* codes with a
message naming the problem. Source-value refusals are startup configuration
errors (exit 2) that never echo the supplied value.
"""
import json
import os
import re
import socket
import stat
import tomllib

import pytest

import credential_sources_oracle as cs
import harness as h
from ads_mcp import auth
from ads_mcp.config import ConfigError, load_config
from ads_mcp.server import create_server

ROOT = h.CONTRACT_DIR.parents[2]
SOURCES = ("files", "adc", "google_ads_yaml")
ADC_CLIENT = "oracle-own-desktop-client.apps.googleusercontent.com"
ADC_SECRET = "GOCSPX-oracle-adc-secret-7777"
ADC_REFRESH = "1//oracle-adc-refresh-token-ALPHA"
YAML_CLIENT = "oracle-yaml-client.apps.googleusercontent.com"
YAML_SECRET = "GOCSPX-oracle-yaml-secret-5151"
YAML_REFRESH = "1//oracle-yaml-refresh-token-BRAVO"
CONTENT_MARKER = "oracle-file-content-DO-NOT-ECHO"
PLANTED = (ADC_SECRET, ADC_REFRESH, YAML_SECRET, YAML_REFRESH, CONTENT_MARKER,
           h.FAKE_CLIENT_SECRET, h.FAKE_REFRESH_TOKEN, h.FAKE_DEVELOPER_TOKEN)
NEW_SOURCE = ["tests/test_credential_sources_contract.py", "tests/credential_sources_oracle.py"]


@pytest.fixture
def double(monkeypatch, tmp_path):
    """Offline provider double plus ambient-credential isolation for in-process servers."""
    for name in list(os.environ):
        if name.startswith(("GOOGLE_ADS_", "ADS_MCP_")) or name.lower() in (
                "http_proxy", "https_proxy", "all_proxy", "no_proxy"):
            monkeypatch.delenv(name, raising=False)
    h.isolate_credential_environment(monkeypatch, tmp_path / "home")
    (tmp_path / "home").mkdir(exist_ok=True)
    provider = cs.ProviderDouble()

    def deny(*args, **kwargs):
        provider.record("network attempted")
        raise OSError("credential-source oracle forbids networking")

    for name in ("connect", "connect_ex", "sendto"):
        monkeypatch.setattr(socket.socket, name, deny)
    monkeypatch.setattr(socket, "getaddrinfo", deny)
    monkeypatch.setattr(socket, "create_connection", deny)
    return provider.install(monkeypatch.setattr)


def environment(root, source=None, **extra):
    (root / "home").mkdir(parents=True, exist_ok=True)
    env = {"GOOGLE_ADS_CUSTOMER_ID": h.CUSTOMER_ID_DASHED, "ADS_MCP_AUDIT_LOG": str(h.audit_file(root)),
           "ADS_MCP_RETRY_BASE_SECONDS": "0.001", "HOME": str(root / "home")}
    if source is not None:
        env["GOOGLE_ADS_CREDENTIAL_SOURCE"] = source
    env.update(extra)
    return {key: value for key, value in env.items() if value is not None}


def mirror(monkeypatch, env):
    """Ambient Google variables follow the same mapping load_config receives."""
    for name in ("HOME", "GOOGLE_APPLICATION_CREDENTIALS", "CLOUDSDK_CONFIG"):
        if name in env:
            monkeypatch.setenv(name, env[name])
        else:
            monkeypatch.delenv(name, raising=False)


def serve(monkeypatch, env, *, clock=None):
    mirror(monkeypatch, env)
    try:
        config = load_config(env)
    except ConfigError as exc:
        pytest.fail(f"otherwise valid {env.get('GOOGLE_ADS_CREDENTIAL_SOURCE', 'files')} configuration "
                    f"was refused at startup: {exc}")
    return create_server(config, clock=clock or h.FakeClock())


def guardrails(root):
    return {"read_only": True, "require_dry_run": True, "max_daily_budget": None,
            "max_bid_increase_pct": None, "max_first_bid": None, "plan_ttl_seconds": 900,
            "row_limit": 1000, "audit_log": str(h.audit_file(root))}


def files_health(root, cred, tok, *, login=h.LOGIN_CUSTOMER_ID, developer="present"):
    """Today's exact files-source payload (as pinned by the F064 health golden)."""
    return {"status": "OK",
            "config": {"customer_id": h.CUSTOMER_ID, "login_customer_id": login, "developer_token": developer,
                       "credentials_path": str(cred), "token_path": str(tok)},
            "credentials": {"state": "OK"}, "guardrails": guardrails(root)}


def source_health(root, source, detail, *, login=None, developer="absent", unused=None):
    config = {"customer_id": h.CUSTOMER_ID, "login_customer_id": login, "developer_token": developer,
              "credentials_path": None, "token_path": None, "credential_source": source,
              "credential_detail": detail}
    if unused:
        config["unused_settings"] = sorted(unused)
    return {"status": "OK", "config": config, "credentials": {"state": "OK"}, "guardrails": guardrails(root)}


def adc(path, client_id=ADC_CLIENT, secret=ADC_SECRET, refresh=ADC_REFRESH, **extra):
    return cs.write_json(path, cs.authorized_user(client_id, secret, refresh, **extra))


def yaml_text(*, login="'111-222-3333'", extra=""):
    lines = [f"client_id: {YAML_CLIENT}", f"client_secret: {YAML_SECRET}", f"refresh_token: '{YAML_REFRESH}'"]
    if login is not None:
        lines.append(f"login_customer_id: {login}")
    return "\n".join(lines) + "\n" + extra


def write_yaml(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(text, bytes):
        path.write_bytes(text)
    else:
        path.write_text(text)
    os.chmod(path, 0o600)
    return path


def rpcs(double):
    return double.of("rpc")


def assert_clean_provider_use(double, *, refresh=None, client_id=None, login=None):
    assert not double.of("metadata probe") and not double.of("unexpected http"), double.events
    assert not double.of("network attempted")
    calls = rpcs(double)
    assert calls, "health must perform an authenticated provider read"
    for call in calls:
        metadata = call["metadata"]
        assert "developer-token" not in metadata, "a developer-token header was sent"
        assert "linked-customer-id" not in metadata
        assert metadata.get("authorization", "").startswith("Bearer ya29.oracle-access-token-")
        if login is None:
            assert "login-customer-id" not in metadata
        else:
            assert metadata.get("login-customer-id") == login
    tokens = double.of("token")
    assert tokens and all(event["grant_type"] == "refresh_token" and not event["proxies"] for event in tokens)
    if refresh is not None:
        assert {event["refresh_token"] for event in tokens} == {refresh}
        assert {event["client_id"] for event in tokens} == {client_id}
    for channel in double.of("channel"):
        assert channel["target"].split(":")[0] == "googleads.googleapis.com"


def assert_lazy_failure(payload, *, keywords, code_prefix="AUTH_CONFIG_"):
    assert payload["status"] == "CONFIG_INVALID", payload
    credentials = payload["credentials"]
    assert credentials["state"] == "FAILED" and credentials["code"].startswith(code_prefix), credentials
    message = credentials["message"]
    lowered = message.lower()
    assert any(keyword.lower() in lowered for keyword in keywords), (keywords, message)
    for planted in PLANTED:
        assert planted not in json.dumps(payload)
    assert "Traceback" not in json.dumps(payload)
    return credentials


# ---------------------------------------------------------------------------
# The source variable.


BAD_SOURCES = ["ADC", "Files", "file", "yaml", "google-ads-yaml", "google_ads.yaml", "adc,files", "adc files",
               "metadata", "service_account", "\x00adc", "adc\nfiles", "DO-NOT-ECHO-" + "s" * 200]


@pytest.mark.parametrize("value", BAD_SOURCES, ids=lambda value: repr(value)[:24])
def test_bad_source_value_is_refused_first_naming_the_variable_and_allowed_values(tmp_path, value):
    valid = h.google_ads_env(tmp_path)
    with pytest.raises(ConfigError) as refused:
        load_config({**valid, "GOOGLE_ADS_CREDENTIAL_SOURCE": value})
    message = str(refused.value)
    assert "GOOGLE_ADS_CREDENTIAL_SOURCE" in message, message
    assert all(allowed in message for allowed in SOURCES), message
    assert "DO-NOT-ECHO" not in message and "\x00" not in message and "\n" not in message
    with pytest.raises(ConfigError) as first:
        load_config({"GOOGLE_ADS_CREDENTIAL_SOURCE": value})
    assert "GOOGLE_ADS_CREDENTIAL_SOURCE" in str(first.value), "the source must be validated before other settings"
    assert "GOOGLE_ADS_CUSTOMER_ID" not in str(first.value)


@pytest.mark.parametrize("value", ["yaml", "DO-NOT-ECHO-source-" + "x" * 80])
def test_installed_console_refuses_a_bad_source_with_exit_2_and_one_line(tmp_path, value):
    env = {**h.google_ads_env(tmp_path), "GOOGLE_ADS_CREDENTIAL_SOURCE": value,
           "ADS_MCP_AUDIT_LOG": str(h.audit_file(tmp_path)), "HOME": str(tmp_path)}
    proc = h.run_console("ads-mcp", env_overlay=env, timeout=30)
    lines = [line for line in proc.stderr.splitlines() if line.strip()]
    assert proc.returncode == 2 and not proc.stdout, (proc.returncode, proc.stderr)
    assert len(lines) == 1 and lines[0].startswith("ads-mcp: configuration error:"), proc.stderr
    assert "GOOGLE_ADS_CREDENTIAL_SOURCE" in lines[0] and all(allowed in lines[0] for allowed in SOURCES)
    assert "DO-NOT-ECHO" not in proc.stderr and "Traceback" not in proc.stderr
    h.assert_no_secrets(proc.stderr)


def test_values_are_trimmed_and_blank_means_files(tmp_path, monkeypatch, double):
    cred, tok = h.write_oauth_files(tmp_path)
    adc(tmp_path / "home/.config/gcloud/application_default_credentials.json")
    for value in [None, "", "   ", "files", " files\t"]:
        env = environment(tmp_path, value, GOOGLE_ADS_CREDENTIALS_PATH=str(cred), GOOGLE_ADS_TOKEN_PATH=str(tok),
                          GOOGLE_ADS_LOGIN_CUSTOMER_ID=h.LOGIN_CUSTOMER_ID_DASHED,
                          GOOGLE_ADS_DEVELOPER_TOKEN=h.FAKE_DEVELOPER_TOKEN)
        assert h.call(serve(monkeypatch, env), "health_check") == files_health(tmp_path, cred, tok), value
    env = environment(tmp_path, "\tadc ")
    payload = h.call(serve(monkeypatch, env), "health_check")
    assert payload["config"].get("credential_source") == "adc", payload
    yaml = write_yaml(tmp_path / "home/google-ads.yaml", yaml_text())
    payload = h.call(serve(monkeypatch, environment(tmp_path, " google_ads_yaml ")), "health_check")
    assert payload["config"].get("credential_source") == "google_ads_yaml" and payload["status"] == "OK", payload
    assert payload["config"]["credential_detail"]["file"] == str(yaml)


@pytest.mark.parametrize("source", ["adc", "google_ads_yaml"])
def test_customer_id_stays_required_and_path_variables_are_not(tmp_path, monkeypatch, double, source):
    adc(tmp_path / "home/.config/gcloud/application_default_credentials.json")
    write_yaml(tmp_path / "home/google-ads.yaml", yaml_text())
    env = environment(tmp_path, source)
    mirror(monkeypatch, env)
    try:
        load_config(env)
    except ConfigError as exc:
        pytest.fail(f"{source} must not require the files-source path variables: {exc}")
    without = {key: value for key, value in env.items() if key != "GOOGLE_ADS_CUSTOMER_ID"}
    with pytest.raises(ConfigError, match="GOOGLE_ADS_CUSTOMER_ID"):
        load_config(without)


# ---------------------------------------------------------------------------
# Application Default Credentials.


@pytest.mark.parametrize("winner", ["explicit", "cloudsdk", "home"])
def test_adc_lookup_order_and_exact_health(tmp_path, monkeypatch, double, winner):
    explicit = adc(tmp_path / "explicit/adc.json", client_id="explicit-" + ADC_CLIENT, refresh=ADC_REFRESH + "-explicit")
    cloudsdk = adc(tmp_path / "cloudsdk/application_default_credentials.json",
                   client_id="cloudsdk-" + ADC_CLIENT, refresh=ADC_REFRESH + "-cloudsdk")
    home = adc(tmp_path / "home/.config/gcloud/application_default_credentials.json",
               client_id="home-" + ADC_CLIENT, refresh=ADC_REFRESH + "-home")
    env = environment(tmp_path, "adc", CLOUDSDK_CONFIG=str(cloudsdk.parent),
                      GOOGLE_APPLICATION_CREDENTIALS=str(explicit) if winner == "explicit" else None)
    if winner == "home":
        env.pop("CLOUDSDK_CONFIG")
    chosen = {"explicit": explicit, "cloudsdk": cloudsdk, "home": home}[winner]
    payload = h.call(serve(monkeypatch, env), "health_check")
    assert payload == source_health(tmp_path, "adc", {"type": "authorized_user", "file": str(chosen)}), payload
    assert_clean_provider_use(double, refresh=ADC_REFRESH + "-" + winner, client_id=winner + "-" + ADC_CLIENT)


def test_adc_without_a_file_is_a_named_lazy_error_without_metadata_probe(tmp_path, monkeypatch, double):
    env = environment(tmp_path, "adc", CLOUDSDK_CONFIG=str(tmp_path / "empty-cloudsdk"))
    server = serve(monkeypatch, env)
    assert "health_check" in h.tool_names(server)
    payload = h.call(server, "health_check")
    credentials = assert_lazy_failure(payload, keywords=["GOOGLE_APPLICATION_CREDENTIALS",
                                                         "application_default_credentials.json"])
    error = h.error_of(h.call(server, "run_gaql", {"query": "SELECT customer.id FROM customer"}))
    assert error["code"] == credentials["code"]
    assert not double.of("token") and not rpcs(double)
    assert not double.of("metadata probe") and not double.of("unexpected http") and not double.of("network attempted")


@pytest.mark.parametrize("kind", ["service_account", "external_account", "impersonated_service_account",
                                  "external_account_authorized_user", "gdch_service_account", "Authorized_User", None])
def test_adc_accepts_only_authorized_user_and_refuses_others_by_name(tmp_path, monkeypatch, double, kind):
    control = tmp_path / "control.json"
    adc(control)
    payload = h.call(serve(monkeypatch, environment(tmp_path, "adc", GOOGLE_APPLICATION_CREDENTIALS=str(control))),
                     "health_check")
    assert payload["status"] == "OK" and payload["config"].get("credential_source") == "adc", payload
    value = {"client_id": ADC_CLIENT, "client_secret": ADC_SECRET, "refresh_token": ADC_REFRESH,
             "private_key": "-----BEGIN PRIVATE KEY-----" + CONTENT_MARKER, "client_email": CONTENT_MARKER + "@example.invalid"}
    if kind is not None:
        value["type"] = kind
    refused = cs.write_json(tmp_path / "refused.json", value)
    before = len(double.of("token"))
    payload = h.call(serve(monkeypatch, environment(tmp_path / "refused-root", "adc",
                                                     GOOGLE_APPLICATION_CREDENTIALS=str(refused))), "health_check")
    assert_lazy_failure(payload, keywords=[kind or "type", "authorized_user"])
    assert "authorized_user" in payload["credentials"]["message"]
    if kind is not None:
        assert kind in payload["credentials"]["message"], "the refused type must be named"
    assert len(double.of("token")) == before and not double.of("metadata probe")


def test_adc_refuses_gcloud_builtin_client_with_own_client_guidance(tmp_path, monkeypatch, double):
    builtin = adc(tmp_path / "gcloud.json", client_id=cs.GCLOUD_CLIENT_ID)
    payload = h.call(serve(monkeypatch, environment(tmp_path, "adc", GOOGLE_APPLICATION_CREDENTIALS=str(builtin))),
                     "health_check")
    credentials = assert_lazy_failure(payload, keywords=["--client-id-file"])
    assert "--client-id-file" in credentials["message"] and "adwords" in credentials["message"]
    assert not double.of("token") and not rpcs(double)
    own = adc(tmp_path / "own.json")
    ok = h.call(serve(monkeypatch, environment(tmp_path / "own-root", "adc", GOOGLE_APPLICATION_CREDENTIALS=str(own))),
                "health_check")
    assert ok["status"] == "OK" and ok["config"]["credential_detail"] == {"type": "authorized_user", "file": str(own)}


@pytest.mark.parametrize("content,keyword", [
    (b"{not json " + CONTENT_MARKER.encode(), "json"),
    (json.dumps([CONTENT_MARKER]).encode(), "object"),
    (json.dumps({"type": "authorized_user", "client_id": ADC_CLIENT, "client_secret": ADC_SECRET}).encode(), "refresh_token"),
    (json.dumps({"type": "authorized_user", "client_id": ADC_CLIENT, "client_secret": "  ",
                 "refresh_token": ADC_REFRESH}).encode(), "client_secret"),
    (json.dumps({"type": "authorized_user", "client_id": 12345, "client_secret": ADC_SECRET,
                 "refresh_token": ADC_REFRESH}).encode(), "client_id"),
])
def test_adc_malformed_files_are_named_errors_without_contents(tmp_path, monkeypatch, double, content, keyword):
    path = tmp_path / "adc.json"
    path.write_bytes(content)
    payload = h.call(serve(monkeypatch, environment(tmp_path, "adc", GOOGLE_APPLICATION_CREDENTIALS=str(path))),
                     "health_check")
    assert_lazy_failure(payload, keywords=[keyword])
    assert not double.of("token") and not double.of("metadata probe")


# ---------------------------------------------------------------------------
# google-ads.yaml.


@pytest.mark.parametrize("location", ["explicit", "home"])
def test_yaml_source_reads_oauth_keys_and_login_id_with_exact_health(tmp_path, monkeypatch, double, location):
    explicit = write_yaml(tmp_path / "config/custom.yaml", yaml_text(login="'555-666-7777'"))
    home = write_yaml(tmp_path / "home/google-ads.yaml", yaml_text(login="1112223333"))
    env = environment(tmp_path, "google_ads_yaml",
                      GOOGLE_ADS_CONFIGURATION_FILE_PATH=str(explicit) if location == "explicit" else None)
    login = h.OTHER_CUSTOMER_ID if location == "explicit" else h.LOGIN_CUSTOMER_ID
    chosen = explicit if location == "explicit" else home
    payload = h.call(serve(monkeypatch, env), "health_check")
    assert payload == source_health(tmp_path, "google_ads_yaml",
                                    {"file": str(chosen), "login_customer_id_source": "yaml"}, login=login), payload
    assert_clean_provider_use(double, refresh=YAML_REFRESH, client_id=YAML_CLIENT, login=login)


@pytest.mark.parametrize("override,expected,source", [
    (h.LOGIN_CUSTOMER_ID_DASHED, h.LOGIN_CUSTOMER_ID, "env"), (None, h.OTHER_CUSTOMER_ID, "yaml")])
def test_environment_login_id_overrides_the_yaml_value(tmp_path, monkeypatch, double, override, expected, source):
    path = write_yaml(tmp_path / "home/google-ads.yaml", yaml_text(login="'555-666-7777'"))
    env = environment(tmp_path, "google_ads_yaml", GOOGLE_ADS_LOGIN_CUSTOMER_ID=override)
    payload = h.call(serve(monkeypatch, env), "health_check")
    assert payload == source_health(tmp_path, "google_ads_yaml",
                                    {"file": str(path), "login_customer_id_source": source}, login=expected), payload
    assert_clean_provider_use(double, refresh=YAML_REFRESH, client_id=YAML_CLIENT, login=expected)


def test_yaml_without_login_id_reports_none_and_sends_no_login_header(tmp_path, monkeypatch, double):
    path = write_yaml(tmp_path / "home/google-ads.yaml", yaml_text(login=None))
    payload = h.call(serve(monkeypatch, environment(tmp_path, "google_ads_yaml")), "health_check")
    assert payload == source_health(tmp_path, "google_ads_yaml", {"file": str(path), "login_customer_id_source": "none"})
    assert_clean_provider_use(double, refresh=YAML_REFRESH, client_id=YAML_CLIENT, login=None)


def test_hostile_yaml_settings_are_ignored(tmp_path, monkeypatch, double):
    hostile_log = tmp_path / "hostile-sdk.log"
    adc(tmp_path / "home/.config/gcloud/application_default_credentials.json")
    extra = (
        "developer_token: " + h.FAKE_DEVELOPER_TOKEN + "\n"
        "endpoint: synthetic-hostile.invalid:443\n"
        "http_proxy: http://127.0.0.1:9\n"
        "json_key_file_path: " + str(tmp_path / "missing-key.json") + "\n"
        "use_application_default_credentials: true\n"
        "use_cloud_org_for_api_access: false\n"
        "linked_customer_id: '5556667777'\n"
        "use_proto_plus: false\n"
        "logging:\n"
        "  version: 1\n"
        "  disable_existing_loggers: true\n"
        "  handlers:\n"
        "    hostile:\n"
        "      class: logging.FileHandler\n"
        "      filename: " + str(hostile_log) + "\n"
        "  loggers:\n"
        "    '':\n"
        "      handlers: [hostile]\n"
        "      level: DEBUG\n")
    path = write_yaml(tmp_path / "home/google-ads.yaml", yaml_text(extra=extra))
    server = serve(monkeypatch, environment(tmp_path, "google_ads_yaml"))
    payload = h.call(server, "health_check")
    assert payload == source_health(tmp_path, "google_ads_yaml",
                                    {"file": str(path), "login_customer_id_source": "yaml"}, login=h.LOGIN_CUSTOMER_ID)
    rows = h.expect_ok(h.call(server, "run_gaql", {"query": "SELECT customer.id FROM customer"}))["rows"]
    assert rows == [{"customer": {"id": int(h.CUSTOMER_ID)}}]
    assert_clean_provider_use(double, refresh=YAML_REFRESH, client_id=YAML_CLIENT, login=h.LOGIN_CUSTOMER_ID)
    assert not hostile_log.exists(), "yaml logging configuration was applied"


@pytest.mark.parametrize("login,expected", [
    ("'123-456-7890'", "1234567890"), ("1234567890", "1234567890"), ("'0123456789'", "0123456789"),
    ('"9876543210"', "9876543210")])
def test_yaml_login_id_accepts_quoted_strings_and_exact_ten_digit_ints(tmp_path, monkeypatch, double, login, expected):
    write_yaml(tmp_path / "home/google-ads.yaml", yaml_text(login=login))
    payload = h.call(serve(monkeypatch, environment(tmp_path, "google_ads_yaml")), "health_check")
    assert payload["status"] == "OK" and payload["config"]["login_customer_id"] == expected, payload
    assert rpcs(double)[-1]["metadata"].get("login-customer-id") == expected


@pytest.mark.parametrize("login,keyword", [("0123456701", "quote"), ("123456789", "quote"), ("12345678901", "quote"),
                                           ("'12345'", "login_customer_id"), ("[1234567890]", "login_customer_id")])
def test_yaml_login_id_refusals_ask_to_quote_or_name_the_key(tmp_path, monkeypatch, double, login, keyword):
    write_yaml(tmp_path / "control/google-ads.yaml", yaml_text())
    control = h.call(serve(monkeypatch, environment(tmp_path / "control", "google_ads_yaml",
                                                    HOME=str(tmp_path / "control"))), "health_check")
    assert control["status"] == "OK" and control["config"].get("credential_source") == "google_ads_yaml", control
    write_yaml(tmp_path / "home/google-ads.yaml", yaml_text(login=login))
    before = len(double.of("token"))
    payload = h.call(serve(monkeypatch, environment(tmp_path, "google_ads_yaml")), "health_check")
    assert_lazy_failure(payload, keywords=[keyword])
    assert len(double.of("token")) == before


def _large(size):
    body = yaml_text().encode()
    filler = b"# " + b"x" * (size - len(body) - 3) + b"\n"
    data = body + filler
    assert len(data) == size
    return data


@pytest.mark.parametrize("content,keyword", [
    ("anchors", "alias|anchor"), ("duplicate", "duplicate"), ("list", "mapping"), ("scalar", "mapping"),
    ("empty", "mapping"), ("null", "mapping"), ("utf8", "utf-8"), ("oversize", "64"), ("directory", "regular"),
    ("missing", "google-ads.yaml"),
])
def test_yaml_file_shape_refusals_are_named_and_never_echo_contents(tmp_path, monkeypatch, double, content, keyword):
    path = tmp_path / "home/google-ads.yaml"
    values = {
        "anchors": yaml_text() + f"extra: &secret '{CONTENT_MARKER}'\nagain: *secret\n",
        "duplicate": yaml_text() + f"client_secret: {CONTENT_MARKER}\n",
        "list": f"- {CONTENT_MARKER}\n- {YAML_SECRET}\n",
        "scalar": f"{CONTENT_MARKER}\n",
        "empty": "",
        "null": "~\n",
        "utf8": yaml_text().encode() + b"comment: \xff\xfe " + CONTENT_MARKER.encode() + b"\n",
        "oversize": _large(64 * 1024 + 1),
    }
    if content == "directory":
        path.mkdir(parents=True)
    elif content != "missing":
        write_yaml(path, values[content])
    payload = h.call(serve(monkeypatch, environment(tmp_path, "google_ads_yaml")), "health_check")
    keywords = keyword.split("|") + (["GOOGLE_ADS_CONFIGURATION_FILE_PATH"] if content == "missing" else [])
    assert_lazy_failure(payload, keywords=keywords)
    assert not double.of("token") and not rpcs(double)


def test_yaml_at_exactly_64_kib_is_accepted(tmp_path, monkeypatch, double):
    path = write_yaml(tmp_path / "home/google-ads.yaml", _large(64 * 1024))
    payload = h.call(serve(monkeypatch, environment(tmp_path, "google_ads_yaml")), "health_check")
    assert payload["status"] == "OK" and payload["config"]["credential_detail"]["file"] == str(path), payload


@pytest.mark.parametrize("key,value", [("client_id", "12345"), ("client_secret", "'   '"), ("refresh_token", "[a, b]"),
                                       ("client_id", None), ("refresh_token", "null")])
def test_yaml_oauth_values_must_be_nonblank_strings(tmp_path, monkeypatch, double, key, value):
    lines = [line for line in yaml_text().splitlines() if not line.startswith(key + ":")]
    if value is not None:
        lines.append(f"{key}: {value}")
    write_yaml(tmp_path / "home/google-ads.yaml", "\n".join(lines) + "\n")
    payload = h.call(serve(monkeypatch, environment(tmp_path, "google_ads_yaml")), "health_check")
    assert_lazy_failure(payload, keywords=[key])
    assert key in payload["credentials"]["message"]
    assert not double.of("token")


# ---------------------------------------------------------------------------
# Unused settings, exact files health, redaction.


def test_unused_settings_are_ignored_and_listed_per_source(tmp_path, monkeypatch, double):
    cred, tok = h.write_oauth_files(tmp_path)
    yaml = write_yaml(tmp_path / "config/google-ads.yaml", yaml_text())
    home_adc = adc(tmp_path / "home/.config/gcloud/application_default_credentials.json")
    paths = {"GOOGLE_ADS_CREDENTIALS_PATH": str(cred), "GOOGLE_ADS_TOKEN_PATH": str(tok),
             "GOOGLE_ADS_LOGIN_CUSTOMER_ID": h.LOGIN_CUSTOMER_ID_DASHED,
             "GOOGLE_ADS_DEVELOPER_TOKEN": h.FAKE_DEVELOPER_TOKEN}
    files = h.call(serve(monkeypatch, environment(tmp_path, "files", GOOGLE_ADS_CONFIGURATION_FILE_PATH=str(yaml), **paths)),
                   "health_check")
    expected = files_health(tmp_path, cred, tok)
    expected["config"]["unused_settings"] = ["GOOGLE_ADS_CONFIGURATION_FILE_PATH"]
    assert files == expected, files
    exact = h.call(serve(monkeypatch, environment(tmp_path, "files", GOOGLE_APPLICATION_CREDENTIALS=str(home_adc),
                                                  CLOUDSDK_CONFIG=str(home_adc.parent), **paths)), "health_check")
    assert exact == files_health(tmp_path, cred, tok), "files health must stay exactly today's"
    missing = {"GOOGLE_ADS_CREDENTIALS_PATH": str(tmp_path / "absent-client.json"),
               "GOOGLE_ADS_TOKEN_PATH": str(tmp_path / "absent-token.json")}
    adc_payload = h.call(serve(monkeypatch, environment(tmp_path, "adc", GOOGLE_ADS_CONFIGURATION_FILE_PATH=str(yaml),
                                                        **missing)), "health_check")
    assert adc_payload == source_health(tmp_path, "adc", {"type": "authorized_user", "file": str(home_adc)}, unused=[
        "GOOGLE_ADS_CONFIGURATION_FILE_PATH", "GOOGLE_ADS_CREDENTIALS_PATH", "GOOGLE_ADS_TOKEN_PATH"]), adc_payload
    yaml_payload = h.call(serve(monkeypatch, environment(tmp_path, "google_ads_yaml",
                                                         GOOGLE_ADS_CONFIGURATION_FILE_PATH=str(yaml), **missing)),
                          "health_check")
    assert yaml_payload == source_health(tmp_path, "google_ads_yaml", {"file": str(yaml), "login_customer_id_source": "yaml"},
                                         login=h.LOGIN_CUSTOMER_ID,
                                         unused=["GOOGLE_ADS_CREDENTIALS_PATH", "GOOGLE_ADS_TOKEN_PATH"]), yaml_payload


@pytest.mark.parametrize("source", SOURCES)
def test_every_source_redacts_secrets_and_later_access_tokens_and_never_sends_developer_tokens(
        tmp_path, monkeypatch, double, source):
    cred, tok = h.write_oauth_files(tmp_path)
    adc(tmp_path / "home/.config/gcloud/application_default_credentials.json")
    write_yaml(tmp_path / "home/google-ads.yaml", yaml_text(extra=f"developer_token: {h.FAKE_DEVELOPER_TOKEN}\n"))
    extra = {"GOOGLE_ADS_DEVELOPER_TOKEN": h.FAKE_DEVELOPER_TOKEN}
    if source == "files":
        extra.update(GOOGLE_ADS_CREDENTIALS_PATH=str(cred), GOOGLE_ADS_TOKEN_PATH=str(tok))
    refresh, secret = {"files": (h.FAKE_REFRESH_TOKEN, h.FAKE_CLIENT_SECRET), "adc": (ADC_REFRESH, ADC_SECRET),
                       "google_ads_yaml": (YAML_REFRESH, YAML_SECRET)}[source]
    double.settings = {"short_lived": True}
    server = serve(monkeypatch, environment(tmp_path, source, ADS_MCP_READ_ONLY="false", **extra))
    assert h.call(server, "health_check")["status"] == "OK"
    for _ in range(2):
        h.expect_ok(h.call(server, "run_gaql", {"query": "SELECT customer.id FROM customer"}))
    double.settings = {"short_lived": True, "fault": True, "echo_refresh": refresh, "echo_secret": secret}
    result = h.call_result(server, "run_gaql", {"query": "SELECT customer.id FROM customer"})
    error = h.error_of(h.payload_of(result))
    issued = [event["access_token"] for event in double.of("token")]
    assert len(issued) >= 3, "short-lived synthetic tokens must be refreshed after startup"
    text = h.result_text(result) + json.dumps(h.call(server, "health_check"))
    if h.audit_file(tmp_path).exists():
        text += h.audit_file(tmp_path).read_text()
    for value in (*issued, refresh, secret, h.FAKE_DEVELOPER_TOKEN):
        assert value not in text, f"{source}: credential material leaked ({value[:12]}...)"
    assert "synthetic provider echo" in error["message"]
    for call in rpcs(double):
        assert "developer-token" not in call["metadata"]


# ---------------------------------------------------------------------------
# Installed console.


def _session(env, calls, timeout=60):
    frames = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "credential-oracle", "version": "0"}}},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}]
    for index, (name, args) in enumerate(calls, start=3):
        frames.append({"jsonrpc": "2.0", "id": index, "method": "tools/call", "params": {"name": name, "arguments": args}})
    proc = h._run_console_holding_pipe("ads-mcp", env_overlay=env,
                                       input_text="".join(json.dumps(frame) + "\n" for frame in frames), timeout=timeout)
    replies = {message["id"]: message for message in map(json.loads, proc.stdout.splitlines()) if "id" in message}
    results = []
    for index in range(3, 3 + len(calls)):
        result = replies[index]["result"]
        value = result.get("structuredContent")
        if value is None:
            value = json.loads("".join(part.get("text", "") for part in result["content"]))
        results.append(value.get("result", value) if set(value) == {"result"} else value)
    return proc, replies[2]["result"]["tools"], results


def _installed_env(tmp_path, overlay):
    injection = tmp_path / "injection"
    injection.mkdir(exist_ok=True)
    (injection / "sitecustomize.py").write_text(cs.INJECTION)
    marker, control = tmp_path / "provider-events.jsonl", tmp_path / "provider-control.json"
    control.write_text("{}")
    env = {"PYTHONPATH": str(injection), "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1",
           "ORACLE_TESTS": str(h.CONTRACT_DIR.parents[1]), "ORACLE_MARKER": str(marker),
           "ORACLE_CONTROL": str(control), "HOME": str(tmp_path / "home"),
           "ADS_MCP_AUDIT_LOG": str(h.audit_file(tmp_path)), "ADS_MCP_RETRY_BASE_SECONDS": "0.001", **overlay}
    return env, marker


def _events(marker):
    return [json.loads(line) for line in marker.read_text().splitlines() if line.strip()] if marker.exists() else []


@pytest.mark.parametrize("source,customer", [("adc", h.CUSTOMER_ID_DASHED), ("google_ads_yaml", "555-666-7777")])
def test_installed_console_serves_non_files_sources_for_two_accounts(tmp_path, source, customer):
    (tmp_path / "home").mkdir()
    adc_path = adc(tmp_path / "home/.config/gcloud/application_default_credentials.json")
    yaml_path = write_yaml(tmp_path / "home/google-ads.yaml", yaml_text(login=None))
    env, marker = _installed_env(tmp_path, {"GOOGLE_ADS_CREDENTIAL_SOURCE": source, "GOOGLE_ADS_CUSTOMER_ID": customer,
                                            "GOOGLE_ADS_LOGIN_CUSTOMER_ID": h.LOGIN_CUSTOMER_ID_DASHED})
    cid = customer.replace("-", "")
    other = h.OTHER_CUSTOMER_ID if cid != h.OTHER_CUSTOMER_ID else h.CUSTOMER_ID
    proc, tools, (health, own, cross) = _session(env, [
        ("health_check", {}), ("run_gaql", {"query": "SELECT customer.id FROM customer"}),
        ("run_gaql", {"query": "SELECT customer.id FROM customer", "customer_id": other})])
    assert proc.returncode == 0 and "Traceback" not in proc.stdout + proc.stderr, proc.stderr
    assert "health_check" in {tool["name"] for tool in tools}
    detail = ({"type": "authorized_user", "file": str(adc_path)} if source == "adc"
              else {"file": str(yaml_path), "login_customer_id_source": "env"})
    assert health["status"] == "OK" and health["config"]["credential_source"] == source, health
    assert health["config"]["credential_detail"] == detail and health["config"]["customer_id"] == cid
    assert health["config"]["credentials_path"] is None and health["config"]["token_path"] is None
    assert own["rows"] == [{"customer": {"id": int(cid)}}] and cross["rows"] == [{"customer": {"id": int(other)}}]
    events = _events(marker)
    assert any(event["event"] == "loaded" for event in events)
    assert not [event for event in events if event["event"] in ("network attempted", "metadata probe", "unexpected http")]
    calls = [event for event in events if event["event"] == "rpc"]
    assert {call["customer_id"] for call in calls} == {cid, other}
    assert all(call["metadata"].get("login-customer-id") == h.LOGIN_CUSTOMER_ID for call in calls)
    assert all("developer-token" not in call["metadata"] for call in calls)
    tokens = [event for event in events if event["event"] == "token"]
    assert {event["refresh_token"] for event in tokens} == {ADC_REFRESH if source == "adc" else YAML_REFRESH}
    public = proc.stdout + proc.stderr
    for value in (*PLANTED, *[event["access_token"] for event in tokens]):
        assert value not in public
    helper = h.run_console("ads-mcp-generate-token", env_overlay={
        "GOOGLE_ADS_CREDENTIAL_SOURCE": source, "GOOGLE_APPLICATION_CREDENTIALS": str(adc_path),
        "GOOGLE_ADS_CONFIGURATION_FILE_PATH": str(yaml_path), "HOME": str(tmp_path / "home")}, timeout=30)
    assert helper.returncode == 2 and "AUTH_CONFIG_INCOMPLETE" in helper.stderr, "generate-token stays files-only"


def test_installed_yaml_fifo_is_refused_without_blocking(tmp_path):
    (tmp_path / "home").mkdir()
    fifo = tmp_path / "home/google-ads.yaml"
    os.mkfifo(fifo)
    assert stat.S_ISFIFO(fifo.stat().st_mode)
    env, marker = _installed_env(tmp_path, {"GOOGLE_ADS_CREDENTIAL_SOURCE": "google_ads_yaml",
                                            "GOOGLE_ADS_CUSTOMER_ID": h.CUSTOMER_ID_DASHED})
    proc, _, (health,) = _session(env, [("health_check", {})], timeout=30)
    assert proc.returncode == 0, proc.stderr
    assert_lazy_failure(health, keywords=["regular"])
    assert not [event for event in _events(marker) if event["event"] in ("token", "rpc")]


# ---------------------------------------------------------------------------
# Declared surface: functions, dependencies, documentation and archives.


def test_dependencies_documentation_functions_and_archives_are_declared():
    from offline_contract import load_script

    project = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]
    declared = {re.split(r"[<>=!~ ;\[]", item, maxsplit=1)[0].lower().replace("_", "-"): item
                for item in project["dependencies"]}
    assert "pyyaml" in declared and ">=6.0.3" in declared["pyyaml"].replace(" ", ""), project["dependencies"]
    assert "google-auth" in declared and ">=2.61.0" in declared["google-auth"].replace(" ", "")
    minimum = (ROOT / "requirements-min.txt").read_text().splitlines()
    assert any(line.strip().lower() == "pyyaml==6.0.3" for line in minimum)
    assert "google-auth==2.61.0" in [line.strip().lower() for line in minimum]
    configuration = (ROOT / "docs/configuration.md").read_text()
    for variable in ("GOOGLE_ADS_CREDENTIAL_SOURCE", "GOOGLE_ADS_CONFIGURATION_FILE_PATH"):
        assert re.search(r"^\| `" + variable + r"` \|", configuration, re.M), f"docs/configuration.md lacks a {variable} row"
    for name in ("load_oauth_material", "build_client", "collect_secrets"):
        assert callable(getattr(auth, name))
    assert callable(load_config)
    checker = load_script("check_release_archives")
    assert set(NEW_SOURCE) <= set(checker.REQUIRED_SOURCE)
    with pytest.raises(ValueError):
        checker.check_inventory(sorted(set(checker.REQUIRED_SOURCE) - {NEW_SOURCE[0]}), source_archive=True)
