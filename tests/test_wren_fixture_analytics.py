"""Fixture-only checks for opt-in Wren analytics tools."""

from __future__ import annotations

import asyncio
import json
from typing import Any, cast

from django.test import SimpleTestCase
from fastmcp import Client
from fastmcp.exceptions import ToolError

from tests.wren_fixture import (
    DEFAULT_QUERY_RESULT,
    DENIED_TOOLS,
    QUERY_RELEASE_SECONDS,
    REVIEWED_TOOLS,
    WrenFixture,
)

SQL = "SELECT COUNT(*) AS total_orders FROM orders"


def _schema(tool: Any) -> dict[str, Any]:
    schema = tool.input_schema
    if not isinstance(schema, dict):
        raise AssertionError(f"{tool.name} has no object input schema")
    return schema


class WrenFixtureAnalyticsTests(SimpleTestCase):
    def test_default_mode_forbids_execution(self) -> None:
        async def exercise(fixture: WrenFixture) -> None:
            async with Client(fixture.url) as client:
                listed = await client.list_tools()
                self.assertEqual({tool.name for tool in listed}, {"get_mdl", "run_sql"})
                with self.assertRaises(ToolError):
                    await client.call_tool("run_sql", {"sql": SQL})
                with self.assertRaises(ToolError):
                    await client.call_tool("list_models", {})

        with WrenFixture() as fixture:
            asyncio.run(exercise(fixture))
            self.assertEqual(fixture.calls, ["run_sql"])
            self.assertEqual(fixture.queries, [])
            self.assertFalse(fixture.query_started.is_set())

    def test_reviewed_tools_execute_and_record_queries(self) -> None:
        async def exercise(fixture: WrenFixture) -> None:
            async with Client(fixture.url) as client:
                listed = {tool.name: tool for tool in await client.list_tools()}
                self.assertEqual(set(listed), {"get_mdl", *REVIEWED_TOOLS})
                run_sql = _schema(listed["run_sql"])
                self.assertEqual(run_sql["required"], ["sql"])
                self.assertEqual(run_sql["properties"]["sql"]["type"], "string")
                self.assertNotIn("limit", run_sql["required"])
                self.assertIs(listed["run_sql"].annotations.read_only_hint, True)
                self.assertEqual(
                    _schema(listed["describe_model"])["properties"]["name"]["type"],
                    "string",
                )
                self.assertEqual(_schema(listed["list_models"])["properties"], {})
                self.assertEqual(
                    _schema(listed["dry_plan"])["properties"]["sql"]["type"],
                    "string",
                )

                listed_models = await client.call_tool("list_models", {})
                described = await client.call_tool("describe_model", {"name": "orders"})
                instructions = await client.call_tool("get_instructions", {})
                planned = await client.call_tool("dry_plan", {"sql": SQL})
                omitted = await client.call_tool("run_sql", {"sql": SQL})
                capped = await client.call_tool("run_sql", {"sql": SQL, "limit": 1})
                explicit_null = await client.call_tool(
                    "run_sql", {"sql": "SELECT 1", "limit": None}
                )

            self.assertEqual(listed_models.data["models"], ["customers", "orders"])
            self.assertEqual(described.data["name"], "orders")
            self.assertEqual(instructions.data, "Synthetic Jaffle instructions.")
            self.assertEqual(planned.data, SQL)
            self.assertEqual(omitted.data, DEFAULT_QUERY_RESULT)
            self.assertEqual(capped.data, DEFAULT_QUERY_RESULT)
            self.assertEqual(explicit_null.data, DEFAULT_QUERY_RESULT)

        with WrenFixture(analytics=True) as fixture:
            asyncio.run(exercise(fixture))
            self.assertEqual(
                fixture.queries,
                [(SQL, None), (SQL, 1), ("SELECT 1", None)],
            )

    def test_query_waits_for_release_before_result_or_error(self) -> None:
        async def exercise(fixture: WrenFixture) -> None:
            fixture.release.clear()
            async with Client(fixture.url) as client:
                task = asyncio.create_task(
                    client.call_tool("run_sql", {"sql": SQL, "limit": 1})
                )
                self.assertTrue(await asyncio.to_thread(fixture.query_started.wait, 10))
                self.assertFalse(task.done())
                self.assertEqual(fixture.queries, [(SQL, 1)])
                planned = await client.call_tool("dry_plan", {"sql": SQL})
                self.assertEqual(planned.data, SQL)
                fixture.query_error = RuntimeError("warehouse down")
                fixture.release.set()
                with self.assertRaises(ToolError):
                    await task

                fixture.query_error = None
                fixture.query_result = {
                    "columns": ["n"],
                    "rows": [{"n": 7}],
                    "row_count": 1,
                    "truncated": False,
                }
                fixture.query_started.clear()
                fixture.release.clear()
                follow = asyncio.create_task(
                    client.call_tool("run_sql", {"sql": "SELECT 7", "limit": 7})
                )
                self.assertTrue(await asyncio.to_thread(fixture.query_started.wait, 10))
                fixture.release.set()
                result = await follow
                self.assertEqual(result.data["rows"], [{"n": 7}])

        with WrenFixture(analytics=True) as fixture:
            asyncio.run(exercise(fixture))

    def test_release_wait_is_bounded(self) -> None:
        async def exercise(fixture: WrenFixture) -> None:
            self.assertEqual(fixture.release_timeout, QUERY_RELEASE_SECONDS)
            fixture.release_timeout = 0.05
            fixture.release.clear()
            async with Client(fixture.url) as client:
                with self.assertRaises(ToolError):
                    await client.call_tool("run_sql", {"sql": SQL, "limit": 1})

        with WrenFixture(analytics=True) as fixture:
            asyncio.run(exercise(fixture))
            self.assertEqual(fixture.queries, [(SQL, 1)])
            self.assertTrue(fixture.query_started.is_set())

    def test_annotated_schema_still_executes(self) -> None:
        async def exercise(fixture: WrenFixture) -> None:
            async with Client(fixture.url) as client:
                listed = {tool.name: tool for tool in await client.list_tools()}
                run_sql = listed["run_sql"]
                schema = _schema(run_sql)
                limit = schema["properties"]["limit"]
                self.assertEqual(schema["title"], "run_sqlArguments")
                self.assertEqual(schema["description"], "Harmless schema annotation.")
                self.assertEqual(limit["default"], None)
                self.assertEqual(
                    limit["anyOf"], [{"type": "integer"}, {"type": "null"}]
                )
                self.assertTrue(
                    run_sql.annotations is None
                    or run_sql.annotations.read_only_hint is None
                )
                result = await client.call_tool("run_sql", {"sql": SQL, "limit": 1})
                self.assertEqual(result.data, DEFAULT_QUERY_RESULT)

        with WrenFixture(analytics=True, schema_variant="annotated") as fixture:
            asyncio.run(exercise(fixture))
            self.assertEqual(fixture.queries, [(SQL, 1)])

    def test_incompatible_arguments_do_not_execute(self) -> None:
        async def exercise(fixture: WrenFixture) -> None:
            async with Client(fixture.url) as client:
                listed = {tool.name: tool for tool in await client.list_tools()}
                self.assertNotIn("sql", _schema(listed["run_sql"])["properties"])
                self.assertIn("statement", _schema(listed["run_sql"])["properties"])
                described = _schema(listed["describe_model"])["properties"]
                self.assertNotIn("name", described)
                self.assertIn("token", _schema(listed["list_models"])["properties"])
                self.assertEqual(_schema(listed["dry_plan"])["properties"], {})
                for name, arguments in (
                    ("run_sql", {"sql": SQL, "limit": 1}),
                    ("run_sql", {"statement": 1}),
                    ("list_models", {}),
                    ("describe_model", {"name": "orders"}),
                    ("dry_plan", {"sql": SQL}),
                    ("get_instructions", {}),
                ):
                    with self.assertRaises(ToolError):
                        await client.call_tool(name, arguments)

        with WrenFixture(analytics=True, schema_variant="incompatible") as fixture:
            asyncio.run(exercise(fixture))
            self.assertEqual(fixture.queries, [])

    def test_unknown_and_write_tools_are_denied(self) -> None:
        async def absent(fixture: WrenFixture) -> None:
            async with Client(fixture.url) as client:
                names = {tool.name for tool in await client.list_tools()}
                self.assertTrue(set(DENIED_TOOLS).isdisjoint(names))
                with self.assertRaises(ToolError):
                    await client.call_tool("write_model", {"name": "orders"})
                with self.assertRaises(ToolError):
                    await client.call_tool("recall_queries", {"question": "orders"})

        async def present(fixture: WrenFixture) -> None:
            async with Client(fixture.url) as client:
                listed = {tool.name: tool for tool in await client.list_tools()}
                self.assertTrue(set(DENIED_TOOLS).issubset(listed))
                self.assertIs(listed["write_model"].annotations.read_only_hint, False)
                self.assertIs(listed["write_model"].annotations.destructive_hint, True)
                self.assertIs(listed["recall_queries"].annotations.read_only_hint, True)
                for name, arguments in (
                    ("write_model", {"name": "orders"}),
                    ("recall_queries", {"question": "orders"}),
                    ("get_context", {"question": "orders"}),
                    ("list_knowledge", {}),
                ):
                    with self.assertRaises(ToolError):
                        await client.call_tool(name, arguments)

        with WrenFixture(analytics=True) as fixture:
            asyncio.run(absent(fixture))
            self.assertEqual(fixture.queries, [])
            self.assertNotIn("write_model", fixture.calls)
        with WrenFixture(analytics=True, denied_tools=True) as fixture:
            asyncio.run(present(fixture))
            self.assertEqual(fixture.queries, [])
            self.assertTrue(set(DENIED_TOOLS).issubset(fixture.calls))

    def test_project_and_mdl_stay_mutable(self) -> None:
        async def exercise(fixture: WrenFixture) -> None:
            seen: list[str] = []
            fixture.on_mdl = lambda: seen.append("mdl")
            async with Client(fixture.url) as client:
                mdl = await client.call_tool("get_mdl", {})
                self.assertEqual(seen, ["mdl"])
                self.assertEqual(mdl.data["dataSource"], "duckdb")
                fixture.mdl["models"] = [{"name": "only", "columns": []}]
                listed = await client.call_tool("list_models", {})
                self.assertEqual(listed.data["models"], ["only"])
                fixture.project["name"] = "other-project"
                contents = await client.read_resource("wren://project")
            self.assertEqual(json.loads(contents[0].text)["name"], "other-project")

        with WrenFixture(analytics=True) as fixture:
            asyncio.run(exercise(fixture))
            self.assertEqual(
                fixture.calls,
                ["get_mdl", "list_models", "wren://project"],
            )

    def test_variant_flags_require_analytics(self) -> None:
        with self.assertRaises(ValueError):
            WrenFixture(schema_variant="annotated")
        with self.assertRaises(ValueError):
            WrenFixture(denied_tools=True)
        with self.assertRaises(ValueError):
            WrenFixture(analytics=True, schema_variant=cast(Any, "scoped"))
