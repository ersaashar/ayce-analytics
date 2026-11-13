"""Chat governance: quarantine, department context and run-time revocation."""

from __future__ import annotations

import asyncio
import json
import re
import socketserver
import threading
import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import patch

from asgiref.sync import async_to_sync, sync_to_async
from django.contrib.auth.models import User
from django.db import connection
from django.db.migrations.executor import MigrationExecutor
from django.test import Client, override_settings
from pydantic_ai import FunctionToolset, RunContext
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    UserPromptPart,
)
from pydantic_ai.models.function import DeltaToolCall

from accounts import services
from accounts.models import Department, DepartmentMembership
from chat import service, threads
from chat.models import ChatAttachment, ChatThread, ChatTurn, ChatTurnDataset
from datasets.discovery import configured_source_key
from datasets.models import (
    Dataset,
    DatasetCatalogState,
    DepartmentDataset,
    MembershipDataset,
)
from datasets.services import refresh_catalog
from tests.support import (
    PROVIDER_SETTINGS,
    ChatHttpTestCase,
    GovernanceTestCase,
    next_frame,
    read_stream,
)
from tests.wren_fixture import WrenFixture

BOOTSTRAP_PATTERN = re.compile(
    r'<script id="chat-bootstrap" type="application/json">(.*?)</script>', re.S
)
SUPERADMIN_GROUP = "AYCE Superadmin"
STAFF_GROUP = "AYCE Staff"
LEAD_GROUP = "AYCE Lead"
PASSWORD = "manage-pass-7f3a"


def canonical(messages: list[Any]) -> str:
    from pydantic_ai.messages import ModelMessagesTypeAdapter

    return ModelMessagesTypeAdapter.dump_json(messages).decode("utf-8")


def plain_history(question: str = "hi", answer: str = "hello") -> str:
    return canonical(
        [
            ModelRequest(parts=[UserPromptPart(question)]),
            ModelResponse(parts=[TextPart(answer)]),
        ]
    )


def tool_history() -> str:
    return canonical(
        [
            ModelRequest(parts=[UserPromptPart("hi")]),
            ModelResponse(
                parts=[
                    ToolCallPart(
                        "query_warehouse", {"sql": "select 1"}, tool_call_id="c1"
                    ),
                    TextPart("hello"),
                ]
            ),
        ]
    )


class ChatGovernanceTestCase(ChatHttpTestCase):
    def setUp(self) -> None:
        ChatHttpTestCase.setUp(self)
        services.ensure_fixed_roles()

    def revision(self) -> int:
        return services.current_state()["revision"]

    def make_account(
        self,
        username: str,
        *,
        password: str = PASSWORD,
        superuser: bool = False,
        groups: tuple[str, ...] = (),
    ) -> User:
        from django.contrib.auth.models import Group

        user = User.objects.create_user(username=username, password=password)
        if superuser:
            user.is_superuser = True
            user.is_staff = True
            user.save(update_fields=["is_superuser", "is_staff"])
        for name in groups:
            user.groups.add(Group.objects.get(name=name))
        return user


class LegacyClassificationTests(ChatGovernanceTestCase):
    """The migration locks anything whose provenance cannot be proven safe."""

    def insert_thread(
        self,
        *,
        owner: User,
        title: str,
        history_json: str,
        display: list[dict[str, str]],
        revision: int = 3,
    ) -> uuid.UUID:
        thread_id = uuid.uuid4()
        now = datetime.now(UTC)
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO chat_chatthread "
                "(id, owner_id, title, history_json, display, revision, "
                "created_at, updated_at, quarantined) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                [
                    thread_id.hex,
                    owner.pk,
                    title,
                    history_json,
                    json.dumps(display),
                    revision,
                    now.isoformat(),
                    now.isoformat(),
                    0,
                ],
            )
        return thread_id

    def insert_turn(
        self,
        *,
        thread_id: uuid.UUID,
        checkpoint: str | None,
        activity: dict | None,
    ) -> uuid.UUID:
        turn_id = uuid.uuid4()
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO chat_chatturn "
                "(id, thread_id, turn_index, pre_history_json, requested_model, "
                "resolved_model, completed_at, duration_ms, output_tokens, "
                "activity, output_limit_reached) "
                "VALUES (%s, %s, %s, %s, NULL, NULL, NULL, NULL, NULL, %s, 0)",
                [
                    turn_id.hex,
                    thread_id.hex,
                    0,
                    checkpoint,
                    json.dumps(activity) if activity is not None else None,
                ],
            )
        return turn_id

    def reread(self, thread_id: uuid.UUID) -> tuple:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT title, history_json, display, revision, owner_id, quarantined "
                "FROM chat_chatthread WHERE id = %s",
                [thread_id.hex],
            )
            return cursor.fetchone()

    def test_classification_locks_only_unprovable_histories(self) -> None:
        owner = self.make_account("legacy-owner")
        plain_id = self.insert_thread(
            owner=owner,
            title="Plain legacy chat",
            history_json=plain_history(),
            display=[
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": "hello"},
            ],
        )
        tool_id = self.insert_thread(
            owner=owner,
            title="Tool legacy chat",
            history_json=tool_history(),
            display=[
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": "hello"},
            ],
        )
        unknown_id = self.insert_thread(
            owner=owner,
            title="Unknown part chat",
            history_json=json.dumps(
                [
                    {
                        "kind": "request",
                        "parts": [{"part_kind": "user-prompt", "content": "hi"}],
                    },
                    {
                        "kind": "response",
                        "parts": [{"part_kind": "mystery", "content": "hello"}],
                    },
                ]
            ),
            display=[
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": "hello"},
            ],
        )
        malformed_id = self.insert_thread(
            owner=owner,
            title="Malformed chat",
            history_json="{not json",
            display=[],
        )
        activity_id = self.insert_thread(
            owner=owner,
            title="Activity chat",
            history_json=plain_history(),
            display=[
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": "hello"},
            ],
        )
        self.insert_turn(
            thread_id=activity_id,
            checkpoint="[]",
            activity={
                "version": 1,
                "turn_id": str(uuid.uuid4()),
                "steps": [
                    {
                        "id": 0,
                        "kind": "wren_tool",
                        "status": "complete",
                    }
                ],
                "truncated": False,
            },
        )
        checkpoint_id = self.insert_thread(
            owner=owner,
            title="Checkpoint chat",
            history_json=plain_history(),
            display=[
                {"role": "user", "content": "hi"},
                {"role": "assistant", "content": "hello"},
            ],
        )
        self.insert_turn(
            thread_id=checkpoint_id,
            checkpoint=tool_history(),
            activity=None,
        )

        before = {
            thread_id: self.reread(thread_id)
            for thread_id in (
                plain_id,
                tool_id,
                unknown_id,
                malformed_id,
                activity_id,
                checkpoint_id,
            )
        }

        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(
            [("chat", "0005_alter_chatthread_options_chatthread_department_and_more")]
        )
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(executor.loader.graph.leaf_nodes())

        states = {}
        for thread_id, row in before.items():
            after = self.reread(thread_id)
            self.assertEqual(after[:5], row[:5])
            states[thread_id] = after[5]
        self.assertFalse(states[plain_id])
        self.assertTrue(states[tool_id])
        self.assertTrue(states[unknown_id])
        self.assertTrue(states[malformed_id])
        self.assertTrue(states[activity_id])
        self.assertTrue(states[checkpoint_id])


class LockedThreadTests(ChatGovernanceTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.owner = self.make_account("locked-owner")
        self.other = self.make_account("lock-other")
        self.thread = ChatThread.objects.create(
            owner=self.owner,
            title="Sensitive injected title",
            history_json=plain_history("secret question", "secret answer"),
            display=[
                {"role": "user", "content": "secret question"},
                {"role": "assistant", "content": "secret answer"},
            ],
            revision=2,
            quarantined=True,
        )
        ChatTurn.objects.create(
            thread=self.thread,
            turn_index=0,
            pre_history_json="[]",
            activity={
                "version": 1,
                "turn_id": str(uuid.uuid4()),
                "steps": [{"id": 0, "kind": "model", "status": "complete"}],
                "truncated": False,
            },
        )
        ChatAttachment.objects.create(
            turn=ChatTurn.objects.get(thread=self.thread),
            position=0,
            name="secret-invoice.pdf",
            media_type="application/pdf",
            content_media_type="application/pdf",
            size=10,
            data=b"%PDF-1.4\n",
        )

    def sync_client(self, user: User) -> Client:
        client = Client()
        client.force_login(user)
        return client

    def test_locked_projection_redacts_title_content_and_files(self) -> None:
        detail = self.sync_client(self.owner).get(f"/api/threads/{self.thread.pk}/")
        self.assertEqual(detail.status_code, 200)
        body = detail.content.decode()
        for secret in (
            "Sensitive injected title",
            "secret question",
            "secret answer",
            "secret-invoice.pdf",
        ):
            self.assertNotIn(secret, body)
        thread = detail.json()["thread"]
        self.assertEqual(thread["title"], "Restricted chat")
        self.assertIsNone(thread["department"])
        self.assertEqual(thread["display"], [])
        self.assertEqual(thread["turns"], [])
        self.assertEqual(thread["access"], "quarantined")
        self.assertIn("locked", thread["conversation_error"])

        listed = self.sync_client(self.owner).get("/api/threads/").json()["threads"]
        self.assertEqual(listed[0]["title"], "Restricted chat")

        page = self.sync_client(self.owner).get(f"/chat/{self.thread.pk}/")
        self.assertEqual(page.status_code, 200)
        html = page.content.decode()
        for secret in (
            "Sensitive injected title",
            "secret question",
            "secret answer",
            "secret-invoice.pdf",
        ):
            self.assertNotIn(secret, html)
        self.assertIn("Restricted chat", html)

    def test_locked_thread_rejects_actions_but_allows_whole_deletion(self) -> None:
        client = self.sync_client(self.owner)
        self.assertEqual(
            client.patch(
                f"/api/threads/{self.thread.pk}/",
                data=json.dumps({"title": "rename", "revision": 2}),
                content_type="application/json",
            ).json()["error"]["code"],
            "history_quarantined",
        )
        self.assertEqual(
            client.post(
                f"/api/threads/{self.thread.pk}/messages/",
                data=json.dumps({"message": "hello", "revision": 2}),
                content_type="application/json",
            ).json()["error"]["code"],
            "history_quarantined",
        )
        turn = ChatTurn.objects.get(thread=self.thread)
        self.assertEqual(
            client.delete(
                f"/api/threads/{self.thread.pk}/turns/{turn.pk}/",
                data=json.dumps({"revision": 2}),
                content_type="application/json",
            ).json()["error"]["code"],
            "history_quarantined",
        )

        foreign = self.sync_client(self.other).get(f"/api/threads/{self.thread.pk}/")
        self.assertEqual(foreign.status_code, 404)
        self.assertEqual(foreign.json()["error"]["code"], "thread_not_found")

        deleted = client.delete(
            f"/api/threads/{self.thread.pk}/",
            data=json.dumps({"revision": 2}),
            content_type="application/json",
        )
        self.assertEqual(deleted.status_code, 200)
        self.assertFalse(ChatThread.objects.filter(pk=self.thread.pk).exists())
        self.assertEqual(ChatAttachment.objects.count(), 0)


class DepartmentThreadTests(ChatGovernanceTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.owner = self.make_account("dept-owner", groups=(STAFF_GROUP,))
        self.department = Department.objects.create(code="finance", name="Finance")
        self.membership = DepartmentMembership.objects.create(
            user=self.owner, department=self.department, membership_role="staff"
        )

    def sync_client(self, user: User) -> Client:
        client = Client()
        client.force_login(user)
        return client

    def create(self, client: Client, body: dict) -> Any:
        return client.post(
            "/api/threads/",
            data=json.dumps(body),
            content_type="application/json",
        )

    def test_department_bound_creation_validates_membership(self) -> None:
        general = self.create(self.sync_client(self.owner), {})
        self.assertEqual(general.status_code, 201)
        self.assertIsNone(general.json()["thread"]["department"])

        bound = self.create(
            self.sync_client(self.owner), {"department_id": str(self.department.pk)}
        )
        self.assertEqual(bound.status_code, 201)
        self.assertEqual(bound.json()["thread"]["department"]["code"], "finance")

        self.membership.is_active = False
        self.membership.save(update_fields=["is_active"])
        rejected = self.create(
            self.sync_client(self.owner), {"department_id": str(self.department.pk)}
        )
        self.assertEqual(rejected.status_code, 404)
        self.assertEqual(rejected.json()["error"]["code"], "resource_not_found")
        self.membership.is_active = True
        self.membership.save(update_fields=["is_active"])

        forged = self.create(
            self.sync_client(self.owner),
            {"department_id": "00000000-0000-4000-8000-000000000000"},
        )
        self.assertEqual(forged.status_code, 404)

    def test_context_cannot_change_after_creation(self) -> None:
        bound = self.create(
            self.sync_client(self.owner), {"department_id": str(self.department.pk)}
        ).json()["thread"]
        response = self.sync_client(self.owner).patch(
            f"/api/threads/{bound['id']}/",
            data=json.dumps(
                {
                    "title": "renamed",
                    "revision": bound["revision"],
                    "department_id": None,
                }
            ),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_thread")
        self.assertEqual(
            ChatThread.objects.get(pk=bound["id"]).department_id,
            self.department.pk,
        )

    def test_revoked_department_hides_content(self) -> None:
        bound = self.create(
            self.sync_client(self.owner), {"department_id": str(self.department.pk)}
        ).json()["thread"]
        ChatThread.objects.filter(pk=bound["id"]).update(
            history_json=plain_history("dept question", "dept answer"),
            display=[
                {"role": "user", "content": "dept question"},
                {"role": "assistant", "content": "dept answer"},
            ],
            title="Dept chat",
        )
        self.membership.is_active = False
        self.membership.save(update_fields=["is_active"])
        detail = self.sync_client(self.owner).get(f"/api/threads/{bound['id']}/")
        self.assertEqual(detail.status_code, 200)
        thread = detail.json()["thread"]
        self.assertEqual(thread["access"], "department_revoked")
        self.assertEqual(thread["title"], "Restricted chat")
        self.assertEqual(thread["display"], [])
        self.assertIn("department", thread["conversation_error"])

        send = self.sync_client(self.owner).post(
            f"/api/threads/{bound['id']}/messages/",
            data=json.dumps({"message": "hello", "revision": thread["revision"]}),
            content_type="application/json",
        )
        self.assertEqual(send.json()["error"]["code"], "department_access_revoked")


@override_settings(**PROVIDER_SETTINGS)
class RevocationDuringRunTests(ChatGovernanceTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.owner = self.make_account("run-owner", groups=(STAFF_GROUP,))
        self.department = Department.objects.create(code="finance", name="Finance")
        self.membership = DepartmentMembership.objects.create(
            user=self.owner, department=self.department, membership_role="staff"
        )

    async def bound_thread(self) -> ChatThread:
        return await sync_to_async(ChatThread.objects.create)(
            owner=self.owner, department=self.department
        )

    def test_epoch_change_between_generation_and_commit_rolls_back(self) -> None:
        thread = ChatThread.objects.create(owner=self.owner, department=self.department)
        stale_epoch = services.current_state()["epoch"]
        services.create_department(
            actor_id=self.make_account("epoch-admin", superuser=True).pk,
            revision=self.revision(),
            name="Sales",
            description="",
        )
        token = threads.acquire_run(
            owner_id=self.owner.pk, thread_id=thread.pk, revision=0
        )

        async def run() -> None:
            with self.assertRaises(service.ChatError) as caught:
                await sync_to_async(threads.commit_turn)(
                    owner_id=self.owner.pk,
                    snapshot=thread,
                    message="hello",
                    completed=service.CompletedReply(
                        reply="answer", history_json=plain_history()
                    ),
                    turn_id=uuid.uuid4(),
                    run_token=token,
                    authorization_epoch=stale_epoch,
                )
            self.assertEqual(caught.exception.code, "authorization_changed")

        asyncio.run(run())
        row = ChatThread.objects.get(pk=thread.pk)
        self.assertEqual(row.revision, 0)
        self.assertEqual(row.display, [])
        self.assertEqual(ChatTurn.objects.count(), 0)
        self.assertIsNotNone(row.active_run_token)
        threads.release_run(
            owner_id=self.owner.pk, thread_id=thread.pk, run_token=token
        )
        self.assertIsNone(ChatThread.objects.get(pk=thread.pk).active_run_token)

    def test_general_chat_survives_unrelated_policy_changes(self) -> None:
        thread = ChatThread.objects.create(owner=self.owner)
        token = threads.acquire_run(
            owner_id=self.owner.pk, thread_id=thread.pk, revision=0
        )
        services.create_department(
            actor_id=self.make_account("policy-admin", superuser=True).pk,
            revision=self.revision(),
            name="Sales",
            description="",
        )

        async def run() -> ChatThread:
            return await sync_to_async(threads.commit_turn)(
                owner_id=self.owner.pk,
                snapshot=thread,
                message="hello",
                completed=service.CompletedReply(
                    reply="answer", history_json=plain_history()
                ),
                turn_id=uuid.uuid4(),
                run_token=token,
                authorization_epoch=None,
            )

        committed = asyncio.run(run())
        self.assertEqual(committed.revision, 1)
        self.assertEqual(len(committed.display), 2)

    def test_deactivated_user_cannot_commit_either_mode(self) -> None:
        admin = self.make_account("deactivate-admin", superuser=True)
        thread = ChatThread.objects.create(owner=self.owner)
        token = threads.acquire_run(
            owner_id=self.owner.pk, thread_id=thread.pk, revision=0
        )
        services.update_user(
            actor_id=admin.pk,
            revision=self.revision(),
            user_id=self.owner.pk,
            changes={"is_active": False},
        )

        async def run() -> None:
            with self.assertRaises(service.ChatError) as caught:
                await sync_to_async(threads.commit_turn)(
                    owner_id=self.owner.pk,
                    snapshot=thread,
                    message="hello",
                    completed=service.CompletedReply(
                        reply="answer", history_json=plain_history()
                    ),
                    turn_id=uuid.uuid4(),
                    run_token=token,
                    authorization_epoch=None,
                )
            self.assertEqual(caught.exception.code, "authentication_required")

        asyncio.run(run())
        self.assertEqual(ChatThread.objects.get(pk=thread.pk).display, [])

    async def test_revocation_cancels_a_paused_run(self) -> None:
        gate = asyncio.Event()

        async def stream(messages: list[Any], info: Any) -> Any:
            yield "partial"
            await gate.wait()
            yield "never"

        self._patch_agent(stream)
        thread = await self.bound_thread()
        client = await self.client_for(self.owner)
        token = await self.token(client)

        with patch.object(threads, "RUN_RENEW_INTERVAL_SECONDS", 60):
            response = await self.send_turn(client, token, thread.pk, "hello", 0)
            iterator = response.streaming_content.__aiter__()
            await anext(iterator)
            await next_frame(iterator, "delta")

            await sync_to_async(
                DepartmentMembership.objects.filter(pk=self.membership.pk).update
            )(is_active=False)

            frame = await asyncio.wait_for(next_frame(iterator, "error"), timeout=5)
            self.assertIn(b"department_access_revoked", frame)
            rest = await asyncio.wait_for(read_stream(iterator), timeout=5)
            self.assertNotIn(b"event: done", frame + rest)
            await sync_to_async(response.close)()

        row = await self.fetch_row(thread.pk)
        self.assertEqual(row.display, [])
        self.assertEqual(row.revision, 0)
        self.assertIsNone(row.active_run_token)
        self.assertEqual(await self.chat_turns(thread.pk), [])
        attachments = await sync_to_async(ChatAttachment.objects.count)()
        self.assertEqual(attachments, 0)

    async def test_revoking_membership_cancels_a_paused_regeneration(self) -> None:
        gate = asyncio.Event()
        calls = {"count": 0}

        async def stream(messages: list[Any], info: Any) -> Any:
            calls["count"] += 1
            if calls["count"] == 1:
                yield "first answer"
                return
            yield "partial"
            await gate.wait()
            yield "never"

        self._patch_agent(stream)
        thread = await self.bound_thread()
        client = await self.client_for(self.owner)
        token = await self.token(client)

        with patch.object(threads, "RUN_RENEW_INTERVAL_SECONDS", 60):
            _payload, revision = await self.completed_turn(
                client, token, thread.pk, "hello", 0
            )
            turn_id = await sync_to_async(
                lambda: ChatTurn.objects.get(thread_id=thread.pk).pk
            )()

            response = await self.regenerate(
                client, token, thread.pk, turn_id, revision
            )
            iterator = response.streaming_content.__aiter__()
            await anext(iterator)
            await next_frame(iterator, "delta")

            await sync_to_async(
                DepartmentMembership.objects.filter(pk=self.membership.pk).update
            )(is_active=False)

            frame = await asyncio.wait_for(next_frame(iterator, "error"), timeout=5)
            self.assertIn(b"department_access_revoked", frame)
            rest = await asyncio.wait_for(read_stream(iterator), timeout=5)
            self.assertNotIn(b"event: done", frame + rest)
            await sync_to_async(response.close)()

        row = await self.fetch_row(thread.pk)
        self.assertEqual(row.revision, revision)
        self.assertEqual(row.display[-1]["content"], "first answer")
        self.assertIsNone(row.active_run_token)


@override_settings(**{**PROVIDER_SETTINGS, "CHAT_SHOW_REASONING_DETAIL": True})
class ReasoningDetailTests(ChatGovernanceTestCase):
    @staticmethod
    def _toolset(calls: list[str]) -> FunctionToolset[None]:
        toolset: FunctionToolset[None] = FunctionToolset()

        @toolset.tool
        def query_warehouse(ctx: RunContext[None], sql: str = "select 1") -> int:
            """Run one read-only statement."""
            calls.append(sql)
            return 42

        return toolset

    @staticmethod
    def _sequence(*rounds: Any) -> Any:
        state = {"index": 0}

        async def stream(messages: list[ModelMessage], info: Any) -> Any:
            index = min(state["index"], len(rounds) - 1)
            state["index"] += 1
            for item in rounds[index]:
                yield item

        return stream

    def stored_turn(self, thread: ChatThread) -> ChatTurn:
        turn_id = uuid.uuid4()
        return ChatTurn.objects.create(
            id=turn_id,
            thread=thread,
            turn_index=0,
            pre_history_json="[]",
            activity={
                "version": 1,
                "turn_id": str(turn_id),
                "steps": [
                    {
                        "id": 0,
                        "kind": "wren_tool",
                        "status": "complete",
                        "title": "Assembling Tool Call: query_warehouse(...)",
                    }
                ],
                "truncated": False,
            },
            completed_at=datetime.now(UTC),
        )

    def bootstrap(self, user: User) -> dict:
        client = Client()
        client.force_login(user)
        page = client.get("/")
        match = BOOTSTRAP_PATTERN.search(page.content.decode())
        self.assertIsNotNone(match)
        return json.loads(match.group(1))

    def test_detail_is_private_to_native_superusers(self) -> None:
        staff = self.make_account("detail-staff", groups=(STAFF_GROUP,))
        lead = self.make_account("detail-lead", groups=(LEAD_GROUP,))
        app_admin = self.make_account("detail-admin", groups=(SUPERADMIN_GROUP,))
        operator = self.make_account("detail-operator", superuser=True)

        for user in (staff, lead, app_admin):
            with self.subTest(username=user.username):
                self.assertFalse(self.bootstrap(user)["show_reasoning_detail"])
        self.assertTrue(self.bootstrap(operator)["show_reasoning_detail"])

        thread = ChatThread.objects.create(
            owner=staff,
            history_json=plain_history("q", "a"),
            display=[
                {"role": "user", "content": "q"},
                {"role": "assistant", "content": "a"},
            ],
        )
        self.stored_turn(thread)
        detail = threads.thread_detail(thread, show_reasoning_detail=False)
        self.assertEqual(
            set(detail["turns"][0]["activity"]["steps"][0]),
            {"id", "kind", "status"},
        )
        trusted = threads.thread_detail(thread, show_reasoning_detail=True)
        self.assertIn("title", trusted["turns"][0]["activity"]["steps"][0])

    def test_each_turn_keeps_only_its_own_stored_activity(self) -> None:
        owner = self.make_account("isolation-owner", superuser=True)
        thread = ChatThread.objects.create(
            owner=owner,
            history_json=canonical(
                [
                    ModelRequest(parts=[UserPromptPart("first")]),
                    ModelResponse(
                        parts=[
                            ToolCallPart(
                                "query_warehouse",
                                {"sql": "select 'first-marker'"},
                                tool_call_id="c1",
                            ),
                            TextPart("one"),
                        ]
                    ),
                    ModelRequest(parts=[UserPromptPart("second")]),
                    ModelResponse(
                        parts=[
                            ToolCallPart(
                                "query_warehouse",
                                {"sql": "select 'second-marker'"},
                                tool_call_id="c2",
                            ),
                            TextPart("two"),
                        ]
                    ),
                    ModelRequest(parts=[UserPromptPart("third")]),
                    ModelResponse(
                        parts=[
                            ToolCallPart(
                                "query_warehouse",
                                {"sql": "select 'third-marker'"},
                                tool_call_id="c3",
                            ),
                            TextPart("three"),
                        ]
                    ),
                ]
            ),
            display=[
                {"role": "user", "content": "first"},
                {"role": "assistant", "content": "one"},
                {"role": "user", "content": "second"},
                {"role": "assistant", "content": "two"},
                {"role": "user", "content": "third"},
                {"role": "assistant", "content": "three"},
            ],
        )
        titled = self.stored_turn(thread)
        untitled_id = uuid.uuid4()
        ChatTurn.objects.create(
            id=untitled_id,
            thread=thread,
            turn_index=1,
            pre_history_json="[]",
            activity={
                "version": 1,
                "turn_id": str(untitled_id),
                "steps": [{"id": 0, "kind": "wren_tool", "status": "complete"}],
                "truncated": False,
            },
            completed_at=datetime.now(UTC),
        )
        mismatched = ChatTurn.objects.create(
            id=uuid.uuid4(),
            thread=thread,
            turn_index=2,
            pre_history_json="[]",
            activity={
                "version": 1,
                "turn_id": str(uuid.uuid4()),
                "steps": [
                    {
                        "id": 0,
                        "kind": "wren_tool",
                        "status": "complete",
                        "title": "Assembling Tool Call: query_warehouse(...)",
                    }
                ],
                "truncated": False,
            },
            completed_at=datetime.now(UTC),
        )

        detail = threads.thread_detail(thread, show_reasoning_detail=True)
        serialized = json.dumps(detail)
        self.assertNotIn("first-marker", serialized)
        self.assertNotIn("second-marker", serialized)
        self.assertNotIn("third-marker", serialized)
        turns = {entry["turn_id"]: entry["activity"] for entry in detail["turns"]}
        self.assertEqual(
            turns[str(titled.id)]["steps"][0]["title"],
            "Assembling Tool Call: query_warehouse(...)",
        )
        self.assertNotIn("title", turns[str(untitled_id)]["steps"][0])
        self.assertIsNone(turns[str(mismatched.id)])

    async def test_streamed_activity_hides_detail_from_staff(self) -> None:
        calls: list[str] = []
        self._patch_agent(
            self._sequence(
                [
                    {
                        0: DeltaToolCall(
                            name="query_warehouse",
                            json_args='{"sql":"select 42"}',
                            tool_call_id="call-1",
                        )
                    }
                ],
                ["There are 42 rows."],
            ),
            toolsets=(self._toolset(calls),),
        )
        staff = await sync_to_async(self.make_account)(
            "stream-staff", groups=(STAFF_GROUP,)
        )
        thread = await self.make_thread(staff)
        client = await self.client_for(staff)
        token = await self.token(client)
        body, parsed = await self.turn(client, token, thread.pk, "count", 0)
        self.assertNotIn(b"query_warehouse", body)
        self.assertNotIn(b"Assembling Tool Call", body)
        done = parsed[-1][1]
        for step in done["turn"]["activity"]["steps"]:
            self.assertNotIn("title", step)
            self.assertNotIn("duration_ms", step)
        self.assertEqual(calls, ["select 42"])

    def test_quarantined_history_never_reaches_a_model(self) -> None:
        owner = self.make_account("quarantine-owner")
        thread = ChatThread.objects.create(
            owner=owner,
            history_json=plain_history("secret", "answer"),
            display=[
                {"role": "user", "content": "secret"},
                {"role": "assistant", "content": "answer"},
            ],
            quarantined=True,
        )
        factory = self._echo_agent()
        client = Client()
        client.force_login(owner)
        response = client.post(
            f"/api/threads/{thread.pk}/messages/",
            data=json.dumps({"message": "hello", "revision": 0}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "history_quarantined")
        self.assertEqual(factory.seen, [])
        self.assertEqual(factory.agents, [])


class RecordingServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class WrenBoundaryTests(ChatGovernanceTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.received: list[bytes] = []

        class Handler(socketserver.BaseRequestHandler):
            def handle(inner) -> None:
                inner.request.settimeout(0.2)
                try:
                    self.received.append(inner.request.recv(64))
                except OSError:
                    return

        self.server = RecordingServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.shutdown)

    async def test_configured_wren_endpoint_is_never_contacted(self) -> None:
        self._echo_agent()
        user = await sync_to_async(self.make_account)(
            "canary-owner", groups=(STAFF_GROUP,)
        )
        thread = await self.make_thread(user)
        client = await self.client_for(user)
        token = await self.token(client)
        with override_settings(
            **{
                **PROVIDER_SETTINGS,
                "WREN_MCP_URL": f"http://127.0.0.1:{self.port}/mcp",
            }
        ):
            response = await self.send_turn(client, token, thread.pk, "hello", 0)
            self.assertEqual(response.status_code, 200)
            body = b"".join([chunk async for chunk in response.streaming_content])
            await sync_to_async(response.close)()
        self.assertIn(b"event: done", body)
        self.assertEqual(self.received, [])

    @staticmethod
    def dataset(code: str, display_name: str, physical_table: str) -> Dataset:
        return Dataset.objects.create(
            code=code,
            display_name=display_name,
            description="",
            source_key=configured_source_key(),
            project_key="jaffle-wren",
            model_name=code,
            catalog="jaffle_shop",
            schema="main",
            physical_table=physical_table,
            is_active=True,
            is_available=True,
            validation_status="metadata_discovered",
        )

    async def test_only_assigned_datasets_are_named_to_the_model(self) -> None:
        factory = self._echo_agent()
        user = await sync_to_async(self.make_account)(
            "grant-owner", groups=(STAFF_GROUP,)
        )
        department = await sync_to_async(Department.objects.create)(
            code="finance", name="Finance"
        )
        with override_settings(
            **{
                **PROVIDER_SETTINGS,
                "WREN_MCP_URL": f"http://127.0.0.1:{self.port}/mcp",
            }
        ):

            def seed() -> None:
                membership = DepartmentMembership.objects.create(
                    user=user, department=department, membership_role="staff"
                )
                assigned = self.dataset("orders", "Orders", "raw_orders_table")
                granted_only = self.dataset(
                    "customers", "Customers", "raw_customers_table"
                )
                MembershipDataset.objects.create(
                    membership=membership,
                    department_grant=DepartmentDataset.objects.create(
                        department=department, dataset=assigned
                    ),
                )
                DepartmentDataset.objects.create(
                    department=department, dataset=granted_only
                )

            await sync_to_async(seed)()
            client = await self.client_for(user)
            token = await self.token(client)
            created = await client.post(
                "/api/threads/",
                data=json.dumps({"department_id": str(department.pk)}),
                content_type="application/json",
                headers={"X-CSRFToken": token},
            )
            self.assertEqual(created.status_code, 201)
            thread_id = created.json()["thread"]["id"]
            response = await self.send_turn(client, token, thread_id, "hello", 0)
            self.assertEqual(response.status_code, 200)
            body = b"".join([chunk async for chunk in response.streaming_content])
            await sync_to_async(response.close)()

        self.assertIn(b"event: done", body)
        instructions = "\n".join(
            message.instructions
            for message in factory.seen[0]
            if isinstance(message, ModelRequest) and message.instructions
        )
        self.assertIn("Orders", instructions)
        self.assertIn("orders", instructions)
        self.assertNotIn("Customers", instructions)
        self.assertNotIn("raw_orders_table", instructions)
        self.assertNotIn("raw_customers_table", instructions)
        self.assertEqual(self.received, [])


class BootstrapGovernanceTests(ChatGovernanceTestCase):
    def test_bootstrap_carries_capabilities_memberships_and_management(self) -> None:
        lead = self.make_account("boot-lead", groups=(LEAD_GROUP, STAFF_GROUP))
        department = Department.objects.create(code="finance", name="Finance")
        DepartmentMembership.objects.create(
            user=lead, department=department, membership_role="lead"
        )
        client = Client()
        client.force_login(lead)
        page = client.get("/")
        match = BOOTSTRAP_PATTERN.search(page.content.decode())
        assert match is not None
        bootstrap = json.loads(match.group(1))
        self.assertFalse(bootstrap["analytics_enabled"])
        self.assertEqual(bootstrap["analytics_department_ids"], [])
        self.assertFalse(bootstrap["capabilities"]["manage_users"])
        self.assertTrue(bootstrap["capabilities"]["manage_department_staff"])
        self.assertEqual(bootstrap["memberships"][0]["department"]["code"], "finance")
        self.assertEqual(bootstrap["urls"]["management"], "/management/")
        self.assertNotIn("admin", bootstrap["urls"])

        staff = self.make_account("boot-staff", groups=(STAFF_GROUP,))
        staff_client = Client()
        staff_client.force_login(staff)
        staff_page = staff_client.get("/")
        staff_match = BOOTSTRAP_PATTERN.search(staff_page.content.decode())
        assert staff_match is not None
        staff_bootstrap = json.loads(staff_match.group(1))
        self.assertIsNone(staff_bootstrap["urls"]["management"])
        self.assertEqual(staff_bootstrap["memberships"], [])
        self.assertFalse(staff_bootstrap["analytics_enabled"])
        self.assertEqual(staff_bootstrap["analytics_department_ids"], [])

    def test_stale_governance_setting_still_reports_analytics_unavailable(self) -> None:
        user = self.make_account("stale-user", groups=(STAFF_GROUP,))
        client = Client()
        client.force_login(user)
        for enabled in (False, True):
            with (
                self.subTest(enabled=enabled),
                self.settings(AYCE_DATASET_GOVERNANCE_ENABLED=enabled),
            ):
                match = BOOTSTRAP_PATTERN.search(client.get("/").content.decode())
                self.assertIsNotNone(match)
                payload = json.loads(match.group(1))
                self.assertFalse(payload["analytics_enabled"])
                self.assertEqual(payload["analytics_department_ids"], [])
                me = client.get("/api/me/").json()
                self.assertFalse(me["analytics_enabled"])
                self.assertNotIn("analytics_department_ids", me)


class WorkspaceAnalyticsEligibilityTests(GovernanceTestCase):
    def test_sales_full_marketing_partial_and_no_grant_skip_wren(self) -> None:
        with (
            WrenFixture() as fixture,
            self.settings(
                DEBUG=True,
                AYCE_ANALYTICS_MODE="dev_jaffle",
                AYCE_DEV_JAFFLE_CONFIRMED=True,
                AYCE_DEV_WREN_PROJECT="jaffle-wren",
                WREN_MCP_URL=fixture.url,
                AYCE_DEV_WREN_MCP_URL=fixture.url,
            ),
        ):
            actor = self.make_account("catalog-root", superuser=True)
            async_to_sync(refresh_catalog)(actor_id=actor.pk, revision=self.revision())
            sales = self.make_department("sales", "Sales")
            marketing = self.make_department("marketing", "Marketing")
            customers = Dataset.objects.get(model_name="customers")
            orders = Dataset.objects.get(model_name="orders")
            for department in (sales, marketing):
                self.grant(department, customers)
                self.grant(department, orders)
            mixed = self.make_account("mixed-staff", groups=(STAFF_GROUP,))
            self.join(mixed, sales, datasets=(customers, orders))
            self.join(mixed, marketing, datasets=(orders,))
            partial = self.make_account("partial-staff", groups=(STAFF_GROUP,))
            self.join(partial, marketing, datasets=(orders,))
            bare = self.make_account("bare-staff", groups=(STAFF_GROUP,))
            self.join(bare, marketing)
            root = self.make_account("open-root", superuser=True)
            self.join(root, sales)
            calls = list(fixture.calls)
            self.assertTrue(calls)

            def workspace(user: User) -> dict:
                client = Client()
                client.force_login(user)
                page = client.get("/")
                self.assertEqual(page.status_code, 200)
                match = BOOTSTRAP_PATTERN.search(page.content.decode())
                self.assertIsNotNone(match)
                assert match is not None
                payload = json.loads(match.group(1))
                me = client.get("/api/me/").json()
                self.assertNotIn("analytics_department_ids", me)
                self.assertEqual(me["analytics_enabled"], payload["analytics_enabled"])
                return payload

            closed = workspace(bare)
            self.assertFalse(closed["analytics_enabled"])
            self.assertEqual(closed["analytics_department_ids"], [])

            opened = workspace(mixed)
            member_ids = {row["department"]["id"] for row in opened["memberships"]}
            self.assertEqual(member_ids, {str(sales.pk), str(marketing.pk)})
            self.assertTrue(opened["analytics_enabled"])
            self.assertEqual(opened["analytics_department_ids"], [str(sales.pk)])
            self.assertNotIn(str(marketing.pk), opened["analytics_department_ids"])

            marketing_only = workspace(partial)
            self.assertFalse(marketing_only["analytics_enabled"])
            self.assertEqual(marketing_only["analytics_department_ids"], [])

            unassigned = workspace(root)
            self.assertFalse(unassigned["analytics_enabled"])
            self.assertEqual(unassigned["analytics_department_ids"], [])

            again = workspace(bare)
            self.assertFalse(again["analytics_enabled"])
            self.assertEqual(again["analytics_department_ids"], [])
            self.assertEqual(fixture.calls, calls)


class HistoricalDatasetAccessTests(GovernanceTestCase):
    def test_other_department_grant_cannot_authorize_saved_rows(self) -> None:
        with WrenFixture() as fixture, self.settings(WREN_MCP_URL=fixture.url):
            actor = self.make_account("catalog-root", superuser=True)
            async_to_sync(refresh_catalog)(actor_id=actor.pk, revision=self.revision())
            user = self.make_account("analyst", groups=("AYCE Staff",))
            first = self.make_department("first")
            second = self.make_department("second")
            dataset = Dataset.objects.get(model_name="customers")
            membership = self.join(user, first, datasets=(dataset,))
            self.join(user, second, datasets=(dataset,))
            thread = ChatThread.objects.create(owner=user, department=first)
            turn = ChatTurn.objects.create(
                thread=thread, turn_index=0, pre_history_json="[]"
            )
            catalog = DatasetCatalogState.objects.get(pk=configured_source_key())
            provenance = ChatTurnDataset.objects.create(
                turn=turn,
                dataset=dataset,
                definition_sha256=dataset.definition_sha256,
                structure_sha256=catalog.structure_sha256,
            )
            self.assertEqual(
                threads.thread_access(owner_id=user.pk, thread=thread),
                threads.ACCESS_AVAILABLE,
            )
            membership.dataset_assignments.all().delete()
            self.assertEqual(
                threads.thread_access(owner_id=user.pk, thread=thread),
                threads.ACCESS_QUARANTINED,
            )
            self.assertFalse(
                threads._available_thread_pks(owner_id=user.pk)
                .filter(pk=thread.pk)
                .exists()
            )
            MembershipDataset.objects.create(
                membership=membership, department_grant=self.grant(first, dataset)
            )
            self.assertEqual(
                threads.thread_access(owner_id=user.pk, thread=thread),
                threads.ACCESS_AVAILABLE,
            )
            with self.settings(WREN_MCP_URL=""):
                self.assertEqual(
                    threads.thread_access(owner_id=user.pk, thread=thread),
                    threads.ACCESS_QUARANTINED,
                )
            provenance.structure_sha256 = ""
            provenance.save(update_fields=["structure_sha256"])
            self.assertEqual(
                threads.thread_access(owner_id=user.pk, thread=thread),
                threads.ACCESS_QUARANTINED,
            )
            provenance.structure_sha256 = catalog.structure_sha256
            provenance.save(update_fields=["structure_sha256"])
            self.assertEqual(
                threads.thread_access(owner_id=user.pk, thread=thread),
                threads.ACCESS_AVAILABLE,
            )
            MembershipDataset.objects.filter(membership__department=second).delete()
            self.assertEqual(
                threads.thread_access(owner_id=user.pk, thread=thread),
                threads.ACCESS_AVAILABLE,
            )
            self.assertFalse(thread.quarantined)
            threads.delete_thread(
                owner_id=user.pk, thread_id=thread.pk, revision=thread.revision
            )
            self.assertFalse(ChatThread.objects.filter(pk=thread.pk).exists())

    def test_revoked_assignment_redacts_saved_turn(self) -> None:
        with WrenFixture() as fixture, self.settings(WREN_MCP_URL=fixture.url):
            actor = self.make_account("catalog-root", superuser=True)
            async_to_sync(refresh_catalog)(actor_id=actor.pk, revision=self.revision())
            user = self.make_account("analyst", groups=("AYCE Staff",))
            department = self.make_department("first")
            customers = Dataset.objects.get(model_name="customers")
            membership = self.join(user, department, datasets=(customers,))
            catalog = DatasetCatalogState.objects.get(pk=configured_source_key())
            thread = ChatThread.objects.create(
                owner=user,
                department=department,
                title="Customers",
                history_json=plain_history(question="question", answer="answer"),
                display=[
                    {"role": "user", "content": "question"},
                    {"role": "assistant", "content": "answer"},
                ],
                revision=1,
            )
            turn = ChatTurn.objects.create(
                thread=thread, turn_index=0, pre_history_json="[]"
            )
            ChatTurnDataset.objects.create(
                turn=turn,
                dataset=customers,
                definition_sha256=customers.definition_sha256,
                structure_sha256=catalog.structure_sha256,
            )
            membership.dataset_assignments.filter(
                department_grant__dataset=customers
            ).delete()
            client = Client()
            client.force_login(user)
            detail = client.get(f"/api/threads/{thread.pk}/").json()["thread"]
            self.assertEqual(detail["access"], "quarantined")
            self.assertEqual(detail["display"], [])
            self.assertEqual(detail["turns"], [])
            self.assertEqual(
                client.get("/api/threads/").json()["threads"][0]["title"],
                "Restricted chat",
            )

    def test_provenance_bearing_turn_cannot_regenerate(self) -> None:
        with (
            WrenFixture() as fixture,
            self.settings(WREN_MCP_URL=fixture.url, **PROVIDER_SETTINGS),
        ):
            actor = self.make_account("catalog-root", superuser=True)
            async_to_sync(refresh_catalog)(actor_id=actor.pk, revision=self.revision())
            user = self.make_account("regen-user", groups=("AYCE Staff",))
            department = self.make_department("first")
            dataset = Dataset.objects.get(model_name="customers")
            self.join(user, department, datasets=(dataset,))
            catalog = DatasetCatalogState.objects.get(pk=configured_source_key())
            thread = ChatThread.objects.create(
                owner=user,
                department=department,
                history_json=plain_history("question", "answer"),
                display=[
                    {"role": "user", "content": "question"},
                    {"role": "assistant", "content": "answer"},
                ],
            )
            turn = ChatTurn.objects.create(
                thread=thread, turn_index=0, pre_history_json="[]"
            )
            provenance = ChatTurnDataset.objects.create(
                turn=turn,
                dataset=dataset,
                definition_sha256=dataset.definition_sha256,
                structure_sha256=catalog.structure_sha256,
            )
            revision_before = thread.revision
            client = Client()
            client.force_login(user)
            detail = client.get(f"/api/threads/{thread.pk}/").json()["thread"]
            self.assertTrue(detail["turns"][0]["can_delete"])
            self.assertFalse(detail["turns"][0]["can_regenerate"])

            response = client.post(
                f"/api/threads/{thread.pk}/turns/{turn.pk}/regenerate/",
                data=json.dumps({"revision": revision_before}),
                content_type="application/json",
            )
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.json()["error"]["code"], "warehouse_unavailable")

            token = threads.acquire_run(
                owner_id=user.pk, thread_id=thread.pk, revision=revision_before
            )
            thread.refresh_from_db()
            with self.assertRaises(service.ChatError) as caught:
                threads.commit_regeneration(
                    owner_id=user.pk,
                    snapshot=thread,
                    turn=turn,
                    completed=service.CompletedReply(
                        reply="replacement", history_json=thread.history_json
                    ),
                    run_token=token,
                    authorization_epoch=threads.current_epoch(),
                )
            self.assertEqual(caught.exception.code, "warehouse_unavailable")
            thread.refresh_from_db()
            self.assertEqual(thread.revision, revision_before)
            self.assertEqual(thread.display[1]["content"], "answer")
            self.assertTrue(ChatTurnDataset.objects.filter(pk=provenance.pk).exists())
