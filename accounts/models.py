"""Accounts, departments, and the audited authorization boundary.

``AuthorizationState`` is a singleton row that serializes every administration
write and carries the epoch that department-bound chat runs compare against.
``AuthorizationAudit`` is append-only application history: it has no foreign key
to its target so a later deletion cannot erase what happened.
"""

from __future__ import annotations

import uuid
from typing import Any, ClassVar

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models

from .department_names import normalize_department_name

MEMBERSHIP_ROLES: ClassVar[tuple[tuple[str, str], ...]] = (
    ("staff", "Staff"),
    ("lead", "Lead"),
)


class AuthorizationState(models.Model):
    """One row, ``id=1``: the administration revision and authorization epoch."""

    id = models.PositiveSmallIntegerField(primary_key=True, default=1, editable=False)
    revision = models.PositiveBigIntegerField(default=0)
    epoch = models.PositiveBigIntegerField(default=0)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = "Authorization state"
        verbose_name_plural = "Authorization state"
        permissions: ClassVar[list[tuple[str, str]]] = [
            ("manage_users", "Can create, update and deactivate accounts"),
            ("manage_roles", "Can change fixed application roles"),
            ("view_audit", "Can read the authorization audit log"),
        ]

    def __str__(self) -> str:
        return f"revision={self.revision} epoch={self.epoch}"


class AuditQuerySet(models.QuerySet):
    """Append-only: no bulk update and no bulk delete through the ORM."""

    def update(self, **kwargs: Any) -> int:
        raise ValidationError("Authorization audit records are append-only.")

    def delete(self) -> tuple[int, dict[str, int]]:
        raise ValidationError("Authorization audit records are append-only.")


class AuditManager(models.Manager.from_queryset(AuditQuerySet)):
    """Manager that only ever creates audit records."""


class AuthorizationAudit(models.Model):
    """One bounded, append-only authorization decision record."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="+",
        null=True,
        blank=True,
        default=None,
    )
    action = models.CharField(max_length=64)
    target_type = models.CharField(max_length=32)
    target_id = models.CharField(max_length=64)
    allowed = models.BooleanField(default=True)
    reason_code = models.CharField(max_length=64)
    before = models.JSONField(default=dict)
    after = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    objects: ClassVar[AuditManager] = AuditManager()

    class Meta:
        ordering = ("-created_at", "-id")
        indexes: ClassVar[list[models.Index]] = [
            models.Index(
                fields=("-created_at", "-id"),
                name="audit_recency_idx",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.action} {self.target_type}:{self.target_id}"

    def save(self, *args: Any, **kwargs: Any) -> None:
        if not self._state.adding:
            raise ValidationError("Authorization audit records are append-only.")
        super().save(*args, **kwargs)

    def delete(self, *args: Any, **kwargs: Any) -> tuple[int, dict[str, int]]:
        raise ValidationError("Authorization audit records are append-only.")


class Department(models.Model):
    """A stable grouping that owns dataset grants and chat context."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    code = models.SlugField(max_length=64, unique=True, allow_unicode=True)
    name = models.CharField(max_length=150)
    normalized_name = models.CharField(max_length=450, unique=True, editable=False)
    description = models.TextField(blank=True, default="")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    users = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        through="DepartmentMembership",
        through_fields=("department", "user"),
        related_name="departments",
        blank=True,
    )

    class Meta:
        ordering = ("code", "id")
        permissions: ClassVar[list[tuple[str, str]]] = [
            ("manage_departments", "Can create, update and deactivate departments"),
            (
                "manage_department_staff",
                "Can manage staff memberships of a led department",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.code} ({self.name})"

    def save(self, *args: Any, **kwargs: Any) -> None:
        update_fields = kwargs.get("update_fields")
        if update_fields is None or "name" in update_fields:
            self.name, self.normalized_name = normalize_department_name(self.name)
            if update_fields is not None:
                kwargs["update_fields"] = set(update_fields) | {"normalized_name"}
        super().save(*args, **kwargs)


class DepartmentMembership(models.Model):
    """One account's role and dataset subset inside one department."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="department_memberships",
    )
    department = models.ForeignKey(
        Department,
        on_delete=models.PROTECT,
        related_name="memberships",
    )
    membership_role = models.CharField(
        max_length=16,
        choices=MEMBERSHIP_ROLES,
        default="staff",
    )
    is_active = models.BooleanField(default=True)
    assigned_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="+",
        null=True,
        blank=True,
        default=None,
    )
    assigned_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("department__code", "user__username", "id")
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=("user", "department"),
                name="unique_user_department_membership",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.user_id}@{self.department_id}:{self.membership_role}"
