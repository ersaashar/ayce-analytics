"""Dataset eligibility and explicit assignments.

Eligibility needs an active user, the native analytics capability, an active
membership in an active department and an explicit assignment pointing at that
same department's current grant. Nothing here is inherited.
"""

from __future__ import annotations

from django.contrib.auth.models import User
from django.test import Client

from accounts import services
from accounts.authorization import active_dataset_queryset
from accounts.models import Department, DepartmentMembership
from datasets.models import Dataset, DepartmentDataset, MembershipDataset
from datasets.services import (
    replace_department_datasets,
    replace_membership_datasets,
)
from tests.support import GovernanceTestCase

SUPERADMIN_GROUP = "AYCE Superadmin"
STAFF_GROUP = "AYCE Staff"


class DatasetEligibilityTests(GovernanceTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.root = self.make_account("root-operator", superuser=True)
        self.finance = self.make_department("finance", "Finance")
        self.sales = self.make_department("sales", "Sales")
        self.orders = self.make_dataset("orders", model_name="orders")
        self.customers = self.make_dataset("customers", model_name="customers")
        self.third = self.make_dataset("synthetic_third", model_name="third")
        self.person = self.make_account("person-one", groups=(STAFF_GROUP,))
        self.join(self.person, self.finance)
        self.join(self.person, self.sales)

    def client_for(self, user: User) -> Client:
        client = Client()
        client.force_login(user)
        return client

    def self_datasets(self, user: User, department_id: str | None = None) -> list[str]:
        query = "" if department_id is None else f"?department_id={department_id}"
        response = self.client_for(user).get(f"/api/me/datasets/{query}")
        self.assertEqual(response.status_code, 200, response.content)
        return [item["code"] for item in response.json()["items"]]

    def assign(self, department: Department, datasets: list[Dataset]) -> None:
        replace_membership_datasets(
            actor_id=self.root.pk,
            revision=self.revision(),
            department_id=department.pk,
            user_id=self.person.pk,
            dataset_ids=[dataset.pk for dataset in datasets],
        )

    def test_empty_subset_then_one_explicit_assignment(self) -> None:
        for dataset in (self.orders, self.customers, self.third):
            self.grant(self.finance, dataset)

        self.assertEqual(self.self_datasets(self.person), [])
        items = self.client_for(self.person).get("/api/me/departments/").json()["items"]
        finance = next(
            item for item in items if item["department"]["code"] == "finance"
        )
        self.assertEqual(finance["dataset_ids"], [])

        self.assign(self.finance, [self.orders])

        self.assertEqual(self.self_datasets(self.person), ["orders"])
        self.assertNotIn("customers", self.self_datasets(self.person))
        self.assertNotIn("synthetic_third", self.self_datasets(self.person))

        finance_only = self.self_datasets(self.person, str(self.finance.pk))
        self.assertEqual(finance_only, ["orders"])

        response = self.client_for(self.person).get("/api/me/datasets/")
        item = response.json()["items"][0]
        self.assertEqual(
            set(item),
            {
                "id",
                "code",
                "display_name",
                "description",
                "department_ids",
            },
        )
        self.assertEqual(item["department_ids"], [str(self.finance.pk)])

    def test_union_across_departments_keeps_selected_department_scoped(self) -> None:
        sales_dataset = self.make_dataset("sales_orders", model_name="sales_orders")
        self.grant(self.finance, self.orders)
        self.grant(self.sales, sales_dataset)
        self.assign(self.finance, [self.orders])
        self.assign(self.sales, [sales_dataset])

        self.assertEqual(self.self_datasets(self.person), ["orders", "sales_orders"])
        self.assertEqual(
            self.self_datasets(self.person, str(self.finance.pk)), ["orders"]
        )
        self.assertEqual(
            self.self_datasets(self.person, str(self.sales.pk)), ["sales_orders"]
        )

    def test_one_catalog_row_two_department_grants(self) -> None:
        shared = self.make_dataset("shared_orders", model_name="shared")
        self.grant(self.finance, shared)
        self.grant(self.sales, shared)
        self.assertEqual(Dataset.objects.filter(code="shared_orders").count(), 1)
        self.assertEqual(DepartmentDataset.objects.filter(dataset=shared).count(), 2)
        self.assertEqual(
            MembershipDataset.objects.filter(membership__user=self.person).count(), 0
        )

    def test_superadmins_have_no_dataset_bypass(self) -> None:
        self.grant(self.finance, self.orders)
        delegated = self.make_account("delegated-admin", groups=(SUPERADMIN_GROUP,))
        self.assertEqual(active_dataset_queryset(user_id=self.root.pk).count(), 0)
        self.assertEqual(active_dataset_queryset(user_id=delegated.pk).count(), 0)
        self.assertEqual(self.self_datasets(delegated), [])

    def test_missing_analytics_capability_yields_nothing(self) -> None:
        plain = User.objects.create_user("plain-user", password="manage-pass-7f3a")
        self.join(plain, self.finance)
        self.grant(self.finance, self.orders)
        membership = DepartmentMembership.objects.get(user=plain)
        MembershipDataset.objects.create(
            membership=membership,
            department_grant=DepartmentDataset.objects.get(
                department=self.finance, dataset=self.orders
            ),
        )
        self.assertFalse(plain.has_perm("chat.use_analytics"))
        self.assertEqual(active_dataset_queryset(user_id=plain.pk).count(), 0)
        self.assertEqual(self.self_datasets(plain), [])

    def test_inactive_states_remove_access(self) -> None:
        self.grant(self.finance, self.orders)
        self.assign(self.finance, [self.orders])
        self.assertEqual(self.self_datasets(self.person), ["orders"])

        membership = DepartmentMembership.objects.get(
            user=self.person, department=self.finance
        )
        membership.is_active = False
        membership.save(update_fields=["is_active"])
        self.assertEqual(self.self_datasets(self.person), [])

        membership.is_active = True
        membership.save(update_fields=["is_active"])
        self.assertEqual(self.self_datasets(self.person), ["orders"])

        self.finance.is_active = False
        self.finance.save(update_fields=["is_active"])
        self.assertEqual(self.self_datasets(self.person), [])
        self.finance.is_active = True
        self.finance.save(update_fields=["is_active"])

        self.orders.is_active = False
        self.orders.save(update_fields=["is_active"])
        self.assertEqual(self.self_datasets(self.person), [])
        self.orders.is_active = True
        self.orders.save(update_fields=["is_active"])

        self.person.is_active = False
        self.person.save(update_fields=["is_active"])
        self.assertEqual(active_dataset_queryset(user_id=self.person.pk).count(), 0)
        self.person.is_active = True
        self.person.save(update_fields=["is_active"])
        self.assertEqual(self.self_datasets(self.person), ["orders"])

    def test_grant_revocation_cascades_and_regrant_starts_empty(self) -> None:
        self.grant(self.finance, self.orders)
        self.assign(self.finance, [self.orders])
        self.assertEqual(MembershipDataset.objects.count(), 1)

        replace_department_datasets(
            actor_id=self.root.pk,
            revision=self.revision(),
            department_id=self.finance.pk,
            dataset_ids=[],
        )
        self.assertEqual(
            DepartmentDataset.objects.filter(department=self.finance).count(), 0
        )
        self.assertEqual(MembershipDataset.objects.count(), 0)
        self.assertEqual(self.self_datasets(self.person), [])

        replace_department_datasets(
            actor_id=self.root.pk,
            revision=self.revision(),
            department_id=self.finance.pk,
            dataset_ids=[self.orders.pk],
        )
        self.assertEqual(
            DepartmentDataset.objects.filter(department=self.finance).count(), 1
        )
        self.assertEqual(MembershipDataset.objects.count(), 0)
        self.assertEqual(self.self_datasets(self.person), [])

    def test_membership_deactivation_clears_and_reactivation_starts_empty(self) -> None:
        self.grant(self.finance, self.orders)
        self.assign(self.finance, [self.orders])
        membership = DepartmentMembership.objects.get(
            user=self.person, department=self.finance
        )
        services.replace_department_members(
            actor_id=self.root.pk,
            revision=self.revision(),
            department_id=self.finance.pk,
            members=[
                services.DepartmentMemberInput(
                    user_id=self.person.pk,
                    membership_role="staff",
                    is_active=False,
                    dataset_ids=(),
                )
            ],
        )
        membership.refresh_from_db()
        self.assertFalse(membership.is_active)
        # Deactivation deletes this membership's explicit assignments.
        self.assertEqual(
            MembershipDataset.objects.filter(membership=membership).count(), 0
        )
        self.assertEqual(self.self_datasets(self.person), [])

        services.replace_department_members(
            actor_id=self.root.pk,
            revision=self.revision(),
            department_id=self.finance.pk,
            members=[
                services.DepartmentMemberInput(
                    user_id=self.person.pk,
                    membership_role="staff",
                    is_active=True,
                    dataset_ids=(),
                )
            ],
        )
        membership.refresh_from_db()
        self.assertTrue(membership.is_active)
        self.assertEqual(self.self_datasets(self.person), [])
