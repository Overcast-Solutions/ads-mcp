"""Provider double for the credential-source oracle: OAuth token endpoint + gRPC channel.

Only two seams are substituted. HTTP sends through requests' adapter answer
Google's OAuth token endpoint with synthetic access tokens (anything else is
recorded as unexpected and refused). google.api_core.grpc_helpers.create_channel
returns an offline channel: requests are serialized and responses deserialized
by the genuine v25 SDK serializers; the credentials handed to the channel are
applied per call exactly as gRPC's auth plugin does (before_request). Real
GoogleAdsClient construction, interceptors and error mapping all execute.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from urllib.parse import parse_qs

TOKEN_URIS = ("https://oauth2.googleapis.com/token", "https://accounts.google.com/o/oauth2/token",
              "https://www.googleapis.com/oauth2/v4/token")
GCLOUD_CLIENT_ID = "764086051850-6qr4p6gpi6hn506pt8ejuq83di341hur.apps.googleusercontent.com"
FAILURE_KEY = "google.ads.googleads.v25.errors.googleadsfailure-bin"
METADATA_HOSTS = ("169.254.169.254", "metadata.google.internal", "metadata")


class ProviderDouble:
    """Records token, channel and RPC events; ``mode()`` steers faults."""

    def __init__(self, *, marker=None, control=None):
        self.marker = Path(marker) if marker else None
        self.control = Path(control) if control else None
        self.events: list[dict] = []
        self.lock = threading.Lock()
        self.issued = 0
        self.settings: dict = {}

    # -- observation
    def record(self, event, **fields):
        item = {"event": event, **fields}
        with self.lock:
            self.events.append(item)
            if self.marker is not None:
                with self.marker.open("a") as stream:
                    stream.write(json.dumps(item) + "\n")

    def mode(self) -> dict:
        if self.control is not None and self.control.exists():
            return json.loads(self.control.read_text() or "{}")
        return dict(self.settings)

    def of(self, kind):
        return [event for event in self.events if event["event"] == kind]

    # -- OAuth token endpoint (requests adapter seam)
    def http_send(self, adapter, request, stream=False, timeout=None, verify=True, cert=None, proxies=None):
        import requests

        url = request.url or ""
        active_proxies = {key: value for key, value in (proxies or {}).items() if value}
        if request.method != "POST" or url.split("?")[0] not in TOKEN_URIS:
            host = url.split("/")[2] if "://" in url else url
            self.record("unexpected http", host=host, metadata_server=host.split(":")[0] in METADATA_HOSTS)
            raise requests.ConnectionError("synthetic provider refuses this destination")
        body = request.body or b""
        if isinstance(body, bytes):
            body = body.decode("utf-8")
        form = {key: values[0] for key, values in parse_qs(body).items()}
        mode = self.mode()
        self.issued += 1
        token = f"ya29.oracle-access-token-{self.issued:04d}-synthetic"
        self.record("token", url=url, grant_type=form.get("grant_type"), client_id=form.get("client_id"),
                    client_secret=form.get("client_secret"), refresh_token=form.get("refresh_token"),
                    proxies=sorted(active_proxies), access_token=token)
        response = requests.Response()
        response.url = url
        response.request = request
        response.encoding = "utf-8"
        response.headers["Content-Type"] = "application/json"
        if mode.get("token") == "invalid_grant":
            response.status_code = 400
            response._content = json.dumps({"error": "invalid_grant",
                                             "error_description": "Token has been expired or revoked."}).encode()
            return response
        response.status_code = 200
        response._content = json.dumps({"access_token": token, "token_type": "Bearer",
                                         "expires_in": 1 if mode.get("short_lived") else 3599,
                                         "scope": "https://www.googleapis.com/auth/adwords"}).encode()
        return response

    # -- gRPC channel seam
    def create_channel(self, target, credentials=None, scopes=None, ssl_credentials=None,
                       credentials_file=None, quota_project_id=None, default_scopes=None,
                       default_host=None, compression=None, attempt_direct_path=False, **kwargs):
        options = kwargs.get("options") or ()
        self.record("channel", target=str(target), credentials=type(credentials).__name__,
                    credentials_file=credentials_file, quota_project_id=quota_project_id,
                    options=[list(map(str, option)) for option in options])
        return _channel(self, credentials)

    def metadata_probe(self, *args, **kwargs):
        self.record("metadata probe")
        return False

    def install(self, setattr):
        import google.api_core.grpc_helpers as grpc_helpers
        import requests.adapters
        from google.auth.compute_engine import _metadata

        double = self

        def send(adapter, request, stream=False, timeout=None, verify=True, cert=None, proxies=None):
            return double.http_send(adapter, request, stream, timeout, verify, cert, proxies)

        setattr(requests.adapters.HTTPAdapter, "send", send)
        setattr(grpc_helpers, "create_channel", self.create_channel)
        setattr(_metadata, "ping", self.metadata_probe)
        setattr(_metadata, "is_on_gce", self.metadata_probe)
        return self


def _channel(double, credentials):
    import grpc

    class Call(grpc.Call, grpc.Future):
        def __init__(self, response=None, error=None, trailing=()):
            self._response, self._error, self._trailing = response, error, tuple(trailing)

        def initial_metadata(self):
            return ()

        def trailing_metadata(self):
            return self._trailing

        def code(self):
            return grpc.StatusCode.INVALID_ARGUMENT if self._error else grpc.StatusCode.OK

        def details(self):
            return "synthetic provider failure" if self._error else ""

        def is_active(self):
            return False

        def time_remaining(self):
            return None

        def cancel(self):
            return False

        def cancelled(self):
            return False

        def running(self):
            return False

        def done(self):
            return True

        def add_callback(self, callback):
            return False

        def add_done_callback(self, callback):
            callback(self)

        def result(self, timeout=None):
            if self._error is not None:
                raise self._error
            return self._response

        def exception(self, timeout=None):
            return self._error

        def traceback(self, timeout=None):
            return None

    class Failure(grpc.RpcError, Call):
        def __init__(self, trailing):
            Call.__init__(self, error=self, trailing=trailing)

    def authorize(method):
        headers = {}
        if credentials is not None:
            from google.auth.transport.requests import Request

            credentials.before_request(Request(), "POST", "https://googleads.googleapis.com" + method, headers)
        return headers

    def respond(method, request, metadata):
        from google.ads.googleads.v25.errors.types.errors import GoogleAdsFailure
        from google.ads.googleads.v25.services.types import google_ads_service
        from google.protobuf.json_format import ParseDict

        mode = double.mode()
        if mode.get("fault"):
            secrets = {"access": metadata.get("authorization", "").removeprefix("Bearer "),
                       "refresh": mode.get("echo_refresh", ""), "secret": mode.get("echo_secret", "")}
            failure = GoogleAdsFailure()
            ParseDict({"errors": [{"error_code": {"authorization_error": "ACTION_NOT_PERMITTED"},
                                   "message": "synthetic provider echo {access} {refresh} {secret}".format(**secrets)}]},
                      GoogleAdsFailure.pb(failure))
            raise Failure(((FAILURE_KEY, GoogleAdsFailure.serialize(failure)), ("request-id", "synthetic-request")))
        if method.endswith("GoogleAdsService/Search"):
            rows = []
            if " FROM customer" in request.query or " from customer" in request.query.lower():
                rows.append({"customer": {"id": int(request.customer_id), "currency_code": "USD",
                                          "descriptive_name": "Synthetic account", "time_zone": "America/Los_Angeles",
                                          "auto_tagging_enabled": True, "manager": False, "test_account": False}})
            response = google_ads_service.SearchGoogleAdsResponse()
            ParseDict({"results": rows}, google_ads_service.SearchGoogleAdsResponse.pb(response))
            return response
        if method.endswith("GoogleAdsService/SearchStream"):
            rows = []
            if " from customer" in request.query.lower():
                rows.append({"customer": {"id": int(request.customer_id)}})
            response = google_ads_service.SearchGoogleAdsStreamResponse()
            ParseDict({"results": rows}, google_ads_service.SearchGoogleAdsStreamResponse.pb(response))
            return response
        if method.endswith("GoogleAdsFieldService/SearchGoogleAdsFields"):
            from google.ads.googleads.v25.services.types import google_ads_field_service

            return google_ads_field_service.SearchGoogleAdsFieldsResponse()
        raise Failure((("request-id", "synthetic-request"),))

    def invoke(method, request, serializer, deserializer, timeout, metadata):
        headers = authorize(method)
        merged = {str(key).lower(): value for key, value in (metadata or ())}
        merged.update({key.lower(): value for key, value in headers.items()})
        wire = serializer(request) if serializer is not None else request
        decoded = type(request).deserialize(wire) if hasattr(type(request), "deserialize") else request
        double.record("rpc", method=method, metadata={k: (v if isinstance(v, str) else "<bytes>")
                                                      for k, v in merged.items()},
                      customer_id=getattr(decoded, "customer_id", None), query=getattr(decoded, "query", None),
                      timeout=timeout if isinstance(timeout, (int, float)) else None)
        response = respond(method, decoded, merged)
        return deserializer(type(response).serialize(response)) if deserializer is not None else response

    class UnaryUnary:
        def __init__(self, method, serializer, deserializer):
            self.method, self.serializer, self.deserializer = method, serializer, deserializer

        def with_call(self, request, timeout=None, metadata=None, credentials=None, wait_for_ready=None,
                      compression=None):
            response = invoke(self.method, request, self.serializer, self.deserializer, timeout, metadata)
            return response, Call(response)

        def __call__(self, request, timeout=None, metadata=None, credentials=None, wait_for_ready=None,
                     compression=None):
            return self.with_call(request, timeout, metadata)[0]

        def future(self, request, timeout=None, metadata=None, credentials=None, wait_for_ready=None,
                   compression=None):
            try:
                return Call(self(request, timeout, metadata))
            except grpc.RpcError as error:
                return error

    class Stream(Call):
        def __init__(self, responses):
            super().__init__()
            self._items = iter(responses)

        def __iter__(self):
            return self

        def __next__(self):
            return next(self._items)

    class UnaryStream(UnaryUnary):
        def __call__(self, request, timeout=None, metadata=None, credentials=None, wait_for_ready=None,
                     compression=None):
            return Stream([invoke(self.method, request, self.serializer, self.deserializer, timeout, metadata)])

    class Channel(grpc.Channel):
        def unary_unary(self, method, request_serializer=None, response_deserializer=None, *args, **kwargs):
            return UnaryUnary(method, request_serializer, response_deserializer)

        def unary_stream(self, method, request_serializer=None, response_deserializer=None, *args, **kwargs):
            return UnaryStream(method, request_serializer, response_deserializer)

        def stream_unary(self, *args, **kwargs):
            raise AssertionError("unexpected client-streaming RPC")

        stream_stream = stream_unary

        def subscribe(self, *args, **kwargs):
            pass

        def unsubscribe(self, *args, **kwargs):
            pass

        def close(self):
            pass

    return Channel()


def authorized_user(client_id, client_secret, refresh_token, **extra):
    return {"type": "authorized_user", "client_id": client_id, "client_secret": client_secret,
            "refresh_token": refresh_token, **extra}


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))
    os.chmod(path, 0o600)
    return path


INJECTION = r'''
import os, sys
sys.path.insert(0, os.environ["ORACLE_TESTS"])
def _offline(event, args):
    if event in ("socket.connect", "socket.getaddrinfo", "socket.sendto"):
        with open(os.environ["ORACLE_MARKER"], "a") as stream:
            stream.write('{"event": "network attempted"}\n')
        raise OSError("credential-source oracle forbids networking")
sys.addaudithook(_offline)
import credential_sources_oracle as _oracle
_double = _oracle.ProviderDouble(marker=os.environ["ORACLE_MARKER"], control=os.environ["ORACLE_CONTROL"])
_double.install(setattr)
_double.record("loaded")
'''
