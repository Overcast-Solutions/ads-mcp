"""F094 repair: google-ads.yaml login id literal forms and credential parse failures.

Reproduces the verify findings S5 (an unquoted login_customer_id that YAML
reads as an octal, hex, underscored, sexagesimal or signed number was accepted
and silently became another id) and S6 (a deeply nested google-ads.yaml, or an
ADC file on Python 3.12, turned health_check and every tool into INTERNAL).

Provider substitution only, through the F090 credential-source double
(tests/credential_sources_oracle.py): Google's OAuth token endpoint and the
gRPC channel are synthetic; GoogleAdsClient construction, MCP dispatch and the
installed console are real. HOME, GOOGLE_APPLICATION_CREDENTIALS and
CLOUDSDK_CONFIG are isolated by the reused ``double`` fixture.

Contract pinned here: a refused numeric literal gets exactly the payload the
existing quote-it refusal produces for the same file, and a file whose parsing
raises gets exactly the payload of an existing unreadable-file failure for the
same location, apart from the credentials message.
"""
import json

import pytest

import harness as h
from test_credential_sources_contract import (  # noqa: F401 - ``double`` is a fixture
    CONTENT_MARKER,
    PLANTED,
    _events,
    _installed_env,
    _session,
    double,
    environment,
    rpcs,
    serve,
    write_yaml,
    yaml_text,
)

REFERENCE_LITERAL = "0123456701"  # an octal-looking id F090 already refuses with the quote-it error
DEEP_YAML = "nested: " + "[" * 20000 + "]" * 20000 + "\n"
DEEP_JSON = "[" * 30000 + "]" * 30000


def yaml_health(tmp_path, monkeypatch, login, **extra):
    write_yaml(tmp_path / "home/google-ads.yaml", yaml_text(login=login))
    return h.call(serve(monkeypatch, environment(tmp_path, "google_ads_yaml", **extra)), "health_check")


def without_message(payload):
    copy = json.loads(json.dumps(payload))
    copy.get("credentials", {}).pop("message", None)
    return copy


def assert_reported(payload):
    assert "error" not in payload, f"health_check must report a credential problem, never fail: {payload}"
    text = json.dumps(payload)
    assert "INTERNAL" not in text and "Traceback" not in text, text[:400]
    for planted in PLANTED:
        assert planted not in text


# ---------------------------------------------------------------------------
# S5: only ten plain decimal digits survive as an unquoted number.


LITERALS = [
    pytest.param(("010000000000",), "0123456789", "0123456789", id="octal-vs-unquoted-leading-zero-string"),
    pytest.param(("0x499602D2",), "123-456-7890", "1234567890", id="hex-vs-unquoted-dashed-string"),
    pytest.param(("1_234_567_890",), "1234567890", "1234567890", id="underscored-vs-plain-digits"),
    pytest.param(("342935:31:30",), "123-456-7890", "1234567890", id="sexagesimal-vs-unquoted-dashed-string"),
    pytest.param(("+1234567890", "1234567890.0"), "1234567890", "1234567890", id="signed-and-float-vs-plain-digits"),
]


@pytest.mark.parametrize("refused,control,expected", LITERALS)
def test_numeric_login_literals_other_than_ten_plain_digits_get_the_quote_it_refusal(
        tmp_path, monkeypatch, double, refused, control, expected):
    reference = yaml_health(tmp_path, monkeypatch, REFERENCE_LITERAL)
    credentials = reference["credentials"]
    assert credentials["state"] == "FAILED" and "login_customer_id" in credentials["message"], reference
    assert "quote" in credentials["message"].lower(), reference
    accepted = yaml_health(tmp_path, monkeypatch, control)
    assert accepted["status"] == "OK" and accepted["config"]["login_customer_id"] == expected, accepted
    assert rpcs(double)[-1]["metadata"].get("login-customer-id") == expected
    for literal in refused:
        tokens, calls = len(double.of("token")), len(rpcs(double))
        payload = yaml_health(tmp_path, monkeypatch, literal)
        assert payload == reference, (
            f"unquoted login_customer_id {literal} must get the existing quote-it refusal, got "
            f"status={payload.get('status')} login={payload.get('config', {}).get('login_customer_id')}")
        assert len(double.of("token")) == tokens and len(rpcs(double)) == calls, (
            f"{literal} reached the provider as another id")


@pytest.mark.parametrize("literal,expected", [("123-456-7890", "1234567890"), ("0123456789", "0123456789")],
                         ids=["unquoted-dashed", "unquoted-leading-zero"])
def test_string_forms_keep_the_f090_string_rule_while_their_numeric_neighbours_are_refused(
        tmp_path, monkeypatch, double, literal, expected):
    payload = yaml_health(tmp_path, monkeypatch, literal)
    assert payload["status"] == "OK" and payload["config"]["login_customer_id"] == expected, payload
    assert payload["config"]["credential_detail"]["login_customer_id_source"] == "yaml"
    assert rpcs(double)[-1]["metadata"].get("login-customer-id") == expected
    # The digits a numeric neighbour would read as must not slip through either.
    neighbour = {"1234567890": "+1234567890", "0123456789": "010000000000"}[expected]
    refused = yaml_health(tmp_path, monkeypatch, neighbour)
    assert refused["credentials"]["state"] == "FAILED", (
        f"unquoted {neighbour} is a YAML number that is not ten plain digits: {refused}")
    assert "quote" in refused["credentials"]["message"].lower()


# ---------------------------------------------------------------------------
# S6: a file whose parsing raises is an unreadable file, never INTERNAL.


@pytest.mark.parametrize("login", [None, h.LOGIN_CUSTOMER_ID_DASHED], ids=["no-env-login", "env-login"])
def test_deeply_nested_yaml_is_an_unreadable_file_for_health_and_tools(tmp_path, monkeypatch, double, login):
    path = tmp_path / "home/google-ads.yaml"
    env = environment(tmp_path, "google_ads_yaml", GOOGLE_ADS_LOGIN_CUSTOMER_ID=login)
    write_yaml(path, "client_id: [unclosed\n# " + CONTENT_MARKER + "\n")
    reference = h.call(serve(monkeypatch, env), "health_check")
    assert reference["credentials"]["code"] == "AUTH_CONFIG_UNREADABLE_FILE", reference
    deep = DEEP_YAML + yaml_text() + "# " + CONTENT_MARKER + "\n"
    assert len(deep.encode()) < 64 * 1024
    write_yaml(path, deep)
    server = serve(monkeypatch, env)
    payload = h.call(server, "health_check")
    assert_reported(payload)
    assert without_message(payload) == without_message(reference), payload
    assert payload["config"]["login_customer_id"] == (h.LOGIN_CUSTOMER_ID if login else None)
    assert "google-ads.yaml" in payload["credentials"]["message"]
    assert CONTENT_MARKER not in json.dumps(payload)
    tool = h.call(server, "run_gaql", {"query": "SELECT customer.id FROM customer"})
    assert h.error_of(tool)["code"] == "AUTH_CONFIG_UNREADABLE_FILE", tool
    assert CONTENT_MARKER not in json.dumps(tool)
    assert not double.of("token") and not rpcs(double)


def test_deeply_nested_adc_json_is_an_unreadable_file_for_health_and_tools(tmp_path, monkeypatch, double):
    """Reproduces on Python 3.12, whose JSON decoder raises RecursionError; 3.14 parses it."""
    path = tmp_path / "adc.json"
    env = environment(tmp_path, "adc", GOOGLE_APPLICATION_CREDENTIALS=str(path))
    path.write_bytes(b"{not json " + CONTENT_MARKER.encode())
    reference = h.call(serve(monkeypatch, env), "health_check")
    assert reference["credentials"]["code"] == "AUTH_CONFIG_UNREADABLE_FILE", reference
    path.write_text(DEEP_JSON)
    server = serve(monkeypatch, env)
    payload = h.call(server, "health_check")
    assert_reported(payload)
    assert without_message(payload) == without_message(reference), payload
    tool = h.call(server, "run_gaql", {"query": "SELECT customer.id FROM customer"})
    assert h.error_of(tool)["code"] == "AUTH_CONFIG_UNREADABLE_FILE", tool
    assert not double.of("token") and not rpcs(double)


def test_installed_console_reports_a_deeply_nested_yaml_as_an_unreadable_file(tmp_path):
    (tmp_path / "home").mkdir()
    write_yaml(tmp_path / "home/google-ads.yaml", DEEP_YAML + yaml_text() + "# " + CONTENT_MARKER + "\n")
    env, marker = _installed_env(tmp_path, {"GOOGLE_ADS_CREDENTIAL_SOURCE": "google_ads_yaml",
                                            "GOOGLE_ADS_CUSTOMER_ID": h.CUSTOMER_ID_DASHED})
    proc, _, (health, tool) = _session(env, [("health_check", {}),
                                             ("run_gaql", {"query": "SELECT customer.id FROM customer"})])
    assert proc.returncode == 0, proc.stderr
    assert_reported(health)
    assert health["status"] == "CONFIG_INVALID", health
    assert health["credentials"]["state"] == "FAILED", health
    assert health["credentials"]["code"] == "AUTH_CONFIG_UNREADABLE_FILE", health
    assert health["config"]["login_customer_id"] is None
    assert h.error_of(tool)["code"] == "AUTH_CONFIG_UNREADABLE_FILE", tool
    public = proc.stdout + proc.stderr
    assert CONTENT_MARKER not in public and "Traceback" not in public
    assert not [event for event in _events(marker) if event["event"] in ("token", "rpc", "network attempted")]


# ---------------------------------------------------------------------------
# Archives.
