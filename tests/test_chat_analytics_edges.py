"""Checkpoint-2 edges the HTTP suite does not cover.

Real loopback MCP fixture. No mocked native transport.
"""

from __future__ import annotations

import asyncio
import json
import threading
from typing import Any
from unittest.mock import patch
from uuid import uuid4

from asgiref.sync import sync_to_async
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import AsyncClient, override_settings
from pydantic_ai.messages import (
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import DeltaToolCall

from accounts import services as account_services
from chat import mcp_policy, service, threads, views
from chat.mcp_policy import DevScope
from chat.models import ChatAttachment, ChatThread, ChatTurn, ChatTurnDataset
from datasets import services as dataset_services
from datasets.discovery import configured_source_key
from datasets.models import DatasetCatalogState
from tests.support import (
    PROVIDER_SETTINGS,
    ChatHttpTestCase,
    GovernanceTestCase,
    events,
    read_body,
)
from tests.test_chat_analytics_http import (
    ANSWER_MARK,
    CALL_ID,
    COUNT_RESULT,
    ORDINARY,
    SQL,
    AnalyticsHttpTests,
    _dev,
    _tool_names,
)
from tests.wren_fixture import WrenFixture

SECRET = "POST-REVOKE-7c2e"
META_CALL = "call-meta-secret"
PREAMBLE = "PRE-REVOKE"
PHYSICAL = "jaffle_shop"


def _history(question: str, answer: str) -> str:
    return ModelMessagesTypeAdapter.dump_json(
        [
            ModelRequest(parts=[UserPromptPart(question)]),
            ModelResponse(parts=[TextPart(answer)]),
        ]
    ).decode("utf-8")


def _returned(messages: list[Any]) -> list[ToolReturnPart]:
    return [
        part
        for message in messages
        for part in getattr(message, "parts", [])
        if isinstance(part, ToolReturnPart)
    ]


def _call(name: str, args: dict[str, Any], call_id: str) -> dict[int, DeltaToolCall]:
    return {
        0: DeltaToolCall(
            name=name,
            json_args=json.dumps(args),
            tool_call_id=call_id,
        )
    }


class _Gate:
    def __init__(self) -> None:
        self.entered = threading.Event()
        self.release = threading.Event()

    def pause(self) -> None:
        self.entered.set()
        if not self.release.wait(20):
            raise TimeoutError("analytics edge barrier timed out")


@override_settings(**PROVIDER_SETTINGS)
class AnalyticsEdgeTests(ChatHttpTestCase, GovernanceTestCase):
    setUp = AnalyticsHttpTests.setUp
    _provision = AnalyticsHttpTests._provision
    _staff = AnalyticsHttpTests._staff
    _assign = AnalyticsHttpTests._assign
    _drop_customers = AnalyticsHttpTests._drop_customers
    _assert_idle = AnalyticsHttpTests._assert_idle
    _assert_provenance = AnalyticsHttpTests._assert_provenance
    _session = AnalyticsHttpTests._session
    _open = AnalyticsHttpTests._open
    _stream = AnalyticsHttpTests._stream
    _public = AnalyticsHttpTests._public

    def _assert_uncommitted(self, thread_id: Any) -> None:
        row = ChatThread.objects.get(pk=thread_id)
        self.assertEqual(row.revision, 0)
        self.assertEqual(row.display, [])
        self.assertNotIn(SECRET, json.dumps(row.display))
        self._assert_idle(thread_id)
        self.assertFalse(ChatTurn.objects.filter(thread_id=thread_id).exists())
        self.assertFalse(
            ChatTurnDataset.objects.filter(turn__thread_id=thread_id).exists()
        )
        self.assertFalse(
            ChatAttachment.objects.filter(turn__thread_id=thread_id).exists()
        )

    def _assert_error(self, body: bytes, code: str) -> list[tuple[str, Any]]:
        parsed = events(body)
        names = [name for name, _payload in parsed]
        self.assertEqual(parsed[-1][0], "error", parsed)
        self.assertEqual(parsed[-1][1]["error"]["code"], code)
        self.assertNotIn("done", names)
        self._public(body)
        self.assertNotIn(SECRET, body.decode("utf-8"))
        return parsed

    async def _hold_commit(
        self,
        client: AsyncClient,
        token: str,
        thread_id: str,
        mutate,
    ) -> bytes:
        real = threads.commit_turn

        def pausing(**kwargs: Any) -> ChatThread:
            mutate()
            return real(**kwargs)

        with (
            patch.object(service, "STREAM_DEBOUNCE_SECONDS", None),
            patch("chat.threads.commit_turn", pausing),
        ):
            response = await self.send_turn(
                client, token, thread_id, "How many orders are there?", 0
            )
            body = await asyncio.wait_for(read_body(response), 30)
            await sync_to_async(response.close)()
        return body

    async def test_revocation_after_tool_success_withholds_the_answer(self) -> None:
        ready = asyncio.Event()
        resume = asyncio.Event()

        async def stream(messages: list[Any], info: Any) -> Any:
            if "run_sql" in _tool_names(info) and not _returned(messages):
                yield _call("run_sql", {"sql": SQL}, CALL_ID)
                return
            if _returned(messages):
                ready.set()
                await resume.wait()
                yield SECRET
                return
            yield ORDINARY

        self._patch_agent(stream)
        with WrenFixture(  # noqa: SIM117
            analytics=True,
            query_result=COUNT_RESULT,
        ) as fixture:
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
                try:
                    await asyncio.wait_for(ready.wait(), 20)
                    await sync_to_async(self._drop_customers)(world)
                finally:
                    resume.set()
                    body = await asyncio.wait_for(body_task, 30)
                    await sync_to_async(response.close)()

        self._assert_error(body, "authorization_changed")
        self.assertEqual(fixture.queries, [(SQL, 100)])
        await sync_to_async(self._assert_uncommitted)(thread_id)

    async def test_revocation_during_answer_withholds_later_text(self) -> None:
        ready = asyncio.Event()
        resume = asyncio.Event()

        async def stream(messages: list[Any], info: Any) -> Any:
            if "run_sql" in _tool_names(info) and not _returned(messages):
                yield _call("run_sql", {"sql": SQL}, CALL_ID)
                return
            if _returned(messages):
                yield PREAMBLE
                ready.set()
                await resume.wait()
                yield SECRET
                return
            yield ORDINARY

        self._patch_agent(stream)
        with WrenFixture(  # noqa: SIM117
            analytics=True,
            query_result=COUNT_RESULT,
        ) as fixture:
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
                try:
                    await asyncio.wait_for(ready.wait(), 20)
                    await sync_to_async(self._drop_customers)(world)
                finally:
                    resume.set()
                    body = await asyncio.wait_for(body_task, 30)
                    await sync_to_async(response.close)()

        self._assert_error(body, "authorization_changed")
        self.assertEqual(fixture.queries, [(SQL, 100)])
        await sync_to_async(self._assert_uncommitted)(thread_id)

    async def test_revocation_before_commit_rolls_the_turn_back(self) -> None:
        self._patch_agent(self._stream(echo_result=True))
        with WrenFixture(  # noqa: SIM117
            analytics=True,
            query_result=COUNT_RESULT,
        ) as fixture:
            with self.settings(**_dev(fixture.url)):
                world = await sync_to_async(self._provision)()
                client, token = await self._session(world["full"])
                thread_id = await self._open(client, token, world["sales_id"])
                body = await self._hold_commit(
                    client, token, thread_id, lambda: self._drop_customers(world)
                )

        self._assert_error(body, "authorization_changed")
        self.assertNotIn("done", [name for name, _payload in events(body)])
        self.assertEqual(fixture.queries, [(SQL, 100)])
        await sync_to_async(self._assert_uncommitted)(thread_id)

    async def test_catalog_change_before_commit_rolls_the_turn_back(self) -> None:
        self._patch_agent(self._stream(echo_result=True))

        def break_structure() -> None:
            catalog = DatasetCatalogState.objects.get(pk=configured_source_key())
            catalog.structure_sha256 = "d" * 64
            catalog.save(update_fields=["structure_sha256"])

        with WrenFixture(  # noqa: SIM117
            analytics=True,
            query_result=COUNT_RESULT,
        ) as fixture:
            with self.settings(**_dev(fixture.url)):
                world = await sync_to_async(self._provision)()
                client, token = await self._session(world["full"])
                thread_id = await self._open(client, token, world["sales_id"])
                body = await self._hold_commit(
                    client, token, thread_id, break_structure
                )

        self._assert_error(body, "authorization_changed")
        self.assertEqual(fixture.queries, [(SQL, 100)])
        await sync_to_async(self._assert_uncommitted)(thread_id)

    async def test_disabled_dataset_before_commit_rolls_the_turn_back(self) -> None:
        self._patch_agent(self._stream(echo_result=True))

        def disable_orders(world: dict[str, Any]) -> None:
            dataset_services.update_dataset(
                actor_id=world["actor"].pk,
                revision=self.revision(),
                dataset_id=world["orders"].pk,
                changes={"is_active": False},
            )

        with WrenFixture(  # noqa: SIM117
            analytics=True,
            query_result=COUNT_RESULT,
        ) as fixture:
            with self.settings(**_dev(fixture.url)):
                world = await sync_to_async(self._provision)()
                client, token = await self._session(world["full"])
                thread_id = await self._open(client, token, world["sales_id"])
                body = await self._hold_commit(
                    client, token, thread_id, lambda: disable_orders(world)
                )

        self._assert_error(body, "authorization_changed")
        await sync_to_async(self._assert_uncommitted)(thread_id)

    async def test_department_grant_loss_before_commit_rolls_the_turn_back(
        self,
    ) -> None:
        self._patch_agent(self._stream(echo_result=True))

        def drop_grant(world: dict[str, Any]) -> None:
            dataset_services.replace_department_datasets(
                actor_id=world["actor"].pk,
                revision=self.revision(),
                department_id=world["sales_id"],
                dataset_ids=[world["orders"].pk],
            )

        with WrenFixture(  # noqa: SIM117
            analytics=True,
            query_result=COUNT_RESULT,
        ) as fixture:
            with self.settings(**_dev(fixture.url)):
                world = await sync_to_async(self._provision)()
                client, token = await self._session(world["full"])
                thread_id = await self._open(client, token, world["sales_id"])
                body = await self._hold_commit(
                    client, token, thread_id, lambda: drop_grant(world)
                )

        self._assert_error(body, "authorization_changed")
        await sync_to_async(self._assert_uncommitted)(thread_id)

    async def test_membership_removal_before_commit_rolls_back(self) -> None:
        self._patch_agent(self._stream(echo_result=True))
        with WrenFixture(analytics=True, query_result=COUNT_RESULT) as fixture:  # noqa: SIM117
            with self.settings(**_dev(fixture.url)):
                world = await sync_to_async(self._provision)()
                client, token = await self._session(world["full"])
                thread_id = await self._open(client, token, world["sales_id"])

                def revoke() -> None:
                    account_services.replace_user_memberships(
                        actor_id=world["actor"].pk,
                        revision=self.revision(),
                        user_id=world["full"].pk,
                        memberships=[],
                    )

                body = await self._hold_commit(client, token, thread_id, revoke)
        self.assertNotIn("done", [name for name, _ in events(body)])
        self.assertEqual(fixture.queries, [(SQL, 100)])
        await sync_to_async(self._assert_uncommitted)(thread_id)

    async def test_user_deactivation_before_commit_rolls_back(self) -> None:
        self._patch_agent(self._stream(echo_result=True))
        with WrenFixture(analytics=True, query_result=COUNT_RESULT) as fixture:  # noqa: SIM117
            with self.settings(**_dev(fixture.url)):
                world = await sync_to_async(self._provision)()
                client, token = await self._session(world["full"])
                thread_id = await self._open(client, token, world["sales_id"])

                def deactivate() -> None:
                    account_services.update_user(
                        actor_id=world["actor"].pk,
                        revision=self.revision(),
                        user_id=world["full"].pk,
                        changes={"is_active": False},
                    )

                body = await self._hold_commit(client, token, thread_id, deactivate)
        self.assertNotIn("done", [name for name, _ in events(body)])
        self.assertEqual(fixture.queries, [(SQL, 100)])
        await sync_to_async(self._assert_uncommitted)(thread_id)

    async def test_revocation_after_commit_withholds_done(self) -> None:
        self._patch_agent(self._stream(echo_result=True))
        real_done = views._available_done
        world: dict[str, Any] = {}

        def pausing(**kwargs: Any) -> Any:
            self._drop_customers(world)
            return real_done(**kwargs)

        with WrenFixture(  # noqa: SIM117
            analytics=True,
            query_result=COUNT_RESULT,
        ) as fixture:
            with self.settings(**_dev(fixture.url)):
                world.update(await sync_to_async(self._provision)())
                client, token = await self._session(world["full"])
                thread_id = await self._open(client, token, world["sales_id"])
                with (
                    patch.object(service, "STREAM_DEBOUNCE_SECONDS", None),
                    patch("chat.views._available_done", pausing),
                ):
                    response = await self.send_turn(
                        client,
                        token,
                        thread_id,
                        "How many orders are there?",
                        0,
                    )
                    body = await asyncio.wait_for(read_body(response), 30)
                    await sync_to_async(response.close)()
                detail = await self.detail(client, thread_id)

        parsed = events(body)
        self.assertEqual(parsed[-1][0], "error")
        self.assertEqual(parsed[-1][1]["error"]["code"], "history_quarantined")
        self.assertNotIn("done", [name for name, _payload in parsed])
        self.assertNotIn(ANSWER_MARK, json.dumps(detail))
        self.assertEqual(detail["access"], "quarantined")
        self.assertEqual(detail["title"], "Restricted chat")
        self.assertEqual(detail["display"], [])

        def stored() -> None:
            row = ChatThread.objects.get(pk=thread_id)
            self.assertEqual(row.revision, 1)
            self.assertIn(ANSWER_MARK, row.display[-1]["content"])
            self._assert_idle(thread_id)
            self._assert_provenance(thread_id)

        await sync_to_async(stored)()
        self.assertEqual(fixture.queries, [(SQL, 100)])

    async def test_regeneration_refuses_a_used_scope(self) -> None:
        with WrenFixture(analytics=True) as fixture, self.settings(**_dev(fixture.url)):
            world = await sync_to_async(self._provision)()

            def refuse() -> None:
                scope = mcp_policy.resolve_dev_scope(
                    user_id=world["full"].pk,
                    department_id=world["sales_id"],
                )
                self.assertIsNotNone(scope)
                thread = threads.create_thread(
                    owner_id=world["full"].pk,
                    department_id=world["sales_id"],
                )
                epoch = threads.current_epoch()
                token = threads.acquire_run(
                    owner_id=world["full"].pk,
                    thread_id=thread.pk,
                    revision=0,
                )
                committed = threads.commit_turn(
                    owner_id=world["full"].pk,
                    snapshot=thread,
                    message="question",
                    completed=service.CompletedReply(
                        reply="ordinary answer",
                        history_json=_history("question", "ordinary answer"),
                    ),
                    turn_id=uuid4(),
                    run_token=token,
                    authorization_epoch=epoch,
                )
                turn = ChatTurn.objects.get(thread=committed)
                regen = threads.acquire_run(
                    owner_id=world["full"].pk,
                    thread_id=committed.pk,
                    revision=committed.revision,
                )
                with self.assertRaises(service.ChatError) as caught:
                    threads.commit_regeneration(
                        owner_id=world["full"].pk,
                        snapshot=committed,
                        turn=turn,
                        completed=service.CompletedReply(
                            reply=SECRET,
                            history_json=_history("question", SECRET),
                            analytics_scope=scope,
                        ),
                        run_token=regen,
                        authorization_epoch=epoch,
                    )
                self.assertEqual(caught.exception.code, "warehouse_unavailable")
                threads.release_run(
                    owner_id=world["full"].pk,
                    thread_id=committed.pk,
                    run_token=regen,
                )
                committed.refresh_from_db()
                self.assertEqual(committed.revision, 1)
                self.assertEqual(committed.display[-1]["content"], "ordinary answer")
                self.assertNotIn(SECRET, json.dumps(committed.display))
                self.assertFalse(ChatTurnDataset.objects.filter(turn=turn).exists())
                self.assertIsNone(committed.active_run_token)

            await sync_to_async(refuse)()
        self.assertEqual(fixture.queries, [])

    async def test_ordinary_regeneration_stays_tool_free(self) -> None:
        step = {"n": 0}

        async def stream(messages: list[Any], info: Any) -> Any:
            step["n"] += 1
            if step["n"] > 1 and "run_sql" in _tool_names(info):
                yield _call("run_sql", {"sql": SQL}, CALL_ID)
                return
            if step["n"] > 1 and _returned(messages):
                yield SECRET
                return
            yield ORDINARY

        factory = self._patch_agent(stream)
        with WrenFixture(  # noqa: SIM117
            analytics=True,
            query_result=COUNT_RESULT,
        ) as fixture:
            with self.settings(**_dev(fixture.url)):
                world = await sync_to_async(self._provision)()
                client, token = await self._session(world["full"])
                thread_id = await self._open(client, token, world["sales_id"])
                _body, parsed = await self.turn(
                    client, token, thread_id, "How many orders are there?", 0
                )
                self.assertEqual(parsed[-1][0], "done")
                self.assertEqual(parsed[-1][1]["reply"], ORDINARY)
                self.assertTrue(parsed[-1][1]["turn"]["can_regenerate"])
                turn_id = parsed[-1][1]["turn"]["turn_id"]
                _regen_body, regenerated = await self.regenerated(
                    client, token, thread_id, turn_id, 1
                )

        self.assertEqual(regenerated[-1][0], "done")
        self.assertEqual(regenerated[-1][1]["reply"], ORDINARY)
        self.assertNotIn(SECRET, _regen_body.decode("utf-8"))
        self.assertEqual(fixture.queries, [])
        self.assertNotEqual(factory.attached[0], ())
        self.assertEqual(factory.attached[1], ())

        def stored() -> None:
            self.assertFalse(
                ChatTurnDataset.objects.filter(turn__thread_id=thread_id).exists()
            )
            self._assert_idle(thread_id)

        await sync_to_async(stored)()

    async def test_empty_scope_cannot_commit_as_ordinary_history(self) -> None:
        with WrenFixture(analytics=True) as fixture, self.settings(**_dev(fixture.url)):
            world = await sync_to_async(self._provision)()

            def commit_empty() -> None:
                thread = threads.create_thread(
                    owner_id=world["full"].pk,
                    department_id=world["sales_id"],
                )
                token = threads.acquire_run(
                    owner_id=world["full"].pk,
                    thread_id=thread.pk,
                    revision=0,
                )
                empty = DevScope(
                    endpoint=fixture.url,
                    source_key=configured_source_key(),
                    project_key="jaffle-wren",
                    snapshot_sha256="a" * 64,
                    structure_sha256="b" * 64,
                    datasets=(),
                )
                with self.assertRaises(service.ChatError) as caught:
                    threads.commit_turn(
                        owner_id=world["full"].pk,
                        snapshot=thread,
                        message="question",
                        completed=service.CompletedReply(
                            reply=SECRET,
                            history_json=_history("question", SECRET),
                            analytics_scope=empty,
                        ),
                        turn_id=uuid4(),
                        run_token=token,
                        authorization_epoch=threads.current_epoch(),
                    )
                self.assertEqual(caught.exception.code, "authorization_changed")
                threads.release_run(
                    owner_id=world["full"].pk,
                    thread_id=thread.pk,
                    run_token=token,
                )
                thread.refresh_from_db()
                self.assertEqual(thread.revision, 0)
                self.assertEqual(thread.display, [])
                self.assertIsNone(thread.active_run_token)
                self.assertFalse(ChatTurn.objects.filter(thread=thread).exists())
                self.assertFalse(ChatTurnDataset.objects.exists())

            await sync_to_async(commit_empty)()

    async def test_attachment_failure_after_provenance_rolls_back(self) -> None:
        self._patch_agent(self._stream(echo_result=True))
        inserted: list[list[str]] = []
        real = ChatAttachment.objects.bulk_create

        def fail_after(rows: Any, *args: Any, **kwargs: Any) -> Any:
            real(rows, *args, **kwargs)
            inserted.append(
                sorted(
                    ChatTurnDataset.objects.values_list(
                        "dataset__model_name", flat=True
                    )
                )
            )
            raise RuntimeError("insert failed")

        with WrenFixture(  # noqa: SIM117
            analytics=True,
            query_result=COUNT_RESULT,
        ) as fixture:
            with self.settings(**_dev(fixture.url)):
                world = await sync_to_async(self._provision)()
                client, token = await self._session(world["full"])
                thread_id = await self._open(client, token, world["sales_id"])
                with patch(
                    "chat.threads.ChatAttachment.objects.bulk_create",
                    fail_after,
                ):
                    response = await self.send_files(
                        client,
                        token,
                        thread_id,
                        "How many orders are there?",
                        0,
                        [SimpleUploadedFile("a.txt", b"edge-file")],
                    )
                    with self.assertRaises(RuntimeError):
                        await read_body(response)
                    await sync_to_async(response.close)()

        self.assertEqual(inserted, [["customers", "orders"]])
        self.assertEqual(fixture.queries, [(SQL, 100)])
        await sync_to_async(self._assert_uncommitted)(thread_id)
        self.assertEqual(await sync_to_async(ChatAttachment.objects.count)(), 0)

    async def test_metadata_only_use_protects_both_datasets(self) -> None:
        async def stream(messages: list[Any], info: Any) -> Any:
            if "describe_model" in _tool_names(info) and not _returned(messages):
                yield _call("describe_model", {"name": "orders"}, META_CALL)
                return
            if _returned(messages):
                yield f"{ANSWER_MARK} listed"
                return
            yield ORDINARY

        self._patch_agent(stream)
        with WrenFixture(  # noqa: SIM117
            analytics=True,
            query_result=COUNT_RESULT,
        ) as fixture:
            with self.settings(**_dev(fixture.url)):
                world = await sync_to_async(self._provision)()
                client, token = await self._session(world["full"])
                thread_id = await self._open(client, token, world["sales_id"])
                body, parsed = await self.turn(
                    client, token, thread_id, "Describe orders.", 0
                )

        self._public(body)
        text = body.decode("utf-8")
        self.assertNotIn(META_CALL, text)
        self.assertNotIn(PHYSICAL, text)
        self.assertNotIn("tableReference", text)
        self.assertEqual(parsed[-1][0], "done")
        self.assertEqual(parsed[-1][1]["reply"], f"{ANSWER_MARK} listed")
        self.assertFalse(parsed[-1][1]["turn"]["can_regenerate"])
        self.assertEqual(fixture.queries, [])
        self.assertIn("describe_model", fixture.calls)
        self.assertNotIn("run_sql", fixture.calls)

        def stored() -> None:
            self._assert_idle(thread_id)
            self._assert_provenance(thread_id)
            self.assertEqual(ChatTurn.objects.filter(thread_id=thread_id).count(), 1)

        await sync_to_async(stored)()

    async def test_superuser_detail_hides_analytic_payloads(self) -> None:
        self._patch_agent(self._stream(echo_result=True))
        with WrenFixture(  # noqa: SIM117
            analytics=True,
            query_result=COUNT_RESULT,
        ) as fixture:
            with self.settings(
                **_dev(fixture.url),
                CHAT_SHOW_REASONING_DETAIL=True,
            ):
                world = await sync_to_async(self._provision)()
                await sync_to_async(self._assign)(
                    world["actor"].pk,
                    world["actor"].pk,
                    world["sales_id"],
                    (world["customers"].pk, world["orders"].pk),
                )
                client, token = await self._session(world["actor"])
                thread_id = await self._open(client, token, world["sales_id"])
                body, parsed = await self.turn(
                    client, token, thread_id, "How many orders are there?", 0
                )

        self._public(body)
        self.assertEqual(parsed[-1][0], "done")
        self.assertIn(ANSWER_MARK, parsed[-1][1]["reply"])
        steps = parsed[-1][1]["turn"]["activity"]["steps"]
        self.assertTrue(steps)
        for step in steps:
            self.assertEqual(set(step), {"id", "kind", "status"})
        self.assertIn(
            ("wren_tool", "complete"),
            [(step["kind"], step["status"]) for step in steps],
        )

        def stored() -> None:
            turn = ChatTurn.objects.get(thread_id=thread_id)
            blob = json.dumps(turn.activity)
            self.assertNotIn(SQL, blob)
            self.assertNotIn(CALL_ID, blob)
            self.assertNotIn(fixture.url, blob)
            for step in turn.activity["steps"]:
                self.assertEqual(set(step), {"id", "kind", "status"})

        await sync_to_async(stored)()

    async def test_definition_structure_and_source_changes_quarantine(self) -> None:
        self._patch_agent(self._stream(echo_result=True))
        with WrenFixture(  # noqa: SIM117
            analytics=True,
            query_result=COUNT_RESULT,
        ) as fixture:
            with self.settings(**_dev(fixture.url)):
                world = await sync_to_async(self._provision)()
                client, token = await self._session(world["full"])
                thread_id = await self._open(client, token, world["sales_id"])
                _body, parsed = await self.turn(
                    client, token, thread_id, "How many orders are there?", 0
                )
                self.assertEqual(parsed[-1][0], "done")
                mark = parsed[-1][1]["reply"]

                def shift_definition() -> str:
                    dataset = world["customers"]
                    dataset.refresh_from_db()
                    original = dataset.definition_sha256
                    dataset.definition_sha256 = "c" * 64
                    dataset.save(update_fields=["definition_sha256"])
                    return original

                original_definition = await sync_to_async(shift_definition)()
                by_definition = await self.detail(client, thread_id)

                def restore_definition() -> None:
                    dataset = world["customers"]
                    dataset.definition_sha256 = original_definition
                    dataset.save(update_fields=["definition_sha256"])

                await sync_to_async(restore_definition)()

                def shift_structure() -> str:
                    catalog = DatasetCatalogState.objects.get(
                        pk=configured_source_key()
                    )
                    original = catalog.structure_sha256
                    catalog.structure_sha256 = "e" * 64
                    catalog.save(update_fields=["structure_sha256"])
                    return original

                original_structure = await sync_to_async(shift_structure)()
                by_structure = await self.detail(client, thread_id)

                def restore_structure() -> None:
                    catalog = DatasetCatalogState.objects.get(
                        pk=configured_source_key()
                    )
                    catalog.structure_sha256 = original_structure
                    catalog.save(update_fields=["structure_sha256"])

                await sync_to_async(restore_structure)()
                with self.settings(WREN_MCP_URL="http://127.0.0.1:9/mcp"):
                    by_source = await self.detail(client, thread_id)
                restored = await self.detail(client, thread_id)

        for detail in (by_definition, by_structure, by_source):
            self.assertEqual(detail["access"], "quarantined")
            self.assertEqual(detail["title"], "Restricted chat")
            self.assertEqual(detail["display"], [])
            self.assertNotIn(mark, json.dumps(detail))
        self.assertEqual(restored["access"], "available")
        self.assertIn(mark, json.dumps(restored["display"]))

        def still_there() -> None:
            self.assertTrue(
                ChatTurnDataset.objects.filter(turn__thread_id=thread_id).exists()
            )

        await sync_to_async(still_there)()
