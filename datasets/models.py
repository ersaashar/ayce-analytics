"""Discovered dataset metadata and explicit department grants."""

from __future__ import annotations

import uuid
from typing import ClassVar

from django.conf import settings
from django.db import models

VALIDATION_STATUSES: ClassVar[tuple[tuple[str, str], ...]] = (
    ("unverified", "Unverified"),
    ("metadata_discovered", "Metadata discovered"),
)


class Dataset(models.Model):
    """One source-backed semantic model.

    A row is catalog metadata only. It carries no query text or credential, and
    existing here never grants a query: that needs a department grant and an
    explicit per-membership assignment.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    code = models.SlugField(max_length=64, unique=True)
    display_name = models.CharField(max_length=150)
    description = models.TextField(blank=True, default="")
    source_key = models.SlugField(max_length=64)
    project_key = models.CharField(max_length=128)
    model_name = models.CharField(max_length=128)
    catalog = models.CharField(max_length=128, blank=True, default="")
    schema = models.CharField(max_length=128, blank=True, default="")
    physical_table = models.CharField(max_length=128, blank=True, default="")
    is_active = models.BooleanField(default=True)
    is_available = models.BooleanField(default=False)
    definition_sha256 = models.CharField(
        max_length=64, blank=True, default="", editable=False
    )
    catalog_version = models.CharField(max_length=128, blank=True, default="")
    validation_status = models.CharField(
        max_length=32,
        choices=VALIDATION_STATUSES,
        default="unverified",
    )
    validated_at = models.DateTimeField(null=True, blank=True, default=None)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("code", "id")
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=("source_key", "project_key", "model_name"),
                name="unique_dataset_source_model",
            ),
        ]
        permissions: ClassVar[list[tuple[str, str]]] = [
            ("manage_catalog", "Can edit reviewed dataset metadata"),
            ("manage_grants", "Can grant datasets to departments and members"),
        ]

    def __str__(self) -> str:
        return f"{self.code} ({self.source_key}/{self.model_name})"


class DepartmentDataset(models.Model):
    """One dataset granted to one department, independent of any member."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    department = models.ForeignKey(
        "accounts.Department",
        on_delete=models.PROTECT,
        related_name="dataset_grants",
    )
    dataset = models.ForeignKey(
        Dataset,
        on_delete=models.PROTECT,
        related_name="department_grants",
    )
    granted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="+",
        null=True,
        blank=True,
        default=None,
    )
    granted_at = models.DateTimeField(auto_now_add=True)
    needs_review = models.BooleanField(default=False)

    class Meta:
        ordering = ("department__code", "dataset__code", "id")
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=("department", "dataset"),
                name="unique_department_dataset",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.department_id}->{self.dataset_id}"


class DatasetCatalogState(models.Model):
    """Last metadata discovery outcome for one configured source."""

    source_key = models.CharField(max_length=64, primary_key=True)
    last_attempt_at = models.DateTimeField(null=True, blank=True)
    last_success_at = models.DateTimeField(null=True, blank=True)
    snapshot_sha256 = models.CharField(max_length=64, blank=True, default="")
    structure_sha256 = models.CharField(max_length=64, blank=True, default="")
    execution_metadata_json = models.TextField(blank=True, default="", editable=False)
    error_code = models.CharField(max_length=32, blank=True, default="")

    def __str__(self) -> str:
        return self.source_key


class MembershipDataset(models.Model):
    """One explicit dataset subset of one department membership."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    membership = models.ForeignKey(
        "accounts.DepartmentMembership",
        on_delete=models.CASCADE,
        related_name="dataset_assignments",
    )
    department_grant = models.ForeignKey(
        DepartmentDataset,
        on_delete=models.CASCADE,
        related_name="membership_assignments",
    )
    assigned_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        related_name="+",
        null=True,
        blank=True,
        default=None,
    )
    assigned_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("membership_id", "department_grant__dataset__code", "id")
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=("membership", "department_grant"),
                name="unique_membership_dataset",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.membership_id}->{self.department_grant_id}"
