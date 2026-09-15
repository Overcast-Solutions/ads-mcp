"""The tool catalog: single source of truth for names, kinds, registration.

Kinds: ``read`` registers always; ``mutation`` (plan-returning) and ``apply``
(confirm_and_apply) register only when ADS_MCP_READ_ONLY is explicitly false.
docs/tools.md is generated from this registry.
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from dataclasses import dataclass

import anyio
from anyio.lowlevel import RunVar
from mcp.server.mcpserver.utilities.func_metadata import FuncMetadata
from pydantic import ConfigDict, PrivateAttr, ValidationError, create_model
from pydantic_core import PydanticCustomError

from ads_mcp.errors import classify_exception

KIND_READ = "read"
KIND_MUTATION = "mutation"
KIND_APPLY = "apply"


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    kind: str  # KIND_READ | KIND_MUTATION | KIND_APPLY


def guarded(ctx, fn, name: str | None = None, *, plan_id=None):
    """Wrap a tool: structured error payloads out, secrets scrubbed, never a
    raised exception across the MCP boundary. Auth failures land in the
    audit log as observational events."""

    @functools.wraps(fn)
    def _wrapped(**kwargs):
        ctx.current_tool = name or getattr(fn, "__name__", "tool")
        ctx.current_customer = None
        ctx.current_plan_id = plan_id
        ctx.clear_audit_loss()
        try:
            return fn(**kwargs)
        except BaseException as exc:  # noqa: BLE001 — classified & returned
            err = classify_exception(exc, scrub=ctx.scrub)
            ctx.audit_auth_failure(err)
            return {"error": {"code": err.code, "message": ctx.scrub(err.message)}}
        finally:
            ctx.current_tool = ""
            ctx.current_customer = None
            ctx.current_plan_id = None
            ctx.clear_audit_loss()

    return _wrapped


class _PrivateValidationMetadata(FuncMetadata):
    """Scrub argument errors before MCP can wrap or log their diagnostics."""

    _scrub: Callable[[str], str] = PrivateAttr()

    def validate_arguments(self, arguments_to_validate):
        try:
            return super().validate_arguments(arguments_to_validate)
        except ValidationError as exc:
            # MCP distinguishes caller validation from unexpected crashes.
            # Rebuild only genuine validation failures, retaining safe locations
            # and rendered messages but no raw input or exception context.
            errors = [
                {
                    "type": PydanticCustomError(
                        self._scrub(error["type"]), self._scrub(error["msg"]),
                    ),
                    "loc": tuple(self._scrub(part) if isinstance(part, str) else part
                                 for part in error["loc"]),
                }
                for error in exc.errors(include_input=False, include_context=False,
                                        include_url=False)
            ]
            raise ValidationError.from_exception_data(
                self._scrub(exc.title), errors, hide_input=True,
            ) from None
        except ValueError as exc:
            # Keep pre-parser ValueErrors sanitized without promoting them to
            # schema validation or exposing their original exception chain.
            raise ValueError(self._scrub(str(exc))) from None


class _CampaignFilterMetadata(_PrivateValidationMetadata):
    """Leave decimal filter strings to the domain's identifier validation."""

    def pre_parse_json(self, data):
        value = data.get("campaign_id")
        if isinstance(value, str) and value.strip().isdecimal():
            # MCP's optional-string pre-parser attempts json.loads before
            # validation. An oversized decimal raises outside the tool guard,
            # even though scalar numbers normally retain their original string.
            remaining = {key: val for key, val in data.items()
                         if key != "campaign_id"}
            return {**super().pre_parse_json(remaining), "campaign_id": value}
        return super().pre_parse_json(data)


class _RetainedReadMetadata(_CampaignFilterMetadata):
    """Wait for snapshot ownership without occupying a general worker."""

    # Shared by every paginated tool on one server, with a lock per event loop.
    # SnapshotStore's synchronous lock still protects direct/internal callers.
    _queues: RunVar = PrivateAttr()

    async def call_fn(self, fn, fn_is_async, arguments, arguments_to_pass_directly=None):
        kwargs = {**arguments, **(arguments_to_pass_directly or {})}
        queue = self._queues.get(None)
        if queue is None:
            queue = anyio.Lock()
            self._queues.set(queue)
        # Acquisition is cancellable: a cancelled waiter never starts a worker.
        async with queue:
            if fn_is_async:
                return await fn(**kwargs)
            # Once started, wait for the worker to finish before releasing
            # admission, including on cancellation. Construction stays serial.
            return await anyio.to_thread.run_sync(functools.partial(fn, **kwargs))

    async def call_fn_with_arg_validation(
        self, fn, fn_is_async, arguments_to_validate,
        arguments_to_pass_directly, pre_validated=None,
    ):
        # MCP 2.0 uses this entry; newer MCP dispatches through call_fn.
        arguments = (pre_validated if pre_validated is not None
                     else self.validate_arguments(arguments_to_validate))
        return await self.call_fn(fn, fn_is_async, arguments, arguments_to_pass_directly)


class _ConfirmationMetadata(_PrivateValidationMetadata):
    """Queue applies before worker dispatch, leaving inspection capacity free."""

    # A server can also be used by internal callers across separate event loops.
    # Each loop owns its asynchronous queue; the synchronous application lock
    # remains authoritative across all callers and covers the entire outcome.
    _queues: RunVar = PrivateAttr(default_factory=lambda: RunVar("confirmations"))

    async def call_fn(self, fn, fn_is_async, arguments, arguments_to_pass_directly=None):
        kwargs = {**arguments, **(arguments_to_pass_directly or {})}

        async def dispatch():
            if fn_is_async:
                return await fn(**kwargs)
            # Keep the guard, execution and thread-local audit context together.
            # Default shielding waits for a started write even on cancellation.
            return await anyio.to_thread.run_sync(functools.partial(fn, **kwargs))

        if kwargs.get("dry_run", True):
            return await dispatch()
        queue = self._queues.get(None)
        if queue is None:
            queue = anyio.Lock()
            self._queues.set(queue)
        async with queue:
            return await dispatch()

    async def call_fn_with_arg_validation(
        self, fn, fn_is_async, arguments_to_validate,
        arguments_to_pass_directly, pre_validated=None,
    ):
        # MCP 2.0 dispatches here; 2.2 validates first and calls call_fn directly.
        arguments = (pre_validated if pre_validated is not None
                     else self.validate_arguments(arguments_to_validate))
        return await self.call_fn(fn, fn_is_async, arguments, arguments_to_pass_directly)


def register_tools(server, ctx):
    """Register the read surface always; mutations only in write mode."""
    from ads_mcp.tools import reads

    reads.register(server, ctx)

    # All public retained reads expose page_token. Share admission across tools
    # and accounts because they share one snapshot store. Health and other
    # non-retained reads keep their independent worker dispatch.
    read_queues = RunVar("retained_reads")
    for tool in server._tool_manager.list_tools():
        fields = tool.fn_metadata.arg_model.model_fields
        field = fields.get("campaign_id")
        if "page_token" in fields:
            tool.fn_metadata = _RetainedReadMetadata(**dict(tool.fn_metadata))
            tool.fn_metadata._queues = read_queues
        elif field is not None and field.annotation == str | None:
            tool.fn_metadata = _CampaignFilterMetadata(**dict(tool.fn_metadata))

    if not ctx.config.read_only:
        from ads_mcp.tools import mutations

        mutations.register(server, ctx)
        tool = server._tool_manager.get_tool("confirm_and_apply")
        tool.fn_metadata = _ConfirmationMetadata(**dict(tool.fn_metadata))

    # MCP's generated models otherwise discard undeclared inputs. Enforce the
    # signature before dispatch (including confirmation), and advertise the
    # same closed shape. Inherit aliases, defaults and the SDK's dump behavior.
    for tool in server._tool_manager.list_tools():
        if not isinstance(tool.fn_metadata, _PrivateValidationMetadata):
            tool.fn_metadata = _PrivateValidationMetadata(**dict(tool.fn_metadata))
        tool.fn_metadata._scrub = ctx.scrub
        model = tool.fn_metadata.arg_model
        tool.fn_metadata.arg_model = create_model(
            model.__name__,
            __base__=model,
            __config__=ConfigDict(extra="forbid", hide_input_in_errors=True),
        )
        tool.parameters = tool.fn_metadata.arg_model.model_json_schema(by_alias=True)


def _prime_specs():
    """Populate reads.SPECS / mutations.SPECS without a real server: run both
    registration passes against a sink so the registry is inspectable (docs
    generation, catalog tests) independent of any live configuration."""
    from types import SimpleNamespace

    from ads_mcp.tools import mutations, reads

    if reads.SPECS and mutations.SPECS:
        return

    class _Sink:
        def tool(self, **_kw):
            def deco(fn):
                return fn

            return deco

    cfg = SimpleNamespace(
        read_only=False, require_dry_run=True, max_daily_budget=None,
        max_bid_increase_pct=None, audit_log=None, customer_id="0000000000",
        login_customer_id=None, retry_base_seconds=1.0,
        plan_ttl_seconds=900.0, row_limit=1000, developer_token="",
        credentials_path="", token_path="",
    )
    ctx = SimpleNamespace(
        config=cfg, plan_store=None, audit=None, clock=lambda: 0.0,
        scrub=lambda s: s, current_tool="",
    )
    reads.register(_Sink(), ctx)
    mutations.register(_Sink(), ctx)


def all_tool_specs() -> list[ToolSpec]:
    from ads_mcp.tools import mutations, reads

    _prime_specs()
    return list(reads.SPECS) + list(mutations.SPECS)
