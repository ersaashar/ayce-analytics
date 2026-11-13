"""Audited administration mutations.

Every public function takes ``actor_id`` and ``revision`` keyword-only and runs
one ``transaction.atomic()`` block whose first write is a conditional update of
the ``AuthorizationState`` singleton. That row is what serializes management
writes (and last-administrator checks) on SQLite, where row-level
``select_for_update`` does not exist.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC
from typing import Any, Final, Literal
from uuid import UUID

from django.contrib.auth.models import Group, Permission, User
from django.contrib.auth.password_validation import validate_password
from django.contrib.contenttypes.models import ContentType
from django.core.exceptions import ValidationError
from django.core.validators import EmailValidator, validate_slug
from django.db import IntegrityError, OperationalError, transaction
from django.db.models import F, Q

from datasets.models import Dataset, DepartmentDataset, MembershipDataset

from .authorization import (
    ManagementError,
    is_superadmin,
    require_department_lead,
    require_permission,
    require_superadmin,
    user_projection,
)
from .department_names import normalize_department_name
from .models import (
    AuthorizationAudit,
    AuthorizationState,
    Department,
    DepartmentMembership,
)
from .permissions import (
    AUDIT_ACTIONS,
    FIXED_GROUPS,
    LEAD_GROUP,
    MANAGE_DEPARTMENT_STAFF,
    MANAGE_ROLES,
    MANAGE_USERS,
    MAX_AUDIT_SNAPSHOT_BYTES,
    STAFF_GROUP,
    SUPERADMIN_GROUP,
)

MAX_STATE_VALUE: Final = 2**63 - 1
MAX_REPLACEMENT_ENTRIES: Final = 1000
MAX_USERNAME_LENGTH: Final = 150
MAX_NAME_LENGTH: Final = 150
MAX_EMAIL_LENGTH: Final = 254
MAX_DESCRIPTION_LENGTH: Final = 4000
MAX_CODE_LENGTH: Final = 64

EDITABLE_USER_FIELDS: Final = frozenset(
    {"first_name", "last_name", "email", "is_active"}
)
EDITABLE_DEPARTMENT_FIELDS: Final = frozenset({"name", "description", "is_active"})

MEMBERSHIP_ROLE_VALUES: Final = frozenset({"staff", "lead"})
ROLE_LABELS: Final = (SUPERADMIN_GROUP, LEAD_GROUP, STAFF_GROUP)

_EMAIL_VALIDATOR = EmailValidator()
_SLUG_VALIDATOR = validate_slug


@dataclass(frozen=True)
class MembershipInput:
    """One validated membership row submitted for complete replacement."""

    department_id: UUID
    membership_role: Literal["staff", "lead"]
    is_active: bool
    dataset_ids: tuple[UUID, ...]


@dataclass(frozen=True)
class DepartmentMemberInput:
    """One validated department roster row submitted for replacement."""

    user_id: int
    membership_role: Literal["staff", "lead"]
    is_active: bool
    dataset_ids: tuple[UUID, ...]


@dataclass(frozen=True)
class MutationState:
    """Fresh principal plus the state values a mutation produced."""

    actor: User
    revision: int
    epoch: int


@dataclass(frozen=True)
class _RosterRow:
    """Internal, already validated membership replacement row."""

    user_id: int
    department_id: UUID
    membership_role: str
    is_active: bool
    dataset_ids: tuple[UUID, ...]


def valid_state_value(value: Any) -> int | None:
    """Return a revision/epoch the server can store, rejecting booleans."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if not 0 <= value <= MAX_STATE_VALUE:
        return None
    return value


def _lock_conflict(error: OperationalError) -> bool:
    """Whether a SQLite writer reported contention rather than a real fault."""
    message = str(error).lower()
    return "locked" in message or "busy" in message


@contextmanager
def mutation(
    *, actor_id: int, revision: int, bump_epoch: bool = False
) -> Iterator[MutationState]:
    """Serialize one administration write on the authorization singleton."""
    checked = valid_state_value(revision)
    if checked is None:
        raise ManagementError("invalid_management_request")
    try:
        with transaction.atomic():
            AuthorizationState.objects.get_or_create(
                pk=1, defaults={"revision": 0, "epoch": 0}
            )
            update: dict[str, Any] = {"revision": F("revision") + 1}
            if bump_epoch:
                update["epoch"] = F("epoch") + 1
            updated = AuthorizationState.objects.filter(pk=1, revision=checked).update(
                **update
            )
            if not updated:
                raise ManagementError("administration_conflict")
            state = AuthorizationState.objects.get(pk=1)
            actor = User.objects.filter(pk=actor_id).first()
            if actor is None or not actor.is_active:
                raise ManagementError("permission_denied")
            yield MutationState(actor=actor, revision=state.revision, epoch=state.epoch)
    except OperationalError as error:
        if _lock_conflict(error):
            raise ManagementError("administration_conflict") from None
        raise


def current_state() -> dict[str, int]:
    """Return the current administration revision and authorization epoch."""
    state = AuthorizationState.objects.get_or_create(
        pk=1, defaults={"revision": 0, "epoch": 0}
    )[0]
    return {"revision": state.revision, "epoch": state.epoch}


def _encode_snapshot(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"), default=str
    ).encode("utf-8")


def _bounded_snapshot(value: Mapping[str, Any]) -> dict[str, Any]:
    """Keep one audit document under the cap without breaking its JSON shape."""
    document = dict(value)
    if len(_encode_snapshot(document)) <= MAX_AUDIT_SNAPSHOT_BYTES:
        return document

    trimmed = dict(document)
    for key, item in list(trimmed.items()):
        if not isinstance(item, list) or len(item) <= 1:
            continue
        original = len(item)
        keep = item[: original // 2]
        while keep and len(_encode_snapshot({**trimmed, key: keep})) > (
            MAX_AUDIT_SNAPSHOT_BYTES
        ):
            keep = keep[: len(keep) // 2]
        trimmed[key] = keep
        trimmed[f"{key}_omitted"] = original - len(keep)

    if len(_encode_snapshot(trimmed)) > MAX_AUDIT_SNAPSHOT_BYTES:
        return {"truncated": True, "bytes": len(_encode_snapshot(document))}
    return trimmed


def record_audit(
    *,
    actor: User | None,
    action: str,
    target_type: str,
    target_id: str,
    allowed: bool = True,
    reason_code: str = "accepted",
    before: Mapping[str, Any] | None = None,
    after: Mapping[str, Any] | None = None,
) -> AuthorizationAudit:
    """Append one bounded audit record inside the caller's transaction."""
    if action not in AUDIT_ACTIONS:
        raise ValueError(f"unregistered audit action: {action}")
    return AuthorizationAudit.objects.create(
        actor=actor,
        action=action,
        target_type=target_type,
        target_id=str(target_id)[:64],
        allowed=allowed,
        reason_code=reason_code[:64],
        before=_bounded_snapshot(before or {}),
        after=_bounded_snapshot(after or {}),
    )


def record_denial(*, actor_id: int, action: str, reason_code: str) -> None:
    """Append one authenticated denial after its transaction rolled back."""
    actor = User.objects.filter(pk=actor_id, is_active=True).first()
    if actor is None:
        return
    record_audit(
        actor=actor,
        action="authorization.denied",
        target_type="operation",
        target_id=action,
        allowed=False,
        reason_code=reason_code,
    )


def clean_text(value: Any, *, maximum: int, allow_blank: bool = True) -> str:
    if not isinstance(value, str):
        raise ManagementError("invalid_management_request")
    text = value.strip()
    if not allow_blank and not text:
        raise ManagementError("invalid_management_request")
    if len(text) > maximum:
        raise ManagementError("invalid_management_request")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in text):
        raise ManagementError("invalid_management_request")
    return text


def _clean_username(value: Any) -> str:
    if not isinstance(value, str):
        raise ManagementError("invalid_management_request")
    username = value.strip()
    if not username or len(username) > MAX_USERNAME_LENGTH:
        raise ManagementError("invalid_management_request")
    try:
        User._meta.get_field("username").run_validators(username)
    except ValidationError:
        raise ManagementError("invalid_management_request") from None
    if User.objects.filter(username__iexact=username).exists():
        raise ManagementError("invalid_management_request")
    return username


def _clean_email(value: Any) -> str:
    email = clean_text(value, maximum=MAX_EMAIL_LENGTH)
    if not email:
        return ""
    try:
        _EMAIL_VALIDATOR(email)
    except ValidationError:
        raise ManagementError("invalid_management_request") from None
    return email


def _clean_password(value: Any, *, candidate: User) -> str:
    if not isinstance(value, str) or not value:
        raise ManagementError("invalid_password")
    try:
        validate_password(value, user=candidate)
    except ValidationError:
        raise ManagementError("invalid_password") from None
    return value


def _clean_bool(value: Any) -> bool:
    if not isinstance(value, bool):
        raise ManagementError("invalid_management_request")
    return value


def clean_slug(value: Any, *, maximum: int = MAX_CODE_LENGTH) -> str:
    if not isinstance(value, str):
        raise ManagementError("invalid_management_request")
    code = value.strip()
    if not code or len(code) > maximum:
        raise ManagementError("invalid_management_request")
    try:
        _SLUG_VALIDATOR(code)
    except ValidationError:
        raise ManagementError("invalid_management_request") from None
    return code


def clean_uuid(value: Any) -> UUID:
    if isinstance(value, UUID):
        return value
    if not isinstance(value, str):
        raise ManagementError("invalid_management_request")
    try:
        return UUID(value)
    except (AttributeError, TypeError, ValueError):
        raise ManagementError("invalid_management_request") from None


def unique_values(values: Sequence[Any]) -> None:
    if len(set(values)) != len(values):
        raise ManagementError("invalid_management_request")


def _group(name: str) -> Group:
    group, _created = Group.objects.get_or_create(name=name)
    return group


def _permission_model(app_label: str) -> type:
    """The model that owns a fixed permission, used for its content type."""
    if app_label == "chat":
        from chat.models import ChatThread

        return ChatThread
    if app_label == "datasets":
        return Dataset
    return AuthorizationState


def ensure_fixed_roles() -> int:
    """Ensure the three fixed groups carry exactly their declared permissions."""
    repaired = 0
    for name, permissions in FIXED_GROUPS.items():
        group, created = Group.objects.get_or_create(name=name)
        if created:
            repaired += 1
        desired: set[int] = set()
        for label in permissions:
            app_label, codename = label.split(".", 1)
            permission, _ = Permission.objects.get_or_create(
                content_type=ContentType.objects.get_for_model(
                    _permission_model(app_label)
                ),
                codename=codename,
                defaults={"name": f"Can {codename.replace('_', ' ')}"},
            )
            desired.add(permission.pk)
        current = set(group.permissions.values_list("pk", flat=True))
        if current != desired:
            group.permissions.set(sorted(desired))
            repaired += 1
    return repaired


def _in_superadmin_group(user: User) -> bool:
    return user.groups.filter(name=SUPERADMIN_GROUP).exists()


def is_privileged_account(user: User) -> bool:
    """Whether the account holds global administration authority."""
    return bool(user.is_superuser or _in_superadmin_group(user))


def effective_admin_count(*, exclude_user_id: int | None = None) -> int:
    """Count active accounts that could still administer the application."""
    native = User.objects.filter(is_active=True, is_superuser=True)
    delegated = User.objects.filter(
        is_active=True, groups__name=SUPERADMIN_GROUP
    ).distinct()
    if exclude_user_id is not None:
        native = native.exclude(pk=exclude_user_id)
        delegated = delegated.exclude(pk=exclude_user_id)
    return native.count() + delegated.count()


def _guard_last_admin(*, target: User) -> None:
    """Refuse an action that would leave no active administrator."""
    if effective_admin_count(exclude_user_id=target.pk) == 0:
        raise ManagementError("last_admin_protected")


def _sync_lead_group(*, user_id: int) -> None:
    """Project active Lead membership onto the fixed Lead group."""
    holds_lead = DepartmentMembership.objects.filter(
        user_id=user_id,
        membership_role="lead",
        is_active=True,
        department__is_active=True,
        user__is_active=True,
    ).exists()
    user = User.objects.filter(pk=user_id).first()
    if user is None:
        return
    group = _group(LEAD_GROUP)
    if holds_lead:
        user.groups.add(group)
    else:
        user.groups.remove(group)


def _isoformat(value: Any) -> str:
    return value.astimezone(UTC).isoformat()


def user_payload(user: User) -> dict[str, Any]:
    """Safe account view: never a password, hash, flag or permission name."""
    role_names = set(user.groups.values_list("name", flat=True))
    rows = list(
        DepartmentMembership.objects.filter(user_id=user.pk)
        .select_related("department")
        .order_by("department__code", "id")
    )
    payload = user_projection(user)
    payload["email"] = user.email or ""
    payload["is_active"] = user.is_active
    payload["roles"] = [label for label in ROLE_LABELS if label in role_names]
    payload["memberships"] = [
        {
            "department": {
                "id": str(row.department_id),
                "code": row.department.code,
                "name": row.department.name,
            },
            "membership_role": row.membership_role,
            "is_active": row.is_active,
        }
        for row in rows
        if row.is_active
    ]
    return payload


def department_payload(department: Department) -> dict[str, Any]:
    from datasets.services import dataset_availability

    unavailable = sum(
        dataset_availability(grant.dataset, grant=grant) != "available"
        for grant in department.dataset_grants.select_related("dataset")
    )
    return {
        "id": str(department.id),
        "code": department.code,
        "name": department.name,
        "description": department.description,
        "is_active": department.is_active,
        "unavailable_dataset_count": unavailable,
        "created_at": _isoformat(department.created_at),
        "updated_at": _isoformat(department.updated_at),
    }


def _dataset_ids(*, membership: DepartmentMembership) -> list[str]:
    return [
        str(value)
        for value in MembershipDataset.objects.filter(
            membership=membership,
        )
        .order_by("department_grant__dataset__code")
        .values_list("department_grant__dataset_id", flat=True)
    ]


def membership_rows(*, user_id: int) -> list[dict[str, Any]]:
    """Every stored membership of one account with its explicit subset."""
    rows = (
        DepartmentMembership.objects.filter(user_id=user_id)
        .select_related("department")
        .order_by("department__code", "id")
    )
    return [
        {
            "department": {
                "id": str(row.department_id),
                "code": row.department.code,
                "name": row.department.name,
            },
            "membership_role": row.membership_role,
            "is_active": row.is_active,
            "dataset_ids": [
                str(value)
                for value in MembershipDataset.objects.filter(
                    membership=row
                ).values_list("department_grant__dataset_id", flat=True)
            ],
        }
        for row in rows
    ]


def self_service_memberships(*, user_id: int) -> list[dict[str, Any]]:
    """Active memberships of an active user, with their effective datasets."""
    from .authorization import active_dataset_queryset

    rows = (
        DepartmentMembership.objects.filter(
            user_id=user_id,
            is_active=True,
            department__is_active=True,
            user__is_active=True,
        )
        .select_related("department")
        .order_by("department__code", "id")
    )
    return [
        {
            "id": str(row.pk),
            "department": {
                "id": str(row.department_id),
                "code": row.department.code,
                "name": row.department.name,
            },
            "membership_role": row.membership_role,
            "dataset_ids": [
                str(value)
                for value in active_dataset_queryset(
                    user_id=user_id, department_id=row.department_id
                ).values_list("id", flat=True)
            ],
        }
        for row in rows
    ]


def roster_members(*, department_id: UUID) -> list[dict[str, Any]]:
    rows = (
        DepartmentMembership.objects.filter(department_id=department_id)
        .select_related("user")
        .order_by("user__username", "id")
    )
    return [
        {
            "user": user_projection(row.user),
            "membership_role": row.membership_role,
            "is_active": row.is_active,
            "dataset_ids": _dataset_ids(membership=row),
        }
        for row in rows
    ]


def audit_payload(record: AuthorizationAudit) -> dict[str, Any]:
    return {
        "id": str(record.pk),
        "action": record.action,
        "actor": record.actor.get_username() if record.actor is not None else None,
        "target_type": record.target_type,
        "target_id": record.target_id,
        "allowed": record.allowed,
        "reason_code": record.reason_code,
        "before": record.before,
        "after": record.after,
        "created_at": _isoformat(record.created_at),
    }


def create_user(
    *,
    actor_id: int,
    revision: int,
    username: str,
    first_name: str,
    last_name: str,
    email: str,
    password: str,
) -> dict[str, Any]:
    """Create one account with the Staff base role."""
    with mutation(actor_id=actor_id, revision=revision, bump_epoch=True) as state:
        require_superadmin(actor=state.actor)
        clean_username = _clean_username(username)
        clean_first = clean_text(first_name, maximum=MAX_NAME_LENGTH)
        clean_last = clean_text(last_name, maximum=MAX_NAME_LENGTH)
        clean_email = _clean_email(email)
        candidate = User(username=clean_username, email=clean_email)
        clean_password = _clean_password(password, candidate=candidate)

        user = User.objects.create_user(
            username=clean_username,
            first_name=clean_first,
            last_name=clean_last,
            email=clean_email,
            password=clean_password,
        )
        user.groups.add(_group(STAFF_GROUP))
        record_audit(
            actor=state.actor,
            action="user.created",
            target_type="user",
            target_id=str(user.pk),
            after={"username": user.username, "roles": [STAFF_GROUP]},
        )
        payload = user_payload(user)
        payload["revision"] = state.revision
        payload["epoch"] = state.epoch
        return payload


def update_user(
    *,
    actor_id: int,
    revision: int,
    user_id: int,
    changes: Mapping[str, Any],
) -> dict[str, Any]:
    """Apply the whitelisted account fields to one existing account."""
    unknown = set(changes) - EDITABLE_USER_FIELDS
    if not changes or unknown:
        raise ManagementError("invalid_management_request")
    with mutation(
        actor_id=actor_id, revision=revision, bump_epoch="is_active" in changes
    ) as state:
        require_superadmin(actor=state.actor)
        target = User.objects.filter(pk=user_id).first()
        if target is None:
            raise ManagementError("resource_not_found")
        if (
            target.pk != state.actor.pk
            and is_privileged_account(target)
            and not state.actor.is_superuser
        ):
            raise ManagementError("permission_denied")

        updates: dict[str, Any] = {}
        if "first_name" in changes:
            updates["first_name"] = clean_text(
                changes["first_name"], maximum=MAX_NAME_LENGTH
            )
        if "last_name" in changes:
            updates["last_name"] = clean_text(
                changes["last_name"], maximum=MAX_NAME_LENGTH
            )
        if "email" in changes:
            updates["email"] = _clean_email(changes["email"])
        if "is_active" in changes:
            active = _clean_bool(changes["is_active"])
            if not active and is_privileged_account(target):
                _guard_last_admin(target=target)
            updates["is_active"] = active

        before: dict[str, Any] = {}
        after: dict[str, Any] = {}
        for field, value in updates.items():
            stored = getattr(target, field)
            if stored != value:
                before[field] = stored
                after[field] = value
                setattr(target, field, value)

        if after:
            target.save(update_fields=sorted(after))
            if "is_active" in after:
                _sync_lead_group(user_id=target.pk)
                action = (
                    "user.reactivated" if after["is_active"] else "user.deactivated"
                )
            else:
                action = "user.updated"
            record_audit(
                actor=state.actor,
                action=action,
                target_type="user",
                target_id=str(target.pk),
                before=before,
                after=after,
            )
        payload = user_payload(target)
        payload["revision"] = state.revision
        payload["epoch"] = state.epoch
        return payload


def reset_password(
    *,
    actor_id: int,
    revision: int,
    user_id: int,
    current_password: str,
    new_password: str,
) -> dict[str, Any]:
    """Set a new password after re-authenticating the acting administrator."""
    if not isinstance(current_password, str) or not current_password:
        raise ManagementError("reauthentication_failed")
    with mutation(actor_id=actor_id, revision=revision) as state:
        require_superadmin(actor=state.actor)
        if not state.actor.check_password(current_password):
            raise ManagementError("reauthentication_failed")
        target = User.objects.filter(pk=user_id).first()
        if target is None:
            raise ManagementError("resource_not_found")
        if (
            target.pk != state.actor.pk
            and is_privileged_account(target)
            and not state.actor.is_superuser
        ):
            raise ManagementError("permission_denied")
        clean_password = _clean_password(new_password, candidate=target)
        target.set_password(clean_password)
        target.save(update_fields=["password"])
        record_audit(
            actor=state.actor,
            action="user.password_reset",
            target_type="user",
            target_id=str(target.pk),
            after={"sessions_invalidated": True},
        )
        payload = user_payload(target)
        payload["revision"] = state.revision
        payload["epoch"] = state.epoch
        return payload


def set_global_role(
    *,
    actor_id: int,
    revision: int,
    user_id: int,
    role: Literal["staff", "superadmin"],
) -> dict[str, Any]:
    """Set the fixed base role; only a native superuser may touch Superadmin."""
    if role not in {"staff", "superadmin"}:
        raise ManagementError("invalid_management_request")
    with mutation(actor_id=actor_id, revision=revision, bump_epoch=True) as state:
        require_superadmin(actor=state.actor)
        if not state.actor.is_superuser:
            raise ManagementError("permission_denied")
        target = User.objects.filter(pk=user_id).first()
        if target is None:
            raise ManagementError("resource_not_found")
        granted = role == "superadmin"
        holds = _in_superadmin_group(target)
        if holds != granted:
            if holds:
                _guard_last_admin(target=target)
            if granted:
                target.groups.add(_group(SUPERADMIN_GROUP))
                target.groups.remove(_group(STAFF_GROUP))
            else:
                target.groups.remove(_group(SUPERADMIN_GROUP))
                target.groups.add(_group(STAFF_GROUP))
            record_audit(
                actor=state.actor,
                action="user.role_changed",
                target_type="user",
                target_id=str(target.pk),
                before={"role": "superadmin" if holds else "staff"},
                after={"role": role},
            )
        payload = user_payload(target)
        payload["revision"] = state.revision
        payload["epoch"] = state.epoch
        return payload


@contextmanager
def _department_mutation(*, actor_id: int, revision: int, bump_epoch: bool):
    try:
        with mutation(
            actor_id=actor_id, revision=revision, bump_epoch=bump_epoch
        ) as state:
            yield state
    except IntegrityError as error:
        message = str(error).lower()
        if (
            "accounts_department.normalized_name" in message
            or "accounts_department.code" in message
        ):
            raise ManagementError("department_name_conflict") from None
        raise


def _department_name(name: str) -> tuple[str, str]:
    if not isinstance(name, str):
        raise ManagementError("invalid_management_request")
    display, key = normalize_department_name(name)
    display = clean_text(display, maximum=MAX_NAME_LENGTH, allow_blank=False)
    if not key:
        raise ManagementError("invalid_management_request")
    return display, key


def _department_code(key: str) -> str:
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
    code = key if len(key) <= 64 else f"{key[:47].rstrip('-')}-{digest[:16]}"
    if Department.objects.filter(code=code).exists():
        code = f"department-{digest[:53]}"
        if Department.objects.filter(code=code).exists():
            raise ManagementError("department_name_conflict")
    return code


def create_department(
    *,
    actor_id: int,
    revision: int,
    name: str,
    description: str,
) -> dict[str, Any]:
    """Create one department; the code is immutable once stored."""
    with _department_mutation(
        actor_id=actor_id, revision=revision, bump_epoch=True
    ) as state:
        require_superadmin(actor=state.actor)
        clean_name, key = _department_name(name)
        clean_description = clean_text(description, maximum=MAX_DESCRIPTION_LENGTH)
        if Department.objects.filter(normalized_name=key).exists():
            raise ManagementError("department_name_conflict")
        department = Department.objects.create(
            code=_department_code(key),
            name=clean_name,
            normalized_name=key,
            description=clean_description,
        )
        record_audit(
            actor=state.actor,
            action="department.created",
            target_type="department",
            target_id=str(department.pk),
            after={"code": department.code, "name": department.name},
        )
        payload = department_payload(department)
        payload["revision"] = state.revision
        payload["epoch"] = state.epoch
        return payload


def update_department(
    *,
    actor_id: int,
    revision: int,
    department_id: UUID,
    changes: Mapping[str, Any],
) -> dict[str, Any]:
    """Apply the editable department fields; the code is never one of them."""
    unknown = set(changes) - EDITABLE_DEPARTMENT_FIELDS
    if not changes or unknown:
        raise ManagementError("invalid_management_request")
    with _department_mutation(
        actor_id=actor_id, revision=revision, bump_epoch="is_active" in changes
    ) as state:
        require_superadmin(actor=state.actor)
        department = Department.objects.filter(pk=department_id).first()
        if department is None:
            raise ManagementError("resource_not_found")

        before: dict[str, Any] = {}
        after: dict[str, Any] = {}
        if "name" in changes:
            value, key = _department_name(changes["name"])
            if (
                Department.objects.filter(normalized_name=key)
                .exclude(pk=department.pk)
                .exists()
            ):
                raise ManagementError("department_name_conflict")
            if value != department.name:
                before["name"] = department.name
                after["name"] = value
                department.name = value
        if "description" in changes:
            value = clean_text(changes["description"], maximum=MAX_DESCRIPTION_LENGTH)
            if value != department.description:
                before["description"] = department.description
                after["description"] = value
                department.description = value
        if "is_active" in changes:
            value = _clean_bool(changes["is_active"])
            if value != department.is_active:
                before["is_active"] = department.is_active
                after["is_active"] = value
                department.is_active = value

        if after:
            department.save(update_fields=[*sorted(after), "updated_at"])
            if "is_active" in after:
                for user_id in DepartmentMembership.objects.filter(
                    department=department
                ).values_list("user_id", flat=True):
                    _sync_lead_group(user_id=user_id)
            record_audit(
                actor=state.actor,
                action="department.updated",
                target_type="department",
                target_id=str(department.pk),
                before=before,
                after=after,
            )
        payload = department_payload(department)
        payload["revision"] = state.revision
        payload["epoch"] = state.epoch
        return payload


def _granted_dataset_ids(*, department_id: UUID) -> set[UUID]:
    """Active dataset ids currently granted to one department."""
    from datasets.discovery import configured_source_key

    return set(
        DepartmentDataset.objects.filter(
            department_id=department_id,
            dataset__is_active=True,
            dataset__is_available=True,
            dataset__source_key=configured_source_key(),
            needs_review=False,
        ).values_list("dataset_id", flat=True)
    )


def _apply_roster(*, rows: Sequence[_RosterRow], scope: Q, actor: User) -> None:
    """Replace every membership selected by ``scope`` with exactly ``rows``."""
    existing = {
        (row.user_id, row.department_id): row
        for row in DepartmentMembership.objects.filter(scope)
    }
    requested = {(row.user_id, row.department_id): row for row in rows}
    if len(requested) != len(rows):
        raise ManagementError("invalid_management_request")

    for key, stored in existing.items():
        if key in requested:
            continue
        if stored.is_active:
            DepartmentMembership.objects.filter(pk=stored.pk).update(is_active=False)
        MembershipDataset.objects.filter(membership_id=stored.pk).delete()

    for key, row in requested.items():
        stored = existing.get(key)
        if stored is None:
            stored = DepartmentMembership.objects.create(
                user_id=row.user_id,
                department_id=row.department_id,
                membership_role=row.membership_role,
                is_active=row.is_active,
                assigned_by=actor,
            )
        else:
            fields: list[str] = []
            if stored.membership_role != row.membership_role:
                stored.membership_role = row.membership_role
                fields.append("membership_role")
            if stored.is_active != row.is_active:
                stored.is_active = row.is_active
                fields.append("is_active")
            if fields:
                stored.assigned_by = actor
                stored.save(update_fields=[*fields, "assigned_by", "updated_at"])
        if not stored.is_active:
            MembershipDataset.objects.filter(membership_id=stored.pk).delete()
            continue
        _replace_membership_assignments(
            membership=stored, dataset_ids=list(row.dataset_ids), actor=actor
        )


def _replace_membership_assignments(
    *, membership: DepartmentMembership, dataset_ids: list[UUID], actor: User
) -> None:
    """Set one membership's explicit subset to exactly ``dataset_ids``."""
    from datasets.services import dataset_availability

    current = set(
        MembershipDataset.objects.filter(membership=membership).values_list(
            "department_grant_id", flat=True
        )
    )
    grants = {
        grant.dataset_id: grant
        for grant in DepartmentDataset.objects.filter(
            department_id=membership.department_id, dataset_id__in=dataset_ids
        ).select_related("dataset")
        if dataset_availability(grant.dataset, grant=grant) == "available"
        or grant.pk in current
    }
    if len(grants) != len(dataset_ids):
        raise ManagementError("invalid_management_request")
    wanted = {grants[dataset_id].pk for dataset_id in dataset_ids}
    MembershipDataset.objects.filter(
        membership=membership, department_grant_id__in=current - wanted
    ).delete()
    MembershipDataset.objects.bulk_create(
        [
            MembershipDataset(
                membership=membership,
                department_grant_id=grant_id,
                assigned_by=actor,
            )
            for grant_id in wanted - current
        ]
    )


def _validate_roster_rows(
    *, rows: Sequence[_RosterRow], department_ids: set[UUID], user_ids: set[int]
) -> None:
    if len(rows) > MAX_REPLACEMENT_ENTRIES:
        raise ManagementError("invalid_management_request")
    for row in rows:
        if row.department_id not in department_ids:
            raise ManagementError("resource_not_found")
        if row.user_id not in user_ids:
            raise ManagementError("resource_not_found")
        if row.membership_role not in MEMBERSHIP_ROLE_VALUES:
            raise ManagementError("invalid_management_request")
        if len(row.dataset_ids) > MAX_REPLACEMENT_ENTRIES:
            raise ManagementError("invalid_management_request")
        unique_values(row.dataset_ids)

    allowed: dict[UUID, set[UUID]] = {}
    for row in rows:
        if not row.dataset_ids:
            continue
        if row.department_id not in allowed:
            allowed[row.department_id] = _granted_dataset_ids(
                department_id=row.department_id
            )
        retained = (
            set(
                MembershipDataset.objects.filter(
                    membership__user_id=row.user_id,
                    membership__department_id=row.department_id,
                    membership__is_active=True,
                    department_grant__department_id=F("membership__department_id"),
                ).values_list("department_grant__dataset_id", flat=True)
            )
            if row.is_active
            else set()
        )
        if not set(row.dataset_ids) <= (allowed[row.department_id] | retained):
            raise ManagementError("invalid_management_request")


def replace_user_memberships(
    *,
    actor_id: int,
    revision: int,
    user_id: int,
    memberships: Sequence[MembershipInput],
) -> dict[str, Any]:
    """Replace one account's complete membership set."""
    with mutation(actor_id=actor_id, revision=revision, bump_epoch=True) as state:
        require_superadmin(actor=state.actor)
        target = User.objects.filter(pk=user_id).first()
        if target is None:
            raise ManagementError("resource_not_found")
        if not isinstance(memberships, Sequence) or isinstance(
            memberships, (str, bytes)
        ):
            raise ManagementError("invalid_management_request")
        if len(memberships) > MAX_REPLACEMENT_ENTRIES:
            raise ManagementError("invalid_management_request")
        if any(not isinstance(entry, MembershipInput) for entry in memberships):
            raise ManagementError("invalid_management_request")
        unique_values([entry.department_id for entry in memberships])

        rows = [
            _RosterRow(
                user_id=target.pk,
                department_id=entry.department_id,
                membership_role=entry.membership_role,
                is_active=entry.is_active,
                dataset_ids=entry.dataset_ids,
            )
            for entry in memberships
        ]
        department_ids = set(
            Department.objects.filter(
                pk__in=[entry.department_id for entry in memberships],
                is_active=True,
            ).values_list("pk", flat=True)
        )
        _validate_roster_rows(
            rows=rows, department_ids=department_ids, user_ids={target.pk}
        )

        before = sorted(
            f"{row.department.code}:{row.membership_role}"
            for row in DepartmentMembership.objects.filter(
                user_id=target.pk, is_active=True
            ).select_related("department")
        )
        _apply_roster(rows=rows, scope=Q(user_id=target.pk), actor=state.actor)
        _sync_lead_group(user_id=target.pk)
        after = sorted(
            f"{row.department.code}:{row.membership_role}"
            for row in DepartmentMembership.objects.filter(
                user_id=target.pk, is_active=True
            ).select_related("department")
        )
        record_audit(
            actor=state.actor,
            action="department.memberships_changed",
            target_type="user",
            target_id=str(target.pk),
            before={"memberships": before},
            after={"memberships": after},
        )
        payload = user_payload(target)
        payload["memberships_detail"] = membership_rows(user_id=target.pk)
        payload["revision"] = state.revision
        payload["epoch"] = state.epoch
        return payload


def replace_department_members(
    *,
    actor_id: int,
    revision: int,
    department_id: UUID,
    members: Sequence[DepartmentMemberInput],
) -> dict[str, Any]:
    """Replace a department roster; a Lead may only touch its Staff rows."""
    with mutation(actor_id=actor_id, revision=revision, bump_epoch=True) as state:
        global_scope = is_superadmin(state.actor)
        permission = MANAGE_USERS if global_scope else MANAGE_DEPARTMENT_STAFF
        require_permission(actor=state.actor, permission=permission)
        department = Department.objects.filter(pk=department_id, is_active=True).first()
        if department is None:
            raise ManagementError("resource_not_found")
        if not global_scope:
            require_department_lead(actor=state.actor, department_id=department.pk)

        if not isinstance(members, Sequence) or isinstance(members, (str, bytes)):
            raise ManagementError("invalid_management_request")
        if len(members) > MAX_REPLACEMENT_ENTRIES:
            raise ManagementError("invalid_management_request")
        if any(not isinstance(entry, DepartmentMemberInput) for entry in members):
            raise ManagementError("invalid_management_request")
        if any(
            isinstance(entry.user_id, bool) or not isinstance(entry.user_id, int)
            for entry in members
        ):
            raise ManagementError("invalid_management_request")
        unique_values([entry.user_id for entry in members])
        if not global_scope and any(
            entry.membership_role != "staff" for entry in members
        ):
            raise ManagementError("permission_denied")

        targets = {
            user.pk: user
            for user in User.objects.filter(
                pk__in=[entry.user_id for entry in members], is_active=True
            )
        }
        if len(targets) != len(members):
            raise ManagementError("resource_not_found")
        if not global_scope and any(
            is_privileged_account(target) for target in targets.values()
        ):
            raise ManagementError("resource_not_found")

        replaceable = Q(department=department)
        if not global_scope:
            replaceable &= Q(membership_role="staff")
        existing_leads = set(
            DepartmentMembership.objects.filter(
                department=department, membership_role="lead", is_active=True
            ).values_list("user_id", flat=True)
        )
        if not global_scope and existing_leads & set(targets):
            raise ManagementError("permission_denied")

        rows = [
            _RosterRow(
                user_id=entry.user_id,
                department_id=department.pk,
                membership_role=entry.membership_role,
                is_active=entry.is_active,
                dataset_ids=entry.dataset_ids,
            )
            for entry in members
        ]
        _validate_roster_rows(
            rows=rows,
            department_ids={department.pk},
            user_ids=set(targets),
        )

        before = sorted(
            f"{row.user.username}:{row.membership_role}"
            for row in DepartmentMembership.objects.filter(
                department=department, is_active=True
            ).select_related("user")
        )
        affected = set(
            DepartmentMembership.objects.filter(replaceable).values_list(
                "user_id", flat=True
            )
        ) | set(targets)
        _apply_roster(rows=rows, scope=replaceable, actor=state.actor)
        for user_id in affected:
            _sync_lead_group(user_id=user_id)
        after = sorted(
            f"{row.user.username}:{row.membership_role}"
            for row in DepartmentMembership.objects.filter(
                department=department, is_active=True
            ).select_related("user")
        )
        record_audit(
            actor=state.actor,
            action="department.memberships_changed",
            target_type="department",
            target_id=str(department.pk),
            before={"members": before},
            after={"members": after},
        )
        return {
            "department": department_payload(department),
            "members": roster_members(department_id=department.pk),
            "revision": state.revision,
            "epoch": state.epoch,
        }


def seed_fixed_roles(*, actor_id: int, revision: int) -> dict[str, Any]:
    """Idempotently restore the fixed role definitions through the audit path."""
    with mutation(actor_id=actor_id, revision=revision, bump_epoch=True) as state:
        if not state.actor.is_superuser:
            raise ManagementError("permission_denied")
        require_permission(actor=state.actor, permission=MANAGE_ROLES)
        repaired = ensure_fixed_roles()
        if repaired:
            record_audit(
                actor=state.actor,
                action="roles.seeded",
                target_type="roles",
                target_id="fixed",
                after={"repaired_groups": repaired},
            )
        return {
            "repaired_groups": repaired,
            "revision": state.revision,
            "epoch": state.epoch,
        }
