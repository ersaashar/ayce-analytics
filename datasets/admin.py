"""Django Admin views for the discovered catalog, backed by audited services."""

from __future__ import annotations

from typing import Any

from django import forms
from django.contrib import admin

from accounts.admin import RevisionForm, ServiceBackedAdmin, public_message
from accounts.authorization import ManagementError, is_superadmin

from . import services
from .discovery import configured_source_key
from .models import Dataset, DatasetCatalogState, DepartmentDataset, MembershipDataset
from .permissions import MANAGE_CATALOG


class DatasetChangeForm(RevisionForm):
    class Meta:
        model = Dataset
        fields = ("is_active",)

    def clean(self):
        cleaned = super().clean()
        if self.instance.pk is None or self.errors:
            return cleaned
        try:
            services.update_dataset(
                actor_id=self.actor_id,
                revision=cleaned.get("revision"),
                dataset_id=self.instance.pk,
                changes={"is_active": cleaned["is_active"]},
            )
        except ManagementError as exc:
            raise forms.ValidationError(public_message(exc.code)) from None
        self.instance.refresh_from_db()
        return cleaned


@admin.register(Dataset)
class ManagedDatasetAdmin(ServiceBackedAdmin):
    form = DatasetChangeForm
    fieldsets = (
        (None, {"fields": ("code", "revision")}),
        (
            "Discovered model",
            {
                "fields": (
                    "display_name",
                    "description",
                    "source_key",
                    "project_key",
                    "model_name",
                    "catalog",
                    "schema",
                    "physical_table",
                    "is_available",
                    "catalog_version",
                    "validation_status",
                    "validated_at",
                )
            },
        ),
        ("Local access", {"fields": ("is_active",)}),
    )
    list_display = (
        "code",
        "display_name",
        "is_available",
        "validation_status",
        "is_active",
    )
    list_filter = ("is_available", "validation_status", "is_active")
    search_fields = ("code", "display_name", "project_key", "model_name")
    ordering = ("code",)

    def get_queryset(self, request):
        queryset = super().get_queryset(request)
        source_key = configured_source_key()
        if (
            not is_superadmin(request.user)
            or not request.user.has_perm(MANAGE_CATALOG)
            or source_key is None
        ):
            return queryset.none()
        return queryset.filter(source_key=source_key, is_available=True)

    def has_module_permission(self, request) -> bool:
        return is_superadmin(request.user) and request.user.has_perm(MANAGE_CATALOG)

    def has_view_permission(self, request, obj=None) -> bool:
        return is_superadmin(request.user) and request.user.has_perm(MANAGE_CATALOG)

    def has_add_permission(self, request) -> bool:
        return False

    def has_change_permission(self, request, obj=None) -> bool:
        return is_superadmin(request.user) and request.user.has_perm(MANAGE_CATALOG)

    def get_readonly_fields(self, request, obj=None):
        return (
            "code",
            "display_name",
            "description",
            "source_key",
            "project_key",
            "model_name",
            "catalog",
            "schema",
            "physical_table",
            "is_available",
            "catalog_version",
            "validation_status",
            "validated_at",
        )

    def get_form(self, request, obj=None, change=False, **kwargs):
        kwargs["form"] = DatasetChangeForm
        form_class = super().get_form(request, obj, change, **kwargs)
        form_class.__name__ = "DatasetChangeForm"
        actor_id = request.user.pk

        class Bound(form_class):
            def __init__(self, *args: Any, **inner: Any) -> None:
                inner.setdefault("actor_id", actor_id)
                super().__init__(*args, **inner)

        return Bound


class ReadOnlyAdmin(admin.ModelAdmin):
    actions: tuple = ()

    def has_add_permission(self, request) -> bool:
        return False

    def has_change_permission(self, request, obj=None) -> bool:
        return False

    def has_delete_permission(self, request, obj=None) -> bool:
        return False

    def get_actions(self, request):
        return {}

    def has_module_permission(self, request) -> bool:
        return is_superadmin(request.user)

    def has_view_permission(self, request, obj=None) -> bool:
        return is_superadmin(request.user)


@admin.register(DatasetCatalogState)
class DatasetCatalogStateAdmin(ReadOnlyAdmin):
    list_display = ("source_key", "last_attempt_at", "last_success_at", "error_code")
    readonly_fields = (
        "source_key",
        "last_attempt_at",
        "last_success_at",
        "snapshot_sha256",
        "structure_sha256",
        "error_code",
    )


@admin.register(DepartmentDataset)
class DepartmentGrantAdmin(ReadOnlyAdmin):
    list_display = ("department", "dataset", "granted_at")
    search_fields = ("department__code", "dataset__code")


@admin.register(MembershipDataset)
class MembershipDatasetAdmin(ReadOnlyAdmin):
    list_display = ("membership", "department_grant", "assigned_at")
    search_fields = ("membership__user__username", "department_grant__dataset__code")
