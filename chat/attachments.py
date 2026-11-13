"""Conversation-local upload preparation and attachment-aware history helpers.

Nothing here touches the ORM or the chat service: uploads become bounded
``PreparedAttachment`` values, and storage/streaming concerns stay with their
own modules. Every failure is an ``AttachmentError`` carrying one of three
fixed public codes, so parser exceptions, extracted text and raw bytes never
reach a client.
"""

from __future__ import annotations

import contextlib
import csv
import io
import re
import zipfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, time
from typing import Any, Final
from uuid import UUID, uuid4

from pydantic_ai.messages import (
    BinaryContent,
    ModelMessage,
    ModelMessagesTypeAdapter,
    ModelRequest,
    TextContent,
    UserPromptPart,
)

# Public error codes carried by ``AttachmentError``.
INVALID_ATTACHMENT: Final = "invalid_attachment"
UNSUPPORTED_ATTACHMENT: Final = "unsupported_attachment"
ATTACHMENT_LIMIT: Final = "attachment_limit"
CONVERSATION_INVALID: Final = "conversation_invalid"

# Prepared text may only ever use these media types, and the model adapter
# inlines each of them as text.
TEXT_CONTENT_MEDIA_TYPES: Final = frozenset(
    {"text/plain", "text/markdown", "text/csv", "application/json"}
)
IMAGE_CONTENT_MEDIA_TYPES: Final = frozenset(
    {"image/png", "image/jpeg", "image/gif", "image/webp"}
)
DOCUMENT_CONTENT_MEDIA_TYPES: Final = frozenset({"application/pdf"})
NATIVE_CONTENT_MEDIA_TYPES: Final = IMAGE_CONTENT_MEDIA_TYPES | (
    DOCUMENT_CONTENT_MEDIA_TYPES
)
CONTENT_MEDIA_TYPES: Final = TEXT_CONTENT_MEDIA_TYPES | NATIVE_CONTENT_MEDIA_TYPES

# Application-only marker on the leading TextContent of an attachment-bearing
# user prompt. It names stored rows; the bytes stay in their own table.
ATTACHMENT_IDS_KEY: Final = "ayce_attachment_ids"

MAX_NAME_LENGTH: Final = 255

# Fixed OOXML safety guards. These are deliberately independent of the
# adjustable upload quotas: a deployment may raise its file limits without
# widening what a crafted archive can make the parser expand.
MAX_OOXML_ENTRIES: Final = 1_000
MAX_OOXML_MEMBER_BYTES: Final = 5 * 1024 * 1024
MAX_OOXML_TOTAL_BYTES: Final = 20 * 1024 * 1024
MAX_OOXML_BLOCKS: Final = 100_000
MAX_OOXML_DEPTH: Final = 20
_SCAN_CHUNK_BYTES: Final = 64 * 1024
_SCAN_CARRY_BYTES: Final = 16

_OOXML_REQUIRED_PART: Final = {
    "docx": "word/document.xml",
    "xlsx": "xl/workbook.xml",
}
_OOXML_MACRO_MARKERS: Final = (b"vbaproject", b"macroenabled")
_CONTROL_PATTERN: Final = re.compile(r"[\x00-\x1f\x7f]")

# Extension -> (stored original media type, how the payload is prepared).
# The browser-supplied content type is never trusted.
EXTENSIONS: Final[dict[str, tuple[str, str]]] = {
    ".txt": ("text/plain", "text"),
    ".md": ("text/markdown", "text"),
    ".csv": ("text/csv", "text"),
    ".json": ("application/json", "text"),
    ".pdf": ("application/pdf", "native"),
    ".png": ("image/png", "native"),
    ".jpg": ("image/jpeg", "native"),
    ".jpeg": ("image/jpeg", "native"),
    ".gif": ("image/gif", "native"),
    ".webp": ("image/webp", "native"),
    ".docx": (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "docx",
    ),
    ".xlsx": (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "xlsx",
    ),
}

# The public allowlist text kept in step with ``EXTENSIONS``.
SUPPORTED_EXTENSIONS_TEXT: Final = (
    "TXT, MD, CSV, JSON, PDF, PNG, JPG, GIF, WEBP, DOCX, XLSX"
)


class AttachmentError(Exception):
    """A rejected upload or history reference carrying a fixed public code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class AttachmentLimits:
    """One request's immutable snapshot of the configured upload quotas."""

    max_files_per_thread: int
    max_files_per_turn: int
    max_file_bytes: int
    max_text_bytes_per_file: int
    max_text_bytes_per_thread: int


@dataclass(frozen=True)
class PreparedAttachment:
    """One validated upload.

    ``size`` describes the file the reader chose; ``data`` is the model-ready
    payload (the original bytes for native media, normalized UTF-8 text for
    documents).
    """

    id: UUID
    name: str
    media_type: str
    size: int
    content_media_type: str
    data: bytes


def attachment_limits() -> AttachmentLimits:
    """Read the configured quotas once so one request cannot mix policies."""
    from django.conf import settings

    return AttachmentLimits(
        max_files_per_thread=settings.CHAT_MAX_FILES_PER_THREAD,
        max_files_per_turn=settings.CHAT_MAX_FILES_PER_TURN,
        max_file_bytes=settings.CHAT_MAX_FILE_BYTES,
        max_text_bytes_per_file=settings.CHAT_MAX_TEXT_BYTES_PER_FILE,
        max_text_bytes_per_thread=settings.CHAT_MAX_TEXT_BYTES_PER_THREAD,
    )


def attachment_text_bytes(attachment: PreparedAttachment) -> int:
    """The prepared-text contribution of one stored attachment."""
    if attachment.content_media_type not in TEXT_CONTENT_MEDIA_TYPES:
        return 0
    return len(attachment.data)


def check_attachment_quota(
    *,
    existing_count: int,
    existing_bytes: int,
    existing_text_bytes: int,
    new: Sequence[PreparedAttachment],
    limits: AttachmentLimits,
) -> None:
    """Reject a batch that would exceed a count, byte or prepared-text quota.

    Equality is accepted; an over-quota batch is refused whole rather than
    truncated. The original-byte envelope is derived from the configured
    counts, so there is no second, competing byte policy.
    """
    if not new:
        return
    if existing_count + len(new) > limits.max_files_per_thread:
        raise AttachmentError(ATTACHMENT_LIMIT)
    if len(new) > limits.max_files_per_turn:
        raise AttachmentError(ATTACHMENT_LIMIT)

    new_bytes = sum(attachment.size for attachment in new)
    byte_envelope = limits.max_files_per_thread * limits.max_file_bytes
    if existing_bytes + new_bytes > byte_envelope:
        raise AttachmentError(ATTACHMENT_LIMIT)

    new_text = sum(attachment_text_bytes(attachment) for attachment in new)
    if existing_text_bytes + new_text > limits.max_text_bytes_per_thread:
        raise AttachmentError(ATTACHMENT_LIMIT)


def prepare_attachments(
    files: Iterable[Any],
    *,
    limits: AttachmentLimits,
) -> tuple[PreparedAttachment, ...]:
    """Validate every upload or reject the request as a whole."""
    uploads = list(files)
    if len(uploads) > limits.max_files_per_turn:
        raise AttachmentError(ATTACHMENT_LIMIT)

    prepared = [_prepare_one(upload, limits) for upload in uploads]
    total_text = sum(attachment_text_bytes(attachment) for attachment in prepared)
    if total_text > limits.max_text_bytes_per_thread:
        raise AttachmentError(ATTACHMENT_LIMIT)
    return tuple(prepared)


def _prepare_one(upload: Any, limits: AttachmentLimits) -> PreparedAttachment:
    name = clean_attachment_name(getattr(upload, "name", ""))
    extension = _extension_of(name)
    entry = EXTENSIONS.get(extension)
    if entry is None:
        raise AttachmentError(UNSUPPORTED_ATTACHMENT)
    media_type, kind = entry

    data = _read_bounded(upload, limits.max_file_bytes)
    if not data:
        raise AttachmentError(INVALID_ATTACHMENT)
    if len(data) > limits.max_file_bytes:
        raise AttachmentError(ATTACHMENT_LIMIT)

    if kind == "text":
        text = _normalize_text(_decode_utf8(data))
        payload = _bounded_text(text, limits.max_text_bytes_per_file)
        content_media_type = media_type
    elif kind in {"docx", "xlsx"}:
        text = _extract_office(data, kind=kind, limits=limits)
        payload = _bounded_text(text, limits.max_text_bytes_per_file)
        content_media_type = "text/plain"
    else:
        _check_signature(data, media_type)
        payload = data
        content_media_type = media_type

    return PreparedAttachment(
        id=uuid4(),
        name=name,
        media_type=media_type,
        size=len(data),
        content_media_type=content_media_type,
        data=payload,
    )


def _read_bounded(upload: Any, max_file_bytes: int) -> bytes:
    try:
        data = upload.read(max_file_bytes + 1)
    except Exception:
        raise AttachmentError(INVALID_ATTACHMENT) from None
    if not isinstance(data, bytes):
        raise AttachmentError(INVALID_ATTACHMENT)
    return data


def clean_attachment_name(raw: object) -> str:
    """Reduce a label to its final component and strip control characters."""
    value = raw if isinstance(raw, str) else ""
    # Windows and POSIX separators both end a path component.
    component = value.replace("\\", "/").rsplit("/", 1)[-1]
    component = _CONTROL_PATTERN.sub("", component).strip()
    if not component:
        raise AttachmentError(INVALID_ATTACHMENT)
    return component[:MAX_NAME_LENGTH]


def _extension_of(name: str) -> str:
    lowered = name.lower()
    dot = lowered.rfind(".")
    if dot <= 0:
        return ""
    return lowered[dot:]


def _decode_utf8(data: bytes) -> str:
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise AttachmentError(INVALID_ATTACHMENT) from None
    return _normalize_text(text)


def _normalize_text(text: str) -> str:
    if "\x00" in text or not text.strip():
        raise AttachmentError(INVALID_ATTACHMENT)
    return text


def _bounded_text(text: str, max_bytes: int) -> bytes:
    payload = text.encode("utf-8")
    if len(payload) > max_bytes:
        raise AttachmentError(ATTACHMENT_LIMIT)
    return payload


def _check_signature(data: bytes, media_type: str) -> None:
    """Reject a payload whose leading bytes contradict its extension."""
    checks = {
        "application/pdf": data.startswith(b"%PDF-"),
        "image/png": data.startswith(b"\x89PNG\r\n\x1a\n"),
        "image/jpeg": data.startswith(b"\xff\xd8\xff"),
        "image/gif": data.startswith((b"GIF87a", b"GIF89a")),
        "image/webp": data[:4] == b"RIFF" and data[8:12] == b"WEBP",
    }
    if not checks.get(media_type, False):
        raise AttachmentError(INVALID_ATTACHMENT)


def _extract_office(data: bytes, *, kind: str, limits: AttachmentLimits) -> str:
    _inspect_ooxml(data, kind=kind)
    budget = _ExtractionBudget(max_bytes=limits.max_text_bytes_per_file)
    try:
        if kind == "docx":
            return _docx_text(data, budget=budget)
        return _xlsx_text(data, budget=budget)
    except AttachmentError:
        raise
    except Exception:
        raise AttachmentError(INVALID_ATTACHMENT) from None


def _inspect_ooxml(data: bytes, *, kind: str) -> None:
    """Inspect the archive without extracting it to disk."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except (zipfile.BadZipFile, OSError, ValueError):
        raise AttachmentError(INVALID_ATTACHMENT) from None

    with archive:
        infos = archive.infolist()
        if len(infos) > MAX_OOXML_ENTRIES:
            raise AttachmentError(ATTACHMENT_LIMIT)

        names: set[str] = set()
        total = 0
        for info in infos:
            name = info.filename
            if info.flag_bits & 0x1:
                raise AttachmentError(INVALID_ATTACHMENT)
            if name in names:
                raise AttachmentError(INVALID_ATTACHMENT)
            names.add(name)
            if any(marker in name.lower().encode() for marker in _OOXML_MACRO_MARKERS):
                raise AttachmentError(UNSUPPORTED_ATTACHMENT)
            if info.file_size > MAX_OOXML_MEMBER_BYTES:
                raise AttachmentError(ATTACHMENT_LIMIT)
            total += info.file_size
            if total > MAX_OOXML_TOTAL_BYTES:
                raise AttachmentError(ATTACHMENT_LIMIT)

        if "[Content_Types].xml" not in names:
            raise AttachmentError(INVALID_ATTACHMENT)
        if _OOXML_REQUIRED_PART[kind] not in names:
            raise AttachmentError(INVALID_ATTACHMENT)

        _scan_members(archive, infos)


def _scan_members(archive: zipfile.ZipFile, infos: list[zipfile.ZipInfo]) -> None:
    """Bound real decompression and refuse DTD/entity and macro payloads."""
    read_total = 0
    carry = b""
    for info in infos:
        if info.is_dir():
            continue
        try:
            reader = archive.open(info)
        except Exception:
            raise AttachmentError(INVALID_ATTACHMENT) from None
        with reader:
            while True:
                try:
                    chunk = reader.read(_SCAN_CHUNK_BYTES)
                except Exception:
                    raise AttachmentError(INVALID_ATTACHMENT) from None
                if not chunk:
                    break
                read_total += len(chunk)
                if read_total > MAX_OOXML_TOTAL_BYTES:
                    raise AttachmentError(ATTACHMENT_LIMIT)
                lowered = (carry + chunk).lower()
                if b"<!doctype" in lowered or b"<!entity" in lowered:
                    raise AttachmentError(INVALID_ATTACHMENT)
                if "[content_types].xml" in info.filename.lower() and any(
                    marker in lowered for marker in _OOXML_MACRO_MARKERS
                ):
                    raise AttachmentError(UNSUPPORTED_ATTACHMENT)
                carry = lowered[-_SCAN_CARRY_BYTES:]


@dataclass
class _ExtractionBudget:
    """Bound text output and traversal for one office document."""

    max_bytes: int
    blocks: int = 0
    text_bytes: int = 0

    def visit(self, blocks: int = 1) -> None:
        self.blocks += blocks
        if self.blocks > MAX_OOXML_BLOCKS:
            raise AttachmentError(ATTACHMENT_LIMIT)

    def add_text(self, line: str) -> None:
        self.text_bytes += len(line.encode("utf-8")) + 1
        if self.text_bytes > self.max_bytes:
            raise AttachmentError(ATTACHMENT_LIMIT)


def _tsv(values: Sequence[str]) -> str:
    buffer = io.StringIO()
    csv.writer(buffer, delimiter="\t", lineterminator="").writerow(list(values))
    return buffer.getvalue()


def _docx_text(data: bytes, *, budget: _ExtractionBudget) -> str:
    from docx import Document

    document = Document(io.BytesIO(data))
    lines: list[str] = []
    _walk_docx(document, lines, budget, depth=0, include_paragraphs=True)
    text = "\n".join(line for line in lines if line.strip())
    if not text.strip():
        raise AttachmentError(INVALID_ATTACHMENT)
    return text


def _walk_docx(
    container: Any,
    lines: list[str],
    budget: _ExtractionBudget,
    *,
    depth: int,
    include_paragraphs: bool,
) -> None:
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    for block in container.iter_inner_content():
        budget.visit()
        if isinstance(block, Paragraph):
            if include_paragraphs and block.text.strip():
                line = block.text
                lines.append(line)
                budget.add_text(line)
            continue
        if isinstance(block, Table):
            _walk_docx_table(block, lines, budget, depth=depth)
            continue


def _walk_docx_table(
    table: Any,
    lines: list[str],
    budget: _ExtractionBudget,
    *,
    depth: int,
) -> None:
    if depth >= MAX_OOXML_DEPTH:
        raise AttachmentError(ATTACHMENT_LIMIT)
    for row in table.rows:
        cells = list(row.cells)
        budget.visit(len(cells))
        values = [cell.text.replace("\r\n", "\n").replace("\n", " ") for cell in cells]
        if any(value.strip() for value in values):
            line = _tsv(values)
            lines.append(line)
            budget.add_text(line)
        for cell in cells:
            # A cell's own paragraphs are already in its TSV value; recurse only
            # to reach nested tables.
            _walk_docx(
                cell,
                lines,
                budget,
                depth=depth + 1,
                include_paragraphs=False,
            )


def _xlsx_text(data: bytes, *, budget: _ExtractionBudget) -> str:
    from openpyxl import load_workbook

    workbook = load_workbook(
        io.BytesIO(data),
        read_only=True,
        data_only=False,
        keep_links=False,
    )
    try:
        lines: list[str] = []
        for sheet in workbook.worksheets:
            heading = f"Sheet: {sheet.title}"
            lines.append(heading)
            budget.add_text(heading)
            # A wrong declared dimension must not silently truncate the sheet.
            sheet.reset_dimensions()
            for row in sheet.iter_rows(values_only=True):
                values = [_cell_text(value) for value in row]
                budget.visit(len(values))
                if not any(values):
                    continue
                line = _tsv(values)
                lines.append(line)
                budget.add_text(line)
        text = "\n".join(lines)
        if not text.strip():
            raise AttachmentError(INVALID_ATTACHMENT)
        return text
    finally:
        with contextlib.suppress(Exception):
            workbook.close()


def _cell_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    return str(value)


def hydrate_user_content(
    text: str,
    attachment_ids: Sequence[UUID],
    attachments: Mapping[UUID, PreparedAttachment],
) -> list[Any]:
    """Build the model-facing prompt parts for one attachment-bearing turn.

    The leading TextContent keeps the application-only marker, so serializing a
    finished run can strip the payloads without losing which files belong to
    the prompt. The model adapter never forwards that metadata.
    """
    items: list[Any] = [
        TextContent(
            text,
            metadata={
                ATTACHMENT_IDS_KEY: [
                    str(attachment_id) for attachment_id in attachment_ids
                ]
            },
        )
    ]
    for attachment_id in attachment_ids:
        attachment = attachments.get(attachment_id)
        if attachment is None:
            raise AttachmentError(CONVERSATION_INVALID)
        items.append(f"Attached file: {attachment.name}")
        items.append(
            BinaryContent(
                attachment.data,
                media_type=_allowed_content_media_type(attachment.content_media_type),
                identifier=str(attachment.id),
            )
        )
    return items


def _allowed_content_media_type(value: object) -> str:
    if not isinstance(value, str) or value not in CONTENT_MEDIA_TYPES:
        raise AttachmentError(CONVERSATION_INVALID)
    return value


def _marker_ids(part: UserPromptPart, *, strict: bool) -> list[UUID] | None:
    """Read the attachment marker from a user prompt.

    ``None`` means the prompt carries no marker at all. Malformed or
    non-canonical shapes raise so a stored conversation cannot smuggle extra
    content into a prompt that claims to reference files.
    """
    content = part.content
    if not isinstance(content, list) or not content:
        return None
    first = content[0]
    if not isinstance(first, TextContent):
        return None
    metadata = first.metadata
    if not isinstance(metadata, dict) or ATTACHMENT_IDS_KEY not in metadata:
        return None
    if not strict:
        return []

    raw = metadata[ATTACHMENT_IDS_KEY]
    if not isinstance(raw, list) or not raw:
        raise AttachmentError(CONVERSATION_INVALID)
    if len(content) != 1:
        raise AttachmentError(CONVERSATION_INVALID)

    ids: list[UUID] = []
    for value in raw:
        if not isinstance(value, str):
            raise AttachmentError(CONVERSATION_INVALID)
        try:
            parsed = UUID(value)
        except (TypeError, ValueError):
            raise AttachmentError(CONVERSATION_INVALID) from None
        if parsed in ids:
            raise AttachmentError(CONVERSATION_INVALID)
        ids.append(parsed)
    return ids


def hydrate_history(
    messages: Sequence[ModelMessage],
    attachments: Mapping[UUID, PreparedAttachment],
    *,
    used: set[UUID] | None = None,
) -> list[ModelMessage]:
    """Return a fresh model view with stored attachment payloads resolved.

    Only ids present in ``attachments`` — the authenticated thread's own rows —
    can be resolved, so a copied marker can never reach another conversation's
    bytes. The stored messages are never mutated.
    """
    hydrated: list[ModelMessage] = []
    for message in messages:
        if not isinstance(message, ModelRequest):
            hydrated.append(message)
            continue
        parts = []
        for part in message.parts:
            if not isinstance(part, UserPromptPart):
                parts.append(part)
                continue
            ids = _marker_ids(part, strict=True)
            if ids is None:
                parts.append(part)
                continue
            text = part.content[0].content
            if used is not None:
                used.update(ids)
            parts.append(
                replace(part, content=hydrate_user_content(text, ids, attachments))
            )
        hydrated.append(replace(message, parts=parts))
    return hydrated


def dehydrate_messages(messages: Sequence[ModelMessage]) -> list[ModelMessage]:
    """Return a fresh view where marked prompts hold only their marker.

    Serializing this view keeps file bytes out of canonical history while
    leaving every unrelated part — tool calls, results, thinking, signatures,
    instructions and timestamps — untouched. Applying it twice is the same as
    applying it once.
    """
    dehydrated: list[ModelMessage] = []
    for message in messages:
        if not isinstance(message, ModelRequest):
            dehydrated.append(message)
            continue
        changed = False
        parts = []
        for part in message.parts:
            if isinstance(part, UserPromptPart):
                ids = _marker_ids(part, strict=False)
                if ids is not None:
                    parts.append(replace(part, content=[part.content[0]]))
                    changed = True
                    continue
            parts.append(part)
        dehydrated.append(replace(message, parts=parts) if changed else message)
    return dehydrated


def serialize_messages(messages: Sequence[ModelMessage]) -> str:
    """Serialize canonical history as UTF-8 text for storage."""
    return ModelMessagesTypeAdapter.dump_json(list(messages)).decode("utf-8")


def has_native_attachments(
    attachments: Iterable[PreparedAttachment],
) -> bool:
    """Whether a batch needs provider support for native PDF or image input."""
    return any(
        attachment.content_media_type in NATIVE_CONTENT_MEDIA_TYPES
        for attachment in attachments
    )
