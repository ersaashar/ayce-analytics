"""Metadata discovery and source isolation tests."""

from __future__ import annotations

import hashlib
import importlib
from copy import deepcopy

from asgiref.sync import async_to_sync
from django.test import SimpleTestCase, override_settings

from accounts import services as account_services
from accounts.authorization import ManagementError, active_dataset_queryset
from datasets.discovery import (
    _argument_free_schema,
    configured_source_key,
    discover_catalog,
)
from datasets.models import (
    Dataset,
    DatasetCatalogState,
    DepartmentDataset,
    MembershipDataset,
)
from datasets.services import (
    _apply_discovery_snapshot,
    refresh_catalog,
    replace_department_datasets,
    replace_membership_datasets,
    update_dataset,
)
from tests.support import GovernanceTestCase
from tests.wren_fixture import MDL, WrenFixture


class ConfiguredSourceKeyTests(SimpleTestCase):
    @override_settings(WREN_MCP_URL="")
    def test_blank_endpoint_has_no_source(self) -> None:
        self.assertIsNone(configured_source_key())

    @override_settings(WREN_MCP_URL="  https://example.test/mcp  ")
    def test_endpoint_is_hashed_and_trimmed(self) -> None:
        expected = hashlib.sha256(b"https://example.test/mcp").hexdigest()[:59]
        self.assertEqual(configured_source_key(), "wren-" + expected)
        with self.settings(WREN_MCP_URL="https://staging.example.test/mcp"):
            self.assertNotEqual(configured_source_key(), "wren-" + expected)

    def test_cancellation_closes_loopback_client(self) -> None:
        import asyncio
        import threading

        started = threading.Event()
        release = threading.Event()
        with WrenFixture() as fixture, self.settings(WREN_MCP_URL=fixture.url):

            def block_tool() -> None:
                started.set()
                release.wait(10)

            fixture.on_mdl = block_tool

            async def exercise() -> None:
                task = asyncio.create_task(discover_catalog())
                try:
                    self.assertTrue(await asyncio.to_thread(started.wait, 10))
                    task.cancel()
                    release.set()
                    with self.assertRaises(asyncio.CancelledError):
                        await task
                finally:
                    release.set()

            asyncio.run(exercise())


class LegacyCatalogMigrationTests(GovernanceTestCase):
    def test_legacy_grants_and_subsets_are_preserved_but_flagged(self) -> None:
        from django.apps import apps
        from django.db import connection

        dataset = self.make_dataset("orders")
        department = self.make_department("finance")
        user = self.make_account("legacy-member")
        membership = self.join(user, department, datasets=(dataset,))
        grant = department.dataset_grants.get(dataset=dataset)
        migration = importlib.import_module(
            "datasets.migrations.0003_mark_legacy_unavailable"
        )
        with connection.schema_editor() as editor:
            migration.mark_legacy_unavailable(apps, editor)
        dataset.refresh_from_db()
        grant.refresh_from_db()
        self.assertFalse(dataset.is_available)
        self.assertEqual(dataset.validation_status, "unverified")
        self.assertTrue(grant.needs_review)
        self.assertTrue(
            membership.dataset_assignments.filter(department_grant=grant).exists()
        )

    def test_catalog_state_admin_is_read_only(self) -> None:
        from django.test import Client

        from datasets.models import DatasetCatalogState

        state = DatasetCatalogState.objects.create(source_key="wren-test")
        admin = self.make_account("catalog-admin", superuser=True)
        client = Client()
        client.force_login(admin)
        self.assertEqual(
            client.get("/admin/datasets/datasetcatalogstate/").status_code, 200
        )
        response = client.post(
            f"/admin/datasets/datasetcatalogstate/{state.pk}/change/",
            {"source_key": "modified"},
        )
        self.assertEqual(response.status_code, 403)
        self.assertTrue(DatasetCatalogState.objects.filter(pk="wren-test").exists())


class ArgumentFreeSchemaTests(SimpleTestCase):
    def test_live_wren_title_is_argument_free(self) -> None:
        live = {
            "properties": {},
            "title": "get_mdlArguments",
            "type": "object",
        }
        empty = {"type": "object", "properties": {}}
        for base in (live, empty):
            self.assertTrue(_argument_free_schema(base))
            self.assertTrue(
                _argument_free_schema({**base, "additionalProperties": False})
            )
            self.assertTrue(_argument_free_schema({**base, "required": []}))
            self.assertTrue(
                _argument_free_schema(
                    {
                        **base,
                        "description": "Return the full compiled MDL.",
                        "$schema": "https://json-schema.org/draft/2020-12/schema",
                    }
                )
            )
        self.assertFalse(_argument_free_schema(None))
        self.assertFalse(_argument_free_schema({"properties": {}}))
        self.assertFalse(
            _argument_free_schema(
                {"type": "object", "properties": {"note": {"type": "string"}}}
            )
        )
        self.assertFalse(_argument_free_schema({**empty, "required": ["argument"]}))
        self.assertFalse(_argument_free_schema({**empty, "additionalProperties": True}))
        self.assertFalse(_argument_free_schema({**empty, "patternProperties": {}}))
        self.assertFalse(_argument_free_schema({**empty, "$defs": {}}))


class MetadataRefreshTests(GovernanceTestCase):
    def test_loopback_discovery_and_lost_assignment_confirmation(self) -> None:
        with WrenFixture() as fixture, self.settings(WREN_MCP_URL=fixture.url):
            actor = self.make_account("catalog-root", superuser=True)
            result = async_to_sync(refresh_catalog)(
                actor_id=actor.pk, revision=self.revision()
            )
            self.assertEqual(result["created"], 2)
            state = DatasetCatalogState.objects.get(source_key=configured_source_key())
            self.assertTrue(state.execution_metadata_json)
            orders = Dataset.objects.get(model_name="orders")
            self.assertEqual(
                (orders.project_key, orders.catalog, orders.schema),
                ("jaffle-wren", "jaffle_shop", "main"),
            )
            self.assertEqual(
                fixture.calls, ["wren://project", "get_mdl", "wren://project"]
            )
            self.assertEqual(DepartmentDataset.objects.count(), 0)
            department = self.make_department("test")
            user = self.make_account("analyst", groups=("AYCE Staff",))
            grant = self.grant(department, orders)
            membership = self.join(user, department, datasets=(orders,))
            self.assertTrue(
                active_dataset_queryset(user_id=user.pk).filter(pk=orders.pk).exists()
            )
            fixture.mdl["models"] = [
                model for model in fixture.mdl["models"] if model["name"] != "orders"
            ]
            result = async_to_sync(refresh_catalog)(
                actor_id=actor.pk, revision=self.revision()
            )
            self.assertEqual(result["missing"], 1)
            grant.refresh_from_db()
            self.assertTrue(grant.needs_review)
            self.assertTrue(
                MembershipDataset.objects.filter(
                    membership=membership, department_grant=grant
                ).exists()
            )
            self.assertFalse(
                active_dataset_queryset(user_id=user.pk).filter(pk=orders.pk).exists()
            )
            account_services.replace_user_memberships(
                actor_id=actor.pk,
                revision=self.revision(),
                user_id=user.pk,
                memberships=[
                    account_services.MembershipInput(
                        department_id=department.pk,
                        membership_role="staff",
                        is_active=True,
                        dataset_ids=(orders.pk,),
                    )
                ],
            )
            self.assertTrue(
                MembershipDataset.objects.filter(
                    membership=membership, department_grant=grant
                ).exists()
            )
            with self.assertRaises(ManagementError):
                account_services.replace_user_memberships(
                    actor_id=actor.pk,
                    revision=self.revision(),
                    user_id=self.make_account("new-analyst", groups=("AYCE Staff",)).pk,
                    memberships=[
                        account_services.MembershipInput(
                            department_id=department.pk,
                            membership_role="staff",
                            is_active=True,
                            dataset_ids=(orders.pk,),
                        )
                    ],
                )
            fixture.mdl["models"].append(
                deepcopy(
                    next(model for model in MDL["models"] if model["name"] == "orders")
                )
            )
            async_to_sync(refresh_catalog)(actor_id=actor.pk, revision=self.revision())
            grant.refresh_from_db()
            self.assertTrue(grant.needs_review)
            replace_department_datasets(
                actor_id=actor.pk,
                revision=self.revision(),
                department_id=department.pk,
                dataset_ids=[orders.pk],
                confirmed_dataset_ids=[orders.pk],
            )
            grant.refresh_from_db()
            self.assertFalse(grant.needs_review)
            self.assertFalse(
                MembershipDataset.objects.filter(
                    membership=membership, department_grant=grant
                ).exists()
            )
            self.assertFalse(
                active_dataset_queryset(user_id=user.pk).filter(pk=orders.pk).exists()
            )
            replace_membership_datasets(
                actor_id=actor.pk,
                revision=self.revision(),
                department_id=department.pk,
                user_id=user.pk,
                dataset_ids=[orders.pk],
            )
            self.assertTrue(
                active_dataset_queryset(user_id=user.pk).filter(pk=orders.pk).exists()
            )

    def test_invalid_snapshot_preserves_previous_catalog(self) -> None:
        with WrenFixture() as fixture, self.settings(WREN_MCP_URL=fixture.url):
            actor = self.make_account("catalog-root", superuser=True)
            async_to_sync(refresh_catalog)(actor_id=actor.pk, revision=self.revision())
            before = self.revision()
            retained = DatasetCatalogState.objects.get(
                pk=configured_source_key()
            ).execution_metadata_json
            self.assertTrue(retained)
            fixture.mdl["models"].append(deepcopy(fixture.mdl["models"][0]))
            with self.assertRaises(ManagementError) as caught:
                async_to_sync(refresh_catalog)(actor_id=actor.pk, revision=before)
            self.assertEqual(caught.exception.code, "catalog_unavailable")
            self.assertEqual(self.revision(), before)
            self.assertEqual(Dataset.objects.filter(is_available=True).count(), 2)
            failed_state = DatasetCatalogState.objects.get(pk=configured_source_key())
            self.assertEqual(failed_state.error_code, "invalid_metadata")
            self.assertEqual(failed_state.execution_metadata_json, retained)

    def test_project_switch_during_probe_rejects_the_snapshot(self) -> None:
        with WrenFixture() as fixture, self.settings(WREN_MCP_URL=fixture.url):
            actor = self.make_account("catalog-root", superuser=True)
            fixture.on_mdl = lambda: fixture.project.update(name="another-project")
            with self.assertRaises(ManagementError) as caught:
                async_to_sync(refresh_catalog)(
                    actor_id=actor.pk, revision=self.revision()
                )
            self.assertEqual(caught.exception.code, "catalog_unavailable")
            self.assertFalse(Dataset.objects.exists())
            self.assertEqual(
                DatasetCatalogState.objects.get(pk=configured_source_key()).error_code,
                "invalid_metadata",
            )

    def test_unsupported_contract_and_missing_resource_are_not_empty_success(
        self,
    ) -> None:
        actor = self.make_account("catalog-root", superuser=True)
        for options in (
            {"required_argument": True},
            {"project_resource": False},
            {"open_schema": True},
        ):
            with (
                WrenFixture(**options) as fixture,
                self.settings(WREN_MCP_URL=fixture.url),
            ):
                with self.assertRaises(ManagementError) as caught:
                    async_to_sync(refresh_catalog)(
                        actor_id=actor.pk, revision=self.revision()
                    )
                self.assertEqual(caught.exception.code, "catalog_unavailable")
                self.assertEqual(
                    DatasetCatalogState.objects.get(
                        pk=configured_source_key()
                    ).error_code,
                    "unsupported_metadata",
                )
                self.assertFalse(Dataset.objects.exists())

    def test_titled_empty_schema_refreshes(self) -> None:
        with (
            WrenFixture(schema_title="get_mdlArguments") as fixture,
            self.settings(WREN_MCP_URL=fixture.url),
        ):
            actor = self.make_account("catalog-root", superuser=True)
            result = async_to_sync(refresh_catalog)(
                actor_id=actor.pk, revision=self.revision()
            )
            self.assertEqual(result["created"], 2)
            self.assertEqual(
                fixture.calls, ["wren://project", "get_mdl", "wren://project"]
            )
            self.assertEqual(
                DatasetCatalogState.objects.get(pk=configured_source_key()).error_code,
                "",
            )

    def test_oversized_metadata_keeps_last_success(self) -> None:
        with WrenFixture() as fixture, self.settings(WREN_MCP_URL=fixture.url):
            actor = self.make_account("catalog-root", superuser=True)
            async_to_sync(refresh_catalog)(actor_id=actor.pk, revision=self.revision())
            state = DatasetCatalogState.objects.get(pk=configured_source_key())
            last_success = state.last_success_at
            fixture.mdl["extra"] = "x" * (1024 * 1024)
            with self.assertRaises(ManagementError):
                async_to_sync(refresh_catalog)(
                    actor_id=actor.pk, revision=self.revision()
                )
            state.refresh_from_db()
            self.assertEqual(state.last_success_at, last_success)
            self.assertEqual(state.error_code, "invalid_metadata")
            self.assertEqual(Dataset.objects.filter(is_available=True).count(), 2)

    def test_generated_code_collision_rejects_entire_snapshot(self) -> None:
        with WrenFixture() as fixture, self.settings(WREN_MCP_URL=fixture.url):
            actor = self.make_account("catalog-root", superuser=True)
            snapshot = async_to_sync(discover_catalog)()
            legacy = self.make_dataset(snapshot.models[1].code, model_name="legacy")
            Dataset.objects.filter(pk=legacy.pk).update(source_key="legacy")
            revision = self.revision()
            with self.assertRaises(ManagementError) as caught:
                async_to_sync(refresh_catalog)(actor_id=actor.pk, revision=revision)
            self.assertEqual(caught.exception.code, "catalog_unavailable")
            self.assertEqual(Dataset.objects.count(), 1)
            self.assertEqual(self.revision(), revision)
            self.assertEqual(
                DatasetCatalogState.objects.get(pk=snapshot.source_key).error_code,
                "apply_conflict",
            )

    def test_audit_failure_rolls_back_catalog_and_revision(self) -> None:
        from unittest.mock import patch

        with WrenFixture() as fixture, self.settings(WREN_MCP_URL=fixture.url):
            actor = self.make_account("catalog-root", superuser=True)
            snapshot = async_to_sync(discover_catalog)()
            revision = self.revision()
            with (
                patch(
                    "datasets.services.record_audit",
                    side_effect=RuntimeError("audit failed"),
                ),
                self.assertRaises(RuntimeError),
            ):
                _apply_discovery_snapshot(
                    snapshot=snapshot, actor_id=actor.pk, revision=revision
                )
            self.assertEqual(self.revision(), revision)
            self.assertFalse(Dataset.objects.exists())
            self.assertFalse(
                DatasetCatalogState.objects.filter(pk=snapshot.source_key).exists()
            )

    def test_concurrent_snapshots_only_one_commits(self) -> None:
        from concurrent.futures import ThreadPoolExecutor
        from dataclasses import replace
        from threading import Barrier

        from django.db import close_old_connections

        with WrenFixture() as fixture, self.settings(WREN_MCP_URL=fixture.url):
            actor = self.make_account("catalog-root", superuser=True)
            snapshot = async_to_sync(discover_catalog)()
            alternative = replace(
                snapshot,
                models=(
                    replace(snapshot.models[0], description="Changed"),
                    *snapshot.models[1:],
                ),
                snapshot_sha256="f" * 64,
            )
            revision = self.revision()
            barrier = Barrier(2)

            def apply(index: int) -> str:
                close_old_connections()
                try:
                    barrier.wait(timeout=5)
                    _apply_discovery_snapshot(
                        snapshot=(snapshot, alternative)[index],
                        actor_id=actor.pk,
                        revision=revision,
                    )
                    return "committed"
                except ManagementError as error:
                    return error.code
                finally:
                    close_old_connections()

            with ThreadPoolExecutor(max_workers=2) as pool:
                outcomes = list(pool.map(apply, range(2)))
            self.assertCountEqual(outcomes, ["committed", "administration_conflict"])
            self.assertEqual(Dataset.objects.count(), 2)

    def test_description_and_addition_do_not_invalidate_grants(self) -> None:
        with WrenFixture() as fixture, self.settings(WREN_MCP_URL=fixture.url):
            actor = self.make_account("catalog-root", superuser=True)
            async_to_sync(refresh_catalog)(actor_id=actor.pk, revision=self.revision())
            orders = Dataset.objects.get(model_name="orders")
            grant = self.grant(self.make_department("test"), orders)
            epoch = self.epoch()
            revision = self.revision()
            async_to_sync(refresh_catalog)(actor_id=actor.pk, revision=revision)
            self.assertEqual(self.revision(), revision)
            self.assertEqual(self.epoch(), epoch)
            fixture.mdl["models"][0]["properties"]["description"] = (
                "Revised description"
            )
            model = deepcopy(fixture.mdl["models"][0])
            model["name"] = "payments"
            model["tableReference"]["table"] = "payments"
            fixture.mdl["models"].append(model)
            result = async_to_sync(refresh_catalog)(
                actor_id=actor.pk, revision=self.revision()
            )
            self.assertEqual(result["created"], 1)
            self.assertEqual(self.epoch(), epoch)
            grant.refresh_from_db()
            self.assertFalse(grant.needs_review)
            self.assertEqual(Dataset.objects.get(model_name="orders").pk, orders.pk)

    def test_refresh_keeps_local_disabled_state(self) -> None:
        with WrenFixture() as fixture, self.settings(WREN_MCP_URL=fixture.url):
            actor = self.make_account("catalog-root", superuser=True)
            async_to_sync(refresh_catalog)(actor_id=actor.pk, revision=self.revision())
            dataset = Dataset.objects.get(model_name="orders")
            update_dataset(
                actor_id=actor.pk,
                revision=self.revision(),
                dataset_id=dataset.pk,
                changes={"is_active": False},
            )
            async_to_sync(refresh_catalog)(actor_id=actor.pk, revision=self.revision())
            dataset.refresh_from_db()
            self.assertFalse(dataset.is_active)

    def test_structural_changes_flag_all_source_grants(self) -> None:
        with WrenFixture() as fixture, self.settings(WREN_MCP_URL=fixture.url):
            actor = self.make_account("catalog-root", superuser=True)
            async_to_sync(refresh_catalog)(actor_id=actor.pk, revision=self.revision())
            department = self.make_department("test")
            grants = [
                self.grant(department, dataset) for dataset in Dataset.objects.all()
            ]
            epoch = self.epoch()
            fixture.mdl["models"][0]["columns"][0]["type"] = "varchar"
            result = async_to_sync(refresh_catalog)(
                actor_id=actor.pk, revision=self.revision()
            )
            self.assertEqual(result["review_required"], 2)
            self.assertEqual(self.epoch(), epoch + 1)
            for grant in grants:
                grant.refresh_from_db()
                self.assertTrue(grant.needs_review)

    def test_endpoint_switch_never_reuses_previous_source(self) -> None:
        from django.test import Client

        with WrenFixture() as dev, WrenFixture() as staging:
            actor = self.make_account("catalog-root", superuser=True)
            client = Client()
            department = self.make_department("test")
            client.force_login(actor)
            with self.settings(WREN_MCP_URL=dev.url):
                async_to_sync(refresh_catalog)(
                    actor_id=actor.pk, revision=self.revision()
                )
                old = Dataset.objects.get(model_name="orders")
            staging.mdl["models"] = None
            with self.settings(WREN_MCP_URL=staging.url):
                self.assertEqual(client.get("/api/admin/datasets/").json()["items"], [])
                self.assertEqual(
                    client.get(f"/api/admin/datasets/{old.pk}/").status_code, 404
                )
                with self.assertRaises(ManagementError) as foreign:
                    replace_department_datasets(
                        actor_id=actor.pk,
                        revision=self.revision(),
                        department_id=department.pk,
                        dataset_ids=[old.pk],
                    )
                self.assertEqual(foreign.exception.code, "invalid_management_request")
                with self.assertRaises(ManagementError):
                    async_to_sync(refresh_catalog)(
                        actor_id=actor.pk, revision=self.revision()
                    )
                self.assertEqual(client.get("/api/admin/datasets/").json()["items"], [])
                status = client.get("/api/admin/datasets/catalog-status/").json()[
                    "result"
                ]
                self.assertIsNone(status["last_success_at"])
                model = deepcopy(MDL["models"][0])
                model["name"] = "payments"
                model["tableReference"]["table"] = "payments"
                staging.mdl["models"] = [model]
                async_to_sync(refresh_catalog)(
                    actor_id=actor.pk, revision=self.revision()
                )
                items = client.get("/api/admin/datasets/").json()["items"]
                self.assertEqual([item["model_name"] for item in items], ["payments"])

    def test_startup_failure_completes_and_records_safe_status(self) -> None:
        import asyncio

        from asgiref.testing import ApplicationCommunicator

        from config.lifespan import CatalogLifespan

        async def delegate(scope, receive, send):
            raise AssertionError("lifespan must not be delegated")

        async def exercise() -> None:
            communicator = ApplicationCommunicator(
                CatalogLifespan(delegate), {"type": "lifespan"}
            )
            await communicator.send_input({"type": "lifespan.startup"})
            self.assertEqual(
                await communicator.receive_output(timeout=25),
                {"type": "lifespan.startup.complete"},
            )
            await communicator.send_input({"type": "lifespan.shutdown"})
            self.assertEqual(
                await communicator.receive_output(),
                {"type": "lifespan.shutdown.complete"},
            )
            await communicator.wait()

        with (
            WrenFixture(project_resource=False) as fixture,
            self.settings(WREN_MCP_URL=fixture.url),
        ):
            asyncio.run(exercise())
            state = DatasetCatalogState.objects.get(pk=configured_source_key())
            self.assertEqual(state.error_code, "unsupported_metadata")
            self.assertIsNone(state.last_success_at)


class LifespanTests(SimpleTestCase):
    def test_startup_runs_once_and_http_is_delegated(self) -> None:
        import asyncio
        from unittest.mock import AsyncMock, patch

        from asgiref.testing import ApplicationCommunicator

        from config.lifespan import CatalogLifespan

        delegated = AsyncMock()
        application = CatalogLifespan(delegated)
        refresh = AsyncMock()

        async def exercise() -> None:
            communicator = ApplicationCommunicator(application, {"type": "lifespan"})
            for _ in range(2):
                await communicator.send_input({"type": "lifespan.startup"})
                self.assertEqual(
                    await communicator.receive_output(),
                    {"type": "lifespan.startup.complete"},
                )
            await communicator.send_input({"type": "lifespan.shutdown"})
            self.assertEqual(
                await communicator.receive_output(),
                {"type": "lifespan.shutdown.complete"},
            )
            await communicator.wait()
            await application({"type": "http"}, AsyncMock(), AsyncMock())

        with patch("datasets.services.refresh_catalog_on_startup", refresh):
            asyncio.run(exercise())
        refresh.assert_awaited_once()
        delegated.assert_awaited_once()
