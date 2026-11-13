"""Shared offline HTTP fixtures for the authenticated chat test suite."""

from __future__ import annotations

import json
import re
from typing import Any
from unittest.mock import patch

import pydantic_ai.models as pai_models
from asgiref.sync import sync_to_async
from django.contrib.auth.models import Group, User
from django.test import AsyncClient, TransactionTestCase
from pydantic_ai import Agent, ModelSettings
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse
from pydantic_ai.models.function import FunctionModel

from accounts import services
from accounts.models import (
    AuthorizationAudit,
    Department,
    DepartmentMembership,
)
from chat import threads
from chat.models import ChatThread, ChatTurn
from datasets.discovery import configured_source_key
from datasets.models import Dataset, DepartmentDataset, MembershipDataset

PROVIDER_SETTINGS = {
    "LLM_BASE_URL": "https://offline.invalid/v1",
    "LLM_API_KEY": "offline-dummy-key",
    "LLM_MODEL": "offline-model",
    "CHAT_SHOW_REASONING_DETAIL": False,
}

CSRF_PATTERN = re.compile(r'name="csrfmiddlewaretoken" value="([^"]+)"')
THREADS_URL = "/api/threads/"
EMPTY_HEADING = "Where should we begin?"


def prompt_texts(messages: list[ModelMessage]) -> list[str]:
    texts: list[str] = []
    for message in messages:
        if not isinstance(message, ModelRequest):
            continue
        for part in message.parts:
            content = getattr(part, "content", None)
            if isinstance(content, str):
                texts.append(content)
    return texts


def reply_texts(messages: list[ModelMessage]) -> list[str]:
    texts: list[str] = []
    for message in messages:
        if isinstance(message, ModelResponse):
            for part in message.parts:
                content = getattr(part, "content", None)
                if isinstance(content, str):
                    texts.append(content)
    return texts


def frame(event: str, payload: dict[str, Any]) -> bytes:
    """Encode one expected SSE frame, matching the view's wire format."""
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event}\ndata: {data}\n\n".encode()


def events(body: bytes) -> list[tuple[str, Any]]:
    """Split a consumed SSE body into ``(event, parsed data)`` pairs."""
    parsed: list[tuple[str, Any]] = []
    for block in body.decode("utf-8").split("\n\n"):
        if not block.strip() or block.lstrip().startswith(":"):
            continue
        event = ""
        data: list[str] = []
        for line in block.split("\n"):
            if line.startswith("event:"):
                event = line[len("event:") :].strip()
            elif line.startswith("data:"):
                data.append(line[len("data:") :].lstrip())
        parsed.append((event, json.loads("\n".join(data))))
    return parsed


def named(parsed: list[tuple[str, Any]], name: str) -> list[Any]:
    """Return the payloads of one frame kind, in stream order."""
    return [data for event, data in parsed if event == name]


def deltas(parsed: list[tuple[str, Any]]) -> list[str]:
    """Return the text of every delta frame, in stream order."""
    return [data["text"] for data in named(parsed, "delta")]


async def next_frame(iterator: Any, name: str) -> bytes:
    """Consume frames until one of ``name`` arrives, returning its bytes."""
    async for chunk in iterator:
        if chunk.startswith(f"event: {name}\n".encode()):
            return chunk
    raise AssertionError(f"no {name} frame arrived")


async def read_stream(iterator: Any) -> bytes:
    return b"".join([chunk async for chunk in iterator])


async def read_body(response: Any) -> bytes:
    return await read_stream(response.streaming_content)


class TrackedAgent:
    def __init__(self, agent: Agent[None, str]) -> None:
        self.agent = agent
        self.exited = 0

    async def __aenter__(self) -> Agent[None, str]:
        await self.agent.__aenter__()
        return self.agent

    async def __aexit__(self, *exc_info: object) -> bool | None:
        self.exited += 1
        return await self.agent.__aexit__(*exc_info)


class AgentFactory:
    """Build fresh real agents around a streaming FunctionModel callback.

    A non-empty constructor sequence is attached alone. Otherwise the
    server-supplied sequence is attached exactly as passed.
    """

    def __init__(
        self, stream: Any, *, timeout: float = 60, toolsets: tuple = ()
    ) -> None:
        self.stream = stream
        self.timeout = timeout
        self.toolsets = toolsets
        self.seen: list[list[ModelMessage]] = []
        self.attached: list[tuple] = []
        self.agents: list[TrackedAgent] = []

    def __call__(self, **kwargs: Any) -> TrackedAgent:
        supplied = tuple(kwargs.get("toolsets") or ())
        attached = list(self.toolsets) if self.toolsets else list(supplied)
        self.attached.append(tuple(attached))

        def recording(messages: list[ModelMessage], info: Any) -> Any:
            self.seen.append(list(messages))
            return self.stream(messages, info)

        tracked = TrackedAgent(
            Agent(
                FunctionModel(stream_function=recording),
                name="offline_chat",
                toolsets=attached,
                model_settings=ModelSettings(
                    request_timeout=self.timeout,
                    max_retries=0,
                ),
            )
        )
        self.agents.append(tracked)
        return tracked


class ChatHttpTestCase(TransactionTestCase):
    """Base with authenticated clients, thread fixtures, and a patched agent."""

    def setUp(self) -> None:
        self._previous_allow = pai_models.ALLOW_MODEL_REQUESTS
        pai_models.ALLOW_MODEL_REQUESTS = False
        self.addCleanup(self._restore_allow)

    def _restore_allow(self) -> None:
        pai_models.ALLOW_MODEL_REQUESTS = self._previous_allow

    def _patch_agent(
        self,
        stream: Any,
        *,
        timeout: float = 60,
        toolsets: tuple = (),
    ) -> AgentFactory:
        factory = AgentFactory(stream, timeout=timeout, toolsets=toolsets)
        patcher = patch("chat.service.build_agent", factory)
        patcher.start()
        self.addCleanup(patcher.stop)
        return factory

    def _echo_agent(self) -> AgentFactory:
        def stream(messages: list[ModelMessage], info: Any) -> Any:
            async def produce() -> Any:
                yield f"echo:{prompt_texts(messages)[-1]}"

            return produce()

        return self._patch_agent(stream)

    async def make_user(self, username: str, password: str = "chat-pass-7f3a") -> User:
        return await sync_to_async(User.objects.create_user)(
            username=username,
            password=password,
        )

    async def client_for(self, user: User) -> AsyncClient:
        client = AsyncClient()
        await client.aforce_login(user)
        return client

    async def token(self, client: AsyncClient, path: str = "/") -> str:
        response = await client.get(path)
        self.assertEqual(response.status_code, 200)
        match = CSRF_PATTERN.search(response.content.decode())
        self.assertIsNotNone(match)
        return match.group(1)

    async def make_thread(self, user: User, title: str | None = None) -> ChatThread:
        def create() -> ChatThread:
            thread = ChatThread.objects.create(owner=user)
            if title is not None:
                thread.title = title
                thread.save(update_fields=["title"])
            return thread

        return await sync_to_async(create)()

    async def fetch_row(self, thread_id: Any) -> ChatThread:
        return await sync_to_async(ChatThread.objects.get)(pk=thread_id)

    async def chat_turns(self, thread_id: Any) -> list[dict[str, Any]]:
        """Return the stored private turn rows as plain dictionaries."""

        def load() -> list[dict[str, Any]]:
            return list(
                ChatTurn.objects.filter(thread_id=thread_id)
                .order_by("turn_index")
                .values(
                    "id",
                    "turn_index",
                    "pre_history_json",
                    "requested_model",
                    "resolved_model",
                    "completed_at",
                    "duration_ms",
                    "output_tokens",
                    "activity",
                )
            )

        return await sync_to_async(load)()

    @staticmethod
    def thread_state(row: ChatThread) -> dict[str, Any]:
        """The persisted conversation fields a failed action must not change."""
        return {
            "history_json": row.history_json,
            "display": row.display,
            "revision": row.revision,
            "title": row.title,
            "updated_at": row.updated_at,
        }

    async def row_exists(self, thread_id: Any) -> bool:
        return await sync_to_async(ChatThread.objects.filter(pk=thread_id).exists)()

    async def detail(self, client: AsyncClient, thread_id: Any) -> dict[str, Any]:
        response = await client.get(f"/api/threads/{thread_id}/")
        self.assertEqual(response.status_code, 200)
        return response.json()["thread"]

    async def summary_list(self, client: AsyncClient) -> list[dict[str, Any]]:
        response = await client.get(THREADS_URL)
        self.assertEqual(response.status_code, 200)
        return response.json()["threads"]

    async def send_turn(
        self,
        client: AsyncClient,
        token: str,
        thread_id: Any,
        message: str,
        revision: int,
    ) -> Any:
        return await client.post(
            f"/api/threads/{thread_id}/messages/",
            data=json.dumps({"message": message, "revision": revision}),
            content_type="application/json",
            headers={"X-CSRFToken": token},
        )

    async def regenerate(
        self,
        client: AsyncClient,
        token: str,
        thread_id: Any,
        turn_id: Any,
        revision: int,
    ) -> Any:
        return await client.post(
            f"/api/threads/{thread_id}/turns/{turn_id}/regenerate/",
            data=json.dumps({"revision": revision}),
            content_type="application/json",
            headers={"X-CSRFToken": token},
        )

    async def send_files(
        self,
        client: AsyncClient,
        token: str,
        thread_id: Any,
        message: str,
        revision: int,
        files: list[Any],
    ) -> Any:
        """POST one multipart turn with repeated ``files`` fields."""
        return await client.post(
            f"/api/threads/{thread_id}/messages/",
            data={"message": message, "revision": str(revision), "files": files},
            headers={"X-CSRFToken": token},
        )

    async def uploaded_turn(
        self,
        client: AsyncClient,
        token: str,
        thread_id: Any,
        message: str,
        revision: int,
        files: list[Any],
    ) -> tuple[bytes, list[tuple[str, Any]]]:
        response = await self.send_files(
            client, token, thread_id, message, revision, files
        )
        self.assertEqual(response.status_code, 200)
        body = await read_body(response)
        await sync_to_async(response.close)()
        return body, events(body)

    async def delete_exchange(
        self,
        client: AsyncClient,
        token: str,
        thread_id: Any,
        turn_id: Any,
        revision: int,
    ) -> Any:
        return await client.delete(
            f"/api/threads/{thread_id}/turns/{turn_id}/",
            data=json.dumps({"revision": revision}),
            content_type="application/json",
            headers={"X-CSRFToken": token},
        )

    async def regenerated(
        self,
        client: AsyncClient,
        token: str,
        thread_id: Any,
        turn_id: Any,
        revision: int,
    ) -> tuple[bytes, list[tuple[str, Any]]]:
        response = await self.regenerate(client, token, thread_id, turn_id, revision)
        self.assertEqual(response.status_code, 200)
        body = await read_body(response)
        await sync_to_async(response.close)()
        return body, events(body)

    async def turns(self, client: AsyncClient, thread_id: Any) -> list[dict[str, Any]]:
        return (await self.detail(client, thread_id))["turns"]

    async def turn(
        self,
        client: AsyncClient,
        token: str,
        thread_id: Any,
        message: str,
        revision: int,
    ) -> tuple[bytes, list[tuple[str, Any]]]:
        response = await self.send_turn(client, token, thread_id, message, revision)
        self.assertEqual(response.status_code, 200)
        body = await read_body(response)
        await sync_to_async(response.close)()
        return body, events(body)

    async def completed_turn(
        self,
        client: AsyncClient,
        token: str,
        thread_id: Any,
        message: str,
        revision: int,
    ) -> tuple[dict[str, Any], int]:
        _body, parsed = await self.turn(client, token, thread_id, message, revision)
        self.assertEqual(parsed[-1][0], "done", parsed)
        payload = parsed[-1][1]
        return payload, payload["thread"]["revision"]

    def error_code(self, response: Any) -> str:
        return response.json()["error"]["code"]

    @staticmethod
    def max_bytes() -> int:
        return threads.MAX_HISTORY_BYTES


class GovernanceTestCase(TransactionTestCase):
    """Fixtures and helpers for the accounts/datasets governance suites.

    Fixtures are created directly for read-path tests; every mutation under
    test goes through the public service so the audit/revision contract is
    exercised rather than bypassed.
    """

    maxDiff = None

    def setUp(self) -> None:
        super().setUp()
        self.enterContext(self.settings(WREN_MCP_URL="http://127.0.0.1:18888/mcp"))
        # A TransactionTestCase flush removes rows created by data migrations,
        # so every governance test restores the fixed definitions first.
        services.ensure_fixed_roles()

    def make_account(
        self,
        username: str,
        *,
        password: str = "manage-pass-7f3a",
        superuser: bool = False,
        groups: tuple[str, ...] = (),
    ) -> User:
        user = User.objects.create_user(username=username, password=password)
        if superuser:
            user.is_superuser = True
            user.is_staff = True
            user.save(update_fields=["is_superuser", "is_staff"])
        for name in groups:
            user.groups.add(Group.objects.get(name=name))
        return user

    def revision(self) -> int:
        return services.current_state()["revision"]

    def epoch(self) -> int:
        return services.current_state()["epoch"]

    def group(self, name: str) -> Group:
        return Group.objects.get(name=name)

    def make_department(self, code: str, name: str | None = None) -> Department:
        return Department.objects.create(code=code, name=name or code.title())

    def make_dataset(
        self,
        code: str,
        *,
        model_name: str | None = None,
        is_active: bool = True,
    ) -> Dataset:
        return Dataset.objects.create(
            code=code,
            display_name=code.replace("_", " ").title(),
            description="",
            source_key=configured_source_key(),
            project_key="jaffle-wren",
            model_name=model_name or code,
            catalog="jaffle_shop",
            schema="main",
            physical_table=model_name or code,
            is_active=is_active,
            is_available=True,
            validation_status="metadata_discovered",
        )

    def grant(self, department: Department, dataset: Dataset) -> DepartmentDataset:
        grant, _created = DepartmentDataset.objects.get_or_create(
            department=department, dataset=dataset
        )
        return grant

    def join(
        self,
        user: User,
        department: Department,
        *,
        role: str = "staff",
        datasets: tuple[Dataset, ...] = (),
        is_active: bool = True,
    ) -> DepartmentMembership:
        membership = DepartmentMembership.objects.create(
            user=user,
            department=department,
            membership_role=role,
            is_active=is_active,
        )
        for dataset in datasets:
            MembershipDataset.objects.create(
                membership=membership,
                department_grant=self.grant(department, dataset),
            )
        return membership

    def audit_actions(self) -> list[str]:
        return list(AuthorizationAudit.objects.values_list("action", flat=True))
