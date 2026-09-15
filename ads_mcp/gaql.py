"""GAQL execution: faithful passthrough with lossless field projection.

The SELECT list is the projection contract: every selected field appears
in every row of JSON, CSV and table output, including nested fields. Zeros
remain zeros and unset fields are explicit nulls.
"""

from __future__ import annotations

import csv as csv_mod
import io
import re

from google.ads.googleads.errors import GoogleAdsException

from ads_mcp.errors import ToolError, cloud_project_access_error
from ads_mcp.continuation import retained

_SELECT_RE = re.compile(r"^\s*SELECT\s+(?P<fields>.+?)\s+FROM\s", re.IGNORECASE | re.DOTALL)

MAX_PAGE_SIZE = 10_000


def selected_fields(query: str) -> list[str]:
    match = _SELECT_RE.match(query)
    if not match:
        raise ToolError(
            "INVALID_QUERY",
            "query must be a GAQL SELECT statement (SELECT ... FROM ...)",
        )
    return [f.strip() for f in match.group("fields").split(",") if f.strip()]


def validate_query(query: str) -> list[str]:
    if not query or not query.strip():
        raise ToolError("INVALID_QUERY", "query is empty")
    head = query.strip().split(None, 1)[0].upper()
    if head != "SELECT":
        raise ToolError(
            "INVALID_QUERY",
            f"GAQL is read-only here: statement must start with SELECT, got {head}",
        )
    return selected_fields(query)


def validate_page_size(page_size) -> int | None:
    if page_size is None:
        return None
    if type(page_size) is not int:
        raise ToolError("INVALID_PAGE_SIZE", f"page_size {page_size!r} is not an integer")
    value = page_size
    if value <= 0 or value > MAX_PAGE_SIZE:
        raise ToolError(
            "INVALID_PAGE_SIZE",
            f"page_size {value} is outside 1..{MAX_PAGE_SIZE}",
        )
    return value


def extract_field(row, dotted: str):
    """Walk a dotted GAQL field path on a proto-plus row.

    Returns an explicit None when any message segment on the path is unset —
    a selected field never silently disappears.
    """
    obj = row
    for part in dotted.split("."):
        pb = getattr(obj, "_pb", None)
        if pb is not None:
            descriptor = pb.DESCRIPTOR.fields_by_name.get(part)
            if descriptor is None:
                # The SDK escapes names such as GAQL's type as type_. Its
                # descriptor retains the public spelling in json_name.
                descriptor = pb.DESCRIPTOR.fields_by_camelcase_name.get(part)
            if descriptor is None:
                raise ToolError(
                    "INVALID_QUERY",
                    f"unknown field segment {part!r} in selected field {dotted!r}",
                )
            part = descriptor.name
            if descriptor.message_type is not None:
                try:
                    if not pb.HasField(part):
                        return None
                except ValueError:
                    pass  # repeated message field — no presence semantics
        obj = getattr(obj, part)
    return leaf_value(obj)


def leaf_value(value):
    name = getattr(value, "name", None)
    if name is not None and hasattr(type(value), "__members__"):
        return name  # proto-plus enum -> its symbolic name
    if isinstance(value, (list, tuple)) or type(value).__name__ in (
        "Repeated",
        "RepeatedComposite",
        "RepeatedScalarContainer",
    ):
        return [leaf_value(v) for v in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    pb = getattr(value, "_pb", None)
    if pb is not None:
        from google.protobuf import json_format

        return json_format.MessageToDict(pb, preserving_proto_field_name=True)
    return str(value)


def nest(fields: list[str], values: list) -> dict:
    out: dict = {}
    for dotted, value in zip(fields, values):
        node = out
        parts = dotted.split(".")
        for part in parts[:-1]:
            nxt = node.get(part)
            if not isinstance(nxt, dict):
                nxt = {}
                node[part] = nxt
            node = nxt
        node[parts[-1]] = value
    return out


@retained("rows")
def _read_rows(ctx, *, query, customer_id, format, page_size, page_token):
    fields = selected_fields(query)
    rows = ctx.search_iter(query, customer_id=customer_id)
    return {
        "customer_id": customer_id,
        "rows": (nest(fields, [extract_field(row, field) for field in fields]) for row in rows),
    }


def run(ctx, *, query: str, customer_id=None, format: str = "json",
        page_size=None, page_token=None) -> dict:
    fields = validate_query(query)
    size = validate_page_size(page_size)
    fmt = (format or "json").strip().lower()
    if fmt not in ("json", "table", "csv"):
        raise ToolError("INVALID_FORMAT", f"format must be json, table, or csv; got {format!r}")
    cid = ctx.resolve_customer(customer_id)
    try:
        result = _read_rows(ctx, query=query, customer_id=cid, format=fmt,
                            page_size=size, page_token=page_token)
    except GoogleAdsException as exc:
        project_error = cloud_project_access_error(exc)
        if project_error is not None:
            raise project_error from None
        messages = "; ".join(
            e.message for e in exc.failure.errors if getattr(e, "message", "")
        ) or "the Google Ads API rejected the query"
        raise ToolError("GAQL_ERROR", messages) from None
    rows = result.pop("rows")
    if fmt == "json":
        return {**result, "format": fmt, "fields": fields, "rows": rows}

    def value(row, field):
        for part in field.split("."):
            if row is None:
                return None
            row = row[part]
        return row

    projected = [[value(row, field) for field in fields] for row in rows]
    if fmt == "table":
        return {**result, "format": fmt, "columns": fields, "rows": projected}
    buf = io.StringIO()
    writer = csv_mod.writer(buf)
    writer.writerow(fields)
    for values in projected:
        writer.writerow(["" if v is None else v for v in values])
    return {**result, "format": fmt, "csv": buf.getvalue()}
