"""Credential sources: OAuth files, Application Default Credentials, google-ads.yaml.

GOOGLE_ADS_CREDENTIAL_SOURCE selects the source; ``load_config`` validates it
before anything else and resolves the candidate paths from the same environment
mapping it was handed. Turning those paths into OAuth material happens here,
lazily, so a credential problem reaches the caller as a named AUTH_CONFIG_*
error through health_check and the tools instead of killing startup.

Only client_id, client_secret, refresh_token and (for google-ads.yaml)
login_customer_id are ever read. A configuration file's endpoint, proxy,
logging, developer-token or impersonation settings are ignored, never applied:
this server's transport, destination and headers come from its own
configuration alone.
"""

from __future__ import annotations

import errno
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional

from ads_mcp.config import ConfigError, normalize_customer_id
from ads_mcp.errors import AuthConfigError

#: Accepted GOOGLE_ADS_CREDENTIAL_SOURCE values; the first is the default.
SOURCES = ("files", "adc", "google_ads_yaml")

#: Settings a source does not read, reported as health's unused_settings.
UNUSED_SETTINGS = {
    "files": ("GOOGLE_ADS_CONFIGURATION_FILE_PATH",),
    "adc": ("GOOGLE_ADS_CREDENTIALS_PATH", "GOOGLE_ADS_TOKEN_PATH",
            "GOOGLE_ADS_CONFIGURATION_FILE_PATH"),
    "google_ads_yaml": ("GOOGLE_ADS_CREDENTIALS_PATH", "GOOGLE_ADS_TOKEN_PATH"),
}

ADC_FILENAME = "application_default_credentials.json"
YAML_FILENAME = "google-ads.yaml"
#: A credential file is a handful of short fields; anything larger is a
#: mistake or a feed, and is refused before it is parsed.
MAX_FILE_BYTES = 64 * 1024
OWN_CLIENT_GUIDANCE = (
    "gcloud auth application-default login "
    "--client-id-file=<your own Desktop OAuth client JSON> "
    "--scopes=https://www.googleapis.com/auth/adwords"
)
# gcloud's built-in OAuth client cannot carry the adwords scope, so ADC minted
# with it authenticates but can never reach the Google Ads API.
GCLOUD_BUILTIN_CLIENT_ID = (
    "764086051850-6qr4p6gpi6hn506pt8ejuq83di341hur.apps.googleusercontent.com"
)


@dataclass(frozen=True)
class Resolution:
    """Resolved credential material plus what health reports about it."""

    material: dict
    login_customer_id: Optional[str]
    detail: Optional[dict]


def select_source(env: Mapping[str, str]) -> str:
    """The configured credential source. Validated before any other setting.

    Never echoes the supplied value: an unrecognized source is operator input
    that may be long, binary or newline-bearing, and the one-line startup
    refusal must stay readable.
    """
    raw = env.get("GOOGLE_ADS_CREDENTIAL_SOURCE")
    value = str(raw).strip() if raw is not None else ""
    if not value:
        return SOURCES[0]
    if value not in SOURCES:
        raise ConfigError(
            "GOOGLE_ADS_CREDENTIAL_SOURCE is not a recognized credential "
            "source; the allowed values are files, adc and google_ads_yaml "
            "(e.g. GOOGLE_ADS_CREDENTIAL_SOURCE=files)"
        )
    return value


def unused_settings(source: str, env: Mapping[str, str]) -> tuple:
    """The configured settings this source ignores, in sorted order."""
    return tuple(
        sorted(
            name
            for name in UNUSED_SETTINGS.get(source, ())
            if env.get(name) is not None and str(env[name]).strip()
        )
    )


def _setting(env: Mapping[str, str], name: str) -> Optional[str]:
    value = env.get(name)
    return str(value) if value is not None and str(value).strip() else None


def source_paths(source: str, env: Mapping[str, str]) -> tuple:
    """Candidate credential files for a source, in lookup order.

    Resolved while the environment mapping is in hand so the lazy resolution
    below cannot pick up a different ambient HOME or gcloud configuration than
    the one this server was configured with.
    """
    from ads_mcp.config import expand_path

    if source == "google_ads_yaml":
        explicit = _setting(env, "GOOGLE_ADS_CONFIGURATION_FILE_PATH")
        if explicit:
            return (expand_path(explicit),)
        home = _setting(env, "HOME")
        return (os.path.join(home, YAML_FILENAME),) if home else ()
    if source == "adc":
        explicit = _setting(env, "GOOGLE_APPLICATION_CREDENTIALS")
        if explicit:
            return (expand_path(explicit),)
        candidates = []
        gcloud_config = _setting(env, "CLOUDSDK_CONFIG")
        if gcloud_config:
            candidates.append(os.path.join(expand_path(gcloud_config), ADC_FILENAME))
        home = _setting(env, "HOME")
        if home:
            candidates.append(os.path.join(home, ".config", "gcloud", ADC_FILENAME))
        return tuple(candidates)
    return ()


def resolve(config) -> Resolution:
    """OAuth material for the configured source, with health's description."""
    source = getattr(config, "credential_source", SOURCES[0])
    if source == "adc":
        return _resolve_adc(config)
    if source == "google_ads_yaml":
        return _resolve_yaml(config)
    # The two-file source keeps its own reader, errors and variables.
    from ads_mcp import auth

    return Resolution(
        material=auth.load_oauth_material(config),
        login_customer_id=config.login_customer_id,
        detail=None,
    )


# ---------------------------------------------------------------------------
# Reading a credential file


def _read_bounded(path: Path, label: str, missing: str) -> bytes:
    """At most MAX_FILE_BYTES+1 bytes of a regular file, never blocking.

    A FIFO at the credential path must not park the server waiting for a
    writer, and a directory must not reach the parser, so the descriptor's own
    type decides — not a prior stat of a name that can change underneath.
    """
    try:
        handle = os.open(path, os.O_RDONLY | os.O_NONBLOCK)
    except OSError as exc:
        if exc.errno in (errno.ENOENT, errno.ENOTDIR, errno.ENAMETOOLONG):
            raise AuthConfigError("AUTH_CONFIG_MISSING_FILE", missing) from None
        raise AuthConfigError(
            "AUTH_CONFIG_UNREADABLE_FILE",
            f"{label} {path} cannot be opened "
            f"({errno.errorcode.get(exc.errno, 'OSError')})",
        ) from None
    try:
        if not stat.S_ISREG(os.fstat(handle).st_mode):
            raise AuthConfigError(
                "AUTH_CONFIG_UNREADABLE_FILE",
                f"{label} {path} must be a regular file",
            )
        chunks, total = [], 0
        while total <= MAX_FILE_BYTES:
            chunk = os.read(handle, MAX_FILE_BYTES + 1 - total)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
    except OSError as exc:
        raise AuthConfigError(
            "AUTH_CONFIG_UNREADABLE_FILE",
            f"{label} {path} cannot be read "
            f"({errno.errorcode.get(exc.errno, 'OSError')})",
        ) from None
    finally:
        os.close(handle)
    data = b"".join(chunks)
    if len(data) > MAX_FILE_BYTES:
        raise AuthConfigError(
            "AUTH_CONFIG_UNREADABLE_FILE",
            f"{label} {path} is larger than 64 KiB; a credential file holds "
            "a handful of short fields",
        )
    return data


def _decode(data: bytes, path: Path, label: str) -> str:
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise AuthConfigError(
            "AUTH_CONFIG_UNREADABLE_FILE", f"{label} {path} is not valid UTF-8"
        ) from None


def _nonblank(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _oauth_material(data: dict, path: Path, label: str) -> dict:
    """The three OAuth fields, each a non-blank string, or a named refusal."""
    material = {}
    for key in ("client_id", "client_secret", "refresh_token"):
        if not _nonblank(data.get(key)):
            raise AuthConfigError(
                "AUTH_CONFIG_INCOMPLETE",
                f"{label} {path} requires a non-blank string for {key}",
            )
        material[key] = data[key]
    if material["client_id"] == GCLOUD_BUILTIN_CLIENT_ID:
        raise AuthConfigError(
            "AUTH_CONFIG_UNSUPPORTED",
            f"{label} {path} was authorized with gcloud's own built-in OAuth "
            "client, which cannot hold the Google Ads API scope. Authorize "
            "your own Desktop OAuth client instead: " + OWN_CLIENT_GUIDANCE,
        )
    return material


# ---------------------------------------------------------------------------
# Application Default Credentials


ADC_LABEL = "the Application Default Credentials file"


def _described_type(value) -> str:
    """A bounded rendering of a refused credential type.

    The type names the refusal, but it is file content: only a short printable
    scalar is quoted back, so a hostile file cannot use the message as an echo.
    """
    if isinstance(value, str) and 0 < len(value) <= 64 and value.isprintable():
        return repr(value)
    return "an unsupported value"


def _resolve_adc(config) -> Resolution:
    candidates = [Path(candidate) for candidate in config.credential_paths]
    for candidate in candidates:
        if os.path.lexists(candidate):
            path = candidate
            break
    else:
        looked = (
            " (looked at " + ", ".join(str(item) for item in candidates) + ")"
            if candidates
            else ""
        )
        raise AuthConfigError(
            "AUTH_CONFIG_MISSING_FILE",
            f"no Application Default Credentials {ADC_FILENAME} was found"
            f"{looked}; point GOOGLE_APPLICATION_CREDENTIALS at one, or "
            "create it with " + OWN_CLIENT_GUIDANCE,
        )
    missing = (
        f"{ADC_LABEL} {path} does not exist; point "
        f"GOOGLE_APPLICATION_CREDENTIALS at an {ADC_FILENAME}, or create it "
        "with " + OWN_CLIENT_GUIDANCE
    )
    text = _decode(_read_bounded(path, ADC_LABEL, missing), path, ADC_LABEL)
    try:
        data = json.loads(text)
    except ValueError as exc:
        raise AuthConfigError(
            "AUTH_CONFIG_UNREADABLE_FILE",
            f"{ADC_LABEL} {path} is not valid JSON: {type(exc).__name__}",
        ) from None
    except Exception as exc:
        # A decoder can fail outside its own error type: thirty thousand
        # nested lists exhaust the C scanner's stack and raise RecursionError
        # on the minimum runtime. That is still an unreadable credential
        # file, not an internal fault, and the name of the failure is all
        # that may be said about the contents.
        raise AuthConfigError(
            "AUTH_CONFIG_UNREADABLE_FILE",
            f"{ADC_LABEL} {path} could not be parsed: {type(exc).__name__}",
        ) from None
    if not isinstance(data, dict):
        raise AuthConfigError(
            "AUTH_CONFIG_UNREADABLE_FILE",
            f"{ADC_LABEL} {path} must contain a JSON object",
        )
    kind = data.get("type")
    if kind != "authorized_user":
        held = (
            "has no type field"
            if "type" not in data
            else f"holds credentials of type {_described_type(kind)}"
        )
        raise AuthConfigError(
            "AUTH_CONFIG_UNSUPPORTED",
            f"{ADC_LABEL} {path} {held}; only type authorized_user is "
            "accepted — service accounts, workload identity federation and "
            "impersonation cannot authorize a Google Ads API user. Create "
            "authorized-user credentials with " + OWN_CLIENT_GUIDANCE,
        )
    return Resolution(
        material=_oauth_material(data, path, ADC_LABEL),
        login_customer_id=config.login_customer_id,
        detail={"type": "authorized_user", "file": str(path)},
    )


# ---------------------------------------------------------------------------
# google-ads.yaml


YAML_LABEL = f"the {YAML_FILENAME} configuration file"


class _YamlRefused(Exception):
    """A document shape this server will not accept; carries its own wording."""


#: Ten decimal digits with no leading zero: the only unquoted login id text
#: that is the account it reads as. Every other YAML number form resolves to
#: some other integer.
TEN_PLAIN_DIGITS = re.compile(r"[1-9][0-9]{9}\Z", re.ASCII)


class _YamlNumber:
    """A scalar YAML resolved to a number, keeping the text it was written as.

    YAML 1.1 has far more number forms than decimal, and several of them read
    like a customer id while meaning another one: 010000000000 is octal,
    0x499602D2 hex, 1_234_567_890 underscored and 342935:31:30 sexagesimal.
    The resolved integer cannot tell them apart from ten plain digits, so the
    source text is carried alongside it and decides.

    Equality follows the resolved value so duplicate numeric keys are still
    duplicates however each one was spelled.
    """

    __slots__ = ("value", "text")

    def __init__(self, value, text: str):
        self.value = value
        self.text = text

    def __eq__(self, other):
        if isinstance(other, _YamlNumber):
            return self.value == other.value
        return NotImplemented

    def __hash__(self):
        return hash(self.value)


def _strict_loader():
    """``yaml.SafeLoader`` with anchors, aliases and duplicate keys refused.

    An alias lets one credential file name the same node twice, and a repeated
    key lets it carry two values for client_secret where only the last one
    would be read. Both make the file mean something other than it reads, so
    neither is accepted.
    """
    import yaml

    class StrictLoader(yaml.SafeLoader):
        def compose_node(self, parent, index):
            event = self.peek_event()
            if getattr(event, "anchor", None) is not None:
                raise _YamlRefused(
                    "uses a YAML anchor or alias; write every value out in full"
                )
            return super().compose_node(parent, index)

        def construct_mapping(self, node, deep=False):
            seen = []
            for key_node, _ in node.value:
                key = self.construct_object(key_node, deep=deep)
                if key in seen:
                    raise _YamlRefused(
                        "sets a duplicate key; every setting must appear once"
                    )
                seen.append(key)
            return super().construct_mapping(node, deep=deep)

        def construct_number(self, node):
            tag = node.tag.rsplit(":", 1)[-1]
            build = (self.construct_yaml_int if tag == "int"
                     else self.construct_yaml_float)
            return _YamlNumber(build(node), node.value)

    for tag in ("int", "float"):
        StrictLoader.add_constructor(
            f"tag:yaml.org,2002:{tag}", StrictLoader.construct_number
        )
    return StrictLoader


def _resolve_yaml(config) -> Resolution:
    paths = config.credential_paths
    path = Path(paths[0]) if paths else None
    missing = (
        f"no {YAML_FILENAME} was found"
        + (f" at {path}" if path is not None else "")
        + "; set GOOGLE_ADS_CONFIGURATION_FILE_PATH to its location, or place "
        f"{YAML_FILENAME} in the home directory of the account running the server"
    )
    if path is None:
        raise AuthConfigError("AUTH_CONFIG_MISSING_FILE", missing)
    text = _decode(_read_bounded(path, YAML_LABEL, missing), path, YAML_LABEL)

    import yaml

    try:
        data = yaml.load(text, Loader=_strict_loader())
    except _YamlRefused as refusal:
        raise AuthConfigError(
            "AUTH_CONFIG_UNREADABLE_FILE", f"{YAML_LABEL} {path} {refusal}"
        ) from None
    except yaml.YAMLError as exc:
        raise AuthConfigError(
            "AUTH_CONFIG_UNREADABLE_FILE",
            f"{YAML_LABEL} {path} is not valid YAML: {type(exc).__name__}",
        ) from None
    except Exception as exc:
        # The composer recurses once per nesting level, so a document twenty
        # thousand sequences deep raises RecursionError rather than a
        # YAMLError. Either way the file cannot be read, and only the name of
        # the failure may be reported.
        raise AuthConfigError(
            "AUTH_CONFIG_UNREADABLE_FILE",
            f"{YAML_LABEL} {path} could not be parsed: {type(exc).__name__}",
        ) from None
    if not isinstance(data, dict):
        raise AuthConfigError(
            "AUTH_CONFIG_UNREADABLE_FILE",
            f"{YAML_LABEL} {path} must contain a YAML mapping of settings",
        )

    material = _oauth_material(data, path, YAML_LABEL)
    if config.login_customer_id:
        login, origin = config.login_customer_id, "env"
    elif data.get("login_customer_id") is None:
        login, origin = None, "none"
    else:
        login, origin = _yaml_login(data["login_customer_id"], path), "yaml"
    return Resolution(
        material=material,
        login_customer_id=login,
        detail={"file": str(path), "login_customer_id_source": origin},
    )


def _yaml_login(value, path: Path) -> str:
    """The yaml login_customer_id: a quoted id, or ten unquoted plain digits.

    An unquoted id is a number to YAML, and a leading zero makes it an octal
    one: 0123456789 is not the account it looks like. A number is therefore
    accepted only when it was written as exactly ten decimal digits with no
    leading zero; a string keeps the ordinary customer-id rule, which allows
    the dashed form and a leading zero. Everything else asks for quotes rather
    than becoming a different account.
    """
    text = None
    if isinstance(value, str):
        text = value
    elif isinstance(value, _YamlNumber) and TEN_PLAIN_DIGITS.match(value.text):
        text = value.text
    if text is not None:
        try:
            return normalize_customer_id(text)
        except ConfigError:
            pass
    raise AuthConfigError(
        "AUTH_CONFIG_INCOMPLETE",
        f"login_customer_id in {YAML_LABEL} {path} is not a Google Ads "
        "customer id; quote it as ten digits (e.g. login_customer_id: "
        "'123-456-7890') so a leading zero survives",
    )
