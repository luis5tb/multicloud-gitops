"""Regression tests for deterministic MAS approval continuation."""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
from contextlib import asynccontextmanager
from typing import AsyncGenerator
from typing import AsyncIterator
from pathlib import Path

import httpx
import pytest
from a2a.types import AgentCapabilities, AgentCard, AgentInterface, StreamResponse, TaskState
from google.adk.a2a import _compat
from google.adk.a2a.utils.agent_to_a2a import to_a2a
from google.adk.agents.llm_agent import Agent
from google.adk.models.base_llm import BaseLlm
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import Runner
from google.adk.sessions.in_memory_session_service import InMemorySessionService
from google.genai import types as genai_types

from acme_agent.approval import (
    ApprovalAwareRemoteA2aAgent,
    approval_aware_executor_factory,
    approval_resume_before_model,
    remote_context_builder,
    remote_request_config,
)


MAS_TASK_ID = "mas-task-1"
MAS_CONTEXT_ID = "mas-context-1"
MAS_CONTEXT_ID_2 = "mas-context-2"
MAS_CONTEXT_ID_3 = "mas-context-3"
ACME_CONTEXT_ID = "acme-context-1"
APPROVAL_PROMPT = "Analysis complete. Approval required to run Execution on cluster X."


class FakeMAS:
    """A mocked downstream A2A HTTP endpoint that pauses then resumes."""

    def __init__(
        self,
        *,
        decision_error_status: int | None = None,
        pause_again: bool = False,
        terminal_state: str | None = None,
        context_ids: list[str] | None = None,
        first_state: str = "input-required",
        resume_eof: bool = False,
        decision_rpc_error: bool = False,
        query_state: str | None = None,
        approval_request: dict | None = None,
        terminal_explanation: bool = False,
        contentless_pause: bool = False,
    ) -> None:
        self.requests: list[dict] = []
        self.queries: list[dict] = []
        self.decision_error_status = decision_error_status
        self.pause_again = pause_again
        self.terminal_state = terminal_state
        self.context_ids = context_ids or [MAS_CONTEXT_ID]
        self.first_state = first_state
        self.resume_eof = resume_eof
        self.decision_rpc_error = decision_rpc_error
        self.query_state = query_state
        self.approval_request = approval_request
        self.terminal_explanation = terminal_explanation
        self.contentless_pause = contentless_pause

    def paused_task(self, state="input-required"):
        parts = [_compat.make_text_part(APPROVAL_PROMPT)]
        if self.approval_request:
            parts.append(_compat.make_data_part(data={"approvalRequest": self.approval_request}))
        return _compat.make_task(
            id=MAS_TASK_ID,
            context_id=self.context_ids[0],
            status=_compat.make_task_status(
                {
                    "input-required": _compat.TS_INPUT_REQUIRED,
                    "auth-required": _compat.TS_AUTH_REQUIRED,
                    "working": _compat.TS_WORKING,
                    "submitted": _compat.TS_SUBMITTED,
                    "completed": _compat.TS_COMPLETED,
                }[state],
                message=None if self.contentless_pause else _compat.make_message(
                    message_id="mas-approval-prompt", role="agent", parts=parts
                ),
            ),
        )

    def handler(self, request: httpx.Request) -> httpx.Response:
        rpc_request = json.loads(request.content)
        if rpc_request["method"] in {"GetTask", "tasks/get"}:
            self.queries.append(rpc_request)
            if self.query_state is None:
                return httpx.Response(404, json={"detail": "MAS task not found"})
            return httpx.Response(200, json={
                "jsonrpc": "2.0", "id": rpc_request["id"],
                "result": _compat.a2a_to_dict(self.paused_task(self.query_state)),
            })
        self.requests.append(rpc_request)
        context_id = self.context_ids[
            min(len(self.requests) - 1, len(self.context_ids) - 1)
        ]
        if len(self.requests) == 2 and self.decision_error_status:
            return httpx.Response(
                self.decision_error_status,
                json={"detail": "MAS task not found"},
            )
        if len(self.requests) == 2 and self.decision_rpc_error:
            return httpx.Response(200, json={
                "jsonrpc": "2.0", "id": rpc_request["id"],
                "error": {"code": -32602, "message": "Invalid decision"},
            })
        if len(self.requests) == 1:
            stream_items = [] if self.first_state == "empty" else [
                StreamResponse(task=self.paused_task(self.first_state))
            ]
        elif len(self.requests) == 2 and self.pause_again:
            prompt = _compat.make_message(
                message_id="mas-retry-prompt",
                role="agent",
                parts=[_compat.make_text_part("MAS still requires a decision")],
            )
            stream_items = [
                StreamResponse(
                    task=_compat.make_task(
                        id=MAS_TASK_ID,
                        context_id=context_id,
                        status=_compat.make_task_status(
                            _compat.TS_INPUT_REQUIRED, message=prompt
                        ),
                    )
                )
            ]
        elif len(self.requests) == 2 and self.terminal_state:
            terminal_states = {
                "completed": _compat.TS_COMPLETED,
                "failed": _compat.TS_FAILED,
                "canceled": _compat.TS_CANCELED,
                "rejected": TaskState.TASK_STATE_REJECTED,
            }
            update = _compat.make_task_status_update_event(
                task_id=MAS_TASK_ID,
                context_id=context_id,
                status=_compat.make_task_status(
                    terminal_states[self.terminal_state],
                    message=_compat.make_message(
                        message_id="rejection", role="agent",
                        parts=[_compat.make_text_part("Policy denied the request")],
                    ) if self.terminal_explanation else None,
                ),
            )
            stream_items = [StreamResponse(status_update=update)]
        else:
            stream_items = []
            for message_id, text in (
                ("execution", "Execution stage started"),
                ("verification", "Automatic verification started"),
            ):
                status_message = _compat.make_message(
                    message_id=message_id,
                    role="agent",
                    parts=[_compat.make_text_part(text)],
                )
                update = _compat.make_task_status_update_event(
                    task_id=MAS_TASK_ID,
                    context_id=context_id,
                    status=_compat.make_task_status(
                        _compat.TS_WORKING, message=status_message
                    ),
                )
                stream_items.append(StreamResponse(status_update=update))

            if self.resume_eof:
                stream_items = stream_items[:1]
            answer = _compat.make_artifact(
                artifact_id="verification-result",
                parts=[_compat.make_text_part("Verification passed")],
            )
            if not self.resume_eof:
                stream_items.append(
                StreamResponse(
                    task=_compat.make_task(
                        id=MAS_TASK_ID,
                        context_id=context_id,
                        status=_compat.make_task_status(_compat.TS_COMPLETED),
                        artifacts=[answer],
                    )
                )
            )

        events = [
            {
                "jsonrpc": "2.0",
                "id": rpc_request["id"],
                "result": _compat.a2a_to_dict(item),
            }
            for item in stream_items
        ]
        content = b"".join(
            b"data: " + json.dumps(event).encode() + b"\n\n" for event in events
        )
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=content,
        )


class FakeRouterModel(BaseLlm):
    """Transfer the first turn; answer normally on a later unrelated turn."""

    model: str = "fake-router"
    requests: list[LlmRequest] = []
    transfer_every_turn: bool = False

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        self.requests.append(llm_request.model_copy(deep=True))
        if self.transfer_every_turn or len(self.requests) == 1:
            content = genai_types.Content(
                role="model",
                parts=[
                    genai_types.Part(
                        function_call=genai_types.FunctionCall(
                            id="initial-transfer-call",
                            name="transfer_to_agent",
                            args={"agent_name": "mas_orchestrator"},
                        )
                    )
                ],
            )
        else:
            content = genai_types.Content(
                role="model", parts=[genai_types.Part(text="Normal root reply")]
            )
        yield LlmResponse(content=content)


def _unmatched_function_call_ids(contents: list[genai_types.Content]) -> set[str]:
    calls = {
        part.function_call.id
        for content in contents
        for part in content.parts or []
        if part.function_call and part.function_call.id
    }
    responses = {
        part.function_response.id
        for content in contents
        for part in content.parts or []
        if part.function_response and part.function_response.id
    }
    return calls - responses


def _find_task(results: list[dict]) -> dict:
    latest = None
    for result in results:
        task = result.get("task")
        if task:
            latest = task
        elif result.get("kind") == "task" or (
            result.get("id") and result.get("status")
        ):
            latest = result
        elif result.get("taskId") and result.get("status"):
            latest = {
                "id": result["taskId"],
                "contextId": result.get("contextId"),
                "status": result["status"],
            }
    if latest is None:
        raise AssertionError(f"No task snapshot in A2A results: {results!r}")
    return latest


@asynccontextmanager
async def _agent_harness(
    mas: FakeMAS, *, transfer_every_turn: bool = False
) -> AsyncIterator[dict]:
    remote_card = AgentCard(
        name="MAS",
        description="Fake MAS A2A server",
        supported_interfaces=[
            AgentInterface(
                url="https://mas.example.test/a2a",
                protocol_binding="JSONRPC",
                protocol_version="1.0",
            )
        ],
        version="1.0",
        capabilities=AgentCapabilities(streaming=True),
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
    )
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(mas.handler)
    ) as downstream_client:
        remote = ApprovalAwareRemoteA2aAgent(
            name="mas_orchestrator",
            agent_card=remote_card,
            httpx_client=downstream_client,
            context_builder=remote_context_builder,
            config=remote_request_config("mas_orchestrator"),
            use_legacy=False,
        )
        router_model = FakeRouterModel(transfer_every_turn=transfer_every_turn)
        approval_transfers: list[str] = []
        resume_callback = approval_resume_before_model(["mas_orchestrator"])

        def count_approval_transfer(callback_context, llm_request):
            response = resume_callback(callback_context, llm_request)
            if response and response.content:
                for part in response.content.parts or []:
                    if part.function_call and part.function_call.name == "transfer_to_agent":
                        approval_transfers.append(
                            part.function_call.args.get("agent_name", "")
                        )
            return response

        root_agent = Agent(
            name="acme_agent",
            model=router_model,
            instruction="Delegate the request to the MAS sub-agent.",
            sub_agents=[remote],
            before_model_callback=count_approval_transfer,
        )
        session_service = InMemorySessionService()
        runner = Runner(
            app_name="acme",
            agent=root_agent,
            session_service=session_service,
        )
        acme_card = AgentCard(
            name="ACME",
            description="Test ACME router",
            supported_interfaces=[
                AgentInterface(
                    url="http://acme.example.test/",
                    protocol_binding="JSONRPC",
                    protocol_version="1.0",
                )
            ],
            version="1.0",
            capabilities=AgentCapabilities(streaming=True),
            default_input_modes=["text/plain"],
            default_output_modes=["text/plain"],
        )
        app = to_a2a(
            root_agent,
            runner=runner,
            agent_card=acme_card,
            host="acme.example.test",
            agent_executor_factory=approval_aware_executor_factory,
        )
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://acme.example.test",
            ) as acme_client:

                async def send(message: dict) -> list[dict]:
                    response = await acme_client.post(
                        "/",
                        json={
                            "jsonrpc": "2.0",
                            "id": message["messageId"],
                            "method": "message/stream",
                            "params": {"message": message},
                        },
                        headers={"accept": "text/event-stream"},
                    )
                    assert response.status_code == 200, response.text
                    return [
                        json.loads(line[6:])["result"]
                        for line in response.text.splitlines()
                        if line.startswith("data: ")
                    ]

                yield {
                    "send": send,
                    "router_model": router_model,
                    "approval_transfers": approval_transfers,
                    "session_service": session_service,
                }


@pytest.mark.parametrize("decision", ["approve option 2", "reject"])
def test_approval_reply_routes_without_llm_and_resumes_mas_task(
    decision: str,
) -> None:
    """The real to_a2a executor routes a plain decision to the paused MAS task."""

    async def run():
        mas = FakeMAS()
        remote_card = AgentCard(
            name="MAS",
            description="Fake MAS A2A server",
            supported_interfaces=[
                AgentInterface(
                    url="https://mas.example.test/a2a",
                    protocol_binding="JSONRPC",
                    protocol_version="1.0",
                )
            ],
            version="1.0",
            capabilities=AgentCapabilities(streaming=True),
            default_input_modes=["text/plain"],
            default_output_modes=["text/plain"],
        )
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(mas.handler)
        ) as downstream_client:
            remote = ApprovalAwareRemoteA2aAgent(
                name="mas_orchestrator",
                agent_card=remote_card,
                httpx_client=downstream_client,
                context_builder=remote_context_builder,
                config=remote_request_config("mas_orchestrator"),
                use_legacy=False,
            )
            router_model = FakeRouterModel()
            approval_transfers: list[str] = []
            resume_callback = approval_resume_before_model(["mas_orchestrator"])

            def count_approval_transfer(callback_context, llm_request):
                response = resume_callback(callback_context, llm_request)
                if response and response.content:
                    for part in response.content.parts or []:
                        if part.function_call and part.function_call.name == "transfer_to_agent":
                            approval_transfers.append(
                                part.function_call.args.get("agent_name", "")
                            )
                return response

            root_agent = Agent(
                name="acme_agent",
                model=router_model,
                instruction="Delegate the request to the MAS sub-agent.",
                sub_agents=[remote],
                before_model_callback=count_approval_transfer,
            )
            runner = Runner(
                app_name="acme",
                agent=root_agent,
                session_service=InMemorySessionService(),
            )
            acme_card = AgentCard(
                name="ACME",
                description="Test ACME router",
                supported_interfaces=[
                    AgentInterface(
                        url="http://acme.example.test/",
                        protocol_binding="JSONRPC",
                        protocol_version="1.0",
                    )
                ],
                version="1.0",
                capabilities=AgentCapabilities(streaming=True),
                default_input_modes=["text/plain"],
                default_output_modes=["text/plain"],
            )
            app = to_a2a(
                root_agent,
                runner=runner,
                agent_card=acme_card,
                host="acme.example.test",
                agent_executor_factory=approval_aware_executor_factory,
            )
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="http://acme.example.test",
                ) as acme_client:

                    async def send(message: dict) -> list[dict]:
                        response = await acme_client.post(
                            "/",
                            json={
                                "jsonrpc": "2.0",
                                "id": message["messageId"],
                                "method": "message/stream",
                                "params": {"message": message},
                            },
                            headers={"accept": "text/event-stream"},
                        )
                        assert response.status_code == 200, response.text
                        return [
                            json.loads(line[6:])["result"]
                            for line in response.text.splitlines()
                            if line.startswith("data: ")
                        ]

                    first_results = await send(
                        {
                            "messageId": "acme-initial",
                            "role": "user",
                            "contextId": ACME_CONTEXT_ID,
                            "parts": [
                                {"kind": "text", "text": "Investigate cluster X"}
                            ],
                        }
                    )
                    first_task = _find_task(first_results)
                    first_state = first_task["status"]["state"].lower().replace(
                        "_", "-"
                    )
                    assert first_state.removeprefix("task-state-") == "input-required"
                    acme_task_id = first_task["id"]
                    acme_context_id = first_task.get("contextId", ACME_CONTEXT_ID)

                    new_task_results = await send(
                        {
                            "messageId": "acme-new-task-approval-text",
                            "role": "user",
                            "contextId": acme_context_id,
                            "parts": [{"kind": "text", "text": decision}],
                        }
                    )
                    requests_after_new_task = len(router_model.requests)
                    assert requests_after_new_task == 2
                    assert len(mas.requests) == 1
                    assert approval_transfers == []

                    resumed_results = await send(
                        {
                            "messageId": "acme-approval",
                            "role": "user",
                            "taskId": acme_task_id,
                            "contextId": acme_context_id,
                            "parts": [{"kind": "text", "text": decision}],
                        }
                    )
                    requests_after_approval = len(router_model.requests)
                    assert requests_after_approval == requests_after_new_task
                    assert approval_transfers == ["mas_orchestrator"]

                    unrelated_results = await send(
                        {
                            "messageId": "acme-unrelated",
                            "role": "user",
                            "contextId": acme_context_id,
                            "parts": [
                                {"kind": "text", "text": "Unrelated question"}
                            ],
                        }
                    )

        return (
            mas,
            router_model,
            first_results,
            new_task_results,
            resumed_results,
            unrelated_results,
            requests_after_new_task,
            requests_after_approval,
            approval_transfers,
        )

    (
        mas,
        router_model,
        first_results,
        new_task_results,
        resumed_results,
        unrelated_results,
        requests_after_new_task,
        requests_after_approval,
        approval_transfers,
    ) = asyncio.run(run())

    assert len(mas.requests) == 2
    assert requests_after_new_task == 2
    assert requests_after_approval == requests_after_new_task
    assert approval_transfers == ["mas_orchestrator"]
    initial_message = mas.requests[0]["params"]["message"]
    resumed_message = mas.requests[1]["params"]["message"]
    assert initial_message["parts"] == [{"text": "Investigate cluster X"}]
    assert resumed_message["taskId"] == MAS_TASK_ID
    assert resumed_message["contextId"] == MAS_CONTEXT_ID
    assert resumed_message["parts"] == [{"text": decision}]
    new_task_text = json.dumps(new_task_results)
    assert "Normal root reply" in new_task_text
    assert len(router_model.requests) == 3

    resumed_text = json.dumps(resumed_results)
    assert "Execution stage started" in resumed_text
    assert "Automatic verification started" in resumed_text
    assert "Verification passed" in resumed_text
    resumed_task = _find_task(resumed_results)
    resumed_state = resumed_task["status"]["state"].lower().replace("_", "-")
    assert resumed_state.removeprefix("task-state-") == "completed"
    assert APPROVAL_PROMPT in json.dumps(first_results)
    assert "Normal root reply" in json.dumps(unrelated_results)
    assert len(router_model.requests) == 3
    assert not _unmatched_function_call_ids(router_model.requests[-1].contents)


async def _start_paused(send):
    results = await send(
        {
            "messageId": "acme-initial",
            "role": "user",
            "contextId": ACME_CONTEXT_ID,
            "parts": [{"kind": "text", "text": "Investigate cluster X"}],
        }
    )
    task = _find_task(results)
    assert task["status"]["state"].lower().replace("_", "-").removeprefix(
        "task-state-"
    ) == "input-required"
    return results, task["id"], task.get("contextId", ACME_CONTEXT_ID)


async def _pending_state(harness):
    session = await harness["session_service"].get_session(
        app_name="acme",
        user_id=f"A2A_USER_{ACME_CONTEXT_ID}",
        session_id=ACME_CONTEXT_ID,
    )
    assert session is not None
    return session.state.get("acme_pending_approvals", {})


@pytest.mark.parametrize("terminal_state,explanation", [
    ("completed", False), ("failed", False), ("canceled", False),
    ("rejected", False), ("rejected", True),
])
def test_contentless_terminal_status_clears_pending_pause(
    terminal_state: str, explanation: bool,
) -> None:
    """Contentless terminal status updates must not leave the pause resumable."""

    async def run():
        mas = FakeMAS(terminal_state=terminal_state, terminal_explanation=explanation)
        async with _agent_harness(mas) as harness:
            send = harness["send"]
            _, acme_task_id, acme_context_id = await _start_paused(send)
            terminal_results = await send(
                {
                    "messageId": "acme-approval",
                    "role": "user",
                    "taskId": acme_task_id,
                    "contextId": acme_context_id,
                    "parts": [{"kind": "text", "text": "approve option 2"}],
                }
            )
            pending_after_terminal = await _pending_state(harness)
            model_requests_after_terminal = len(harness["router_model"].requests)
            unrelated = await send(
                {
                    "messageId": "acme-unrelated",
                    "role": "user",
                    "contextId": acme_context_id,
                    "parts": [{"kind": "text", "text": "Unrelated question"}],
                }
            )
            return (
                mas,
                harness,
                pending_after_terminal,
                model_requests_after_terminal,
                terminal_results,
                unrelated,
            )

    mas, harness, pending, model_requests, terminal_results, unrelated = (
        asyncio.run(run())
    )
    assert len(mas.requests) == 2
    assert pending == {}
    assert model_requests == 1
    assert len(harness["router_model"].requests) == 2
    assert "Normal root reply" in json.dumps(unrelated)
    terminal_task = _find_task(terminal_results)
    terminal_status = terminal_task["status"]
    actual_state = terminal_status["state"].lower().replace("_", "-")
    actual_state = actual_state.removeprefix("task-state-")
    expected_state = "completed" if terminal_state == "completed" else "failed"
    assert actual_state == expected_state
    if terminal_state in {"failed", "canceled", "rejected"}:
        expected_message = {
            "failed": "Downstream task failed",
            "canceled": "Downstream task was canceled",
            "rejected": "Downstream task was rejected",
        }[terminal_state]
        assert expected_message in json.dumps(terminal_results)
    if explanation:
        assert "Policy denied the request" in json.dumps(terminal_results)


def test_ordinary_delegations_reuse_latest_mas_context_without_task_id() -> None:
    """Completed turns retain MAS context, without resuming a finished task."""

    async def run():
        mas = FakeMAS(context_ids=[MAS_CONTEXT_ID, MAS_CONTEXT_ID_2, MAS_CONTEXT_ID_3])
        async with _agent_harness(mas, transfer_every_turn=True) as harness:
            send = harness["send"]
            _, acme_task_id, acme_context_id = await _start_paused(send)
            await send(
                {
                    "messageId": "acme-approval",
                    "role": "user",
                    "taskId": acme_task_id,
                    "contextId": acme_context_id,
                    "parts": [{"kind": "text", "text": "approve option 2"}],
                }
            )
            await send(
                {
                    "messageId": "acme-follow-up-1",
                    "role": "user",
                    "contextId": acme_context_id,
                    "parts": [{"kind": "text", "text": "First follow-up"}],
                }
            )
            await send(
                {
                    "messageId": "acme-follow-up-2",
                    "role": "user",
                    "contextId": acme_context_id,
                    "parts": [{"kind": "text", "text": "Second follow-up"}],
                }
            )
            return mas

    mas = asyncio.run(run())
    assert len(mas.requests) == 4
    first_follow_up = mas.requests[2]["params"]["message"]
    second_follow_up = mas.requests[3]["params"]["message"]
    assert first_follow_up["contextId"] == MAS_CONTEXT_ID_2
    assert "taskId" not in first_follow_up
    assert second_follow_up["contextId"] == MAS_CONTEXT_ID_3
    assert "taskId" not in second_follow_up


def test_mas_input_required_response_keeps_approval_retryable() -> None:
    """Keep the pause only when MAS explicitly reports input-required again."""

    async def run():
        mas = FakeMAS(pause_again=True)
        async with _agent_harness(mas) as harness:
            send = harness["send"]
            _, acme_task_id, acme_context_id = await _start_paused(send)
            await send(
                {
                    "messageId": "acme-approval-1",
                    "role": "user",
                    "taskId": acme_task_id,
                    "contextId": acme_context_id,
                    "parts": [{"kind": "text", "text": "approve option 2"}],
                }
            )
            pending_after_retry_required = await _pending_state(harness)
            await send(
                {
                    "messageId": "acme-approval-2",
                    "role": "user",
                    "taskId": acme_task_id,
                    "contextId": acme_context_id,
                    "parts": [{"kind": "text", "text": "approve option 2"}],
                }
            )
            return mas, harness, pending_after_retry_required, acme_task_id

    mas, harness, pending, acme_task_id = asyncio.run(run())
    assert len(mas.requests) == 3
    assert harness["approval_transfers"] == ["mas_orchestrator", "mas_orchestrator"]
    assert len(harness["router_model"].requests) == 1
    assert list(pending) == [acme_task_id]
    approval = pending[acme_task_id]
    assert approval == {
        "agent": "mas_orchestrator",
        "mas_task_id": MAS_TASK_ID,
        "mas_context_id": MAS_CONTEXT_ID,
        "acme_task_id": acme_task_id,
    }


def test_unknown_mas_task_error_clears_pending_approval() -> None:
    """A 404/non-resumable MAS decision error clears the matching pause."""

    async def run():
        mas = FakeMAS(decision_error_status=404)
        async with _agent_harness(mas) as harness:
            send = harness["send"]
            _, acme_task_id, acme_context_id = await _start_paused(send)
            await send(
                {
                    "messageId": "acme-approval",
                    "role": "user",
                    "taskId": acme_task_id,
                    "contextId": acme_context_id,
                    "parts": [{"kind": "text", "text": "approve option 2"}],
                }
            )
            pending_after_error = await _pending_state(harness)
            retry_results = await send(
                {
                    "messageId": "acme-retry-after-unknown-task",
                    "role": "user",
                    "contextId": acme_context_id,
                    "parts": [{"kind": "text", "text": "approve option 2"}],
                }
            )
            return mas, harness, pending_after_error, retry_results

    mas, harness, pending, retry_results = asyncio.run(run())
    assert len(mas.requests) == 2
    assert pending == {}
    assert len(harness["router_model"].requests) == 2
    assert "Normal root reply" in json.dumps(retry_results)
    assert harness["approval_transfers"] == ["mas_orchestrator"]


def _state(results):
    return _find_task(results)["status"]["state"].lower().replace(
        "_", "-"
    ).removeprefix("task-state-")


async def _decision(send, task_id, context_id, text="approve option 2"):
    return await send({
        "messageId": str(len(text)) + text,
        "role": "user", "taskId": task_id, "contextId": context_id,
        "parts": [{"kind": "text", "text": text}],
    })


@pytest.mark.parametrize("first_state", ["submitted", "working", "empty"])
def test_nonterminal_or_empty_eof_is_not_success(first_state):
    async def run():
        mas = FakeMAS(first_state=first_state)
        async with _agent_harness(mas) as harness:
            results = await harness["send"]({
                "messageId": "initial-eof", "role": "user",
                "contextId": ACME_CONTEXT_ID,
                "parts": [{"kind": "text", "text": "Investigate"}],
            })
            return results, await _pending_state(harness)
    results, pending = asyncio.run(run())
    assert _state(results) == "failed"
    assert pending == {}
    assert "ended" in json.dumps(results)


def test_working_eof_after_approval_keeps_active_binding_but_fails_attachment():
    async def run():
        mas = FakeMAS(resume_eof=True, query_state="working")
        async with _agent_harness(mas) as harness:
            _, task_id, context_id = await _start_paused(harness["send"])
            results = await _decision(harness["send"], task_id, context_id)
            session = await harness["session_service"].get_session(
                app_name="acme", user_id=f"A2A_USER_{ACME_CONTEXT_ID}",
                session_id=ACME_CONTEXT_ID,
            )
            return mas, results, session.state, task_id
    mas, results, state, task_id = asyncio.run(run())
    assert _state(results) == "failed"
    assert state["acme_pending_approvals"] == {}
    assert state["acme_active_downstream_tasks"][task_id]["mas_task_id"] == MAS_TASK_ID
    assert len(mas.requests) == 2  # recovery is read-only, never replays approval
    assert len(mas.queries) == 1
    assert "Verification passed" not in json.dumps(results)


@pytest.mark.parametrize("query_state,expected", [
    ("input-required", "input-required"), ("completed", "completed"),
])
def test_working_eof_can_project_a_later_confirmed_pause_or_result(query_state, expected):
    async def run():
        mas = FakeMAS(resume_eof=True, query_state=query_state)
        async with _agent_harness(mas) as harness:
            _, task_id, context_id = await _start_paused(harness["send"])
            results = await _decision(harness["send"], task_id, context_id)
            return mas, results, await _pending_state(harness), task_id
    mas, results, pending, task_id = asyncio.run(run())
    assert _state(results) == expected
    assert len(mas.requests) == 2
    assert len(mas.queries) == 1
    assert list(pending) == ([task_id] if query_state == "input-required" else [])


@pytest.mark.parametrize("query_state", [None, "working", "auth-required", "input-required"])
def test_rpc_validation_error_recovers_only_a_confirmed_open_gate(query_state):
    async def run():
        mas = FakeMAS(decision_rpc_error=True, query_state=query_state)
        async with _agent_harness(mas) as harness:
            _, task_id, context_id = await _start_paused(harness["send"])
            invalid = await _decision(harness["send"], task_id, context_id, "approve option -1")
            pending = await _pending_state(harness)
            session = await harness["session_service"].get_session(
                app_name="acme", user_id=f"A2A_USER_{ACME_CONTEXT_ID}",
                session_id=ACME_CONTEXT_ID,
            )
            active = session.state.get("acme_active_downstream_tasks", {})
            corrected = None
            if query_state == "input-required":
                corrected = await _decision(harness["send"], task_id, context_id)
            return mas, harness, task_id, invalid, pending, active, corrected
    mas, harness, task_id, invalid, pending, active, corrected = asyncio.run(run())
    assert len(mas.queries) == 1
    if query_state == "input-required":
        assert _state(invalid) == "input-required"
        assert list(pending) == [task_id]
        assert _state(corrected) == "completed"
        assert "Verification passed" in json.dumps(corrected)
        assert mas.requests[-1]["params"]["message"]["taskId"] == MAS_TASK_ID
        assert len(harness["router_model"].requests) == 1
    else:
        assert _state(invalid) == "failed"
        assert pending == {}
        assert len(mas.requests) == 2
        assert "A2A request failed" in json.dumps(invalid)
        assert active[task_id]["mas_task_id"] == MAS_TASK_ID
        assert active[task_id]["mas_context_id"] == MAS_CONTEXT_ID


def test_structured_mas_options_survive_the_real_adk_input_call():
    approval = {
        "stage": "Execution", "run": "run-1", "targetCluster": "cluster-x",
        "decisions": ["approve", "reject"],
        "options": [
            {"index": i, "number": i + 1, "title": f"Option {i + 1}",
             "summary": (f"Full summary {i + 1}. " * 25).strip()}
            for i in range(3)
        ],
    }
    async def run():
        mas = FakeMAS(approval_request=approval)
        async with _agent_harness(mas) as harness:
            results, _, _ = await _start_paused(harness["send"])
            return results
    results = asyncio.run(run())

    def find_calls(value):
        if isinstance(value, dict):
            if isinstance(value.get("args"), dict) and "input_required" in value["args"]:
                yield value["args"]["input_required"]
            for child in value.values():
                yield from find_calls(child)
        elif isinstance(value, list):
            for child in value:
                yield from find_calls(child)
    payloads = list(find_calls(results))
    assert payloads
    assert any(payload.get("approvalRequest") == approval for payload in payloads)
    # Exercise the actual browser extraction/labels on the real ADK projection,
    # not a handcrafted function-call payload.
    if shutil.which("node"):
        ui = (Path(__file__).parents[1] / "src/acme_agent/ui.py").read_text()
        functions = ui[ui.index("    function findOptions("):ui.index("    function updateApprovalPanel(")]
        payload = next(p for p in payloads if p.get("approvalRequest") == approval)
        js = functions + f"\nconst options = findOptions({json.dumps(payload)});\n"
        js += "console.log(JSON.stringify({options, labels: options.map(optionLabel)}));"
        browser = json.loads(subprocess.run(
            ["node", "-e", js], capture_output=True, text=True, check=True
        ).stdout)
        assert browser["options"] == approval["options"]
        assert len(browser["labels"]) == 3
        assert approval["options"][2]["summary"] in browser["labels"][2]


def test_contentless_input_required_is_an_outer_pause_not_completion():
    async def run():
        mas = FakeMAS(contentless_pause=True)
        async with _agent_harness(mas) as harness:
            _, task_id, context_id = await _start_paused(harness["send"])
            pending = await _pending_state(harness)
            result = await _decision(harness["send"], task_id, context_id)
            return pending, task_id, result
    pending, task_id, result = asyncio.run(run())
    assert list(pending) == [task_id]
    assert _state(result) == "completed"


def test_actual_mas_115_projection_survives_pinned_acme_and_correction():
    """Replay MAS GetTask JSON through ADK 2.10.0 / A2A 1.2.1 and the UI.

    Captured from MAS's pinned SDK 1.1.5 inbound app using a synthetic durable
    pending gate; no credentials or live deployment data are in the fixture.
    Preserve protobuf Struct's integral float indexes in this wire contract.
    """

    projection = json.loads(
        (Path(__file__).parent / "fixtures/mas_execution_approval_task.json").read_text()
    )
    approval = projection["status"]["message"]["parts"][1]["data"]["approvalRequest"]

    class CapturedMAS:
        def __init__(self):
            self.requests = []
            self.queries = []

        def handler(self, request):
            rpc = json.loads(request.content)
            if rpc["method"] in {"GetTask", "tasks/get"}:
                self.queries.append(rpc)
                assert rpc["params"]["id"] == projection["id"]
                return httpx.Response(200, json={
                    "jsonrpc": "2.0", "id": rpc["id"], "result": projection,
                })
            self.requests.append(rpc)
            if len(self.requests) == 2:
                return httpx.Response(200, json={
                    "jsonrpc": "2.0", "id": rpc["id"],
                    "error": {"code": -32602, "message": "Invalid decision"},
                })
            task = json.loads(json.dumps(projection))
            if len(self.requests) == 3:
                task["status"]["state"] = "TASK_STATE_COMPLETED"
                task["status"]["message"]["parts"] = [{"text": "Verification passed"}]
            event = {"jsonrpc": "2.0", "id": rpc["id"], "result": {"task": task}}
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"},
                content=f"data: {json.dumps(event)}\n\n".encode(),
            )

    async def run():
        mas = CapturedMAS()
        async with _agent_harness(mas) as harness:
            paused, task_id, context_id = await _start_paused(harness["send"])
            invalid = await _decision(harness["send"], task_id, context_id, "approve option -1")
            pending = await _pending_state(harness)
            corrected = await _decision(harness["send"], task_id, context_id)
            return mas, harness, paused, invalid, pending, corrected, task_id

    mas, harness, paused, invalid, pending, corrected, task_id = asyncio.run(run())
    assert _state(invalid) == "input-required"
    assert pending[task_id]["mas_task_id"] == projection["id"]
    assert pending[task_id]["mas_context_id"] == projection["contextId"]
    assert _state(corrected) == "completed"
    assert "Verification passed" in json.dumps(corrected)
    assert len(mas.requests) == 3 and len(mas.queries) == 1
    assert len(harness["router_model"].requests) == 1
    for rpc in mas.requests[1:]:
        message = rpc["params"]["message"]
        assert message["taskId"] == projection["id"]
        assert message["contextId"] == projection["contextId"]

    # Use the actual embedded browser functions on both the original pause
    # and recovered gate; recovery must not lose the complete options object.
    def input_payload(value):
        if isinstance(value, dict):
            if isinstance(value.get("args"), dict) and "input_required" in value["args"]:
                return value["args"]["input_required"]
            children = value.values()
        elif isinstance(value, list):
            children = value
        else:
            return None
        return next((found for child in children if (found := input_payload(child))), None)

    payloads = [input_payload(results) for results in (paused, invalid)]
    assert all(payload and payload.get("approvalRequest") == approval for payload in payloads)
    if shutil.which("node"):
        ui = (Path(__file__).parents[1] / "src/acme_agent/ui.py").read_text()
        functions = ui[ui.index("    function findOptions("):ui.index("    function updateApprovalPanel(")]
        for payload in payloads:
            js = functions + f"\nconst options = findOptions({json.dumps(payload)});\n"
            js += "console.log(JSON.stringify({options, labels: options.map(optionLabel)}));"
            browser = json.loads(subprocess.run(
                ["node", "-e", js], capture_output=True, text=True, check=True
            ).stdout)
            assert browser["options"] == approval["options"]
            assert browser["labels"] == [
                "Option one: First remediation", "Option two: Second remediation",
            ]
