"""Owner-scoped thread CRUD, revision guards, and session-cleanup migration."""

from __future__ import annotations

import json
import uuid
from datetime import timedelta
from typing import Any
from unittest.mock import patch

from asgiref.sync import sync_to_async
from django.conf import settings
from django.contrib.auth.models import User
from django.contrib.sessions.models import Session
from django.db import IntegrityError, connection
from django.db.migrations.executor import MigrationExecutor
from django.test import TransactionTestCase, override_settings
from django.utils import timezone
from django.utils.module_loading import import_module

from chat import service, threads
from chat.models import ChatThread, ChatTurn
from tests.support import PROVIDER_SETTINGS, THREADS_URL, ChatHttpTestCase

SUMMARY_KEYS = {
    "id",
    "title",
    "revision",
    "created_at",
    "updated_at",
    "access",
    "department",
    "page_url",
    "detail_url",
    "messages_url",
}
MISSING_ID = "2f9d4b1e-6c3a-4f5b-8e7d-0a1b2c3d4e5f"


@override_settings(**PROVIDER_SETTINGS)
class SerializationTests(ChatHttpTestCase):
    async def test_create_returns_a_detail_with_same_origin_urls(self) -> None:
        user = await self.make_user("create-owner")
        client = await self.client_for(user)
        token = await self.token(client)

        response = await client.post(
            THREADS_URL,
            data="{}",
            content_type="application/json",
            headers={"X-CSRFToken": token},
        )
        self.assertEqual(response.status_code, 201)
        thread = response.json()["thread"]

        self.assertEqual(
            set(thread), SUMMARY_KEYS | {"display", "turns", "conversation_error"}
        )
        self.assertEqual(thread["title"], "New chat")
        self.assertEqual(thread["revision"], 0)
        self.assertEqual(thread["display"], [])
        self.assertIsNone(thread["conversation_error"])
        self.assertEqual(thread["turns"], [])
        self.assertEqual(thread["page_url"], f"/chat/{thread['id']}/")
        self.assertEqual(thread["detail_url"], f"/api/threads/{thread['id']}/")
        self.assertEqual(
            thread["messages_url"],
            f"/api/threads/{thread['id']}/messages/",
        )
        self.assertEqual(response["Location"], thread["detail_url"])

        listed = await self.summary_list(client)
        self.assertEqual(set(listed[0]), SUMMARY_KEYS)

    async def test_lists_are_scoped_and_ordered_by_recency(self) -> None:
        self._echo_agent()
        first_owner = await self.make_user("list-owner")
        second_owner = await self.make_user("list-stranger")

        first_client = await self.client_for(first_owner)
        first_token = await self.token(first_client)
        first = await self.make_thread(first_owner, title="First chat")
        await self.completed_turn(first_client, first_token, first.id, "one", 0)
        await self.make_thread(first_owner, title="Second chat")

        stranger_client = await self.client_for(second_owner)
        stranger_token = await self.token(stranger_client)
        stranger = await self.make_thread(second_owner, title="Stranger chat")
        await self.completed_turn(stranger_client, stranger_token, stranger.id, "hi", 0)

        listed = await self.summary_list(first_client)
        self.assertEqual(
            [entry["title"] for entry in listed], ["Second chat", "First chat"]
        )
        self.assertNotIn("Stranger chat", [entry["title"] for entry in listed])

        stranger_list = await self.summary_list(stranger_client)
        self.assertEqual([entry["id"] for entry in stranger_list], [str(stranger.id)])

        recency = [entry["updated_at"] for entry in listed]
        self.assertEqual(recency, sorted(recency, reverse=True))

    async def test_get_does_not_change_recency(self) -> None:
        self._echo_agent()
        user = await self.make_user("recency-owner")
        client = await self.client_for(user)
        token = await self.token(client)

        older = await self.make_thread(user, title="Older chat")
        await self.completed_turn(client, token, older.id, "older", 0)
        newer = await self.make_thread(user, title="Newer chat")

        before = await self.summary_list(client)
        self.assertEqual(
            [entry["id"] for entry in before], [str(newer.id), str(older.id)]
        )

        await self.detail(client, older.id)
        page = await client.get(f"/chat/{older.id}/")
        self.assertEqual(page.status_code, 200)

        after = await self.summary_list(client)
        self.assertEqual(
            [(entry["id"], entry["updated_at"]) for entry in after],
            [(entry["id"], entry["updated_at"]) for entry in before],
        )


@override_settings(**PROVIDER_SETTINGS)
class OwnershipTests(ChatHttpTestCase):
    async def prepare(self) -> Any:
        class Fixture:
            pass

        fixture = Fixture()
        fixture.factory = self._echo_agent()
        fixture.owner = await self.make_user("owner-a")
        fixture.stranger = await self.make_user("owner-b")
        fixture.owner_client = await self.client_for(fixture.owner)
        fixture.owner_token = await self.token(fixture.owner_client)
        fixture.stranger_client = await self.client_for(fixture.stranger)
        fixture.stranger_token = await self.token(fixture.stranger_client)
        fixture.owner_thread = await self.make_thread(
            fixture.owner, title="Owner private"
        )
        await self.completed_turn(
            fixture.owner_client,
            fixture.owner_token,
            fixture.owner_thread.id,
            "owner question",
            0,
        )
        fixture.owner_thread = await self.fetch_row(fixture.owner_thread.id)
        return fixture

    async def test_foreign_ids_are_indistinguishable_from_missing(self) -> None:
        fixture = await self.prepare()
        stranger = fixture.stranger_client
        token = fixture.stranger_token
        foreign_id = fixture.owner_thread.id

        operations = {
            "detail": lambda thread_id: stranger.get(f"/api/threads/{thread_id}/"),
            "rename": lambda thread_id: stranger.patch(
                f"/api/threads/{thread_id}/",
                data=json.dumps({"title": "Taken over", "revision": 1}),
                content_type="application/json",
                headers={"X-CSRFToken": token},
            ),
            "delete": lambda thread_id: stranger.delete(
                f"/api/threads/{thread_id}/",
                data=json.dumps({"revision": 1}),
                content_type="application/json",
                headers={"X-CSRFToken": token},
            ),
            "send": lambda thread_id: stranger.post(
                f"/api/threads/{thread_id}/messages/",
                data=json.dumps({"message": "hello", "revision": 1}),
                content_type="application/json",
                headers={"X-CSRFToken": token},
            ),
        }

        for label, call in operations.items():
            with self.subTest(operation=label):
                foreign = await call(foreign_id)
                missing = await call(MISSING_ID)
                self.assertEqual(foreign.status_code, 404)
                self.assertEqual(missing.status_code, 404)
                self.assertEqual(foreign.json(), missing.json())
                self.assertEqual(self.error_code(foreign), "thread_not_found")
                self.assertNotIn("Owner private", foreign.content.decode())
                self.assertNotIn("owner question", foreign.content.decode())

        row = await self.fetch_row(foreign_id)
        self.assertEqual(row.title, "Owner private")
        self.assertEqual(row.revision, 1)
        self.assertEqual(len(row.display), 2)
        # Only the owner's fixture turn ever reached the model.
        self.assertEqual(len(fixture.factory.seen), 1)

    async def test_foreign_send_is_rejected_before_provider_configuration(self) -> None:
        fixture = await self.prepare()
        calls_before = len(fixture.factory.seen)
        with override_settings(LLM_API_KEY="", LLM_BASE_URL="", LLM_MODEL=""):
            response = await fixture.stranger_client.post(
                f"/api/threads/{fixture.owner_thread.id}/messages/",
                data=json.dumps({"message": "hello", "revision": 1}),
                content_type="application/json",
                headers={"X-CSRFToken": fixture.stranger_token},
            )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(self.error_code(response), "thread_not_found")
        self.assertEqual(len(fixture.factory.seen), calls_before)

    async def test_supplied_identity_fields_are_rejected(self) -> None:
        fixture = await self.prepare()
        client = fixture.owner_client
        token = fixture.owner_token
        detail_url = f"/api/threads/{fixture.owner_thread.id}/"

        cases = {
            "patch owner": (
                "PATCH",
                detail_url,
                {"title": "x", "revision": 1, "owner": fixture.owner.pk},
                "invalid_thread",
            ),
            "patch history": (
                "PATCH",
                detail_url,
                {"title": "x", "revision": 1, "history_json": "[]"},
                "invalid_thread",
            ),
            "delete display": (
                "DELETE",
                detail_url,
                {"revision": 1, "display": []},
                "invalid_thread",
            ),
            "send display": (
                "POST",
                f"{detail_url}messages/",
                {"message": "hi", "revision": 1, "display": []},
                "invalid_message",
            ),
            "send history": (
                "POST",
                f"{detail_url}messages/",
                {"message": "hi", "revision": 1, "history": []},
                "invalid_message",
            ),
            "create owner": (
                "POST",
                THREADS_URL,
                {"owner": fixture.owner.pk},
                "invalid_thread",
            ),
        }

        for label, (method, url, payload, code) in cases.items():
            with self.subTest(case=label):
                response = await client.generic(
                    method,
                    url,
                    data=json.dumps(payload),
                    content_type="application/json",
                    headers={"X-CSRFToken": token},
                )
                self.assertEqual(response.status_code, 400)
                self.assertEqual(self.error_code(response), code)

    async def test_malformed_ids_share_the_json_404(self) -> None:
        fixture = await self.prepare()
        for path in ("/api/threads/not-a-uuid/", "/api/threads/1234/"):
            with self.subTest(path=path):
                response = await fixture.owner_client.get(path)
                self.assertEqual(response.status_code, 404)
                self.assertEqual(self.error_code(response), "thread_not_found")

        malformed_send = await fixture.owner_client.post(
            "/api/threads/not-a-uuid/messages/",
            data=json.dumps({"message": "hi", "revision": 0}),
            content_type="application/json",
        )
        self.assertEqual(malformed_send.status_code, 404)
        self.assertEqual(self.error_code(malformed_send), "thread_not_found")

    async def test_revision_is_required_and_strict(self) -> None:
        fixture = await self.prepare()
        client = fixture.owner_client
        token = fixture.owner_token
        detail_url = f"/api/threads/{fixture.owner_thread.id}/"

        for label, payload in (
            ("missing", {"title": "x"}),
            ("boolean", {"title": "x", "revision": True}),
            ("negative", {"title": "x", "revision": -1}),
            ("string", {"title": "x", "revision": "1"}),
        ):
            with self.subTest(case=label):
                response = await client.patch(
                    detail_url,
                    data=json.dumps(payload),
                    content_type="application/json",
                    headers={"X-CSRFToken": token},
                )
                self.assertEqual(response.status_code, 400)
                self.assertEqual(self.error_code(response), "invalid_thread")

        delete_without_revision = await client.delete(
            detail_url,
            data=json.dumps({}),
            content_type="application/json",
            headers={"X-CSRFToken": token},
        )
        self.assertEqual(delete_without_revision.status_code, 400)
        self.assertEqual(self.error_code(delete_without_revision), "invalid_thread")


@override_settings(**PROVIDER_SETTINGS)
class TitleTests(ChatHttpTestCase):
    async def test_title_length_and_blank_rules(self) -> None:
        user = await self.make_user("title-owner")
        thread = await self.make_thread(user)
        client = await self.client_for(user)
        token = await self.token(client)
        url = f"/api/threads/{thread.id}/"

        cases = {
            "blank": "   ",
            "empty": "",
            "too long": "\U0001f600" * 81,
            "non-string": 5,
        }
        for label, value in cases.items():
            with self.subTest(case=label):
                response = await client.patch(
                    url,
                    data=json.dumps({"title": value, "revision": 0}),
                    content_type="application/json",
                    headers={"X-CSRFToken": token},
                )
                self.assertEqual(response.status_code, 400)
                self.assertEqual(self.error_code(response), "invalid_thread")

        accepted = await client.patch(
            url,
            data=json.dumps({"title": "  " + "a" * 78, "revision": 0}),
            content_type="application/json",
            headers={"X-CSRFToken": token},
        )
        self.assertEqual(accepted.status_code, 200)
        self.assertEqual(accepted.json()["thread"]["title"], "a" * 78)
        self.assertEqual(accepted.json()["thread"]["revision"], 1)

        exact = await client.patch(
            url,
            data=json.dumps({"title": "\U0001f600" * 80, "revision": 1}),
            content_type="application/json",
            headers={"X-CSRFToken": token},
        )
        self.assertEqual(exact.status_code, 200)
        self.assertEqual(exact.json()["thread"]["title"], "\U0001f600" * 80)

    async def test_rename_persists_for_a_new_session(self) -> None:
        self._echo_agent()
        user = await self.make_user("rename-owner")
        thread = await self.make_thread(user)
        client = await self.client_for(user)
        token = await self.token(client)
        await self.completed_turn(client, token, thread.id, "hello", 0)

        renamed = await client.patch(
            f"/api/threads/{thread.id}/",
            data=json.dumps({"title": "Project notes", "revision": 1}),
            content_type="application/json",
            headers={"X-CSRFToken": token},
        )
        self.assertEqual(renamed.status_code, 200)
        self.assertEqual(renamed.json()["thread"]["title"], "Project notes")
        self.assertEqual(renamed.json()["thread"]["revision"], 2)

        again = await self.client_for(user)
        listed = await self.summary_list(again)
        self.assertEqual(listed[0]["title"], "Project notes")
        page = (await again.get(f"/chat/{thread.id}/")).content.decode()
        self.assertIn("Project notes", page)

    async def test_rename_keeps_display_and_error_state(self) -> None:
        user = await self.make_user("rename-corrupt-owner")
        thread = await self.make_thread(user)
        await sync_to_async(ChatThread.objects.filter(pk=thread.id).update)(
            history_json="broken",
            display="broken",
        )
        client = await self.client_for(user)
        token = await self.token(client)

        renamed = await client.patch(
            f"/api/threads/{thread.id}/",
            data=json.dumps({"title": "Still editable", "revision": 0}),
            content_type="application/json",
            headers={"X-CSRFToken": token},
        )
        self.assertEqual(renamed.status_code, 200)

        detail = await self.detail(client, thread.id)
        self.assertEqual(detail["title"], "Still editable")
        self.assertEqual(detail["display"], [])
        self.assertEqual(
            detail["conversation_error"],
            threads.CONVERSATION_INVALID_MESSAGE,
        )
        row = await self.fetch_row(thread.id)
        self.assertEqual(row.history_json, "broken")


@override_settings(**PROVIDER_SETTINGS)
class RevisionTests(ChatHttpTestCase):
    async def test_stale_rename_is_rejected_and_changes_nothing(self) -> None:
        user = await self.make_user("stale-rename-owner")
        thread = await self.make_thread(user)
        client = await self.client_for(user)
        token = await self.token(client)

        first = await client.patch(
            f"/api/threads/{thread.id}/",
            data=json.dumps({"title": "Current name", "revision": 0}),
            content_type="application/json",
            headers={"X-CSRFToken": token},
        )
        self.assertEqual(first.status_code, 200)

        stale = await client.patch(
            f"/api/threads/{thread.id}/",
            data=json.dumps({"title": "Stale name", "revision": 0}),
            content_type="application/json",
            headers={"X-CSRFToken": token},
        )
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(self.error_code(stale), "thread_conflict")

        row = await self.fetch_row(thread.id)
        self.assertEqual(row.title, "Current name")
        self.assertEqual(row.revision, 1)

    async def test_delete_requires_the_current_revision(self) -> None:
        user = await self.make_user("stale-delete-owner")
        thread = await self.make_thread(user)
        client = await self.client_for(user)
        token = await self.token(client)

        await client.patch(
            f"/api/threads/{thread.id}/",
            data=json.dumps({"title": "Renamed", "revision": 0}),
            content_type="application/json",
            headers={"X-CSRFToken": token},
        )

        stale = await client.delete(
            f"/api/threads/{thread.id}/",
            data=json.dumps({"revision": 0}),
            content_type="application/json",
            headers={"X-CSRFToken": token},
        )
        self.assertEqual(stale.status_code, 409)
        self.assertEqual(self.error_code(stale), "thread_conflict")
        self.assertTrue(await self.row_exists(thread.id))

        deleted = await client.delete(
            f"/api/threads/{thread.id}/",
            data=json.dumps({"revision": 1}),
            content_type="application/json",
            headers={"X-CSRFToken": token},
        )
        self.assertEqual(deleted.status_code, 200)
        self.assertFalse(await self.row_exists(thread.id))

    async def test_delete_removes_only_the_selected_thread(self) -> None:
        self._echo_agent()
        owner = await self.make_user("delete-owner")
        stranger = await self.make_user("delete-stranger")
        owner_client = await self.client_for(owner)
        owner_token = await self.token(owner_client)
        first = await self.make_thread(owner, title="Delete me")
        second = await self.make_thread(owner, title="Keep me")
        stranger_thread = await self.make_thread(stranger, title="Stranger keeps")

        deleted = await owner_client.delete(
            f"/api/threads/{first.id}/",
            data=json.dumps({"revision": 0}),
            content_type="application/json",
            headers={"X-CSRFToken": owner_token},
        )
        self.assertEqual(deleted.status_code, 200)
        self.assertEqual(deleted.json(), {"ok": True})

        self.assertFalse(await self.row_exists(first.id))
        self.assertTrue(await self.row_exists(second.id))
        self.assertTrue(await self.row_exists(stranger_thread.id))

        again = await self.client_for(owner)
        listed = await self.summary_list(again)
        self.assertEqual([entry["title"] for entry in listed], ["Keep me"])

    async def test_deleting_the_user_cascades_threads(self) -> None:
        user = await self.make_user("cascade-owner")
        first = await self.make_thread(user)
        second = await self.make_thread(user)

        await sync_to_async(user.delete)()

        self.assertFalse(await self.row_exists(first.id))
        self.assertFalse(await self.row_exists(second.id))
        self.assertFalse(await sync_to_async(User.objects.filter(pk=user.pk).exists)())


class LegacySessionCleanupTests(TransactionTestCase):
    """The data migration removes exactly the two anonymous chat keys."""

    def migrate(self, targets: list[tuple[str, str]]) -> None:
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(targets)

    def tearDown(self) -> None:
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(executor.loader.graph.leaf_nodes())
        super().tearDown()

    def test_only_the_two_chat_keys_disappear(self) -> None:
        self.migrate([("chat", "0001_initial")])

        user = User.objects.create_user("migration-user", password="unused-pass-1")
        # The current model has newer columns than this schema, so the fixture
        # row is inserted with the columns 0001 actually defines.
        thread_id = uuid.uuid4()
        now = timezone.now()
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO chat_chatthread "
                "(id, owner_id, title, history_json, display, revision, "
                "created_at, updated_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                [
                    thread_id.hex,
                    user.pk,
                    "Kept thread",
                    "[]",
                    "[]",
                    0,
                    now.isoformat(),
                    now.isoformat(),
                ],
            )
        store = import_module(settings.SESSION_ENGINE).SessionStore()
        expires = timezone.now() + timedelta(days=1)

        payloads: dict[str, Any] = {
            "legacy": {
                "chat_history_json": "[]",
                "chat_display": [],
                "unrelated_sentinel": "keep",
            },
            "clean": {"unrelated": {"nested": [1, 2]}},
            "display-only": {"chat_display": [{"role": "user", "content": "x"}]},
        }
        rows: dict[str, str] = {}
        for session_key, data in payloads.items():
            encoded = store.encode(data)
            rows[session_key] = encoded
            Session.objects.create(
                session_key=session_key,
                session_data=encoded,
                expire_date=expires,
            )
        Session.objects.create(
            session_key="garbage",
            session_data="not a session payload",
            expire_date=expires,
        )
        before = {
            session.session_key: (session.session_data, session.expire_date)
            for session in Session.objects.all()
        }
        thread_count = ChatThread.objects.count()

        self.migrate([("chat", "0002_remove_legacy_chat_session_keys")])

        after = {
            session.session_key: (session.session_data, session.expire_date)
            for session in Session.objects.all()
        }

        self.assertEqual(set(after), set(before))
        for session_key in before:
            self.assertEqual(after[session_key][1], before[session_key][1])
        self.assertEqual(after["clean"][0], before["clean"][0])
        self.assertEqual(after["garbage"][0], before["garbage"][0])
        self.assertEqual(
            store.decode(after["legacy"][0]),
            {"unrelated_sentinel": "keep"},
        )
        self.assertEqual(
            store.decode(after["display-only"][0]),
            {},
        )
        self.assertEqual(ChatThread.objects.count(), thread_count)
        self.assertTrue(ChatThread.objects.filter(pk=thread_id).exists())
        self.assertTrue(User.objects.filter(pk=user.pk).exists())


@override_settings(**PROVIDER_SETTINGS)
class TurnStorageTests(ChatHttpTestCase):
    """Private per-turn checkpoints and their public projection."""

    async def rows(self, thread_id: Any) -> list[ChatTurn]:
        return await sync_to_async(
            lambda: list(
                ChatTurn.objects.filter(thread_id=thread_id).order_by("turn_index")
            )
        )()

    async def test_two_turns_store_exact_round_trippable_checkpoints(self) -> None:
        self._echo_agent()
        user = await self.make_user("turn-owner")
        thread = await self.make_thread(user)
        client = await self.client_for(user)
        token = await self.token(client)

        await self.completed_turn(client, token, thread.id, "first prompt", 0)
        after_first = await self.fetch_row(thread.id)
        await self.completed_turn(client, token, thread.id, "second prompt", 1)

        rows = await self.rows(thread.id)
        self.assertEqual([row.turn_index for row in rows], [0, 1])
        self.assertEqual(rows[0].pre_history_json, "[]")
        self.assertEqual(rows[1].pre_history_json, after_first.history_json)
        for row in rows:
            service.load_history(row.pre_history_json)
            self.assertIsNotNone(row.completed_at)
            self.assertIsNone(row.resolved_model)
            self.assertEqual(row.requested_model, "offline-model")

    async def test_public_turns_hide_the_checkpoint(self) -> None:
        self._echo_agent()
        user = await self.make_user("turn-shape-owner")
        thread = await self.make_thread(user)
        client = await self.client_for(user)
        token = await self.token(client)
        await self.completed_turn(client, token, thread.id, "one", 0)
        await self.completed_turn(client, token, thread.id, "two", 1)

        detail = await self.detail(client, thread.id)
        self.assertEqual([turn["turn_index"] for turn in detail["turns"]], [0, 1])
        for turn in detail["turns"]:
            self.assertEqual(
                set(turn),
                {
                    "turn_id",
                    "turn_index",
                    "response",
                    "activity",
                    "attachments",
                    "can_regenerate",
                    "can_delete",
                    "regenerate_url",
                    "delete_url",
                },
            )
            self.assertEqual(turn["attachments"], [])
            self.assertEqual(
                set(turn["response"]),
                {
                    "requested_model",
                    "resolved_model",
                    "completed_at",
                    "duration_ms",
                    "output_tokens",
                    "output_limit_reached",
                },
            )
            self.assertFalse(turn["response"]["output_limit_reached"])
            self.assertEqual(turn["response"]["requested_model"], "offline-model")
            self.assertIsNone(turn["response"]["resolved_model"])
            self.assertGreaterEqual(turn["response"]["duration_ms"], 0)
            self.assertEqual(
                turn["regenerate_url"],
                f"/api/threads/{thread.id}/turns/{turn['turn_id']}/regenerate/",
            )
            self.assertEqual(
                turn["delete_url"],
                f"/api/threads/{thread.id}/turns/{turn['turn_id']}/",
            )

        # Only the newest exchange is actionable.
        self.assertFalse(detail["turns"][0]["can_regenerate"])
        self.assertFalse(detail["turns"][0]["can_delete"])
        self.assertTrue(detail["turns"][1]["can_regenerate"])
        self.assertTrue(detail["turns"][1]["can_delete"])

        body = json.dumps(detail)
        self.assertNotIn("pre_history_json", body)
        self.assertNotIn("history_json", body)

    async def test_legacy_display_has_no_turn_records(self) -> None:
        self._echo_agent()
        user = await self.make_user("legacy-turn-owner")
        thread = await self.make_thread(user)
        client = await self.client_for(user)
        token = await self.token(client)
        await self.completed_turn(client, token, thread.id, "legacy prompt", 0)
        await sync_to_async(ChatTurn.objects.filter(thread_id=thread.id).delete)()

        detail = await self.detail(client, thread.id)
        self.assertEqual(len(detail["display"]), 2)
        self.assertEqual(detail["turns"], [])

        page = (await client.get(f"/chat/{thread.id}/")).content.decode()
        self.assertIn("legacy prompt", page)

    async def test_corrupt_auxiliary_data_stays_readable_and_null(self) -> None:
        self._echo_agent()
        user = await self.make_user("turn-corrupt-owner")
        thread = await self.make_thread(user)
        client = await self.client_for(user)
        token = await self.token(client)
        await self.completed_turn(client, token, thread.id, "kept prompt", 0)
        row = (await self.rows(thread.id))[0]

        await sync_to_async(ChatTurn.objects.filter(pk=row.pk).update)(
            activity={
                "version": 1,
                "turn_id": "not-a-uuid",
                "steps": [],
                "truncated": False,
            },
            requested_model="bad\nlabel",
            duration_ms=2_147_483_648,
        )

        detail = await self.detail(client, thread.id)
        self.assertEqual(len(detail["display"]), 2)
        self.assertEqual(len(detail["turns"]), 1)
        turn = detail["turns"][0]
        self.assertIsNone(turn["activity"])
        self.assertIsNone(turn["response"]["requested_model"])
        self.assertIsNone(turn["response"]["duration_ms"])
        self.assertTrue(turn["can_delete"])

    async def test_corrupt_checkpoint_disables_actions_only(self) -> None:
        self._echo_agent()
        user = await self.make_user("turn-broken-checkpoint")
        thread = await self.make_thread(user)
        client = await self.client_for(user)
        token = await self.token(client)
        await self.completed_turn(client, token, thread.id, "prompt", 0)
        row = (await self.rows(thread.id))[0]

        await sync_to_async(ChatTurn.objects.filter(pk=row.pk).update)(
            pre_history_json="not canonical"
        )

        detail = await self.detail(client, thread.id)
        self.assertEqual(len(detail["display"]), 2)
        turn = detail["turns"][0]
        self.assertFalse(turn["can_regenerate"])
        self.assertFalse(turn["can_delete"])
        self.assertIsNotNone(turn["response"])

    async def test_live_lease_disables_actions(self) -> None:
        self._echo_agent()
        user = await self.make_user("turn-lease-owner")
        thread = await self.make_thread(user)
        client = await self.client_for(user)
        token = await self.token(client)
        await self.completed_turn(client, token, thread.id, "prompt", 0)

        await sync_to_async(ChatThread.objects.filter(pk=thread.id).update)(
            active_run_token=uuid.uuid4(),
            active_run_expires_at=timezone.now() + timedelta(seconds=60),
        )
        detail = await self.detail(client, thread.id)
        self.assertFalse(detail["turns"][0]["can_regenerate"])
        self.assertFalse(detail["turns"][0]["can_delete"])

        await sync_to_async(ChatThread.objects.filter(pk=thread.id).update)(
            active_run_expires_at=timezone.now() - timedelta(seconds=1)
        )
        detail = await self.detail(client, thread.id)
        self.assertTrue(detail["turns"][0]["can_delete"])

    async def test_regenerate_flag_requires_a_configured_provider(self) -> None:
        self._echo_agent()
        user = await self.make_user("turn-provider-owner")
        thread = await self.make_thread(user)
        client = await self.client_for(user)
        token = await self.token(client)
        await self.completed_turn(client, token, thread.id, "prompt", 0)

        with override_settings(LLM_API_KEY="", LLM_BASE_URL="", LLM_MODEL=""):
            detail = await self.detail(client, thread.id)
        turn = detail["turns"][0]
        self.assertFalse(turn["can_regenerate"])
        self.assertTrue(turn["can_delete"])

    async def test_checkpoint_aggregate_bound_refuses_the_turn(self) -> None:
        self._echo_agent()
        user = await self.make_user("turn-checkpoint-budget")
        thread = await self.make_thread(user)
        client = await self.client_for(user)
        token = await self.token(client)
        await self.completed_turn(client, token, thread.id, "first", 0)
        before = await self.fetch_row(thread.id)
        rows_before = await self.rows(thread.id)

        with patch.object(threads, "MAX_CHECKPOINT_BYTES", 10):
            body, parsed = await self.turn(client, token, thread.id, "second", 1)

        self.assertEqual(parsed[-1][0], "error")
        self.assertEqual(parsed[-1][1]["error"]["code"], "conversation_limit")
        self.assertNotIn(b"event: done", body)

        after = await self.fetch_row(thread.id)
        self.assertEqual(after.history_json, before.history_json)
        self.assertEqual(after.display, before.display)
        self.assertEqual(after.revision, before.revision)
        self.assertEqual(await self.rows(thread.id), rows_before)

    async def test_child_write_failure_rolls_back_the_thread(self) -> None:
        self._echo_agent()
        user = await self.make_user("turn-rollback-owner")
        thread = await self.make_thread(user)
        client = await self.client_for(user)
        token = await self.token(client)
        await self.completed_turn(client, token, thread.id, "first", 0)
        snapshot = await self.fetch_row(thread.id)
        completed = service.CompletedReply(
            reply="replacement",
            history_json=snapshot.history_json,
            activity=None,
        )

        run_token = await sync_to_async(threads.acquire_run)(
            owner_id=user.pk, thread_id=thread.id, revision=snapshot.revision
        )

        with (
            patch.object(
                type(ChatTurn.objects),
                "create",
                side_effect=IntegrityError("synthetic insert failure"),
            ),
            self.assertRaises(IntegrityError),
        ):
            await sync_to_async(threads.commit_turn)(
                owner_id=user.pk,
                snapshot=snapshot,
                message="second",
                completed=completed,
                turn_id=uuid.uuid4(),
                run_token=run_token,
            )

        after = await self.fetch_row(thread.id)
        self.assertEqual(after.display, snapshot.display)
        self.assertEqual(after.history_json, snapshot.history_json)
        self.assertEqual(after.revision, snapshot.revision)
        self.assertEqual(len(await self.rows(thread.id)), 1)

    async def test_turns_cascade_with_thread_and_account(self) -> None:
        self._echo_agent()
        user = await self.make_user("turn-cascade-owner")
        first = await self.make_thread(user)
        second = await self.make_thread(user)
        client = await self.client_for(user)
        token = await self.token(client)
        await self.completed_turn(client, token, first.id, "keep", 0)
        await self.completed_turn(client, token, second.id, "drop", 0)

        await client.delete(
            f"/api/threads/{second.id}/",
            data=json.dumps({"revision": 1}),
            content_type="application/json",
            headers={"X-CSRFToken": token},
        )
        self.assertEqual(await self.rows(second.id), [])
        self.assertEqual(len(await self.rows(first.id)), 1)

        await sync_to_async(user.delete)()
        self.assertEqual(await self.rows(first.id), [])


class TurnSchemaMigrationTests(TransactionTestCase):
    """0003 adds the turn table and lease without touching conversations."""

    def migrate(self, targets: list[tuple[str, str]]) -> None:
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(targets)

    def tearDown(self) -> None:
        executor = MigrationExecutor(connection)
        executor.loader.build_graph()
        executor.migrate(executor.loader.graph.leaf_nodes())
        super().tearDown()

    def test_existing_threads_survive_and_no_turns_are_invented(self) -> None:
        self.migrate([("chat", "0002_remove_legacy_chat_session_keys")])

        user = User.objects.create_user("turn-migration-user", password="unused-pass-1")
        thread_id = uuid.uuid4()
        display = json.dumps(
            [
                {"role": "user", "content": "old question"},
                {"role": "assistant", "content": "old answer"},
            ]
        )
        now = timezone.now()
        with connection.cursor() as cursor:
            cursor.execute(
                "INSERT INTO chat_chatthread "
                "(id, owner_id, title, history_json, display, revision, "
                "created_at, updated_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                [
                    thread_id.hex,
                    user.pk,
                    "Kept thread",
                    "[]",
                    display,
                    4,
                    now.isoformat(),
                    now.isoformat(),
                ],
            )

        self.migrate([("chat", "0003_chatthread_turn_activity")])

        # 0005 adds columns the current model selects, so read the row raw.
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT title, history_json, display, revision, "
                "active_run_token, active_run_expires_at "
                "FROM chat_chatthread WHERE id = %s",
                [thread_id.hex],
            )
            kept = cursor.fetchone()
        self.assertIsNotNone(kept)
        title, history_json, stored_display, revision, token, expires = kept
        self.assertEqual(title, "Kept thread")
        self.assertEqual(history_json, "[]")
        self.assertEqual(json.loads(stored_display), json.loads(display))
        self.assertEqual(revision, 4)
        self.assertIsNone(token)
        self.assertIsNone(expires)
        self.assertEqual(ChatTurn.objects.count(), 0)

        self.migrate([("chat", "0002_remove_legacy_chat_session_keys")])
        tables = connection.introspection.table_names()
        self.assertNotIn("chat_chatturn", tables)
        # The current model selects columns 0003 removed, so read the raw row.
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT display, revision FROM chat_chatthread WHERE id = %s",
                [thread_id.hex],
            )
            stored_display, stored_revision = cursor.fetchone()
        self.assertEqual(json.loads(stored_display), json.loads(display))
        self.assertEqual(stored_revision, 4)
