"""GoogleAdsFieldService-backed field catalogue read (get_resource_metadata).

Account-free by construction: the field service describes the API's own
schema, so no customer id is accepted, sent or returned, and no account
data is read. Every tool call spends at most ``MAX_REQUESTS``
prefix-limited ``SearchGoogleAdsFields`` requests inside one
``DEADLINE_SECONDS`` budget; a server-local catalogue keeps a top-level
family for at most ``CACHE_SECONDS`` measured on the server's clock.

Pagination is self-contained: tokens are signed, opaque and bound to the
tool, the requested name and the effective page size, so no retained
snapshot of provider rows is held for this read.
"""

from __future__ import annotations

import base64
from collections import OrderedDict
import difflib
import hashlib
import hmac
import json
import re
import secrets
import threading
import time

from ads_mcp.errors import ToolError, error_payload
from ads_mcp.transport import TransportError

TOOL = "get_resource_metadata"

MAX_REQUESTS = 3
DEADLINE_SECONDS = 10.0
CACHE_SECONDS = 3600.0
PROVIDER_PAGE_SIZE = 10_000

DEFAULT_PAGE_SIZE = 100
MAX_PAGE_SIZE = 1000
MAX_NAME_LENGTH = 128
ECHO_LIMIT = 64
MAX_ENUM_VALUES = 100
MAX_CLOSE_MATCHES = 5
CLOSE_MATCH_CUTOFF = 0.6
MAX_TOKEN_LENGTH = 256
MAX_CACHED_FAMILIES = 32

# Field attributes projected from the provider; nothing else is read, so no
# resource names, type urls or other raw provider payload can reach output.
SELECTED = ("name", "category", "data_type", "selectable", "filterable",
            "sortable", "is_repeated", "enum_values", "selectable_with",
            "attribute_resources")

NAME_RULE = re.compile(r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*")
NAME_GUIDANCE = (
    "name must be dot-separated lowercase segments, each matching "
    "[a-z][a-z0-9_]*, with a total length of 1 to 128 characters and no "
    "whitespace, quotes or GAQL syntax"
)
# A bare pseudo-resource would describe the whole reporting surface; the
# caller gets the narrower request that actually answers the question.
TOO_BROAD = {"metrics": "metrics.clicks", "segments": "segments.date"}
TOKEN_REFUSAL = (
    "page_token is malformed, or belongs to another tool, name or page_size. "
    "Restart from the first page without a page token."
)


def refuse_token():
    raise ToolError("INVALID_PAGE_TOKEN", TOKEN_REFUSAL)


def sanitize(value) -> str:
    """At most 64 printable characters of caller input, safe to echo."""
    text = value if isinstance(value, str) else repr(value)
    return "".join(ch for ch in text[:ECHO_LIMIT] if ch.isprintable())


def is_field_name(value) -> bool:
    """The same name rule, as a predicate for provider-supplied names."""
    return (isinstance(value, str) and 1 <= len(value) <= MAX_NAME_LENGTH
            and NAME_RULE.fullmatch(value) is not None)


def validate_name(name) -> str:
    """The name rule, applied before any provider access."""
    if (not isinstance(name, str) or not 1 <= len(name) <= MAX_NAME_LENGTH
            or NAME_RULE.fullmatch(name) is None):
        raise ToolError(
            "INVALID_ARGUMENT", f"{NAME_GUIDANCE}; received {sanitize(name)!r}"
        )
    narrower = TOO_BROAD.get(name)
    if narrower is not None:
        raise ToolError(
            "INVALID_ARGUMENT",
            f"{name!r} on its own is too broad to describe: ask for one field "
            f"such as {narrower!r}, or for a resource such as 'campaign', "
            f"whose answer names every {name}. field it can be selected with",
        )
    return name


def validate_page_size(page_size) -> int:
    if page_size is None:
        return DEFAULT_PAGE_SIZE
    if type(page_size) is not int:
        raise ToolError("INVALID_PAGE_SIZE",
                        f"page_size {sanitize(page_size)!r} is not an integer")
    if not 1 <= page_size <= MAX_PAGE_SIZE:
        raise ToolError("INVALID_PAGE_SIZE",
                        f"page_size {page_size} is outside 1..{MAX_PAGE_SIZE}")
    return page_size


class FieldCatalog:
    """One server's bounded field-family cache and page-token signer."""

    def __init__(self):
        self._lock = threading.Lock()
        self._families: OrderedDict[str, tuple[float, dict]] = OrderedDict()
        self._secret = secrets.token_bytes(32)

    # -- cache

    def family(self, ctx, top: str, fetch) -> dict:
        now = ctx.clock()
        with self._lock:
            for key, (stored, _) in list(self._families.items()):
                if now - stored >= CACHE_SECONDS:
                    del self._families[key]
            cached = self._families.get(top)
        if cached is not None:
            return cached[1]
        table = fetch()
        with self._lock:
            self._families[top] = (ctx.clock(), table)
            self._families.move_to_end(top)
            while len(self._families) > MAX_CACHED_FAMILIES:
                self._families.popitem(last=False)
        return table

    # -- page tokens

    def _signature(self, scope: str, body: str) -> str:
        return hmac.new(self._secret, f"{scope}|{body}".encode(),
                        hashlib.sha256).hexdigest()

    def token(self, scope: str, offset: int) -> str:
        body = base64.urlsafe_b64encode(str(offset).encode()).decode().rstrip("=")
        return f"{body}.{self._signature(scope, body)}"

    def offset(self, scope: str, token) -> int:
        if not isinstance(token, str) or not token or len(token) > MAX_TOKEN_LENGTH:
            refuse_token()
        parts = token.split(".")
        if len(parts) != 2:
            refuse_token()
        body, signature = parts
        expected = self._signature(scope, body)
        if not hmac.compare_digest(signature.encode(), expected.encode()):
            refuse_token()
        padded = body + "=" * (-len(body) % 4)
        raw = base64.urlsafe_b64decode(padded.encode()).decode("ascii")
        if not raw.isdecimal() or int(raw) <= 0:
            refuse_token()
        return int(raw)


class _Budget:
    """At most MAX_REQUESTS field-service requests under one wall deadline."""

    def __init__(self):
        self.remaining = MAX_REQUESTS
        self.deadline = time.monotonic() + DEADLINE_SECONDS

    def take(self) -> float:
        left = self.deadline - time.monotonic()
        if self.remaining <= 0 or left <= 0:
            raise TransportError(
                "the field-metadata budget of "
                f"{MAX_REQUESTS} requests within {DEADLINE_SECONDS:g}s is exhausted"
            )
        self.remaining -= 1
        return left


def _like(value: str) -> str:
    """Match a literal underscore instead of LIKE's single-character wildcard."""
    return value.replace("_", "[_]")


def _query(condition: str) -> str:
    return "SELECT " + ", ".join(SELECTED) + " WHERE " + condition


def _record(message) -> dict:
    return {
        "name": message.name,
        "category": message.category.name,
        "data_type": message.data_type.name,
        "selectable": bool(message.selectable),
        "filterable": bool(message.filterable),
        "sortable": bool(message.sortable),
        "is_repeated": bool(message.is_repeated),
        "enum_values": list(message.enum_values),
        "selectable_with": list(message.selectable_with),
        "attribute_resources": list(message.attribute_resources),
    }


def _search(ctx, budget: _Budget, query: str) -> list:
    def once():
        timeout = budget.take()
        client = ctx.client()
        service = client.get_service("GoogleAdsFieldService")
        request = client.get_type("SearchGoogleAdsFieldsRequest")
        request.query = query
        request.page_size = PROVIDER_PAGE_SIZE
        return list(service.search_google_ads_fields(request=request, timeout=timeout))

    return ctx.retry(once)


def _fetch_family(ctx, top: str, budget: _Budget) -> dict:
    """The resource row and its descendants, restricted to the exact prefix."""
    table: dict[str, dict] = {}
    for condition in (f"name = '{top}'", f"name LIKE '{_like(top)}.%'"):
        for message in _search(ctx, budget, _query(condition)):
            record = _record(message)
            if record["name"] == top or record["name"].startswith(top + "."):
                table[record["name"]] = record
    return table


def family_table(ctx, name: str) -> dict:
    """The cached top-level family of ``name``: at most ``MAX_REQUESTS``
    prefix-limited field-service requests inside one shared deadline."""
    top = name.split(".")[0]
    budget = _Budget()
    return ctx.field_catalog.family(ctx, top, lambda: _fetch_family(ctx, top, budget))


def close_names(name: str, table: dict) -> list[str]:
    """Up to five catalogue names closest to ``name`` in its own family."""
    return difflib.get_close_matches(
        name, sorted(table), n=MAX_CLOSE_MATCHES, cutoff=CLOSE_MATCH_CUTOFF
    )


def _detail(record: dict) -> dict:
    values = record["enum_values"]
    return {
        "name": record["name"],
        "category": record["category"],
        "data_type": record["data_type"],
        "selectable": record["selectable"],
        "filterable": record["filterable"],
        "sortable": record["sortable"],
        "is_repeated": record["is_repeated"],
        "enum_values": values[:MAX_ENUM_VALUES],
        "enum_values_truncated": len(values) > MAX_ENUM_VALUES,
    }


def _not_found(resolved: str, table: dict) -> dict:
    """NOT_FOUND with the closest names from the same top-level family."""
    matches = close_names(resolved, table)
    payload = error_payload(
        "NOT_FOUND",
        f"no Google Ads API resource or field is named {sanitize(resolved)!r}",
    )
    payload["error"]["close_matches"] = matches
    return payload


def _selected_names(record: dict) -> dict:
    selectable_with = record["selectable_with"]
    return {
        "attribute_resources": sorted(record["attribute_resources"]),
        "segments": sorted(n for n in selectable_with if n.startswith("segments.")),
        "metrics": sorted(n for n in selectable_with if n.startswith("metrics.")),
    }


def run(ctx, *, name, page_size=None, page_token=None) -> dict:
    """Describe one resource, field or field prefix from the field service."""
    resolved = validate_name(name)
    size = validate_page_size(page_size)
    catalog = ctx.field_catalog
    scope = json.dumps([TOOL, resolved, size], sort_keys=True)
    offset = 0 if page_token is None else catalog.offset(scope, page_token)

    table = family_table(ctx, resolved)

    item = table.get(resolved)
    names: dict = {}
    if item is not None and item["category"] == "RESOURCE":
        # The resource's own attributes in full detail; its attribute
        # resources, segments and metrics as names only, straight from the
        # resource row, so no per-field lookup is needed for them.
        chosen = [record for key, record in table.items()
                  if key.startswith(resolved + ".") and record["category"] == "ATTRIBUTE"]
        names = _selected_names(item)
    elif item is not None:
        chosen = [item]
    else:
        chosen = [record for key, record in table.items()
                  if key.startswith(resolved + ".")]
    if not chosen:
        return _not_found(resolved, table)

    entries = [_detail(record) for record in sorted(chosen, key=lambda r: r["name"])]
    if offset and (offset >= len(entries) or offset % size):
        refuse_token()
    following = offset + size
    return {
        "name": resolved,
        "fields": entries[offset:following],
        **names,
        "next_page_token": (catalog.token(scope, following)
                            if following < len(entries) else None),
    }
