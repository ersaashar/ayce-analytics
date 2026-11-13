"""SSE and HTTP behavior for the authenticated thread chat application."""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from datetime import timedelta
from typing import Any
from unittest.mock import patch

from asgiref.sync import sync_to_async
from asgiref.testing import ApplicationCommunicator
from django.conf import settings
from django.core.asgi import get_asgi_application
from django.test import AsyncClient, override_settings
from django.utils import timezone
from pydantic_ai import FunctionToolset, RunContext
from pydantic_ai.exceptions import (
    ModelHTTPError,
    ModelRateLimitError,
    ModelRequestTimeout,
)
from pydantic_ai.messages import (
    ModelMessage,
    ModelMessagesTypeAdapter,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import DeltaToolCall

from chat import attachments, service, threads, views
from chat.models import ChatThread
from tests.support import (
    EMPTY_HEADING,
    PROVIDER_SETTINGS,
    THREADS_URL,
    ChatHttpTestCase,
    deltas,
    events,
    frame,
    named,
    next_frame,
    prompt_texts,
    read_body,
    read_stream,
    reply_texts,
)

CONNECTED = b": connected\n\n"


@override_settings(**PROVIDER_SETTINGS)
class PageTests(ChatHttpTestCase):
    async def test_workspace_renders_for_the_owner(self) -> None:
        self._echo_agent()
        user = await self.make_user("page-owner")
        thread = await self.make_thread(user)
        client = await self.client_for(user)
        token = await self.token(client)
        await self.completed_turn(client, token, thread.id, "hello page", 0)

        response = await client.get(f"/chat/{thread.id}/")
        body = response.content.decode()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Cache-Control"], "no-store")
        self.assertIn("AYCE Analytics V2", body)
        self.assertIn('id="chat-root"', body)
        self.assertIn('id="chat-bootstrap"', body)
        self.assertIn('name="csrfmiddlewaretoken"', body)
        self.assertIn("hello page", body)
        self.assertNotIn("offline-dummy-key", body)
        self.assertNotIn("offline.invalid", body)
        self.assertNotIn("chat_history_json", body)

    async def test_unselected_workspace_has_empty_composition(self) -> None:
        user = await self.make_user("empty-owner")
        client = await self.client_for(user)
        response = await client.get("/")
        body = response.content.decode()
        self.assertEqual(response.status_code, 200)
        self.assertIn(EMPTY_HEADING, body)
        self.assertNotIn('data-role="user"', body)

    async def test_foreign_and_unknown_thread_pages_are_the_same_404(self) -> None:
        owner = await self.make_user("page-owner-two")
        stranger = await self.make_user("page-stranger")
        thread = await self.make_thread(owner, title="Owner private title")

        stranger_client = await self.client_for(stranger)
        foreign = await stranger_client.get(f"/chat/{thread.id}/")
        missing = await stranger_client.get(
            "/chat/2f9d4b1e-6c3a-4f5b-8e7d-0a1b2c3d4e5f/"
        )
        self.assertEqual(foreign.status_code, 404)
        self.assertEqual(missing.status_code, 404)
        self.assertNotIn("Owner private title", foreign.content.decode())


@override_settings(**PROVIDER_SETTINGS)
class ValidationTests(ChatHttpTestCase):
    async def test_rejects_malformed_payloads(self) -> None:
        factory = self._echo_agent()
        user = await self.make_user("validation-owner")
        thread = await self.make_thread(user)
        client = await self.client_for(user)
        token = await self.token(client)
        url = f"/api/threads/{thread.id}/messages/"

        cases = {
            "malformed json": "{not json",
            "non-object": '"just a string"',
            "array body": "[]",
            "empty object": "{}",
            "unknown key": '{"message": "hi", "revision": 0, "history": []}',
            "non-string message": '{"message": 5, "revision": 0}',
            "null message": '{"message": null, "revision": 0}',
            "whitespace only": '{"message": "   \\n\\t ", "revision": 0}',
            "too long": json.dumps({"message": "a" * 2001, "revision": 0}),
            "missing revision": '{"message": "hi"}',
            "boolean revision": '{"message": "hi", "revision": true}',
            "negative revision": '{"message": "hi", "revision": -1}',
            "string revision": '{"message": "hi", "revision": "0"}',
            "float revision": '{"message": "hi", "revision": 1.5}',
        }
        for label, body in cases.items():
            with self.subTest(case=label):
                response = await client.post(
                    url,
                    data=body,
                    content_type="application/json",
                    headers={"X-CSRFToken": token},
                )
                self.assertEqual(response.status_code, 400)
                self.assertEqual(self.error_code(response), "invalid_message")

        bad_utf8 = await client.post(
            url,
            data=b'{"message": "\xff\xfe", "revision": 0}',
            content_type="application/json",
            headers={"X-CSRFToken": token},
        )
        self.assertEqual(bad_utf8.status_code, 400)
        self.assertEqual(self.error_code(bad_utf8), "invalid_message")

        wrong_type = await client.post(
            url,
            data='{"message": "hi", "revision": 0}',
            content_type="text/plain",
            headers={"X-CSRFToken": token},
        )
        self.assertEqual(wrong_type.status_code, 400)
        self.assertEqual(self.error_code(wrong_type), "invalid_message")
        self.assertEqual(factory.seen, [])

    async def test_accepts_exact_character_boundary(self) -> None:
        for label, message in (
            ("ascii", "a" * 2000),
            ("unicode", "\u00e9" * 2000),
            ("supplementary", "\U0001f600" * 2000),
        ):
            with self.subTest(case=label):
                user = await self.make_user(f"boundary-{label}")
                thread = await self.make_thread(user)
                client = await self.client_for(user)
                token = await self.token(client)
                self._echo_agent()

                _body, parsed = await self.turn(client, token, thread.id, message, 0)
                self.assertEqual(deltas(parsed), [f"echo:{message}"])
                self.assertEqual(
                    parsed[-1][0],
                    "done",
                )
                self.assertEqual(parsed[-1][1]["reply"], f"echo:{message}")

    async def test_trims_surrounding_whitespace_before_sending(self) -> None:
        factory = self._echo_agent()
        user = await self.make_user("trim-owner")
        thread = await self.make_thread(user)
        client = await self.client_for(user)
        token = await self.token(client)

        _body, parsed = await self.turn(
            client, token, thread.id, "   padded message   ", 0
        )
        self.assertEqual(parsed[-1][1]["reply"], "echo:padded message")
        self.assertEqual(prompt_texts(factory.seen[0])[-1], "padded message")

        row = await self.fetch_row(thread.id)
        self.assertEqual(
            row.display,
            [
                {"role": "user", "content": "padded message"},
                {"role": "assistant", "content": "echo:padded message"},
            ],
        )


@override_settings(**PROVIDER_SETTINGS)
class CsrfAndMethodTests(ChatHttpTestCase):
    async def test_csrf_is_enforced_for_every_mutation(self) -> None:
        user = await self.make_user("csrf-owner")
        thread = await self.make_thread(user)
        guarded = AsyncClient(enforce_csrf_checks=True)
        await guarded.aforce_login(user)

        responses = {
            "send": await guarded.post(
                f"/api/threads/{thread.id}/messages/",
                data='{"message": "hi", "revision": 0}',
                content_type="application/json",
            ),
            "create": await guarded.post(
                THREADS_URL, data="{}", content_type="application/json"
            ),
            "rename": await guarded.patch(
                f"/api/threads/{thread.id}/",
                data='{"title": "new", "revision": 0}',
                content_type="application/json",
            ),
            "delete": await guarded.delete(
                f"/api/threads/{thread.id}/",
                data='{"revision": 0}',
                content_type="application/json",
            ),
        }
        for label, response in responses.items():
            with self.subTest(endpoint=label):
                self.assertEqual(response.status_code, 403)
                self.assertEqual(self.error_code(response), "csrf_failed")

    async def test_csrf_token_allows_each_operation(self) -> None:
        self._echo_agent()
        user = await self.make_user("csrf-token-owner")
        thread = await self.make_thread(user)
        guard = AsyncClient(enforce_csrf_checks=True)
        await guard.aforce_login(user)
        token = await self.token(guard)

        created = await guard.post(
            THREADS_URL,
            data="{}",
            content_type="application/json",
            headers={"X-CSRFToken": token},
        )
        self.assertEqual(created.status_code, 201)

        sent = await self.send_turn(guard, token, thread.id, "hello", 0)
        self.assertEqual(sent.status_code, 200)
        await read_body(sent)
        await sync_to_async(sent.close)()

        renamed = await guard.patch(
            f"/api/threads/{thread.id}/",
            data='{"title": "Guard chat", "revision": 1}',
            content_type="application/json",
            headers={"X-CSRFToken": token},
        )
        self.assertEqual(renamed.status_code, 200)

        deleted = await guard.delete(
            f"/api/threads/{created.json()['thread']['id']}/",
            data='{"revision": 0}',
            content_type="application/json",
            headers={"X-CSRFToken": token},
        )
        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(deleted.json(), {"ok": True})

    async def test_csrf_failure_keeps_html_for_account_pages(self) -> None:
        guarded = AsyncClient(enforce_csrf_checks=True)
        response = await guarded.post("/accounts/logout/")
        self.assertEqual(response.status_code, 403)
        self.assertIn("text/html", response["Content-Type"])

    async def test_wrong_methods_return_fixed_envelopes(self) -> None:
        user = await self.make_user("method-owner")
        thread = await self.make_thread(user)
        client = await self.client_for(user)

        cases = {
            "collection": ("/api/threads/", "PUT", "GET, POST"),
            "detail": (f"/api/threads/{thread.id}/", "PUT", "GET, PATCH, DELETE"),
            "messages": (f"/api/threads/{thread.id}/messages/", "GET", "POST"),
        }
        for label, (url, method, allow) in cases.items():
            with self.subTest(endpoint=label):
                response = await client.generic(method, url)
                self.assertEqual(response.status_code, 405)
                self.assertEqual(response["Allow"], allow)
                self.assertEqual(
                    response.json(),
                    {
                        "error": {
                            "code": "method_not_allowed",
                            "message": "This HTTP method is not allowed.",
                        }
                    },
                )

    async def test_collection_get_and_post_use_their_own_contract(self) -> None:
        user = await self.make_user("collection-owner")
        client = await self.client_for(user)
        token = await self.token(client)

        empty = await client.post(
            THREADS_URL,
            data="{}",
            content_type="application/json",
            headers={"X-CSRFToken": token},
        )
        self.assertEqual(empty.status_code, 201)
        self.assertIn("Location", empty.headers)
        self.assertEqual(empty.json()["thread"]["display"], [])
        self.assertEqual(empty.json()["thread"]["revision"], 0)

        extra = await client.post(
            THREADS_URL,
            data='{"title": "x"}',
            content_type="application/json",
            headers={"X-CSRFToken": token},
        )
        self.assertEqual(extra.status_code, 400)
        self.assertEqual(self.error_code(extra), "invalid_thread")

        wrong_ct = await client.post(
            THREADS_URL,
            data="{}",
            content_type="application/x-www-form-urlencoded",
            headers={"X-CSRFToken": token},
        )
        self.assertEqual(wrong_ct.status_code, 400)
        self.assertEqual(self.error_code(wrong_ct), "invalid_thread")


@override_settings(**PROVIDER_SETTINGS)
class StreamingTests(ChatHttpTestCase):
    async def test_stream_headers_and_initial_comment(self) -> None:
        self._echo_agent()
        user = await self.make_user("stream-owner")
        thread = await self.make_thread(user)
        client = await self.client_for(user)
        token = await self.token(client)
        response = await self.send_turn(client, token, thread.id, "hello", 0)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/event-stream")
        self.assertEqual(response["Cache-Control"], "no-cache, no-store")
        self.assertEqual(response["X-Accel-Buffering"], "no")

        iterator = response.streaming_content.__aiter__()
        self.assertEqual(await anext(iterator), CONNECTED)
        await sync_to_async(response.close)()

    async def test_deltas_arrive_before_the_turn_is_durable(self) -> None:
        gate = asyncio.Event()

        async def stream(messages: list[ModelMessage], info: Any) -> Any:
            yield "first"
            await gate.wait()
            yield " second"

        self._patch_agent(stream)
        user = await self.make_user("gated-owner")
        thread = await self.make_thread(user)
        client = await self.client_for(user)
        token = await self.token(client)
        response = await self.send_turn(client, token, thread.id, "hello", 0)

        iterator = response.streaming_content.__aiter__()
        self.assertEqual(await anext(iterator), CONNECTED)
        self.assertEqual(
            await next_frame(iterator, "delta"), frame("delta", {"text": "first"})
        )

        pending = await self.fetch_row(thread.id)
        self.assertEqual(pending.display, [])
        self.assertEqual(pending.revision, 0)
        self.assertEqual(pending.title, "New chat")
        self.assertFalse(gate.is_set())

        gate.set()
        rest = await read_stream(iterator)
        await sync_to_async(response.close)()

        parsed = events(rest)
        self.assertEqual(parsed[0][0], "delta")
        self.assertEqual(parsed[0][1], {"text": " second"})
        done_event, done = parsed[-1]
        self.assertEqual(done_event, "done")
        self.assertEqual(done["reply"], "first second")

        row = await self.fetch_row(thread.id)
        self.assertEqual(row.revision, 1)
        self.assertEqual(
            row.display,
            [
                {"role": "user", "content": "hello"},
                {"role": "assistant", "content": "first second"},
            ],
        )
        self.assertEqual(row.title, "hello")
        self.assertEqual(done["thread"]["revision"], 1)
        self.assertEqual(done["thread"]["title"], "hello")
        self.assertEqual(done["thread"]["id"], str(thread.id))
        service.load_history(row.history_json)

    async def test_done_appears_only_after_the_commit(self) -> None:
        gate = asyncio.Event()
        observed: dict[str, Any] = {}

        async def stream(messages: list[ModelMessage], info: Any) -> Any:
            yield "answer"
            await gate.wait()

        self._patch_agent(stream)
        user = await self.make_user("commit-order-owner")
        thread = await self.make_thread(user)
        client = await self.client_for(user)
        token = await self.token(client)
        response = await self.send_turn(client, token, thread.id, "hi", 0)

        iterator = response.streaming_content.__aiter__()
        await anext(iterator)
        await anext(iterator)
        gate.set()

        async for chunk in iterator:
            if chunk.startswith(b"event: done"):
                observed["done"] = chunk
                row = await self.fetch_row(thread.id)
                observed["revision"] = row.revision
                observed["display"] = row.display
                observed["history"] = row.history_json
        await sync_to_async(response.close)()

        self.assertIn(b"event: done", observed["done"])
        self.assertEqual(observed["revision"], 1)
        self.assertEqual(len(observed["display"]), 2)
        service.load_history(observed["history"])

    async def test_reload_restores_the_exact_pairs(self) -> None:
        self._echo_agent()
        user = await self.make_user("reload-owner")
        thread = await self.make_thread(user)
        client = await self.client_for(user)
        token = await self.token(client)

        await self.completed_turn(client, token, thread.id, "stored question", 0)
        detail = await self.detail(client, thread.id)

        self.assertEqual(
            detail["display"],
            [
                {"role": "user", "content": "stored question"},
                {"role": "assistant", "content": "echo:stored question"},
            ],
        )
        page = (await client.get(f"/chat/{thread.id}/")).content.decode()
        self.assertIn("stored question", page)
        self.assertIn("echo:stored question", page)

        fresh = await self.client_for(user)
        listed = await self.summary_list(fresh)
        self.assertEqual([entry["id"] for entry in listed], [str(thread.id)])
        self.assertEqual(
            (await self.detail(fresh, thread.id))["display"], detail["display"]
        )

    async def test_no_pending_pair_is_stored_before_completion(self) -> None:
        gate = asyncio.Event()

        async def stream(messages: list[ModelMessage], info: Any) -> Any:
            yield "partial"
            await gate.wait()
            yield " rest"

        self._patch_agent(stream)
        user = await self.make_user("pending-owner")
        thread = await self.make_thread(user)
        client = await self.client_for(user)
        token = await self.token(client)
        response = await self.send_turn(client, token, thread.id, "keep me pending", 0)

        iterator = response.streaming_content.__aiter__()
        await anext(iterator)
        self.assertEqual(
            await next_frame(iterator, "delta"), frame("delta", {"text": "partial"})
        )

        row = await self.fetch_row(thread.id)
        self.assertEqual(row.display, [])
        self.assertEqual(row.revision, 0)

        gate.set()
        await read_stream(iterator)
        await sync_to_async(response.close)()

        row = await self.fetch_row(thread.id)
        self.assertEqual(len(row.display), 2)
        self.assertEqual(row.revision, 1)

    async def test_two_turns_share_real_serialized_history(self) -> None:
        factory = self._echo_agent()
        user = await self.make_user("history-owner")
        thread = await self.make_thread(user)
        client = await self.client_for(user)
        token = await self.token(client)

        await self.completed_turn(client, token, thread.id, "alpha question", 0)
        payload, revision = await self.completed_turn(
            client, token, thread.id, "beta question", 1
        )
        self.assertEqual(revision, 2)
        self.assertEqual(payload["reply"], "echo:beta question")

        second = factory.seen[1]
        prompts = prompt_texts(second)
        self.assertIn("alpha question", prompts)
        self.assertIn("beta question", prompts)
        self.assertIn("echo:alpha question", reply_texts(second))

        row = await self.fetch_row(thread.id)
        self.assertEqual(len(row.display), 4)
        self.assertEqual(row.revision, 2)

    async def test_accounts_never_share_transcripts(self) -> None:
        factory = self._echo_agent()
        first_user = await self.make_user("iso-first")
        second_user = await self.make_user("iso-second")
        first_thread = await self.make_thread(first_user)
        second_thread = await self.make_thread(second_user)

        first = await self.client_for(first_user)
        first_token = await self.token(first)
        await self.completed_turn(
            first, first_token, first_thread.id, "private first", 0
        )

        second = await self.client_for(second_user)
        second_token = await self.token(second)
        page = (await second.get("/")).content.decode()
        self.assertNotIn("private first", page)
        self.assertIn(EMPTY_HEADING, page)

        listed = await self.summary_list(second)
        self.assertEqual([entry["id"] for entry in listed], [str(second_thread.id)])

        await self.completed_turn(
            second, second_token, second_thread.id, "own question", 0
        )
        self.assertEqual(prompt_texts(factory.seen[-1]), ["own question"])


@override_settings(**PROVIDER_SETTINGS)
class FailureTests(ChatHttpTestCase):
    def assert_error_frames(
        self,
        body: bytes,
        parsed: list[tuple[str, Any]],
        code: str,
    ) -> None:
        """Assert one fixed error frame, no completion, and no raw detail."""
        self.assertEqual(len([event for event, _data in parsed if event == "error"]), 1)
        self.assertEqual(parsed[-1][0], "error")
        self.assertEqual(parsed[-1][1]["error"]["code"], code)
        self.assertEqual(parsed[-1][1]["error"]["message"], views.ERRORS[code][1])
        self.assertNotIn(b"event: done", body)
        self.assertNotIn(b"synthetic-secret", body)
        self.assertNotIn(b"Traceback", body)

    def assert_row_unchanged(self, before: ChatThread, after: ChatThread) -> None:
        """Assert a failed turn left the stored row byte-for-byte intact."""
        self.assertEqual(after.display, before.display)
        self.assertEqual(after.history_json, before.history_json)
        self.assertEqual(after.revision, before.revision)
        self.assertEqual(after.title, before.title)
        self.assertEqual(after.updated_at, before.updated_at)

    async def assert_safe_failure(
        self,
        error: Exception,
        code: str,
        *,
        before_delta: bool,
    ) -> None:
        state: dict[str, Exception | None] = {"error": None}

        def stream(messages: list[ModelMessage], info: Any) -> Any:
            async def produce() -> Any:
                if not before_delta:
                    yield "partial"
                if state["error"] is not None:
                    raise state["error"]
                yield f"echo:{prompt_texts(messages)[-1]}"

            return produce()

        self._patch_agent(stream)
        user = await self.make_user(f"failure-{code}-{before_delta}")
        thread = await self.make_thread(user)
        client = await self.client_for(user)
        token = await self.token(client)
        await self.completed_turn(client, token, thread.id, "first", 0)
        before = await self.fetch_row(thread.id)
        state["error"] = error

        if not before_delta:
            with patch.object(service, "STREAM_DEBOUNCE_SECONDS", None):
                body, parsed = await self.turn(client, token, thread.id, "trigger", 1)
        else:
            body, parsed = await self.turn(client, token, thread.id, "trigger", 1)

        self.assert_error_frames(body, parsed, code)
        self.assert_row_unchanged(before, await self.fetch_row(thread.id))

    async def test_http_error_stays_200_with_error_event(self) -> None:
        await self.assert_safe_failure(
            ModelHTTPError(500, "offline-model", body={"detail": "synthetic-secret"}),
            "provider_error",
            before_delta=True,
        )

    async def test_rate_limit_is_an_error_event(self) -> None:
        await self.assert_safe_failure(
            ModelRateLimitError("offline-model", "synthetic-secret"),
            "provider_rate_limited",
            before_delta=True,
        )

    async def test_timeout_is_an_error_event(self) -> None:
        await self.assert_safe_failure(
            ModelRequestTimeout("offline-model", 60),
            "provider_timeout",
            before_delta=False,
        )

    async def test_partial_delta_then_failure_keeps_the_row(self) -> None:
        state = {"failing": False}

        async def stream(messages: list[ModelMessage], info: Any) -> Any:
            if state["failing"]:
                yield "partial"
                raise ModelHTTPError(500, "offline-model", body={"detail": "boom"})
            yield f"echo:{prompt_texts(messages)[-1]}"

        self._patch_agent(stream)
        user = await self.make_user("partial-failure-owner")
        thread = await self.make_thread(user)
        client = await self.client_for(user)
        token = await self.token(client)
        await self.completed_turn(client, token, thread.id, "first", 0)
        before = await self.fetch_row(thread.id)
        state["failing"] = True

        with patch.object(service, "STREAM_DEBOUNCE_SECONDS", None):
            _body, parsed = await self.turn(client, token, thread.id, "trigger", 1)

        self.assertIn(("delta", {"text": "partial"}), parsed)
        self.assertNotIn("done", [event for event, _data in parsed])
        after = await self.fetch_row(thread.id)
        self.assertEqual(after.display, before.display)
        self.assertEqual(after.revision, 1)

    async def test_real_deadline_produces_provider_timeout(self) -> None:
        async def stream(messages: list[ModelMessage], info: Any) -> Any:
            await asyncio.sleep(30)
            yield "too late"

        self._patch_agent(stream, timeout=0.05)
        user = await self.make_user("deadline-owner")
        thread = await self.make_thread(user)
        client = await self.client_for(user)
        token = await self.token(client)

        _body, parsed = await self.turn(client, token, thread.id, "never stored", 0)
        self.assertEqual(parsed[-1][0], "error")
        self.assertEqual(parsed[-1][1]["error"]["code"], "provider_timeout")
        row = await self.fetch_row(thread.id)
        self.assertEqual(row.display, [])
        self.assertEqual(row.revision, 0)

    async def test_chat_error_after_a_delta_keeps_the_row(self) -> None:
        toolset: FunctionToolset[None] = FunctionToolset()

        @toolset.tool
        def query_warehouse(ctx: RunContext[None]) -> int:
            """Fail without exposing backend details."""
            raise service.ChatError("authorization_changed")

        state = {"failing": False}

        async def stream(messages: list[ModelMessage], info: Any) -> Any:
            if not state["failing"]:
                yield f"echo:{prompt_texts(messages)[-1]}"
                return
            yield "partial"
            yield {
                0: DeltaToolCall(
                    name="query_warehouse",
                    json_args="{}",
                    tool_call_id="call-1",
                )
            }

        self._patch_agent(stream, toolsets=(toolset,))
        user = await self.make_user("mcp-failure-owner")
        thread = await self.make_thread(user)
        client = await self.client_for(user)
        token = await self.token(client)
        await self.completed_turn(client, token, thread.id, "first", 0)
        before = await self.fetch_row(thread.id)
        state["failing"] = True

        with patch.object(service, "STREAM_DEBOUNCE_SECONDS", None):
            body, parsed = await self.turn(client, token, thread.id, "trigger", 1)

        self.assertIn(("delta", {"text": "partial"}), parsed)
        self.assert_error_frames(body, parsed, "authorization_changed")
        self.assert_row_unchanged(before, await self.fetch_row(thread.id))

    async def test_missing_configuration_wins_over_corrupt_history(self) -> None:
        factory = self._echo_agent()
        user = await self.make_user("noconfig-owner")
        thread = await self.make_thread(user)
        await sync_to_async(ChatThread.objects.filter(pk=thread.id).update)(
            history_json="broken",
            display="broken",
        )
        client = await self.client_for(user)
        token = await self.token(client)

        with override_settings(LLM_API_KEY=""):
            response = await self.send_turn(client, token, thread.id, "hello", 0)
        self.assertEqual(response.status_code, 503)
        self.assertEqual(self.error_code(response), "provider_not_configured")
        self.assertEqual(factory.seen, [])


@override_settings(**PROVIDER_SETTINGS)
class ToolTurnTests(ChatHttpTestCase):
    async def test_tool_turn_streams_grounded_reply_and_commits(self) -> None:
        calls: list[int] = []
        toolset: FunctionToolset[None] = FunctionToolset()

        @toolset.tool
        def lookup_order_count(ctx: RunContext[None]) -> int:
            """Count the orders in the fixture."""
            calls.append(1)
            return 3

        async def stream(messages: list[ModelMessage], info: Any) -> Any:
            returned = [
                part
                for message in messages
                for part in getattr(message, "parts", [])
                if isinstance(part, ToolReturnPart)
            ]
            if not returned:
                yield "Checking. "
                yield {
                    0: DeltaToolCall(
                        name="lookup_order_count",
                        json_args="{}",
                        tool_call_id="call-1",
                    )
                }
                return
            yield "There are 3 orders."

        self._patch_agent(stream, toolsets=(toolset,))
        user = await self.make_user("tool-turn-owner")
        thread = await self.make_thread(user)
        client = await self.client_for(user)
        token = await self.token(client)

        with patch.object(service, "STREAM_DEBOUNCE_SECONDS", None):
            body, parsed = await self.turn(client, token, thread.id, "count orders", 0)

        self.assertEqual(parsed[-1][0], "done")
        self.assertEqual(parsed[-1][1]["reply"], "Checking. There are 3 orders.")
        self.assertEqual(
            [data["text"] for event, data in parsed if event == "delta"],
            ["Checking. ", "There are 3 orders."],
        )
        self.assertEqual(parsed[-1][1]["thread"]["revision"], 1)
        self.assertIn(b"event: done", body)
        self.assertEqual(calls, [1])

        row = await self.fetch_row(thread.id)
        self.assertEqual(row.revision, 1)
        self.assertEqual(
            [entry["role"] for entry in row.display], ["user", "assistant"]
        )
        self.assertEqual(row.display[-1]["content"], "Checking. There are 3 orders.")
        history = ModelMessagesTypeAdapter.validate_json(row.history_json)
        call_parts = [
            part
            for message in history
            for part in getattr(message, "parts", [])
            if isinstance(part, ToolCallPart)
        ]
        return_parts = [
            part
            for message in history
            for part in getattr(message, "parts", [])
            if isinstance(part, ToolReturnPart)
        ]
        self.assertEqual([part.tool_call_id for part in call_parts], ["call-1"])
        self.assertEqual([part.tool_call_id for part in return_parts], ["call-1"])
        self.assertEqual(return_parts[0].content, 3)


@override_settings(**PROVIDER_SETTINGS)
class DisconnectTests(ChatHttpTestCase):
    async def test_asgi_disconnect_stops_generation_without_a_turn(self) -> None:
        gate = asyncio.Event()

        async def stream(messages: list[ModelMessage], info: Any) -> Any:
            yield "first"
            await gate.wait()
            yield " never"

        factory = self._patch_agent(stream)
        user = await self.make_user("disconnect-owner")
        thread = await self.make_thread(user)
        client = await self.client_for(user)
        token = await self.token(client)

        cookie = "; ".join(
            [
                f"{settings.SESSION_COOKIE_NAME}="
                f"{client.cookies[settings.SESSION_COOKIE_NAME].value}",
                f"{settings.CSRF_COOKIE_NAME}="
                f"{client.cookies[settings.CSRF_COOKIE_NAME].value}",
            ]
        )
        path = f"/api/threads/{thread.id}/messages/"
        body = json.dumps({"message": "hello", "revision": 0}).encode()
        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.1"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "root_path": "",
            "headers": [
                (b"host", b"testserver"),
                (b"cookie", cookie.encode()),
                (b"x-csrftoken", token.encode()),
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
            "client": ("127.0.0.1", 47123),
            "server": ("testserver", 80),
        }

        communicator = ApplicationCommunicator(get_asgi_application(), scope)
        try:
            await communicator.send_input(
                {"type": "http.request", "body": body, "more_body": False}
            )
            start = await asyncio.wait_for(communicator.receive_output(1), timeout=10)
            self.assertEqual(start["type"], "http.response.start")
            self.assertEqual(start["status"], 200)

            connected = await asyncio.wait_for(
                communicator.receive_output(1), timeout=10
            )
            self.assertIn(CONNECTED.strip(), connected["body"])
            for _ in range(20):
                output = await asyncio.wait_for(
                    communicator.receive_output(1), timeout=10
                )
                if b"event: delta" in output["body"]:
                    break
            else:  # pragma: no cover - defensive
                self.fail("no delta frame before disconnect")
            self.assertFalse(gate.is_set())

            await communicator.send_input({"type": "http.disconnect"})
            with contextlib.suppress(asyncio.CancelledError, TimeoutError):
                await asyncio.wait_for(communicator.wait(), timeout=10)
        finally:
            with contextlib.suppress(Exception):
                await communicator.aclose()

        self.assertEqual(len(factory.agents), 1)
        self.assertEqual(factory.agents[0].exited, 1)
        self.assertFalse(gate.is_set())

        row = await self.fetch_row(thread.id)
        self.assertEqual(row.display, [])
        self.assertEqual(row.revision, 0)
        self.assertEqual(row.title, "New chat")


@override_settings(**PROVIDER_SETTINGS)
class CorruptStateTests(ChatHttpTestCase):
    async def corrupt(self, thread_id: Any, **fields: Any) -> None:
        await sync_to_async(ChatThread.objects.filter(pk=thread_id).update)(**fields)

    async def test_invalid_canonical_history_blocks_send_and_stays_put(self) -> None:
        factory = self._echo_agent()
        user = await self.make_user("corrupt-history-owner")
        thread = await self.make_thread(user)
        await self.corrupt(thread.id, history_json="not canonical json", display=[])
        client = await self.client_for(user)
        token = await self.token(client)

        response = await self.send_turn(client, token, thread.id, "hello", 0)
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.error_code(response), "conversation_invalid")
        self.assertEqual(factory.seen, [])

        detail = await self.detail(client, thread.id)
        self.assertEqual(detail["display"], [])
        self.assertEqual(
            detail["conversation_error"],
            threads.CONVERSATION_INVALID_MESSAGE,
        )

        page = (await client.get(f"/chat/{thread.id}/")).content.decode()
        self.assertIn(threads.CONVERSATION_INVALID_MESSAGE, page)

        row = await self.fetch_row(thread.id)
        self.assertEqual(row.history_json, "not canonical json")
        self.assertEqual(row.display, [])

        deleted = await client.delete(
            f"/api/threads/{thread.id}/",
            data='{"revision": 0}',
            content_type="application/json",
            headers={"X-CSRFToken": token},
        )
        self.assertEqual(deleted.status_code, 200)
        self.assertFalse(await self.row_exists(thread.id))

    async def test_mismatched_display_blocks_send(self) -> None:
        factory = self._echo_agent()
        user = await self.make_user("corrupt-display-owner")
        thread = await self.make_thread(user)
        client = await self.client_for(user)
        token = await self.token(client)
        await self.completed_turn(client, token, thread.id, "seed history", 0)
        row = await self.fetch_row(thread.id)
        valid_history = row.history_json

        cases = {
            "history not string": (123, []),
            "odd length": (valid_history, [{"role": "user", "content": "x"}]),
            "wrong role order": (
                valid_history,
                [{"role": "assistant", "content": "x"}],
            ),
            "extra key": (
                valid_history,
                [{"role": "user", "content": "x", "extra": "y"}],
            ),
            "non-string content": (valid_history, [{"role": "user", "content": 5}]),
            "display not list": (valid_history, {"role": "user"}),
        }
        for label, (history, display) in cases.items():
            with self.subTest(case=label):
                factory.seen.clear()
                await self.corrupt(
                    thread.id,
                    history_json=history,
                    display=display,
                )
                response = await self.send_turn(client, token, thread.id, "hello", 1)
                self.assertEqual(response.status_code, 409)
                self.assertEqual(self.error_code(response), "conversation_invalid")
                self.assertEqual(factory.seen, [])


@override_settings(**PROVIDER_SETTINGS)
class LimitTests(ChatHttpTestCase):
    async def test_turn_limit_is_per_thread(self) -> None:
        factory = self._echo_agent()
        user = await self.make_user("limit-owner")
        first = await self.make_thread(user)
        second = await self.make_thread(user)
        client = await self.client_for(user)
        token = await self.token(client)

        revision = 0
        for index in range(threads.MAX_TURNS):
            with self.subTest(turn=index):
                _payload, revision = await self.completed_turn(
                    client, token, first.id, f"turn {index}", revision
                )

        seen_before = len(factory.seen)
        blocked = await self.send_turn(
            client, token, first.id, "one too many", revision
        )
        self.assertEqual(blocked.status_code, 400)
        self.assertEqual(self.error_code(blocked), "conversation_limit")
        self.assertEqual(len(factory.seen), seen_before)

        row = await self.fetch_row(first.id)
        self.assertEqual(len(row.display), threads.MAX_TURNS * 2)

        await self.completed_turn(client, token, second.id, "other thread", 0)

    async def test_rename_does_not_count_as_a_turn(self) -> None:
        self._echo_agent()
        user = await self.make_user("rename-limit-owner")
        thread = await self.make_thread(user)
        client = await self.client_for(user)
        token = await self.token(client)

        revision = 0
        for index in range(threads.MAX_TURNS):
            _payload, revision = await self.completed_turn(
                client, token, thread.id, f"turn {index}", revision
            )

        renamed = await client.patch(
            f"/api/threads/{thread.id}/",
            data=json.dumps({"title": "Renamed at the limit", "revision": revision}),
            content_type="application/json",
            headers={"X-CSRFToken": token},
        )
        self.assertEqual(renamed.status_code, 200)
        self.assertEqual(renamed.json()["thread"]["revision"], revision + 1)

        blocked = await self.send_turn(
            client,
            token,
            thread.id,
            "still blocked",
            revision + 1,
        )
        self.assertEqual(blocked.status_code, 400)
        self.assertEqual(self.error_code(blocked), "conversation_limit")

    async def test_stored_payload_over_byte_limit_refused_before_model(self) -> None:
        factory = self._echo_agent()
        user = await self.make_user("bytes-owner")
        thread = await self.make_thread(user)
        client = await self.client_for(user)
        token = await self.token(client)

        with patch.object(threads, "MAX_HISTORY_BYTES", 40000):
            _payload, revision = await self.completed_turn(
                client, token, thread.id, "grow " + "\u00e9" * 900, 0
            )
            row = await self.fetch_row(thread.id)
            stored_bytes = threads.payload_bytes(row.history_json, row.display)
            self.assertLess(stored_bytes, 40000)

            seen_before = len(factory.seen)
            with patch.object(threads, "MAX_HISTORY_BYTES", stored_bytes):
                blocked = await self.send_turn(
                    client, token, thread.id, "another", revision
                )

        self.assertEqual(blocked.status_code, 400)
        self.assertEqual(self.error_code(blocked), "conversation_limit")
        self.assertEqual(len(factory.seen), seen_before)

    async def test_oversized_candidate_leaves_the_row_untouched(self) -> None:
        self._echo_agent()
        user = await self.make_user("candidate-owner")
        thread = await self.make_thread(user)
        other = await self.make_thread(user)
        client = await self.client_for(user)
        token = await self.token(client)
        await self.completed_turn(client, token, thread.id, "small opener", 0)
        before = await self.fetch_row(thread.id)

        with patch.object(threads, "MAX_HISTORY_BYTES", 2500):
            body, parsed = await self.turn(client, token, thread.id, "x" * 1500, 1)

        self.assertEqual(parsed[-1][0], "error")
        self.assertEqual(parsed[-1][1]["error"]["code"], "conversation_limit")
        self.assertNotIn(b"event: done", body)

        after = await self.fetch_row(thread.id)
        self.assertEqual(after.display, before.display)
        self.assertEqual(after.history_json, before.history_json)
        self.assertEqual(after.revision, before.revision)
        self.assertEqual(after.title, before.title)
        self.assertEqual(after.updated_at, before.updated_at)

        with patch.object(threads, "MAX_HISTORY_BYTES", 40000):
            await self.completed_turn(client, token, other.id, "other works", 0)


@override_settings(**PROVIDER_SETTINGS)
class ConcurrencyTests(ChatHttpTestCase):
    async def test_second_send_is_rejected_while_a_run_is_active(self) -> None:
        gate = asyncio.Event()

        async def stream(messages: list[ModelMessage], info: Any) -> Any:
            yield "reply"
            await gate.wait()

        factory = self._patch_agent(stream)
        user = await self.make_user("race-owner")
        thread = await self.make_thread(user)
        first_client = await self.client_for(user)
        second_client = await self.client_for(user)
        token = await self.token(first_client)

        response = await self.send_turn(first_client, token, thread.id, "first", 0)
        iterator = response.streaming_content.__aiter__()
        await anext(iterator)
        await next_frame(iterator, "delta")

        busy_send = await self.send_turn(second_client, token, thread.id, "second", 0)
        self.assertEqual(busy_send.status_code, 409)
        self.assertEqual(self.error_code(busy_send), "thread_busy")
        self.assertEqual(len(factory.seen), 1)

        gate.set()
        parsed = events(await read_stream(iterator))
        await sync_to_async(response.close)()
        self.assertEqual(parsed[-1][0], "done")

        row = await self.fetch_row(thread.id)
        self.assertEqual(row.revision, 1)
        self.assertEqual(len(row.display), 2)
        self.assertEqual(row.active_run_token, None)
        self.assertEqual(row.active_run_expires_at, None)

        stale = await self.send_turn(second_client, token, thread.id, "stale", 0)
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(self.error_code(stale), "thread_conflict")

    async def test_rename_during_generation_wins(self) -> None:
        gate = asyncio.Event()

        async def stream(messages: list[ModelMessage], info: Any) -> Any:
            yield "answer"
            await gate.wait()

        self._patch_agent(stream)
        user = await self.make_user("rename-race-owner")
        thread = await self.make_thread(user)
        first_client = await self.client_for(user)
        second_client = await self.client_for(user)
        token = await self.token(first_client)

        response = await self.send_turn(first_client, token, thread.id, "hello", 0)
        iterator = response.streaming_content.__aiter__()
        await anext(iterator)
        await next_frame(iterator, "delta")

        renamed = await second_client.patch(
            f"/api/threads/{thread.id}/",
            data='{"title": "Renamed while streaming", "revision": 0}',
            content_type="application/json",
            headers={"X-CSRFToken": token},
        )
        self.assertEqual(renamed.status_code, 200)

        gate.set()
        parsed = events(await read_stream(iterator))
        await sync_to_async(response.close)()
        self.assertEqual(parsed[-1][0], "error")
        self.assertEqual(parsed[-1][1]["error"]["code"], "thread_conflict")

        row = await self.fetch_row(thread.id)
        self.assertEqual(row.title, "Renamed while streaming")
        self.assertEqual(row.display, [])
        self.assertEqual(row.revision, 1)

    async def test_delete_during_generation_cannot_be_recreated(self) -> None:
        gate = asyncio.Event()

        async def stream(messages: list[ModelMessage], info: Any) -> Any:
            yield "answer"
            await gate.wait()

        self._patch_agent(stream)
        user = await self.make_user("delete-race-owner")
        thread = await self.make_thread(user)
        first_client = await self.client_for(user)
        second_client = await self.client_for(user)
        token = await self.token(first_client)

        response = await self.send_turn(first_client, token, thread.id, "hello", 0)
        iterator = response.streaming_content.__aiter__()
        await anext(iterator)
        await next_frame(iterator, "delta")

        deleted = await second_client.delete(
            f"/api/threads/{thread.id}/",
            data='{"revision": 0}',
            content_type="application/json",
            headers={"X-CSRFToken": token},
        )
        self.assertEqual(deleted.status_code, 200)

        gate.set()
        parsed = events(await read_stream(iterator))
        await sync_to_async(response.close)()
        self.assertEqual(parsed[-1][0], "error")
        self.assertEqual(parsed[-1][1]["error"]["code"], "thread_not_found")
        self.assertFalse(await self.row_exists(thread.id))

    async def test_logout_during_generation_does_not_make_a_session(self) -> None:
        gate = asyncio.Event()

        async def stream(messages: list[ModelMessage], info: Any) -> Any:
            yield "answer"
            await gate.wait()

        self._patch_agent(stream)
        user = await self.make_user("logout-race-owner")
        thread = await self.make_thread(user)
        stream_client = await self.client_for(user)
        other_client = await self.client_for(user)
        token = await self.token(other_client)

        response = await self.send_turn(stream_client, token, thread.id, "hello", 0)
        iterator = response.streaming_content.__aiter__()
        await anext(iterator)
        await next_frame(iterator, "delta")

        logged_out = await other_client.post(
            "/accounts/logout/",
            headers={"X-CSRFToken": token},
        )
        self.assertEqual(logged_out.status_code, 302)
        self.assertEqual((await other_client.get("/")).status_code, 302)

        gate.set()
        parsed = events(await read_stream(iterator))
        await sync_to_async(response.close)()
        self.assertEqual(parsed[-1][0], "done")

        row = await self.fetch_row(thread.id)
        self.assertEqual(row.owner_id, user.pk)
        self.assertEqual(len(row.display), 2)
        self.assertEqual((await stream_client.get("/")).status_code, 200)


@override_settings(**PROVIDER_SETTINGS)
class TurnActionTests(ChatHttpTestCase):
    """Regeneration and pair removal against real stored turns."""

    def counting_agent(self) -> Any:
        calls = {"count": 0}

        async def stream(messages: list[ModelMessage], info: Any) -> Any:
            calls["count"] += 1
            yield f"answer {calls['count']}"

        return self._patch_agent(stream)

    async def two_turns(self) -> Any:
        self.counting_agent()
        user = await self.make_user("turn-action-owner")
        thread = await self.make_thread(user)
        client = await self.client_for(user)
        token = await self.token(client)
        await self.completed_turn(client, token, thread.id, "first prompt", 0)
        payload, revision = await self.completed_turn(
            client, token, thread.id, "second prompt", 1
        )

        class Fixture:
            pass

        fixture = Fixture()
        fixture.user = user
        fixture.thread = thread
        fixture.client = client
        fixture.token = token
        fixture.revision = revision
        fixture.turn = payload["turn"]
        fixture.rows = await self.chat_turns(thread.id)
        return fixture

    async def test_regenerate_replaces_only_the_latest_answer(self) -> None:
        fixture = await self.two_turns()
        before = await self.fetch_row(fixture.thread.id)
        later = await self.client_for(fixture.user)

        response = await self.regenerate(
            later,
            fixture.token,
            fixture.thread.id,
            fixture.turn["turn_id"],
            fixture.revision,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "text/event-stream")
        body = await read_body(response)
        await sync_to_async(response.close)()
        parsed = events(body)

        self.assertEqual(parsed[-1][0], "done")
        done = parsed[-1][1]
        self.assertEqual(done["reply"], "answer 3")
        self.assertEqual(done["turn"]["turn_id"], fixture.turn["turn_id"])
        self.assertEqual(done["turn"]["turn_index"], 1)
        self.assertEqual(done["thread"]["revision"], fixture.revision + 1)
        self.assertEqual(done["thread"]["title"], before.title)

        after = await self.fetch_row(fixture.thread.id)
        self.assertEqual(after.revision, fixture.revision + 1)
        self.assertEqual(after.title, before.title)
        self.assertEqual(
            [entry["content"] for entry in after.display],
            ["first prompt", "answer 1", "second prompt", "answer 3"],
        )
        self.assertEqual(
            [entry["role"] for entry in after.display],
            ["user", "assistant", "user", "assistant"],
        )

        rows = await self.chat_turns(fixture.thread.id)
        self.assertEqual([row["turn_index"] for row in rows], [0, 1])
        self.assertEqual(
            rows[0]["pre_history_json"], fixture.rows[0]["pre_history_json"]
        )
        self.assertEqual(
            rows[1]["pre_history_json"], fixture.rows[1]["pre_history_json"]
        )
        self.assertEqual(str(rows[1]["id"]), fixture.turn["turn_id"])
        self.assertEqual(str(rows[0]["id"]), rows[0]["id"].hex and str(rows[0]["id"]))
        service.load_history(after.history_json)

        detail = await self.detail(fixture.client, fixture.thread.id)
        self.assertEqual(len(detail["turns"]), 2)
        self.assertTrue(detail["turns"][1]["can_regenerate"])
        self.assertFalse(detail["turns"][0]["can_regenerate"])
        self.assertEqual(detail["display"], after.display)

    async def test_regeneration_prompt_and_history_come_from_the_database(self) -> None:
        fixture = await self.two_turns()
        factory = fixture.factory = self._patch_agent(self._answer("replacement"))
        row = await self.fetch_row(fixture.thread.id)

        await self.regenerated(
            fixture.client,
            fixture.token,
            fixture.thread.id,
            fixture.turn["turn_id"],
            fixture.revision,
        )

        seen = factory.seen[-1]
        prompts = prompt_texts(seen)
        replies = reply_texts(seen)
        self.assertEqual(prompts, ["first prompt", "second prompt"])
        self.assertIn("answer 1", replies)
        self.assertNotIn("answer 2", replies)
        for message in seen:
            for part in getattr(message, "parts", []):
                self.assertNotIsInstance(part, ToolReturnPart)
        self.assertEqual(row.display[-1]["content"], "answer 2")

    @staticmethod
    def _answer(text: str) -> Any:
        def stream(messages: list[ModelMessage], info: Any) -> Any:
            async def produce() -> Any:
                yield text

            return produce()

        return stream

    async def test_regeneration_failure_keeps_the_old_answer(self) -> None:
        fixture = await self.two_turns()
        before = await self.fetch_row(fixture.thread.id)
        rows_before = await self.chat_turns(fixture.thread.id)

        def failing(messages: list[ModelMessage], info: Any) -> Any:
            async def produce() -> Any:
                yield "partial replacement"
                raise ModelHTTPError(500, "offline-model", body={"detail": "boom"})

            return produce()

        self._patch_agent(failing)
        with patch.object(service, "STREAM_DEBOUNCE_SECONDS", None):
            body, parsed = await self.regenerated(
                fixture.client,
                fixture.token,
                fixture.thread.id,
                fixture.turn["turn_id"],
                fixture.revision,
            )

        self.assertEqual(parsed[-1][0], "error")
        self.assertEqual(parsed[-1][1]["error"]["code"], "provider_error")
        self.assertNotIn(b"event: done", body)

        after = await self.fetch_row(fixture.thread.id)
        self.assertEqual(self.thread_state(after), self.thread_state(before))
        self.assertEqual(await self.chat_turns(fixture.thread.id), rows_before)

    async def test_historical_turn_cannot_be_regenerated(self) -> None:
        fixture = await self.two_turns()
        first_turn = fixture.rows[0]["id"]

        response = await self.regenerate(
            fixture.client,
            fixture.token,
            fixture.thread.id,
            first_turn,
            fixture.revision,
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.error_code(response), "turn_unavailable")

    async def test_delete_removes_the_pair_and_restores_the_checkpoint(self) -> None:
        fixture = await self.two_turns()
        before = await self.fetch_row(fixture.thread.id)

        response = await self.delete_exchange(
            fixture.client,
            fixture.token,
            fixture.thread.id,
            fixture.turn["turn_id"],
            fixture.revision,
        )
        self.assertEqual(response.status_code, 200)
        detail = response.json()["thread"]
        self.assertEqual(
            detail["display"],
            [
                {"role": "user", "content": "first prompt"},
                {"role": "assistant", "content": "answer 1"},
            ],
        )
        self.assertEqual(detail["revision"], fixture.revision + 1)
        self.assertEqual(len(detail["turns"]), 1)
        self.assertTrue(detail["turns"][0]["can_delete"])
        self.assertEqual(detail["title"], before.title)

        after = await self.fetch_row(fixture.thread.id)
        self.assertEqual(after.history_json, fixture.rows[1]["pre_history_json"])
        service.load_history(after.history_json)
        rows = await self.chat_turns(fixture.thread.id)
        self.assertEqual([row["turn_index"] for row in rows], [0])

        reloaded = await self.detail(fixture.client, fixture.thread.id)
        self.assertEqual(reloaded["display"], detail["display"])
        self.assertEqual(reloaded["turns"], detail["turns"])

    async def test_delete_then_send_uses_the_restored_history(self) -> None:
        fixture = await self.two_turns()
        await self.delete_exchange(
            fixture.client,
            fixture.token,
            fixture.thread.id,
            fixture.turn["turn_id"],
            fixture.revision,
        )
        row = await self.fetch_row(fixture.thread.id)

        factory = self._patch_agent(self._answer("continuing"))
        await self.completed_turn(
            fixture.client,
            fixture.token,
            fixture.thread.id,
            "third prompt",
            row.revision,
        )

        prompts = prompt_texts(factory.seen[-1])
        self.assertEqual(prompts, ["first prompt", "third prompt"])
        self.assertNotIn("second prompt", prompts)
        final = await self.fetch_row(fixture.thread.id)
        self.assertEqual(len(final.display), 4)
        self.assertEqual(len(await self.chat_turns(fixture.thread.id)), 2)

    async def test_delete_reaches_an_empty_conversation(self) -> None:
        fixture = await self.two_turns()
        first_revision = fixture.revision
        await self.delete_exchange(
            fixture.client,
            fixture.token,
            fixture.thread.id,
            fixture.turn["turn_id"],
            first_revision,
        )
        row = await self.fetch_row(fixture.thread.id)
        detail = await self.detail(fixture.client, fixture.thread.id)

        response = await self.delete_exchange(
            fixture.client,
            fixture.token,
            fixture.thread.id,
            detail["turns"][0]["turn_id"],
            row.revision,
        )
        self.assertEqual(response.status_code, 200)
        emptied = response.json()["thread"]
        self.assertEqual(emptied["display"], [])
        self.assertEqual(emptied["turns"], [])
        self.assertEqual(emptied["title"], row.title)

        final = await self.fetch_row(fixture.thread.id)
        self.assertEqual(final.history_json, "[]")
        self.assertEqual(final.revision, first_revision + 2)
        self.assertEqual(await self.chat_turns(fixture.thread.id), [])

    async def test_delete_and_regenerate_at_the_turn_limit(self) -> None:
        self.counting_agent()
        user = await self.make_user("turn-limit-actions")
        thread = await self.make_thread(user)
        client = await self.client_for(user)
        token = await self.token(client)

        revision = 0
        for index in range(threads.MAX_TURNS):
            _payload, revision = await self.completed_turn(
                client, token, thread.id, f"turn {index}", revision
            )
        detail = await self.detail(client, thread.id)
        latest = detail["turns"][-1]
        self.assertTrue(latest["can_regenerate"])

        replaced = await self.regenerated(
            client, token, thread.id, latest["turn_id"], revision
        )
        done = replaced[1][-1]
        self.assertEqual(done[0], "done")
        self.assertEqual(done[1]["turn"]["turn_id"], latest["turn_id"])
        self.assertEqual(done[1]["thread"]["revision"], revision + 1)

        newest = done[1]["turn"]
        response = await self.delete_exchange(
            client, token, thread.id, newest["turn_id"], revision + 1
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["thread"]["display"]), 38)

    async def test_oversized_thread_can_still_be_edited(self) -> None:
        fixture = await self.two_turns()
        row = await self.fetch_row(fixture.thread.id)
        payload = threads.payload_bytes(row.history_json, row.display)
        checkpoint = len(fixture.rows[1]["pre_history_json"].encode("utf-8"))
        limit = max(checkpoint, payload - 1)

        with patch.object(threads, "MAX_HISTORY_BYTES", limit):
            # A new send is over budget while the stored checkpoints still fit.
            blocked = await self.send_turn(
                fixture.client,
                fixture.token,
                fixture.thread.id,
                "one more",
                fixture.revision,
            )
            self.assertEqual(blocked.status_code, 400)
            self.assertEqual(self.error_code(blocked), "conversation_limit")

            detail = await self.detail(fixture.client, fixture.thread.id)
            self.assertTrue(detail["turns"][-1]["can_delete"])
            self.assertTrue(detail["turns"][-1]["can_regenerate"])

            response = await self.delete_exchange(
                fixture.client,
                fixture.token,
                fixture.thread.id,
                fixture.turn["turn_id"],
                fixture.revision,
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["thread"]["display"]), 2)

    async def test_delete_needs_no_provider_but_regenerate_does(self) -> None:
        fixture = await self.two_turns()
        with override_settings(LLM_BASE_URL="", LLM_API_KEY="", LLM_MODEL=""):
            blocked = await self.regenerate(
                fixture.client,
                fixture.token,
                fixture.thread.id,
                fixture.turn["turn_id"],
                fixture.revision,
            )
            self.assertEqual(blocked.status_code, 503)
            self.assertEqual(self.error_code(blocked), "provider_not_configured")

            allowed = await self.delete_exchange(
                fixture.client,
                fixture.token,
                fixture.thread.id,
                fixture.turn["turn_id"],
                fixture.revision,
            )
            self.assertEqual(allowed.status_code, 200)

    async def test_turn_bodies_are_exact_and_revisions_are_required(self) -> None:
        fixture = await self.two_turns()
        turn_id = fixture.turn["turn_id"]
        regenerate_url = f"/api/threads/{fixture.thread.id}/turns/{turn_id}/regenerate/"
        delete_url = f"/api/threads/{fixture.thread.id}/turns/{turn_id}/"

        cases = {
            "empty object": "{}",
            "missing revision": '{"message": "hi"}',
            "boolean revision": '{"revision": true}',
            "negative revision": '{"revision": -1}',
            "string revision": '{"revision": "2"}',
            "float revision": '{"revision": 1.5}',
            "extra key": '{"revision": 2, "message": "hi"}',
            "checkpoint key": '{"revision": 2, "pre_history_json": "[]"}',
            "not json": "{oops",
            "array body": "[]",
        }
        for label, body in cases.items():
            with self.subTest(case=label, endpoint="regenerate"):
                response = await fixture.client.post(
                    regenerate_url,
                    data=body,
                    content_type="application/json",
                    headers={"X-CSRFToken": fixture.token},
                )
                self.assertEqual(response.status_code, 400)
                self.assertEqual(self.error_code(response), "invalid_turn")
            with self.subTest(case=label, endpoint="delete"):
                response = await fixture.client.delete(
                    delete_url,
                    data=body,
                    content_type="application/json",
                    headers={"X-CSRFToken": fixture.token},
                )
                self.assertEqual(response.status_code, 400)
                self.assertEqual(self.error_code(response), "invalid_turn")

        stale = await self.delete_exchange(
            fixture.client, fixture.token, fixture.thread.id, turn_id, 0
        )
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(self.error_code(stale), "thread_conflict")

    async def test_missing_and_foreign_turns_are_the_same_404(self) -> None:
        fixture = await self.two_turns()
        stranger = await self.make_user("turn-stranger")
        stranger_client = await self.client_for(stranger)
        stranger_thread = await self.make_thread(stranger)
        stranger_token = await self.token(stranger_client)
        await self.completed_turn(
            stranger_client, stranger_token, stranger_thread.id, "hi", 0
        )
        stranger_rows = await self.chat_turns(stranger_thread.id)

        cases = {
            "missing": (
                fixture.client,
                fixture.token,
                fixture.thread.id,
                "2f9d4b1e-6c3a-4f5b-8e7d-0a1b2c3d4e5f",
                "turn_not_found",
            ),
            "malformed": (
                fixture.client,
                fixture.token,
                fixture.thread.id,
                "not-a-uuid",
                "turn_not_found",
            ),
            "foreign thread": (
                stranger_client,
                stranger_token,
                fixture.thread.id,
                fixture.turn["turn_id"],
                "thread_not_found",
            ),
            "foreign turn": (
                fixture.client,
                fixture.token,
                fixture.thread.id,
                str(stranger_rows[0]["id"]),
                "turn_not_found",
            ),
        }
        for label, (client, token, thread_id, turn_id, code) in cases.items():
            with self.subTest(case=label):
                response = await self.regenerate(
                    client, token, thread_id, turn_id, fixture.revision
                )
                self.assertEqual(response.status_code, 404)
                self.assertEqual(self.error_code(response), code)

                deleted = await self.delete_exchange(
                    client, token, thread_id, turn_id, fixture.revision
                )
                self.assertEqual(deleted.status_code, 404)
                self.assertEqual(self.error_code(deleted), code)

    async def test_turn_routes_enforce_csrf_and_methods(self) -> None:
        fixture = await self.two_turns()
        turn_id = fixture.turn["turn_id"]
        guarded = AsyncClient(enforce_csrf_checks=True)
        await guarded.aforce_login(fixture.user)

        without_token = await guarded.post(
            f"/api/threads/{fixture.thread.id}/turns/{turn_id}/regenerate/",
            data='{"revision": 2}',
            content_type="application/json",
        )
        self.assertEqual(without_token.status_code, 403)
        self.assertEqual(self.error_code(without_token), "csrf_failed")

        without_token = await guarded.delete(
            f"/api/threads/{fixture.thread.id}/turns/{turn_id}/",
            data='{"revision": 2}',
            content_type="application/json",
        )
        self.assertEqual(without_token.status_code, 403)
        self.assertEqual(self.error_code(without_token), "csrf_failed")

        cases = {
            "regenerate": (
                f"/api/threads/{fixture.thread.id}/turns/{turn_id}/regenerate/",
                "GET",
                "POST",
            ),
            "delete": (
                f"/api/threads/{fixture.thread.id}/turns/{turn_id}/",
                "POST",
                "DELETE",
            ),
        }
        for label, (url, method, allow) in cases.items():
            with self.subTest(endpoint=label):
                response = await fixture.client.generic(method, url)
                self.assertEqual(response.status_code, 405)
                self.assertEqual(response["Allow"], allow)
                self.assertEqual(self.error_code(response), "method_not_allowed")

    async def test_activity_frames_expose_only_public_enums(self) -> None:
        calls: list[str] = []
        toolset: FunctionToolset[None] = FunctionToolset()

        @toolset.tool
        def run_sql(ctx: RunContext[None], sql: str = "select 1") -> int:
            """Run one read-only statement."""
            calls.append(sql)
            return 99

        async def stream(messages: list[ModelMessage], info: Any) -> Any:
            returned = [
                part
                for message in messages
                for part in getattr(message, "parts", [])
                if isinstance(part, ToolReturnPart)
            ]
            if not returned:
                yield "Checking. "
                yield {
                    0: DeltaToolCall(
                        name="run_sql",
                        json_args='{"sql":"select 424242"}',
                        tool_call_id="call-secret-id",
                    )
                }
                return
            yield "99 rows."

        self._patch_agent(stream, toolsets=(toolset,))
        user = await self.make_user("activity-frame-owner")
        thread = await self.make_thread(user)
        client = await self.client_for(user)
        token = await self.token(client)

        with patch.object(service, "STREAM_DEBOUNCE_SECONDS", None):
            body, parsed = await self.turn(client, token, thread.id, "count rows", 0)

        updates = named(parsed, "activity")
        self.assertEqual(
            [(update["step"]["kind"], update["step"]["status"]) for update in updates],
            [
                ("model", "running"),
                ("model", "complete"),
                ("wren_tool", "running"),
                ("wren_tool", "complete"),
            ],
        )
        self.assertEqual(named(parsed, "done"), [])
        self.assertEqual(
            named(parsed, "error")[0]["error"]["code"], "authorization_changed"
        )
        for update in updates:
            self.assertEqual(set(update), {"turn_id", "step", "truncated"})
            self.assertEqual(set(update["step"]), {"id", "kind", "status"})

        frame_text = body.decode("utf-8")
        for secret in ("run_sql", "select 424242", "call-secret-id"):
            with self.subTest(secret=secret):
                self.assertNotIn(secret, json.dumps(updates))
                self.assertNotIn(f'"step":{{"id":0,"kind":"{secret}', frame_text)
        self.assertEqual(calls, ["select 424242"])


@override_settings(**PROVIDER_SETTINGS)
class LeaseTests(ChatHttpTestCase):
    """The short run lease that serializes send, regenerate, and delete-turn."""

    async def held_run(self) -> Any:
        """Start a send that stays in flight until the caller releases it."""
        gate = asyncio.Event()
        calls = {"count": 0}

        async def stream(messages: list[ModelMessage], info: Any) -> Any:
            calls["count"] += 1
            if calls["count"] == 1:
                yield "settled reply"
                return
            yield "in flight"
            await gate.wait()

        self._patch_agent(stream)
        user = await self.make_user("lease-owner")
        thread = await self.make_thread(user)
        client = await self.client_for(user)
        token = await self.token(client)
        await self.completed_turn(client, token, thread.id, "settled prompt", 0)

        response = await self.send_turn(client, token, thread.id, "running prompt", 1)
        iterator = response.streaming_content.__aiter__()
        await anext(iterator)
        await next_frame(iterator, "delta")

        class Fixture:
            pass

        fixture = Fixture()
        fixture.gate = gate
        fixture.response = response
        fixture.iterator = iterator
        fixture.user = user
        fixture.thread = thread
        fixture.client = client
        fixture.token = token
        return fixture

    async def test_delete_turn_is_refused_while_a_run_is_active(self) -> None:
        fixture = await self.held_run()
        detail = await self.detail(fixture.client, fixture.thread.id)

        response = await self.delete_exchange(
            fixture.client,
            fixture.token,
            fixture.thread.id,
            detail["turns"][0]["turn_id"],
            detail["revision"],
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.error_code(response), "thread_busy")

        fixture.gate.set()
        await read_stream(fixture.iterator)
        await sync_to_async(fixture.response.close)()

    async def test_regenerate_is_refused_while_a_run_is_active(self) -> None:
        fixture = await self.held_run()
        detail = await self.detail(fixture.client, fixture.thread.id)

        response = await self.regenerate(
            fixture.client,
            fixture.token,
            fixture.thread.id,
            detail["turns"][0]["turn_id"],
            detail["revision"],
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.error_code(response), "thread_busy")

        fixture.gate.set()
        await read_stream(fixture.iterator)
        await sync_to_async(fixture.response.close)()

    async def test_expired_lease_is_reclaimable(self) -> None:
        self._echo_agent()
        user = await self.make_user("lease-expiry-owner")
        thread = await self.make_thread(user)
        client = await self.client_for(user)
        token = await self.token(client)

        await sync_to_async(ChatThread.objects.filter(pk=thread.id).update)(
            active_run_token=uuid.uuid4(),
            active_run_expires_at=timezone.now() - timedelta(seconds=1),
        )
        await self.completed_turn(client, token, thread.id, "after expiry", 0)

        row = await self.fetch_row(thread.id)
        self.assertEqual(len(row.display), 2)
        self.assertIsNone(row.active_run_token)
        self.assertIsNone(row.active_run_expires_at)

    async def test_wrong_token_release_cannot_unlock_a_successor(self) -> None:
        user = await self.make_user("lease-token-owner")
        thread = await self.make_thread(user)

        first = await sync_to_async(threads.acquire_run)(
            owner_id=user.pk, thread_id=thread.id, revision=0
        )
        await sync_to_async(ChatThread.objects.filter(pk=thread.id).update)(
            active_run_expires_at=timezone.now() - timedelta(seconds=1)
        )
        second = await sync_to_async(threads.acquire_run)(
            owner_id=user.pk, thread_id=thread.id, revision=0
        )
        self.assertNotEqual(first, second)

        await sync_to_async(threads.release_run)(
            owner_id=user.pk, thread_id=thread.id, run_token=first
        )
        row = await self.fetch_row(thread.id)
        self.assertEqual(row.active_run_token, second)

        await sync_to_async(threads.release_run)(
            owner_id=user.pk, thread_id=thread.id, run_token=second
        )
        row = await self.fetch_row(thread.id)
        self.assertIsNone(row.active_run_token)

    async def test_stale_worker_cannot_commit(self) -> None:
        self._echo_agent()
        user = await self.make_user("lease-stale-owner")
        thread = await self.make_thread(user)
        snapshot = await self.fetch_row(thread.id)

        run_token = await sync_to_async(threads.acquire_run)(
            owner_id=user.pk, thread_id=thread.id, revision=snapshot.revision
        )
        await sync_to_async(threads.rename_thread)(
            owner_id=user.pk,
            thread_id=thread.id,
            revision=snapshot.revision,
            title="Renamed mid-run",
        )

        completed = service.CompletedReply(reply="lost reply", history_json="[]")
        with self.assertRaises(service.ChatError) as caught:
            await sync_to_async(threads.commit_turn)(
                owner_id=user.pk,
                snapshot=snapshot,
                message="running prompt",
                completed=completed,
                turn_id=uuid.uuid4(),
                run_token=run_token,
            )
        self.assertEqual(caught.exception.code, "thread_conflict")

        row = await self.fetch_row(thread.id)
        self.assertEqual(row.title, "Renamed mid-run")
        self.assertEqual(row.display, [])
        self.assertEqual(row.revision, 1)
        self.assertEqual(await self.chat_turns(thread.id), [])

    async def test_renewal_keeps_a_silent_run_alive(self) -> None:
        gate = asyncio.Event()

        async def stream(messages: list[ModelMessage], info: Any) -> Any:
            yield "slow"
            await gate.wait()

        self._patch_agent(stream)
        user = await self.make_user("lease-renew-owner")
        thread = await self.make_thread(user)
        client = await self.client_for(user)
        token = await self.token(client)

        with patch.object(threads, "RUN_RENEW_INTERVAL_SECONDS", 0.05):
            response = await self.send_turn(client, token, thread.id, "hello", 0)
            iterator = response.streaming_content.__aiter__()
            await anext(iterator)
            await next_frame(iterator, "delta")

            first = await self.fetch_row(thread.id)
            await asyncio.sleep(0.3)
            second = await self.fetch_row(thread.id)

            self.assertIsNotNone(first.active_run_expires_at)
            self.assertGreater(
                second.active_run_expires_at, first.active_run_expires_at
            )

            gate.set()
            parsed = events(await read_stream(iterator))
            await sync_to_async(response.close)()

        self.assertEqual(parsed[-1][0], "done")
        row = await self.fetch_row(thread.id)
        self.assertIsNone(row.active_run_token)

    async def test_lost_renewal_stops_the_run_without_committing(self) -> None:
        never = asyncio.Event()

        async def stream(messages: list[ModelMessage], info: Any) -> Any:
            yield "first"
            await never.wait()

        self._patch_agent(stream)
        user = await self.make_user("lease-lost-owner")
        thread = await self.make_thread(user)
        client = await self.client_for(user)
        token = await self.token(client)

        with (
            patch.object(threads, "RUN_RENEW_INTERVAL_SECONDS", 0.05),
            patch.object(threads, "renew_run", lambda **kwargs: False),
        ):
            body, parsed = await self.turn(client, token, thread.id, "hello", 0)

        self.assertEqual(parsed[-1][0], "error")
        self.assertEqual(parsed[-1][1]["error"]["code"], "thread_busy")
        self.assertNotIn(b"event: done", body)

        row = await self.fetch_row(thread.id)
        self.assertEqual(row.display, [])
        self.assertEqual(row.revision, 0)
        self.assertEqual(await self.chat_turns(thread.id), [])

    async def test_provider_failure_releases_the_lease(self) -> None:
        async def stream(messages: list[ModelMessage], info: Any) -> Any:
            raise ModelHTTPError(500, "offline-model", body={"detail": "boom"})
            yield ""

        self._patch_agent(stream)
        user = await self.make_user("lease-failure-owner")
        thread = await self.make_thread(user)
        client = await self.client_for(user)
        token = await self.token(client)

        _body, parsed = await self.turn(client, token, thread.id, "hello", 0)
        self.assertEqual(parsed[-1][1]["error"]["code"], "provider_error")

        row = await self.fetch_row(thread.id)
        self.assertIsNone(row.active_run_token)
        self.assertIsNone(row.active_run_expires_at)
        self.assertEqual(row.display, [])

    async def test_closing_the_turn_stream_releases_the_lease(self) -> None:
        gate = asyncio.Event()

        async def stream(messages: list[ModelMessage], info: Any) -> Any:
            try:
                yield "partial"
                await gate.wait()
                yield " never"
            finally:
                gate.set()

        factory = self._patch_agent(stream)
        user = await self.make_user("lease-cancel-owner")
        thread = await self.make_thread(user)
        snapshot = await self.fetch_row(thread.id)
        run_token = await sync_to_async(threads.acquire_run)(
            owner_id=user.pk, thread_id=thread.id, revision=snapshot.revision
        )

        turn = views._stream_turn(
            owner_id=user.pk,
            snapshot=snapshot,
            mode="send",
            turn_request=views.TurnRequest(
                message="hello",
                history_json="[]",
                attachments=(),
                stored={},
                limits=attachments.attachment_limits(),
            ),
            turn_id=uuid.uuid4(),
            run_token=run_token,
            turn=None,
        )
        self.assertEqual(await anext(turn), CONNECTED)
        async for chunk in turn:
            if chunk.startswith(b"event: delta"):
                break
        await turn.aclose()

        self.assertTrue(gate.is_set())
        self.assertEqual(factory.agents[0].exited, 1)
        row = await self.fetch_row(thread.id)
        self.assertIsNone(row.active_run_token)
        self.assertIsNone(row.active_run_expires_at)
        self.assertEqual(row.display, [])
        self.assertEqual(await self.chat_turns(thread.id), [])

    def _blocking_agent(self) -> tuple[asyncio.Event, asyncio.Event]:
        """Patch a provider that reports entry, then blocks until cancelled."""
        started = asyncio.Event()
        finished = asyncio.Event()
        gate = asyncio.Event()

        async def stream(messages: list[ModelMessage], info: Any) -> Any:
            started.set()
            try:
                await gate.wait()
                yield "never"
            finally:
                finished.set()

        self._patch_agent(stream)
        return started, finished

    async def _park_inside_provider(
        self,
        iterator: Any,
        started: asyncio.Event,
    ) -> None:
        """Consume the queued activity frame, then confirm the provider is live.

        The first model round publishes its generic ``model`` step before the
        provider is asked for text, so cancelling the pull right after the
        connected frame lands on an already-finished future and leaves the
        generator suspended. ``StreamingHttpResponse.close()`` only runs
        synchronous closers, so nothing would ever close that suspension; the
        pull after the activity frame instead parks inside the provider stream.
        """
        await next_frame(iterator, "activity")
        await asyncio.wait_for(started.wait(), timeout=10)

    async def _disconnect_after_connected(
        self,
        *,
        client: AsyncClient,
        token: str,
        path: str,
        payload: dict[str, Any],
        started: asyncio.Event,
    ) -> None:
        """Drive the real ASGI handler, then drop the client after connected."""
        cookie = "; ".join(
            [
                f"{settings.SESSION_COOKIE_NAME}="
                f"{client.cookies[settings.SESSION_COOKIE_NAME].value}",
                f"{settings.CSRF_COOKIE_NAME}="
                f"{client.cookies[settings.CSRF_COOKIE_NAME].value}",
            ]
        )
        body = json.dumps(payload).encode()
        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.1"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "root_path": "",
            "headers": [
                (b"host", b"testserver"),
                (b"cookie", cookie.encode()),
                (b"x-csrftoken", token.encode()),
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
            "client": ("127.0.0.1", 47123),
            "server": ("testserver", 80),
        }
        communicator = ApplicationCommunicator(get_asgi_application(), scope)
        try:
            await communicator.send_input(
                {"type": "http.request", "body": body, "more_body": False}
            )
            start = await asyncio.wait_for(communicator.receive_output(1), timeout=10)
            self.assertEqual(start["type"], "http.response.start")
            self.assertEqual(start["status"], 200)
            connected = await asyncio.wait_for(
                communicator.receive_output(1), timeout=10
            )
            self.assertIn(CONNECTED.strip(), connected["body"])
            await asyncio.wait_for(started.wait(), timeout=10)
            await communicator.send_input({"type": "http.disconnect"})
            with contextlib.suppress(asyncio.CancelledError, TimeoutError):
                await asyncio.wait_for(communicator.wait(), timeout=10)
        finally:
            with contextlib.suppress(Exception):
                await communicator.aclose()

    async def test_closing_an_unstarted_send_response_releases_the_lease(
        self,
    ) -> None:
        factory = self._echo_agent()
        user = await self.make_user("lease-unstarted-send")
        thread = await self.make_thread(user)
        client = await self.client_for(user)
        token = await self.token(client)

        response = await self.send_turn(client, token, thread.id, "hello", 0)
        self.assertEqual(response.status_code, 200)
        held = await self.fetch_row(thread.id)
        self.assertIsNotNone(held.active_run_token)

        await sync_to_async(response.close)()

        row = await self.fetch_row(thread.id)
        self.assertIsNone(row.active_run_token)
        self.assertIsNone(row.active_run_expires_at)
        self.assertEqual(row.display, [])
        self.assertEqual(row.revision, 0)
        self.assertEqual(await self.chat_turns(thread.id), [])
        self.assertEqual(factory.agents, [])
        self.assertEqual(factory.seen, [])

    async def test_closing_an_unstarted_regenerate_response_releases_the_lease(
        self,
    ) -> None:
        self._echo_agent()
        user = await self.make_user("lease-unstarted-regenerate")
        thread = await self.make_thread(user)
        client = await self.client_for(user)
        token = await self.token(client)
        await self.completed_turn(client, token, thread.id, "first", 0)
        detail = await self.detail(client, thread.id)

        response = await self.regenerate(
            client,
            token,
            thread.id,
            detail["turns"][0]["turn_id"],
            detail["revision"],
        )
        self.assertEqual(response.status_code, 200)
        held = await self.fetch_row(thread.id)
        self.assertIsNotNone(held.active_run_token)

        await sync_to_async(response.close)()

        row = await self.fetch_row(thread.id)
        self.assertIsNone(row.active_run_token)
        self.assertIsNone(row.active_run_expires_at)
        self.assertEqual(row.revision, detail["revision"])
        self.assertEqual(len(row.display), 2)
        self.assertEqual(len(await self.chat_turns(thread.id)), 1)

    async def test_initialization_failure_releases_the_send_lease(self) -> None:
        factory = self._echo_agent()
        user = await self.make_user("lease-init-send")
        thread = await self.make_thread(user)
        client = await self.client_for(user)
        token = await self.token(client)

        response = await self.send_turn(client, token, thread.id, "hello", 0)
        self.assertEqual(response.status_code, 200)
        iterator = response.streaming_content.__aiter__()
        self.assertEqual(await anext(iterator), CONNECTED)
        with (
            patch.object(
                service, "load_grant_instructions", side_effect=RuntimeError("boom")
            ),
            self.assertRaises(RuntimeError),
        ):
            await anext(iterator)
        await sync_to_async(response.close)()

        self.assertEqual(factory.agents, [])
        row = await self.fetch_row(thread.id)
        self.assertIsNone(row.active_run_token)
        self.assertIsNone(row.active_run_expires_at)
        self.assertEqual(row.display, [])
        self.assertEqual(await self.chat_turns(thread.id), [])

    async def test_initialization_failure_releases_the_regenerate_lease(
        self,
    ) -> None:
        self._echo_agent()
        user = await self.make_user("lease-init-regenerate")
        thread = await self.make_thread(user)
        client = await self.client_for(user)
        token = await self.token(client)
        await self.completed_turn(client, token, thread.id, "first", 0)
        detail = await self.detail(client, thread.id)

        response = await self.regenerate(
            client,
            token,
            thread.id,
            detail["turns"][0]["turn_id"],
            detail["revision"],
        )
        self.assertEqual(response.status_code, 200)
        iterator = response.streaming_content.__aiter__()
        self.assertEqual(await anext(iterator), CONNECTED)
        with (
            patch.object(
                service, "load_grant_instructions", side_effect=RuntimeError("boom")
            ),
            self.assertRaises(RuntimeError),
        ):
            await anext(iterator)
        await sync_to_async(response.close)()

        row = await self.fetch_row(thread.id)
        self.assertIsNone(row.active_run_token)
        self.assertIsNone(row.active_run_expires_at)
        self.assertEqual(row.revision, detail["revision"])
        self.assertEqual(len(row.display), 2)
        self.assertEqual(len(await self.chat_turns(thread.id)), 1)

    async def test_cancelling_the_send_stream_releases_the_lease(self) -> None:
        started, finished = self._blocking_agent()
        user = await self.make_user("lease-cancel-send")
        thread = await self.make_thread(user)
        client = await self.client_for(user)
        token = await self.token(client)

        response = await self.send_turn(client, token, thread.id, "hello", 0)
        self.assertEqual(response.status_code, 200)
        iterator = response.streaming_content.__aiter__()
        self.assertEqual(await anext(iterator), CONNECTED)

        await self._park_inside_provider(iterator, started)

        consumer = asyncio.ensure_future(anext(iterator))
        await asyncio.sleep(0)
        self.assertFalse(consumer.done())
        consumer.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await consumer
        await sync_to_async(response.close)()

        self.assertTrue(finished.is_set())
        row = await self.fetch_row(thread.id)
        self.assertIsNone(row.active_run_token)
        self.assertIsNone(row.active_run_expires_at)
        self.assertEqual(row.display, [])
        self.assertEqual(await self.chat_turns(thread.id), [])

    async def test_cancelling_the_regenerate_stream_releases_the_lease(
        self,
    ) -> None:
        self._echo_agent()
        user = await self.make_user("lease-cancel-regenerate")
        thread = await self.make_thread(user)
        client = await self.client_for(user)
        token = await self.token(client)
        await self.completed_turn(client, token, thread.id, "first", 0)
        detail = await self.detail(client, thread.id)
        started, finished = self._blocking_agent()

        response = await self.regenerate(
            client,
            token,
            thread.id,
            detail["turns"][0]["turn_id"],
            detail["revision"],
        )
        self.assertEqual(response.status_code, 200)
        iterator = response.streaming_content.__aiter__()
        self.assertEqual(await anext(iterator), CONNECTED)

        await self._park_inside_provider(iterator, started)

        consumer = asyncio.ensure_future(anext(iterator))
        await asyncio.sleep(0)
        self.assertFalse(consumer.done())
        consumer.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await consumer
        await sync_to_async(response.close)()

        self.assertTrue(finished.is_set())
        row = await self.fetch_row(thread.id)
        self.assertIsNone(row.active_run_token)
        self.assertIsNone(row.active_run_expires_at)
        self.assertEqual(row.revision, detail["revision"])
        self.assertEqual(len(row.display), 2)
        self.assertEqual(len(await self.chat_turns(thread.id)), 1)

    async def test_immediate_disconnect_releases_the_send_lease(self) -> None:
        started, finished = self._blocking_agent()
        user = await self.make_user("lease-disconnect-send")
        thread = await self.make_thread(user)
        client = await self.client_for(user)
        token = await self.token(client)

        await self._disconnect_after_connected(
            client=client,
            token=token,
            path=f"/api/threads/{thread.id}/messages/",
            payload={"message": "hello", "revision": 0},
            started=started,
        )

        self.assertTrue(finished.is_set())
        row = await self.fetch_row(thread.id)
        self.assertIsNone(row.active_run_token)
        self.assertIsNone(row.active_run_expires_at)
        self.assertEqual(row.display, [])
        self.assertEqual(row.revision, 0)
        self.assertEqual(await self.chat_turns(thread.id), [])

    async def test_immediate_disconnect_releases_the_regenerate_lease(
        self,
    ) -> None:
        self._echo_agent()
        user = await self.make_user("lease-disconnect-regenerate")
        thread = await self.make_thread(user)
        client = await self.client_for(user)
        token = await self.token(client)
        await self.completed_turn(client, token, thread.id, "first", 0)
        detail = await self.detail(client, thread.id)
        started, finished = self._blocking_agent()

        await self._disconnect_after_connected(
            client=client,
            token=token,
            path=(
                f"/api/threads/{thread.id}/turns/"
                f"{detail['turns'][0]['turn_id']}/regenerate/"
            ),
            payload={"revision": detail["revision"]},
            started=started,
        )

        self.assertTrue(finished.is_set())
        row = await self.fetch_row(thread.id)
        self.assertIsNone(row.active_run_token)
        self.assertIsNone(row.active_run_expires_at)
        self.assertEqual(row.revision, detail["revision"])
        self.assertEqual(len(row.display), 2)
        self.assertEqual(len(await self.chat_turns(thread.id)), 1)

    async def test_acquire_run_maps_every_failure(self) -> None:
        user = await self.make_user("lease-map-owner")
        thread = await self.make_thread(user)

        with self.assertRaises(service.ChatError) as missing:
            await sync_to_async(threads.acquire_run)(
                owner_id=user.pk,
                thread_id="2f9d4b1e-6c3a-4f5b-8e7d-0a1b2c3d4e5f",
                revision=0,
            )
        self.assertEqual(missing.exception.code, "thread_not_found")

        with self.assertRaises(service.ChatError) as stale:
            await sync_to_async(threads.acquire_run)(
                owner_id=user.pk, thread_id=thread.id, revision=3
            )
        self.assertEqual(stale.exception.code, "thread_conflict")

        await sync_to_async(threads.acquire_run)(
            owner_id=user.pk, thread_id=thread.id, revision=0
        )
        with self.assertRaises(service.ChatError) as busy:
            await sync_to_async(threads.acquire_run)(
                owner_id=user.pk, thread_id=thread.id, revision=0
            )
        self.assertEqual(busy.exception.code, "thread_busy")
