"""Provider-validated, atomic creation of same-campaign URL experiments."""

from datetime import datetime
import hashlib
import json
import unicodedata
from zoneinfo import ZoneInfo

from ads_mcp import pmax_experiments as reads
from ads_mcp.errors import ToolError, classify_exception
from ads_mcp.search_urls import numeric_id, resource


TEXT = "TEXT_ASSET_AUTOMATION"
EXPANSION = "FINAL_URL_EXPANSION_TEXT_ASSET_AUTOMATION"
TOOL = "create_pmax_url_experiment"


def _name(value):
    if (not isinstance(value, str) or not value or value == "null"
            or value != value.strip() or unicodedata.normalize("NFC", value) != value
            or any(unicodedata.category(char) in {"Cc", "Cs"} for char in value)
            or len(value.encode("utf-8")) > 255):
        reads._invalid("name requires 1–255 UTF-8 bytes in NFC, without surrounding whitespace or controls")
    return value


def _inventory(ctx, cid):
    def experiment(row):
        message = row.experiment
        ident = numeric_id(str(message.experiment_id), "provider experiment ID")
        if message.resource_name != resource(cid, "experiments", ident):
            reads._unverified()
        enums = {field: reads._enum(message, attr) for field, attr in (
            ("type", "type_"), ("status", "status"), ("promote_status", "promote_status"))}
        if enums["type"] == "UNSPECIFIED" or enums["status"] == "UNSPECIFIED":
            reads._unverified()
        for value in (message.start_date, message.end_date):
            reads.date_value(value)
        return {"resource_name": message.resource_name, "experiment_id": ident,
                "name": reads._text(message.name), **enums,
                "start_date": message.start_date, "end_date": message.end_date,
                "long_running_operation": reads._text(message.long_running_operation, empty=True)}

    query = "SELECT " + ", ".join("experiment." + field for field in reads.EXPERIMENT_FIELDS) + " FROM experiment"
    experiments = reads._scan(ctx, cid, query, 1000, experiment)
    identities = {item["resource_name"] for item in experiments}

    def arm(row):
        message = row.experiment_arm
        parent = reads._resource_id(message.experiment, cid, "experiments")
        prefix = resource(cid, "experimentArms", parent + "~")
        if message.experiment not in identities or not message.resource_name.startswith(prefix):
            reads._unverified()
        numeric_id(message.resource_name[len(prefix):], "provider arm ID")
        campaigns = list(message.campaigns)
        if not campaigns or len(campaigns) != len(set(campaigns)) or not 0 <= message.traffic_split <= 100:
            reads._unverified()
        for identity in campaigns:
            reads._resource_id(identity, cid, "campaigns")
        return {"resource_name": message.resource_name, "experiment": message.experiment,
                "name": reads._text(message.name), "control": message.control,
                "traffic_split": message.traffic_split, "campaigns": sorted(campaigns)}

    query = "SELECT " + ", ".join("experiment_arm." + field for field in reads.ARM_FIELDS) + " FROM experiment_arm"
    arms = reads._scan(ctx, cid, query, 2000, arm)
    return {"experiments": experiments, "arms": arms}


def _state(ctx, cid, ident, name, start, end):
    metadata = reads.account(ctx, cid)
    campaign = reads.campaign(ctx, cid, ident)
    today = datetime.fromtimestamp(ctx.clock(), ZoneInfo(metadata["time_zone"])).date()
    if not 0 <= (start - today).days <= 365:
        reads._invalid("The experiment start must be today through 365 days ahead in the account timezone")
    if (start < datetime.fromisoformat(campaign["start_date_time"]).date()
            or end > datetime.fromisoformat(campaign["end_date_time"]).date()):
        reads._invalid("The experiment dates must fit within the verified campaign dates")
    settings = {item["asset_automation_type"]: item["asset_automation_status"]
                for item in campaign["asset_automation_settings"]}
    if (campaign["status"] != "ENABLED" or settings.get(EXPANSION) != "OPTED_OUT"
            or any(value not in {"OPTED_IN", "OPTED_OUT"} for value in settings.values())):
        reads._unverified()
    inventory = _inventory(ctx, cid)
    active = {item["resource_name"] for item in inventory["experiments"] if item["status"] != "REMOVED"}
    for item in inventory["experiments"]:
        if (item["resource_name"] in active
                and unicodedata.normalize("NFC", item["name"]).casefold() == name.casefold()):
            raise ToolError("EXPERIMENT_COLLISION", "A nonremoved experiment already uses this name")
    if any(item["experiment"] in active and campaign["resource_name"] in item["campaigns"]
           for item in inventory["arms"]):
        raise ToolError("EXPERIMENT_COLLISION", "The campaign belongs to a nonremoved experiment; resolve it in Google Ads first")
    return reads._bounded({"account": metadata, "campaign": campaign, **inventory})


def _request(ctx, cid, campaign, name, start, end, settings, *, validate_only):
    client = ctx.client()
    request = client.get_type("MutateGoogleAdsRequest")
    request.customer_id = cid
    request.validate_only = validate_only
    request.partial_failure = False
    operation = client.get_type("MutateOperation")
    experiment = operation.experiment_operation.create
    experiment.resource_name = resource(cid, "experiments", "-1")
    experiment.name = name
    experiment.type_ = reads.EXPERIMENT_TYPE
    experiment.start_date = start
    experiment.end_date = end
    request.mutate_operations.append(operation)
    for ident, control, label in (("-2", True, "Control"), ("-3", False, "Treatment")):
        operation = client.get_type("MutateOperation")
        arm = operation.experiment_arm_operation.create
        arm.resource_name = resource(cid, "experimentArms", "-1~" + ident)
        arm.experiment = experiment.resource_name
        arm.name = label
        arm.control = control
        arm.traffic_split = 50
        arm.campaigns.append(campaign)
        request.mutate_operations.append(operation)
    operation = client.get_type("MutateOperation")
    update = operation.campaign_operation
    update.update.resource_name = campaign
    update.update.asset_automation_settings = settings
    update.update_mask.paths.append("asset_automation_settings")
    request.mutate_operations.append(operation)
    return request


def _receipt(response, cid, campaign):
    items = response.mutate_operation_responses
    expected = ("experiment_result", "experiment_arm_result", "experiment_arm_result", "campaign_result")
    if len(items) != 4 or any(item._pb.WhichOneof("response") != kind for item, kind in zip(items, expected)):
        reads._unverified()
    identity = items[0].experiment_result.resource_name
    ident = reads._resource_id(identity, cid, "experiments")
    arms = [item.experiment_arm_result.resource_name for item in items[1:3]]
    prefix = resource(cid, "experimentArms", ident + "~")
    if len(set(arms)) != 2 or items[3].campaign_result.resource_name != campaign:
        reads._unverified()
    for arm in arms:
        if not arm.startswith(prefix):
            reads._unverified()
        numeric_id(arm[len(prefix):], "provider arm ID")
    return ident, identity, arms


def plan(ctx, *, campaign_id, name, date_start, date_end, customer_id=None):
    ident = numeric_id(campaign_id, "campaign_id")
    name = _name(name)
    start, end = reads.date_value(date_start), reads.date_value(date_end)
    if not 0 <= (end - start).days <= 365:
        reads._invalid("Experiment duration requires 1 to 366 inclusive days")
    # Resolve write identity locally before any account access.
    if customer_id is not None:
        if not isinstance(customer_id, str):
            reads._invalid("customer_id requires a ten-digit account string or null")
        from ads_mcp.config import normalize_customer_id

        if normalize_customer_id(customer_id) != ctx.config.customer_id:
            raise ToolError("PLAN_CUSTOMER_MISMATCH", "Experiments can only be created in the configured account")
    cid = reads.customer(ctx, customer_id)
    read = lambda current: _state(current, cid, ident, name, start, end)
    state = reads._verified(ctx, lambda: read(ctx))
    campaign = state["campaign"]["resource_name"]
    settings = {item["asset_automation_type"]: item["asset_automation_status"]
                for item in state["campaign"]["asset_automation_settings"]}
    settings.update({TEXT: "OPTED_IN", EXPANSION: "OPTED_IN"})
    after = [{"asset_automation_type": key, "asset_automation_status": value}
             for key, value in sorted(settings.items())]

    def request(current, validate_only):
        return _request(current, cid, campaign, name, date_start, date_end, after, validate_only=validate_only)

    try:
        ctx.client().get_service("GoogleAdsService").mutate(request=request(ctx, True), retry=None)
    except Exception as exc:
        error = classify_exception(exc, scrub=ctx.scrub)
        if error.code.startswith("AUTH_"):
            raise error from None
        raise ToolError("EXPERIMENT_VALIDATION_FAILED", "Google Ads validate-only refused the experiment; no plan or real mutation was created") from None

    def recheck(current):
        try:
            fresh = reads._verified(current, lambda: read(current))
        except ToolError:
            raise ToolError("STALE_PLAN", "Experiment eligibility is no longer verifiable; stage a fresh plan") from None
        if fresh != state:
            raise ToolError("STALE_PLAN", "Experiment eligibility changed; stage a fresh plan")

    def execute(current):
        recovery = "Inspect experiments and campaign settings before considering another creation; do not retry blindly."
        try:
            response = current.client().get_service("GoogleAdsService").mutate(
                request=request(current, False), retry=None)
        except Exception as exc:
            error = classify_exception(exc, scrub=current.scrub)
            current.audit_auth_failure(error)
            return {"submitted": True, "applied": False, "verification": "unknown",
                    "observation_error": "The creation response was not received; application is possible.",
                    "recovery": recovery}
        try:
            created_id, identity, arm_ids = _receipt(response, cid, campaign)
        except Exception:
            return {"submitted": True, "applied": False, "verification": "unknown",
                    "observation_error": "The creation receipt could not be verified; application is possible.",
                    "recovery": recovery}
        result = {"submitted": True, "applied": False, "experiment_id": created_id,
                  "resource_name": identity, "verification": "unknown",
                  "recovery": "Inspect the returned experiment identity and campaign settings; do not repeat creation."}
        if response.partial_failure_error.code != 0:
            result["observation_error"] = (
                "The creation receipt contains contradictory status and resource "
                "results; application is possible but cannot be confirmed."
            )
            return result
        try:
            observed = reads._verified(current, lambda: reads.detail(current, cid, created_id))
            experiment = observed["experiment"]
            matches = all(experiment[key] == value for key, value in {
                "name": name, "type": reads.EXPERIMENT_TYPE, "start_date": date_start, "end_date": date_end,
            }.items())
            expected_arms = [(True, "Control"), (False, "Treatment")]
            by_identity = {arm["resource_name"]: arm for arm in observed["arms"]}
            for arm_id, (control, label) in zip(arm_ids, expected_arms):
                arm = by_identity.get(arm_id, {})
                matches = matches and all(arm.get(key) == value for key, value in {
                    "name": label, "control": control, "traffic_split": 50, "campaigns": [campaign],
                }.items())
            matches = matches and observed["campaign"]["asset_automation_settings"] == after
            result["observed"] = observed["experiment"]
            result["verification"] = "verified" if matches else "failed"
            result["applied"] = bool(matches)
            if not matches:
                result["observation_error"] = "Observed experiment or campaign settings differ from the submitted request."
        except Exception:
            result["observation_error"] = "Creation was accepted, but its resulting state could not be verified."
        return result

    return {"tool": TOOL, "summary": "Create a same-campaign 50/50 final URL expansion experiment. "
            "Provider validate-only succeeded; this is separate from local confirmation preview and is not a serving guarantee. "
            "Treatment enables text customization and final URL expansion; delivery and spend can change. "
            "State is rechecked before submission, without preventing external races.",
            "operations": [{"name": name, "type": reads.EXPERIMENT_TYPE,
                            "start_date": date_start, "end_date": date_end, "before": state,
                            "after": after, "provider_validation": "succeeded",
                            "arms": [{"name": label, "control": control, "traffic_split": 50,
                                      "campaigns": [campaign]} for control, label in ((True, "Control"), (False, "Treatment"))],
                            "state_fingerprint": hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest()}],
            "irreversible": False, "rechecks": [recheck], "execute": execute}
