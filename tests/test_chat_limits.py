"""Attachment quotas, configuration boundaries, context budget and output cap.

The model is always a real agent graph: a local ``FunctionModel`` for behavioral
checks, or the real OpenAI adapter over a local transport when provider-reported
usage or ``finish_reason`` is what the assertion depends on.
"""

from __future__ import annotations

import json
import os
import re
import runpy
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import httpx2
import pydantic_ai.models as pai_models
from asgiref.sync import sync_to_async
from django.core.exceptions import ImproperlyConfigured
from django.test import SimpleTestCase, override_settings
from pydantic_ai import Agent, FunctionToolset, ModelSettings, RunContext
from pydantic_ai.messages import (
    ModelMessage,
    ModelResponse,
    ToolReturnPart,
)
from pydantic_ai.models.function import DeltaToolCall
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai_compatible import OpenAICompatibleProvider
from pydantic_ai.usage import RequestUsage

from chat import context_budget
from chat.agent import INSTRUCTIONS
from chat.models import ChatAttachment
from tests.support import (
    PROVIDER_SETTINGS,
    ChatHttpTestCase,
    deltas,
    events,
    read_body,
)

BOOTSTRAP_PATTERN = re.compile(
    r'<script id="chat-bootstrap" type="application/json">(.*?)</script>'
)


def settings_env(**overrides: str) -> dict[str, Any]:
    """Load ``config.settings`` freshly with explicit budget values."""
    base = {
        "DJANGO_SECRET_KEY": "offline-test-secret",
        "CHAT_MAX_FILES_PER_THREAD": "5",
        "CHAT_MAX_FILES_PER_TURN": "5",
        "CHAT_MAX_FILE_BYTES": "1048576",
        "CHAT_MAX_TEXT_BYTES_PER_FILE": "65536",
        "CHAT_MAX_TEXT_BYTES_PER_THREAD": "262144",
        "LLM_CONTEXT_WINDOW": "32768",
        "LLM_MAX_OUTPUT_TOKENS": "4096",
    }
    base.update(overrides)
    with patch.dict(os.environ, base, clear=False):
        return runpy.run_module("config.settings")


def upload(name: str, data: bytes) -> Any:
    from django.core.files.uploadedfile import SimpleUploadedFile

    return SimpleUploadedFile(name, data)


def answer(text: str) -> Any:
    async def produce() -> Any:
        yield text

    return produce()


class SettingsTests(SimpleTestCase):
    """Startup validation of the configured budgets."""

    def test_rejects_malformed_and_out_of_range_values(self) -> None:
        cases = [
            ("CHAT_MAX_FILES_PER_THREAD", "abc"),
            ("CHAT_MAX_FILES_PER_THREAD", "1.5"),
            ("CHAT_MAX_FILES_PER_THREAD", "0"),
            ("CHAT_MAX_FILES_PER_THREAD", "-2"),
            ("CHAT_MAX_FILES_PER_THREAD", "2147483648"),
            ("LLM_CONTEXT_WINDOW", "99999999999"),
        ]
        for name, value in cases:
            with (
                self.subTest(name=name, value=value),
                self.assertRaises(ImproperlyConfigured) as caught,
            ):
                settings_env(**{name: value})
            self.assertIn(name, str(caught.exception))

    def test_rejects_inconsistent_relationships(self) -> None:
        cases = [
            {"CHAT_MAX_FILES_PER_TURN": "6"},
            {"CHAT_MAX_TEXT_BYTES_PER_FILE": "300000"},
            {"LLM_MAX_OUTPUT_TOKENS": "32768"},
        ]
        for overrides in cases:
            with (
                self.subTest(overrides=overrides),
                self.assertRaises(ImproperlyConfigured),
            ):
                settings_env(**overrides)

    def test_accepts_custom_values_and_exposes_them(self) -> None:
        loaded = settings_env(
            CHAT_MAX_FILES_PER_THREAD="3",
            CHAT_MAX_FILES_PER_TURN="2",
            CHAT_MAX_FILE_BYTES="64",
            CHAT_MAX_TEXT_BYTES_PER_FILE="32",
            CHAT_MAX_TEXT_BYTES_PER_THREAD="64",
            LLM_CONTEXT_WINDOW="2048",
            LLM_MAX_OUTPUT_TOKENS="256",
        )

        self.assertEqual(loaded["CHAT_MAX_FILES_PER_THREAD"], 3)
        self.assertEqual(loaded["CHAT_MAX_FILES_PER_TURN"], 2)
        self.assertEqual(loaded["LLM_CONTEXT_WINDOW"], 2048)
        # Django's own upload guard follows the per-turn quota.
        self.assertEqual(loaded["DATA_UPLOAD_MAX_NUMBER_FILES"], 2)


@override_settings(**PROVIDER_SETTINGS)
class QuotaTests(ChatHttpTestCase):
    """Per-thread, per-turn, byte and text quotas across real requests."""

    async def test_thread_and_turn_quotas_across_turns(self) -> None:
        factory = self._patch_agent(lambda messages, info: answer("ok"))
        user = await self.make_user("quota-owner")
        thread = await self.make_thread(user)
        client = await self.client_for(user)
        token = await self.token(client)

        await self.uploaded_turn(
            client,
            token,
            thread.id,
            "first batch",
            0,
            [upload(f"a{index}.txt", b"alpha") for index in range(3)],
        )
        await self.uploaded_turn(
            client,
            token,
            thread.id,
            "second batch",
            1,
            [upload(f"b{index}.txt", b"beta") for index in range(2)],
        )
        self.assertEqual(await sync_to_async(ChatAttachment.objects.count)(), 5)
        model_calls = len(factory.seen)

        # A sixth file is refused before the model runs.
        response = await self.send_files(
            client, token, thread.id, "one more", 2, [upload("c.txt", b"gamma")]
        )
        self.assertEqual(response.status_code, 413)
        self.assertEqual(self.error_code(response), "attachment_limit")
        self.assertEqual(len(factory.seen), model_calls)
        self.assertEqual(await sync_to_async(ChatAttachment.objects.count)(), 5)

        # Text-only follow-ups reuse the stored files without consuming a slot.
        await self.completed_turn(client, token, thread.id, "text only", 2)
        self.assertEqual(await sync_to_async(ChatAttachment.objects.count)(), 5)

        # Another conversation owns its own full quota.
        other = await self.make_thread(user)
        await self.uploaded_turn(
            client,
            token,
            other.id,
            "its own files",
            0,
            [upload(f"d{index}.txt", b"delta") for index in range(5)],
        )
        self.assertEqual(await sync_to_async(ChatAttachment.objects.count)(), 10)

    async def test_deleting_the_latest_turn_frees_exactly_its_slots(self) -> None:
        self._patch_agent(lambda messages, info: answer("ok"))
        user = await self.make_user("quota-delete-owner")
        thread = await self.make_thread(user)
        client = await self.client_for(user)
        token = await self.token(client)

        await self.uploaded_turn(
            client,
            token,
            thread.id,
            "first",
            0,
            [upload(f"a{index}.txt", b"alpha") for index in range(3)],
        )
        await self.uploaded_turn(
            client,
            token,
            thread.id,
            "second",
            1,
            [upload(f"b{index}.txt", b"beta") for index in range(2)],
        )
        turns = await self.turns(client, thread.id)
        response = await self.delete_exchange(
            client, token, thread.id, turns[-1]["turn_id"], 2
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(await sync_to_async(ChatAttachment.objects.count)(), 3)

        # The freed slots are usable again; one more than the freed count is not.
        await self.uploaded_turn(
            client,
            token,
            thread.id,
            "third",
            3,
            [upload("c0.txt", b"gamma"), upload("c1.txt", b"gamma")],
        )
        self.assertEqual(await sync_to_async(ChatAttachment.objects.count)(), 5)

    async def test_regeneration_never_increments_the_count(self) -> None:
        self._patch_agent(lambda messages, info: answer("fresh"))
        user = await self.make_user("quota-regen-owner")
        thread = await self.make_thread(user)
        client = await self.client_for(user)
        token = await self.token(client)

        await self.uploaded_turn(
            client,
            token,
            thread.id,
            "first",
            0,
            [upload("a.txt", b"alpha")],
        )
        turns = await self.turns(client, thread.id)
        await self.regenerated(client, token, thread.id, turns[0]["turn_id"], 1)
        self.assertEqual(await sync_to_async(ChatAttachment.objects.count)(), 1)
        detail = await self.detail(client, thread.id)
        self.assertEqual(
            [entry["name"] for entry in detail["turns"][0]["attachments"]],
            ["a.txt"],
        )

    async def test_concurrent_tabs_cannot_take_the_last_slot(self) -> None:
        self._patch_agent(lambda messages, info: answer("ok"))
        user = await self.make_user("quota-race-owner")
        thread = await self.make_thread(user)
        client = await self.client_for(user)
        token = await self.token(client)

        await self.uploaded_turn(
            client,
            token,
            thread.id,
            "four files",
            0,
            [upload(f"a{index}.txt", b"alpha") for index in range(4)],
        )

        held = await self.send_files(
            client, token, thread.id, "last slot", 1, [upload("b.txt", b"beta")]
        )
        self.assertEqual(held.status_code, 200)

        busy = await self.send_files(
            client, token, thread.id, "other tab", 1, [upload("c.txt", b"gamma")]
        )
        self.assertEqual(busy.status_code, 409)
        self.assertEqual(self.error_code(busy), "thread_busy")

        body = await read_body(held)
        await sync_to_async(held.close)()
        self.assertEqual(events(body)[-1][0], "done")
        self.assertEqual(await sync_to_async(ChatAttachment.objects.count)(), 5)

        stale = await self.send_files(
            client, token, thread.id, "stale tab", 1, [upload("d.txt", b"delta")]
        )
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(self.error_code(stale), "thread_conflict")
        self.assertEqual(await sync_to_async(ChatAttachment.objects.count)(), 5)

    @override_settings(CHAT_MAX_FILES_PER_THREAD=5, CHAT_MAX_FILES_PER_TURN=2)
    async def test_per_turn_quota_is_independent_of_thread_headroom(self) -> None:
        factory = self._patch_agent(lambda messages, info: answer("ok"))
        user = await self.make_user("turn-quota-owner")
        thread = await self.make_thread(user)
        client = await self.client_for(user)
        token = await self.token(client)

        response = await self.send_files(
            client,
            token,
            thread.id,
            "too many at once",
            0,
            [upload(f"a{index}.txt", b"alpha") for index in range(3)],
        )
        self.assertEqual(response.status_code, 413)
        self.assertEqual(self.error_code(response), "attachment_limit")
        self.assertEqual(factory.seen, [])

        await self.uploaded_turn(
            client,
            token,
            thread.id,
            "two at once",
            0,
            [upload("b0.txt", b"beta"), upload("b1.txt", b"beta")],
        )
        self.assertEqual(await sync_to_async(ChatAttachment.objects.count)(), 2)

    @override_settings(CHAT_MAX_FILE_BYTES=16)
    async def test_byte_boundary_is_exclusive_of_one_more_byte(self) -> None:
        factory = self._patch_agent(lambda messages, info: answer("ok"))
        user = await self.make_user("byte-quota-owner")
        thread = await self.make_thread(user)
        client = await self.client_for(user)
        token = await self.token(client)

        over = await self.send_files(
            client, token, thread.id, "over", 0, [upload("over.txt", b"x" * 17)]
        )
        self.assertEqual(over.status_code, 413)
        self.assertEqual(self.error_code(over), "attachment_limit")
        self.assertEqual(factory.seen, [])

        await self.uploaded_turn(
            client, token, thread.id, "exact", 0, [upload("exact.txt", b"x" * 16)]
        )
        self.assertEqual(await sync_to_async(ChatAttachment.objects.count)(), 1)

    async def test_lowering_a_quota_keeps_existing_files_usable(self) -> None:
        self._patch_agent(lambda messages, info: answer("ok"))
        user = await self.make_user("lowered-quota-owner")
        thread = await self.make_thread(user)
        client = await self.client_for(user)
        token = await self.token(client)

        await self.uploaded_turn(
            client,
            token,
            thread.id,
            "three files",
            0,
            [upload(f"a{index}.txt", b"alpha") for index in range(3)],
        )

        with override_settings(CHAT_MAX_FILES_PER_THREAD=1, CHAT_MAX_FILES_PER_TURN=1):
            detail = await self.detail(client, thread.id)
            self.assertEqual(len(detail["turns"][0]["attachments"]), 3)

            extra = await self.send_files(
                client, token, thread.id, "more", 1, [upload("b.txt", b"beta")]
            )
            self.assertEqual(extra.status_code, 413)
            self.assertEqual(self.error_code(extra), "attachment_limit")

            await self.completed_turn(client, token, thread.id, "text only", 1)
            detail = await self.detail(client, thread.id)
            self.assertEqual(len(detail["turns"][0]["attachments"]), 3)
            self.assertEqual(detail["display"][-2]["content"], "text only")

    @override_settings(
        CHAT_MAX_FILES_PER_THREAD=3,
        CHAT_MAX_FILES_PER_TURN=2,
        CHAT_MAX_FILE_BYTES=4096,
        LLM_CONTEXT_WINDOW=8192,
        LLM_MAX_OUTPUT_TOKENS=512,
    )
    async def test_bootstrap_advertises_configured_values(self) -> None:
        self._patch_agent(lambda messages, info: answer("ok"))
        user = await self.make_user("bootstrap-limits-owner")
        client = await self.client_for(user)

        page = await client.get("/")
        self.assertEqual(page.status_code, 200)
        match = BOOTSTRAP_PATTERN.search(page.content.decode())
        self.assertIsNotNone(match)
        bootstrap = json.loads(match.group(1))
        self.assertEqual(
            bootstrap["limits"],
            {
                "max_files_per_thread": 3,
                "max_files_per_turn": 2,
                "max_file_bytes": 4096,
                "max_text_bytes_per_file": 65536,
                "max_text_bytes_per_thread": 262144,
                "context_window": 8192,
                "max_output_tokens": 512,
            },
        )


@override_settings(**PROVIDER_SETTINGS)
class ContextBudgetTests(ChatHttpTestCase):
    """The conservative text estimate and the provider-reported backstop."""

    async def test_long_text_is_refused_before_the_model_under_a_small_budget(
        self,
    ) -> None:
        factory = self._patch_agent(lambda messages, info: answer("ok"))
        user = await self.make_user("context-long-owner")
        thread = await self.make_thread(user)
        client = await self.client_for(user)
        token = await self.token(client)

        with override_settings(LLM_CONTEXT_WINDOW=4096, LLM_MAX_OUTPUT_TOKENS=128):
            _body, parsed = await self.uploaded_turn(
                client,
                token,
                thread.id,
                "summarize",
                0,
                [upload("big.txt", b"a" * 10_000)],
            )
        self.assertEqual(parsed[-1][0], "error")
        self.assertEqual(parsed[-1][1]["error"]["code"], "context_limit")
        self.assertEqual(factory.seen, [])
        row = await self.fetch_row(thread.id)
        self.assertEqual(row.revision, 0)
        self.assertEqual(await sync_to_async(ChatAttachment.objects.count)(), 0)

        with override_settings(LLM_CONTEXT_WINDOW=32768, LLM_MAX_OUTPUT_TOKENS=128):
            _body, parsed = await self.uploaded_turn(
                client,
                token,
                thread.id,
                "summarize",
                0,
                [upload("big.txt", b"a" * 10_000)],
            )
        self.assertEqual(parsed[-1][0], "done")
        self.assertEqual(len(factory.seen), 1)

    async def test_large_tool_result_is_refused_before_the_next_call(self) -> None:
        calls: list[str] = []
        toolset: FunctionToolset[None] = FunctionToolset()

        @toolset.tool
        def dump_reference(ctx: RunContext[None]) -> str:
            """Return a large reference payload."""
            calls.append("dump")
            return "r" * 8000

        def stream(messages: list[ModelMessage], info: Any) -> Any:
            returned = [
                part
                for message in messages
                for part in getattr(message, "parts", [])
                if isinstance(part, ToolReturnPart)
            ]
            if not returned:
                return _tool_then_text(
                    DeltaToolCall(
                        name="dump_reference", json_args="{}", tool_call_id="c1"
                    )
                )
            return answer("never reached")

        factory = self._patch_agent(stream, toolsets=(toolset,))
        user = await self.make_user("context-tool-owner")
        thread = await self.make_thread(user)
        client = await self.client_for(user)
        token = await self.token(client)

        with override_settings(LLM_CONTEXT_WINDOW=8192, LLM_MAX_OUTPUT_TOKENS=512):
            _body, parsed = await self.turn(client, token, thread.id, "dump it", 0)
        self.assertEqual(parsed[-1][0], "error")
        self.assertEqual(parsed[-1][1]["error"]["code"], "context_limit")
        self.assertEqual(calls, ["dump"])
        self.assertEqual(len(factory.seen), 1)
        row = await self.fetch_row(thread.id)
        self.assertEqual(row.revision, 0)
        self.assertEqual(row.display, [])

    async def test_a_known_model_window_tightens_the_configured_budget(self) -> None:
        class Tight:
            context_window = 512

        class Unknown:
            context_window = None

        self.assertEqual(
            context_budget.input_allowance(
                Tight(), context_window=32768, max_output_tokens=4096
            ),
            512 - 4096,
        )
        self.assertEqual(
            context_budget.input_allowance(
                Unknown(), context_window=32768, max_output_tokens=4096
            ),
            32768 - 4096,
        )

        hooks = context_budget.make_budget_hooks(
            context_window=32768, max_output_tokens=4096
        )
        request_context = SimpleNamespace(
            model=Tight(),
            messages=[],
            model_request_parameters=SimpleNamespace(
                instruction_parts=[], function_tools=[], output_tools=[]
            ),
        )
        with self.assertRaises(context_budget.ContextBudgetExceeded):
            await hooks.before_model_request(None, request_context)

    async def test_unmeasured_usage_is_not_a_measured_zero(self) -> None:
        class Wide:
            context_window = None

        hooks = context_budget.make_budget_hooks(
            context_window=4096, max_output_tokens=128
        )
        request_context = SimpleNamespace(
            model=Wide(),
            messages=[],
            model_request_parameters=SimpleNamespace(
                instruction_parts=[], function_tools=[], output_tools=[]
            ),
        )
        unmeasured = ModelResponse(
            parts=[],
            usage=RequestUsage(input_tokens=10**9, unmeasured_requests=1),
        )
        self.assertIs(
            await hooks.after_model_request(
                None, request_context=request_context, response=unmeasured
            ),
            unmeasured,
        )

        measured = ModelResponse(
            parts=[],
            usage=RequestUsage(input_tokens=10**9, unmeasured_requests=0),
        )
        with self.assertRaises(context_budget.ContextBudgetExceeded):
            await hooks.after_model_request(
                None, request_context=request_context, response=measured
            )


def _tool_then_text(call: DeltaToolCall) -> Any:
    async def produce() -> Any:
        yield "checking"
        yield {0: call}

    return produce()


def openai_stream(chunks: list[dict[str, Any]]) -> Any:
    """A handler for the real adapter over a local transport, plus its log."""
    requests: list[dict[str, Any]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(json.loads(request.content))
        payload = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
        payload += "data: [DONE]\n\n"
        return httpx2.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=payload.encode(),
        )

    return handler, requests


def openai_agent(handler: Any, *, toolsets: tuple[Any, ...] = ()) -> Any:
    def build(**_: Any) -> Agent[None, str]:
        client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
        provider = OpenAICompatibleProvider(
            "mock",
            base_url="https://offline.invalid/v1",
            api_key="offline-key",
            http_client=client,
        )
        return Agent(
            OpenAIChatModel("mock-model", provider=provider),
            name="mock_chat",
            instructions=INSTRUCTIONS,
            toolsets=list(toolsets),
            model_settings=ModelSettings(max_retries=0),
        )

    return build


def chunk(delta: dict[str, Any], finish: str | None = None) -> dict[str, Any]:
    return {
        "id": "chunk-1",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "mock-model",
        "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
    }


def usage_chunk(
    reason: str, *, prompt_tokens: int = 12, completion_tokens: int = 3
) -> dict[str, Any]:
    return {
        "id": "chunk-1",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "mock-model",
        "choices": [{"index": 0, "delta": {}, "finish_reason": reason}],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


@override_settings(
    **PROVIDER_SETTINGS, LLM_CONTEXT_WINDOW=4096, LLM_MAX_OUTPUT_TOKENS=128
)
class ReportedUsageTests(ChatHttpTestCase):
    """Provider-reported input tokens are the authoritative backstop."""

    def setUp(self) -> None:
        super().setUp()
        pai_models.ALLOW_MODEL_REQUESTS = True
        self.addCleanup(setattr, pai_models, "ALLOW_MODEL_REQUESTS", False)

    async def run_with(
        self, prompt_tokens: int
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        handler, requests = openai_stream(
            [
                chunk({"role": "assistant", "content": "ok"}),
                usage_chunk("stop", prompt_tokens=prompt_tokens),
            ]
        )
        patcher = patch("chat.service.build_agent", openai_agent(handler))
        patcher.start()
        self.addCleanup(patcher.stop)

        user = await self.make_user(f"usage-owner-{prompt_tokens}")
        thread = await self.make_thread(user)
        client = await self.client_for(user)
        token = await self.token(client)
        _body, parsed = await self.turn(client, token, thread.id, "hello", 0)
        return parsed[-1], requests

    async def test_equality_passes_and_one_more_token_is_rejected(self) -> None:
        final, requests = await self.run_with(4096 - 128)
        self.assertEqual(final[0], "done")
        sent = requests[0].get("max_tokens") or requests[0].get("max_completion_tokens")
        self.assertEqual(sent, 128)

        final, requests = await self.run_with(4096 - 128 + 1)
        self.assertEqual(final[0], "error")
        self.assertEqual(final[1]["error"]["code"], "context_limit")


@override_settings(**PROVIDER_SETTINGS, LLM_MAX_OUTPUT_TOKENS=64)
class OutputCapTests(ChatHttpTestCase):
    """``finish_reason=length`` is a partial success, not a retry or an error."""

    def setUp(self) -> None:
        super().setUp()
        pai_models.ALLOW_MODEL_REQUESTS = True
        self.addCleanup(setattr, pai_models, "ALLOW_MODEL_REQUESTS", False)

    async def test_visible_partial_text_is_committed_once(self) -> None:
        handler, requests = openai_stream(
            [
                chunk({"role": "assistant", "content": "Partial answer"}),
                usage_chunk("length", prompt_tokens=20, completion_tokens=9),
            ]
        )
        patcher = patch("chat.service.build_agent", openai_agent(handler))
        patcher.start()
        self.addCleanup(patcher.stop)

        user = await self.make_user("cap-owner")
        thread = await self.make_thread(user)
        client = await self.client_for(user)
        token = await self.token(client)

        _body, parsed = await self.turn(client, token, thread.id, "long one", 0)

        self.assertEqual(deltas(parsed), ["Partial answer"])
        self.assertEqual(parsed[-1][0], "done")
        payload = parsed[-1][1]
        self.assertEqual(payload["reply"], "Partial answer")
        self.assertTrue(payload["turn"]["response"]["output_limit_reached"])
        self.assertEqual(len(requests), 1)

        detail = await self.detail(client, thread.id)
        self.assertEqual(detail["display"][-1]["content"], "Partial answer")
        self.assertTrue(detail["turns"][0]["response"]["output_limit_reached"])
        row = await self.fetch_row(thread.id)
        self.assertEqual(row.revision, 1)

        # The stored partial turn is a valid starting point for a later turn.
        normal, _more = openai_stream(
            [
                chunk({"role": "assistant", "content": "followed up"}),
                usage_chunk("stop"),
            ]
        )
        with patch("chat.service.build_agent", openai_agent(normal)):
            payload, _revision = await self.completed_turn(
                client, token, thread.id, "continue", 1
            )
        self.assertEqual(payload["reply"], "followed up")

    async def test_regeneration_replaces_the_flag_with_the_new_result(self) -> None:
        capped, _requests = openai_stream(
            [chunk({"content": "partial"}), usage_chunk("length")]
        )
        normal, _more = openai_stream(
            [chunk({"content": "complete"}), usage_chunk("stop")]
        )
        patcher = patch("chat.service.build_agent", openai_agent(capped))
        patcher.start()
        self.addCleanup(patcher.stop)

        user = await self.make_user("cap-regen-owner")
        thread = await self.make_thread(user)
        client = await self.client_for(user)
        token = await self.token(client)
        _body, parsed = await self.turn(client, token, thread.id, "long one", 0)
        turn_id = parsed[-1][1]["turn"]["turn_id"]

        with patch("chat.service.build_agent", openai_agent(normal)):
            _body, parsed = await self.regenerated(client, token, thread.id, turn_id, 1)
        self.assertEqual(parsed[-1][0], "done")
        self.assertFalse(parsed[-1][1]["turn"]["response"]["output_limit_reached"])
        detail = await self.detail(client, thread.id)
        self.assertFalse(detail["turns"][0]["response"]["output_limit_reached"])
        self.assertEqual(detail["display"][-1]["content"], "complete")

    async def test_truncated_tool_call_is_not_executed(self) -> None:
        calls: list[str] = []
        toolset: FunctionToolset[None] = FunctionToolset()

        @toolset.tool
        def lookup(ctx: RunContext[None]) -> int:
            """Return a reference number."""
            calls.append("lookup")
            return 7

        handler, requests = openai_stream(
            [
                chunk({"content": "Checking."}),
                chunk(
                    {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call-1",
                                "type": "function",
                                "function": {"name": "lookup", "arguments": "{}"},
                            }
                        ]
                    }
                ),
                usage_chunk("length"),
            ]
        )
        patcher = patch(
            "chat.service.build_agent", openai_agent(handler, toolsets=(toolset,))
        )
        patcher.start()
        self.addCleanup(patcher.stop)

        user = await self.make_user("cap-tool-owner")
        thread = await self.make_thread(user)
        client = await self.client_for(user)
        token = await self.token(client)
        _body, parsed = await self.turn(client, token, thread.id, "look it up", 0)

        self.assertEqual(parsed[-1][0], "done")
        self.assertEqual(calls, [])
        self.assertEqual(len(requests), 1)
        row = await self.fetch_row(thread.id)
        self.assertNotIn("call-1", row.history_json)
        self.assertNotIn('"tool-call"', row.history_json)

    async def test_cap_without_visible_text_fails_without_committing(self) -> None:
        handler, _requests = openai_stream(
            [
                chunk(
                    {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call-2",
                                "type": "function",
                                "function": {"name": "lookup", "arguments": "{}"},
                            }
                        ]
                    }
                ),
                usage_chunk("length"),
            ]
        )
        patcher = patch("chat.service.build_agent", openai_agent(handler))
        patcher.start()
        self.addCleanup(patcher.stop)

        user = await self.make_user("cap-empty-owner")
        thread = await self.make_thread(user)
        client = await self.client_for(user)
        token = await self.token(client)
        _body, parsed = await self.turn(client, token, thread.id, "long one", 0)

        self.assertEqual(parsed[-1][0], "error")
        self.assertEqual(parsed[-1][1]["error"]["code"], "output_limit")
        self.assertEqual(deltas(parsed), [])
        row = await self.fetch_row(thread.id)
        self.assertEqual(row.revision, 0)
        self.assertEqual(row.display, [])

    async def test_two_rounds_below_the_cap_are_not_summed(self) -> None:
        toolset: FunctionToolset[None] = FunctionToolset()

        @toolset.tool
        def lookup(ctx: RunContext[None]) -> int:
            """Return a reference number."""
            return 7

        def stream(messages: list[ModelMessage], info: Any) -> Any:
            returned = [
                part
                for message in messages
                for part in getattr(message, "parts", [])
                if isinstance(part, ToolReturnPart)
            ]
            if not returned:
                return _tool_then_text(
                    DeltaToolCall(name="lookup", json_args="{}", tool_call_id="c1")
                )
            return answer("z" * 200)

        factory = self._patch_agent(stream, toolsets=(toolset,))
        user = await self.make_user("cap-rounds-owner")
        thread = await self.make_thread(user)
        client = await self.client_for(user)
        token = await self.token(client)

        _body, parsed = await self.turn(client, token, thread.id, "look twice", 0)

        self.assertEqual(parsed[-1][0], "done")
        self.assertEqual(len(factory.seen), 2)
        self.assertFalse(parsed[-1][1]["turn"]["response"]["output_limit_reached"])
        self.assertTrue(parsed[-1][1]["reply"].endswith("z" * 200))
