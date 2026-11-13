"""Small, explicit authorization helpers.

There is deliberately no policy engine here: every rule is a named function the
services and views call directly. Authority is always resolved from current
database state, never from a request payload or a cached bootstrap.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any
from uuid import UUID

from django.contrib.auth.models import User
from django.db.models import F, QuerySet

from datasets.models import Dataset, MembershipDataset

from .models import Department, DepartmentMembership
from .permissions import (
    CAPABILITIES,
    MANAGE_USERS,
    ROLE_LABELS,
    SUPERADMIN_GROUP,
    USE_ANALYTICS,
)


class ManagementError(Exception):
    """A management failure carrying only a registered public error code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def is_superadmin(user: Any) -> bool:
    """Whether ``user`` is an active effective application Superadmin."""
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    if not getattr(user, "is_active", False):
        return False
    if user.is_superuser:
        return True
    if not user.groups.filter(name=SUPERADMIN_GROUP).exists():
        return False
    return bool(user.has_perm(MANAGE_USERS))


def require_permission(*, actor: Any, permission: str) -> None:
    """Require one native capability on an active principal."""
    if actor is None or not getattr(actor, "is_active", False):
        raise ManagementError("permission_denied")
    if not actor.has_perm(permission):
        raise ManagementError("permission_denied")


def require_superadmin(*, actor: Any) -> None:
    """Require effective application Superadmin authority."""
    if not is_superadmin(actor):
        raise ManagementError("permission_denied")


def active_membership(
    *, user_id: int, department_id: UUID
) -> DepartmentMembership | None:
    """Return one active membership in an active department, or ``None``."""
    return (
        DepartmentMembership.objects.select_related("department")
        .filter(
            user_id=user_id,
            department_id=department_id,
            is_active=True,
            department__is_active=True,
            user__is_active=True,
        )
        .first()
    )


def led_memberships(actor: Any) -> QuerySet:
    """Active Lead memberships of an active user in active departments."""
    return DepartmentMembership.objects.filter(
        user_id=getattr(actor, "pk", None),
        membership_role="lead",
        is_active=True,
        department__is_active=True,
        user__is_active=True,
    )


def require_department_lead(*, actor: Any, department_id: UUID) -> DepartmentMembership:
    """Return the actor's active Lead membership, or hide the department."""
    require_permission(actor=actor, permission="accounts.manage_department_staff")
    membership = (
        led_memberships(actor)
        .select_related("department")
        .filter(department_id=department_id)
        .first()
    )
    if not membership:
        raise ManagementError("resource_not_found")
    return membership


def require_department_access(*, actor: Any, department_id: UUID) -> bool:
    """Authorize one department scope, returning whether it is global.

    ``True`` means the actor is an effective Superadmin; ``False`` means the
    actor leads this department. Anything else is a scoped 404.
    """
    if is_superadmin(actor):
        return True
    require_department_lead(actor=actor, department_id=department_id)
    return False


def led_department_ids(actor: Any) -> QuerySet:
    """The ids of departments the actor actively leads; never unrestricted."""
    if getattr(actor, "pk", None) is None:
        return DepartmentMembership.objects.none().values("department_id")
    return led_memberships(actor).values("department_id")


def list_user_departments(*, user_id: int) -> QuerySet:
    """Active memberships of an active user in active departments."""
    return (
        DepartmentMembership.objects.filter(
            user_id=user_id,
            is_active=True,
            department__is_active=True,
            user__is_active=True,
        )
        .select_related("department")
        .order_by("department__code", "id")
    )


def effective_dataset_ids(membership_ids: Iterable[UUID]) -> dict[UUID, list[str]]:
    """Map each membership to the dataset ids it currently resolves to."""
    from datasets.discovery import configured_source_key

    resolved: dict[UUID, list[str]] = {}
    rows = (
        MembershipDataset.objects.filter(
            membership_id__in=list(membership_ids),
            membership__is_active=True,
            membership__department__is_active=True,
            department_grant__department__is_active=True,
            department_grant__dataset__is_active=True,
            department_grant__dataset__is_available=True,
            department_grant__dataset__source_key=configured_source_key(),
            department_grant__needs_review=False,
            membership__user__is_active=True,
        )
        .select_related("department_grant__dataset", "membership__user")
        .order_by("department_grant__dataset__code", "id")
    )
    for row in rows:
        if (
            row.department_grant.department_id != row.membership.department_id
            or not row.membership.user.has_perm(USE_ANALYTICS)
        ):
            continue
        resolved.setdefault(row.membership_id, []).append(
            str(row.department_grant.dataset_id)
        )
    return resolved


def eligible_dataset_assignments(
    *, user_id: int, department_id: UUID | None = None
) -> QuerySet:
    """Explicit assignments currently eligible for this user and optional department."""
    from datasets.discovery import configured_source_key

    user = User.objects.filter(pk=user_id).only("id", "is_active").first()
    if user is None or not user.is_active:
        return MembershipDataset.objects.none()
    if not user.has_perm(USE_ANALYTICS):
        return MembershipDataset.objects.none()

    assignments = MembershipDataset.objects.filter(
        membership__user_id=user_id,
        membership__is_active=True,
        membership__department__is_active=True,
        department_grant__department__is_active=True,
        department_grant__dataset__is_active=True,
        department_grant__dataset__is_available=True,
        department_grant__dataset__source_key=configured_source_key(),
        department_grant__needs_review=False,
        department_grant__department_id=F("membership__department_id"),
    )
    if department_id is not None:
        assignments = assignments.filter(membership__department_id=department_id)
    return assignments


def active_dataset_queryset(
    *, user_id: int, department_id: UUID | None = None
) -> QuerySet:
    """Datasets a user may currently see, from explicit state only."""
    assignments = eligible_dataset_assignments(
        user_id=user_id, department_id=department_id
    )
    return Dataset.objects.filter(
        is_active=True,
        pk__in=assignments.values("department_grant__dataset_id"),
    ).order_by("code", "id")


def can_use_management(user: Any) -> bool:
    """Whether this principal has any department administration surface."""
    if user is None or not getattr(user, "is_active", False):
        return False
    if is_superadmin(user):
        return True
    if not user.has_perm("accounts.manage_department_staff"):
        return False
    return led_department_ids(user).exists()


def user_projection(user: Any) -> dict[str, Any]:
    """The only account fields any public response may repeat."""
    return {
        "id": user.pk,
        "username": user.get_username(),
        "first_name": user.first_name or "",
        "last_name": user.last_name or "",
    }


def fixed_roles(user: Any) -> list[str]:
    """Fixed AYCE group labels the account currently holds, in stable order."""
    names = set(user.groups.values_list("name", flat=True))
    return [label for label in ROLE_LABELS if label in names]


def capability_map(user: Any) -> dict[str, bool]:
    """Per-capability booleans for the current principal."""
    active = bool(getattr(user, "is_active", False))
    if not active:
        return dict.fromkeys(CAPABILITIES, False)
    return {name: bool(user.has_perm(perm)) for name, perm in CAPABILITIES.items()}


def department_label(department: Department) -> dict[str, str]:
    return {
        "id": str(department.id),
        "code": department.code,
        "name": department.name,
    }
