"""Read-only boundary around native Wren execution on the synthetic fixture.

Source evidence from the installed Wren package (not application code):

* ``wren/policy.py::validate_sql_policy`` always calls
  ``validate_read_only_ast``, independent of ``strict_mode``. Only
  ``Select``/``SetOperation``/``Subquery``/``Values`` roots are accepted and
  the whole tree is scanned for write, session, and unmodelled nodes
  (``Insert``, ``Update``, ``Delete``, ``Create``, ``Drop``, ``Alter``,
  ``Copy``, ``Into``, ``Set``, ``Use``, ``Lock``, ``Command``, ``Block``, and
  the DDL/DML markers).
* ``sqlglot 30.30.0`` ``parse_one`` returns ``exp.Block`` for more than one
  statement, so ``SELECT 1; DROP TABLE orders`` fails the root check before
  planning.
* Wren's MCP ``run_sql`` always supplies a limit, and
  ``DuckDBConnector.query`` then executes ``SELECT * FROM (<sql>) AS _q
  LIMIT n``; a surviving interior semicolon is a syntax error, not a second
  batch.
* ``DuckDBConnector._attach_database`` attaches each ``.duckdb`` file with
  ``ATTACH DATABASE '<file>' AS "<alias>" (READ_ONLY)``, so the fixture is
  immutable at the storage layer even if a statement evaded the policy.

AYCE therefore forwards model SQL verbatim instead of adding a second SQL
parser or a regex deny list. A local filter would not be an isolation
boundary and could only rewrite what Wren validates. These tests pin that
contract: SQL reaches the native side unchanged with a bounded limit,
planning never executes, and a disposable copy of the approved DuckDB
fixture rejects every write through a read-only attach.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from asgiref.sync import sync_to_async
from django.contrib.auth.models import User
from pydantic_ai import RunContext
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage

from chat.mcp import MAX_QUERY_ROWS, WrenMCPError, WrenToolset, open_wren_toolset
from chat.mcp_policy import AnalyticsRun
from chat.threads import acquire_run, create_thread, release_run
from datasets.discovery import configured_source_key
from datasets.models import Dataset, DatasetCatalogState
from datasets.services import refresh_catalog
from tests.support import GovernanceTestCase
from tests.wren_fixture import WrenFixture

COUNT_SQL = "SELECT COUNT(*) AS total_orders FROM orders"
MCP_ERROR = "Wren MCP operation failed."
HOSTILE_SQL = (
    "SELECT 1; DROP TABLE jaffle_shop.main.orders",
    "SELECT 1; DELETE FROM orders",
    "DROP TABLE jaffle_shop.main.orders",
    "DELETE FROM orders",
    "UPDATE orders SET total = 0",
    "INSERT INTO orders VALUES (1)",
    "CREATE TABLE pwn AS SELECT 1",
    "CREATE OR REPLACE TABLE orders AS SELECT 1",
    "ALTER TABLE orders RENAME TO pwn",
    "COPY (SELECT 1) TO '/tmp/ayce-wren-readonly.csv'",
    "COPY orders TO '/tmp/ayce-wren-readonly.parquet'",
    "ATTACH DATABASE '/tmp/ayce-wren-readonly.duckdb' AS pwn",
    "SET memory_limit = '1GB'",
    "PRAGMA database_list",
    "INSTALL httpfs",
    "SELECT * FROM read_csv('/etc/passwd')",
    "WITH x AS (DELETE FROM orders RETURNING *) SELECT * FROM x",
    "SELECT * INTO pwn FROM orders",
)
FIXTURE_ENV = "AYCE_WREN_JAFFLE_DUCKDB"
APPROVED_SHA256 = "290fc8e5cf4e0f8965f1e1c6a98a439e1fcd44396cd99b39a759398c36e4e67a"
WRITE_STATEMENTS = (
    "DROP TABLE jaffle.main.orders",
    "DELETE FROM jaffle.main.orders",
    "INSERT INTO jaffle.main.orders SELECT * FROM jaffle.main.orders",
    "CREATE TABLE jaffle.main.pwn (x INTEGER)",
    "CREATE OR REPLACE TABLE jaffle.main.orders AS SELECT 1",
    "ALTER TABLE jaffle.main.orders RENAME TO pwn",
)


def _dev(url: str) -> dict[str, object]:
    return {
        "DEBUG": True,
        "AYCE_ANALYTICS_MODE": "dev_jaffle",
        "AYCE_DEV_JAFFLE_CONFIRMED": True,
        "AYCE_DEV_WREN_PROJECT": "jaffle-wren",
        "WREN_MCP_URL": url,
        "AYCE_DEV_WREN_MCP_URL": url,
    }


class ReadOnlyForwardingTests(GovernanceTestCase):
    """AYCE forwards SQL; Wren and DuckDB own the read-only decision."""

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

    async def _world(self) -> dict[str, object]:
        await self._catalog()
        self._seq += 1
        suffix = str(self._seq)

        def build() -> dict[str, object]:
            sales = self.make_department(f"ro-sales-{suffix}", f"RO Sales {suffix}")
            key = configured_source_key()
            datasets = tuple(
                Dataset.objects.filter(
                    model_name__in=("customers", "orders"), source_key=key
                )
            )
            self.assertEqual(len(datasets), 2)
            for dataset in datasets:
                self.grant(sales, dataset)
            full = self.make_account(f"ro-full-{suffix}", groups=("AYCE Staff",))
            self.join(full, sales, datasets=datasets)
            return {"run": self._lease(full, sales.pk)}

        return await sync_to_async(build)()

    def _assert_closed(self, toolset: WrenToolset) -> None:
        native = toolset.wrapped
        self.assertEqual(native._running_count, 0)
        self.assertIsNone(native._exit_stack)
        self.assertFalse(native.client.is_connected())

    async def test_query_surface_returns_the_synthetic_count(self) -> None:
        with (
            WrenFixture(analytics=True) as fixture,
            self.settings(**_dev(fixture.url)),
        ):
            world = await self._world()
            async with open_wren_toolset(run=world["run"]) as toolset:
                self.assertIsInstance(toolset, WrenToolset)
                tool = (await toolset.get_tools(self._ctx()))["run_sql"]
                value = await toolset.call_tool(
                    "run_sql", {"sql": COUNT_SQL}, self._ctx(), tool
                )
                payload = value if isinstance(value, dict) else json.loads(value)
                self.assertEqual(payload["rows"], [{"total_orders": 99}])
                self.assertEqual(payload["row_count"], 1)
                self.assertEqual(fixture.queries, [(COUNT_SQL, MAX_QUERY_ROWS)])
                self.assertIsNotNone(toolset.used_scope)
            self._assert_closed(toolset)

    async def test_write_sql_is_forwarded_verbatim_and_refused(self) -> None:
        secret = "read-only policy refusal-4c1e"
        with (
            WrenFixture(analytics=True, query_error=RuntimeError(secret)) as fixture,
            self.settings(**_dev(fixture.url)),
        ):
            world = await self._world()
            async with open_wren_toolset(run=world["run"]) as toolset:
                self.assertIsInstance(toolset, WrenToolset)
                tool = (await toolset.get_tools(self._ctx()))["run_sql"]
                for sql in HOSTILE_SQL:
                    with (
                        self.subTest(sql=sql),
                        self.assertRaises(WrenMCPError) as caught,
                    ):
                        await toolset.call_tool(
                            "run_sql", {"sql": sql}, self._ctx(), tool
                        )
                    self.assertEqual(str(caught.exception), MCP_ERROR)
                    self.assertNotIn(secret, str(caught.exception))
                    self.assertEqual(fixture.queries[-1], (sql, MAX_QUERY_ROWS))
                self.assertEqual(len(fixture.queries), len(HOSTILE_SQL))
                self.assertIsNone(toolset.used_scope)
            self._assert_closed(toolset)

    async def test_dry_plan_forwards_verbatim_without_execution(self) -> None:
        with (
            WrenFixture(analytics=True) as fixture,
            self.settings(**_dev(fixture.url)),
        ):
            world = await self._world()
            async with open_wren_toolset(run=world["run"]) as toolset:
                self.assertIsInstance(toolset, WrenToolset)
                tool = (await toolset.get_tools(self._ctx()))["dry_plan"]
                for sql in HOSTILE_SQL:
                    with self.subTest(sql=sql):
                        planned = await toolset.call_tool(
                            "dry_plan", {"sql": sql}, self._ctx(), tool
                        )
                        self.assertEqual(planned, sql)
                self.assertEqual(fixture.queries, [])
            self._assert_closed(toolset)


@unittest.skipUnless(
    importlib.util.find_spec("duckdb") is not None, "duckdb is not installed"
)
class FixtureWriteProtectionTests(unittest.TestCase):
    """A read-only attach leaves the disposable fixture copy unchanged.

    Opt-in: set ``AYCE_WREN_JAFFLE_DUCKDB`` to the approved synthetic fixture.
    The proof copies the file, attaches the copy ``(READ_ONLY)``, attempts
    every catalog and data write, and re-hashes both files. The catalog,
    row counts, and both digests must be unchanged afterwards.
    """

    def test_read_only_attach_rejects_writes(self) -> None:
        import duckdb

        source = Path(os.environ.get(FIXTURE_ENV, ""))
        if not source.is_file():
            self.skipTest(f"{FIXTURE_ENV} is not set to the approved fixture")
        digest = hashlib.sha256(source.read_bytes()).hexdigest()
        if digest != APPROVED_SHA256:
            self.skipTest(f"{FIXTURE_ENV} is not the approved synthetic fixture")

        with tempfile.TemporaryDirectory() as directory:
            copy = Path(directory) / source.name
            shutil.copyfile(source, copy)
            escaped = str(copy).replace("'", "''")
            connection = duckdb.connect()
            try:
                connection.execute(f"ATTACH DATABASE '{escaped}' AS jaffle (READ_ONLY)")
                total = connection.execute(
                    "SELECT COUNT(*) FROM jaffle.main.orders"
                ).fetchone()[0]
                self.assertEqual(total, 99)
                for statement in WRITE_STATEMENTS:
                    with (
                        self.subTest(statement=statement),
                        self.assertRaises(duckdb.Error),
                    ):
                        connection.execute(statement)
                after = connection.execute(
                    "SELECT COUNT(*) FROM jaffle.main.orders"
                ).fetchone()[0]
                self.assertEqual(after, 99)
                created = connection.execute(
                    "SELECT COUNT(*) FROM duckdb_tables() WHERE table_name = 'pwn'"
                ).fetchone()[0]
                self.assertEqual(created, 0)
            finally:
                connection.close()
            self.assertEqual(hashlib.sha256(copy.read_bytes()).hexdigest(), digest)
        self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(), digest)
