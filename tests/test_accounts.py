"""Audited administration services: CAS, last-admin guard, passwords, audit."""

from __future__ import annotations

import importlib
import json
from io import StringIO
from unittest.mock import patch
from uuid import uuid4

from django.contrib.auth.models import Group, User
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import OperationalError
from django.test import Client

from accounts import services
from accounts.authorization import ManagementError, require_department_lead
from accounts.models import (
    AuthorizationAudit,
    AuthorizationState,
    Department,
    DepartmentMembership,
)
from datasets.models import DepartmentDataset, MembershipDataset
from datasets.services import (
    replace_department_datasets,
    replace_membership_datasets,
)
from tests.support import GovernanceTestCase

SUPERADMIN_GROUP = "AYCE Superadmin"
LEAD_GROUP = "AYCE Lead"
STAFF_GROUP = "AYCE Staff"
PASSWORD = "manage-pass-7f3a"


class RevisionConflictTests(GovernanceTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.admin = self.make_account("root-admin", superuser=True)

    def test_department_names_are_normalized_and_unique(self) -> None:
        created = services.create_department(
            actor_id=self.admin.pk,
            revision=self.revision(),
            name="  Data  Ops ",
            description="",
        )
        self.assertEqual(created["code"], "data-ops")
        revision = self.revision()
        audits = AuthorizationAudit.objects.count()
        for name in ("DATA OPS", "Data-Ops", "Data_Ops"):
            with self.assertRaises(ManagementError) as caught:
                services.create_department(
                    actor_id=self.admin.pk, revision=revision, name=name, description=""
                )
            self.assertEqual(caught.exception.code, "department_name_conflict")
        self.assertEqual(self.revision(), revision)
        self.assertEqual(AuthorizationAudit.objects.count(), audits)

    def test_department_unicode_long_names_and_rename(self) -> None:
        first = services.create_department(
            actor_id=self.admin.pk,
            revision=self.revision(),
            name="Café",
            description="",
        )
        with self.assertRaises(ManagementError) as caught:
            services.create_department(
                actor_id=self.admin.pk,
                revision=self.revision(),
                name="Cafe\u0301",
                description="",
            )
        self.assertEqual(caught.exception.code, "department_name_conflict")
        for suffix in ("a", "b"):
            result = services.create_department(
                actor_id=self.admin.pk,
                revision=self.revision(),
                name="x" * 70 + suffix,
                description="",
            )
            self.assertLessEqual(len(result["code"]), 64)
            if suffix == "a":
                first_code = result["code"]
            else:
                self.assertNotEqual(first_code, result["code"])
        renamed = services.update_department(
            actor_id=self.admin.pk,
            revision=self.revision(),
            department_id=first["id"],
            changes={"name": "Other"},
        )
        self.assertEqual(renamed["code"], first["code"])
        with self.assertRaises(ManagementError) as caught:
            services.create_department(
                actor_id=self.admin.pk,
                revision=self.revision(),
                name="!!!",
                description="",
            )
        self.assertEqual(caught.exception.code, "invalid_management_request")

    def test_name_migration_detects_collisions_before_backfill(self) -> None:
        from django.apps import apps
        from django.db import connection

        first = Department.objects.create(code="legacy-a", name="Data Ops")
        second = Department.objects.create(code="legacy-b", name="Different")
        Department.objects.filter(pk=second.pk).update(name="Data_Ops")
        migration = importlib.import_module(
            "accounts.migrations.0004_department_normalized_name"
        )
        with (
            connection.schema_editor() as editor,
            self.assertRaises(RuntimeError) as caught,
        ):
            migration.backfill_names(apps, editor)
        self.assertIn(str(first.pk), str(caught.exception))
        self.assertIn(str(second.pk), str(caught.exception))
        second.refresh_from_db()
        self.assertEqual(second.normalized_name, "different")
        self.assertEqual(second.code, "legacy-b")

    def test_rename_rejects_existing_name_and_preserves_grants(self) -> None:
        first = self.make_department("legacy", "Finance")
        self.make_department("ops", "Data Ops")
        dataset = self.make_dataset("orders")
        grant = self.grant(first, dataset)
        revision = self.revision()
        audits = AuthorizationAudit.objects.count()
        with self.assertRaises(ManagementError) as caught:
            services.update_department(
                actor_id=self.admin.pk,
                revision=revision,
                department_id=first.pk,
                changes={"name": "Data_Ops"},
            )
        self.assertEqual(caught.exception.code, "department_name_conflict")
        self.assertEqual(self.revision(), revision)
        self.assertEqual(AuthorizationAudit.objects.count(), audits)
        services.update_department(
            actor_id=self.admin.pk,
            revision=revision,
            department_id=first.pk,
            changes={"name": "Accounting"},
        )
        first.refresh_from_db()
        self.assertEqual(first.code, "legacy")
        self.assertTrue(
            DepartmentDataset.objects.filter(pk=grant.pk, department=first).exists()
        )

    def test_stale_revision_cannot_overwrite(self) -> None:
        first_read = self.revision()
        services.create_department(
            actor_id=self.admin.pk,
            revision=first_read,
            name="Finance",
            description="",
        )
        second_read = self.revision()
        self.assertGreater(second_read, first_read)

        with self.assertRaises(ManagementError) as caught:
            services.create_department(
                actor_id=self.admin.pk,
                revision=first_read,
                name="Sales",
                description="",
            )
        self.assertEqual(caught.exception.code, "administration_conflict")
        self.assertEqual(self.revision(), second_read)
        self.assertFalse(self.admin.departments.filter(code="sales").exists())

    def test_boolean_revision_is_rejected(self) -> None:
        with self.assertRaises(ManagementError) as caught:
            services.create_department(
                actor_id=self.admin.pk,
                revision=True,  # type: ignore[arg-type]
                name="Finance",
                description="",
            )
        self.assertEqual(caught.exception.code, "invalid_management_request")

    def test_failed_validation_rolls_back_revision_and_audit(self) -> None:
        before = self.revision()
        audits = AuthorizationAudit.objects.count()
        with self.assertRaises(ManagementError) as caught:
            services.create_user(
                actor_id=self.admin.pk,
                revision=before,
                username="weakling",
                first_name="",
                last_name="",
                email="",
                password="123",
            )
        self.assertEqual(caught.exception.code, "invalid_password")
        self.assertEqual(self.revision(), before)
        self.assertEqual(AuthorizationAudit.objects.count(), audits)
        self.assertFalse(User.objects.filter(username="weakling").exists())

    def test_audit_failure_rolls_back_everything(self) -> None:
        before = self.revision()
        with (
            patch.object(
                services,
                "record_audit",
                side_effect=RuntimeError("audit store unavailable"),
            ),
            self.assertRaises(RuntimeError),
        ):
            services.create_user(
                actor_id=self.admin.pk,
                revision=before,
                username="rollback-user",
                first_name="",
                last_name="",
                email="",
                password="rollback-pass-9x",
            )
        self.assertEqual(self.revision(), before)
        self.assertFalse(User.objects.filter(username="rollback-user").exists())

    def test_sqlite_lock_contention_maps_to_a_conflict(self) -> None:
        with (
            patch.object(
                AuthorizationState.objects,
                "filter",
                side_effect=OperationalError("database is locked"),
            ),
            self.assertRaises(ManagementError) as caught,
        ):
            services.create_department(
                actor_id=self.admin.pk,
                revision=self.revision(),
                name="Finance",
                description="",
            )
        self.assertEqual(caught.exception.code, "administration_conflict")

    def test_repeated_replacement_is_idempotent(self) -> None:
        department = self.make_department("finance")
        person = self.make_account("person-one", groups=(STAFF_GROUP,))
        rows = [
            services.MembershipInput(
                department_id=department.pk,
                membership_role="staff",
                is_active=True,
                dataset_ids=(),
            )
        ]
        services.replace_user_memberships(
            actor_id=self.admin.pk,
            revision=self.revision(),
            user_id=person.pk,
            memberships=rows,
        )
        services.replace_user_memberships(
            actor_id=self.admin.pk,
            revision=self.revision(),
            user_id=person.pk,
            memberships=rows,
        )
        self.assertEqual(DepartmentMembership.objects.filter(user=person).count(), 1)


class LastAdminTests(GovernanceTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.root = self.make_account("root-admin", superuser=True)

    def test_last_administrator_cannot_be_deactivated_or_demoted(self) -> None:
        with self.assertRaises(ManagementError) as caught:
            services.update_user(
                actor_id=self.root.pk,
                revision=self.revision(),
                user_id=self.root.pk,
                changes={"is_active": False},
            )
        self.assertEqual(caught.exception.code, "last_admin_protected")

        other = self.make_account("second-superuser", superuser=True)
        self.assertEqual(services.effective_admin_count(), 2)
        services.update_user(
            actor_id=other.pk,
            revision=self.revision(),
            user_id=self.root.pk,
            changes={"is_active": False},
        )
        root = User.objects.get(pk=self.root.pk)
        self.assertFalse(root.is_active)
        self.assertEqual(services.effective_admin_count(), 1)

        # Native superuser flags are not editable through the role API, so the
        # remaining admin cannot be demoted by clearing a group it never had.
        services.set_global_role(
            actor_id=other.pk,
            revision=self.revision(),
            user_id=other.pk,
            role="staff",
        )
        self.assertTrue(User.objects.get(pk=other.pk).is_superuser)
        self.assertEqual(services.effective_admin_count(), 1)

    def test_only_native_superuser_can_grant_superadmin(self) -> None:
        delegated = self.make_account("delegated-admin", groups=(SUPERADMIN_GROUP,))
        person = self.make_account("candidate", groups=(STAFF_GROUP,))
        with self.assertRaises(ManagementError) as caught:
            services.set_global_role(
                actor_id=delegated.pk,
                revision=self.revision(),
                user_id=person.pk,
                role="superadmin",
            )
        self.assertEqual(caught.exception.code, "permission_denied")
        self.assertFalse(person.groups.filter(name=SUPERADMIN_GROUP).exists())

        services.set_global_role(
            actor_id=self.root.pk,
            revision=self.revision(),
            user_id=person.pk,
            role="superadmin",
        )
        person.refresh_from_db()
        self.assertTrue(person.groups.filter(name=SUPERADMIN_GROUP).exists())
        self.assertFalse(person.groups.filter(name=STAFF_GROUP).exists())

    def test_delegated_superadmin_cannot_change_a_peer(self) -> None:
        delegated = self.make_account("delegated-admin", groups=(SUPERADMIN_GROUP,))
        peer = self.make_account("peer-admin", groups=(SUPERADMIN_GROUP,))
        with self.assertRaises(ManagementError) as caught:
            services.reset_password(
                actor_id=delegated.pk,
                revision=self.revision(),
                user_id=peer.pk,
                current_password=PASSWORD,
                new_password="another-pass-2b",
            )
        self.assertEqual(caught.exception.code, "permission_denied")


class PasswordAndSessionTests(GovernanceTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.root = self.make_account("root-admin", superuser=True)
        self.target = self.make_account("target-user", password="old-pass-1a")
        self.target_client = Client()
        self.assertTrue(
            self.target_client.login(username="target-user", password="old-pass-1a")
        )

    def test_wrong_actor_password_is_rejected(self) -> None:
        with self.assertRaises(ManagementError) as caught:
            services.reset_password(
                actor_id=self.root.pk,
                revision=self.revision(),
                user_id=self.target.pk,
                current_password="not-the-password",
                new_password="fresh-pass-2b",
            )
        self.assertEqual(caught.exception.code, "reauthentication_failed")

    def test_weak_new_password_is_rejected(self) -> None:
        with self.assertRaises(ManagementError) as caught:
            services.reset_password(
                actor_id=self.root.pk,
                revision=self.revision(),
                user_id=self.target.pk,
                current_password=PASSWORD,
                new_password="123",
            )
        self.assertEqual(caught.exception.code, "invalid_password")

    def test_lead_actor_cannot_reset_a_password(self) -> None:
        lead = self.make_account("finance-lead", groups=(LEAD_GROUP,))
        with self.assertRaises(ManagementError) as caught:
            services.reset_password(
                actor_id=lead.pk,
                revision=self.revision(),
                user_id=self.target.pk,
                current_password=PASSWORD,
                new_password="fresh-pass-2b",
            )
        self.assertEqual(caught.exception.code, "permission_denied")

    def test_valid_reset_invalidates_the_existing_session(self) -> None:
        services.reset_password(
            actor_id=self.root.pk,
            revision=self.revision(),
            user_id=self.target.pk,
            current_password=PASSWORD,
            new_password="fresh-pass-2b",
        )
        denied = self.target_client.get("/api/threads/")
        self.assertEqual(denied.status_code, 401)
        self.assertEqual(denied.json()["error"]["code"], "authentication_required")

        self.assertTrue(
            Client().login(username="target-user", password="fresh-pass-2b")
        )
        self.assertFalse(Client().login(username="target-user", password="old-pass-1a"))

    def test_password_values_and_hashes_never_reach_the_audit(self) -> None:
        services.reset_password(
            actor_id=self.root.pk,
            revision=self.revision(),
            user_id=self.target.pk,
            current_password=PASSWORD,
            new_password="fresh-pass-2b",
        )
        serialized = json.dumps(
            list(AuthorizationAudit.objects.values("action", "before", "after")),
            default=str,
        )
        self.assertNotIn("fresh-pass-2b", serialized)
        self.assertNotIn(PASSWORD, serialized)
        target = User.objects.get(pk=self.target.pk)
        self.assertNotIn(target.password, serialized)

    def test_deactivation_blocks_the_next_request(self) -> None:
        self.assertEqual(self.target_client.get("/api/threads/").status_code, 200)
        services.update_user(
            actor_id=self.root.pk,
            revision=self.revision(),
            user_id=self.target.pk,
            changes={"is_active": False},
        )
        denied = self.target_client.get("/api/threads/")
        self.assertEqual(denied.status_code, 401)


class AuditTrailTests(GovernanceTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.root = self.make_account("root-admin", superuser=True)

    def test_audit_is_append_only(self) -> None:
        record = services.record_audit(
            actor=self.root,
            action="user.updated",
            target_type="user",
            target_id="42",
            after={"first_name": "Ada"},
        )
        record.reason_code = "rewritten"
        with self.assertRaises(ValidationError):
            record.save()
        with self.assertRaises(ValidationError):
            AuthorizationAudit.objects.filter(pk=record.pk).update(allowed=False)
        with self.assertRaises(ValidationError):
            record.delete()
        self.assertEqual(
            AuthorizationAudit.objects.get(pk=record.pk).reason_code, "accepted"
        )

    def test_audit_snapshot_stays_bounded(self) -> None:
        record = services.record_audit(
            actor=self.root,
            action="department.datasets_changed",
            target_type="department",
            target_id="1",
            after={"dataset_ids": [str(uuid4()) for _ in range(5000)]},
        )
        encoded = json.dumps(record.after)
        self.assertLessEqual(len(encoded.encode("utf-8")), 8 * 1024)
        self.assertTrue(record.after.get("dataset_ids_omitted", 0) > 0)

    def test_denial_reason_codes_are_registered(self) -> None:
        services.record_denial(
            actor_id=self.root.pk,
            action="users.collection",
            reason_code="permission_denied",
        )
        record = AuthorizationAudit.objects.get(action="authorization.denied")
        self.assertFalse(record.allowed)
        self.assertEqual(record.reason_code, "permission_denied")
        self.assertEqual(record.target_type, "operation")


class SeedRolesCommandTests(GovernanceTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.root = self.make_account("root-admin", superuser=True)

    def test_command_requires_a_native_superuser_actor(self) -> None:
        with self.assertRaises(CommandError):
            call_command(
                "seed_ayce_roles", actor="missing-actor", revision=self.revision()
            )
        delegated = self.make_account("delegated-admin", groups=(SUPERADMIN_GROUP,))
        with self.assertRaises(CommandError):
            call_command(
                "seed_ayce_roles", actor=delegated.username, revision=self.revision()
            )

    def test_command_repairs_a_broken_fixed_definition(self) -> None:
        group = Group.objects.get(name=STAFF_GROUP)
        group.permissions.clear()
        out = StringIO()
        call_command(
            "seed_ayce_roles",
            actor=self.root.username,
            revision=self.revision(),
            stdout=out,
        )
        self.assertIn("Repaired", out.getvalue())
        self.assertTrue(
            Group.objects.get(name=STAFF_GROUP)
            .permissions.filter(codename="use_analytics")
            .exists()
        )
        self.assertTrue(
            AuthorizationAudit.objects.filter(action="roles.seeded").exists()
        )

        quiet = StringIO()
        call_command(
            "seed_ayce_roles",
            actor=self.root.username,
            revision=self.revision(),
            stdout=quiet,
        )
        self.assertIn("already match", quiet.getvalue())


class MembershipAuthorityTests(GovernanceTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.root = self.make_account("root-admin", superuser=True)
        self.finance = self.make_department("finance", "Finance")
        self.sales = self.make_department("sales", "Sales")
        self.orders = self.make_dataset("orders", model_name="orders")
        self.customers = self.make_dataset("customers", model_name="customers")
        self.grant(self.finance, self.orders)
        self.grant(self.finance, self.customers)
        self.sales_dataset = self.make_dataset(
            "sales_orders", model_name="sales_orders"
        )
        self.grant(self.sales, self.sales_dataset)

        self.lead = self.make_account("finance-lead", groups=(LEAD_GROUP, STAFF_GROUP))
        self.lead_membership = self.join(self.lead, self.finance, role="lead")
        self.staff = self.make_account("finance-staff", groups=(STAFF_GROUP,))
        self.staff_membership = self.join(self.staff, self.finance, role="staff")
        # The same person is only a Staff member of Sales.
        self.join(self.staff, self.sales, role="staff")
        self.sales_lead = self.make_account("sales-lead", groups=(LEAD_GROUP,))
        self.join(self.sales_lead, self.sales, role="lead")

    def test_lead_can_assign_only_granted_datasets(self) -> None:
        replace_membership_datasets(
            actor_id=self.lead.pk,
            revision=self.revision(),
            department_id=self.finance.pk,
            user_id=self.staff.pk,
            dataset_ids=[self.orders.pk],
        )
        assignment = MembershipDataset.objects.get(membership=self.staff_membership)
        self.assertEqual(assignment.department_grant.dataset, self.orders)

        with self.assertRaises(ManagementError) as caught:
            replace_membership_datasets(
                actor_id=self.lead.pk,
                revision=self.revision(),
                department_id=self.finance.pk,
                user_id=self.staff.pk,
                dataset_ids=[self.sales_dataset.pk],
            )
        self.assertEqual(caught.exception.code, "invalid_management_request")

    def test_lead_cannot_appoint_or_modify_a_lead(self) -> None:
        with self.assertRaises(ManagementError) as caught:
            services.replace_department_members(
                actor_id=self.lead.pk,
                revision=self.revision(),
                department_id=self.finance.pk,
                members=[
                    services.DepartmentMemberInput(
                        user_id=self.staff.pk,
                        membership_role="lead",
                        is_active=True,
                        dataset_ids=(),
                    )
                ],
            )
        self.assertEqual(caught.exception.code, "permission_denied")

        with self.assertRaises(ManagementError) as caught:
            replace_membership_datasets(
                actor_id=self.lead.pk,
                revision=self.revision(),
                department_id=self.finance.pk,
                user_id=self.lead.pk,
                dataset_ids=[self.orders.pk],
            )
        self.assertEqual(caught.exception.code, "permission_denied")

    def test_lead_roster_replacement_preserves_lead_rows(self) -> None:
        result = services.replace_department_members(
            actor_id=self.lead.pk,
            revision=self.revision(),
            department_id=self.finance.pk,
            members=[
                services.DepartmentMemberInput(
                    user_id=self.staff.pk,
                    membership_role="staff",
                    is_active=True,
                    dataset_ids=(self.orders.pk,),
                )
            ],
        )
        rows = {row["user"]["id"]: row for row in result["members"]}
        self.assertIn(self.lead.pk, rows)
        self.assertEqual(rows[self.lead.pk]["membership_role"], "lead")
        self.assertEqual(rows[self.staff.pk]["dataset_ids"], [str(self.orders.pk)])

    def test_lead_is_scoped_to_led_departments(self) -> None:
        with self.assertRaises(ManagementError) as caught:
            services.replace_department_members(
                actor_id=self.lead.pk,
                revision=self.revision(),
                department_id=self.sales.pk,
                members=[],
            )
        self.assertEqual(caught.exception.code, "resource_not_found")

        with self.assertRaises(ManagementError) as caught:
            replace_membership_datasets(
                actor_id=self.lead.pk,
                revision=self.revision(),
                department_id=self.sales.pk,
                user_id=self.staff.pk,
                dataset_ids=[],
            )
        self.assertEqual(caught.exception.code, "resource_not_found")

    def test_lead_cannot_grant_department_datasets(self) -> None:
        with self.assertRaises(ManagementError) as caught:
            services.replace_user_memberships(
                actor_id=self.lead.pk,
                revision=self.revision(),
                user_id=self.staff.pk,
                memberships=[],
            )
        self.assertEqual(caught.exception.code, "permission_denied")

    def test_lead_group_is_a_projection_of_lead_memberships(self) -> None:
        self.assertTrue(self.lead.groups.filter(name=LEAD_GROUP).exists())
        services.replace_department_members(
            actor_id=self.root.pk,
            revision=self.revision(),
            department_id=self.finance.pk,
            members=[
                services.DepartmentMemberInput(
                    user_id=self.lead.pk,
                    membership_role="staff",
                    is_active=True,
                    dataset_ids=(),
                )
            ],
        )
        lead = User.objects.get(pk=self.lead.pk)
        self.assertFalse(lead.groups.filter(name=LEAD_GROUP).exists())

    def test_duplicate_grant_replacement_keeps_unique_rows(self) -> None:
        replace_department_datasets(
            actor_id=self.root.pk,
            revision=self.revision(),
            department_id=self.finance.pk,
            dataset_ids=[self.orders.pk, self.customers.pk],
        )
        replace_department_datasets(
            actor_id=self.root.pk,
            revision=self.revision(),
            department_id=self.finance.pk,
            dataset_ids=[self.orders.pk, self.customers.pk],
        )
        self.assertEqual(
            DepartmentDataset.objects.filter(department=self.finance).count(), 2
        )
        self.assertEqual(Group.objects.filter(name__startswith="AYCE").count(), 3)


class DepartmentLeadAuthorizationTests(GovernanceTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.finance = self.make_department("finance", "Finance")
        self.sales = self.make_department("sales", "Sales")
        self.orders = self.make_dataset("orders", model_name="orders")
        self.grant(self.finance, self.orders)
        self.grant(self.sales, self.orders)
        self.lead = self.make_account("finance-lead", groups=(LEAD_GROUP,))
        self.lead_membership = self.join(self.lead, self.finance, role="lead")
        self.staff = self.make_account("finance-staff", groups=(STAFF_GROUP,))
        self.staff_membership = self.join(self.staff, self.finance, role="staff")
        self.join(self.staff, self.sales, role="staff")

    def test_returns_the_exact_active_lead_membership(self) -> None:
        membership = require_department_lead(
            actor=self.lead, department_id=self.finance.pk
        )
        self.assertIsInstance(membership, DepartmentMembership)
        self.assertEqual(membership.pk, self.lead_membership.pk)
        self.assertEqual(membership.department_id, self.finance.pk)
        self.assertEqual(membership.user_id, self.lead.pk)
        self.assertEqual(membership.membership_role, "lead")

    def test_missing_capability_is_denied(self) -> None:
        self.assertFalse(self.staff.has_perm("accounts.manage_department_staff"))
        with self.assertRaises(ManagementError) as caught:
            require_department_lead(actor=self.staff, department_id=self.finance.pk)
        self.assertEqual(caught.exception.code, "permission_denied")

    def test_ineligible_memberships_are_hidden(self) -> None:
        capable_without_row = self.make_account("capable", groups=(LEAD_GROUP,))
        with self.assertRaises(ManagementError) as caught:
            require_department_lead(
                actor=capable_without_row, department_id=self.finance.pk
            )
        self.assertEqual(caught.exception.code, "resource_not_found")

        capable_staff = self.make_account("capable-staff", groups=(LEAD_GROUP,))
        self.join(capable_staff, self.finance, role="staff")
        with self.assertRaises(ManagementError) as caught:
            require_department_lead(actor=capable_staff, department_id=self.finance.pk)
        self.assertEqual(caught.exception.code, "resource_not_found")

        with self.assertRaises(ManagementError) as caught:
            require_department_lead(actor=self.lead, department_id=self.sales.pk)
        self.assertEqual(caught.exception.code, "resource_not_found")

        DepartmentMembership.objects.filter(pk=self.lead_membership.pk).update(
            is_active=False
        )
        with self.assertRaises(ManagementError) as caught:
            require_department_lead(actor=self.lead, department_id=self.finance.pk)
        self.assertEqual(caught.exception.code, "resource_not_found")

        DepartmentMembership.objects.filter(pk=self.lead_membership.pk).update(
            is_active=True
        )
        Department.objects.filter(pk=self.finance.pk).update(is_active=False)
        with self.assertRaises(ManagementError) as caught:
            require_department_lead(actor=self.lead, department_id=self.finance.pk)
        self.assertEqual(caught.exception.code, "resource_not_found")

    def test_persisted_inactive_user_is_hidden(self) -> None:
        User.objects.filter(pk=self.lead.pk).update(is_active=False)
        with self.assertRaises(ManagementError) as caught:
            require_department_lead(actor=self.lead, department_id=self.finance.pk)
        self.assertEqual(caught.exception.code, "resource_not_found")

    def _snapshot(self, department: Department) -> tuple:
        return (
            self.revision(),
            self.epoch(),
            sorted(
                DepartmentMembership.objects.filter(department=department).values_list(
                    "user_id", "membership_role", "is_active"
                )
            ),
            sorted(
                DepartmentDataset.objects.filter(department=department).values_list(
                    "dataset_id", flat=True
                )
            ),
            AuthorizationAudit.objects.count(),
        )

    def test_inactive_department_mutations_leave_state_unchanged(self) -> None:
        before = self._snapshot(self.finance)
        Department.objects.filter(pk=self.finance.pk).update(is_active=False)

        with self.assertRaises(ManagementError) as caught:
            services.replace_department_members(
                actor_id=self.lead.pk,
                revision=before[0],
                department_id=self.finance.pk,
                members=[
                    services.DepartmentMemberInput(
                        user_id=self.staff.pk,
                        membership_role="staff",
                        is_active=False,
                        dataset_ids=(),
                    )
                ],
            )
        self.assertEqual(caught.exception.code, "resource_not_found")

        with self.assertRaises(ManagementError) as caught:
            replace_membership_datasets(
                actor_id=self.lead.pk,
                revision=before[0],
                department_id=self.finance.pk,
                user_id=self.staff.pk,
                dataset_ids=[self.orders.pk],
            )
        self.assertEqual(caught.exception.code, "resource_not_found")

        self.assertEqual(self._snapshot(self.finance), before)

    def test_foreign_department_mutations_leave_state_unchanged(self) -> None:
        before = self._snapshot(self.sales)

        with self.assertRaises(ManagementError) as caught:
            services.replace_department_members(
                actor_id=self.lead.pk,
                revision=before[0],
                department_id=self.sales.pk,
                members=[
                    services.DepartmentMemberInput(
                        user_id=self.staff.pk,
                        membership_role="staff",
                        is_active=False,
                        dataset_ids=(),
                    )
                ],
            )
        self.assertEqual(caught.exception.code, "resource_not_found")

        with self.assertRaises(ManagementError) as caught:
            replace_membership_datasets(
                actor_id=self.lead.pk,
                revision=before[0],
                department_id=self.sales.pk,
                user_id=self.staff.pk,
                dataset_ids=[self.orders.pk],
            )
        self.assertEqual(caught.exception.code, "resource_not_found")

        self.assertEqual(self._snapshot(self.sales), before)
