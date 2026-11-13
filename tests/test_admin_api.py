"""Strict management/self-service HTTP contracts and native admin parity."""

from __future__ import annotations

import json
import re

from asgiref.sync import async_to_sync
from django.contrib.auth.models import Permission, User
from django.test import Client

from accounts.models import AuthorizationAudit, DepartmentMembership
from datasets.models import Dataset, MembershipDataset
from datasets.services import refresh_catalog
from tests.support import GovernanceTestCase
from tests.wren_fixture import WrenFixture

SUPERADMIN_GROUP = "AYCE Superadmin"
LEAD_GROUP = "AYCE Lead"
STAFF_GROUP = "AYCE Staff"
PASSWORD = "manage-pass-7f3a"
CSRF_PATTERN = re.compile(r'name="csrfmiddlewaretoken" value="([^"]+)"')


class AdminApiTestCase(GovernanceTestCase):
    def client_for(self, user: User, *, csrf: bool = False) -> Client:
        client = Client(enforce_csrf_checks=csrf)
        client.force_login(user)
        return client

    def token(self, client: Client) -> str:
        response = client.get("/management/")
        self.assertEqual(response.status_code, 200)
        match = CSRF_PATTERN.search(response.content.decode())
        self.assertIsNotNone(match)
        return match.group(1)

    def send(
        self,
        client: Client,
        method: str,
        path: str,
        body: dict | None = None,
    ) -> object:
        token = self.token(client)
        payload = json.dumps(body if body is not None else {})
        return getattr(client, method)(
            path,
            data=payload,
            content_type="application/json",
            headers={"X-CSRFToken": token},
        )

    def post(self, client: Client, path: str, body: dict | None = None):
        return self.send(client, "post", path, body)

    def patch(self, client: Client, path: str, body: dict | None = None):
        return self.send(client, "patch", path, body)

    def put(self, client: Client, path: str, body: dict | None = None):
        return self.send(client, "put", path, body)

    def delete(self, client: Client, path: str, body: dict | None = None):
        return self.send(client, "delete", path, body)

    def create_admin_user(
        self,
        client: Client,
        *,
        username: str,
        password: str = PASSWORD,
    ) -> dict:
        response = self.post(
            client,
            "/api/admin/users/",
            {
                "revision": self.revision(),
                "username": username,
                "first_name": "Ada",
                "last_name": "Lovelace",
                "email": f"{username}@example.test",
                "password": password,
            },
        )
        self.assertEqual(response.status_code, 201, response.content)
        return response.json()


class DepartmentCreationTests(AdminApiTestCase):
    def test_generated_code_and_duplicate_name_conflicts(self) -> None:
        admin = self.make_account("department-admin", superuser=True)
        client = self.client_for(admin)
        response = self.post(
            client,
            "/api/admin/departments/",
            {"revision": self.revision(), "name": "  Test  ", "description": ""},
        )
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(response.json()["department"]["code"], "test")
        revision = self.revision()
        audits = AuthorizationAudit.objects.count()
        for name in ("TEST", "test!", "test_"):
            response = self.post(
                client,
                "/api/admin/departments/",
                {"revision": revision, "name": name, "description": ""},
            )
            self.assertEqual(response.status_code, 409)
            self.assertEqual(
                response.json()["error"]["code"], "department_name_conflict"
            )
        self.assertEqual(self.revision(), revision)
        self.assertEqual(AuthorizationAudit.objects.count(), audits)
        response = self.post(
            client,
            "/api/admin/departments/",
            {"revision": revision, "name": "New", "code": "new", "description": ""},
        )
        self.assertEqual(response.status_code, 400)


class CatalogRefreshAuthorizationTests(AdminApiTestCase):
    def test_denied_and_invalid_requests_never_probe_wren(self) -> None:
        from unittest.mock import AsyncMock, patch

        root = self.make_account("catalog-root", superuser=True)
        staff = self.make_account("catalog-staff", groups=(STAFF_GROUP,))
        lead = self.make_account("catalog-lead", groups=(LEAD_GROUP, STAFF_GROUP))
        with patch(
            "datasets.services.discover_catalog", new_callable=AsyncMock
        ) as probe:
            anonymous = Client()
            self.assertEqual(
                anonymous.post(
                    "/api/admin/datasets/refresh/",
                    data=json.dumps({"revision": self.revision()}),
                    content_type="application/json",
                ).status_code,
                401,
            )
            for user in (staff, lead):
                client = self.client_for(user)
                response = client.post(
                    "/api/admin/datasets/refresh/",
                    data=json.dumps({"revision": self.revision()}),
                    content_type="application/json",
                )
                self.assertEqual(response.status_code, 403)
                self.assertEqual(
                    client.get("/api/admin/datasets/catalog-status/").status_code, 403
                )
            client = self.client_for(root, csrf=True)
            response = client.post(
                "/api/admin/datasets/refresh/",
                data=json.dumps({"revision": self.revision()}),
                content_type="application/json",
            )
            self.assertEqual(response.status_code, 403)
            response = self.post(
                client, "/api/admin/datasets/refresh/", {"revision": True}
            )
            self.assertEqual(response.status_code, 400)
            probe.assert_not_awaited()


class SelfServiceTests(AdminApiTestCase):
    def test_anonymous_management_reads_are_rejected(self) -> None:
        client = Client()
        for path in ("/api/me/", "/api/me/departments/", "/api/me/datasets/"):
            with self.subTest(path=path):
                response = client.get(path)
                self.assertEqual(response.status_code, 401)
                self.assertEqual(
                    response.json()["error"]["code"], "authentication_required"
                )
                self.assertEqual(response["Cache-Control"], "no-store")

    def test_me_reports_roles_and_capabilities(self) -> None:
        staff = self.make_account("plain-staff", groups=(STAFF_GROUP,))
        lead = self.make_account("dept-lead", groups=(LEAD_GROUP, STAFF_GROUP))
        department = self.make_department("finance")
        self.join(lead, department, role="lead")

        response = self.client_for(staff).get("/api/me/")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["roles"], [STAFF_GROUP])
        self.assertFalse(payload["capabilities"]["manage_users"])
        self.assertFalse(payload["analytics_enabled"])
        self.assertNotIn("analytics_department_ids", payload)
        self.assertNotIn("password", json.dumps(payload))

        lead_payload = self.client_for(lead).get("/api/me/").json()
        self.assertEqual(lead_payload["roles"], [LEAD_GROUP, STAFF_GROUP])
        self.assertTrue(lead_payload["capabilities"]["manage_department_staff"])
        self.assertFalse(lead_payload["capabilities"]["manage_users"])

    def test_me_eligibility_follows_assignments_without_wren(self) -> None:
        with (
            WrenFixture() as fixture,
            self.settings(
                DEBUG=True,
                AYCE_ANALYTICS_MODE="dev_jaffle",
                AYCE_DEV_JAFFLE_CONFIRMED=True,
                AYCE_DEV_WREN_PROJECT="jaffle-wren",
                WREN_MCP_URL=fixture.url,
                AYCE_DEV_WREN_MCP_URL=fixture.url,
            ),
        ):
            actor = self.make_account("catalog-root", superuser=True)
            async_to_sync(refresh_catalog)(actor_id=actor.pk, revision=self.revision())
            sales = self.make_department("sales", "Sales")
            marketing = self.make_department("marketing", "Marketing")
            customers = Dataset.objects.get(model_name="customers")
            orders = Dataset.objects.get(model_name="orders")
            for department in (sales, marketing):
                self.grant(department, customers)
                self.grant(department, orders)
            mixed = self.make_account("mixed-staff", groups=(STAFF_GROUP,))
            self.join(mixed, sales, datasets=(customers, orders))
            self.join(mixed, marketing, datasets=(orders,))
            partial = self.make_account("partial-staff", groups=(STAFF_GROUP,))
            self.join(partial, marketing, datasets=(orders,))
            bare = self.make_account("bare-staff", groups=(STAFF_GROUP,))
            self.join(bare, marketing)
            root = self.make_account("open-root", superuser=True)
            self.join(root, sales)
            calls = list(fixture.calls)
            self.assertTrue(calls)

            def enabled(user: User) -> bool:
                body = self.client_for(user).get("/api/me/").json()
                self.assertNotIn("analytics_department_ids", body)
                return body["analytics_enabled"]

            self.assertFalse(enabled(bare))
            self.assertTrue(enabled(mixed))
            self.assertFalse(enabled(partial))
            self.assertFalse(enabled(root))
            self.assertFalse(enabled(bare))
            self.assertEqual(fixture.calls, calls)

    def test_self_service_departments_show_empty_subsets(self) -> None:
        person = self.make_account("member-one", groups=(STAFF_GROUP,))
        department = self.make_department("finance")
        self.join(person, department, role="staff")
        response = self.client_for(person).get("/api/me/departments/")
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(
            set(payload),
            {
                "items",
                "page",
                "page_size",
                "total",
                "revision",
                "epoch",
            },
        )
        self.assertEqual(len(payload["items"]), 1)
        item = payload["items"][0]
        self.assertEqual(item["department"]["code"], "finance")
        self.assertEqual(item["dataset_ids"], [])

    def test_unknown_query_keys_are_rejected(self) -> None:
        person = self.make_account("member-two", groups=(STAFF_GROUP,))
        client = self.client_for(person)
        response = client.get("/api/me/departments/?page=1&limit=5")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_management_request")
        oversized = client.get("/api/me/departments/?page_size=101")
        self.assertEqual(oversized.status_code, 400)


class SuperadminApiTests(AdminApiTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.root = self.make_account("root-admin", superuser=True)
        self.client = self.client_for(self.root, csrf=True)

    def test_csrf_is_required_for_mutations(self) -> None:
        client = self.client_for(self.root, csrf=True)
        response = client.post(
            "/api/admin/users/",
            data=json.dumps({}),
            content_type="application/json",
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "csrf_failed")

    def test_create_list_and_update_account(self) -> None:
        created = self.create_admin_user(self.client, username="new-person")
        self.assertEqual(created["user"]["roles"], [STAFF_GROUP])
        self.assertEqual(created["user"]["memberships"], [])
        self.assertTrue(
            AuthorizationAudit.objects.filter(action="user.created").exists()
        )

        listed = self.client.get("/api/admin/users/?q=new-person")
        self.assertEqual(listed.status_code, 200)
        items = listed.json()["items"]
        self.assertEqual([item["username"] for item in items], ["new-person"])

        user_id = created["user"]["id"]
        updated = self.patch(
            self.client,
            f"/api/admin/users/{user_id}/",
            {"revision": self.revision(), "first_name": "Grace"},
        )
        self.assertEqual(updated.status_code, 200)
        self.assertEqual(updated.json()["user"]["first_name"], "Grace")

        stale = self.patch(
            self.client,
            f"/api/admin/users/{user_id}/",
            {"revision": created["revision"], "last_name": "Hopper"},
        )
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(stale.json()["error"]["code"], "administration_conflict")

    def test_unknown_and_extra_fields_are_rejected(self) -> None:
        response = self.post(
            self.client,
            "/api/admin/users/",
            {
                "revision": self.revision(),
                "username": "extra-field",
                "first_name": "",
                "last_name": "",
                "email": "",
                "password": PASSWORD,
                "is_superuser": True,
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_management_request")
        self.assertFalse(User.objects.filter(username="extra-field").exists())

        empty = self.patch(
            self.client,
            f"/api/admin/users/{self.root.pk}/",
            {"revision": self.revision()},
        )
        self.assertEqual(empty.status_code, 400)

    def test_reset_password_requires_the_actor_password(self) -> None:
        created = self.create_admin_user(
            self.client, username="reset-target", password="target-pass-1a"
        )
        wrong = self.post(
            self.client,
            f"/api/admin/users/{created['user']['id']}/reset-password/",
            {
                "revision": self.revision(),
                "current_password": "nope",
                "new_password": "replacement-pass-3c",
            },
        )
        self.assertEqual(wrong.status_code, 403)
        self.assertEqual(wrong.json()["error"]["code"], "reauthentication_failed")

        ok = self.post(
            self.client,
            f"/api/admin/users/{created['user']['id']}/reset-password/",
            {
                "revision": self.revision(),
                "current_password": PASSWORD,
                "new_password": "replacement-pass-3c",
            },
        )
        self.assertEqual(ok.status_code, 200)
        self.assertNotIn("password", ok.json()["user"])

    def test_last_admin_guard_reaches_the_api(self) -> None:
        response = self.patch(
            self.client,
            f"/api/admin/users/{self.root.pk}/",
            {"revision": self.revision(), "is_active": False},
        )
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "last_admin_protected")
        root = User.objects.get(pk=self.root.pk)
        self.assertTrue(root.is_active)

    def test_memberships_are_replaced_completely(self) -> None:
        person = self.make_account("replacee", groups=(STAFF_GROUP,))
        department = self.make_department("finance")
        dataset = self.make_dataset("orders", model_name="orders")
        self.grant(department, dataset)

        first = self.put(
            self.client,
            f"/api/admin/users/{person.pk}/departments/",
            {
                "revision": self.revision(),
                "memberships": [
                    {
                        "department_id": str(department.pk),
                        "membership_role": "staff",
                        "is_active": True,
                        "dataset_ids": [str(dataset.pk)],
                    }
                ],
            },
        )
        self.assertEqual(first.status_code, 200, first.content)
        membership = DepartmentMembership.objects.get(user=person)
        self.assertEqual(
            MembershipDataset.objects.filter(membership=membership).count(), 1
        )

        second = self.put(
            self.client,
            f"/api/admin/users/{person.pk}/departments/",
            {"revision": self.revision(), "memberships": []},
        )
        self.assertEqual(second.status_code, 200)
        membership.refresh_from_db()
        self.assertFalse(membership.is_active)
        self.assertEqual(MembershipDataset.objects.count(), 0)

    def test_forged_dataset_id_is_rejected(self) -> None:
        person = self.make_account("forger", groups=(STAFF_GROUP,))
        department = self.make_department("finance")
        other = self.make_department("sales")
        dataset = self.make_dataset("sales_only", model_name="sales_only")
        self.grant(other, dataset)
        response = self.put(
            self.client,
            f"/api/admin/users/{person.pk}/departments/",
            {
                "revision": self.revision(),
                "memberships": [
                    {
                        "department_id": str(department.pk),
                        "membership_role": "staff",
                        "is_active": True,
                        "dataset_ids": [str(dataset.pk)],
                    }
                ],
            },
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"]["code"], "invalid_management_request")

    def test_roles_endpoint_lists_fixed_definitions(self) -> None:
        response = self.client.get("/api/admin/roles/")
        self.assertEqual(response.status_code, 200)
        roles = response.json()["roles"]
        names = [role["name"] for role in roles]
        self.assertEqual(names, [SUPERADMIN_GROUP, LEAD_GROUP, STAFF_GROUP])
        staff = next(role for role in roles if role["name"] == STAFF_GROUP)
        self.assertEqual(staff["capabilities"], [])
        self.assertTrue(staff["uses_analytics"])

    def test_audit_endpoint_is_permission_gated(self) -> None:
        self.create_admin_user(self.client, username="audited")
        response = self.client.get("/api/admin/audit/")
        self.assertEqual(response.status_code, 200)
        actions = [item["action"] for item in response.json()["items"]]
        self.assertIn("user.created", actions)
        rendered = json.dumps(response.json())
        self.assertNotIn(PASSWORD, rendered)

    def test_non_superadmin_staff_gets_403(self) -> None:
        staff = self.make_account("plain-staff", groups=(STAFF_GROUP,))
        client = self.client_for(staff, csrf=True)
        response = client.get("/api/admin/users/")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "permission_denied")
        self.assertTrue(
            AuthorizationAudit.objects.filter(
                action="authorization.denied", allowed=False
            ).exists()
        )

    def test_management_page_is_denied_for_plain_staff(self) -> None:
        staff = self.make_account("plain-staff", groups=(STAFF_GROUP,))
        response = self.client_for(staff).get("/management/")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response["Cache-Control"], "no-store")
        anonymous = Client().get("/management/")
        self.assertEqual(anonymous.status_code, 302)
        self.assertIn("/accounts/login/", anonymous["Location"])
        self.assertEqual(Client().get("/management/?section=nope").status_code, 302)


class LeadScopeTests(AdminApiTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.root = self.make_account("root-admin", superuser=True)
        self.finance = self.make_department("finance", "Finance")
        self.sales = self.make_department("sales", "Sales")
        self.orders = self.make_dataset("orders", model_name="orders")
        self.grant(self.finance, self.orders)
        self.lead = self.make_account("finance-lead", groups=(LEAD_GROUP, STAFF_GROUP))
        self.join(self.lead, self.finance, role="lead")
        self.finance_staff = self.make_account("finance-staff", groups=(STAFF_GROUP,))
        self.join(self.finance_staff, self.finance, role="staff")
        self.sales_person = self.make_account("sales-person", groups=(STAFF_GROUP,))
        self.join(self.sales_person, self.sales, role="staff")
        self.client = self.client_for(self.lead, csrf=True)

    def test_management_page_sections_respect_lead_scope(self) -> None:
        page_client = self.client_for(self.lead)
        self.assertEqual(
            page_client.get("/management/?section=departments").status_code, 200
        )
        self.assertEqual(
            page_client.get("/management/?section=datasets").status_code, 200
        )
        self.assertEqual(page_client.get("/management/?section=users").status_code, 403)
        self.assertEqual(page_client.get("/management/?section=roles").status_code, 403)
        self.assertEqual(page_client.get("/management/?section=audit").status_code, 403)
        self.assertEqual(
            page_client.get("/management/?section=unknown").status_code, 404
        )

    def test_department_list_is_limited_to_led_departments(self) -> None:
        response = self.client.get("/api/admin/departments/")
        self.assertEqual(response.status_code, 200)
        codes = [item["code"] for item in response.json()["items"]]
        self.assertEqual(codes, ["finance"])

    def test_foreign_department_is_indistinguishable_from_missing(self) -> None:
        foreign = self.client.get(f"/api/admin/departments/{self.sales.pk}/")
        missing = self.client.get(
            "/api/admin/departments/00000000-0000-4000-8000-000000000000/"
        )
        self.assertEqual(foreign.status_code, 404)
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(foreign.json(), missing.json())
        self.assertEqual(foreign.json()["error"]["code"], "resource_not_found")

    def test_global_endpoints_are_forbidden(self) -> None:
        for path in ("/api/admin/users/", "/api/admin/roles/", "/api/admin/audit/"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 403)

    def test_lead_edits_staff_subsets_only(self) -> None:
        response = self.put(
            self.client,
            f"/api/admin/departments/{self.finance.pk}/members/",
            {
                "revision": self.revision(),
                "members": [
                    {
                        "user_id": self.finance_staff.pk,
                        "membership_role": "staff",
                        "is_active": True,
                        "dataset_ids": [str(self.orders.pk)],
                    }
                ],
            },
        )
        self.assertEqual(response.status_code, 200, response.content)
        rows = response.json()["members"]
        self.assertEqual(len({row["user"]["id"] for row in rows}), len(rows))
        self.assertIn(self.lead.pk, {row["user"]["id"] for row in rows})

        promotion = self.put(
            self.client,
            f"/api/admin/departments/{self.finance.pk}/members/",
            {
                "revision": self.revision(),
                "members": [
                    {
                        "user_id": self.finance_staff.pk,
                        "membership_role": "lead",
                        "is_active": True,
                        "dataset_ids": [],
                    }
                ],
            },
        )
        self.assertEqual(promotion.status_code, 403)
        self.assertEqual(promotion.json()["error"]["code"], "permission_denied")

    def test_superadmin_grant_put_returns_a_full_department(self) -> None:
        root_client = self.client_for(self.root, csrf=True)
        response = self.put(
            root_client,
            f"/api/admin/departments/{self.finance.pk}/datasets/",
            {"revision": self.revision(), "dataset_ids": [str(self.orders.pk)]},
        )
        self.assertEqual(response.status_code, 200, response.content)
        result = response.json()["result"]
        self.assertEqual(
            set(result["department"]),
            {
                "id",
                "code",
                "name",
                "description",
                "is_active",
                "unavailable_dataset_count",
                "created_at",
                "updated_at",
            },
        )
        self.assertEqual(result["dataset_ids"], [str(self.orders.pk)])

    def test_lead_cannot_change_department_or_grants(self) -> None:
        patch = self.patch(
            self.client,
            f"/api/admin/departments/{self.finance.pk}/",
            {"revision": self.revision(), "name": "Renamed"},
        )
        self.assertEqual(patch.status_code, 403)

        grants = self.put(
            self.client,
            f"/api/admin/departments/{self.finance.pk}/datasets/",
            {"revision": self.revision(), "dataset_ids": [str(self.orders.pk)]},
        )
        self.assertEqual(grants.status_code, 403)

        read = self.client.get(f"/api/admin/departments/{self.finance.pk}/datasets/")
        self.assertEqual(read.status_code, 200)
        self.assertEqual(
            read.json()["datasets"],
            [
                {
                    "id": str(self.orders.pk),
                    "display_name": "Orders",
                    "availability": "available",
                }
            ],
        )

    def test_member_candidates_are_minimal_and_scoped(self) -> None:
        response = self.client.get(
            f"/api/admin/departments/{self.finance.pk}/member-candidates/?q=sa"
        )
        self.assertEqual(response.status_code, 200)
        items = response.json()["items"]
        self.assertEqual([item["username"] for item in items], ["sales-person"])
        self.assertEqual(set(items[0]), {"id", "username", "first_name", "last_name"})
        short = self.client.get(
            f"/api/admin/departments/{self.finance.pk}/member-candidates/?q=s"
        )
        self.assertEqual(short.status_code, 400)

    def test_lead_subset_put_returns_the_membership_envelope(self) -> None:
        response = self.put(
            self.client,
            f"/api/admin/departments/{self.finance.pk}/members/"
            f"{self.finance_staff.pk}/datasets/",
            {"revision": self.revision(), "dataset_ids": []},
        )
        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertEqual(set(body), {"membership", "revision", "epoch"})
        self.assertEqual(body["membership"]["dataset_ids"], [])
        self.assertEqual(body["membership"]["user"]["username"], "finance-staff")

    def test_lead_cannot_touch_a_lead_assignment(self) -> None:
        response = self.put(
            self.client,
            f"/api/admin/departments/{self.finance.pk}/members/{self.lead.pk}/datasets/",
            {"revision": self.revision(), "dataset_ids": []},
        )
        self.assertEqual(response.status_code, 403)


class NativeAdminTests(AdminApiTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.root = self.make_account("root-admin", superuser=True)
        self.target = self.make_account("admin-target", groups=(STAFF_GROUP,))

    def test_department_admin_generates_code_and_rejects_duplicates(self) -> None:
        client = self.client_for(self.root)
        page = client.get("/admin/accounts/department/add/")
        self.assertEqual(page.status_code, 200)
        self.assertNotContains(page, 'name="code"')
        created = client.post(
            "/admin/accounts/department/add/",
            {"revision": self.revision(), "name": "Data Ops", "description": ""},
        )
        self.assertEqual(created.status_code, 302)
        before = self.revision()
        duplicate = client.post(
            "/admin/accounts/department/add/",
            {"revision": before, "name": "Data_Ops", "description": ""},
        )
        self.assertEqual(duplicate.status_code, 200)
        self.assertContains(duplicate, "A department with this name already exists.")
        self.assertEqual(self.revision(), before)

    def test_native_staff_without_application_role_cannot_administer(self) -> None:
        staff = self.make_account("auth-staff")
        staff.is_staff = True
        staff.save(update_fields=["is_staff"])
        staff.user_permissions.add(Permission.objects.get(codename="change_user"))
        client = Client()
        client.force_login(staff)
        self.assertEqual(client.get("/admin/").status_code, 200)
        forbidden = client.get("/admin/auth/user/")
        self.assertEqual(forbidden.status_code, 403)

    def test_superadmin_password_hash_never_reaches_admin_html(self) -> None:
        client = Client()
        client.force_login(self.root)
        response = client.get(f"/admin/auth/user/{self.target.pk}/change/")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(self.target.password, response.content.decode())
        self.assertNotIn("user_permissions", response.content.decode())

    def test_admin_delete_and_bulk_actions_are_disabled(self) -> None:
        client = Client()
        client.force_login(self.root)
        deleted = client.post(f"/admin/auth/user/{self.target.pk}/delete/", {})
        self.assertEqual(deleted.status_code, 403)
        self.assertTrue(User.objects.filter(pk=self.target.pk).exists())

        bulk = client.post(
            "/admin/auth/user/",
            {"action": "delete_selected", "_selected_action": [str(self.target.pk)]},
        )
        # No bulk actions are offered, so the request can never delete a row.
        self.assertIn(bulk.status_code, {200, 403})
        self.assertTrue(User.objects.filter(pk=self.target.pk).exists())

    def test_admin_password_page_requires_the_actor_password(self) -> None:
        client = Client()
        client.force_login(self.root)
        page = client.get(f"/admin/auth/user/{self.target.pk}/password/")
        self.assertEqual(page.status_code, 200)
        self.assertIn("current_password", page.content.decode())

        rejected = client.post(
            f"/admin/auth/user/{self.target.pk}/password/",
            {
                "revision": self.revision(),
                "current_password": "wrong-actor-password",
                "password1": "replacement-pass-3c",
                "password2": "replacement-pass-3c",
            },
        )
        self.assertEqual(rejected.status_code, 200)
        target = User.objects.get(pk=self.target.pk)
        self.assertTrue(target.check_password(PASSWORD))

    def test_admin_group_edits_are_unavailable(self) -> None:
        client = Client()
        client.force_login(self.root)
        group = self.group(STAFF_GROUP)
        listed = client.get("/admin/auth/group/")
        self.assertEqual(listed.status_code, 200)
        changed = client.post(
            f"/admin/auth/group/{group.pk}/change/",
            {"name": "Renamed", "permissions": []},
        )
        self.assertEqual(changed.status_code, 403)
        self.assertTrue(self.group(STAFF_GROUP))
