"""Owner-scoped thread storage, validation, and serialization.

Every read and write here takes the owner id from the authenticated request, so
an unknown thread and another account's thread are indistinguishable to callers.
Writes are compare-and-swap on the row revision: a stale tab fails instead of
overwriting newer content.

Completed turns also keep a private per-turn row. ``ChatTurn.pre_history_json``
is the exact canonical history from before that exchange, which is what lets the
latest exchange be regenerated or removed without slicing history by offsets.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, Final
from uuid import UUID, uuid4

from django.conf import settings
from django.contrib.auth.models import User
from django.db import transaction
from django.db.models import Exists, F, OuterRef, Prefetch, Q, QuerySet, Sum
from django.db.models.functions import Length
from django.urls import reverse
from django.utils import timezone
from django.utils.module_loading import import_string

from accounts.authorization import eligible_dataset_assignments
from accounts.models import (
    AuthorizationState,
    Department,
    DepartmentMembership,
)
from datasets.discovery import configured_source_key
from datasets.models import DatasetCatalogState

from . import activity as activity_sidecars
from . import service
from .activity import TurnActivity
from .attachments import (
    TEXT_CONTENT_MEDIA_TYPES,
    AttachmentError,
    AttachmentLimits,
    PreparedAttachment,
    check_attachment_quota,
)
from .models import ChatAttachment, ChatThread, ChatTurn, ChatTurnDataset

MAX_MESSAGE_LENGTH: Final = 2000
MAX_TURNS: Final = 20
MAX_HISTORY_BYTES: Final = 128 * 1024
MAX_TITLE_LENGTH: Final = 80
DEFAULT_TITLE: Final = "New chat"

# A thread is exactly one of these states for its owner.
ACCESS_AVAILABLE: Final = "available"
ACCESS_QUARANTINED: Final = "quarantined"
ACCESS_DEPARTMENT_REVOKED: Final = "department_revoked"

RESTRICTED_TITLE: Final = "Restricted chat"
HISTORY_QUARANTINED_MESSAGE: Final = (
    "This chat is locked because its previous data access could not be verified."
)
DEPARTMENT_REVOKED_MESSAGE: Final = (
    "You no longer have access to this chat's department."
)
ACCESS_MESSAGES: Final = {
    ACCESS_QUARANTINED: HISTORY_QUARANTINED_MESSAGE,
    ACCESS_DEPARTMENT_REVOKED: DEPARTMENT_REVOKED_MESSAGE,
}

# Public, fixed error code for each locked state.
ACCESS_ERROR_CODES: Final = {
    ACCESS_QUARANTINED: "history_quarantined",
    ACCESS_DEPARTMENT_REVOKED: "department_access_revoked",
}

# Private checkpoints are retained for every turn of a thread, so the aggregate
# bound is the per-turn budget times the turn limit.
MAX_CHECKPOINT_BYTES: Final = MAX_TURNS * MAX_HISTORY_BYTES

# PositiveIntegerField storage; public metrics must survive a round trip.
MAX_METRIC: Final = 2_147_483_647

# A run lease keeps two tabs off one thread without holding a transaction.
RUN_LEASE_SECONDS: Final = 300
RUN_RENEW_INTERVAL_SECONDS: Final = 60

# Fixed public text for a row whose stored conversation cannot be decoded.
CONVERSATION_INVALID_MESSAGE = (
    "This chat could not be loaded. Start a new chat or delete it."
)

# Accounting keys only: the byte budget is measured with the same two-key
# payload shape the anonymous session implementation used, so thresholds keep
# their established meaning. Nothing here reads or writes a session.
_PAYLOAD_HISTORY_KEY: Final = "chat_history_json"
_PAYLOAD_DISPLAY_KEY: Final = "chat_display"


def valid_display(value: Any) -> list[dict[str, str]] | None:
    """Return display pairs when ``value`` is a well-formed transcript."""
    if not isinstance(value, list):
        return None
    if len(value) % 2 != 0:
        return None

    display: list[dict[str, str]] = []
    for index, entry in enumerate(value):
        if not isinstance(entry, dict) or set(entry) != {"role", "content"}:
            return None
        role = entry["role"]
        content = entry["content"]
        expected = "user" if index % 2 == 0 else "assistant"
        if role != expected or not isinstance(content, str):
            return None
        display.append({"role": role, "content": content})
    return display


def load_conversation(thread: ChatThread) -> tuple[str, list[dict[str, str]]]:
    """Validate one row's stored conversation, raising when it is unusable."""
    history_json = thread.history_json
    if not isinstance(history_json, str):
        raise service.ChatError("conversation_invalid")

    display = valid_display(thread.display)
    if display is None:
        raise service.ChatError("conversation_invalid")

    service.load_history(history_json)
    return history_json, display


def payload_bytes(history_json: str, display: Iterable[Any]) -> int:
    """Measure the serialized conversation payload used for the byte budget."""
    serializer = import_string(settings.SESSION_SERIALIZER)()
    payload = {
        _PAYLOAD_HISTORY_KEY: history_json,
        _PAYLOAD_DISPLAY_KEY: list(display),
    }
    return len(serializer.dumps(payload))


def valid_revision(value: Any) -> int | None:
    """Return a non-negative revision, rejecting booleans and non-integers."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if value < 0:
        return None
    return value


def clean_title(value: Any) -> str | None:
    """Validate and normalize a chat title, or return ``None`` when invalid."""
    if not isinstance(value, str):
        return None
    if len(value) > MAX_TITLE_LENGTH:
        return None
    title = value.strip()
    if not title:
        return None
    return title


def derive_title(message: str) -> str:
    """Derive a chat title from the first message without a model call."""
    collapsed = " ".join(message.split())
    if not collapsed:
        return DEFAULT_TITLE
    return collapsed[:MAX_TITLE_LENGTH]


def parse_thread_id(value: str | UUID) -> UUID:
    """Parse a thread id, treating malformed ids as absent ones."""
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (AttributeError, TypeError, ValueError):
        raise service.ChatError("thread_not_found") from None


def parse_turn_id(value: str | UUID) -> UUID:
    """Parse a turn id, treating malformed ids as absent ones."""
    if isinstance(value, UUID):
        return value
    try:
        return UUID(str(value))
    except (AttributeError, TypeError, ValueError):
        raise service.ChatError("turn_not_found") from None


def _isoformat(value: Any) -> str:
    return value.astimezone(UTC).isoformat()


def _public_timestamp(value: Any) -> str | None:
    """Serialize a stored completion time, refusing naive values."""
    if not isinstance(value, datetime) or value.tzinfo is None:
        return None
    return _isoformat(value)


def _public_metric(value: Any) -> int | None:
    """Return a stored counter that the public contract can represent."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if not 0 <= value <= MAX_METRIC:
        return None
    return value


def _public_checkpoint(value: Any) -> str | None:
    """Return a stored checkpoint only when it is a usable canonical history."""
    if not isinstance(value, str) or not value:
        return None
    if len(value.encode("utf-8")) > MAX_HISTORY_BYTES:
        return None
    try:
        service.load_history(value)
    except service.ChatError:
        return None
    return value


def _lease_active(thread: ChatThread, *, now: datetime) -> bool:
    """Whether a live run lease currently blocks turn actions."""
    expires = thread.active_run_expires_at
    token = thread.active_run_token
    if expires is None or token is None:
        return False
    return expires > now


def provider_available() -> bool:
    """Whether generation is configured, without contacting the provider."""
    try:
        service.validate_provider_settings()
    except service.ChatError:
        return False
    return True


def _response_metadata(turn: ChatTurn) -> dict[str, Any] | None:
    """Project the stored response metadata, keeping every public key."""
    completed_at = _public_timestamp(turn.completed_at)
    if completed_at is None:
        return None
    return {
        "requested_model": service.public_model_label(turn.requested_model),
        "resolved_model": service.public_model_label(turn.resolved_model),
        "completed_at": completed_at,
        "duration_ms": _public_metric(turn.duration_ms),
        "output_tokens": _public_metric(turn.output_tokens),
        "output_limit_reached": bool(turn.output_limit_reached),
    }


def _public_attachments(turn: ChatTurn) -> list[dict[str, Any]]:
    """Project one turn's stored file labels, never their payloads."""
    return [
        {
            "id": str(attachment.id),
            "name": attachment.name,
            "media_type": attachment.media_type,
            "size": attachment.size,
        }
        for attachment in turn.attachments.all()
    ]


def _stored_activity(
    turn: ChatTurn, *, show_reasoning_detail: bool = False
) -> TurnActivity | None:
    try:
        activity = activity_sidecars.valid_activity(turn.activity, completed=True)
        if activity is None or activity.get("turn_id") != str(turn.id):
            return None
        if not show_reasoning_detail:
            return activity_sidecars.strip_activity_detail(activity)
        return activity
    except Exception:
        return None


def turn_detail(
    turn: ChatTurn,
    *,
    pair_count: int,
    provider_configured: bool,
    lease_active: bool,
    provenance_bearing: bool,
    show_reasoning_detail: bool,
) -> dict[str, Any]:
    """Project one stored turn for the browser, with action hints only."""
    is_latest = turn.turn_index == pair_count - 1
    checkpoint = _public_checkpoint(turn.pre_history_json)
    changeable = is_latest and checkpoint is not None and not lease_active
    thread_id = str(turn.thread_id)
    turn_id = str(turn.id)
    return {
        "turn_id": turn_id,
        "turn_index": turn.turn_index,
        "response": _response_metadata(turn),
        "activity": _stored_activity(turn, show_reasoning_detail=show_reasoning_detail),
        "attachments": _public_attachments(turn),
        "can_regenerate": changeable and provider_configured and not provenance_bearing,
        "can_delete": changeable,
        "regenerate_url": reverse("chat:thread_regenerate", args=[thread_id, turn_id]),
        "delete_url": reverse("chat:thread_turn", args=[thread_id, turn_id]),
    }


def _turn_details(
    thread: ChatThread, *, pair_count: int, show_reasoning_detail: bool = False
) -> list[dict[str, Any]]:
    """Serialize this thread's turn records, newest features first."""
    rows = list(
        ChatTurn.objects.filter(thread_id=thread.pk, turn_index__lt=pair_count)
        .annotate(
            has_dataset_provenance=Exists(
                ChatTurnDataset.objects.filter(turn_id=OuterRef("pk"))
            )
        )
        .order_by("turn_index")
        .prefetch_related(
            Prefetch(
                "attachments",
                # Labels only: rendering a transcript never loads payloads.
                queryset=ChatAttachment.objects.only(
                    "id", "turn_id", "position", "name", "media_type", "size"
                ),
            )
        )
    )
    if not rows:
        return []
    now = timezone.now()
    lease_active = _lease_active(thread, now=now)
    provider_configured = provider_available()
    return [
        turn_detail(
            turn,
            pair_count=pair_count,
            provider_configured=provider_configured,
            lease_active=lease_active,
            provenance_bearing=bool(turn.has_dataset_provenance),
            show_reasoning_detail=show_reasoning_detail,
        )
        for turn in rows
    ]


def owner_scope(*, owner_id: int) -> tuple[bool, frozenset[UUID]]:
    """Current owner activity plus the departments they may still open."""
    active = User.objects.filter(pk=owner_id, is_active=True).exists()
    if not active:
        return False, frozenset()
    department_ids = frozenset(
        DepartmentMembership.objects.filter(
            user_id=owner_id,
            is_active=True,
            department__is_active=True,
        ).values_list("department_id", flat=True)
    )
    return True, department_ids


def _with_warehouse_access(queryset: QuerySet, *, owner_id: int) -> QuerySet:
    assignments = eligible_dataset_assignments(user_id=owner_id).filter(
        membership__department_id=OuterRef("turn__thread__department_id"),
        department_grant__dataset_id=OuterRef("dataset_id"),
    )
    source = configured_source_key()
    catalogs = DatasetCatalogState.objects.filter(
        source_key=source,
        structure_sha256=OuterRef("structure_sha256"),
    ).exclude(structure_sha256="")
    dependencies = ChatTurnDataset.objects.filter(turn__thread_id=OuterRef("pk"))
    denied = dependencies.filter(
        Q(turn__thread__department__isnull=True)
        | Q(definition_sha256="")
        | Q(structure_sha256="")
        | ~Q(definition_sha256=F("dataset__definition_sha256"))
        | ~Exists(assignments)
        | ~Exists(catalogs)
    )
    return queryset.annotate(warehouse_denied=Exists(denied))


def access_for(thread: ChatThread, *, scope: tuple[bool, frozenset[UUID]]) -> str:
    """Resolve one thread's state from current state, never cached data."""
    active, departments = scope
    if thread.quarantined:
        return ACCESS_QUARANTINED
    if not active:
        return ACCESS_DEPARTMENT_REVOKED if thread.department_id else ACCESS_AVAILABLE
    if thread.department_id is not None and thread.department_id not in departments:
        return ACCESS_DEPARTMENT_REVOKED
    denied = getattr(thread, "warehouse_denied", None)
    if denied is None:
        denied = (
            _with_warehouse_access(
                ChatThread.objects.filter(owner_id=thread.owner_id, pk=thread.pk),
                owner_id=thread.owner_id,
            )
            .values_list("warehouse_denied", flat=True)
            .first()
        )
        if denied is None:
            return ACCESS_QUARANTINED
    if denied:
        return ACCESS_QUARANTINED
    return ACCESS_AVAILABLE


def thread_access(*, owner_id: int, thread: ChatThread) -> str:
    """Single-thread form of :func:`access_for`."""
    return access_for(thread, scope=owner_scope(owner_id=owner_id))


def require_thread_available(*, owner_id: int, thread: ChatThread) -> str:
    """Return the available state or raise the matching locked-history error."""
    state = thread_access(owner_id=owner_id, thread=thread)
    if state != ACCESS_AVAILABLE:
        raise service.ChatError(ACCESS_ERROR_CODES[state])
    return state


def _thread_field(thread: ChatThread, name: str) -> Any:
    """Read a governance field that a narrow ``only()`` row may have deferred."""
    return getattr(thread, name)


def thread_summary(
    thread: ChatThread, *, access: str = ACCESS_AVAILABLE
) -> dict[str, Any]:
    """Serialize the metadata every thread response and the sidebar share."""
    locked = access != ACCESS_AVAILABLE
    department = None if locked else _thread_field(thread, "department")
    return {
        "id": str(thread.id),
        "title": RESTRICTED_TITLE if locked else thread.title,
        "revision": thread.revision,
        "created_at": _isoformat(thread.created_at),
        "updated_at": _isoformat(thread.updated_at),
        "access": access,
        "department": (
            None
            if department is None
            else {
                "id": str(department.id),
                "code": department.code,
                "name": department.name,
            }
        ),
        "page_url": reverse("chat:thread_page", args=[thread.id]),
        "detail_url": reverse("chat:thread_detail", args=[thread.id]),
        "messages_url": reverse("chat:thread_messages", args=[thread.id]),
    }


def thread_detail(
    thread: ChatThread,
    *,
    access: str = ACCESS_AVAILABLE,
    show_reasoning_detail: bool = False,
) -> dict[str, Any]:
    """Serialize a thread, surfacing corrupt or locked storage as an error."""
    detail = thread_summary(thread, access=access)
    if access != ACCESS_AVAILABLE:
        detail["display"] = []
        detail["turns"] = []
        detail["conversation_error"] = ACCESS_MESSAGES[access]
        return detail
    try:
        _history_json, display = load_conversation(thread)
    except service.ChatError:
        detail["display"] = []
        detail["turns"] = []
        detail["conversation_error"] = CONVERSATION_INVALID_MESSAGE
    else:
        detail["display"] = display
        detail["turns"] = _turn_details(
            thread,
            pair_count=len(display) // 2,
            show_reasoning_detail=show_reasoning_detail,
        )
        detail["conversation_error"] = None
    return detail


def list_threads(*, owner_id: int) -> list[ChatThread]:
    """Return one owner's thread metadata, newest update first."""
    return list(
        _with_warehouse_access(
            ChatThread.objects.filter(owner_id=owner_id), owner_id=owner_id
        )
        .select_related("department")
        .only(
            "id",
            "title",
            "revision",
            "created_at",
            "updated_at",
            "quarantined",
            "department",
            "department__id",
            "department__code",
            "department__name",
        )
    )


def _available_thread_pks(*, owner_id: int) -> QuerySet:
    """Threads whose owner is active and whose department scope still holds."""
    return (
        _with_warehouse_access(
            ChatThread.objects.filter(
                Q(department__isnull=True)
                | Q(
                    department__is_active=True,
                    department__memberships__user_id=owner_id,
                    department__memberships__is_active=True,
                ),
                quarantined=False,
                owner__is_active=True,
                owner_id=owner_id,
            ),
            owner_id=owner_id,
        )
        .filter(warehouse_denied=False)
        .values("pk")
    )


def get_thread(*, owner_id: int, thread_id: str | UUID) -> ChatThread:
    """Fetch one owner-scoped thread, hiding absence and foreign ids alike."""
    parsed = parse_thread_id(thread_id)
    thread = (
        _with_warehouse_access(
            ChatThread.objects.filter(owner_id=owner_id, pk=parsed), owner_id=owner_id
        )
        .select_related("department")
        .first()
    )
    if thread is None:
        raise service.ChatError("thread_not_found")
    return thread


def get_latest_turn(
    *,
    owner_id: int,
    snapshot: ChatThread,
    turn_id: str | UUID,
) -> ChatTurn:
    """Return the newest exchange when it is eligible to change, or raise.

    A turn qualifies only when it exists in this thread, is the final completed
    pair, and carries a decodable checkpoint. Nothing is derived or backfilled
    for legacy exchanges.
    """
    parsed = parse_turn_id(turn_id)
    turn = ChatTurn.objects.filter(
        thread_id=snapshot.pk,
        thread__owner_id=owner_id,
        pk=parsed,
    ).first()
    if turn is None:
        raise service.ChatError("turn_not_found")

    pair_count = len(snapshot.display) // 2
    if pair_count < 1 or turn.turn_index != pair_count - 1:
        raise service.ChatError("turn_unavailable")
    if _public_checkpoint(turn.pre_history_json) is None:
        raise service.ChatError("turn_unavailable")
    return turn


def create_thread(*, owner_id: int, department_id: UUID | None = None) -> ChatThread:
    """Create an empty thread owned by the authenticated account.

    A non-null department needs a current active membership: the server never
    infers a context the caller did not select.
    """
    if not User.objects.filter(pk=owner_id, is_active=True).exists():
        raise service.ChatError("authentication_required")
    if department_id is None:
        return ChatThread.objects.create(owner_id=owner_id)
    department = Department.objects.filter(pk=department_id, is_active=True).first()
    membership = (
        DepartmentMembership.objects.filter(
            user_id=owner_id,
            department_id=department_id,
            is_active=True,
            user__is_active=True,
        ).first()
        if department is not None
        else None
    )
    if department is None or membership is None:
        raise service.ChatError("resource_not_found")
    return ChatThread.objects.create(owner_id=owner_id, department=department)


def _raise_write_failure(*, owner_id: int, thread_id: UUID) -> None:
    """Distinguish a vanished row from a revision conflict after a 0-row write."""
    if ChatThread.objects.filter(owner_id=owner_id, pk=thread_id).exists():
        raise service.ChatError("thread_conflict")
    raise service.ChatError("thread_not_found")


def _thread_state(*, owner_id: int, thread_id: UUID) -> dict[str, Any] | None:
    """Read the fields a failed conditional write needs to explain itself."""
    row = (
        _with_warehouse_access(
            ChatThread.objects.filter(owner_id=owner_id, pk=thread_id),
            owner_id=owner_id,
        )
        .values(
            "revision",
            "quarantined",
            "department_id",
            "owner__is_active",
            "warehouse_denied",
        )
        .first()
    )
    return row


def _access_for_state(*, owner_id: int, row: dict[str, Any]) -> str:
    if row["quarantined"]:
        return ACCESS_QUARANTINED
    if not row["owner__is_active"]:
        return (
            ACCESS_DEPARTMENT_REVOKED
            if row["department_id"] is not None
            else ACCESS_AVAILABLE
        )
    if row["department_id"] is None:
        return ACCESS_QUARANTINED if row["warehouse_denied"] else ACCESS_AVAILABLE
    active = DepartmentMembership.objects.filter(
        user_id=owner_id,
        department_id=row["department_id"],
        is_active=True,
        department__is_active=True,
    ).exists()
    if not active:
        return ACCESS_DEPARTMENT_REVOKED
    return ACCESS_QUARANTINED if row["warehouse_denied"] else ACCESS_AVAILABLE


def current_epoch() -> int:
    """The stored authorization epoch, or zero before the row exists."""
    state = AuthorizationState.objects.filter(pk=1).values("epoch").first()
    return 0 if state is None else state["epoch"]


def _raise_conditional_failure(
    *,
    owner_id: int,
    thread_id: UUID,
    revision: int,
    authorization_epoch: int | None = None,
) -> None:
    """Map a 0-row guarded write onto its public error, in safety order."""
    if not User.objects.filter(pk=owner_id, is_active=True).exists():
        raise service.ChatError("authentication_required")
    row = _thread_state(owner_id=owner_id, thread_id=thread_id)
    if row is None:
        raise service.ChatError("thread_not_found")
    access = _access_for_state(owner_id=owner_id, row=row)
    if access != ACCESS_AVAILABLE:
        raise service.ChatError(ACCESS_ERROR_CODES[access])
    if (
        authorization_epoch is not None
        and row["department_id"] is not None
        and current_epoch() != authorization_epoch
    ):
        raise service.ChatError("authorization_changed")
    if row["revision"] != revision:
        raise service.ChatError("thread_conflict")
    raise service.ChatError("thread_busy")


def authorization_current(
    *,
    owner_id: int,
    thread_id: UUID,
    authorization_epoch: int | None,
) -> bool:
    """Whether a running turn's principal and scope are still valid."""
    if not User.objects.filter(pk=owner_id, is_active=True).exists():
        return False
    row = _thread_state(owner_id=owner_id, thread_id=thread_id)
    if row is None:
        return False
    if _access_for_state(owner_id=owner_id, row=row) != ACCESS_AVAILABLE:
        return False
    if row["department_id"] is None:
        return True
    if authorization_epoch is None:
        return False
    return current_epoch() == authorization_epoch


def require_run_current(
    *,
    owner_id: int,
    thread_id: UUID,
    department_id: UUID | None,
    authorization_epoch: int | None,
    run_token: UUID,
) -> None:
    """Reject a model round whose principal, history, scope, or lease moved.

    General chat ignores an epoch change that does not affect this thread.
    """
    if not User.objects.filter(pk=owner_id, is_active=True).exists():
        raise service.ChatError("authentication_required")
    thread = get_thread(owner_id=owner_id, thread_id=thread_id)
    access = thread_access(owner_id=owner_id, thread=thread)
    if access != ACCESS_AVAILABLE:
        raise service.ChatError(ACCESS_ERROR_CODES[access])
    bound_epoch_stale = department_id is not None and not authorization_current(
        owner_id=owner_id,
        thread_id=thread.pk,
        authorization_epoch=authorization_epoch,
    )
    lease_current = ChatThread.objects.filter(
        owner_id=owner_id,
        pk=thread.pk,
        active_run_token=run_token,
        active_run_expires_at__gt=timezone.now(),
    ).exists()
    if thread.department_id != department_id or bound_epoch_stale or not lease_current:
        raise service.ChatError("authorization_changed")


def require_turn_regenerable(turn: ChatTurn) -> None:
    """Block regeneration while a turn still carries warehouse provenance."""
    if ChatTurnDataset.objects.filter(turn_id=turn.pk).exists():
        raise service.ChatError("warehouse_unavailable")


def _lock_authorization(*, authorization_epoch: int | None, bound: bool) -> None:
    """Serialize a commit with management writes before re-checking scope."""
    AuthorizationState.objects.get_or_create(pk=1, defaults={"revision": 0, "epoch": 0})
    if bound:
        if authorization_epoch is None:
            raise service.ChatError("authorization_changed")
        locked = AuthorizationState.objects.filter(
            pk=1, epoch=authorization_epoch
        ).update(epoch=F("epoch"))
        if not locked:
            raise service.ChatError("authorization_changed")
    else:
        AuthorizationState.objects.filter(pk=1).update(epoch=F("epoch"))


def failure_code(
    *,
    owner_id: int,
    thread_id: UUID,
    revision: int,
    authorization_epoch: int | None = None,
) -> str:
    """Return the public code a failed conditional write should report."""
    try:
        _raise_conditional_failure(
            owner_id=owner_id,
            thread_id=thread_id,
            revision=revision,
            authorization_epoch=authorization_epoch,
        )
    except service.ChatError as exc:
        return exc.code
    return "thread_busy"


def _idle_lease_filter(now: datetime) -> Q:
    """Rows whose lease is absent, half-cleared, or already expired."""
    return (
        Q(active_run_token__isnull=True)
        | Q(active_run_expires_at__isnull=True)
        | Q(active_run_expires_at__lte=now)
    )


def acquire_run(
    *,
    owner_id: int,
    thread_id: str | UUID,
    revision: int,
) -> UUID:
    """Claim the generation lease for one thread at one revision."""
    parsed = parse_thread_id(thread_id)
    now = timezone.now()
    token = uuid4()
    expires = now + timedelta(seconds=RUN_LEASE_SECONDS)
    with transaction.atomic():
        updated = (
            ChatThread.objects.filter(
                owner_id=owner_id,
                pk=parsed,
                revision=revision,
                pk__in=_available_thread_pks(owner_id=owner_id),
            )
            .filter(_idle_lease_filter(now))
            .update(active_run_token=token, active_run_expires_at=expires)
        )
        if not updated:
            _raise_conditional_failure(
                owner_id=owner_id,
                thread_id=parsed,
                revision=revision,
            )
    return token


def renew_run(*, owner_id: int, thread_id: str | UUID, run_token: UUID) -> bool:
    """Extend a live lease; ``False`` means the worker no longer owns the run."""
    parsed = parse_thread_id(thread_id)
    now = timezone.now()
    renewed = ChatThread.objects.filter(
        owner_id=owner_id,
        pk=parsed,
        active_run_token=run_token,
        active_run_expires_at__gt=now,
        pk__in=_available_thread_pks(owner_id=owner_id),
    ).update(active_run_expires_at=now + timedelta(seconds=RUN_LEASE_SECONDS))
    return bool(renewed)


def release_run(*, owner_id: int, thread_id: str | UUID, run_token: UUID) -> None:
    """Clear a lease only while this token still owns it."""
    parsed = parse_thread_id(thread_id)
    ChatThread.objects.filter(
        owner_id=owner_id,
        pk=parsed,
        active_run_token=run_token,
    ).update(active_run_token=None, active_run_expires_at=None)


def rename_thread(
    *,
    owner_id: int,
    thread_id: str | UUID,
    revision: int,
    title: str,
) -> ChatThread:
    """Replace a title when the caller's revision is still current."""
    parsed = parse_thread_id(thread_id)
    with transaction.atomic():
        updated = ChatThread.objects.filter(
            owner_id=owner_id,
            pk=parsed,
            revision=revision,
            pk__in=_available_thread_pks(owner_id=owner_id),
        ).update(title=title, revision=revision + 1, updated_at=timezone.now())
        if not updated:
            _raise_conditional_failure(
                owner_id=owner_id, thread_id=parsed, revision=revision
            )
        return ChatThread.objects.get(owner_id=owner_id, pk=parsed)


def delete_thread(
    *,
    owner_id: int,
    thread_id: str | UUID,
    revision: int,
) -> None:
    """Delete a thread only when the caller's revision is still current."""
    parsed = parse_thread_id(thread_id)
    with transaction.atomic():
        deleted, _details = ChatThread.objects.filter(
            owner_id=owner_id,
            pk=parsed,
            revision=revision,
        ).delete()
        if not deleted:
            _raise_write_failure(owner_id=owner_id, thread_id=parsed)


def _checkpoint_total(thread_id: UUID) -> int:
    """Measure this thread's stored checkpoints, ignoring unusable values."""
    stored = ChatTurn.objects.filter(thread_id=thread_id).values_list(
        "pre_history_json", flat=True
    )
    return sum(len(value.encode("utf-8")) for value in stored if isinstance(value, str))


def _prepared(row: ChatAttachment) -> PreparedAttachment:
    """Project one stored row into its model-ready form."""
    return PreparedAttachment(
        id=row.id,
        name=row.name,
        media_type=row.media_type,
        size=row.size,
        content_media_type=row.content_media_type,
        data=bytes(row.data),
    )


def _attachment_usage(thread_id: UUID) -> tuple[int, int, int]:
    """Count a thread's stored files, original bytes and prepared text bytes."""
    rows = ChatAttachment.objects.filter(turn__thread_id=thread_id)
    count = rows.count()
    total_bytes = rows.aggregate(total=Sum("size"))["total"] or 0
    text_bytes = (
        rows.filter(content_media_type__in=TEXT_CONTENT_MEDIA_TYPES)
        .annotate(payload_bytes=Length("data"))
        .aggregate(total=Sum("payload_bytes"))["total"]
        or 0
    )
    return count, int(total_bytes), int(text_bytes)


def thread_attachment_usage(thread_id: UUID) -> tuple[int, int, int]:
    """Public form of the per-thread attachment accounting."""
    return _attachment_usage(thread_id)


def load_thread_attachments(
    *, owner_id: int, thread_id: UUID
) -> dict[UUID, PreparedAttachment]:
    """Load every attachment of one available thread exactly once."""
    thread = ChatThread.objects.filter(owner_id=owner_id, pk=thread_id).first()
    if thread is None:
        raise service.ChatError("thread_not_found")
    require_thread_available(owner_id=owner_id, thread=thread)
    rows = ChatAttachment.objects.filter(turn__thread_id=thread_id).only(
        "id", "name", "media_type", "size", "content_media_type", "data"
    )
    return {row.id: _prepared(row) for row in rows}


def turn_attachments(*, owner_id: int, turn_id: UUID) -> tuple[PreparedAttachment, ...]:
    """Load the attachments a single owned turn introduced, in stored order."""
    turn = (
        ChatTurn.objects.filter(
            pk=turn_id,
            thread__owner_id=owner_id,
        )
        .select_related("thread")
        .first()
    )
    if turn is None:
        raise service.ChatError("turn_not_found")
    require_thread_available(owner_id=owner_id, thread=turn.thread)
    rows = ChatAttachment.objects.filter(turn_id=turn_id)
    return tuple(_prepared(row) for row in rows)


def _turn_row(
    *,
    thread_id: UUID,
    turn_index: int,
    checkpoint: str,
    completed: service.CompletedReply,
    now: datetime,
) -> dict[str, Any]:
    """Build the validated private metadata for one completed turn."""
    try:
        activity = activity_sidecars.valid_activity(completed.activity, completed=True)
    except Exception:
        activity = None
    return {
        "thread_id": thread_id,
        "turn_index": turn_index,
        "pre_history_json": checkpoint,
        "requested_model": service.public_model_label(completed.requested_model),
        "resolved_model": service.public_model_label(completed.resolved_model),
        "completed_at": now,
        "duration_ms": _public_metric(completed.duration_ms),
        "output_tokens": _public_metric(completed.output_tokens),
        "activity": activity,
        "output_limit_reached": bool(completed.output_limit_reached),
    }


def commit_turn(
    *,
    owner_id: int,
    snapshot: ChatThread,
    message: str,
    completed: service.CompletedReply,
    turn_id: UUID,
    run_token: UUID,
    attachments: Sequence[PreparedAttachment] = (),
    attachment_limits: AttachmentLimits | None = None,
    authorization_epoch: int | None = None,
) -> ChatThread:
    """Persist one completed turn as a single conditional update.

    The candidate transcript is size-checked before any write, then history,
    display, title, revision, ``updated_at``, the new child rows and the lease
    move together, so a conflicting, oversized or over-quota turn leaves the
    previous state byte-for-byte intact. A used analytics scope is rechecked
    under the authorization lock; its provenance rows commit with the turn.
    """
    candidate_display = [
        *snapshot.display,
        {"role": "user", "content": message},
        {"role": "assistant", "content": completed.reply},
    ]
    try:
        size = payload_bytes(completed.history_json, candidate_display)
    except (TypeError, ValueError):
        raise service.ChatError("conversation_limit") from None
    if size > MAX_HISTORY_BYTES:
        raise service.ChatError("conversation_limit")

    checkpoint = snapshot.history_json
    if _public_checkpoint(checkpoint) is None:
        raise service.ChatError("conversation_invalid")

    turn_index = len(snapshot.display) // 2
    if turn_index >= MAX_TURNS:
        raise service.ChatError("conversation_limit")
    if _checkpoint_total(snapshot.pk) + len(checkpoint.encode("utf-8")) > (
        MAX_CHECKPOINT_BYTES
    ):
        raise service.ChatError("conversation_limit")

    title = snapshot.title
    if title == DEFAULT_TITLE and not snapshot.display:
        title = derive_title(message)

    now = timezone.now()
    bound = snapshot.department_id is not None
    with transaction.atomic():
        _lock_authorization(authorization_epoch=authorization_epoch, bound=bound)
        if not User.objects.filter(pk=owner_id, is_active=True).exists():
            raise service.ChatError("authentication_required")
        scope = completed.analytics_scope
        if scope is not None:
            if not scope.datasets:
                raise service.ChatError("authorization_changed")
            from .mcp_policy import AnalyticsRun, require_scope_current

            require_scope_current(
                run=AnalyticsRun(
                    owner_id=owner_id,
                    thread_id=snapshot.pk,
                    department_id=snapshot.department_id,
                    authorization_epoch=authorization_epoch,
                    run_token=run_token,
                ),
                scope=scope,
            )
        updated = ChatThread.objects.filter(
            owner_id=owner_id,
            pk=snapshot.pk,
            revision=snapshot.revision,
            active_run_token=run_token,
            active_run_expires_at__gt=now,
            pk__in=_available_thread_pks(owner_id=owner_id),
        ).update(
            history_json=completed.history_json,
            display=candidate_display,
            title=title,
            revision=snapshot.revision + 1,
            updated_at=now,
            active_run_token=None,
            active_run_expires_at=None,
        )
        if not updated:
            _raise_conditional_failure(
                owner_id=owner_id,
                thread_id=snapshot.pk,
                revision=snapshot.revision,
                authorization_epoch=authorization_epoch,
            )
        ChatTurn.objects.create(
            id=turn_id,
            **_turn_row(
                thread_id=snapshot.pk,
                turn_index=turn_index,
                checkpoint=checkpoint,
                completed=completed,
                now=now,
            ),
        )
        if scope is not None:
            ChatTurnDataset.objects.bulk_create(
                [
                    ChatTurnDataset(
                        turn_id=turn_id,
                        dataset_id=use.dataset_id,
                        definition_sha256=use.definition_sha256,
                        structure_sha256=use.structure_sha256,
                    )
                    for use in scope.datasets
                ]
            )
        if attachments:
            if attachment_limits is None:
                raise service.ChatError("attachment_limit")
            existing_count, existing_bytes, existing_text = _attachment_usage(
                snapshot.pk
            )
            try:
                check_attachment_quota(
                    existing_count=existing_count,
                    existing_bytes=existing_bytes,
                    existing_text_bytes=existing_text,
                    new=attachments,
                    limits=attachment_limits,
                )
            except AttachmentError as exc:
                raise service.ChatError(exc.code) from None
            ChatAttachment.objects.bulk_create(
                [
                    ChatAttachment(
                        # The id is already named by the stored prompt marker.
                        id=attachment.id,
                        turn_id=turn_id,
                        position=position,
                        name=attachment.name,
                        media_type=attachment.media_type,
                        content_media_type=attachment.content_media_type,
                        size=attachment.size,
                        data=attachment.data,
                    )
                    for position, attachment in enumerate(attachments)
                ]
            )
        return ChatThread.objects.get(owner_id=owner_id, pk=snapshot.pk)


def commit_regeneration(
    *,
    owner_id: int,
    snapshot: ChatThread,
    turn: ChatTurn,
    completed: service.CompletedReply,
    run_token: UUID,
    authorization_epoch: int | None = None,
) -> ChatThread:
    """Replace the latest answer in place, keeping the same turn.

    Only the final assistant content and that turn's metadata change: the user
    prompt, every earlier exchange, the title, the turn id, and its checkpoint
    stay exactly as they were. A used analytics scope is refused: regeneration
    does not write provenance.
    """
    if completed.analytics_scope is not None:
        raise service.ChatError("warehouse_unavailable")
    if len(snapshot.display) < 2 or snapshot.display[-1].get("role") != "assistant":
        raise service.ChatError("turn_unavailable")
    if turn.turn_index != len(snapshot.display) // 2 - 1:
        raise service.ChatError("turn_unavailable")

    candidate_display = [
        *snapshot.display[:-1],
        {"role": "assistant", "content": completed.reply},
    ]
    try:
        size = payload_bytes(completed.history_json, candidate_display)
    except (TypeError, ValueError):
        raise service.ChatError("conversation_limit") from None
    if size > MAX_HISTORY_BYTES:
        raise service.ChatError("conversation_limit")

    now = timezone.now()
    bound = snapshot.department_id is not None
    row = _turn_row(
        thread_id=snapshot.pk,
        turn_index=turn.turn_index,
        checkpoint=turn.pre_history_json,
        completed=completed,
        now=now,
    )
    with transaction.atomic():
        _lock_authorization(authorization_epoch=authorization_epoch, bound=bound)
        if not User.objects.filter(pk=owner_id, is_active=True).exists():
            raise service.ChatError("authentication_required")
        require_run_current(
            owner_id=owner_id,
            thread_id=snapshot.pk,
            department_id=snapshot.department_id,
            authorization_epoch=authorization_epoch,
            run_token=run_token,
        )
        require_turn_regenerable(turn)
        updated = ChatThread.objects.filter(
            owner_id=owner_id,
            pk=snapshot.pk,
            revision=snapshot.revision,
            active_run_token=run_token,
            active_run_expires_at__gt=now,
            pk__in=_available_thread_pks(owner_id=owner_id),
        ).update(
            history_json=completed.history_json,
            display=candidate_display,
            revision=snapshot.revision + 1,
            updated_at=now,
            active_run_token=None,
            active_run_expires_at=None,
        )
        if not updated:
            _raise_conditional_failure(
                owner_id=owner_id,
                thread_id=snapshot.pk,
                revision=snapshot.revision,
                authorization_epoch=authorization_epoch,
            )
        ChatTurn.objects.filter(pk=turn.pk, thread_id=snapshot.pk).update(
            requested_model=row["requested_model"],
            resolved_model=row["resolved_model"],
            completed_at=row["completed_at"],
            duration_ms=row["duration_ms"],
            output_tokens=row["output_tokens"],
            activity=row["activity"],
            output_limit_reached=row["output_limit_reached"],
        )
        return ChatThread.objects.get(owner_id=owner_id, pk=snapshot.pk)


def delete_latest_turn(
    *,
    owner_id: int,
    snapshot: ChatThread,
    turn_id: str | UUID,
) -> ChatThread:
    """Remove the newest exchange, restoring its exact pre-turn history."""
    turn = get_latest_turn(owner_id=owner_id, snapshot=snapshot, turn_id=turn_id)
    checkpoint = _public_checkpoint(turn.pre_history_json)
    if checkpoint is None:
        raise service.ChatError("turn_unavailable")

    now = timezone.now()
    with transaction.atomic():
        updated = (
            ChatThread.objects.filter(
                owner_id=owner_id,
                pk=snapshot.pk,
                revision=snapshot.revision,
                pk__in=_available_thread_pks(owner_id=owner_id),
            )
            .filter(_idle_lease_filter(now))
            .update(
                history_json=checkpoint,
                display=snapshot.display[:-2],
                revision=snapshot.revision + 1,
                updated_at=now,
                active_run_token=None,
                active_run_expires_at=None,
            )
        )
        if not updated:
            _raise_conditional_failure(
                owner_id=owner_id,
                thread_id=snapshot.pk,
                revision=snapshot.revision,
            )
        ChatTurn.objects.filter(pk=turn.pk, thread_id=snapshot.pk).delete()
        return ChatThread.objects.get(owner_id=owner_id, pk=snapshot.pk)
