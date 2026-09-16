"""Bounded inspection and direct reporting for same-campaign URL experiments."""

from __future__ import annotations

from datetime import date, datetime
from itertools import islice
import json
import math
import re
import unicodedata
from zoneinfo import ZoneInfo

from ads_mcp.errors import ToolError, classify_exception
from ads_mcp.search_urls import numeric_id, resource


EXPERIMENT_TYPE = "PMAX_TEXT_CUSTOMIZATION_FINAL_URL_EXPANSION"
STATE_BYTES = 16 * 1024 * 1024
CATALOG_LIMIT = 100
EXPERIMENT_FIELDS = (
    "experiment_id", "resource_name", "name", "type", "status", "start_date",
    "end_date", "promote_status", "long_running_operation",
)
ARM_FIELDS = ("resource_name", "experiment", "name", "control", "traffic_split", "campaigns")
CAMPAIGN_FIELDS = (
    "id", "resource_name", "name", "status", "advertising_channel_type",
    "start_date_time", "end_date_time", "asset_automation_settings",
)
ACCOUNT_FIELDS = ("id", "resource_name", "currency_code", "time_zone")
METRICS = (
    "clicks", "control_clicks", "impressions", "control_impressions", "cost_micros",
    "control_cost_micros", "conversions", "control_conversions", "conversions_value",
    "control_conversion_value", "clicks_point_estimate", "clicks_margin_of_error",
    "clicks_p_value", "conversions_absolute_change_point_estimate",
    "conversions_absolute_change_margin_of_error", "conversions_absolute_change_p_value",
)
# Recognized account currencies; never infer a currency from a missing value.
CURRENCIES = frozenset("""
AED AFN ALL AMD ANG AOA ARS AUD AWG AZN BAM BBD BDT BGN BHD BIF BMD BND BOB
BRL BSD BTN BWP BYN BZD CAD CDF CHF CLP CNY COP CRC CUP CVE CZK DJF DKK DOP
DZD EGP ERN ETB EUR FJD FKP GBP GEL GHS GIP GMD GNF GTQ GYD HKD HNL HTG HUF
IDR ILS INR IQD IRR ISK JMD JOD JPY KES KGS KHR KMF KPW KRW KWD KYD KZT
LAK LBP LKR LRD LSL LYD MAD MDL MGA MKD MMK MNT MOP MRU MUR MVR MWK MXN
MYR MZN NAD NGN NIO NOK NPR NZD OMR PAB PEN PGK PHP PKR PLN PYG QAR RON RSD
RUB RWF SAR SBD SCR SDG SEK SGD SHP SLE SOS SRD SSP STN SVC SYP SZL THB TJS
TMT TND TOP TRY TTD TWD TZS UAH UGX USD UYU UZS VES VND VUV WST XAF XCD XCG
XOF XPF YER ZAR ZMW ZWG
""".split())


def _invalid(message):
    raise ToolError("INVALID_EXPERIMENT_ARGUMENT", message)


def _unverified():
    raise ToolError(
        "EXPERIMENT_STATE_UNVERIFIED",
        "Could not verify complete supported experiment, campaign and account state. "
        "Inspect the selected account and experiment before trying again.",
    )


def customer(ctx, value):
    if value is not None and (
        not isinstance(value, str)
        or re.fullmatch(r"(?:[0-9]{10}|[0-9]{3}-[0-9]{3}-[0-9]{4})", value) is None
    ):
        _invalid("customer_id requires a ten-digit account string or null")
    return ctx.resolve_customer(value)


def date_value(value):
    if not isinstance(value, str) or re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value) is None:
        _invalid("Dates require a real calendar date in YYYY-MM-DD form")
    try:
        return date.fromisoformat(value)
    except ValueError:
        _invalid("Dates require a real calendar date in YYYY-MM-DD form")


def _text(value, *, empty=False):
    if (not isinstance(value, str) or (not value and not empty)
            or any(unicodedata.category(char) in {"Cc", "Cs"} for char in value)):
        _unverified()
    return value


def _enum(message, field):
    raw = message._pb
    value = raw.DESCRIPTOR.fields_by_name[field].enum_type.values_by_number.get(getattr(raw, field))
    if value is None or value.name == "UNKNOWN":
        _unverified()
    return value.name


def _resource_id(value, cid, kind):
    prefix = resource(cid, kind, "")
    if not isinstance(value, str) or not value.startswith(prefix):
        _unverified()
    return numeric_id(value[len(prefix):], "provider resource ID")


def _encoded_size(value):
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))


def _bounded(value):
    if _encoded_size(value) > STATE_BYTES:
        _unverified()
    return value


def _scan(ctx, cid, query, limit, project):
    def read():
        result, identities, size = [], set(), 2
        for row in islice(ctx.search_iter(query + f" LIMIT {limit + 1}", cid), limit + 1):
            if len(result) == limit:
                _unverified()
            item = project(row)
            identity = item["resource_name"]
            if identity in identities:
                _unverified()
            identities.add(identity)
            size += _encoded_size(item) + 1
            if size > STATE_BYTES:
                _unverified()
            result.append(item)
        return sorted(result, key=lambda item: item["resource_name"])

    return ctx.retry_account_read(read, cid)


def _query(kind, fields, where):
    return "SELECT " + ", ".join(kind + "." + field for field in fields) + f" FROM {kind} WHERE {where}"


def _one(ctx, cid, kind, fields, where, project):
    rows = _scan(ctx, cid, _query(kind, fields, where), 1, lambda row: project(getattr(row, kind)))
    if len(rows) != 1:
        _unverified()
    return rows[0]


def account(ctx, cid):
    def project(message):
        if str(message.id) != cid or message.resource_name != f"customers/{cid}":
            _unverified()
        if message.currency_code not in CURRENCIES:
            _unverified()
        _text(message.time_zone)
        ZoneInfo(message.time_zone)
        return {"resource_name": message.resource_name, "customer_id": cid,
                "currency": message.currency_code, "time_zone": message.time_zone}

    result = _one(ctx, cid, "customer", ACCOUNT_FIELDS, f"customer.id = {cid}", project)
    return {key: value for key, value in result.items() if key != "resource_name"}


def _experiment(message, cid):
    ident = numeric_id(str(message.experiment_id), "provider experiment ID")
    kind, status, promote = (_enum(message, field) for field in ("type_", "status", "promote_status"))
    if (message.resource_name != resource(cid, "experiments", ident)
            or kind != EXPERIMENT_TYPE or status == "UNSPECIFIED"):
        _unverified()
    if date_value(message.end_date) < date_value(message.start_date):
        _unverified()
    return {"experiment_id": ident, "resource_name": message.resource_name,
            "name": _text(message.name), "type": kind, "status": status,
            "start_date": message.start_date, "end_date": message.end_date,
            "promote_status": promote,
            "long_running_operation": _text(message.long_running_operation, empty=True)}


def campaign(ctx, cid, ident):
    def project(message):
        if str(message.id) != ident or message.resource_name != resource(cid, "campaigns", ident):
            _unverified()
        status = _enum(message, "status")
        channel = _enum(message, "advertising_channel_type")
        if channel != "PERFORMANCE_MAX" or status == "UNSPECIFIED":
            _unverified()
        dates = []
        for value in (message.start_date_time, message.end_date_time):
            if re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}:[0-9]{2}", value) is None:
                _unverified()
            dates.append(datetime.fromisoformat(value))
        if dates[1] < dates[0]:
            _unverified()
        settings, seen = [], set()
        for setting in message.asset_automation_settings:
            kind = _enum(setting, "asset_automation_type")
            state = _enum(setting, "asset_automation_status")
            if kind == "UNSPECIFIED" or kind in seen:
                _unverified()
            seen.add(kind)
            settings.append({"asset_automation_type": kind, "asset_automation_status": state})
        return {"campaign_id": ident, "resource_name": message.resource_name,
                "name": _text(message.name), "status": status, "advertising_channel_type": channel,
                "start_date_time": message.start_date_time, "end_date_time": message.end_date_time,
                "asset_automation_settings": sorted(settings, key=lambda item: item["asset_automation_type"])}

    return _one(ctx, cid, "campaign", CAMPAIGN_FIELDS, f"campaign.id = {ident}", project)


def _graph(ctx, cid, experiment):
    identity = experiment["resource_name"]
    prefix = resource(cid, "experimentArms", experiment["experiment_id"] + "~")

    def project(row):
        message = row.experiment_arm
        if message.experiment != identity or not message.resource_name.startswith(prefix):
            _unverified()
        ident = numeric_id(message.resource_name[len(prefix):], "provider arm ID")
        campaigns = list(message.campaigns)
        if message.traffic_split != 50 or len(campaigns) != 1:
            _unverified()
        _resource_id(campaigns[0], cid, "campaigns")
        return {"resource_name": message.resource_name, "experiment": identity,
                "arm_id": ident, "name": _text(message.name), "control": message.control,
                "traffic_split": message.traffic_split, "campaigns": campaigns}

    arms = _scan(ctx, cid, _query("experiment_arm", ARM_FIELDS,
                 f"experiment_arm.experiment = '{identity}'"), 2, project)
    if (len(arms) != 2 or {item["control"] for item in arms} != {True, False}
            or arms[0]["campaigns"] != arms[1]["campaigns"]):
        _unverified()
    ident = _resource_id(arms[0]["campaigns"][0], cid, "campaigns")
    return _bounded({"experiment": experiment, "arms": arms, "campaign": campaign(ctx, cid, ident)})


def detail(ctx, cid, ident):
    experiment = _one(ctx, cid, "experiment", EXPERIMENT_FIELDS,
                      f"experiment.experiment_id = {ident}", lambda message: _experiment(message, cid))
    if experiment["experiment_id"] != ident:
        _unverified()
    return _graph(ctx, cid, experiment)


def _verified(ctx, call):
    try:
        return _bounded(call())
    except Exception as exc:
        error = classify_exception(exc, scrub=ctx.scrub)
        if error.code.startswith("AUTH_") or error.code == "ACCOUNT_NOT_ACCESSIBLE":
            raise error from None
        _unverified()


def inspect(ctx, *, experiment_id=None, customer_id=None):
    cid = customer(ctx, customer_id)
    ident = numeric_id(experiment_id, "experiment_id") if experiment_id is not None else None

    def read():
        metadata = account(ctx, cid)
        if ident is not None:
            return {**metadata, **detail(ctx, cid, ident)}
        experiments = _scan(ctx, cid, _query("experiment", EXPERIMENT_FIELDS,
                            f"experiment.type = '{EXPERIMENT_TYPE}'"), CATALOG_LIMIT,
                            lambda row: _experiment(row.experiment, cid))
        graphs, size = [], _encoded_size(metadata)
        for experiment in experiments:
            graph = _graph(ctx, cid, experiment)
            size += _encoded_size(graph) + 1
            if size > STATE_BYTES:
                _unverified()
            graphs.append(graph)
        return {**metadata, "complete": True, "experiments": graphs}

    return _verified(ctx, read)


def results(ctx, *, experiment_id, date_start, date_end, customer_id=None):
    cid = customer(ctx, customer_id)
    ident = numeric_id(experiment_id, "experiment_id")
    start, end = date_value(date_start), date_value(date_end)
    if not 0 <= (end - start).days <= 365:
        _invalid("Report windows require 1 to 366 inclusive days")

    def read():
        metadata = account(ctx, cid)
        graph = detail(ctx, cid, ident)
        identity = graph["experiment"]["resource_name"]

        def project(row):
            if row.experiment.resource_name != identity or str(row.experiment.experiment_id) != ident:
                _unverified()
            values = {}
            raw = row.metrics._pb
            for field in METRICS:
                value = getattr(raw, field) if raw.HasField(field) else None
                if value is not None:
                    if not math.isfinite(value):
                        _unverified()
                    if ((field.endswith(("clicks", "impressions", "cost_micros", "margin_of_error")) and value < 0)
                            or (field.endswith("p_value") and not 0 <= value <= 1)):
                        _unverified()
                values[field] = value
            return {"resource_name": identity, "values": values}

        selected = ", ".join(["experiment.resource_name", "experiment.experiment_id"]
                             + ["metrics." + field for field in METRICS])
        query = (f"SELECT {selected} FROM experiment WHERE experiment.experiment_id = {ident} "
                 f"AND segments.date BETWEEN '{date_start}' AND '{date_end}'")
        rows = _scan(ctx, cid, query, 1, project)
        values = rows[0]["values"] if rows else dict.fromkeys(METRICS)
        treatment = {field: values[field] for field in (
            "clicks", "impressions", "cost_micros", "conversions", "conversions_value")}
        control = {field: values["control_" + ("conversion_value" if field == "conversions_value" else field)]
                   for field in treatment}
        statistics = {
            "clicks": {"unit": "relative_change", **{field: values[field] for field in METRICS[10:13]}},
            "conversions": {"unit": "absolute_treatment_minus_control",
                            **{field: values[field] for field in METRICS[13:]}},
        }
        return {**metadata, "experiment_id": ident, "resource_name": identity,
                "date_start": date_start, "date_end": date_end, "no_data": not rows,
                "treatment": treatment, "control": control, "statistics": statistics}

    return _verified(ctx, read)
