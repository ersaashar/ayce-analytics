"""Authenticated HTTP analytics against the loopback Wren fixture.

The provider is a FunctionModel. The adapter and MCP transport are real.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any
from unittest.mock import patch
from uuid import UUID

from asgiref.sync import async_to_sync, sync_to_async
from django.contrib.auth.models import User
from django.test import AsyncClient, override_settings
from pydantic_ai.messages import (
    ModelMessagesTypeAdapter,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import DeltaToolCall

from accounts import services as account_services
from chat import service
from chat.models import ChatThread, ChatTurn, ChatTurnDataset
from datasets import services as dataset_services
from datasets.discovery import configured_source_key
from datasets.models import Dataset, DatasetCatalogState
from tests.support import (
    PROVIDER_SETTINGS,
    THREADS_URL,
    ChatHttpTestCase,
    GovernanceTestCase,
    events,
    read_body,
)
from tests.wren_fixture import WrenFixture

SQL = "SELECT COUNT(*) AS total_orders FROM orders"
CALL_ID = "call-http-secret"
ANSWER_MARK = "ANSWER-7f3a-orders"
ROW_MARK = "ROWSECRET-http-7c2e"
ORDINARY = "ordinary department answer"
_DEV = {
    "DEBUG": True,
    "AYCE_ANALYTICS_MODE": "dev_jaffle",
    "AYCE_DEV_JAFFLE_CONFIRMED": True,
    "AYCE_DEV_WREN_PROJECT": "jaffle-wren",
}
COUNT_RESULT = {
    "columns": ["total_orders"],
    "rows": [{"total_orders": 99}],
    "row_count": 1,
    "truncated": False,
}
BLOCKED_RESULT = {
    "columns": ["note"],
    "rows": [{"note": ROW_MARK}],
    "row_count": 1,
    "truncated": False,
}


def _dev(url: str) -> dict[str, object]:
    return {**_DEV, "WREN_MCP_URL": url, "AYCE_DEV_WREN_MCP_URL": url}


def _payload(content: Any) -> Any:
    if isinstance(content, str):
        return json.loads(content)
    return content


def _args(part: ToolCallPart) -> dict[str, Any]:
    raw = part.args
    if isinstance(raw, str):
        loaded = json.loads(raw)
        return loaded if isinstance(loaded, dict) else {}
    if isinstance(raw, dict):
        return raw
    as_dict = getattr(part, "args_as_dict", None)
    if callable(as_dict):
        loaded = as_dict()
        return loaded if isinstance(loaded, dict) else {}
    return {}


def _parts(history_json: str, kind: type) -> list[Any]:
    history = ModelMessagesTypeAdapter.validate_json(history_json)
    return [
        part
        for message in history
        for part in getattr(message, "parts", [])
        if isinstance(part, kind)
    ]


def _tool_names(info: Any) -> set[str]:
    return {tool.name for tool in info.function_tools}


@override_settings(**PROVIDER_SETTINGS)
class AnalyticsHttpTests(ChatHttpTestCase, GovernanceTestCase):
    def setUp(self) -> None:
        ChatHttpTestCase.setUp(self)
        GovernanceTestCase.setUp(self)

    def _provision(self) -> dict[str, Any]:
        actor = self.make_account("catalog-root", superuser=True)
        async_to_sync(dataset_services.refresh_catalog)(
            actor_id=actor.pk, revision=self.revision()
        )
        key = configured_source_key()
        customers = Dataset.objects.get(model_name="customers", source_key=key)
        orders = Dataset.objects.get(model_name="orders", source_key=key)
        created = account_services.create_department(
            actor_id=actor.pk,
            revision=self.revision(),
            name="Jaffle Sales",
            description="",
        )
        sales_id = UUID(created["id"])
        dataset_services.replace_department_datasets(
            actor_id=actor.pk,
            revision=self.revision(),
            department_id=sales_id,
            dataset_ids=[customers.pk, orders.pk],
        )
        full = self._staff(actor.pk, "wren-full")
        partial = self._staff(actor.pk, "wren-partial")
        self._assign(actor.pk, full.pk, sales_id, (customers.pk, orders.pk))
        self._assign(actor.pk, partial.pk, sales_id, (orders.pk,))
        return {
            "actor": actor,
            "sales_id": sales_id,
            "customers": customers,
            "orders": orders,
            "full": full,
            "partial": partial,
        }

    def _staff(self, actor_id: int, username: str) -> User:
        created = account_services.create_user(
            actor_id=actor_id,
            revision=self.revision(),
            username=username,
            first_name="",
            last_name="",
            email=f"{username}@example.invalid",
            password="manage-pass-7f3a",
        )
        return User.objects.get(pk=created["id"])

    def _assign(
        self,
        actor_id: int,
        user_id: int,
        department_id: UUID,
        dataset_ids: tuple[UUID, ...],
    ) -> None:
        account_services.replace_user_memberships(
            actor_id=actor_id,
            revision=self.revision(),
            user_id=user_id,
            memberships=[
                account_services.MembershipInput(
                    department_id=department_id,
                    membership_role="staff",
                    is_active=True,
                    dataset_ids=dataset_ids,
                )
            ],
        )

    def _drop_customers(self, world: dict[str, Any]) -> None:
        dataset_services.replace_membership_datasets(
            actor_id=world["actor"].pk,
            revision=self.revision(),
            department_id=world["sales_id"],
            user_id=world["full"].pk,
            dataset_ids=[world["orders"].pk],
        )

    def _assert_idle(self, thread_id: Any) -> None:
        row = ChatThread.objects.get(pk=thread_id)
        self.assertIsNone(row.active_run_token)
        self.assertIsNone(row.active_run_expires_at)

    def _assert_provenance(self, thread_id: Any) -> None:
        rows = list(
            ChatTurnDataset.objects.filter(turn__thread_id=thread_id).select_related(
                "dataset"
            )
        )
        catalog = DatasetCatalogState.objects.get(pk=rows[0].dataset.source_key)
        self.assertEqual(
            sorted(row.dataset.model_name for row in rows),
            ["customers", "orders"],
        )
        for row in rows:
            self.assertEqual(row.definition_sha256, row.dataset.definition_sha256)
            self.assertEqual(row.structure_sha256, catalog.structure_sha256)
            self.assertTrue(row.definition_sha256)
            self.assertTrue(row.structure_sha256)

    async def _session(self, user: User) -> tuple[AsyncClient, str]:
        client = await self.client_for(user)
        return client, await self.token(client)

    async def _open(self, client: AsyncClient, token: str, department_id: UUID) -> str:
        response = await client.post(
            THREADS_URL,
            data=json.dumps({"department_id": str(department_id)}),
            content_type="application/json",
            headers={"X-CSRFToken": token},
        )
        self.assertEqual(response.status_code, 201, response.content)
        return response.json()["thread"]["id"]

    def _stream(self, echo_result: bool) -> Any:
        async def stream(messages: list[Any], info: Any) -> Any:
            returned = [
                part
                for message in messages
                for part in getattr(message, "parts", [])
                if isinstance(part, ToolReturnPart)
            ]
            if "run_sql" in _tool_names(info) and not returned:
                yield {
                    0: DeltaToolCall(
                        name="run_sql",
                        json_args=json.dumps({"sql": SQL}),
                        tool_call_id=CALL_ID,
                    )
                }
                return
            if returned and echo_result:
                payload = _payload(returned[-1].content)
                count = payload["rows"][0]["total_orders"]
                yield f"{ANSWER_MARK} {count}"
                return
            if returned:
                yield f"LEAKED {returned[-1].content}"
                return
            yield ORDINARY

        return stream

    async def _turn(
        self,
        client: AsyncClient,
        token: str,
        thread_id: str,
        revision: int,
    ) -> tuple[bytes, list[tuple[str, Any]]]:
        with patch.object(service, "STREAM_DEBOUNCE_SECONDS", None):
            return await self.turn(
                client, token, thread_id, "How many orders are there?", revision
            )

    def _public(self, body: bytes) -> None:
        text = body.decode("utf-8")
        self.assertTrue(text.startswith(": connected\n\n"))
        self.assertNotIn(SQL, text)
        self.assertNotIn(CALL_ID, text)
        self.assertNotIn("127.0.0.1", text)
        self.assertNotIn("analytics_scope", text)

    async def test_full_turn_commits_tool_call_and_both_provenance_rows(self) -> None:
        factory = self._patch_agent(self._stream(echo_result=True))
        with WrenFixture(analytics=True, query_result=COUNT_RESULT) as fixture:  # noqa: SIM117
            with self.settings(**_dev(fixture.url)):
                world = await sync_to_async(self._provision)()
                client, token = await self._session(world["full"])
                thread_id = await self._open(client, token, world["sales_id"])
                body, parsed = await self._turn(client, token, thread_id, 0)

        self._public(body)
        names = [name for name, _payload in parsed]
        self.assertEqual(names.count("done"), 1)
        self.assertNotIn("error", names)
        self.assertEqual(parsed[-1][0], "done")
        done = parsed[-1][1]
        self.assertEqual(done["reply"], f"{ANSWER_MARK} 99")
        self.assertEqual(done["thread"]["revision"], 1)
        self.assertFalse(done["turn"]["can_regenerate"])
        self.assertTrue(done["turn"]["can_delete"])
        steps = [
            (step["kind"], step["status"]) for step in done["turn"]["activity"]["steps"]
        ]
        self.assertIn(("wren_tool", "complete"), steps)
        for step in done["turn"]["activity"]["steps"]:
            self.assertEqual(set(step), {"id", "kind", "status"})
        activity = [
            (frame["step"]["kind"], frame["step"]["status"])
            for name, frame in events(body)
            if name == "activity" and frame["step"] is not None
        ]
        self.assertIn(("wren_tool", "running"), activity)
        self.assertIn(("wren_tool", "complete"), activity)

        def stored() -> None:
            row = ChatThread.objects.get(pk=thread_id)
            self.assertEqual(row.revision, 1)
            self.assertEqual(row.display[-1]["content"], f"{ANSWER_MARK} 99")
            self._assert_idle(thread_id)
            self.assertEqual(ChatTurn.objects.filter(thread_id=thread_id).count(), 1)
            self._assert_provenance(thread_id)
            calls = _parts(row.history_json, ToolCallPart)
            returns = _parts(row.history_json, ToolReturnPart)
            self.assertEqual([part.tool_name for part in calls], ["run_sql"])
            self.assertEqual(_args(calls[0]).get("sql"), SQL)
            self.assertEqual(calls[0].tool_call_id, CALL_ID)
            self.assertEqual([part.tool_call_id for part in returns], [CALL_ID])
            self.assertEqual(_payload(returns[0].content), COUNT_RESULT)

        await sync_to_async(stored)()
        self.assertEqual(fixture.queries, [(SQL, 100)])
        self.assertTrue(factory.attached)
        self.assertTrue(factory.attached[0])

    async def test_partial_grant_answers_without_a_native_query(self) -> None:
        factory = self._patch_agent(self._stream(echo_result=True))
        with WrenFixture(analytics=True, query_result=COUNT_RESULT) as fixture:  # noqa: SIM117
            with self.settings(**_dev(fixture.url)):
                world = await sync_to_async(self._provision)()
                baseline = list(fixture.calls)
                client, token = await self._session(world["partial"])
                thread_id = await self._open(client, token, world["sales_id"])
                body, parsed = await self._turn(client, token, thread_id, 0)
                self.assertEqual(fixture.calls, baseline)
                self.assertEqual(fixture.queries, [])

        self.assertEqual(parsed[-1][0], "done")
        self.assertEqual(parsed[-1][1]["reply"], ORDINARY)
        self.assertTrue(parsed[-1][1]["turn"]["can_regenerate"])
        self.assertNotIn(ANSWER_MARK, body.decode("utf-8"))
        self.assertNotIn("run_sql", json.dumps(factory.seen, default=str))
        self.assertEqual(factory.attached, [()])

        def stored() -> None:
            self._assert_idle(thread_id)
            self.assertEqual(ChatThread.objects.get(pk=thread_id).revision, 1)
            self.assertFalse(
                ChatTurnDataset.objects.filter(turn__thread_id=thread_id).exists()
            )

        await sync_to_async(stored)()

    async def test_revocation_during_blocked_query_commits_nothing(self) -> None:
        factory = self._patch_agent(self._stream(echo_result=False))
        with WrenFixture(analytics=True, query_result=BLOCKED_RESULT) as fixture:
            fixture.release.clear()
            with self.settings(**_dev(fixture.url)):
                world = await sync_to_async(self._provision)()
                client, token = await self._session(world["full"])
                thread_id = await self._open(client, token, world["sales_id"])
                with patch.object(service, "STREAM_DEBOUNCE_SECONDS", None):
                    response = await self.send_turn(
                        client,
                        token,
                        thread_id,
                        "How many orders are there?",
                        0,
                    )
                body_task = asyncio.create_task(read_body(response))
                started = False
                try:
                    started = await asyncio.to_thread(fixture.query_started.wait, 20)
                    if started:
                        await sync_to_async(self._drop_customers)(world)
                finally:
                    fixture.release.set()
                    body = await asyncio.wait_for(body_task, 30)
                    await sync_to_async(response.close)()
                self.assertTrue(started)

        parsed = events(body)
        text = body.decode("utf-8")
        self.assertTrue(text.startswith(": connected\n\n"))
        self.assertEqual(parsed[-1][0], "error")
        self.assertEqual(parsed[-1][1]["error"]["code"], "authorization_changed")
        self.assertNotIn("done", [name for name, _payload in parsed])
        self.assertNotIn(ROW_MARK, text)
        self.assertNotIn(SQL, text)
        self.assertNotIn(CALL_ID, text)
        self.assertNotIn(fixture.url, text)
        self.assertNotIn(ROW_MARK, json.dumps(factory.seen, default=str))
        self.assertEqual(fixture.queries, [(SQL, 100)])

        def stored() -> None:
            row = ChatThread.objects.get(pk=thread_id)
            self.assertEqual(row.revision, 0)
            self.assertEqual(row.display, [])
            self._assert_idle(thread_id)
            self.assertFalse(ChatTurn.objects.filter(thread_id=thread_id).exists())
            self.assertFalse(
                ChatTurnDataset.objects.filter(turn__thread_id=thread_id).exists()
            )

        await sync_to_async(stored)()

    async def test_history_lock_regeneration_refusal_and_deletion(self) -> None:
        self._patch_agent(self._stream(echo_result=True))
        with WrenFixture(analytics=True, query_result=COUNT_RESULT) as fixture:  # noqa: SIM117
            with self.settings(**_dev(fixture.url)):
                world = await sync_to_async(self._provision)()
                client, token = await self._session(world["full"])
                thread_id = await self._open(client, token, world["sales_id"])
                _body, parsed = await self._turn(client, token, thread_id, 0)
                self.assertEqual(parsed[-1][0], "done")
                turn_id = parsed[-1][1]["turn"]["turn_id"]
                queries = list(fixture.queries)

                refused = await self.regenerate(client, token, thread_id, turn_id, 1)
                self.assertEqual(refused.status_code, 503)
                self.assertEqual(self.error_code(refused), "warehouse_unavailable")
                self.assertEqual(fixture.queries, queries)

                deleted = await self.delete_exchange(
                    client, token, thread_id, turn_id, 1
                )
                self.assertEqual(deleted.status_code, 200)
                removed = deleted.json()["thread"]
                self.assertEqual(removed["revision"], 2)
                self.assertEqual(removed["display"], [])
                self.assertEqual(removed["turns"], [])

                def restored() -> None:
                    row = ChatThread.objects.get(pk=thread_id)
                    self.assertEqual(row.history_json, "[]")
                    self.assertEqual(row.display, [])
                    self._assert_idle(thread_id)
                    self.assertFalse(ChatTurn.objects.filter(pk=turn_id).exists())
                    self.assertFalse(
                        ChatTurnDataset.objects.filter(turn_id=turn_id).exists()
                    )

                await sync_to_async(restored)()

                _body, parsed = await self._turn(client, token, thread_id, 2)
                self.assertEqual(parsed[-1][0], "done")
                locked_mark = parsed[-1][1]["reply"]
                await sync_to_async(self._drop_customers)(world)
                after_queries = list(fixture.queries)

                detail = await self.detail(client, thread_id)
                listed = await self.summary_list(client)
                page = await client.get(f"/chat/{thread_id}/")
                blocked = await self.send_turn(
                    client, token, thread_id, "ask again", detail["revision"]
                )

        self.assertEqual(detail["access"], "quarantined")
        self.assertEqual(detail["title"], "Restricted chat")
        self.assertEqual(detail["display"], [])
        self.assertEqual(detail["turns"], [])
        self.assertNotIn(locked_mark, json.dumps(detail))
        self.assertEqual(listed[0]["title"], "Restricted chat")
        self.assertEqual(page.status_code, 200)
        html = page.content.decode()
        self.assertNotIn(locked_mark, html)
        self.assertNotIn(SQL, html)
        self.assertIn("Restricted chat", html)
        self.assertEqual(blocked.status_code, 403)
        self.assertEqual(self.error_code(blocked), "history_quarantined")
        self.assertEqual(fixture.queries, after_queries)

        def stored() -> None:
            self._assert_idle(thread_id)
            row = ChatThread.objects.get(pk=thread_id)
            self.assertIn(locked_mark, row.display[-1]["content"])
            self.assertTrue(
                ChatTurnDataset.objects.filter(turn__thread_id=thread_id).exists()
            )

        await sync_to_async(stored)()
