"""Synthetic DEV scope admission. Policy lookups must not open Wren."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4

from asgiref.sync import async_to_sync
from django.contrib.auth.models import User
from django.test import SimpleTestCase

from accounts.models import AuthorizationState, DepartmentMembership
from chat.mcp_policy import (
    AnalyticsRun,
    eligible_dev_department_ids,
    require_scope_current,
    resolve_dev_scope,
)
from chat.service import ChatError
from chat.threads import acquire_run, create_thread, release_run
from datasets.discovery import DiscoveryError, catalog_snapshot, configured_source_key
from datasets.models import (
    Dataset,
    DatasetCatalogState,
    DepartmentDataset,
    MembershipDataset,
)
from datasets.services import refresh_catalog
from tests.support import GovernanceTestCase
from tests.wren_fixture import WrenFixture

ROOT = Path(__file__).resolve().parents[1]
_DEV = {
    "DEBUG": True,
    "AYCE_ANALYTICS_MODE": "dev_jaffle",
    "AYCE_DEV_JAFFLE_CONFIRMED": True,
    "AYCE_DEV_WREN_PROJECT": "jaffle-wren",
    "WREN_MCP_URL": "http://127.0.0.1:18080/mcp",
    "AYCE_DEV_WREN_MCP_URL": "http://127.0.0.1:18080/mcp",
}
_LEAK = "leak-marker"


def _at(url: str) -> dict[str, object]:
    return {**_DEV, "WREN_MCP_URL": url, "AYCE_DEV_WREN_MCP_URL": url}


def _dev_env(**overrides: str) -> dict[str, str]:
    env = {
        "DJANGO_DEBUG": "true",
        "AYCE_ANALYTICS_MODE": "dev_jaffle",
        "AYCE_DEV_JAFFLE_CONFIRMED": "true",
        "AYCE_DEV_WREN_PROJECT": "jaffle-wren",
        "WREN_MCP_URL": "http://127.0.0.1:18080/mcp",
        "AYCE_DEV_WREN_MCP_URL": "http://127.0.0.1:18080/mcp",
    }
    env.update(overrides)
    return env


def _setup(**overrides: str) -> subprocess.CompletedProcess[str]:
    if not os.environ.get("DJANGO_SECRET_KEY"):
        raise AssertionError("DJANGO_SECRET_KEY missing from the test environment")
    env = os.environ.copy()
    env["DJANGO_SETTINGS_MODULE"] = "config.settings"
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env.update(overrides)
    return subprocess.run(
        [sys.executable, "-c", "import django; django.setup()"],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )


def _output(result: subprocess.CompletedProcess[str]) -> str:
    return result.stdout + result.stderr


class DevAnalyticsStartupTests(SimpleTestCase):
    def test_off_ignores_dev_settings(self) -> None:
        result = _setup(
            AYCE_ANALYTICS_MODE="off",
            AYCE_DEV_WREN_MCP_URL=f"http://user:{_LEAK}@example.invalid/mcp?q=1",
            AYCE_DEV_WREN_PROJECT="not-jaffle",
            AYCE_DEV_JAFFLE_CONFIRMED="maybe",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(_LEAK, _output(result))

    def test_blank_mode_is_off(self) -> None:
        result = _setup(AYCE_ANALYTICS_MODE="   ")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_loopback_dev_jaffle_starts(self) -> None:
        urls = {
            "ipv4": "http://127.0.0.1:18080/mcp",
            "ipv6": "http://[::1]:18080/mcp",
            "trimmed": "  http://127.0.0.1:18080/mcp  ",
        }
        for label, url in urls.items():
            with self.subTest(label):
                result = _setup(**_dev_env(WREN_MCP_URL=url, AYCE_DEV_WREN_MCP_URL=url))
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_unknown_mode_names_the_setting_only(self) -> None:
        result = _setup(**_dev_env(AYCE_ANALYTICS_MODE="scoped"))
        output = _output(result)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("AYCE_ANALYTICS_MODE", output)
        self.assertNotIn("http://", output)
        self.assertNotIn("18080", output)

    def test_invalid_dev_configuration_names_settings_only(self) -> None:
        remote = "http://example.invalid:18080/mcp"
        userinfo = f"http://user:{_LEAK}@127.0.0.1:18080/mcp"
        query = f"http://127.0.0.1:18080/mcp?token={_LEAK}"
        fragment = f"http://127.0.0.1:18080/mcp#{_LEAK}"
        cases = {
            "debug": {"DJANGO_DEBUG": "false"},
            "confirmation": {"AYCE_DEV_JAFFLE_CONFIRMED": "false"},
            "confirmation-garbage": {"AYCE_DEV_JAFFLE_CONFIRMED": "maybe"},
            "project": {"AYCE_DEV_WREN_PROJECT": "other-project"},
            "remote": {"WREN_MCP_URL": remote, "AYCE_DEV_WREN_MCP_URL": remote},
            "userinfo": {"WREN_MCP_URL": userinfo, "AYCE_DEV_WREN_MCP_URL": userinfo},
            "query": {"WREN_MCP_URL": query, "AYCE_DEV_WREN_MCP_URL": query},
            "fragment": {
                "WREN_MCP_URL": fragment,
                "AYCE_DEV_WREN_MCP_URL": fragment,
            },
            "localhost": {
                "WREN_MCP_URL": "http://localhost:18080/mcp",
                "AYCE_DEV_WREN_MCP_URL": "http://localhost:18080/mcp",
            },
            "missing-port": {
                "WREN_MCP_URL": "http://127.0.0.1/mcp",
                "AYCE_DEV_WREN_MCP_URL": "http://127.0.0.1/mcp",
            },
            "mismatch": {"AYCE_DEV_WREN_MCP_URL": "http://127.0.0.1:18080/mcp/"},
            "other-port": {"AYCE_DEV_WREN_MCP_URL": "http://127.0.0.1:18081/mcp"},
        }
        expected = {
            "debug": "DJANGO_DEBUG",
            "confirmation": "AYCE_DEV_JAFFLE_CONFIRMED",
            "confirmation-garbage": "AYCE_DEV_JAFFLE_CONFIRMED",
            "project": "AYCE_DEV_WREN_PROJECT",
            "remote": "WREN_MCP_URL",
            "userinfo": "WREN_MCP_URL",
            "query": "WREN_MCP_URL",
            "fragment": "WREN_MCP_URL",
            "localhost": "WREN_MCP_URL",
            "missing-port": "WREN_MCP_URL",
            "mismatch": "WREN_MCP_URL",
            "other-port": "WREN_MCP_URL",
        }
        for label, overrides in cases.items():
            with self.subTest(label):
                result = _setup(**_dev_env(**overrides))
                output = _output(result)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(expected[label], output)
                self.assertNotIn(_LEAK, output)
                self.assertNotIn("example.invalid", output)
                self.assertNotIn("localhost", output)
                self.assertNotIn("18080", output)
                self.assertNotIn("18081", output)


class DevAnalyticsRuntimeTests(SimpleTestCase):
    def test_invalid_config_returns_no_scope_without_a_catalog(self) -> None:
        department_id = uuid4()
        remote = "http://example.invalid:9/mcp"
        cases = [
            {"AYCE_ANALYTICS_MODE": "off"},
            {"AYCE_ANALYTICS_MODE": "scoped"},
            {**_DEV, "DEBUG": False},
            {**_DEV, "AYCE_DEV_JAFFLE_CONFIRMED": False},
            {**_DEV, "AYCE_DEV_WREN_PROJECT": "other"},
            {**_DEV, "WREN_MCP_URL": "http://127.0.0.1:18081/mcp"},
            {
                **_DEV,
                "WREN_MCP_URL": remote,
                "AYCE_DEV_WREN_MCP_URL": remote,
            },
            {
                **_DEV,
                "WREN_MCP_URL": f"http://user:{_LEAK}@127.0.0.1:9/mcp",
                "AYCE_DEV_WREN_MCP_URL": f"http://user:{_LEAK}@127.0.0.1:9/mcp",
            },
            {
                **_DEV,
                "WREN_MCP_URL": "http://127.0.0.1:9/mcp?x=1",
                "AYCE_DEV_WREN_MCP_URL": "http://127.0.0.1:9/mcp?x=1",
            },
            {
                **_DEV,
                "WREN_MCP_URL": "http://127.0.0.1:9/mcp#x",
                "AYCE_DEV_WREN_MCP_URL": "http://127.0.0.1:9/mcp#x",
            },
            {
                **_DEV,
                "WREN_MCP_URL": "http://localhost:9/mcp",
                "AYCE_DEV_WREN_MCP_URL": "http://localhost:9/mcp",
            },
            {
                **_DEV,
                "WREN_MCP_URL": "http://127.0.0.1:18080/mcp/",
            },
        ]
        for index, case in enumerate(cases):
            with self.subTest(index), self.settings(**case):
                self.assertIsNone(
                    resolve_dev_scope(user_id=1, department_id=department_id)
                )

    def test_unbound_department_returns_no_scope(self) -> None:
        with self.settings(**_DEV):
            self.assertIsNone(resolve_dev_scope(user_id=1, department_id=None))


class SyntheticScopeTests(GovernanceTestCase):
    def _discover(self) -> None:
        actor = self.make_account("catalog-root", superuser=True)
        async_to_sync(refresh_catalog)(actor_id=actor.pk, revision=self.revision())

    def _world(self):
        sales = self.make_department("sales", "Sales")
        marketing = self.make_department("marketing", "Marketing")
        customers = Dataset.objects.get(model_name="customers")
        orders = Dataset.objects.get(model_name="orders")
        for department in (sales, marketing):
            self.grant(department, customers)
            self.grant(department, orders)
        alice = self.make_account("alice", groups=("AYCE Staff",))
        self.join(alice, sales, datasets=(customers, orders))
        self.join(alice, marketing)
        bob = self.make_account("bob", groups=("AYCE Staff",))
        self.join(bob, sales)
        return alice, bob, sales, marketing, customers, orders

    def _assert_none(self, user_id: int, department_id) -> None:
        self.assertIsNone(
            resolve_dev_scope(user_id=user_id, department_id=department_id)
        )

    def test_full_sales_scope_and_negative_identities(self) -> None:
        with WrenFixture() as fixture, self.settings(**_at(fixture.url)):
            self._discover()
            alice, bob, sales, marketing, customers, orders = self._world()
            calls = list(fixture.calls)
            state = DatasetCatalogState.objects.get(pk=configured_source_key())

            scope = resolve_dev_scope(user_id=alice.pk, department_id=sales.pk)
            self.assertIsNotNone(scope)
            assert scope is not None
            self.assertEqual(scope.endpoint, fixture.url)
            self.assertEqual(scope.source_key, state.source_key)
            self.assertEqual(scope.project_key, "jaffle-wren")
            self.assertEqual(scope.snapshot_sha256, state.snapshot_sha256)
            self.assertEqual(scope.structure_sha256, state.structure_sha256)
            self.assertEqual(
                [item.model_name for item in scope.datasets], ["customers", "orders"]
            )
            self.assertEqual(
                {item.dataset_id for item in scope.datasets},
                {customers.pk, orders.pk},
            )
            self.assertTrue(
                all(
                    item.definition_sha256
                    and item.structure_sha256 == scope.structure_sha256
                    for item in scope.datasets
                )
            )
            self.assertEqual(eligible_dev_department_ids(user_id=alice.pk), (sales.pk,))
            self.assertEqual(fixture.calls, calls)

            self._assert_none(alice.pk, None)
            self._assert_none(alice.pk, marketing.pk)
            self._assert_none(bob.pk, sales.pk)
            self.assertEqual(eligible_dev_department_ids(user_id=bob.pk), ())

            orders_only = self.make_account("orders-only", groups=("AYCE Staff",))
            self.join(orders_only, sales, datasets=(orders,))
            self._assert_none(orders_only.pk, sales.pk)

            root = self.make_account("root-open", superuser=True)
            self.join(root, sales)
            self._assert_none(root.pk, sales.pk)
            lead = self.make_account("lead-open", groups=("AYCE Lead",))
            self.join(lead, sales, role="lead")
            self._assert_none(lead.pk, sales.pk)
            delegated = self.make_account("delegated-open", groups=("AYCE Superadmin",))
            self.join(delegated, sales)
            self._assert_none(delegated.pk, sales.pk)

            granted_root = self.make_account("root-granted", superuser=True)
            self.join(granted_root, sales, datasets=(customers, orders))
            self.assertIsNotNone(
                resolve_dev_scope(user_id=granted_root.pk, department_id=sales.pk)
            )
            granted_lead = self.make_account("lead-granted", groups=("AYCE Lead",))
            self.join(granted_lead, sales, role="lead", datasets=(customers, orders))
            self.assertIsNotNone(
                resolve_dev_scope(user_id=granted_lead.pk, department_id=sales.pk)
            )

            split = self.make_account("split-user", groups=("AYCE Staff",))
            self.join(split, sales, datasets=(customers,))
            self.join(split, marketing, datasets=(orders,))
            self._assert_none(split.pk, sales.pk)
            self._assert_none(split.pk, marketing.pk)
            self.assertEqual(eligible_dev_department_ids(user_id=split.pk), ())

            plain = User.objects.create_user("plain-user", password="manage-pass-7f3a")
            self.join(plain, sales, datasets=(customers, orders))
            self._assert_none(plain.pk, sales.pk)

            alice.is_active = False
            alice.save(update_fields=["is_active"])
            self._assert_none(alice.pk, sales.pk)
            self.assertEqual(eligible_dev_department_ids(user_id=alice.pk), ())
            alice.is_active = True
            alice.save(update_fields=["is_active"])

            membership = DepartmentMembership.objects.get(user=alice, department=sales)
            membership.is_active = False
            membership.save(update_fields=["is_active"])
            self._assert_none(alice.pk, sales.pk)
            membership.is_active = True
            membership.save(update_fields=["is_active"])

            sales.is_active = False
            sales.save(update_fields=["is_active"])
            self._assert_none(alice.pk, sales.pk)
            self.assertEqual(eligible_dev_department_ids(user_id=alice.pk), ())
            sales.is_active = True
            sales.save(update_fields=["is_active"])

            customers.is_active = False
            customers.save(update_fields=["is_active"])
            self._assert_none(alice.pk, sales.pk)
            customers.is_active = True
            customers.is_available = False
            customers.save(update_fields=["is_active", "is_available"])
            self._assert_none(alice.pk, sales.pk)
            customers.is_available = True
            customers.validation_status = "unverified"
            customers.save(update_fields=["is_available", "validation_status"])
            self._assert_none(alice.pk, sales.pk)
            customers.validation_status = "metadata_discovered"
            customers.catalog_version = "ab" * 32
            customers.save(update_fields=["validation_status", "catalog_version"])
            self._assert_none(alice.pk, sales.pk)
            customers.catalog_version = state.snapshot_sha256
            customers.save(update_fields=["catalog_version"])

            grant = DepartmentDataset.objects.get(department=sales, dataset=customers)
            grant.needs_review = True
            grant.save(update_fields=["needs_review"])
            self._assert_none(alice.pk, sales.pk)
            grant.needs_review = False
            grant.save(update_fields=["needs_review"])
            self.assertIsNotNone(
                resolve_dev_scope(user_id=alice.pk, department_id=sales.pk)
            )

            marketing_membership = DepartmentMembership.objects.get(
                user=alice, department=marketing
            )
            created = [
                MembershipDataset.objects.create(
                    membership=marketing_membership,
                    department_grant=DepartmentDataset.objects.get(
                        department=marketing, dataset=dataset
                    ),
                )
                for dataset in (customers, orders)
            ]
            self.assertEqual(
                eligible_dev_department_ids(user_id=alice.pk),
                (marketing.pk, sales.pk),
            )
            for row in created:
                row.delete()
            self.assertEqual(eligible_dev_department_ids(user_id=alice.pk), (sales.pk,))

            other = "http://127.0.0.1:9/mcp"
            with self.settings(WREN_MCP_URL=other, AYCE_DEV_WREN_MCP_URL=other):
                self._assert_none(alice.pk, sales.pk)
            self.assertEqual(fixture.calls, calls)

            self._reject_mutated_identity(alice.pk, sales.pk, state)
            self.assertIsNotNone(
                resolve_dev_scope(user_id=alice.pk, department_id=sales.pk)
            )
            self.assertEqual(fixture.calls, calls)

    def _reject_mutated_identity(self, user_id: int, department_id, state) -> None:
        original = (
            state.execution_metadata_json,
            state.snapshot_sha256,
            state.structure_sha256,
            state.error_code,
            state.last_success_at,
        )

        def reject(mutate) -> None:
            stored = json.loads(original[0])
            mutate(stored)
            project_text = json.dumps(stored["project"])
            try:
                snapshot = catalog_snapshot(
                    state.source_key, project_text, stored["mdl"], project_text
                )
            except DiscoveryError:
                state.execution_metadata_json = json.dumps(stored)
                state.save(update_fields=["execution_metadata_json"])
            else:
                state.execution_metadata_json = snapshot.execution_metadata_json
                state.snapshot_sha256 = snapshot.snapshot_sha256
                state.structure_sha256 = snapshot.structure_sha256
                state.save()
            self._assert_none(user_id, department_id)
            (
                state.execution_metadata_json,
                state.snapshot_sha256,
                state.structure_sha256,
                state.error_code,
                state.last_success_at,
            ) = original
            state.save()

        def rename(stored) -> None:
            stored["project"]["name"] = "other-wren"

        def retarget(stored) -> None:
            stored["project"]["data_source"] = "postgres"
            stored["mdl"]["dataSource"] = "postgres"

        def add_model(stored) -> None:
            extra = deepcopy(stored["mdl"]["models"][0])
            extra["name"] = "payments"
            extra["tableReference"] = {
                **extra["tableReference"],
                "table": "payments",
            }
            stored["mdl"]["models"].append(extra)

        def drop_model(stored) -> None:
            stored["mdl"]["models"] = [
                model
                for model in stored["mdl"]["models"]
                if model["name"] != "customers"
            ]

        def duplicate(stored) -> None:
            stored["mdl"]["models"].append(deepcopy(stored["mdl"]["models"][0]))

        def ref_sql(stored) -> None:
            model = next(
                item for item in stored["mdl"]["models"] if item["name"] == "customers"
            )
            model.pop("tableReference", None)
            model["refSql"] = "select 1"

        def wrong_table(stored) -> None:
            model = next(
                item for item in stored["mdl"]["models"] if item["name"] == "customers"
            )
            model["tableReference"]["table"] = "customers_raw"

        def drop_relationship(stored) -> None:
            stored["mdl"]["relationships"] = []

        for mutate in (
            rename,
            retarget,
            add_model,
            drop_model,
            duplicate,
            ref_sql,
            wrong_table,
            drop_relationship,
        ):
            with self.subTest(mutate=mutate.__name__):
                reject(mutate)

        state.last_success_at = None
        state.save(update_fields=["last_success_at"])
        self._assert_none(user_id, department_id)
        state.last_success_at = original[4]
        state.error_code = "connection_failed"
        state.save(update_fields=["last_success_at", "error_code"])
        self._assert_none(user_id, department_id)
        state.error_code = ""
        state.snapshot_sha256 = ""
        state.save(update_fields=["error_code", "snapshot_sha256"])
        self._assert_none(user_id, department_id)
        state.snapshot_sha256 = original[1]
        state.execution_metadata_json = ""
        state.save(update_fields=["snapshot_sha256", "execution_metadata_json"])
        self._assert_none(user_id, department_id)
        state.execution_metadata_json = original[0]
        stored = json.loads(original[0])
        stored["project"]["name"] = "tampered"
        state.execution_metadata_json = json.dumps(stored)
        state.save()
        self._assert_none(user_id, department_id)
        (
            state.execution_metadata_json,
            state.snapshot_sha256,
            state.structure_sha256,
            state.error_code,
            state.last_success_at,
        ) = original
        state.save()

    def test_require_scope_current_tracks_lease_and_versions(self) -> None:
        with WrenFixture() as fixture, self.settings(**_at(fixture.url)):
            self._discover()
            alice, bob, sales, marketing, customers, _orders = self._world()
            scope = resolve_dev_scope(user_id=alice.pk, department_id=sales.pk)
            self.assertIsNotNone(scope)
            assert scope is not None
            thread = create_thread(owner_id=alice.pk, department_id=sales.pk)
            token = acquire_run(
                owner_id=alice.pk, thread_id=thread.pk, revision=thread.revision
            )
            self.addCleanup(
                release_run,
                owner_id=alice.pk,
                thread_id=thread.pk,
                run_token=token,
            )
            run = AnalyticsRun(alice.pk, thread.pk, sales.pk, self.epoch(), token)
            calls = list(fixture.calls)
            with (
                patch(
                    "datasets.discovery.catalog_snapshot",
                    side_effect=AssertionError("reparse"),
                ),
                patch(
                    "datasets.discovery.discover_catalog",
                    side_effect=AssertionError("discover"),
                ),
                patch(
                    "datasets.discovery.read_catalog_snapshot",
                    side_effect=AssertionError("read"),
                ),
            ):
                require_scope_current(run=run, scope=scope)
            self.assertEqual(fixture.calls, calls)

            foreign = create_thread(owner_id=bob.pk, department_id=sales.pk)
            with self.assertRaises(ChatError) as caught:
                require_scope_current(
                    run=AnalyticsRun(
                        alice.pk, foreign.pk, sales.pk, self.epoch(), token
                    ),
                    scope=scope,
                )
            self.assertEqual(caught.exception.code, "thread_not_found")

            with self.assertRaises(ChatError) as caught:
                require_scope_current(
                    run=AnalyticsRun(
                        alice.pk, thread.pk, marketing.pk, self.epoch(), token
                    ),
                    scope=scope,
                )
            self.assertEqual(caught.exception.code, "authorization_changed")

            general = create_thread(owner_id=alice.pk)
            general_token = acquire_run(
                owner_id=alice.pk,
                thread_id=general.pk,
                revision=general.revision,
            )
            self.addCleanup(
                release_run,
                owner_id=alice.pk,
                thread_id=general.pk,
                run_token=general_token,
            )
            with self.assertRaises(ChatError) as caught:
                require_scope_current(
                    run=AnalyticsRun(
                        alice.pk, general.pk, None, self.epoch(), general_token
                    ),
                    scope=scope,
                )
            self.assertEqual(caught.exception.code, "authorization_changed")

            state = AuthorizationState.objects.get(pk=1)
            original_epoch = state.epoch
            state.epoch = original_epoch + 1
            state.save(update_fields=["epoch"])
            with self.assertRaises(ChatError) as caught:
                require_scope_current(run=run, scope=scope)
            self.assertEqual(caught.exception.code, "authorization_changed")
            state.epoch = original_epoch
            state.save(update_fields=["epoch"])

            catalog = DatasetCatalogState.objects.get(pk=scope.source_key)
            catalog.snapshot_sha256 = "cd" * 32
            catalog.save(update_fields=["snapshot_sha256"])
            with self.assertRaises(ChatError) as caught:
                require_scope_current(run=run, scope=scope)
            self.assertEqual(caught.exception.code, "authorization_changed")
            catalog.snapshot_sha256 = scope.snapshot_sha256
            catalog.error_code = "connection_failed"
            catalog.save(update_fields=["snapshot_sha256", "error_code"])
            with self.assertRaises(ChatError) as caught:
                require_scope_current(run=run, scope=scope)
            self.assertEqual(caught.exception.code, "authorization_changed")
            catalog.error_code = ""
            catalog.save(update_fields=["error_code"])

            original_definition = customers.definition_sha256
            customers.definition_sha256 = "0" * 64
            customers.save(update_fields=["definition_sha256"])
            with self.assertRaises(ChatError) as caught:
                require_scope_current(run=run, scope=scope)
            self.assertEqual(caught.exception.code, "authorization_changed")
            customers.definition_sha256 = original_definition
            customers.save(update_fields=["definition_sha256"])

            other = "http://127.0.0.1:9/mcp"
            with (
                self.settings(WREN_MCP_URL=other, AYCE_DEV_WREN_MCP_URL=other),
                self.assertRaises(ChatError) as caught,
            ):
                require_scope_current(run=run, scope=scope)
            self.assertEqual(caught.exception.code, "authorization_changed")

            assignment = MembershipDataset.objects.get(
                membership__user=alice,
                membership__department=sales,
                department_grant__dataset=customers,
            )
            assignment.delete()
            with self.assertRaises(ChatError) as caught:
                require_scope_current(run=run, scope=scope)
            self.assertEqual(caught.exception.code, "authorization_changed")

            alice.is_active = False
            alice.save(update_fields=["is_active"])
            with self.assertRaises(ChatError) as caught:
                require_scope_current(run=run, scope=scope)
            self.assertEqual(caught.exception.code, "authentication_required")
            self.assertEqual(fixture.calls, calls)
