"""Guarded lifecycle actions and authenticated, bounded promotion observation."""

from datetime import datetime
import hashlib
import json
import unicodedata
from zoneinfo import ZoneInfo

from google.api_core.operation import Operation
from google.ads.googleads.v25.services.types import PromoteExperimentMetadata
from google.ads.googleads.v25.services.services.experiment_service.pagers import (
    ListExperimentAsyncErrorsPager,
)
from google.longrunning import operations_pb2
from google.protobuf import empty_pb2
from google.rpc import code_pb2

from ads_mcp import pmax_experiments as reads
from ads_mcp.errors import ToolError, classify_exception
from ads_mcp.pmax_experiment_create import TEXT, EXPANSION
from ads_mcp.search_urls import numeric_id


TIMEOUT = 60.0
RECOVERY = "Inspect the experiment and its latest operation before another action; do not retry blindly."


def _handle(value):
    if (not isinstance(value, str) or not value or value == "null" or len(value) > 2048
            or any(char.isspace() or unicodedata.category(char) in {"Cc", "Cs"} for char in value)):
        reads._invalid("operation_name requires 1–2048 characters without whitespace or controls")
    return value


def _state(ctx, cid, ident, *, eligible=False):
    state = reads._bounded({**reads.account(ctx, cid),
                           **reads.detail(ctx, cid, ident, observe_promotion=not eligible)})
    if eligible:
        experiment = state["experiment"]
        today = datetime.fromtimestamp(ctx.clock(), ZoneInfo(state["time_zone"])).date()
        if (experiment["status"] != "ENABLED" or experiment["promote_status"] != "NOT_STARTED"
                or not reads.date_value(experiment["start_date"]) <= today <= reads.date_value(experiment["end_date"])
                or any(item["asset_automation_status"] not in {"OPTED_IN", "OPTED_OUT"}
                       for item in state["campaign"]["asset_automation_settings"])):
            reads._unverified()
    return state


def _code(value):
    try:
        return {"code": value, "name": code_pb2.Code.Name(value)}
    except ValueError:
        reads._unverified()


def _async_errors(ctx, cid, ident, identity, name):
    result = {"scope": "experiment/latest-operation", "complete": False, "errors": []}
    token, seen, size = "", set(), 0
    try:
        latest = reads._verified(ctx, lambda: _state(ctx, cid, ident))
        if latest["experiment"]["long_running_operation"] != name:
            result["reason"] = "LATEST_OPERATION_CHANGED"
            return result
        service = ctx.client().get_service("ExperimentService")
        for _ in range(10):
            request = ctx.client().get_type("ListExperimentAsyncErrorsRequest")
            request.resource_name = identity
            request.page_size = 100
            request.page_token = token
            pager = ctx.retry_account_read(lambda: service.list_experiment_async_errors(
                request=request, retry=None, timeout=TIMEOUT), cid)
            if not isinstance(pager, ListExperimentAsyncErrorsPager):
                result["reason"] = "INVALID_PAGE"
                return result
            # Read only this response; SDK pager iteration would fetch more pages.
            page = pager._response
            size += page._pb.ByteSize()
            if size > reads.STATE_BYTES:
                result["reason"] = "BYTE_LIMIT"
                return result
            for status in page.errors:
                if len(result["errors"]) == 1000:
                    result["reason"] = "STATUS_LIMIT"
                    return result
                result["errors"].append(_code(status.code))
            token = page.next_page_token
            if not token:
                result["complete"] = True
                return result
            if token in seen:
                result["reason"] = "REPEATED_PAGE_TOKEN"
                return result
            if len(result["errors"]) == 1000:
                result["reason"] = "STATUS_LIMIT"
                return result
            seen.add(token)
        result["reason"] = "PAGE_LIMIT"
    except Exception:
        result["reason"] = "DETAIL_READ_UNVERIFIED"
    return result


def _bound_operation(operation, identity, expected_name=None):
    if not isinstance(operation, operations_pb2.Operation):
        reads._unverified()
    if operation.ByteSize() > reads.STATE_BYTES:
        reads._unverified()
    name = _handle(operation.name)
    if expected_name is not None and name != expected_name:
        reads._unverified()
    metadata = PromoteExperimentMetadata()._pb
    if not operation.metadata.Unpack(metadata) or metadata.experiment != identity:
        reads._unverified()
    terminal = operation.WhichOneof("result")
    if (operation.done and terminal is None) or (not operation.done and terminal is not None):
        reads._unverified()
    if terminal == "response" and not operation.response.Unpack(empty_pb2.Empty()):
        reads._unverified()
    if terminal == "error":
        _code(operation.error.code)
    return name, terminal


def _observe(ctx, cid, ident, operation, state, *, expected_name=None):
    identity = state["experiment"]["resource_name"]
    name, terminal = _bound_operation(operation, identity, expected_name)
    result = {key: state[key] for key in ("customer_id", "currency", "time_zone")}
    result.update({"experiment_id": ident, "resource_name": identity, "operation_name": name,
                   "submitted": True, "completed": bool(operation.done), "applied": False,
                   "state": "pending", "promote_status": state["experiment"]["promote_status"],
                   "warnings": state["experiment"]["promote_status"] == "COMPLETED_WITH_WARNING"})
    if terminal == "error":
        result.update(state="failed", operation_error=_code(operation.error.code),
                      async_errors=_async_errors(ctx, cid, ident, identity, name))
    elif terminal == "response":
        result["state"] = "completed"
        try:
            observed = reads._verified(ctx, lambda: _state(ctx, cid, ident))
            if observed["experiment"]["long_running_operation"] != name:
                reads._unverified()
            result["promote_status"] = observed["experiment"]["promote_status"]
            result["warnings"] = result["promote_status"] == "COMPLETED_WITH_WARNING"
            settings = {item["asset_automation_type"]: item["asset_automation_status"]
                        for item in observed["campaign"]["asset_automation_settings"]}
            result["applied"] = (result["promote_status"] in {"COMPLETED", "COMPLETED_WITH_WARNING"}
                                 and all(settings.get(key) == "OPTED_IN" for key in (TEXT, EXPANSION)))
        except Exception:
            result["observation_error"] = "The operation completed, but its application could not be verified."
            result["recovery"] = RECOVERY
    return result


def observe(ctx, *, experiment_id, operation_name, customer_id=None):
    ident = numeric_id(experiment_id, "experiment_id")
    name = _handle(operation_name)
    cid = reads.customer(ctx, customer_id)
    state = reads._verified(ctx, lambda: _state(ctx, cid, ident))
    if state["experiment"]["long_running_operation"] != name:
        raise ToolError("EXPERIMENT_OPERATION_UNBOUND", "The supplied operation is not the experiment's latest operation")
    try:
        transport = ctx.client().get_service("ExperimentService").transport
        operation = ctx.retry_account_read(lambda: transport.operations_client.get_operation(
            name=name, retry=None, timeout=TIMEOUT), cid)
        return _observe(ctx, cid, ident, operation, state, expected_name=name)
    except Exception as exc:
        error = classify_exception(exc, scrub=ctx.scrub)
        if error.code.startswith("AUTH_") or error.code == "ACCOUNT_NOT_ACCESSIBLE":
            raise error from None
        raise ToolError("EXPERIMENT_OBSERVATION_UNVERIFIED",
                        "Operation progress could not be verified. " + RECOVERY) from None


def plan(ctx, *, action, experiment_id, customer_id=None):
    ident = numeric_id(experiment_id, "experiment_id")
    cid = reads.customer(ctx, customer_id)
    if cid != ctx.config.customer_id:
        raise ToolError("PLAN_CUSTOMER_MISMATCH", "Lifecycle actions require the configured account")
    state = reads._verified(ctx, lambda: _state(ctx, cid, ident, eligible=True))
    identity = state["experiment"]["resource_name"]
    ending = action == "end"
    tool = "end_pmax_url_experiment" if ending else "promote_pmax_url_experiment"

    def submit(current, validate_only):
        request = current.client().get_type("EndExperimentRequest" if ending else "PromoteExperimentRequest")
        if ending:
            request.experiment = identity
        else:
            request.resource_name = identity
        request.validate_only = validate_only
        service = current.client().get_service("ExperimentService")
        method = service.end_experiment if ending else service.promote_experiment
        return method(request=request, retry=None)

    try:
        submit(ctx, True)
    except Exception as exc:
        error = classify_exception(exc, scrub=ctx.scrub)
        if error.code.startswith("AUTH_"):
            raise error from None
        raise ToolError("EXPERIMENT_VALIDATION_FAILED",
                        "Google Ads validate-only refused the lifecycle action; no plan or real action was created") from None

    def recheck(current):
        try:
            fresh = reads._verified(current, lambda: _state(current, cid, ident, eligible=True))
        except ToolError:
            raise ToolError("STALE_PLAN", "Experiment eligibility is no longer verifiable; stage a fresh plan") from None
        if fresh != state:
            raise ToolError("STALE_PLAN", "Experiment state changed; stage a fresh plan")

    def execute(current):
        result = {"submitted": True, "applied": False, "state": "unknown",
                  "experiment_id": ident, "resource_name": identity, "recovery": RECOVERY}
        try:
            response = submit(current, False)
        except Exception as exc:
            current.audit_auth_failure(classify_exception(exc, scrub=current.scrub))
            result["observation_error"] = "The lifecycle response was not received; the action may have been accepted."
            return result
        if ending:
            result["state"] = "submitted"
            try:
                # The generated synchronous EndExperiment method returns None.
                if response is not None and not isinstance(response, empty_pb2.Empty):
                    reads._unverified()
                observed = reads._verified(current, lambda: _state(current, cid, ident))
                result["observed"] = observed["experiment"]
            except Exception:
                result["observation_error"] = "End was accepted, but its resulting state could not be verified."
            return result
        try:
            if not isinstance(response, Operation):
                reads._unverified()
            # Accessing the underlying message never calls done() or result().
            return {**_observe(current, cid, ident, response.operation, state), "recovery": RECOVERY}
        except Exception:
            result["observation_error"] = "Promotion was accepted, but its operation receipt could not be verified."
            return result

    effect = ("End the experiment immediately. This workflow cannot resume it, and observed status is reported separately."
              if ending else "Promote treatment settings permanently. Submission may be pending; this workflow cannot undo promotion.")
    return {"tool": tool, "summary": effect + " Provider validate-only succeeded; local confirmation preview is separate. "
            "Complete state and account-local time are rechecked before one submission; external races remain possible.",
            "operations": [{"experiment": identity, "action": action, "before": state,
                            "provider_validation": "succeeded",
                            "state_fingerprint": hashlib.sha256(json.dumps(state, sort_keys=True).encode()).hexdigest()}],
            "irreversible": True, "rechecks": [recheck], "execute": execute}
