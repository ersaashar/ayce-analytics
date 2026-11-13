"""Guarded native Wren toolset against the loopback fixture.

No adapter or MCP mocks. Local denials and external failures surface as
``WrenMCPError``. Lease and scope movement stay ``ChatError``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import contextmanager, suppress
from dataclasses import replace
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch
from uuid import uuid4

from asgiref.sync import sync_to_async
from django.contrib.auth.models import User
from django.test import SimpleTestCase
from pydantic_ai import RunContext
from pydantic_ai.mcp import MCPToolset
from pydantic_ai.models.test import TestModel
from pydantic_ai.toolsets import ToolsetTool
from pydantic_ai.usage import RunUsage

from chat.mcp import (
    MAX_QUERY_ROWS,
    MAX_SQL_BYTES,
    MAX_TOOL_RESULT_BYTES,
    REVIEWED_TOOLS,
    WrenMCPError,
    WrenToolset,
    _visible_result,
    open_wren_toolset,
)
from chat.mcp_policy import AnalyticsRun, resolve_dev_scope
from chat.service import ChatError
from chat.threads import acquire_run, create_thread, release_run
from datasets.discovery import configured_source_key
from datasets.models import Dataset, DatasetCatalogState, MembershipDataset
from datasets.services import refresh_catalog
from tests.support import GovernanceTestCase
from tests.wren_fixture import DEFAULT_QUERY_RESULT, DENIED_TOOLS, WrenFixture

SQL = "SELECT COUNT(*) AS total_orders FROM orders"
LEAK = "LEAKSQL-9f3a"
ROW_SECRET = "ROWSECRET-2a8c"
OVERSIZE = "OVERSIZE-4b9d"
MCP_ERROR = "Wren MCP operation failed."
_ADMISSION = ["wren://project", "get_mdl", "wren://project"]
_DEV = {
    "DEBUG": True,
    "AYCE_ANALYTICS_MODE": "dev_jaffle",
    "AYCE_DEV_JAFFLE_CONFIRMED": True,
    "AYCE_DEV_WREN_PROJECT": "jaffle-wren",
}


def _dev(url: str) -> dict[str, object]:
    return {**_DEV, "WREN_MCP_URL": url, "AYCE_DEV_WREN_MCP_URL": url}


def _payload(value: Any) -> Any:
    if isinstance(value, str):
        return json.loads(value)
    return value


def _rows(count: int, marker: str = ROW_SECRET) -> dict[str, Any]:
    return {
        "columns": ["note"],
        "rows": [{"note": f"{marker}-{index}"} for index in range(count)],
        "row_count": count,
        "truncated": False,
    }


class _Logs(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())
        if record.exc_info and record.exc_info[1] is not None:
            self.messages.append(str(record.exc_info[1]))


@contextmanager
def _chat_logs():
    handler = _Logs()
    logger = logging.getLogger("chat")
    previous = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        yield handler
    finally:
        logger.removeHandler(handler)
        logger.setLevel(previous)


class ResultBoundaryTests(SimpleTestCase):
    def test_rejects_non_text_resource_and_app_content(self) -> None:
        for content_type in ("image", "audio", "resource", "resource_link"):
            with (
                self.subTest(content_type=content_type),
                self.assertRaises(WrenMCPError),
            ):
                _visible_result(
                    "list_models",
                    SimpleNamespace(content=[SimpleNamespace(type=content_type)]),
                )
        for meta in (
            {"ui": {"resourceUri": "ui://resource"}},
            {"ui/resourceUri": "ui://resource"},
        ):
            with self.subTest(meta=meta), self.assertRaises(WrenMCPError):
                _visible_result(
                    "list_models",
                    SimpleNamespace(
                        content=[SimpleNamespace(type="text", text="safe")],
                        meta=meta,
                    ),
                )


class WrenToolsetTests(GovernanceTestCase):
    def setUp(self) -> None:
        super().setUp()
        self._seq = 0

    def _ctx(self) -> RunContext[None]:
        return RunContext(deps=None, model=TestModel(), usage=RunUsage())

    def _lease(self, user: User, department_id) -> AnalyticsRun:
        thread = create_thread(owner_id=user.pk, department_id=department_id)
        token = acquire_run(
            owner_id=user.pk, thread_id=thread.pk, revision=thread.revision
        )
        self.addCleanup(
            release_run, owner_id=user.pk, thread_id=thread.pk, run_token=token
        )
        return AnalyticsRun(user.pk, thread.pk, department_id, self.epoch(), token)

    async def _catalog(self) -> None:
        def ready() -> bool:
            key = configured_source_key()
            return bool(
                key
                and DatasetCatalogState.objects.filter(pk=key, error_code="").exists()
            )

        if await sync_to_async(ready)():
            return

        def actor() -> User:
            existing = User.objects.filter(username="catalog-root").first()
            if existing is not None:
                return existing
            return self.make_account("catalog-root", superuser=True)

        operator = await sync_to_async(actor)()
        await refresh_catalog(
            actor_id=operator.pk, revision=await sync_to_async(self.revision)()
        )

    async def _world(self):
        await self._catalog()
        self._seq += 1
        suffix = str(self._seq)

        def build():
            sales = self.make_department(f"sales-{suffix}", f"Sales {suffix}")
            key = configured_source_key()
            customers = Dataset.objects.get(model_name="customers", source_key=key)
            orders = Dataset.objects.get(model_name="orders", source_key=key)
            self.grant(sales, customers)
            self.grant(sales, orders)
            full = self.make_account(f"full-{suffix}", groups=("AYCE Staff",))
            self.join(full, sales, datasets=(customers, orders))
            partial = self.make_account(f"partial-{suffix}", groups=("AYCE Staff",))
            self.join(partial, sales, datasets=(orders,))
            plain = self.make_account(f"plain-{suffix}")
            self.join(plain, sales, datasets=(customers, orders))
            return {
                "full": full,
                "partial": partial,
                "plain": plain,
                "sales": sales,
                "customers": customers,
                "orders": orders,
                "full_run": self._lease(full, sales.pk),
                "partial_run": self._lease(partial, sales.pk),
                "plain_run": self._lease(plain, sales.pk),
            }

        return await sync_to_async(build)()

    def _added(self, fixture: WrenFixture, baseline: list[str]) -> list[str]:
        return fixture.calls[len(baseline) :]

    def _no_leak(self, exc: BaseException, *secrets: str) -> None:
        parts = [str(exc)]
        if exc.__cause__ is not None:
            parts.append(str(exc.__cause__))
        if exc.__context__ is not None and not exc.__suppress_context__:
            parts.append(str(exc.__context__))
        text = "\n".join(parts)
        self.assertNotIn("http://", text)
        self.assertNotIn("127.0.0.1", text)
        for secret in secrets:
            self.assertNotIn(secret, text)

    def _assert_mcp_error(self, exc: BaseException, *secrets: str) -> None:
        self.assertIsInstance(exc, WrenMCPError)
        self.assertEqual(str(exc), MCP_ERROR)
        self.assertIsNone(exc.__cause__)
        self._no_leak(exc, *secrets)

    def _assert_closed(self, toolset: WrenToolset) -> None:
        native = toolset.wrapped
        self.assertIsInstance(native, MCPToolset)
        self.assertEqual(native._running_count, 0)
        self.assertIsNone(native._exit_stack)
        self.assertFalse(native.client.is_connected())

    def _assert_native(self, toolset: WrenToolset, url: str) -> None:
        native = toolset.wrapped
        self.assertIsInstance(native, MCPToolset)
        self.assertEqual(native.id, "wren")
        self.assertTrue(native._id_explicit)
        self.assertFalse(native.include_instructions)
        self.assertEqual(native.tool_error_behavior, "error")
        self.assertEqual(native.max_retries, 0)
        self.assertIs(native.prefer_tasks, False)
        self.assertEqual(native.client.mode, "legacy")
        self.assertEqual(native.client.transport.url, url)
        self.assertEqual(native.client._init_timeout, 10)
        self.assertEqual(native.client._session_kwargs["read_timeout_seconds"], 60)

    def _assert_reviewed_schema(self, tools: dict[str, ToolsetTool[None]]) -> None:
        self.assertEqual(set(tools), set(REVIEWED_TOOLS))
        for name, tool in tools.items():
            schema = tool.tool_def.parameters_json_schema
            self.assertEqual(schema.get("type"), "object", name)
            self.assertIsInstance(schema.get("properties"), dict, name)
        run_sql = tools["run_sql"].tool_def.parameters_json_schema
        describe = tools["describe_model"].tool_def.parameters_json_schema
        dry_plan = tools["dry_plan"].tool_def.parameters_json_schema
        self.assertIn("sql", run_sql["properties"])
        self.assertIn("sql", run_sql.get("required", []))
        self.assertIn("limit", run_sql["properties"])
        self.assertNotIn("limit", run_sql.get("required", []))
        self.assertIn("name", describe["properties"])
        self.assertIn("name", describe.get("required", []))
        self.assertIn("sql", dry_plan["properties"])
        self.assertIn("sql", dry_plan.get("required", []))
        for name in ("list_models", "get_instructions"):
            required = tools[name].tool_def.parameters_json_schema.get("required", [])
            self.assertEqual(required, [])
        self.assertNotIn("get_mdl", tools)
        self.assertTrue(set(DENIED_TOOLS).isdisjoint(tools))

    def _forge(
        self, toolset: WrenToolset, source: ToolsetTool[None], name: str
    ) -> ToolsetTool[None]:
        return ToolsetTool(
            toolset=toolset,
            tool_def=replace(source.tool_def, name=name),
            max_retries=source.max_retries,
            args_validator=source.args_validator,
        )

    async def _expect_idle(
        self, fixture: WrenFixture, baseline: list[str], run: AnalyticsRun | None
    ) -> None:
        queries = list(fixture.queries)
        async with open_wren_toolset(run=run) as toolset:
            self.assertIsNone(toolset)
        self.assertEqual(fixture.calls, baseline)
        self.assertEqual(fixture.queries, queries)

    async def _expect_none_after_metadata(
        self, fixture: WrenFixture, baseline: list[str], run: AnalyticsRun
    ) -> None:
        async with open_wren_toolset(run=run) as toolset:
            self.assertIsNone(toolset)
        added = self._added(fixture, baseline)
        self.assertIn("wren://project", added)
        self.assertIn("get_mdl", added)
        self.assertNotIn("run_sql", added)
        self.assertEqual(fixture.queries, [])

    def test_bounds_match_the_reviewed_contract(self) -> None:
        self.assertEqual(
            REVIEWED_TOOLS,
            frozenset(
                {
                    "list_models",
                    "describe_model",
                    "get_instructions",
                    "dry_plan",
                    "run_sql",
                }
            ),
        )
        self.assertEqual(MAX_QUERY_ROWS, 100)
        self.assertEqual(MAX_SQL_BYTES, 16 * 1024)
        self.assertEqual(MAX_TOOL_RESULT_BYTES, 64 * 1024)
        self.assertEqual(str(WrenMCPError()), MCP_ERROR)

    async def test_semantic_schemas_execute_once_per_admission(self) -> None:
        variants = ("equivalent", "annotated")
        for variant in variants:
            with (
                self.subTest(variant=variant),
                WrenFixture(analytics=True, schema_variant=variant) as fixture,
                self.settings(**_dev(fixture.url)),
            ):
                world = await self._world()
                baseline = list(fixture.calls)
                with _chat_logs() as logs:
                    async with open_wren_toolset(run=world["full_run"]) as toolset:
                        self.assertIsInstance(toolset, WrenToolset)
                        self._assert_native(toolset, fixture.url)
                        self.assertIsNone(toolset.used_scope)
                        tools = await toolset.get_tools(self._ctx())
                        self._assert_reviewed_schema(tools)
                        self.assertEqual(self._added(fixture, baseline), _ADMISSION)
                        self.assertIsNone(toolset.used_scope)
                        listed = _payload(
                            await toolset.call_tool(
                                "list_models", {}, self._ctx(), tools["list_models"]
                            )
                        )
                        self.assertEqual(set(listed["models"]), {"customers", "orders"})
                        described = _payload(
                            await toolset.call_tool(
                                "describe_model",
                                {"name": "customers"},
                                self._ctx(),
                                tools["describe_model"],
                            )
                        )
                        self.assertEqual(described["name"], "customers")
                        instructions = await toolset.call_tool(
                            "get_instructions",
                            {},
                            self._ctx(),
                            tools["get_instructions"],
                        )
                        self.assertIn("Synthetic Jaffle", str(instructions))
                        planned = await toolset.call_tool(
                            "dry_plan",
                            {"sql": SQL},
                            self._ctx(),
                            tools["dry_plan"],
                        )
                        self.assertIn(SQL, str(planned))
                        result = _payload(
                            await toolset.call_tool(
                                "run_sql",
                                {"sql": SQL, "limit": 1},
                                self._ctx(),
                                tools["run_sql"],
                            )
                        )
                        self.assertEqual(result, DEFAULT_QUERY_RESULT)
                        self.assertEqual(fixture.queries, [(SQL, 1)])
                        self.assertEqual(
                            self._added(fixture, baseline).count("get_mdl"), 1
                        )
                        scope = await sync_to_async(resolve_dev_scope)(
                            user_id=world["full"].pk,
                            department_id=world["sales"].pk,
                        )
                        self.assertEqual(toolset.used_scope, scope)
                        again = await toolset.get_tools(self._ctx())
                        self.assertEqual(set(again), set(REVIEWED_TOOLS))
                        self.assertEqual(
                            self._added(fixture, baseline).count("get_mdl"), 1
                        )
                self._assert_closed(toolset)
                text = "\n".join(logs.messages)
                self.assertNotIn(SQL, text)
                self.assertNotIn(fixture.url, text)
                self.assertNotIn("total_orders", text)

    async def test_ineligible_scope_does_not_contact_mcp(self) -> None:
        with WrenFixture(analytics=True) as fixture, self.settings(**_dev(fixture.url)):
            world = await self._world()
            baseline = list(fixture.calls)
            await self._expect_idle(fixture, baseline, None)
            await self._expect_idle(fixture, baseline, world["partial_run"])
            await self._expect_idle(fixture, baseline, world["plain_run"])
            general = await sync_to_async(self._lease)(world["full"], None)
            await self._expect_idle(fixture, baseline, general)
            forged = AnalyticsRun(
                world["full"].pk,
                world["full_run"].thread_id,
                uuid4(),
                world["full_run"].authorization_epoch,
                world["full_run"].run_token,
            )
            await self._expect_idle(fixture, baseline, forged)
            with self.settings(AYCE_ANALYTICS_MODE="off"):
                await self._expect_idle(fixture, baseline, world["full_run"])
            with self.settings(DEBUG=False):
                await self._expect_idle(fixture, baseline, world["full_run"])
            dead = "http://127.0.0.1:9/mcp"
            with self.settings(WREN_MCP_URL=dead, AYCE_DEV_WREN_MCP_URL=dead):
                await self._expect_idle(fixture, baseline, world["full_run"])

            def drop_lease() -> None:
                release_run(
                    owner_id=world["full"].pk,
                    thread_id=world["full_run"].thread_id,
                    run_token=world["full_run"].run_token,
                )

            await sync_to_async(drop_lease)()
            with self.assertRaises(ChatError) as caught:
                async with open_wren_toolset(run=world["full_run"]):
                    self.fail("ineligible lease opened a toolset")
            self.assertEqual(caught.exception.code, "authorization_changed")
            self._no_leak(caught.exception, SQL)
            self.assertEqual(fixture.calls, baseline)
            self.assertEqual(fixture.queries, [])

    async def test_incompatible_inventory_stays_tool_free(self) -> None:
        cases = (
            {"analytics": True, "schema_variant": "incompatible"},
            {"analytics": False},
        )
        for options in cases:
            with (
                self.subTest(options=options),
                WrenFixture(**options) as fixture,
                self.settings(**_dev(fixture.url)),
            ):
                world = await self._world()
                baseline = list(fixture.calls)
                await self._expect_none_after_metadata(
                    fixture, baseline, world["full_run"]
                )

        with WrenFixture(analytics=True) as fixture, self.settings(**_dev(fixture.url)):
            world = await self._world()
            fixture.project["name"] = "other-wren"
            baseline = list(fixture.calls)
            await self._expect_none_after_metadata(fixture, baseline, world["full_run"])
            fixture.project["name"] = "jaffle-wren"
            extra = json.loads(json.dumps(fixture.mdl["models"][0]))
            extra["name"] = "payments"
            fixture.mdl["models"].append(extra)
            baseline = list(fixture.calls)
            await self._expect_none_after_metadata(fixture, baseline, world["full_run"])

    async def test_denied_and_forged_calls_never_reach_the_fixture(self) -> None:
        with (
            WrenFixture(analytics=True, denied_tools=True) as fixture,
            self.settings(**_dev(fixture.url)),
        ):
            world = await self._world()
            async with open_wren_toolset(run=world["full_run"]) as toolset:
                self.assertIsInstance(toolset, WrenToolset)
                tools = await toolset.get_tools(self._ctx())
                self._assert_reviewed_schema(tools)
                admitted = self._forge(toolset, tools["run_sql"], "run_sql")
                for name in (
                    *DENIED_TOOLS,
                    "get_mdl",
                    "query_cube",
                    "dry_run",
                    "extract_everything",
                ):
                    with self.subTest(name=name):
                        before = list(fixture.calls)
                        forged = self._forge(toolset, tools["run_sql"], name)
                        with self.assertRaises(WrenMCPError) as caught:
                            await toolset.call_tool(
                                name, {"name": LEAK, "sql": LEAK}, self._ctx(), forged
                            )
                        self._assert_mcp_error(caught.exception, LEAK)
                        self.assertEqual(fixture.calls, before)
                        self.assertEqual(fixture.queries, [])
                        self.assertIsNone(toolset.used_scope)
                before = list(fixture.calls)
                forged = self._forge(toolset, tools["run_sql"], "write_model")
                with self.assertRaises(WrenMCPError) as caught:
                    await toolset.call_tool(
                        "run_sql",
                        {"sql": SQL, "limit": 1},
                        self._ctx(),
                        forged,
                    )
                self._assert_mcp_error(caught.exception, SQL)
                self.assertEqual(fixture.calls, before)
                self.assertEqual(fixture.queries, [])
                with self.assertRaises(WrenMCPError) as caught:
                    await toolset.call_tool(
                        "run_sql",
                        {"sql": SQL, "limit": True, "token": LEAK},
                        self._ctx(),
                        admitted,
                    )
                self._assert_mcp_error(caught.exception, SQL, LEAK)
                self.assertEqual(fixture.queries, [])
                self.assertIsNone(toolset.used_scope)
            self._assert_closed(toolset)

    async def test_sql_limit_and_result_bounds(self) -> None:
        with (
            WrenFixture(analytics=True, query_result=_rows(1)) as fixture,
            self.settings(**_dev(fixture.url)),
        ):
            world = await self._world()
            async with open_wren_toolset(run=world["full_run"]) as toolset:
                self.assertIsInstance(toolset, WrenToolset)
                tools = await toolset.get_tools(self._ctx())
                tool = tools["run_sql"]

                async def reject(args: dict[str, Any], *secrets: str) -> None:
                    before = list(fixture.queries)
                    with self.assertRaises(WrenMCPError) as caught:
                        await toolset.call_tool("run_sql", args, self._ctx(), tool)
                    self._assert_mcp_error(caught.exception, *secrets)
                    self.assertEqual(fixture.queries, before)
                    self.assertIsNone(toolset.used_scope)

                await reject({"limit": 1}, SQL)
                await reject({"sql": "", "limit": 1})
                await reject({"sql": LEAK, "limit": True}, LEAK)
                await reject({"sql": LEAK, "limit": False}, LEAK)
                await reject({"sql": LEAK, "limit": 0}, LEAK)
                await reject({"sql": LEAK, "limit": -1}, LEAK)
                await reject({"sql": LEAK, "limit": 1.5}, LEAK)
                await reject({"sql": LEAK, "limit": "10"}, LEAK)
                await reject({"sql": b"select 1", "limit": 1})
                await reject(
                    {"sql": "x" * (MAX_SQL_BYTES + 1), "limit": 1},
                    "x" * 32,
                )
                over_chars = "é" * ((MAX_SQL_BYTES // 2) + 1)
                self.assertGreater(len(over_chars.encode()), MAX_SQL_BYTES)
                await reject({"sql": over_chars, "limit": 1}, "é")
                await reject({"sql": LEAK, "limit": 1, "token": LEAK}, LEAK)

                exact = "x" * MAX_SQL_BYTES
                exact_bytes = "é" * (MAX_SQL_BYTES // 2)
                self.assertEqual(len(exact_bytes.encode()), MAX_SQL_BYTES)
                forwarded = (
                    ({"sql": exact}, 100),
                    ({"sql": exact_bytes, "limit": None}, 100),
                    ({"sql": SQL, "limit": 1}, 1),
                    ({"sql": SQL, "limit": MAX_QUERY_ROWS}, MAX_QUERY_ROWS),
                    ({"sql": SQL, "limit": MAX_QUERY_ROWS + 50}, MAX_QUERY_ROWS),
                )
                for args, limit in forwarded:
                    with self.subTest(limit=limit, size=len(str(args["sql"]))):
                        await toolset.call_tool("run_sql", args, self._ctx(), tool)
                        self.assertEqual(fixture.queries[-1], (args["sql"], limit))
                self.assertIsNotNone(toolset.used_scope)
            self._assert_closed(toolset)

        oversize = {
            "columns": ["note"],
            "rows": [{"note": OVERSIZE + ("A" * (MAX_TOOL_RESULT_BYTES + 1))}],
            "row_count": 1,
            "truncated": False,
        }
        too_many = _rows(MAX_QUERY_ROWS + 1)
        capped = _rows(MAX_QUERY_ROWS)
        for label, result, accepted in (
            ("oversize", oversize, False),
            ("too-many-rows", too_many, False),
            ("capped-rows", capped, True),
        ):
            with (
                self.subTest(result=label),
                WrenFixture(analytics=True, query_result=result) as fixture,
                self.settings(**_dev(fixture.url)),
                _chat_logs() as logs,
            ):
                world = await self._world()
                async with open_wren_toolset(run=world["full_run"]) as toolset:
                    self.assertIsInstance(toolset, WrenToolset)
                    tools = await toolset.get_tools(self._ctx())
                    if accepted:
                        payload = _payload(
                            await toolset.call_tool(
                                "run_sql",
                                {"sql": SQL, "limit": 1},
                                self._ctx(),
                                tools["run_sql"],
                            )
                        )
                        self.assertEqual(payload["row_count"], MAX_QUERY_ROWS)
                        self.assertEqual(len(payload["rows"]), MAX_QUERY_ROWS)
                        self.assertIsNotNone(toolset.used_scope)
                    else:
                        with self.assertRaises(WrenMCPError) as caught:
                            await toolset.call_tool(
                                "run_sql",
                                {"sql": SQL, "limit": 1},
                                self._ctx(),
                                tools["run_sql"],
                            )
                        self._assert_mcp_error(caught.exception, ROW_SECRET)
                        self.assertIsNone(toolset.used_scope)
                    self.assertEqual(fixture.queries, [(SQL, 1)])
                self._assert_closed(toolset)
                text = "\n".join(logs.messages)
                self.assertNotIn(OVERSIZE, text)
                self.assertNotIn(ROW_SECRET, text)
                self.assertNotIn(fixture.url, text)

    async def test_cancellation_and_native_failure_close_the_client(self) -> None:
        secret = "secret-db-detail-8e21"
        with (
            WrenFixture(analytics=True, query_error=RuntimeError(secret)) as fixture,
            self.settings(**_dev(fixture.url)),
            _chat_logs() as logs,
        ):
            world = await self._world()
            async with open_wren_toolset(run=world["full_run"]) as toolset:
                self.assertIsInstance(toolset, WrenToolset)
                tools = await toolset.get_tools(self._ctx())
                with self.assertRaises(WrenMCPError) as caught:
                    await toolset.call_tool(
                        "run_sql",
                        {"sql": SQL, "limit": 1},
                        self._ctx(),
                        tools["run_sql"],
                    )
                self._assert_mcp_error(caught.exception, secret, SQL)
                self.assertIsNone(toolset.used_scope)
                self.assertEqual(fixture.queries, [(SQL, 1)])
            self._assert_closed(toolset)
            self.assertNotIn(secret, "\n".join(logs.messages))

        with WrenFixture(analytics=True) as fixture, self.settings(**_dev(fixture.url)):
            world = await self._world()
            holder: dict[str, WrenToolset] = {}

            async def body() -> None:
                async with open_wren_toolset(run=world["full_run"]) as toolset:
                    assert isinstance(toolset, WrenToolset)
                    holder["toolset"] = toolset
                    tools = await toolset.get_tools(self._ctx())
                    fixture.query_started.clear()
                    fixture.release.clear()
                    await toolset.call_tool(
                        "run_sql",
                        {"sql": SQL, "limit": 1},
                        self._ctx(),
                        tools["run_sql"],
                    )

            task = asyncio.create_task(body())
            try:
                self.assertTrue(await asyncio.to_thread(fixture.query_started.wait, 10))
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            finally:
                fixture.release.set()
                if not task.done():
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task
            self.assertIsNone(holder["toolset"].used_scope)
            self._assert_closed(holder["toolset"])

        with WrenFixture(analytics=True) as fixture, self.settings(**_dev(fixture.url)):
            url = fixture.url
            world = await self._world()
        with self.settings(**_dev(url)), self.assertRaises(WrenMCPError) as caught:
            async with open_wren_toolset(run=world["full_run"]):
                self.fail("connected to a stopped fixture")
        self._assert_mcp_error(caught.exception, url)

    async def test_operation_deadline_closes_the_connection(self) -> None:
        with WrenFixture(analytics=True) as fixture, self.settings(**_dev(fixture.url)):
            world = await self._world()
            async with open_wren_toolset(run=world["full_run"]) as toolset:
                self.assertIsInstance(toolset, WrenToolset)
                tools = await toolset.get_tools(self._ctx())
                fixture.release.clear()
                real_timeout = asyncio.timeout

                def short_timeout(seconds: float):
                    return real_timeout(0.2 if seconds == 60 else seconds)

                try:
                    with (
                        patch("chat.mcp.asyncio.timeout", short_timeout),
                        self.assertRaises(WrenMCPError),
                    ):
                        await toolset.call_tool(
                            "run_sql",
                            {"sql": SQL, "limit": 1},
                            self._ctx(),
                            tools["run_sql"],
                        )
                    self.assertTrue(fixture.query_started.is_set())
                    self.assertIsNone(toolset.used_scope)
                finally:
                    fixture.release.set()
            self._assert_closed(toolset)

    async def test_authorization_is_checked_before_and_after_the_call(self) -> None:
        with WrenFixture(analytics=True) as fixture, self.settings(**_dev(fixture.url)):
            world = await self._world()

            def drop_assignment() -> None:
                MembershipDataset.objects.filter(
                    membership__user=world["full"],
                    membership__department=world["sales"],
                    department_grant__dataset=world["customers"],
                ).delete()

            async with open_wren_toolset(run=world["full_run"]) as toolset:
                self.assertIsInstance(toolset, WrenToolset)
                tools = await toolset.get_tools(self._ctx())
                await sync_to_async(drop_assignment)()
                before = list(fixture.queries)
                with self.assertRaises(ChatError) as caught:
                    await toolset.call_tool(
                        "run_sql",
                        {"sql": SQL, "limit": 1},
                        self._ctx(),
                        tools["run_sql"],
                    )
                self.assertEqual(caught.exception.code, "authorization_changed")
                self._no_leak(caught.exception, SQL)
                self.assertEqual(fixture.queries, before)
                self.assertIsNone(toolset.used_scope)
            self._assert_closed(toolset)

            def restore() -> None:
                membership = world["full"].department_memberships.get(
                    department=world["sales"]
                )
                grant = world["sales"].dataset_grants.get(dataset=world["customers"])
                MembershipDataset.objects.create(
                    membership=membership, department_grant=grant
                )

            await sync_to_async(restore)()
            fixture.query_started.clear()
            fixture.release.clear()
            fixture.query_result = _rows(1, ROW_SECRET)
            task: asyncio.Task[Any] | None = None
            try:
                async with open_wren_toolset(run=world["full_run"]) as toolset:
                    self.assertIsInstance(toolset, WrenToolset)
                    tools = await toolset.get_tools(self._ctx())
                    task = asyncio.create_task(
                        toolset.call_tool(
                            "run_sql",
                            {"sql": SQL, "limit": 1},
                            self._ctx(),
                            tools["run_sql"],
                        )
                    )
                    self.assertTrue(
                        await asyncio.to_thread(fixture.query_started.wait, 10)
                    )
                    await sync_to_async(drop_assignment)()
                    fixture.release.set()
                    with self.assertRaises(ChatError) as caught:
                        await task
                    task = None
                    self.assertEqual(caught.exception.code, "authorization_changed")
                    self._no_leak(caught.exception, ROW_SECRET, SQL)
                    self.assertEqual(fixture.queries, [(SQL, 1)])
                    self.assertIsNone(toolset.used_scope)
                self._assert_closed(toolset)
            finally:
                fixture.release.set()
                if task is not None and not task.done():
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task

            def spoil_catalog() -> None:
                catalog = DatasetCatalogState.objects.get(pk=world["orders"].source_key)
                catalog.snapshot_sha256 = "ab" * 32
                catalog.save(update_fields=["snapshot_sha256"])

            await sync_to_async(restore)()
            baseline = list(fixture.calls)
            await sync_to_async(spoil_catalog)()
            await self._expect_idle(fixture, baseline, world["full_run"])

    async def test_runs_do_not_share_scope_or_contact_partial(self) -> None:
        with WrenFixture(analytics=True) as fixture, self.settings(**_dev(fixture.url)):
            world = await self._world()
            baseline = list(fixture.calls)
            async with open_wren_toolset(run=world["partial_run"]) as denied:
                self.assertIsNone(denied)
            self.assertEqual(fixture.calls, baseline)

            def extra_run() -> AnalyticsRun:
                user = self.make_account("other-full", groups=("AYCE Staff",))
                self.join(
                    user,
                    world["sales"],
                    datasets=(world["customers"], world["orders"]),
                )
                return self._lease(user, world["sales"].pk)

            other = await sync_to_async(extra_run)()
            fixture.query_started.clear()
            fixture.release.clear()
            task: asyncio.Task[Any] | None = None
            try:
                async with open_wren_toolset(run=world["full_run"]) as allowed:
                    self.assertIsInstance(allowed, WrenToolset)
                    self.assertIsNone(allowed.used_scope)
                    tools = await allowed.get_tools(self._ctx())
                    task = asyncio.create_task(
                        allowed.call_tool(
                            "run_sql",
                            {"sql": SQL, "limit": 1},
                            self._ctx(),
                            tools["run_sql"],
                        )
                    )
                    self.assertTrue(
                        await asyncio.to_thread(fixture.query_started.wait, 10)
                    )
                    during = list(fixture.calls)
                    async with open_wren_toolset(run=world["partial_run"]) as denied:
                        self.assertIsNone(denied)
                    self.assertEqual(fixture.calls, during)
                    async with open_wren_toolset(run=other) as sibling:
                        self.assertIsInstance(sibling, WrenToolset)
                        self.assertIsNone(sibling.used_scope)
                        self.assertIsNot(sibling, allowed)
                    fixture.release.set()
                    result = _payload(await task)
                    task = None
                    self.assertEqual(result, DEFAULT_QUERY_RESULT)
                    self.assertIsNotNone(allowed.used_scope)
                    self.assertIsNone(sibling.used_scope)
                self._assert_closed(allowed)
                self._assert_closed(sibling)
            finally:
                fixture.release.set()
                if task is not None and not task.done():
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task
