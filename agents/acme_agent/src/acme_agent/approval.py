"""Deterministic routing and A2A request shaping for approval replies."""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Callable, Iterable
from typing import Any

from a2a.types import GetTaskRequest
from google.adk.a2a import _compat
from google.adk.a2a.agent import A2aRemoteAgentConfig, RequestInterceptor
from google.adk.a2a.converters.request_converter import (
    convert_a2a_request_to_agent_run_request,
)
from google.adk.a2a.executor.a2a_agent_executor import A2aAgentExecutor
from google.adk.a2a.executor.config import A2aAgentExecutorConfig
from google.adk.agents.callback_context import CallbackContext
from google.adk.agents.invocation_context import InvocationContext
from google.adk.agents.remote_a2a_agent import A2A_METADATA_PREFIX, RemoteA2aAgent
from google.adk.events.event import Event
from google.adk.events.event_actions import EventActions
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import Runner
from google.genai import types as genai_types


INBOUND_TASK_ID_METADATA_KEY = "acme_inbound_task_id"
PENDING_APPROVALS_STATE_KEY = "acme_pending_approvals"
ACTIVE_TASKS_STATE_KEY = "acme_active_downstream_tasks"
_TERMINAL_STATES = {"completed", "failed", "canceled", "rejected"}

# ACME does not authenticate inbound callers. With the shared workload token,
# approval authority is possession of the ACME task/context IDs; MAS can enforce
# task ownership only when ACME forwards per-user tokens.


def _normalize_state(state: object) -> str | None:
    if state is None:
        return None
    value = getattr(state, "value", state)
    if not isinstance(value, str):
        return None
    return value.lower().replace("_", "-").removeprefix("task-state-")


def _task_bindings(state: Any, key: str) -> dict[str, dict[str, Any]]:
    """Copy per-ACME-task bindings without modifying session state in place."""

    value = state.get(key, {})
    if not isinstance(value, dict):
        return {}
    return {
        task_id: dict(approval)
        for task_id, approval in value.items()
        if isinstance(task_id, str) and isinstance(approval, dict)
    }


def _pending_approvals(state: Any) -> dict[str, dict[str, Any]]:
    return _task_bindings(state, PENDING_APPROVALS_STATE_KEY)


def _status_message(response: object) -> dict[str, Any]:
    payload = _compat.a2a_to_dict(response)
    return (payload.get("status") or {}).get("message") or {}


def _project_approval_options(event: Event, response: object) -> None:
    """Keep MAS's structured DataPart inside ADK's long-running input call."""

    message = _status_message(response)
    approval = next(
        (
            part["data"]["approvalRequest"]
            for part in message.get("parts", [])
            if isinstance(part.get("data"), dict)
            and isinstance(part["data"].get("approvalRequest"), dict)
        ),
        None,
    )
    if approval is None or not event.content:
        return
    prompt = "\n".join(
        part["text"] for part in message.get("parts", []) if part.get("text")
    )
    for part in event.content.parts or []:
        if part.function_call and "input_required" in (part.function_call.args or {}):
            part.function_call.args["input_required"] = {
                "prompt": prompt,
                "approvalRequest": approval,
            }


def _inbound_task_id(context: Any) -> str | None:
    """Read the trusted A2A task ID added by our executor request converter."""

    metadata = getattr(context, "_custom_metadata", None)
    task_id = (
        metadata.get(INBOUND_TASK_ID_METADATA_KEY)
        if isinstance(metadata, dict)
        else None
    )
    return task_id if isinstance(task_id, str) and task_id else None


def _response_state(response: object) -> tuple[str | None, str | None, str | None]:
    status = getattr(response, "status", None)
    state = _normalize_state(getattr(status, "state", None))
    if state is None:
        # The A2A SDK's normalized streaming task is a protobuf message; enum
        # values are integers there, unlike the Pydantic response objects.
        descriptor = getattr(response, "DESCRIPTOR", None)
        status_field = getattr(descriptor, "fields_by_name", {}).get("status")
        state_field = getattr(
            getattr(status_field, "message_type", None), "fields_by_name", {}
        ).get("state")
        enum_value = getattr(state_field, "enum_type", None)
        enum_value = getattr(enum_value, "values_by_number", {}).get(
            getattr(status, "state", None)
        )
        state = _normalize_state(getattr(enum_value, "name", None))
    task_id = getattr(response, "id", None) or getattr(response, "task_id", None)
    context_id = getattr(response, "context_id", None)
    return (
        state,
        task_id if isinstance(task_id, str) else None,
        context_id if isinstance(context_id, str) else None,
    )


def _record_lifecycle(
    event: Event | None,
    response: object,
    agent_name: str,
    invocation_context: InvocationContext,
) -> Event:
    """Attach this MAS response's lifecycle delta to its ADK session event."""

    state, mas_task_id, mas_context_id = _response_state(response)
    if state is None:
        if event is None:
            raise ValueError("A response without task state cannot make an event")
        return event

    if event is None:
        event = Event(
            author=agent_name,
            invocation_id=invocation_context.invocation_id,
            branch=invocation_context.branch,
        )

    if state in ("failed", "canceled", "rejected"):
        event.error_code = f"DOWNSTREAM_TASK_{state.upper()}"
        description = {
            "failed": "Downstream task failed",
            "canceled": "Downstream task was canceled",
            "rejected": "Downstream task was rejected",
        }[state]
        explanation = "\n".join(
            part["text"]
            for part in _status_message(response).get("parts", [])
            if part.get("text")
        )
        event.error_message = f"{description}: {explanation}" if explanation else description

    if state in {"input-required", "auth-required"}:
        if not event.long_running_tool_ids:
            # Contentless pauses still need an explicit outer pause, not EOF
            # followed by the stock executor's implicit completion.
            auth = state == "auth-required"
            call_id = str(uuid.uuid4())
            key = "auth_required" if auth else "input_required"
            call = genai_types.Part(function_call=genai_types.FunctionCall(
                id=call_id,
                name=("mock_function_call_for_required_user_auth" if auth
                      else "mock_function_call_for_required_user_input"),
                args={key: f"Downstream task requires {'authentication' if auth else 'input'}"},
            ))
            if event.content is None:
                event.content = genai_types.Content(role="model", parts=[])
            event.content.parts = list(event.content.parts or []) + [call]
            event.long_running_tool_ids = {call_id}
        if state == "input-required":
            _project_approval_options(event, response)

    event.actions = event.actions or EventActions()
    pending = _pending_approvals(invocation_context.session.state)
    active = _task_bindings(invocation_context.session.state, ACTIVE_TASKS_STATE_KEY)
    acme_task_id = _inbound_task_id(invocation_context)
    if acme_task_id and mas_task_id and state not in _TERMINAL_STATES:
        active[acme_task_id] = {
            "agent": agent_name,
            "mas_task_id": mas_task_id,
            "mas_context_id": mas_context_id,
            "acme_task_id": acme_task_id,
        }
    if mas_task_id and state in _TERMINAL_STATES:
        active = {
            owner: binding for owner, binding in active.items()
            if binding.get("agent") != agent_name
            or binding.get("mas_task_id") != mas_task_id
        }
    if state == "input-required" and acme_task_id and mas_task_id:
        existing_owner = next(
            (
                owner
                for owner, approval in pending.items()
                if approval.get("agent") == agent_name
                and approval.get("mas_task_id") == mas_task_id
            ),
            None,
        )
        if existing_owner is None or existing_owner == acme_task_id:
            pending[acme_task_id] = {
                "agent": agent_name,
                "mas_task_id": mas_task_id,
                "mas_context_id": mas_context_id,
                "acme_task_id": acme_task_id,
            }
    elif mas_task_id:
        # Working and terminal states are not resumable approvals. Only clear
        # the matching MAS task; another ACME task's pause is retained.
        for pending_acme_task_id, approval in list(pending.items()):
            if (
                approval.get("agent") == agent_name
                and approval.get("mas_task_id") == mas_task_id
            ):
                del pending[pending_acme_task_id]

    event.actions.state_delta[PENDING_APPROVALS_STATE_KEY] = pending
    event.actions.state_delta[ACTIVE_TASKS_STATE_KEY] = active
    event.custom_metadata = event.custom_metadata or {}
    event.custom_metadata["acme_downstream_state"] = state
    if mas_task_id:
        event.custom_metadata[A2A_METADATA_PREFIX + "task_id"] = mas_task_id
    if mas_context_id:
        event.custom_metadata[A2A_METADATA_PREFIX + "context_id"] = mas_context_id
    return event


def _lifecycle_converter(
    agent_name: str,
    converter: Callable[..., Event | None],
) -> Callable[..., Event | None]:
    """Keep lifecycle changes even when ADK drops a contentless status event."""

    def convert(response, author, invocation_context, part_converter):
        event = converter(response, author, invocation_context, part_converter)
        state, _, _ = _response_state(response)
        if state is None or invocation_context is None:
            return event
        return _record_lifecycle(event, response, agent_name, invocation_context)

    return convert


def acme_request_converter(request, part_converter):
    """Pass the executor-trusted inbound A2A task ID into this ADK run."""

    run_request = convert_a2a_request_to_agent_run_request(request, part_converter)
    run_config = run_request.run_config
    if run_config is not None:
        custom_metadata = dict(run_config.custom_metadata or {})
        # RequestContext.task_id is supplied by the A2A server (not caller
        # metadata); the stock ADK converter otherwise discards it.
        custom_metadata[INBOUND_TASK_ID_METADATA_KEY] = request.task_id
        run_config.custom_metadata = custom_metadata
    return run_request


def approval_aware_executor_factory(runner: Runner) -> A2aAgentExecutor:
    """Use ADK's normal executor with the trusted task-ID request converter."""

    return A2aAgentExecutor(
        runner=runner,
        config=A2aAgentExecutorConfig(request_converter=acme_request_converter),
    )


class ApprovalAwareRemoteA2aAgent(RemoteA2aAgent):
    """Track active tasks separately from human pauses and fail closed on EOF."""

    async def _handle_a2a_response(self, a2a_response, ctx):
        """Track task updates on ADK's legacy A2A response-conversion path."""

        event = await super()._handle_a2a_response(a2a_response, ctx)
        if isinstance(a2a_response, tuple):
            task, update = a2a_response
            lifecycle_response = update if update is not None else task
        else:
            lifecycle_response = a2a_response

        state, _, _ = _response_state(lifecycle_response)
        if state is None:
            return event
        event = _record_lifecycle(event, lifecycle_response, self.name, ctx)
        if isinstance(a2a_response, tuple):
            task, _ = a2a_response
            event.custom_metadata[A2A_METADATA_PREFIX + "task_id"] = task.id
            if task.context_id:
                event.custom_metadata[A2A_METADATA_PREFIX + "context_id"] = (
                    task.context_id
                )
        return event

    async def _run_async_impl(self, ctx: InvocationContext):
        saw_outcome = False
        async for event in super()._run_async_impl(ctx):
            if event.error_message:
                # The stock remote emits several errors without error_code;
                # ADK's outer executor otherwise interprets them as success.
                if not event.error_code:
                    recovered = await self._recover_bound_task(ctx)
                    if recovered is not None:
                        saw_outcome = True
                        yield recovered
                        return
                    event.error_code = "DOWNSTREAM_ATTACHMENT_FAILED"
                pending = _pending_approvals(ctx.session.state)
                current_acme_task_id = _inbound_task_id(ctx)
                request = (event.custom_metadata or {}).get(
                    A2A_METADATA_PREFIX + "request"
                )
                request = request if isinstance(request, dict) else {}
                mas_task_id = request.get("taskId") or request.get("task_id")
                has_binding = current_acme_task_id is not None or isinstance(
                    mas_task_id, str
                )

                for acme_task_id, approval in list(pending.items()):
                    same_acme_task = (
                        current_acme_task_id is None
                        or acme_task_id == current_acme_task_id
                    )
                    same_mas_task = (
                        not isinstance(mas_task_id, str)
                        or approval.get("mas_task_id") == mas_task_id
                    )
                    if (
                        has_binding
                        and approval.get("agent") == self.name
                        and same_acme_task
                        and same_mas_task
                    ):
                        del pending[acme_task_id]

                event.actions = event.actions or EventActions()
                event.actions.state_delta[PENDING_APPROVALS_STATE_KEY] = pending
                saw_outcome = True
            elif event.custom_metadata and event.custom_metadata.get("acme_downstream_state"):
                saw_outcome = event.custom_metadata["acme_downstream_state"] in (
                    _TERMINAL_STATES | {"input-required", "auth-required"}
                )
            elif (event.custom_metadata or {}).get(A2A_METADATA_PREFIX + "response"):
                # A stateless Message is a valid outcome without a task state.
                response = event.custom_metadata[A2A_METADATA_PREFIX + "response"]
                if "status" not in response:
                    saw_outcome = True
            yield event
        if not saw_outcome:
            # Also cover empty streams, for which ADK has no last_task to check.
            pending = _pending_approvals(ctx.session.state)
            current_task_id = _inbound_task_id(ctx)
            if pending.get(current_task_id or "", {}).get("agent") == self.name:
                pending.pop(current_task_id, None)
            yield Event(
                author=self.name,
                invocation_id=ctx.invocation_id,
                branch=ctx.branch,
                error_code="DOWNSTREAM_ATTACHMENT_INTERRUPTED",
                error_message=(
                    "Downstream stream ended without a terminal result or an "
                    "explicit input/authentication pause. The remote task may "
                    "still be running; this is not successful completion."
                ),
                actions=EventActions(state_delta={PENDING_APPROVALS_STATE_KEY: pending}),
            )

    async def _recover_bound_task(self, ctx: InvocationContext) -> Event | None:
        """Re-expose a confirmed open gate after an RPC/transport error.

        A read never re-submits a decision. Unknown/unreachable or still-working
        tasks fail this attachment explicitly, retaining their active binding
        for diagnosis rather than treating loss of transport as terminality.
        """

        acme_task_id = _inbound_task_id(ctx)
        binding = _task_bindings(ctx.session.state, ACTIVE_TASKS_STATE_KEY).get(
            acme_task_id or ""
        ) or _pending_approvals(ctx.session.state).get(acme_task_id or "")
        if not binding or binding.get("agent") != self.name:
            return None
        try:
            async with asyncio.timeout(10):
                client = await self._ensure_resolved(ctx)
                task = await client.get_task(GetTaskRequest(id=binding["mas_task_id"]))
            state, task_id, context_id = _response_state(task)
            if task_id != binding["mas_task_id"] or (
                binding.get("mas_context_id")
                and context_id != binding["mas_context_id"]
            ):
                return None
            if state not in (_TERMINAL_STATES | {"input-required"}):
                return None
            return await self._handle_a2a_response((task, None), ctx)
        except Exception:
            # Recovery is best-effort; keep the original explicit error.
            return None


def approval_resume_before_model(remote_names: Iterable[str]):
    """Short-circuit the root LLM only for this ACME task's paused approval."""

    configured_names = set(remote_names)

    def before_model_callback(
        callback_context: CallbackContext,
        llm_request: LlmRequest,
    ) -> LlmResponse | None:
        del llm_request
        current_task_id = callback_context.custom_metadata.get(
            INBOUND_TASK_ID_METADATA_KEY
        )
        if not isinstance(current_task_id, str):
            return None

        pending = _pending_approvals(callback_context.state).get(current_task_id)
        if not pending or pending.get("agent") not in configured_names:
            return None

        agent_name = pending["agent"]
        return LlmResponse(
            content=genai_types.Content(
                role="model",
                parts=[
                    genai_types.Part(
                        function_call=genai_types.FunctionCall(
                            id=str(uuid.uuid4()),
                            name="transfer_to_agent",
                            args={"agent_name": agent_name},
                        )
                    )
                ],
            )
        )

    return before_model_callback


def remote_context_builder(
    ctx: Any,
    agent_name: str,
    genai_part_converter: Any,
) -> tuple[list[Any], str | None]:
    """Forward this user input with the latest MAS conversational context.

    ADK's default history builder includes the router's transfer call and the
    synthetic long-running function call. The MAS decision endpoint expects a
    plain text decision, so this builder extracts the current user input only.
    Ordinary turns reuse the latest context for this remote agent; only a
    matching pending approval supplies a MAS task ID through the interceptor.
    A function-response DataPart is rendered to its ``response.result`` text.
    """

    user_event = next(
        (
            event
            for event in reversed(ctx.session.events)
            if event.author == "user" and event.content and event.content.parts
        ),
        None,
    )
    if user_event is None:
        return [], None

    genai_parts: list[genai_types.Part] = []
    for part in user_event.content.parts:
        if part.function_response:
            response = part.function_response.response or {}
            value = response.get("result") if isinstance(response, dict) else None
            if value is not None:
                text = value if isinstance(value, str) else json.dumps(value)
                genai_parts.append(genai_types.Part(text=text))
        elif part.text:
            genai_parts.append(genai_types.Part(text=part.text))
        else:
            genai_parts.append(part)

    a2a_parts: list[Any] = []
    for part in genai_parts:
        converted = genai_part_converter(part)
        if isinstance(converted, list):
            a2a_parts.extend(converted)
        elif converted is not None:
            a2a_parts.append(converted)

    inbound_task_id = _inbound_task_id(ctx)
    pending = _pending_approvals(ctx.session.state).get(inbound_task_id or "")
    context_id = (
        pending.get("mas_context_id")
        if pending and pending.get("agent") == agent_name
        else None
    )
    if not isinstance(context_id, str) or not context_id:
        context_id = next(
            (
                event.custom_metadata.get(A2A_METADATA_PREFIX + "context_id")
                for event in reversed(ctx.session.events)
                if event.author == agent_name
                and isinstance(event.custom_metadata, dict)
                and isinstance(
                    event.custom_metadata.get(A2A_METADATA_PREFIX + "context_id"),
                    str,
                )
                and event.custom_metadata[A2A_METADATA_PREFIX + "context_id"]
            ),
            None,
        )
    return a2a_parts, context_id


def remote_request_config(agent_name: str) -> A2aRemoteAgentConfig:
    """Stamp MAS IDs only when the inbound ACME task owns this pause."""

    async def stamp_paused_task(ctx, a2a_request, parameters):
        inbound_task_id = _inbound_task_id(ctx)
        pending = _pending_approvals(ctx.session.state).get(inbound_task_id or "")
        if pending and pending.get("agent") == agent_name:
            a2a_request.task_id = pending.get("mas_task_id")
            if pending.get("mas_context_id"):
                a2a_request.context_id = pending["mas_context_id"]
        return a2a_request, parameters

    from google.adk.a2a.converters.to_adk_event import (
        convert_a2a_status_update_to_event,
        convert_a2a_task_to_event,
    )

    return A2aRemoteAgentConfig(
        request_interceptors=[
            RequestInterceptor(before_request=stamp_paused_task)
        ],
        a2a_task_converter=_lifecycle_converter(
            agent_name, convert_a2a_task_to_event
        ),
        a2a_status_update_converter=_lifecycle_converter(
            agent_name, convert_a2a_status_update_to_event
        ),
    )
