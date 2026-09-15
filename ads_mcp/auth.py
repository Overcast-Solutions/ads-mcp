"""OAuth credential loading and the authenticated GoogleAdsClient factory.

Credential failures surface as structured, named errors (AUTH_TOKEN_REVOKED,
AUTH_CONFIG_*) — never a generic failure, and never retried by transport.
The Secrets registry lets every outgoing message be scrubbed of credential
material regardless of which layer produced it.
"""

from __future__ import annotations

import json
from pathlib import Path

from ads_mcp.errors import AuthConfigError

_REDACTED = "[redacted]"


class Secrets:
    """Known secret values; ``scrub`` removes them from any outgoing text."""

    def __init__(self, values=()):
        self._values = []
        for value in values:
            self.add(value)

    MIN_LENGTH = 8

    def add(self, value):
        # Substring replacement on a short value would mangle unrelated text
        # (and reveal, by its absence, that the value was short).
        if (
            isinstance(value, str)
            and len(value) >= self.MIN_LENGTH
            and value not in self._values
        ):
            self._values.append(value)

    def scrub(self, text: str) -> str:
        for value in self._values:
            if value:
                text = text.replace(value, _REDACTED)
        return text


def collect_secrets(config) -> Secrets:
    """Best-effort registry of every credential value we can know about."""
    secrets = Secrets([config.developer_token])
    for path in (config.credentials_path, config.token_path):
        try:
            data = json.loads(Path(path).read_text())
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        for blob in (data, *(v for v in data.values() if isinstance(v, dict))):
            if isinstance(blob, dict):
                for key in ("client_secret", "refresh_token"):
                    secrets.add(blob.get(key))
    return secrets


def _read_json(path: str, var: str) -> dict:
    p = Path(path)
    if not p.exists():
        raise AuthConfigError(
            "AUTH_CONFIG_MISSING_FILE",
            f"credential file {path} ({var}) does not exist",
        )
    try:
        data = json.loads(p.read_text())
    except Exception as exc:
        raise AuthConfigError(
            "AUTH_CONFIG_UNREADABLE_FILE",
            f"credential file {path} ({var}) is not valid JSON: {type(exc).__name__}",
        ) from None
    if not isinstance(data, dict):
        raise AuthConfigError(
            "AUTH_CONFIG_UNREADABLE_FILE",
            f"credential file {path} ({var}) must contain a JSON object",
        )
    return data


def load_oauth_material(config) -> dict:
    """Extract client_id/client_secret/refresh_token from the two files."""
    cred = _read_json(config.credentials_path, "GOOGLE_ADS_CREDENTIALS_PATH")
    tok = _read_json(config.token_path, "GOOGLE_ADS_TOKEN_PATH")

    for wrapper in ("installed", "web"):
        if wrapper in cred and not isinstance(cred[wrapper], dict):
            raise AuthConfigError(
                "AUTH_CONFIG_INCOMPLETE",
                f"credentials field {wrapper} must contain a JSON client object",
            )
    installed = cred.get("installed", cred.get("web", cred))
    client_id = installed.get("client_id", tok.get("client_id"))
    client_secret = installed.get("client_secret", tok.get("client_secret"))
    refresh_token = tok.get("refresh_token")

    missing = [
        name
        for name, val in (
            ("client_id", client_id),
            ("client_secret", client_secret),
            ("refresh_token", refresh_token),
        )
        if not isinstance(val, str) or not val.strip()
    ]
    if missing:
        raise AuthConfigError(
            "AUTH_CONFIG_INCOMPLETE",
            "credential files require non-blank strings for: " + ", ".join(missing),
        )
    return {
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
    }


def build_client(config):
    """Construct an authenticated GoogleAdsClient from the resolved config."""
    material = load_oauth_material(config)
    from google.ads.googleads.client import GoogleAdsClient

    conf = {
        # SDK 31.4 still requires this key, but Cloud-project access omits
        # the header in both unary and streaming interceptors. Never pass
        # legacy credential material or a placeholder token to the SDK.
        "developer_token": None,
        "use_cloud_org_for_api_access": True,
        "client_id": material["client_id"],
        "client_secret": material["client_secret"],
        "refresh_token": material["refresh_token"],
        "use_proto_plus": True,
    }
    if config.login_customer_id:
        conf["login_customer_id"] = config.login_customer_id
    return GoogleAdsClient.load_from_dict(conf)


def generate_token_main(argv=None):
    """Console entry for the OAuth refresh-token helper (`ads-mcp-generate-token`).

    Runs Google's loopback OAuth consent flow for a Desktop-app client and
    writes the resulting refresh token JSON to GOOGLE_ADS_TOKEN_PATH. This is
    the re-auth step every AUTH_TOKEN_REVOKED error points at.
    """
    import argparse
    import base64
    import errno
    import hashlib
    import http.client
    import http.server
    import json as json_mod
    import os
    import re
    import secrets
    import stat
    import unicodedata
    import urllib.error
    import urllib.parse
    import urllib.request
    import webbrowser

    parser = argparse.ArgumentParser(
        prog="ads-mcp-generate-token",
        description=(
            "Generate the OAuth refresh token ads-mcp authenticates with: "
            "opens Google's consent screen for your Desktop-app OAuth client "
            "(GOOGLE_ADS_CREDENTIALS_PATH), captures the loopback redirect, "
            "exchanges the code, and writes the refresh-token JSON to "
            "GOOGLE_ADS_TOKEN_PATH. External OAuth apps left in Testing normally "
            "receive refresh tokens that expire after seven days. See the "
            "README for OAuth publishing and setup guidance."
        ),
    )
    parser.add_argument(
        "--credentials",
        default=os.environ.get("GOOGLE_ADS_CREDENTIALS_PATH"),
        help="OAuth client JSON (default: GOOGLE_ADS_CREDENTIALS_PATH)",
    )
    parser.add_argument(
        "--token-path",
        default=os.environ.get("GOOGLE_ADS_TOKEN_PATH"),
        help="where to write the refresh-token JSON (default: GOOGLE_ADS_TOKEN_PATH)",
    )
    parser.add_argument("--port", default=8085, help="loopback port (1-65535)")
    args = parser.parse_args(argv)

    try:
        args.port = int(args.port)
        if not 1 <= args.port <= 65535:
            raise ValueError
    except ValueError:
        print(
            "AUTH_LOOPBACK_PORT_INVALID: choose an integer loopback port "
            "between 1 and 65535 with --port.",
            file=__import__("sys").stderr,
        )
        return 2

    if not args.credentials or not args.token_path:
        print(
            "AUTH_CONFIG_INCOMPLETE: --credentials and --token-path are "
            "required (or set GOOGLE_ADS_CREDENTIALS_PATH / "
            "GOOGLE_ADS_TOKEN_PATH)",
            file=__import__("sys").stderr,
        )
        return 2

    try:
        cred = _read_json(args.credentials, "GOOGLE_ADS_CREDENTIALS_PATH")
        installed = cred.get("installed") or cred.get("web") or {}
        if not isinstance(installed, dict):
            raise AuthConfigError(
                "AUTH_CONFIG_INCOMPLETE",
                "credentials file must contain an installed or web client object",
            )
        client_id = installed.get("client_id")
        client_secret = installed.get("client_secret")
        if not all(
            isinstance(value, str) and value.strip()
            for value in (client_id, client_secret)
        ):
            raise AuthConfigError(
                "AUTH_CONFIG_INCOMPLETE",
                "credentials file requires non-empty client_id/client_secret strings",
            )
    except AuthConfigError as exc:
        print(f"{exc.code}: {exc.message}", file=__import__("sys").stderr)
        return 2

    # Separate OS-backed 256-bit draws keep the public state independent of
    # the private PKCE verifier. Unpadded base64url yields 43 valid characters.
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(32)
    challenge = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode("ascii")).digest()
    ).decode("ascii").rstrip("=")
    redirect = f"http://localhost:{args.port}"
    auth_url = (
        "https://accounts.google.com/o/oauth2/v2/auth?"
        + urllib.parse.urlencode(
            {
                "client_id": client_id,
                "redirect_uri": redirect,
                "scope": "https://www.googleapis.com/auth/adwords",
                "response_type": "code",
                "access_type": "offline",
                "prompt": "consent",
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
    )
    callback = {}

    def respond(handler, outcome):
        callback["outcome"] = outcome
        responses = {
            "received": (
                b"<h1>Consent received.</h1>"
                b"<p>Check the terminal while token exchange and saving finish.</p>"
            ),
            "denied": (
                b"<h1>Consent denied.</h1>"
                b"<p>Check the terminal and rerun the helper to try again.</p>"
            ),
            "invalid": (
                b"<h1>Invalid callback.</h1>"
                b"<p>Check the terminal and rerun the helper to try again.</p>"
            ),
        }
        handler.send_response(200 if outcome == "received" else 400)
        handler.send_header("Content-Type", "text/html; charset=utf-8")
        handler.end_headers()
        if getattr(handler, "command", None) != "HEAD":
            handler.wfile.write(responses[outcome])

    class Handler(http.server.BaseHTTPRequestHandler):
        def handle(self):
            # Reaching a handler means a connection was accepted, even if it
            # ends at EOF. Only an untouched listener can report no-request timeout.
            callback["outcome"] = "invalid"
            super().handle()

        def parse_request(self):
            parsed = super().parse_request()
            # The stdlib silently rejects an empty request line. Give it the
            # same static recovery page as other invalid callbacks.
            if not parsed and not self.requestline.split():
                respond(self, "invalid")
            return parsed

        def do_GET(self):  # noqa: N802 — stdlib API
            outcome = "invalid"
            try:
                # Validate before urlsplit can discard literal controls, and
                # before parse_qs can silently repair malformed encoding.
                self.path.encode("utf-8", errors="strict")
                if any(unicodedata.category(c) == "Cc" for c in self.path):
                    raise ValueError
                if re.search(r"%(?![0-9A-Fa-f]{2})", self.path):
                    raise ValueError
                parsed = urllib.parse.urlsplit(self.path)
                if (
                    parsed.path not in ("", "/")
                    or parsed.scheme or parsed.netloc or parsed.fragment
                ):
                    raise ValueError
                params = urllib.parse.parse_qs(
                    parsed.query, keep_blank_values=True, errors="strict"
                )
                if params.get("state") != [state]:
                    raise ValueError
                if "error" in params:
                    if "code" in params or len(params["error"]) != 1:
                        raise ValueError
                    if not params["error"][0].strip():
                        raise ValueError
                    outcome = "denied"
                else:
                    codes = params.get("code", [])
                    if len(codes) != 1 or not codes[0]:
                        raise ValueError
                    if any(
                        c.isspace() or unicodedata.category(c) == "Cc"
                        for c in codes[0]
                    ):
                        raise ValueError
                    callback["code"] = codes[0]
                    outcome = "received"
            except (ValueError, UnicodeError):
                pass
            respond(self, outcome)

        def send_error(self, code, message=None, explain=None):
            # Parser and unsupported-method diagnostics can contain raw
            # callback secrets. Discard them before emitting any HTTP bytes.
            self.close_connection = True
            respond(self, "invalid")

        def log_message(self, *a):  # silence request logging
            pass

    try:
        server = http.server.HTTPServer(("127.0.0.1", args.port), Handler)
    except OSError:
        print(
            "AUTH_LOOPBACK_UNAVAILABLE: unable to listen on the loopback port; "
            "choose an available port with --port and rerun the helper.",
            file=__import__("sys").stderr,
        )
        return 1
    try:
        print(f"Open this URL to authorize (waiting on {redirect}):\n\n{auth_url}\n")
        try:
            opened = webbrowser.open(auth_url)
        except (webbrowser.Error, OSError):
            opened = False
        if not opened:
            print("Unable to open a browser; copy the URL above into a browser manually.")
        server.timeout = 300
        server.handle_request()
    finally:
        server.server_close()
    outcome = callback.get("outcome")
    if outcome != "received":
        errors = {
            "invalid": (
                "AUTH_CALLBACK_INVALID: invalid authorization callback; "
                "rerun the helper to try again."
            ),
            "denied": (
                "AUTH_CONSENT_DENIED: consent was denied; "
                "rerun the helper to try again."
            ),
            None: (
                "AUTH_CALLBACK_TIMEOUT: no callback received before timeout; "
                "rerun the helper to try again."
            ),
        }
        print(errors[outcome], file=__import__("sys").stderr)
        return 1

    body = urllib.parse.urlencode(
        {
            "code": callback["code"],
            "code_verifier": verifier,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect,
            "grant_type": "authorization_code",
        }
    ).encode()
    try:
        with urllib.request.urlopen(
            urllib.request.Request(
                "https://oauth2.googleapis.com/token", data=body, method="POST"
            ),
            timeout=30,
        ) as resp:
            token = json_mod.loads(resp.read())
    except urllib.error.HTTPError:
        print(
            "AUTH_EXCHANGE_REJECTED: authorization was rejected; rerun the "
            "helper to authorize again and complete consent.",
            file=__import__("sys").stderr,
        )
        return 1
    except (urllib.error.URLError, OSError, http.client.HTTPException):
        print(
            "AUTH_EXCHANGE_UNAVAILABLE: unable to complete the token exchange; "
            "check the network connection and retry the helper.",
            file=__import__("sys").stderr,
        )
        return 1
    except (ValueError, UnicodeError):
        print(
            "AUTH_EXCHANGE_INVALID_RESPONSE: token exchange returned an "
            "invalid response; rerun the helper to authorize again.",
            file=__import__("sys").stderr,
        )
        return 1
    refresh = token.get("refresh_token") if isinstance(token, dict) else None
    if not isinstance(refresh, str) or not refresh.strip():
        # Never dump response values or keys: either can contain credentials.
        print(
            "AUTH_EXCHANGE_INVALID_RESPONSE: token exchange returned no valid "
            "refresh token; rerun the helper to authorize again with consent.",
            file=__import__("sys").stderr,
        )
        return 1
    out = Path(args.token_path)

    def private_output(path, flags):
        # Delay truncating existing content until the opened file is private.
        # New files are private from creation, independent of the user's umask.
        # A FIFO must not wait for a reader before the descriptor type check.
        fd = os.open(path, (flags & ~os.O_TRUNC) | os.O_NONBLOCK, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode):
                raise OSError("token output must be a regular file")
            if stat.S_IMODE(info.st_mode) != 0o600:
                os.fchmod(fd, 0o600)
            os.ftruncate(fd, 0)
            return fd
        except BaseException:
            os.close(fd)
            raise

    try:
        with open(out, "w", encoding="utf-8", opener=private_output) as stream:
            stream.write(json_mod.dumps({"refresh_token": refresh}))
    except (AttributeError, NotImplementedError, OSError) as exc:
        # Unsupported errno names can be absent or aliases on other platforms.
        # An ordinary OSError without an errno must not match an absent name.
        unavailable = isinstance(exc, (AttributeError, NotImplementedError)) or (
            isinstance(exc, OSError)
            and exc.errno is not None
            and any(
                exc.errno == getattr(errno, name, None)
                for name in ("ENOSYS", "ENOTSUP", "EOPNOTSUPP")
            )
        )
        if unavailable:
            message = (
                "AUTH_TOKEN_WRITE_FAILED: a required secure token-storage "
                "capability is unavailable; use a platform and filesystem with "
                "POSIX private-file permissions and nonblocking opens, then "
                "rerun the helper. See the README for storage requirements."
            )
        else:
            message = (
                "AUTH_TOKEN_WRITE_FAILED: unable to save the refresh token; "
                "choose a writable token file path and check its parent directory "
                "and permissions, then rerun the helper."
            )
        print(message, file=__import__("sys").stderr)
        return 1
    print(f"refresh token written to {out}")
    return 0
