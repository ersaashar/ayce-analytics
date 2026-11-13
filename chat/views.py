"""HTTP views for the authenticated, multi-thread chat workspace."""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, Final
from uuid import UUID, uuid4

from asgiref.sync import sync_to_async
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.contrib.auth.views import LoginView, LogoutView
from django.core.exceptions import (
    RequestDataTooBig,
    TooManyFieldsSent,
    TooManyFilesSent,
)
from django.http import (
    Http404,
    HttpRequest,
    HttpResponse,
    JsonResponse,
    StreamingHttpResponse,
)
from django.http.multipartparser import MultiPartParserError
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.csrf import csrf_protect

from accounts.authorization import can_use_management, capability_map
from accounts.services import self_service_memberships

from . import attachments, service, threads
from .attachments import AttachmentError, AttachmentLimits, PreparedAttachment
from .mcp_policy import AnalyticsRun, eligible_dev_department_ids, require_scope_current
from .models import ChatThread, ChatTurn

# Fixed public error envelope: code -> (HTTP status, public message).
ERRORS: dict[str, tuple[int, str]] = {
    "invalid_message": (
        400,
        "Enter a message between 1 and 2,000 characters.",
    ),
    "invalid_thread": (
        400,
        "Enter a chat name between 1 and 80 characters and a valid revision.",
    ),
    "authentication_required": (
        401,
        "Sign in to access your chats.",
    ),
    "thread_not_found": (
        404,
        "This chat is no longer available. Choose another chat or start a new one.",
    ),
    "thread_conflict": (
        409,
        "This chat changed in another tab. Reload it before trying again.",
    ),
    "thread_busy": (
        409,
        "This chat is already working on a reply. Wait for it to finish.",
    ),
    "invalid_turn": (
        400,
        "Enter a valid chat revision.",
    ),
    "turn_not_found": (
        404,
        "This exchange is no longer available. Reload the chat.",
    ),
    "turn_unavailable": (
        409,
        "Only the latest completed exchange with a saved checkpoint can be changed.",
    ),
    "provider_not_configured": (
        503,
        "Configure your LLM settings in .env.",
    ),
    "provider_error": (
        502,
        "The model provider could not complete your request. Try again.",
    ),
    "provider_timeout": (
        504,
        "The model provider timed out. Try again.",
    ),
    "provider_rate_limited": (
        429,
        "The model provider is rate limited. Try again later.",
    ),
    "mcp_error": (
        502,
        "The analytics service could not complete this request. Try again.",
    ),
    "warehouse_unavailable": (
        503,
        "Analytics queries are temporarily unavailable. You can still chat and "
        "work within your department.",
    ),
    "invalid_attachment": (
        400,
        "One or more files could not be read. Use a supported, non-empty file.",
    ),
    "unsupported_attachment": (
        400,
        "Supported files: TXT, MD, CSV, JSON, PDF, PNG, JPG, GIF, WEBP, DOCX, "
        "XLSX. Save older Word or Excel files as DOCX or XLSX.",
    ),
    "attachment_limit": (
        413,
        "Files exceed the configured attachment limits. Remove a selected "
        "file or start a new chat.",
    ),
    "attachment_model_error": (
        502,
        "The model provider could not process this chat's files. Check that "
        "the configured model supports PDF or image input and try again.",
    ),
    "context_limit": (
        400,
        "This request exceeds the configured context budget. Shorten the "
        "message, use smaller files, or start a new chat.",
    ),
    "output_limit": (
        400,
        "The model reached its output limit before producing answer text. Try "
        "a shorter request or raise LLM_MAX_OUTPUT_TOKENS.",
    ),
    "conversation_limit": (
        400,
        "This conversation has reached its limit. Start a new chat.",
    ),
    "conversation_invalid": (
        409,
        threads.CONVERSATION_INVALID_MESSAGE,
    ),
    "csrf_failed": (
        403,
        "Your page token is missing or expired. Refresh and try again.",
    ),
    "method_not_allowed": (
        405,
        "This HTTP method is not allowed.",
    ),
    "permission_denied": (
        403,
        "You do not have permission to perform this action.",
    ),
    "resource_not_found": (
        404,
        "This item is not available.",
    ),
    "invalid_management_request": (
        400,
        "Check the submitted fields and try again.",
    ),
    "administration_conflict": (
        409,
        "These settings changed. Reload them before saving.",
    ),
    "department_name_conflict": (
        409,
        "A department with this name already exists.",
    ),
    "last_admin_protected": (
        409,
        "Keep at least one active administrator.",
    ),
    "reauthentication_failed": (
        403,
        "Your current password could not be verified.",
    ),
    "invalid_password": (
        400,
        "Use a password that meets the configured password requirements.",
    ),
    "catalog_unavailable": (
        503,
        "Wren dataset discovery is unavailable. Try refreshing again.",
    ),
    "history_quarantined": (
        403,
        threads.HISTORY_QUARANTINED_MESSAGE,
    ),
    "department_access_revoked": (
        403,
        threads.DEPARTMENT_REVOKED_MESSAGE,
    ),
    "authorization_changed": (
        409,
        "Access settings changed while this reply was running. Start the request "
        "again.",
    ),
}

CONNECTED_FRAME: Final = b": connected\n\n"
SSE_CONTENT_TYPE: Final = "text/event-stream"


def error_response(code: str) -> JsonResponse:
    """Build the fixed JSON error envelope for a stable application code."""
    status, message = ERRORS[code]
    return JsonResponse(
        {"error": {"code": code, "message": message}},
        status=status,
        headers={"Cache-Control": "no-store"},
    )


def _json_response(payload: dict[str, Any], *, status: int = 200) -> JsonResponse:
    return JsonResponse(payload, status=status, headers={"Cache-Control": "no-store"})


def _sse_frame(event: str, data: dict[str, Any]) -> bytes:
    """Encode one UTF-8 SSE frame; JSON escapes keep each frame single-line."""
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event}\ndata: {payload}\n\n".encode()


def _read_json_object(
    request: HttpRequest,
    *,
    code: str,
    keys: frozenset[str] | set[str],
    optional_keys: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """Parse a strict JSON object body with an exact key set.

    ``optional_keys`` extends the accepted set; every required key must still
    be present and nothing outside the union is accepted.
    """
    content_type = request.content_type or ""
    if content_type.split(";")[0].strip().lower() != "application/json":
        raise service.ChatError(code)

    try:
        raw = request.body
    except RequestDataTooBig:
        raise service.ChatError(code) from None

    try:
        payload: Any = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise service.ChatError(code) from None

    if not isinstance(payload, dict):
        raise service.ChatError(code)
    required = set(keys)
    allowed = required | set(optional_keys)
    if not required <= set(payload) or not set(payload) <= allowed:
        raise service.ChatError(code)
    return payload


def _clean_message(value: Any) -> str:
    """Validate one message string, returning it trimmed."""
    if not isinstance(value, str):
        raise service.ChatError("invalid_message")
    if len(value) > threads.MAX_MESSAGE_LENGTH:
        raise service.ChatError("invalid_message")

    stripped = value.strip()
    if not stripped:
        raise service.ChatError("invalid_message")
    return stripped


_DECIMAL_REVISION: Final = re.compile(r"^(?:0|[1-9][0-9]*)$")

# Multipart framing, headers and field names beyond the file bytes themselves.
_MULTIPART_OVERHEAD_BYTES: Final = 1024 * 1024


@dataclass(frozen=True)
class TurnRequest:
    """One validated turn input shared by the stream worker and its commit."""

    message: str
    history_json: str
    attachments: tuple[PreparedAttachment, ...]
    stored: dict[UUID, PreparedAttachment]
    limits: AttachmentLimits
    # Current epoch for a department-bound run; ``None`` for a general chat.
    authorization_epoch: int | None = None


def _read_turn_request(request: HttpRequest) -> tuple[str, int, tuple[Any, ...]]:
    """Read one send body, returning its message, revision and uploads."""
    content_type = (request.content_type or "").split(";")[0].strip().lower()
    if content_type == "multipart/form-data":
        return _read_multipart_turn_request(request)

    payload = _read_json_object(
        request,
        code="invalid_message",
        keys={"message", "revision"},
    )
    message = _clean_message(payload["message"])
    revision = threads.valid_revision(payload["revision"])
    if revision is None:
        raise service.ChatError("invalid_message")
    return message, revision, ()


def _read_multipart_turn_request(
    request: HttpRequest,
) -> tuple[str, int, tuple[Any, ...]]:
    """Read ``message``, ``revision`` and repeated ``files`` from a form body."""
    _reject_oversized_multipart(request)
    try:
        fields = set(request.POST.keys())
        message_values = request.POST.getlist("message")
        revision_values = request.POST.getlist("revision")
        file_fields = set(request.FILES.keys())
        uploads = tuple(request.FILES.getlist("files"))
    except RequestDataTooBig:
        raise service.ChatError("attachment_limit") from None
    except TooManyFilesSent:
        raise service.ChatError("attachment_limit") from None
    except TooManyFieldsSent:
        raise service.ChatError("invalid_message") from None
    except MultiPartParserError:
        raise service.ChatError("invalid_attachment") from None

    # Exactly one scalar message and revision, and no other named fields.
    if fields != {"message", "revision"}:
        raise service.ChatError("invalid_message")
    if len(message_values) != 1 or len(revision_values) != 1:
        raise service.ChatError("invalid_message")
    if file_fields - {"files"}:
        raise service.ChatError("invalid_message")

    message = _clean_message(message_values[0])
    revision = _parse_multipart_revision(revision_values[0])
    return message, revision, uploads


def _parse_multipart_revision(raw: object) -> int:
    """Accept canonical decimal digits, then apply the shared revision rule."""
    if not isinstance(raw, str) or not _DECIMAL_REVISION.match(raw):
        raise service.ChatError("invalid_message")
    revision = threads.valid_revision(int(raw))
    if revision is None:
        raise service.ChatError("invalid_message")
    return revision


def _reject_oversized_multipart(request: HttpRequest) -> None:
    """Refuse an obviously oversized body before Django buffers it.

    A missing or spoofed length is not trusted: the real uploaded sizes are
    enforced by ``prepare_attachments``.
    """
    raw = request.META.get("CONTENT_LENGTH") or ""
    try:
        length = int(raw)
    except (TypeError, ValueError):
        return
    if length <= 0:
        return
    limits = attachments.attachment_limits()
    envelope = limits.max_files_per_turn * limits.max_file_bytes
    if length > envelope + _MULTIPART_OVERHEAD_BYTES:
        raise service.ChatError("attachment_limit")


def _preflight(
    request: HttpRequest,
    owner_id: int,
    thread_id: str,
) -> tuple[ChatThread, TurnRequest]:
    """Validate the body, ownership, configuration, and revision before streaming.

    Runs in a worker thread because it reads the database. Ownership is resolved
    before provider configuration so a foreign thread never reveals whether the
    deployment is configured, while a missing configuration still wins over a
    corrupt conversation inside an owned thread.

    Only after those checks do uploads get prepared and the thread's stored
    attachments load once, so a rejected body never parses a document and a
    single run never mixes two quota policies.
    """
    message, revision, uploads = _read_turn_request(request)

    thread = threads.get_thread(owner_id=owner_id, thread_id=thread_id)
    threads.require_thread_available(owner_id=owner_id, thread=thread)
    service.validate_provider_settings()

    if revision != thread.revision:
        raise service.ChatError("thread_conflict")

    history_json, display = threads.load_conversation(thread)
    if len(display) // 2 >= threads.MAX_TURNS:
        raise service.ChatError("conversation_limit")
    if threads.payload_bytes(history_json, display) >= threads.MAX_HISTORY_BYTES:
        raise service.ChatError("conversation_limit")

    limits = attachments.attachment_limits()
    try:
        prepared = attachments.prepare_attachments(uploads, limits=limits)
        existing_count, existing_bytes, existing_text = threads.thread_attachment_usage(
            thread.pk
        )
        attachments.check_attachment_quota(
            existing_count=existing_count,
            existing_bytes=existing_bytes,
            existing_text_bytes=existing_text,
            new=prepared,
            limits=limits,
        )
    except AttachmentError as exc:
        raise service.ChatError(exc.code) from None

    stored = threads.load_thread_attachments(owner_id=owner_id, thread_id=thread.pk)
    return thread, TurnRequest(
        message=message,
        history_json=history_json,
        attachments=prepared,
        stored=stored,
        limits=limits,
        authorization_epoch=(
            threads.current_epoch() if thread.department_id is not None else None
        ),
    )


def _method_not_allowed(allow: str) -> JsonResponse:
    response = error_response("method_not_allowed")
    response["Allow"] = allow
    return response


def _management_url(user: Any) -> str | None:
    """Return the management page URL only for principals who may use it."""
    if not can_use_management(user):
        return None
    return reverse("accounts:management")


def _show_reasoning_detail(user: Any) -> bool:
    """Detailed activity stays private to the operator's native superusers."""
    return bool(getattr(settings, "CHAT_SHOW_REASONING_DETAIL", False)) and bool(
        getattr(user, "is_superuser", False)
    )


def _size_label(size: int) -> str:
    """Render a byte count for the server-side transcript fallback."""
    if size >= 1024 * 1024:
        return f"{size / (1024 * 1024):.1f} MB"
    if size >= 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size} B"


def _fallback_transcript(detail: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Join display pairs to their turn's file labels for the noscript view.

    This is a rendering-only projection: bootstrap.display keeps its exact
    two-key shape and the database still stores only pairs.
    """
    if detail is None:
        return []
    labels: dict[int, list[dict[str, str]]] = {
        turn["turn_index"]: [
            {"name": attachment["name"], "size_label": _size_label(attachment["size"])}
            for attachment in turn["attachments"]
        ]
        for turn in detail["turns"]
    }
    transcript: list[dict[str, Any]] = []
    for index, entry in enumerate(detail["display"]):
        is_user = entry["role"] == "user"
        transcript.append(
            {
                "role": entry["role"],
                "content": entry["content"],
                "attachments": labels.get(index // 2, []) if is_user else [],
            }
        )
    return transcript


def _bootstrap_limits() -> dict[str, int]:
    """Advertise the configured application budgets, not provider capacity."""
    return {
        "max_files_per_thread": settings.CHAT_MAX_FILES_PER_THREAD,
        "max_files_per_turn": settings.CHAT_MAX_FILES_PER_TURN,
        "max_file_bytes": settings.CHAT_MAX_FILE_BYTES,
        "max_text_bytes_per_file": settings.CHAT_MAX_TEXT_BYTES_PER_FILE,
        "max_text_bytes_per_thread": settings.CHAT_MAX_TEXT_BYTES_PER_THREAD,
        "context_window": settings.LLM_CONTEXT_WINDOW,
        "max_output_tokens": settings.LLM_MAX_OUTPUT_TOKENS,
    }


def _render_workspace(
    request: HttpRequest,
    *,
    active: ChatThread | None,
) -> HttpResponse:
    """Render private HTML that is never cached and always includes a fallback."""
    user = request.user
    scope = threads.owner_scope(owner_id=user.pk)
    show_detail = _show_reasoning_detail(user)
    summaries = [
        threads.thread_summary(thread, access=threads.access_for(thread, scope=scope))
        for thread in threads.list_threads(owner_id=user.pk)
    ]
    active_detail = (
        None
        if active is None
        else threads.thread_detail(
            active,
            access=threads.access_for(active, scope=scope),
            show_reasoning_detail=show_detail,
        )
    )

    for summary in summaries:
        summary["is_active"] = active is not None and summary["id"] == str(active.id)

    transcript = _fallback_transcript(active_detail)
    page_error = (
        "" if active_detail is None else active_detail["conversation_error"] or ""
    )
    locked = (
        active_detail is not None
        and active_detail["access"] != threads.ACCESS_AVAILABLE
    )
    if active is None:
        active_title = "AYCE Analytics V2"
    elif locked:
        active_title = threads.RESTRICTED_TITLE
    else:
        active_title = active.title

    eligible_ids = eligible_dev_department_ids(user_id=user.pk)

    bootstrap = {
        "user": {
            "username": user.get_username(),
            "is_staff": user.is_staff,
        },
        "threads": [
            {key: value for key, value in summary.items() if key != "is_active"}
            for summary in summaries
        ],
        "active_thread": active_detail,
        "limits": _bootstrap_limits(),
        "show_reasoning_detail": show_detail,
        "capabilities": capability_map(user),
        "memberships": self_service_memberships(user_id=user.pk),
        "analytics_enabled": bool(eligible_ids),
        "analytics_department_ids": [
            str(department_id) for department_id in eligible_ids
        ],
        "urls": {
            "threads": reverse("chat:threads"),
            "login": reverse("login"),
            "logout": reverse("logout"),
            "management": _management_url(user),
        },
    }

    response = render(
        request,
        "chat/index.html",
        {
            "threads": summaries,
            "display": transcript,
            "page_error": page_error,
            "active_title": active_title,
            "logout_url": reverse("logout"),
            "bootstrap": bootstrap,
        },
    )
    response["Cache-Control"] = "no-store"
    return response


@login_required
def index(request: HttpRequest) -> HttpResponse:
    """Render the unselected new-chat workspace."""
    return _render_workspace(request, active=None)


@login_required
def thread_page(request: HttpRequest, thread_id: str) -> HttpResponse:
    """Render one owned thread so a refresh or link restores it exactly."""
    try:
        thread = threads.get_thread(owner_id=request.user.pk, thread_id=thread_id)
    except service.ChatError:
        raise Http404("No such chat.") from None
    return _render_workspace(request, active=thread)


@csrf_protect
def threads_collection(request: HttpRequest) -> HttpResponse:
    """List the current account's chats, or create an empty one."""
    if not request.user.is_authenticated:
        return error_response("authentication_required")

    if request.method == "GET":
        scope = threads.owner_scope(owner_id=request.user.pk)
        rows = threads.list_threads(owner_id=request.user.pk)
        return _json_response(
            {
                "threads": [
                    threads.thread_summary(
                        thread, access=threads.access_for(thread, scope=scope)
                    )
                    for thread in rows
                ]
            }
        )

    if request.method == "POST":
        try:
            payload = _read_json_object(
                request,
                code="invalid_thread",
                keys=set(),
                optional_keys=frozenset({"department_id"}),
            )
            department_id = payload.get("department_id")
            parsed: UUID | None = None
            if department_id is not None:
                if not isinstance(department_id, str):
                    raise service.ChatError("invalid_thread")
                try:
                    parsed = UUID(department_id)
                except (AttributeError, TypeError, ValueError):
                    raise service.ChatError("invalid_thread") from None
            thread = threads.create_thread(
                owner_id=request.user.pk, department_id=parsed
            )
        except service.ChatError as exc:
            return error_response(exc.code)
        detail = threads.thread_detail(
            thread, show_reasoning_detail=_show_reasoning_detail(request.user)
        )
        response = _json_response({"thread": detail}, status=201)
        response["Location"] = reverse("chat:thread_detail", args=[thread.id])
        return response

    return _method_not_allowed("GET, POST")


@csrf_protect
def thread_detail(request: HttpRequest, thread_id: str) -> HttpResponse:
    """Read, rename, or delete one owned chat."""
    if not request.user.is_authenticated:
        return error_response("authentication_required")

    owner_id = request.user.pk
    if request.method == "GET":
        try:
            thread = threads.get_thread(owner_id=owner_id, thread_id=thread_id)
        except service.ChatError as exc:
            return error_response(exc.code)
        return _json_response(
            {
                "thread": threads.thread_detail(
                    thread,
                    access=threads.thread_access(owner_id=owner_id, thread=thread),
                    show_reasoning_detail=_show_reasoning_detail(request.user),
                )
            }
        )

    if request.method == "PATCH":
        try:
            payload = _read_json_object(
                request,
                code="invalid_thread",
                keys={"title", "revision"},
            )
            title = threads.clean_title(payload["title"])
            revision = threads.valid_revision(payload["revision"])
            if title is None or revision is None:
                raise service.ChatError("invalid_thread")
            thread = threads.rename_thread(
                owner_id=owner_id,
                thread_id=thread_id,
                revision=revision,
                title=title,
            )
        except service.ChatError as exc:
            return error_response(exc.code)
        scope = threads.owner_scope(owner_id=owner_id)
        return _json_response(
            {
                "thread": threads.thread_summary(
                    thread, access=threads.access_for(thread, scope=scope)
                )
            }
        )

    if request.method == "DELETE":
        try:
            payload = _read_json_object(
                request,
                code="invalid_thread",
                keys={"revision"},
            )
            revision = threads.valid_revision(payload["revision"])
            if revision is None:
                raise service.ChatError("invalid_thread")
            threads.delete_thread(
                owner_id=owner_id,
                thread_id=thread_id,
                revision=revision,
            )
        except service.ChatError as exc:
            return error_response(exc.code)
        return _json_response({"ok": True})

    return _method_not_allowed("GET, PATCH, DELETE")


def _error_payload(code: str) -> dict[str, str]:
    return {"code": code, "message": ERRORS[code][1]}


def _activity_frame(update: service.ActivityUpdate) -> bytes:
    """Encode one public activity change: fixed enums plus optional detail."""
    return _sse_frame(
        "activity",
        {
            "turn_id": update.turn_id,
            "step": update.step,
            "truncated": update.truncated,
        },
    )


# How often a running turn re-reads its authorization state. The lease itself
# is extended on its own interval and stays valid for 300 seconds.
AUTHORIZATION_CHECK_SECONDS: Final = 1.0


class _RunWatch:
    """Authorization gate for one streaming run with bounded staleness.

    Every check reads current database state; nothing trusts the request's
    cached principal. At most one check happens per interval, so a fast delta
    stream cannot turn into a database query per frame.
    """

    def __init__(
        self,
        *,
        owner_id: int,
        thread_id: UUID,
        authorization_epoch: int | None,
        lost: asyncio.Event,
    ) -> None:
        self.owner_id = owner_id
        self.thread_id = thread_id
        self.authorization_epoch = authorization_epoch
        self.lost = lost
        self._checked = 0.0

    async def refresh(self) -> bool:
        """Re-read authorization now, flagging loss on the shared event."""
        if self.lost.is_set():
            return False
        allowed = await sync_to_async(
            threads.authorization_current, thread_sensitive=True
        )(
            owner_id=self.owner_id,
            thread_id=self.thread_id,
            authorization_epoch=self.authorization_epoch,
        )
        self._checked = time.monotonic()
        if not allowed:
            self.lost.set()
        return allowed

    async def allow(self) -> bool:
        """Whether the next public frame may still be emitted."""
        if self.lost.is_set():
            return False
        if time.monotonic() - self._checked >= AUTHORIZATION_CHECK_SECONDS:
            return await self.refresh()
        return True


async def _renew_run_lease(
    *,
    watch: _RunWatch,
    run_token: UUID,
    lost: asyncio.Event,
) -> None:
    """Extend the lease on its own timer and re-check authorization on another.

    The two cadences are independent: the lease follows the configured renewal
    interval while the authorization re-read never happens more than once per
    check interval, so a fast renewal cannot become a query per tick.
    """
    last_check = time.monotonic()
    last_renewal = time.monotonic()
    while not lost.is_set():
        renew_interval = threads.RUN_RENEW_INTERVAL_SECONDS
        now = time.monotonic()
        due = min(
            last_check + AUTHORIZATION_CHECK_SECONDS - now,
            last_renewal + renew_interval - now,
        )
        await asyncio.sleep(max(0.0, due))
        now = time.monotonic()
        if now - last_check >= AUTHORIZATION_CHECK_SECONDS:
            last_check = now
            if not await watch.refresh():
                return
        if now - last_renewal < renew_interval:
            continue
        last_renewal = now
        renewed = await sync_to_async(threads.renew_run, thread_sensitive=True)(
            owner_id=watch.owner_id,
            thread_id=watch.thread_id,
            run_token=run_token,
        )
        if not renewed:
            lost.set()
            return


async def _release_run_lease(
    *,
    owner_id: int,
    thread_id: UUID,
    run_token: UUID,
) -> None:
    """Clear this worker's lease, finishing even while a cancellation unwinds."""
    task = asyncio.ensure_future(
        sync_to_async(threads.release_run, thread_sensitive=True)(
            owner_id=owner_id,
            thread_id=thread_id,
            run_token=run_token,
        )
    )
    with contextlib.suppress(BaseException):
        await asyncio.shield(task)
    if not task.done():
        with contextlib.suppress(BaseException):
            await asyncio.wait_for(asyncio.shield(task), timeout=5)


_PENDING_RELEASES: set[asyncio.Task[None]] = set()


def _discard_release(task: asyncio.Task[None]) -> None:
    """Forget a scheduled release and mark its outcome as observed."""
    _PENDING_RELEASES.discard(task)
    with contextlib.suppress(BaseException):
        task.exception()


def _release_run_lease_sync(
    *,
    owner_id: int,
    thread_id: UUID,
    run_token: UUID,
) -> None:
    """Release a lease from a synchronous response closer.

    Django calls ``StreamingHttpResponse.close()`` under ``sync_to_async`` even
    when the async iterator never started, so the generator's own ``finally``
    never runs. A live loop can only schedule the shielded release; anywhere
    else the same token-scoped clear runs in place. The clear only touches the
    lease while this token still owns it, so a duplicate release is harmless.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        with contextlib.suppress(Exception):
            threads.release_run(
                owner_id=owner_id,
                thread_id=thread_id,
                run_token=run_token,
            )
        return
    task = loop.create_task(
        _release_run_lease(
            owner_id=owner_id,
            thread_id=thread_id,
            run_token=run_token,
        )
    )
    _PENDING_RELEASES.add(task)
    task.add_done_callback(_discard_release)


_STREAM_END: Final = object()
_LEASE_LOST: Final = object()


async def _pump_stream(
    stream: AsyncIterator[Any],
    queue: asyncio.Queue[Any],
) -> None:
    """Drain the service iterator inside one task.

    Pydantic AI tracks usage with context variables that must be entered and
    exited in the same task, so the whole native stream is consumed here rather
    than by a fresh task per `anext` call.
    """
    try:
        async for event in stream:
            queue.put_nowait(event)
    except Exception as exc:
        queue.put_nowait(exc)
    finally:
        queue.put_nowait(_STREAM_END)


async def _next_stream_event(
    queue: asyncio.Queue[Any],
    lost: asyncio.Event,
) -> Any:
    """Take the next event, giving up the moment the lease is gone."""
    getter = asyncio.ensure_future(queue.get())
    waiter = asyncio.ensure_future(lost.wait())
    try:
        done, _remaining = await asyncio.wait(
            {getter, waiter},
            return_when=asyncio.FIRST_COMPLETED,
        )
    finally:
        waiter.cancel()
        with contextlib.suppress(BaseException):
            await waiter

    if getter in done:
        return getter.result()

    getter.cancel()
    with contextlib.suppress(BaseException):
        await getter
    return _LEASE_LOST


async def _stop_run_task(task: asyncio.Task[Any] | None) -> None:
    """Cancel one background run task and drain its cancellation."""
    if task is None:
        return
    task.cancel()
    with contextlib.suppress(BaseException):
        await task


async def _stop_run_stream(
    pump: asyncio.Task[Any] | None,
    stream: AsyncIterator[Any] | None,
) -> None:
    """Stop the provider pump and close the iterator it was draining.

    Safe to call more than once, including when initialization failed before
    either object existed.
    """
    await _stop_run_task(pump)
    aclose = getattr(stream, "aclose", None)
    if aclose is not None:
        with contextlib.suppress(BaseException):
            await aclose()


def _available_done(
    *,
    owner_id: int,
    thread_id: UUID,
    turn_id: UUID,
    show_detail: bool,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Refetch one owner-scoped thread and refuse locked history before done.

    Commit has already cleared the lease. Do not call ``require_run_current``.
    """
    thread = threads.get_thread(owner_id=owner_id, thread_id=thread_id)
    access = threads.require_thread_available(owner_id=owner_id, thread=thread)
    detail = threads.thread_detail(
        thread, access=access, show_reasoning_detail=show_detail
    )
    committed_turn = next(
        (entry for entry in detail["turns"] if entry["turn_id"] == str(turn_id)),
        None,
    )
    if committed_turn is None:
        raise service.ChatError("provider_error")
    return threads.thread_summary(thread, access=access), committed_turn


async def _stream_turn(
    *,
    owner_id: int,
    snapshot: ChatThread,
    mode: str,
    turn_request: TurnRequest,
    turn_id: UUID,
    run_token: UUID,
    turn: ChatTurn | None,
    show_detail: bool = False,
) -> AsyncIterator[bytes]:
    """Stream one turn, commit it under its lease, then confirm it.

    The generated work always happens outside a transaction. The lease is
    renewed in the background while the provider and tools are silent, and it
    is released in the outer ``finally`` so a cancelled, failed, or conflicting
    run cannot leave the thread pinned. That ``finally`` spans the whole
    generator, so an initialization failure or a close right after the
    connected frame still clears the lease; a response closed before its
    iterator starts is covered by the response's synchronous closer. Prepared
    uploads travel with the request and are only written as part of a
    successful commit.

    A send run passes ``AnalyticsRun`` built only from the owner, the persisted
    thread, the captured epoch and the acquired token. Regeneration passes
    none. A queued delta that carries a scope is rechecked directly before it
    is serialized; the watcher still gates every public frame, and activity
    stays on that gate. ``done`` is emitted only after an owner-scoped refetch
    that is still available. The cleared lease is not consulted again.
    """
    completed: service.CompletedReply | None = None
    queue: asyncio.Queue[Any] = asyncio.Queue()
    analytics_run: AnalyticsRun | None = None
    stream: AsyncIterator[Any] | None = None
    pump: asyncio.Task[Any] | None = None
    renewer: asyncio.Task[Any] | None = None
    try:
        yield CONNECTED_FRAME

        # Loaded once per run, before any lease work is scheduled: the model
        # may only ever name the datasets this owner currently holds in this
        # department.
        grant_instructions = await sync_to_async(
            service.load_grant_instructions, thread_sensitive=True
        )(
            owner_id=owner_id,
            department_id=snapshot.department_id,
        )

        async def check_authorization() -> None:
            await sync_to_async(threads.require_run_current, thread_sensitive=True)(
                owner_id=owner_id,
                thread_id=snapshot.pk,
                department_id=snapshot.department_id,
                authorization_epoch=turn_request.authorization_epoch,
                run_token=run_token,
            )

        lost = asyncio.Event()
        watch = _RunWatch(
            owner_id=owner_id,
            thread_id=snapshot.pk,
            authorization_epoch=turn_request.authorization_epoch,
            lost=lost,
        )
        renewer = asyncio.ensure_future(
            _renew_run_lease(watch=watch, run_token=run_token, lost=lost)
        )
        analytics_run = (
            AnalyticsRun(
                owner_id=owner_id,
                thread_id=snapshot.pk,
                department_id=snapshot.department_id,
                authorization_epoch=turn_request.authorization_epoch,
                run_token=run_token,
            )
            if mode == "send"
            else None
        )
        stream = service.stream_answer(
            turn_request.message,
            history_json=turn_request.history_json,
            turn_id=turn_id,
            attachments=turn_request.attachments,
            stored_attachments=turn_request.stored,
            show_reasoning_detail=show_detail,
            grant_instructions=grant_instructions,
            check_authorization=check_authorization,
            analytics_run=analytics_run,
        )
        pump = asyncio.ensure_future(_pump_stream(stream, queue))
        try:
            while True:
                event = await _next_stream_event(queue, lost)
                if event is _LEASE_LOST or event is _STREAM_END:
                    break
                if isinstance(event, BaseException):
                    raise event
                if isinstance(event, service.TextDelta):
                    if event.analytics_scope is not None:
                        if analytics_run is None:
                            raise service.ChatError("authorization_changed")
                        await sync_to_async(
                            require_scope_current, thread_sensitive=True
                        )(
                            run=analytics_run,
                            scope=event.analytics_scope,
                        )
                    if not await watch.allow():
                        break
                    # Private scope stays off the wire.
                    yield _sse_frame("delta", {"text": event.text})
                    continue
                if isinstance(event, service.ActivityUpdate):
                    if not await watch.allow():
                        break
                    yield _activity_frame(event)
                    continue
                completed = event
                break
        except service.ChatError as exc:
            yield _sse_frame("error", {"error": _error_payload(exc.code)})
            return
        finally:
            await _stop_run_stream(pump, stream)
            await _stop_run_task(renewer)

        if lost.is_set():
            # The worker no longer owns this thread; nothing may be written.
            code = await sync_to_async(_lost_lease_code, thread_sensitive=True)(
                owner_id=owner_id,
                snapshot=snapshot,
                authorization_epoch=turn_request.authorization_epoch,
            )
            yield _sse_frame("error", {"error": _error_payload(code)})
            return

        if completed is None:
            yield _sse_frame("error", {"error": _error_payload("provider_error")})
            return

        try:
            if mode == "regenerate":
                if turn is None:
                    yield _sse_frame(
                        "error", {"error": _error_payload("turn_unavailable")}
                    )
                    return
                await sync_to_async(threads.commit_regeneration, thread_sensitive=True)(
                    owner_id=owner_id,
                    snapshot=snapshot,
                    turn=turn,
                    completed=completed,
                    run_token=run_token,
                    authorization_epoch=turn_request.authorization_epoch,
                )
            else:
                await sync_to_async(threads.commit_turn, thread_sensitive=True)(
                    owner_id=owner_id,
                    snapshot=snapshot,
                    message=turn_request.message,
                    completed=completed,
                    turn_id=turn_id,
                    run_token=run_token,
                    attachments=turn_request.attachments,
                    attachment_limits=turn_request.limits,
                    authorization_epoch=turn_request.authorization_epoch,
                )
            summary, committed_turn = await sync_to_async(
                _available_done, thread_sensitive=True
            )(
                owner_id=owner_id,
                thread_id=snapshot.pk,
                turn_id=turn_id,
                show_detail=show_detail,
            )
        except service.ChatError as exc:
            yield _sse_frame("error", {"error": _error_payload(exc.code)})
            return

        yield _sse_frame(
            "done",
            {
                "reply": completed.reply,
                "thread": summary,
                "turn": committed_turn,
            },
        )
    finally:
        await _stop_run_stream(pump, stream)
        await _stop_run_task(renewer)
        await _release_run_lease(
            owner_id=owner_id,
            thread_id=snapshot.pk,
            run_token=run_token,
        )


def _lost_lease_code(
    *,
    owner_id: int,
    snapshot: ChatThread,
    authorization_epoch: int | None = None,
) -> str:
    """Report why a lost lease matters without exposing the stored token."""
    return threads.failure_code(
        owner_id=owner_id,
        thread_id=snapshot.pk,
        revision=snapshot.revision,
        authorization_epoch=authorization_epoch,
    )


class _LeaseClosingStream:
    """Async SSE iterator that also frees its run lease on a sync close.

    ``StreamingHttpResponse`` only registers a synchronous closer for streaming
    content that provides ``close``; an async generator does not, so a response
    closed before (or while) its iterator runs could leave the thread pinned.
    The generator's own ``finally`` releases too, and the token-scoped clear
    makes that duplicate release harmless.
    """

    def __init__(
        self,
        iterator: AsyncIterator[bytes],
        *,
        owner_id: int,
        thread_id: UUID,
        run_token: UUID,
    ) -> None:
        self._iterator = iterator
        self._owner_id = owner_id
        self._thread_id = thread_id
        self._run_token = run_token
        self._closed = False

    def __aiter__(self) -> AsyncIterator[bytes]:
        return self._iterator

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        _release_run_lease_sync(
            owner_id=self._owner_id,
            thread_id=self._thread_id,
            run_token=self._run_token,
        )


def _sse_response(
    iterator: AsyncIterator[bytes],
    *,
    owner_id: int,
    thread_id: UUID,
    run_token: UUID,
) -> StreamingHttpResponse:
    """Build an uncached SSE response that frees the run lease when closed."""
    response = StreamingHttpResponse(
        _LeaseClosingStream(
            iterator,
            owner_id=owner_id,
            thread_id=thread_id,
            run_token=run_token,
        ),
        content_type=SSE_CONTENT_TYPE,
    )
    response["Content-Type"] = SSE_CONTENT_TYPE
    response["Cache-Control"] = "no-cache, no-store"
    response["X-Accel-Buffering"] = "no"
    return response


@csrf_protect
async def thread_messages(request: HttpRequest, thread_id: str) -> HttpResponse:
    """Stream one chat turn over SSE, committing it only when it completes."""
    user = await request.auser()
    if not user.is_authenticated:
        return error_response("authentication_required")
    if request.method != "POST":
        return _method_not_allowed("POST")

    owner_id = user.pk
    try:
        snapshot, turn_request = await sync_to_async(_preflight, thread_sensitive=True)(
            request, owner_id, thread_id
        )
        run_token = await sync_to_async(threads.acquire_run, thread_sensitive=True)(
            owner_id=owner_id,
            thread_id=snapshot.pk,
            revision=snapshot.revision,
        )
    except service.ChatError as exc:
        return error_response(exc.code)

    turn_id = uuid4()
    try:
        return _sse_response(
            _stream_turn(
                owner_id=owner_id,
                snapshot=snapshot,
                mode="send",
                turn_request=turn_request,
                turn_id=turn_id,
                run_token=run_token,
                turn=None,
                show_detail=_show_reasoning_detail(user),
            ),
            owner_id=owner_id,
            thread_id=snapshot.pk,
            run_token=run_token,
        )
    except BaseException:
        await _release_run_lease(
            owner_id=owner_id,
            thread_id=snapshot.pk,
            run_token=run_token,
        )
        raise


def _read_revision_body(request: HttpRequest) -> int:
    """Read the exact ``{revision}`` body shared by the turn endpoints."""
    payload = _read_json_object(
        request,
        code="invalid_turn",
        keys={"revision"},
    )
    revision = threads.valid_revision(payload["revision"])
    if revision is None:
        raise service.ChatError("invalid_turn")
    return revision


def _regenerate_preflight(
    request: HttpRequest,
    owner_id: int,
    thread_id: str,
    turn_id: str,
) -> tuple[ChatThread, ChatTurn, TurnRequest]:
    """Validate a regeneration from stored state only.

    The prompt and starting history come from the database, never from the
    request, so a browser cannot inject text or a checkpoint. The resubmitted
    prompt reuses that turn's already-stored files, which consumes no upload
    quota and cannot duplicate them.
    """
    revision = _read_revision_body(request)

    thread = threads.get_thread(owner_id=owner_id, thread_id=thread_id)
    threads.require_thread_available(owner_id=owner_id, thread=thread)
    service.validate_provider_settings()

    if revision != thread.revision:
        raise service.ChatError("thread_conflict")

    _history_json, display = threads.load_conversation(thread)
    turn = threads.get_latest_turn(
        owner_id=owner_id,
        snapshot=thread,
        turn_id=turn_id,
    )
    threads.require_turn_regenerable(turn)
    checkpoint = turn.pre_history_json
    if checkpoint is None or len(display) < 2:
        raise service.ChatError("turn_unavailable")
    turn_request = TurnRequest(
        message=display[-2]["content"],
        history_json=checkpoint,
        attachments=threads.turn_attachments(owner_id=owner_id, turn_id=turn.pk),
        stored=threads.load_thread_attachments(owner_id=owner_id, thread_id=thread.pk),
        limits=attachments.attachment_limits(),
        authorization_epoch=(
            threads.current_epoch() if thread.department_id is not None else None
        ),
    )
    return thread, turn, turn_request


def _delete_turn_preflight(
    request: HttpRequest,
    owner_id: int,
    thread_id: str,
    turn_id: str,
) -> ChatThread:
    """Validate a pair removal without needing a provider request."""
    revision = _read_revision_body(request)

    thread = threads.get_thread(owner_id=owner_id, thread_id=thread_id)
    threads.require_thread_available(owner_id=owner_id, thread=thread)
    if revision != thread.revision:
        raise service.ChatError("thread_conflict")

    threads.load_conversation(thread)
    threads.get_latest_turn(owner_id=owner_id, snapshot=thread, turn_id=turn_id)
    return thread


@csrf_protect
async def thread_regenerate(
    request: HttpRequest,
    thread_id: str,
    turn_id: str,
) -> HttpResponse:
    """Replace the latest answer in place, streaming the new one."""
    user = await request.auser()
    if not user.is_authenticated:
        return error_response("authentication_required")
    if request.method != "POST":
        return _method_not_allowed("POST")

    owner_id = user.pk
    try:
        snapshot, turn, turn_request = await sync_to_async(
            _regenerate_preflight, thread_sensitive=True
        )(request, owner_id, thread_id, turn_id)
        run_token = await sync_to_async(threads.acquire_run, thread_sensitive=True)(
            owner_id=owner_id,
            thread_id=snapshot.pk,
            revision=snapshot.revision,
        )
    except service.ChatError as exc:
        return error_response(exc.code)

    try:
        return _sse_response(
            _stream_turn(
                owner_id=owner_id,
                snapshot=snapshot,
                mode="regenerate",
                turn_request=turn_request,
                turn_id=turn.id,
                run_token=run_token,
                turn=turn,
                show_detail=_show_reasoning_detail(user),
            ),
            owner_id=owner_id,
            thread_id=snapshot.pk,
            run_token=run_token,
        )
    except BaseException:
        await _release_run_lease(
            owner_id=owner_id,
            thread_id=snapshot.pk,
            run_token=run_token,
        )
        raise


@csrf_protect
async def thread_turn(
    request: HttpRequest,
    thread_id: str,
    turn_id: str,
) -> HttpResponse:
    """Remove the latest completed prompt and response pair."""
    user = await request.auser()
    if not user.is_authenticated:
        return error_response("authentication_required")
    if request.method != "DELETE":
        return _method_not_allowed("DELETE")

    owner_id = user.pk
    try:
        snapshot = await sync_to_async(_delete_turn_preflight, thread_sensitive=True)(
            request, owner_id, thread_id, turn_id
        )
        committed = await sync_to_async(
            threads.delete_latest_turn, thread_sensitive=True
        )(owner_id=owner_id, snapshot=snapshot, turn_id=turn_id)
        detail = await sync_to_async(threads.thread_detail, thread_sensitive=True)(
            committed, show_reasoning_detail=_show_reasoning_detail(user)
        )
    except service.ChatError as exc:
        return error_response(exc.code)
    return _json_response({"thread": detail})


class LoginPageView(LoginView):
    """Built-in login view that never lets a private page be cached."""

    def dispatch(self, request: HttpRequest, *args: Any, **kwargs: Any) -> Any:
        response = super().dispatch(request, *args, **kwargs)
        response["Cache-Control"] = "no-store"
        return response


class LogoutPageView(LogoutView):
    """Built-in POST-only logout view with the same cache policy."""

    def dispatch(self, request: HttpRequest, *args: Any, **kwargs: Any) -> Any:
        response = super().dispatch(request, *args, **kwargs)
        response["Cache-Control"] = "no-store"
        return response


def csrf_failure(request: HttpRequest, reason: str = "") -> HttpResponse:
    """Keep fixed JSON for chat APIs and Django's page for account forms."""
    if request.path.startswith("/api/"):
        return error_response("csrf_failed")

    from django.views.csrf import csrf_failure as django_csrf_failure

    return django_csrf_failure(request, reason=reason)
