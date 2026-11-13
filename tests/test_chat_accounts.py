"""Provisioned account, login, logout, and API authentication tests."""

from __future__ import annotations

import json

from asgiref.sync import sync_to_async
from django.conf import settings
from django.contrib.auth.models import User
from django.test import AsyncClient, override_settings

from tests.support import (
    CSRF_PATTERN,
    PROVIDER_SETTINGS,
    THREADS_URL,
    ChatHttpTestCase,
)


def form_token(html: str) -> str:
    match = CSRF_PATTERN.search(html)
    if match is None:
        raise AssertionError("login form is missing a CSRF token")
    return match.group(1)


@override_settings(**PROVIDER_SETTINGS)
class LoginTests(ChatHttpTestCase):
    async def test_login_page_renders_a_labeled_form(self) -> None:
        response = await AsyncClient().get("/accounts/login/")
        body = response.content.decode()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Cache-Control"], "no-store")
        self.assertIn("Sign in", body)
        self.assertIn('autocomplete="username"', body)
        self.assertIn('autocomplete="current-password"', body)
        self.assertIn('name="csrfmiddlewaretoken"', body)
        self.assertIn('method="post"', body)
        self.assertNotIn("chat.js", body)

    async def test_workspace_requires_login_with_a_safe_next(self) -> None:
        thread_id = "2f9d4b1e-6c3a-4f5b-8e7d-0a1b2c3d4e5f"
        anonymous = AsyncClient()

        home = await anonymous.get("/")
        self.assertEqual(home.status_code, 302)
        self.assertEqual(home["Location"], "/accounts/login/?next=/")

        thread = await anonymous.get(f"/chat/{thread_id}/")
        self.assertEqual(thread.status_code, 302)
        self.assertEqual(
            thread["Location"],
            f"/accounts/login/?next=/chat/{thread_id}/",
        )

    async def test_valid_credentials_create_a_private_session(self) -> None:
        await self.make_user("login-owner", password="secure-chat-pass-42")
        client = AsyncClient()
        page = (await client.get("/accounts/login/")).content.decode()

        response = await client.post(
            "/accounts/login/",
            data={
                "username": "login-owner",
                "password": "secure-chat-pass-42",
                "csrfmiddlewaretoken": form_token(page),
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/")

        morsel = response.cookies[settings.SESSION_COOKIE_NAME]
        self.assertTrue(morsel["httponly"])
        self.assertEqual(str(morsel["samesite"]).lower(), "lax")

        workspace = await client.get("/")
        self.assertEqual(workspace.status_code, 200)
        self.assertIn("login-owner", workspace.content.decode())
        self.assertEqual(workspace["Cache-Control"], "no-store")

    async def test_bad_credentials_keep_the_generic_error(self) -> None:
        await self.make_user("login-owner", password="secure-chat-pass-42")
        client = AsyncClient()
        page = (await client.get("/accounts/login/")).content.decode()

        response = await client.post(
            "/accounts/login/",
            data={
                "username": "login-owner",
                "password": "wrong-password",
                "csrfmiddlewaretoken": form_token(page),
            },
        )
        body = response.content.decode()

        self.assertEqual(response.status_code, 200)
        self.assertIn("Please enter a correct", body)
        self.assertNotIn("wrong-password", body)
        self.assertEqual((await client.get("/")).status_code, 302)

    async def test_inactive_account_cannot_login(self) -> None:
        user = await self.make_user("inactive-owner", password="secure-chat-pass-42")
        user.is_active = False
        await sync_to_async(user.save)(update_fields=["is_active"])

        client = AsyncClient()
        page = (await client.get("/accounts/login/")).content.decode()
        response = await client.post(
            "/accounts/login/",
            data={
                "username": "inactive-owner",
                "password": "secure-chat-pass-42",
                "csrfmiddlewaretoken": form_token(page),
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual((await client.get("/")).status_code, 302)

    async def test_safe_next_is_honored_and_foreign_next_is_dropped(self) -> None:
        await self.make_user("next-owner", password="secure-chat-pass-42")
        target = "/chat/2f9d4b1e-6c3a-4f5b-8e7d-0a1b2c3d4e5f/"

        for label, next_value, expected in (
            ("safe", target, target),
            ("absolute", "https://evil.test/takeover", "/"),
            ("scheme-relative", "//evil.test/takeover", "/"),
        ):
            with self.subTest(case=label):
                client = AsyncClient()
                page = (await client.get("/accounts/login/")).content.decode()
                response = await client.post(
                    "/accounts/login/",
                    data={
                        "username": "next-owner",
                        "password": "secure-chat-pass-42",
                        "next": next_value,
                        "csrfmiddlewaretoken": form_token(page),
                    },
                )
                self.assertEqual(response.status_code, 302)
                self.assertEqual(response["Location"], expected)

    async def test_authenticated_login_page_redirects_away(self) -> None:
        user = await self.make_user("already-signed-in")
        client = await self.client_for(user)
        response = await client.get("/accounts/login/")
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/")


@override_settings(**PROVIDER_SETTINGS)
class LogoutTests(ChatHttpTestCase):
    async def test_logout_requires_post_and_csrf(self) -> None:
        user = await self.make_user("logout-owner")
        client = await self.client_for(user)

        get_response = await client.get("/accounts/logout/")
        self.assertEqual(get_response.status_code, 405)

        guarded = AsyncClient(enforce_csrf_checks=True)
        await guarded.aforce_login(user)
        without_token = await guarded.post("/accounts/logout/")
        self.assertEqual(without_token.status_code, 403)

    async def test_logout_keeps_account_threads(self) -> None:
        self._echo_agent()
        user = await self.make_user("logout-thread-owner")
        thread = await self.make_thread(user)
        client = await self.client_for(user)
        token = await self.token(client)
        await self.completed_turn(client, token, thread.id, "keep me", 0)

        response = await client.post(
            "/accounts/logout/",
            headers={"X-CSRFToken": token},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response["Location"], "/accounts/login/")

        self.assertEqual((await client.get("/")).status_code, 302)
        self.assertTrue(await self.row_exists(thread.id))

        again = await self.client_for(user)
        detail = await self.detail(again, thread.id)
        self.assertEqual(detail["display"][0]["content"], "keep me")


@override_settings(**PROVIDER_SETTINGS)
class AdminAccessTests(ChatHttpTestCase):
    async def test_nonstaff_user_cannot_reach_admin(self) -> None:
        user = await self.make_user("plain-user")
        client = await self.client_for(user)
        response = await client.get("/admin/")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/admin/login/", response["Location"])

    async def test_staff_superuser_can_reach_admin(self) -> None:
        admin = await sync_to_async(User.objects.create_superuser)(
            "provisioning-admin",
            password="secure-chat-pass-42",
        )
        client = await self.client_for(admin)
        response = await client.get("/admin/")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Site administration", response.content.decode())


@override_settings(**PROVIDER_SETTINGS)
class ApiAuthenticationTests(ChatHttpTestCase):
    async def test_anonymous_api_returns_json_401(self) -> None:
        anonymous = AsyncClient()
        listing = await anonymous.get(THREADS_URL)
        self.assertEqual(listing.status_code, 401)
        self.assertEqual(
            listing.json(),
            {
                "error": {
                    "code": "authentication_required",
                    "message": "Sign in to access your chats.",
                }
            },
        )
        self.assertEqual(listing["Cache-Control"], "no-store")
        self.assertNotIn("text/html", listing["Content-Type"])

    async def test_valid_csrf_without_login_still_returns_json_401(self) -> None:
        guarded = AsyncClient(enforce_csrf_checks=True)
        page = (await guarded.get("/accounts/login/")).content.decode()
        token = form_token(page)

        response = await guarded.post(
            THREADS_URL,
            data=json.dumps({}),
            content_type="application/json",
            headers={"X-CSRFToken": token},
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(self.error_code(response), "authentication_required")
        self.assertNotIn("text/html", response["Content-Type"])

    async def test_anonymous_mutations_are_never_html(self) -> None:
        thread_id = "2f9d4b1e-6c3a-4f5b-8e7d-0a1b2c3d4e5f"
        turn_id = "9a8b7c6d-5e4f-4a3b-8c2d-1e0f9a8b7c6d"
        anonymous = AsyncClient()
        responses = {
            "detail": await anonymous.get(f"/api/threads/{thread_id}/"),
            "rename": await anonymous.patch(
                f"/api/threads/{thread_id}/",
                data=json.dumps({"title": "x", "revision": 0}),
                content_type="application/json",
            ),
            "delete": await anonymous.delete(
                f"/api/threads/{thread_id}/",
                data=json.dumps({"revision": 0}),
                content_type="application/json",
            ),
            "send": await anonymous.post(
                f"/api/threads/{thread_id}/messages/",
                data=json.dumps({"message": "hi", "revision": 0}),
                content_type="application/json",
            ),
            "regenerate": await anonymous.post(
                f"/api/threads/{thread_id}/turns/{turn_id}/regenerate/",
                data=json.dumps({"revision": 0}),
                content_type="application/json",
            ),
            "delete-exchange": await anonymous.delete(
                f"/api/threads/{thread_id}/turns/{turn_id}/",
                data=json.dumps({"revision": 0}),
                content_type="application/json",
            ),
        }
        for label, response in responses.items():
            with self.subTest(endpoint=label):
                self.assertEqual(response.status_code, 401)
                self.assertEqual(self.error_code(response), "authentication_required")


@override_settings(**PROVIDER_SETTINGS)
class ProvisioningTests(ChatHttpTestCase):
    async def test_deactivating_a_user_hides_their_threads(self) -> None:
        user = await self.make_user("deactivate-owner")
        thread = await self.make_thread(user)

        user.is_active = False
        await sync_to_async(user.save)(update_fields=["is_active"])

        again = AsyncClient()
        page = (await again.get("/accounts/login/")).content.decode()
        response = await again.post(
            "/accounts/login/",
            data={
                "username": "deactivate-owner",
                "password": "chat-pass-7f3a",
                "csrfmiddlewaretoken": form_token(page),
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertTrue(await self.row_exists(thread.id))
