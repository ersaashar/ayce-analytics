"""Offline coverage for conversation-local file attachments.

Every model call runs through the real agent graph with a local
``FunctionModel`` (or a real OpenAI adapter over a local transport), so what the
model sees is asserted from actual model input rather than from helper echo.
"""

from __future__ import annotations

import io
import json
import uuid
import zipfile
from datetime import date
from typing import Any
from unittest.mock import patch

import httpx2
import pydantic_ai.models as pai_models
from asgiref.sync import sync_to_async
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase, override_settings
from pydantic_ai import Agent, ModelSettings
from pydantic_ai.exceptions import ModelHTTPError
from pydantic_ai.messages import (
    BinaryContent,
    ModelMessage,
    ModelRequest,
    TextContent,
    UserPromptPart,
)
from pydantic_ai.models.openai import OpenAIChatModel
from pydantic_ai.providers.openai_compatible import OpenAICompatibleProvider

from chat import attachments, service
from chat.agent import INSTRUCTIONS
from chat.models import ChatAttachment, ChatThread, ChatTurn
from tests.support import (
    PROVIDER_SETTINGS,
    ChatHttpTestCase,
    deltas,
    events,
    read_body,
)

DEFAULTS = attachments.AttachmentLimits(
    max_files_per_thread=5,
    max_files_per_turn=5,
    max_file_bytes=5 * 1024 * 1024,
    max_text_bytes_per_file=64 * 1024,
    max_text_bytes_per_thread=256 * 1024,
)


def upload(name: str, data: bytes) -> SimpleUploadedFile:
    return SimpleUploadedFile(name, data)


class RawUpload:
    """Minimal upload shape for names Django's own file wrapper refuses."""

    def __init__(self, name: str, data: bytes) -> None:
        self.name = name
        self._data = data

    def read(self, size: int = -1) -> bytes:
        return self._data[:size] if size >= 0 else self._data


def docx_bytes() -> bytes:
    from docx import Document

    document = Document()
    document.add_paragraph("Quarterly summary")
    table = document.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "region"
    table.cell(0, 1).text = "total"
    table.cell(1, 0).text = "north"
    table.cell(1, 1).text = "137"
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def xlsx_bytes() -> bytes:
    from openpyxl import Workbook

    workbook = Workbook()
    first = workbook.active
    first.title = "Totals"
    first.append(["region", "invoice_total"])
    first.append(["north", 137])
    first["C1"] = "=SUM(B2:B2)"
    second = workbook.create_sheet("Notes")
    second.append(["date", "note"])
    second.append([date(2026, 1, 2), "hello"])
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def prepared(
    name: str = "notes.txt",
    data: bytes = b"invoice_total=137",
    *,
    media_type: str = "text/plain",
    content_media_type: str | None = None,
) -> attachments.PreparedAttachment:
    return attachments.PreparedAttachment(
        id=uuid.uuid4(),
        name=name,
        media_type=media_type,
        size=len(data),
        content_media_type=content_media_type or media_type,
        data=data,
    )


def prompt_content(messages: list[ModelMessage]) -> list[Any]:
    """Flatten every user-prompt item the model actually received."""
    items: list[Any] = []
    for message in messages:
        if not isinstance(message, ModelRequest):
            continue
        for part in message.parts:
            if not isinstance(part, UserPromptPart):
                continue
            if isinstance(part.content, str):
                items.append(part.content)
            else:
                items.extend(part.content)
    return items


def binary_payloads(messages: list[ModelMessage]) -> list[tuple[str, bytes]]:
    return [
        (item.media_type, item.data)
        for item in prompt_content(messages)
        if isinstance(item, BinaryContent)
    ]


def marker_ids(messages: list[ModelMessage]) -> list[str]:
    ids: list[str] = []
    for message in messages:
        if not isinstance(message, ModelRequest):
            continue
        for part in message.parts:
            if not isinstance(part, UserPromptPart):
                continue
            content = part.content
            if not isinstance(content, list):
                continue
            for item in content:
                if isinstance(item, TextContent) and isinstance(item.metadata, dict):
                    ids.extend(item.metadata.get(attachments.ATTACHMENT_IDS_KEY, []))
    return ids


class PreparationTests(SimpleTestCase):
    """Upload validation and Office text extraction, without HTTP or a model."""

    def prepare(self, *uploads: Any, limits: Any = DEFAULTS):
        return attachments.prepare_attachments(uploads, limits=limits)

    def test_docx_keeps_paragraphs_and_table_rows_in_order(self) -> None:
        result = self.prepare(upload("report.docx", docx_bytes()))

        self.assertEqual(len(result), 1)
        attachment = result[0]
        self.assertEqual(attachment.content_media_type, "text/plain")
        self.assertEqual(
            attachment.media_type,
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
        text = attachment.data.decode()
        self.assertIn("Quarterly summary", text)
        self.assertIn("region\ttotal", text)
        self.assertIn("north\t137", text)
        self.assertLess(text.index("Quarterly summary"), text.index("north"))

    def test_xlsx_keeps_sheets_formulas_and_iso_dates(self) -> None:
        result = self.prepare(upload("book.xlsx", xlsx_bytes()))

        text = result[0].data.decode()
        self.assertIn("Sheet: Totals", text)
        self.assertIn("region\tinvoice_total\t=SUM(B2:B2)", text)
        self.assertIn("north\t137", text)
        self.assertIn("Sheet: Notes", text)
        self.assertIn("2026-01-02T00:00:00\thello", text)
        self.assertLess(text.index("Sheet: Totals"), text.index("Sheet: Notes"))

    def test_rejects_unsupported_and_legacy_extensions(self) -> None:
        for name in ("old.doc", "old.xls", "macro.docm", "sheet.xlsm", "photo.svg"):
            with self.subTest(name=name):
                with self.assertRaises(attachments.AttachmentError) as caught:
                    self.prepare(upload(name, b"data"))
                self.assertEqual(caught.exception.code, "unsupported_attachment")

    def test_rejects_empty_undecodable_and_whitespace_only_text(self) -> None:
        cases = {
            "empty.txt": b"",
            "binary.txt": b"\xff\xfe\x00\x01",
            "blank.txt": b"   \n\t ",
            "nul.txt": b"abc\x00def",
        }
        for name, data in cases.items():
            with self.subTest(name=name):
                with self.assertRaises(attachments.AttachmentError) as caught:
                    self.prepare(upload(name, data))
                self.assertEqual(caught.exception.code, "invalid_attachment")

    def test_accepts_utf8_bom_and_normalizes_it_away(self) -> None:
        result = self.prepare(upload("bom.txt", b"\xef\xbb\xbfhello"))

        self.assertEqual(result[0].data, b"hello")

    def test_rejects_signature_mismatches_and_corrupt_documents(self) -> None:
        cases = {
            "fake.png": b"not a png at all",
            "fake.pdf": b"plain text",
            "fake.jpg": b"\x89PNG\r\n\x1a\n",
            "fake.gif": b"GIF00a",
            "fake.webp": b"RIFFxxxxWAVE",
            "broken.docx": b"PK\x03\x04not really",
        }
        for name, data in cases.items():
            with self.subTest(name=name):
                with self.assertRaises(attachments.AttachmentError) as caught:
                    self.prepare(upload(name, data))
                self.assertEqual(caught.exception.code, "invalid_attachment")

    def test_rejects_encrypted_archives(self) -> None:
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as archive:
            info = zipfile.ZipInfo("word/document.xml")
            info.flag_bits |= 0x1
            archive.writestr(info, b"<document/>")
        with self.assertRaises(attachments.AttachmentError) as caught:
            self.prepare(upload("secret.docx", buffer.getvalue()))
        self.assertEqual(caught.exception.code, "invalid_attachment")

    def test_rejects_archive_expansion_beyond_fixed_guards(self) -> None:
        many = io.BytesIO()
        with zipfile.ZipFile(many, "w") as archive:
            archive.writestr("[Content_Types].xml", b"<Types/>")
            archive.writestr("word/document.xml", b"<document/>")
            for index in range(attachments.MAX_OOXML_ENTRIES):
                archive.writestr(f"word/part{index}.xml", b"<x/>")
        with self.assertRaises(attachments.AttachmentError) as caught:
            self.prepare(upload("many.docx", many.getvalue()))
        self.assertEqual(caught.exception.code, "attachment_limit")

        oversized = io.BytesIO()
        with zipfile.ZipFile(oversized, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("[Content_Types].xml", b"<Types/>")
            archive.writestr("word/document.xml", b"<document/>")
            archive.writestr(
                "word/media/blob.bin",
                b"\x00" * (attachments.MAX_OOXML_MEMBER_BYTES + 1),
            )
        with self.assertRaises(attachments.AttachmentError) as caught:
            self.prepare(upload("burst.docx", oversized.getvalue()))
        self.assertEqual(caught.exception.code, "attachment_limit")

    def test_rejects_macro_projects_and_dtd_payloads(self) -> None:
        macro = io.BytesIO()
        with zipfile.ZipFile(macro, "w") as archive:
            archive.writestr("[Content_Types].xml", b"<Types/>")
            archive.writestr("word/document.xml", b"<document/>")
            archive.writestr("word/vbaProject.bin", b"macro")
        with self.assertRaises(attachments.AttachmentError) as caught:
            self.prepare(upload("macro.docx", macro.getvalue()))
        self.assertEqual(caught.exception.code, "unsupported_attachment")

        dtd = io.BytesIO()
        with zipfile.ZipFile(dtd, "w") as archive:
            archive.writestr("[Content_Types].xml", b"<Types/>")
            archive.writestr(
                "word/document.xml",
                b'<!DOCTYPE x [<!ENTITY e SYSTEM "file:///etc/passwd">]><document/>',
            )
        with self.assertRaises(attachments.AttachmentError) as caught:
            self.prepare(upload("dtd.docx", dtd.getvalue()))
        self.assertEqual(caught.exception.code, "invalid_attachment")

    def test_rejects_empty_extracted_text(self) -> None:
        empty = io.BytesIO()
        with zipfile.ZipFile(empty, "w") as archive:
            archive.writestr("[Content_Types].xml", b"<Types/>")
            archive.writestr("word/document.xml", b"<document/>")
        with self.assertRaises(attachments.AttachmentError) as caught:
            self.prepare(upload("blank.docx", empty.getvalue()))
        self.assertEqual(caught.exception.code, "invalid_attachment")

    def test_enforces_count_byte_and_text_boundaries(self) -> None:
        limits = attachments.AttachmentLimits(
            max_files_per_thread=3,
            max_files_per_turn=2,
            max_file_bytes=8,
            max_text_bytes_per_file=6,
            max_text_bytes_per_thread=10,
        )
        # Equality is accepted at every boundary.
        self.assertEqual(
            len(self.prepare(upload("a.txt", b"123456"), limits=limits)), 1
        )
        self.assertEqual(
            len(
                self.prepare(
                    upload("a.txt", b"123456"), upload("b.txt", b"1"), limits=limits
                )
            ),
            2,
        )
        # A native payload is bounded by original bytes only.
        self.assertEqual(
            len(self.prepare(upload("big.pdf", b"%PDF-1.4"), limits=limits)), 1
        )

        cases = [
            # One byte over the per-file original-byte limit.
            [upload("over.txt", b"123456789")],
            # One byte over the per-file prepared-text limit.
            [upload("over.txt", b"1234567")],
            # Two files whose prepared text exceeds the per-thread limit.
            [upload("a.txt", b"123456"), upload("b.txt", b"12345")],
            # More files in one turn than the per-turn quota allows.
            [
                upload("a.txt", b"1"),
                upload("b.txt", b"2"),
                upload("c.txt", b"3"),
            ],
        ]
        for files in cases:
            with self.subTest(names=[item.name for item in files]):
                with self.assertRaises(attachments.AttachmentError) as caught:
                    self.prepare(*files, limits=limits)
                self.assertEqual(caught.exception.code, "attachment_limit")

    def test_cleans_names_and_rejects_blank_labels(self) -> None:
        result = self.prepare(RawUpload(r"C:\Users\ada\my notes.txt", b"hello"))
        self.assertEqual(result[0].name, "my notes.txt")

        result = self.prepare(RawUpload("../../etc/notes.txt", b"hello"))
        self.assertEqual(result[0].name, "notes.txt")

        result = self.prepare(RawUpload("\x00\tnotes.txt", b"hello"))
        self.assertEqual(result[0].name, "notes.txt")

        with self.assertRaises(attachments.AttachmentError) as caught:
            self.prepare(RawUpload("folder/", b"hello"))
        self.assertEqual(caught.exception.code, "invalid_attachment")

        with self.assertRaises(attachments.AttachmentError) as caught:
            self.prepare(RawUpload("\x00\x01 ", b"hello"))
        self.assertEqual(caught.exception.code, "invalid_attachment")

        long_name = f"{'a' * 300}.txt"
        self.assertEqual(len(self.prepare(upload(long_name, b"hello"))[0].name), 255)


@override_settings(**PROVIDER_SETTINGS)
class UploadStreamTests(ChatHttpTestCase):
    """HTTP uploads, model hydration and persistence."""

    async def test_turn_grounds_the_model_and_returns_metadata(self) -> None:
        factory = self._patch_agent(lambda messages, info: _answer("grounded"))
        user = await self.make_user("upload-owner")
        thread = await self.make_thread(user)
        client = await self.client_for(user)
        token = await self.token(client)

        files = [
            upload("notes.txt", b"invoice_total=137"),
            upload("report.docx", docx_bytes()),
            upload("book.xlsx", xlsx_bytes()),
        ]
        _body, parsed = await self.uploaded_turn(
            client, token, thread.id, "what is the invoice total?", 0, files
        )

        self.assertEqual(parsed[-1][0], "done")
        done = parsed[-1][1]
        self.assertEqual(done["reply"], "grounded")
        seen = prompt_content(factory.seen[0])
        payloads = [item for item in seen if isinstance(item, BinaryContent)]
        self.assertEqual(len(payloads), 3)
        self.assertEqual(payloads[0].data, b"invoice_total=137")
        self.assertIn(b"Quarterly summary", payloads[1].data)
        self.assertIn(b"Sheet: Totals", payloads[2].data)
        labels = [item for item in seen if isinstance(item, str)]
        self.assertIn("Attached file: notes.txt", labels)
        self.assertIn("Attached file: report.docx", labels)

        detail = await self.detail(client, thread.id)
        stored = detail["turns"][0]["attachments"]
        self.assertEqual(
            [entry["name"] for entry in stored],
            ["notes.txt", "report.docx", "book.xlsx"],
        )
        self.assertEqual(
            [entry["size"] for entry in stored], [file.size for file in files]
        )
        self.assertEqual(stored[0]["media_type"], "text/plain")
        self.assertNotIn("data", stored[0])
        self.assertEqual(done["turn"]["attachments"], stored)

        row = await sync_to_async(ChatThread.objects.get)(pk=thread.id)
        self.assertNotIn("invoice_total", row.history_json)
        self.assertEqual(len(marker_ids(service.load_history(row.history_json))), 3)

    async def test_follow_up_reuses_files_once(self) -> None:
        factory = self._patch_agent(lambda messages, info: _answer("again"))
        user = await self.make_user("follow-up-owner")
        thread = await self.make_thread(user)
        client = await self.client_for(user)
        token = await self.token(client)

        await self.uploaded_turn(
            client,
            token,
            thread.id,
            "first?",
            0,
            [upload("notes.txt", b"invoice_total=137")],
        )
        await self.completed_turn(client, token, thread.id, "and again?", 1)

        seen = prompt_content(factory.seen[1])
        payloads = [item for item in seen if isinstance(item, BinaryContent)]
        self.assertEqual([item.data for item in payloads], [b"invoice_total=137"])
        self.assertEqual(
            [item for item in seen if isinstance(item, str)].count(
                "Attached file: notes.txt"
            ),
            1,
        )

        row = await sync_to_async(ChatThread.objects.get)(pk=thread.id)
        self.assertEqual(row.history_json.count("invoice_total"), 0)
        self.assertEqual(row.history_json.count("Attached file"), 0)

    async def test_native_payload_stays_out_of_canonical_history(self) -> None:
        factory = self._patch_agent(lambda messages, info: _answer("ok"))
        user = await self.make_user("native-budget-owner")
        thread = await self.make_thread(user)
        client = await self.client_for(user)
        token = await self.token(client)

        pdf = b"%PDF-1.4\n" + b"x" * (150 * 1024)
        await self.uploaded_turn(
            client, token, thread.id, "read this", 0, [upload("chart.pdf", pdf)]
        )

        row = await sync_to_async(ChatThread.objects.get)(pk=thread.id)
        self.assertLess(len(row.history_json.encode("utf-8")), self.max_bytes())
        self.assertNotIn("JVBERi", row.history_json)
        self.assertEqual(
            [
                item.data
                for item in prompt_content(factory.seen[0])
                if isinstance(item, BinaryContent)
            ],
            [pdf],
        )

    async def test_regeneration_reuses_saved_files_once(self) -> None:
        factory = self._patch_agent(lambda messages, info: _answer("fresh"))
        user = await self.make_user("regen-owner")
        thread = await self.make_thread(user)
        client = await self.client_for(user)
        token = await self.token(client)

        await self.uploaded_turn(
            client,
            token,
            thread.id,
            "first?",
            0,
            [upload("notes.txt", b"invoice_total=137")],
        )
        await self.completed_turn(client, token, thread.id, "second?", 1)
        turns = await self.turns(client, thread.id)
        latest = turns[-1]

        _body, parsed = await self.regenerated(
            client, token, thread.id, latest["turn_id"], 2
        )

        self.assertEqual(parsed[-1][0], "done")
        seen = prompt_content(factory.seen[-1])
        payloads = [item for item in seen if isinstance(item, BinaryContent)]
        self.assertEqual([item.data for item in payloads], [b"invoice_total=137"])
        self.assertEqual(
            [item for item in seen if isinstance(item, str)].count(
                "Attached file: notes.txt"
            ),
            1,
        )

        detail = await self.detail(client, thread.id)
        self.assertEqual(detail["display"][-1]["content"], "fresh")
        self.assertEqual(
            [entry["id"] for entry in detail["turns"][0]["attachments"]],
            [entry["id"] for entry in turns[0]["attachments"]],
        )
        self.assertEqual(detail["turns"][1]["attachments"], [])

    async def test_deleting_the_latest_turn_cascades_only_its_files(self) -> None:
        self._patch_agent(lambda messages, info: _answer("ok"))
        user = await self.make_user("cascade-owner")
        thread = await self.make_thread(user)
        client = await self.client_for(user)
        token = await self.token(client)

        await self.uploaded_turn(
            client, token, thread.id, "first?", 0, [upload("a.txt", b"first-file")]
        )
        await self.uploaded_turn(
            client, token, thread.id, "second?", 1, [upload("b.txt", b"second-file")]
        )
        turns = await self.turns(client, thread.id)
        self.assertEqual(len(turns), 2)

        response = await self.delete_exchange(
            client, token, thread.id, turns[-1]["turn_id"], 2
        )
        self.assertEqual(response.status_code, 200)
        detail = response.json()["thread"]
        self.assertEqual(len(detail["turns"]), 1)
        self.assertEqual(
            [entry["name"] for entry in detail["turns"][0]["attachments"]],
            ["a.txt"],
        )

        rows = await sync_to_async(
            lambda: list(ChatAttachment.objects.values_list("name", flat=True))
        )()
        self.assertEqual(rows, ["a.txt"])

    async def test_thread_and_account_deletion_cascade_attachments(self) -> None:
        self._patch_agent(lambda messages, info: _answer("ok"))
        user = await self.make_user("deep-cascade-owner")
        thread = await self.make_thread(user)
        client = await self.client_for(user)
        token = await self.token(client)
        await self.uploaded_turn(
            client, token, thread.id, "first?", 0, [upload("a.txt", b"first-file")]
        )

        response = await client.delete(
            f"/api/threads/{thread.id}/",
            data=json.dumps({"revision": 1}),
            content_type="application/json",
            headers={"X-CSRFToken": token},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(await sync_to_async(ChatAttachment.objects.count)(), 0)

        other = await self.make_thread(user)
        await self.uploaded_turn(
            client, token, other.id, "again?", 0, [upload("b.txt", b"second-file")]
        )
        self.assertEqual(await sync_to_async(ChatAttachment.objects.count)(), 1)
        await sync_to_async(user.delete)()
        self.assertEqual(await sync_to_async(ChatAttachment.objects.count)(), 0)

    async def test_foreign_threads_cannot_read_or_resolve_attachments(self) -> None:
        self._patch_agent(lambda messages, info: _answer("ok"))
        owner = await self.make_user("file-owner")
        other = await self.make_user("file-other")
        thread = await self.make_thread(owner)
        client = await self.client_for(owner)
        token = await self.token(client)
        await self.uploaded_turn(
            client, token, thread.id, "first?", 0, [upload("a.txt", b"first-file")]
        )
        attachment_id = await sync_to_async(
            lambda: str(ChatAttachment.objects.get().id)
        )()

        foreign = await self.client_for(other)
        missing = await foreign.get(f"/api/threads/{thread.id}/")
        self.assertEqual(missing.status_code, 404)

        foreign_thread = await self.make_thread(other)
        poisoned = attachments.serialize_messages(
            [
                ModelRequest(
                    parts=[
                        UserPromptPart(
                            content=[
                                TextContent(
                                    "peek",
                                    metadata={
                                        attachments.ATTACHMENT_IDS_KEY: [attachment_id]
                                    },
                                )
                            ]
                        )
                    ]
                )
            ]
        )

        def poison() -> None:
            ChatThread.objects.filter(pk=foreign_thread.pk).update(
                history_json=poisoned
            )

        await sync_to_async(poison)()
        foreign_token = await self.token(foreign)
        response = await self.send_turn(
            foreign, foreign_token, foreign_thread.id, "peek", 0
        )
        self.assertEqual(response.status_code, 200)
        body = await read_body(response)
        await sync_to_async(response.close)()
        parsed = events(body)
        self.assertEqual(parsed[-1][0], "error")
        self.assertEqual(parsed[-1][1]["error"]["code"], "conversation_invalid")
        self.assertEqual(deltas(parsed), [])

    async def test_attachment_names_are_escaped_not_markup(self) -> None:
        self._patch_agent(lambda messages, info: _answer("ok"))
        user = await self.make_user("escape-owner")
        thread = await self.make_thread(user)
        client = await self.client_for(user)
        token = await self.token(client)
        hostile = 'quarterly <img src=x onerror="boom">.txt'

        await self.uploaded_turn(
            client, token, thread.id, "read it", 0, [upload(hostile, b"hello")]
        )

        page = await client.get(f"/chat/{thread.id}/")
        self.assertEqual(page.status_code, 200)
        html = page.content.decode()
        self.assertNotIn("<img src=x", html)
        self.assertIn("&lt;img src=x onerror=&quot;boom&quot;&gt;", html)
        detail = await self.detail(client, thread.id)
        self.assertEqual(detail["turns"][0]["attachments"][0]["name"], hostile)

    async def test_rejections_leave_no_rows(self) -> None:
        self._patch_agent(lambda messages, info: _answer("never"))
        user = await self.make_user("reject-owner")
        thread = await self.make_thread(user)
        client = await self.client_for(user)
        token = await self.token(client)

        cases = [
            [upload("bad.exe", b"nope")],
            [upload("huge.txt", b"x" * (DEFAULTS.max_file_bytes + 1))],
            [upload("empty.txt", b"")],
        ]
        for files in cases:
            with self.subTest(name=files[0].name):
                response = await self.send_files(
                    client, token, thread.id, "hello", 0, files
                )
                self.assertIn(response.status_code, {400, 413})
                self.assertIn(
                    self.error_code(response),
                    {
                        "invalid_attachment",
                        "unsupported_attachment",
                        "attachment_limit",
                    },
                )

        row = await self.fetch_row(thread.id)
        self.assertEqual(row.revision, 0)
        self.assertEqual(row.display, [])
        self.assertEqual(await sync_to_async(ChatTurn_count)(), 0)
        self.assertEqual(await sync_to_async(ChatAttachment.objects.count)(), 0)

    async def test_provider_failure_leaves_no_attachment_rows(self) -> None:
        async def explode(messages: list[ModelMessage], info: Any) -> Any:
            raise ModelHTTPError(
                status_code=500,
                model_name="offline-model",
                body={"detail": "synthetic-secret-marker"},
            )
            yield "unreachable"  # pragma: no cover

        self._patch_agent(explode)
        user = await self.make_user("provider-fail-owner")
        thread = await self.make_thread(user)
        client = await self.client_for(user)
        token = await self.token(client)

        _body, parsed = await self.uploaded_turn(
            client, token, thread.id, "hello", 0, [upload("a.txt", b"first-file")]
        )
        self.assertEqual(parsed[-1][0], "error")
        self.assertEqual(await sync_to_async(ChatAttachment.objects.count)(), 0)
        row = await self.fetch_row(thread.id)
        self.assertEqual(row.revision, 0)
        self.assertEqual(row.display, [])

    async def test_child_insert_failure_rolls_back_the_turn(self) -> None:
        self._patch_agent(lambda messages, info: _answer("ok"))
        user = await self.make_user("child-fail-owner")
        thread = await self.make_thread(user)
        client = await self.client_for(user)
        token = await self.token(client)

        with patch(
            "chat.threads.ChatAttachment.objects.bulk_create",
            side_effect=RuntimeError("insert failed"),
        ):
            response = await self.send_files(
                client, token, thread.id, "hello", 0, [upload("a.txt", b"first-file")]
            )
            with self.assertRaises(RuntimeError):
                await read_body(response)

        row = await self.fetch_row(thread.id)
        self.assertEqual(row.revision, 0)
        self.assertEqual(row.display, [])
        self.assertIsNone(row.active_run_token)
        self.assertEqual(await sync_to_async(ChatTurn_count)(), 0)
        self.assertEqual(await sync_to_async(ChatAttachment.objects.count)(), 0)


def ChatTurn_count() -> int:

    return ChatTurn.objects.count()


def _answer(text: str) -> Any:
    async def produce() -> Any:
        yield text

    return produce()


def mock_openai_turns(
    chunks: list[dict[str, Any]], *, status: int = 200, body: Any = None
):
    """A handler factory for the real OpenAI adapter over a local transport."""
    requests: list[dict[str, Any]] = []

    def handler(request: httpx2.Request) -> httpx2.Response:
        requests.append(json.loads(request.content))
        if body is not None:
            return httpx2.Response(status, json=body)
        payload = "".join(f"data: {json.dumps(chunk)}\n\n" for chunk in chunks)
        payload += "data: [DONE]\n\n"
        return httpx2.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=payload.encode(),
        )

    return handler, requests


def openai_factory(handler: Any) -> Any:
    def build(**_: Any) -> Agent[None, str]:
        client = httpx2.AsyncClient(transport=httpx2.MockTransport(handler))
        provider = OpenAICompatibleProvider(
            "mock",
            base_url="https://offline.invalid/v1",
            api_key="offline-key",
            http_client=client,
        )
        return Agent(
            OpenAIChatModel("mock-model", provider=provider),
            name="mock_chat",
            instructions=INSTRUCTIONS,
            model_settings=ModelSettings(max_retries=0),
        )

    return build


def text_chunk(text: str, finish: str | None = None) -> dict[str, Any]:
    return {
        "id": "chunk-1",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "mock-model",
        "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": finish}],
    }


def finish_chunk(reason: str, *, prompt_tokens: int = 12, completion_tokens: int = 3):
    return {
        "id": "chunk-1",
        "object": "chat.completion.chunk",
        "created": 0,
        "model": "mock-model",
        "choices": [{"index": 0, "delta": {}, "finish_reason": reason}],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


@override_settings(**PROVIDER_SETTINGS)
class NativeModelTests(ChatHttpTestCase):
    """Provider-side mapping and error families, exercised through the adapter."""

    def setUp(self) -> None:
        super().setUp()
        # The adapter checks this global before a request; the transport below
        # is a local mock, so no network call can leave the process.
        pai_models.ALLOW_MODEL_REQUESTS = True
        self.addCleanup(setattr, pai_models, "ALLOW_MODEL_REQUESTS", False)

    async def test_pdf_and_image_map_to_provider_file_parts(self) -> None:
        handler, requests = mock_openai_turns(
            [text_chunk("read it"), finish_chunk("stop")]
        )
        patcher = patch("chat.service.build_agent", openai_factory(handler))
        patcher.start()
        self.addCleanup(patcher.stop)

        user = await self.make_user("native-map-owner")
        thread = await self.make_thread(user)
        client = await self.client_for(user)
        token = await self.token(client)

        files = [
            upload("chart.png", b"\x89PNG\r\n\x1a\n" + b"p" * 32),
            upload("report.pdf", b"%PDF-1.4\n" + b"r" * 32),
        ]
        _body, parsed = await self.uploaded_turn(
            client, token, thread.id, "what is this?", 0, files
        )
        self.assertEqual(parsed[-1][0], "done")

        content = requests[0]["messages"][-1]["content"]
        parts = [item for item in content if isinstance(item, dict)]
        image = next(item for item in parts if item.get("type") == "image_url")
        document = next(item for item in parts if item.get("type") == "file")
        self.assertTrue(image["image_url"]["url"].startswith("data:image/png;base64,"))
        self.assertTrue(
            document["file"]["file_data"].startswith("data:application/pdf;base64,")
        )

    async def test_unsupported_native_input_maps_to_attachment_model_error(
        self,
    ) -> None:
        handler, _requests = mock_openai_turns(
            [],
            status=400,
            body={
                "error": {
                    "message": "the configured model cannot read images",
                    "type": "invalid_request_error",
                    "code": "invalid_request_error",
                    "param": None,
                }
            },
        )
        patcher = patch("chat.service.build_agent", openai_factory(handler))
        patcher.start()
        self.addCleanup(patcher.stop)

        user = await self.make_user("native-reject-owner")
        thread = await self.make_thread(user)
        client = await self.client_for(user)
        token = await self.token(client)
        body, parsed = await self.uploaded_turn(
            client,
            token,
            thread.id,
            "read this",
            0,
            [upload("chart.png", b"\x89PNG\r\n\x1a\n" + b"p" * 32)],
        )

        self.assertEqual(parsed[-1][0], "error")
        self.assertEqual(parsed[-1][1]["error"]["code"], "attachment_model_error")
        self.assertNotIn("invalid_request_error", body.decode())
        row = await self.fetch_row(thread.id)
        self.assertEqual(row.revision, 0)
        self.assertEqual(await sync_to_async(ChatAttachment.objects.count)(), 0)

    async def test_context_window_error_maps_to_context_limit(self) -> None:
        handler, _requests = mock_openai_turns(
            [],
            status=400,
            body={
                "error": {
                    "message": "This model's maximum context length is 8192 tokens",
                    "type": "invalid_request_error",
                    "code": "context_length_exceeded",
                    "param": None,
                }
            },
        )
        patcher = patch("chat.service.build_agent", openai_factory(handler))
        patcher.start()
        self.addCleanup(patcher.stop)

        user = await self.make_user("native-context-owner")
        thread = await self.make_thread(user)
        client = await self.client_for(user)
        token = await self.token(client)
        _body, parsed = await self.uploaded_turn(
            client,
            token,
            thread.id,
            "read this",
            0,
            [upload("report.pdf", b"%PDF-1.4\n" + b"r" * 32)],
        )

        self.assertEqual(parsed[-1][0], "error")
        self.assertEqual(parsed[-1][1]["error"]["code"], "context_limit")

    async def test_text_only_rejection_stays_a_provider_error(self) -> None:
        handler, _requests = mock_openai_turns(
            [],
            status=400,
            body={
                "error": {
                    "message": "unsupported parameter",
                    "type": "invalid_request_error",
                    "code": "invalid_request_error",
                    "param": None,
                }
            },
        )
        patcher = patch("chat.service.build_agent", openai_factory(handler))
        patcher.start()
        self.addCleanup(patcher.stop)

        user = await self.make_user("text-reject-owner")
        thread = await self.make_thread(user)
        client = await self.client_for(user)
        token = await self.token(client)
        _body, parsed = await self.turn(client, token, thread.id, "hello", 0)

        self.assertEqual(parsed[-1][0], "error")
        self.assertEqual(parsed[-1][1]["error"]["code"], "provider_error")
