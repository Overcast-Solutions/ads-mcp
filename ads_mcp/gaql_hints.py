"""Field-metadata guidance for run_gaql query errors.

A GAQL query error that names a field is almost always answerable from the
API's own schema: the misspelling has a near neighbour, the required field has
an exact name, the prohibited field is selectable with other resources, and a
rejected operator follows from the field's type, repeatedness and enum values.
This module reads those facts from GoogleAdsFieldService — the same bounded
prefix lookup and shared deadline ``get_resource_metadata`` uses — and returns
them as ``provider_error`` and ``hints`` beside the untouched provider error.

Three invariants hold without exception:

* the query is never rewritten and never resubmitted (nothing here issues a
  search request; only the field service is consulted), so transport retries
  of a transient fault remain F003's and nothing else;
* ``error.code`` stays ``GAQL_ERROR`` and ``error.message`` is the provider's
  own text, hints or no hints;
* a lookup that fails, times out or comes back empty yields the original
  error with no hints and no second error.

Hints carry field names, schema facts and at most a sanitized 64-character
excerpt of the offending name — never credentials, account data, raw provider
payloads or query text.
"""

from __future__ import annotations

import re

from ads_mcp import field_metadata

# The twelve named query errors, by the kind of answer the schema can give.
UNRECOGNIZED = ("UNRECOGNIZED_FIELD", "BAD_FIELD_NAME")
REQUIRED_IN_SELECT = ("EXPECTED_REFERENCED_FIELD_IN_SELECT_CLAUSE",
                      "EXPECTED_DIMENSION_FIELD_IN_SELECT_CLAUSE",
                      "REQUIRED_SEGMENT_FIELD_MISSING")
PROHIBITED_HERE = ("PROHIBITED_FIELD_IN_SELECT_CLAUSE",
                   "PROHIBITED_RESOURCE_TYPE_IN_SELECT_CLAUSE",
                   "PROHIBITED_METRIC_IN_SELECT_OR_WHERE_CLAUSE",
                   "PROHIBITED_SEGMENT_IN_SELECT_OR_WHERE_CLAUSE",
                   "PROHIBITED_SEGMENT_WITH_METRIC_IN_SELECT_OR_WHERE_CLAUSE")
OPERATOR = ("OPERATOR_FIELD_MISMATCH", "BAD_OPERATOR")

MAX_HINTS = 5
MAX_HINT_MESSAGE = 200
MAX_LISTED_RESOURCES = 5
MAX_LISTED_VALUES = 6
# Values every Google Ads enum carries; neither helps a caller fix a filter.
PLACEHOLDER_VALUES = ("UNSPECIFIED", "UNKNOWN")

# A quoted name in provider prose: single quotes only, and only a dotted name
# that satisfies the field-name rule is treated as a field.
_QUOTED = re.compile(r"'([^']*)'")


def _clip(message: str) -> str:
    return message[:MAX_HINT_MESSAGE]


def _hint(kind: str, message: str, fields) -> dict:
    return {"kind": kind, "message": _clip(message), "fields": list(fields)}


def _query_errors(exc):
    """Each GoogleAdsError on the failure whose code really is a query error."""
    for error in exc.failure.errors:
        if error.error_code._pb.WhichOneof("error_code") != "query_error":
            continue
        yield error, error.error_code.query_error.name


def _listed(exc):
    """The first listed query error on the failure, with its enum name."""
    for error, name in _query_errors(exc):
        if name in FAMILIES:
            return error, name
    return None, None


def offending_field(error) -> str | None:
    """The field the provider blamed: its trigger, else its own message.

    A trigger is authoritative when it is a valid field name. Otherwise the
    first single-quoted dotted name in the message that satisfies the same
    rule is used; prose without one leaves the error unguided.
    """
    trigger = error.trigger.string_value
    if field_metadata.is_field_name(trigger):
        return trigger
    for quoted in _QUOTED.findall(error.message or ""):
        if "." in quoted and field_metadata.is_field_name(quoted):
            return quoted
    return None


def _close_name_hints(field: str, record, table) -> list[dict]:
    """Unrecognized field: the closest real names in the same family."""
    matches = field_metadata.close_names(field, table)
    if not matches:
        return []
    return [_hint(
        "close_field_names",
        f"No field is named {field_metadata.sanitize(field)!r}. The closest "
        f"names in this resource's family are listed in fields.",
        matches,
    )]


def _select_hints(field: str, record, table) -> list[dict]:
    """Field required in SELECT: the exact name the provider asked for."""
    if record is None:
        return []
    return [_hint(
        "add_to_select",
        f"This query must also select {field_metadata.sanitize(field)!r}. Add "
        f"that exact field to the SELECT clause and run the query again.",
        [record["name"]],
    )]


def _selectable_with_hints(field: str, record, table) -> list[dict]:
    """Prohibited here: resources the field can be selected with instead."""
    if record is None:
        return []
    resources = sorted(name for name in record["selectable_with"] if "." not in name)
    if not resources:
        return []
    listed = resources[:MAX_LISTED_RESOURCES]
    extra = ("" if len(listed) == len(resources)
             else f" {len(resources)} resources accept it; the first "
                  f"{MAX_LISTED_RESOURCES} are listed.")
    return [_hint(
        "selectable_with",
        f"{field_metadata.sanitize(field)!r} cannot be selected with the "
        f"resource in this FROM clause. The resources in fields accept "
        f"it.{extra}",
        listed,
    )]


def _operator_hints(field: str, record, table) -> list[dict]:
    """Rejected operator: the schema facts an operator has to match."""
    if record is None:
        return []
    name = record["name"]
    hints = [_hint(
        "field_facts",
        f"The operator does not match this field. {name} facts from the API "
        f"schema: category {record['category']}, data type "
        f"{record['data_type']}, filterable: {'yes' if record['filterable'] else 'no'}, "
        f"repeated: {'yes' if record['is_repeated'] else 'no'}.",
        [name],
    )]
    values = [value for value in record["enum_values"]
              if value not in PLACEHOLDER_VALUES]
    if record["data_type"] == "ENUM" and values:
        listed = values[:MAX_LISTED_VALUES]
        # Prose, not an ellipsis: a trailing ", ..." ran into the sentence's
        # own full stop and read as doubled punctuation.
        more = ("" if len(listed) == len(values)
                else f", and {len(values) - len(listed)} more")
        hints.append(_hint(
            "enum_values",
            f"Compare against this field's own enum values: "
            f"{', '.join(listed)}{more}.",
            [name],
        ))
    return hints


FAMILIES = {
    **{enum: _close_name_hints for enum in UNRECOGNIZED},
    **{enum: _select_hints for enum in REQUIRED_IN_SELECT},
    **{enum: _selectable_with_hints for enum in PROHIBITED_HERE},
    **{enum: _operator_hints for enum in OPERATOR},
}


def _hints(ctx, field: str, build) -> list[dict]:
    """Field-service facts for one name, or nothing at all.

    Any lookup failure — rejection, unavailability, an exhausted deadline, a
    revoked credential — leaves the caller with the provider's original error
    rather than a second one, so the exception stops here.
    """
    try:
        table = field_metadata.family_table(ctx, field)
        hints = build(field, table.get(field), table)
    except Exception:
        return []
    return [hint for hint in hints if hint["message"]][:MAX_HINTS]


def details(ctx, exc) -> dict:
    """``provider_error`` and any ``hints`` for a run_gaql query failure."""
    error, enum = _listed(exc)
    if error is None:
        return {}
    field = offending_field(error)
    if field is None:
        return {"provider_error": enum}
    hints = _hints(ctx, field, FAMILIES[enum])
    if not hints:
        return {"provider_error": enum}
    return {"provider_error": enum, "hints": hints}
