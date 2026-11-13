"""Django Admin as a service-backed surface, never a second policy path.

Every write here runs through the same audited services as the JSON APIs, each
form carries the current administration revision, relationships are never
written by ``save_m2m``, and hard deletion is unavailable. Stock User and Group
admins are replaced because their write paths bypass the policy services.
"""

from __future__ import annotations

import contextlib
from typing import Any

from django import forms
from django.contrib import admin
from django.contrib.auth.models import Group, Permission, User
from django.core.exceptions import PermissionDenied
from django.http import Http404, HttpResponseRedirect
from django.template.response import TemplateResponse
from django.urls import path, reverse

from chat.views import ERRORS

from . import services
from .authorization import ManagementError, is_superadmin
from .models import (
    AuthorizationAudit,
    AuthorizationState,
    Department,
    DepartmentMembership,
)

# The stock auth admins write straight to the models; remove them before this
# module registers the service-backed replacements.
for _stock in (User, Group):
    with contextlib.suppress(admin.sites.NotRegistered):
        admin.site.unregister(_stock)


def public_message(code: str) -> str:
    return ERRORS.get(code, ("Invalid request.", ""))[1]


def _bound(form_class: type, *, actor_id: int, **extra: Any) -> type:
    """Bind the acting principal to one admin form class."""

    class Bound(form_class):  # type: ignore[misc, valid-type]
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            kwargs.setdefault("actor_id", actor_id)
            for key, value in extra.items():
                kwargs.setdefault(key, value)
            super().__init__(*args, **kwargs)

    Bound.__name__ = form_class.__name__
    return Bound


class ServiceBackedAdmin(admin.ModelAdmin):
    """Admin base: the service writes; the admin never saves relationships."""

    actions: tuple = ()

    def save_model(self, request, obj, form, change) -> None:
        """The mutation already happened inside the form's ``clean``."""

    def save_related(self, request, form, formsets, change) -> None:
        """Relationships are replaced by the service, never by save_m2m."""

    def get_actions(self, request):
        return {}

    def has_delete_permission(self, request, obj=None) -> bool:
        return False

    def log_addition(self, request, obj, message) -> None:
        """Audit records are written by the service, not by Django's log."""

    def log_change(self, request, obj, message) -> None:
        """Audit records are written by the service, not by Django's log."""

    def log_deletion(self, request, obj, object_repr) -> None:
        """Deletion is unavailable; nothing to log."""


class RevisionForm(forms.ModelForm):
    """ModelForm with the current administration revision and actor."""

    revision = forms.IntegerField(widget=forms.HiddenInput, required=True)

    def __init__(self, *args: Any, actor_id: int, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.actor_id = actor_id
        self.fields["revision"].initial = services.current_state()["revision"]
        code = "invalid_management_request"
        self.fields["revision"].error_messages = {
            "required": public_message(code),
            "invalid": public_message(code),
        }


class ManagedUserAddForm(RevisionForm):
    password1 = forms.CharField(label="Password", widget=forms.PasswordInput)
    password2 = forms.CharField(
        label="Password confirmation", widget=forms.PasswordInput
    )

    class Meta:
        model = User
        fields = ("username", "first_name", "last_name", "email")

    def clean(self):
        cleaned = super().clean()
        password1 = cleaned.get("password1")
        password2 = cleaned.get("password2")
        if password1 != password2:
            raise forms.ValidationError("The two password fields did not match.")
        if not self.errors:
            try:
                created = services.create_user(
                    actor_id=self.actor_id,
                    revision=cleaned.get("revision"),
                    username=cleaned.get("username") or "",
                    first_name=cleaned.get("first_name") or "",
                    last_name=cleaned.get("last_name") or "",
                    email=cleaned.get("email") or "",
                    password=password1 or "",
                )
            except ManagementError as exc:
                raise forms.ValidationError(public_message(exc.code)) from None
            self.instance = User.objects.get(pk=created["id"])
        return cleaned


class ManagedUserChangeForm(RevisionForm):
    class Meta:
        model = User
        fields = ("first_name", "last_name", "email", "is_active")

    def clean(self):
        cleaned = super().clean()
        if self.instance.pk is None or self.errors:
            return cleaned
        changes = {
            key: cleaned[key]
            for key in ("first_name", "last_name", "email", "is_active")
            if key in cleaned
        }
        try:
            services.update_user(
                actor_id=self.actor_id,
                revision=cleaned.get("revision"),
                user_id=self.instance.pk,
                changes=changes,
            )
        except ManagementError as exc:
            raise forms.ValidationError(public_message(exc.code)) from None
        self.instance.refresh_from_db()
        return cleaned


class ManagedPasswordChangeForm(forms.Form):
    """Password change form with the acting revision and re-authentication."""

    revision = forms.IntegerField(widget=forms.HiddenInput, required=True)
    current_password = forms.CharField(
        label="Your current password", widget=forms.PasswordInput
    )
    password1 = forms.CharField(label="New password", widget=forms.PasswordInput)
    password2 = forms.CharField(
        label="Confirm new password", widget=forms.PasswordInput
    )

    def __init__(self, user: User, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.user = user
        self.fields["revision"].initial = services.current_state()["revision"]

    def clean(self):
        cleaned = super().clean()
        if cleaned.get("password1") != cleaned.get("password2"):
            raise forms.ValidationError("The two password fields did not match.")
        return cleaned


@admin.register(User)
class ManagedUserAdmin(ServiceBackedAdmin):
    change_form_template = "admin/auth/user/change_form.html"
    fieldsets = (
        (None, {"fields": ("username", "revision")}),
        ("Profile", {"fields": ("first_name", "last_name", "email")}),
        ("Status", {"fields": ("is_active",)}),
    )
    add_fieldsets = (
        (
            None,
            {
                "classes": ("wide",),
                "fields": (
                    "username",
                    "first_name",
                    "last_name",
                    "email",
                    "password1",
                    "password2",
                    "revision",
                ),
            },
        ),
    )
    list_display = ("username", "first_name", "last_name", "is_active")
    list_filter = ()
    search_fields = ("username", "first_name", "last_name", "email")
    ordering = ("username",)
    form = ManagedUserChangeForm
    add_form = ManagedUserAddForm
    filter_horizontal = ()

    def get_queryset(self, request):
        queryset = super().get_queryset(request)
        if not is_superadmin(request.user):
            return queryset.none()
        return queryset.prefetch_related("groups")

    def has_module_permission(self, request) -> bool:
        return is_superadmin(request.user)

    def has_view_permission(self, request, obj=None) -> bool:
        return is_superadmin(request.user)

    def has_add_permission(self, request) -> bool:
        return is_superadmin(request.user)

    def has_change_permission(self, request, obj=None) -> bool:
        return is_superadmin(request.user)

    def has_delete_permission(self, request, obj=None) -> bool:
        return False

    def get_readonly_fields(self, request, obj=None):
        return ("username",) if obj is not None else ()

    def get_urls(self):
        return [
            path(
                "<id>/password/",
                self.admin_site.admin_view(self.user_change_password),
                name="auth_user_password_change",
            ),
            *super().get_urls(),
        ]

    def lookup_allowed(self, lookup, value, request=None):
        if "password" in lookup:
            return False
        return super().lookup_allowed(lookup, value, request)

    def get_form(self, request, obj=None, change=False, **kwargs):
        kwargs["form"] = ManagedUserChangeForm if change else ManagedUserAddForm
        if not change:
            kwargs.pop("fields", None)
        form_class = super().get_form(request, obj, change, **kwargs)
        return _bound(form_class, actor_id=request.user.pk)

    def user_change_password(self, request, id, form_url=""):
        user = self.get_object(request, id)
        if not self.has_change_permission(request, user):
            raise PermissionDenied
        if user is None:
            raise Http404("No such account.")
        if request.method == "POST":
            form = ManagedPasswordChangeForm(user, request.POST)
            if form.is_valid():
                try:
                    services.reset_password(
                        actor_id=request.user.pk,
                        revision=form.cleaned_data["revision"],
                        user_id=user.pk,
                        current_password=form.cleaned_data["current_password"],
                        new_password=form.cleaned_data["password1"],
                    )
                except ManagementError as exc:
                    form.add_error(None, public_message(exc.code))
                else:
                    self.message_user(request, "Password changed.")
                    return HttpResponseRedirect(
                        reverse(
                            f"{self.admin_site.name}:{user._meta.app_label}_{user._meta.model_name}_change",
                            args=(user.pk,),
                        )
                    )
        else:
            form = ManagedPasswordChangeForm(user)

        context = {
            "title": f"Change password: {user.get_username()}",
            "form_url": form_url,
            "form": form,
            "is_popup": False,
            "add": True,
            "change": False,
            "has_delete_permission": False,
            "has_change_permission": True,
            "has_absolute_url": False,
            "opts": self.opts,
            "original": user,
            "save_as": False,
            "show_save": True,
            **self.admin_site.each_context(request),
        }
        request.current_app = self.admin_site.name
        return TemplateResponse(request, "admin/accounts/password_change.html", context)


class DepartmentAddForm(RevisionForm):
    class Meta:
        model = Department
        fields = ("name", "description")

    def clean(self):
        cleaned = super().clean()
        if not self.errors:
            try:
                created = services.create_department(
                    actor_id=self.actor_id,
                    revision=cleaned.get("revision"),
                    name=cleaned.get("name") or "",
                    description=cleaned.get("description") or "",
                )
            except ManagementError as exc:
                raise forms.ValidationError(public_message(exc.code)) from None
            self.instance = Department.objects.get(pk=created["id"])
        return cleaned


class DepartmentChangeForm(RevisionForm):
    class Meta:
        model = Department
        fields = ("name", "description", "is_active")

    def clean(self):
        cleaned = super().clean()
        if self.instance.pk is None or self.errors:
            return cleaned
        changes = {
            key: cleaned[key]
            for key in ("name", "description", "is_active")
            if key in cleaned
        }
        try:
            services.update_department(
                actor_id=self.actor_id,
                revision=cleaned.get("revision"),
                department_id=self.instance.pk,
                changes=changes,
            )
        except ManagementError as exc:
            raise forms.ValidationError(public_message(exc.code)) from None
        self.instance.refresh_from_db()
        return cleaned


@admin.register(Department)
class ManagedDepartmentAdmin(ServiceBackedAdmin):
    form = DepartmentChangeForm
    add_form = DepartmentAddForm
    fieldsets = (
        (None, {"fields": ("code", "revision")}),
        ("Details", {"fields": ("name", "description")}),
        ("Status", {"fields": ("is_active",)}),
    )
    add_fieldsets = (
        (
            None,
            {
                "classes": ("wide",),
                "fields": ("name", "description", "revision"),
            },
        ),
    )
    list_display = ("code", "name", "is_active")
    list_filter = ("is_active",)
    search_fields = ("code", "name")
    ordering = ("code",)

    def get_queryset(self, request):
        queryset = super().get_queryset(request)
        if not is_superadmin(request.user):
            return queryset.none()
        return queryset

    def has_module_permission(self, request) -> bool:
        return is_superadmin(request.user)

    def has_view_permission(self, request, obj=None) -> bool:
        return is_superadmin(request.user)

    def has_add_permission(self, request) -> bool:
        return is_superadmin(request.user)

    def has_change_permission(self, request, obj=None) -> bool:
        return is_superadmin(request.user)

    def get_readonly_fields(self, request, obj=None):
        return ("code",) if obj is not None else ()

    def get_fieldsets(self, request, obj=None):
        return self.add_fieldsets if obj is None else self.fieldsets

    def get_form(self, request, obj=None, change=False, **kwargs):
        kwargs["form"] = DepartmentChangeForm if change else DepartmentAddForm
        if not change:
            kwargs.pop("fields", None)
        form_class = super().get_form(request, obj, change, **kwargs)
        return _bound(form_class, actor_id=request.user.pk)


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


@admin.register(DepartmentMembership)
class MembershipAdmin(ReadOnlyAdmin):
    list_display = ("user", "department", "membership_role", "is_active")
    list_filter = ("membership_role", "is_active")
    search_fields = ("user__username", "department__code")


@admin.register(AuthorizationState)
class AuthorizationStateAdmin(ReadOnlyAdmin):
    list_display = ("id", "revision", "epoch", "updated_at")
    readonly_fields = ("id", "revision", "epoch", "updated_at")

    def has_view_permission(self, request, obj=None) -> bool:
        return is_superadmin(request.user)


@admin.register(AuthorizationAudit)
class AuditAdmin(ReadOnlyAdmin):
    list_display = ("created_at", "action", "actor", "target_type", "allowed")
    list_filter = ("action", "allowed")
    search_fields = ("target_type", "target_id", "action")
    readonly_fields = tuple(field.name for field in AuthorizationAudit._meta.fields)

    def get_queryset(self, request):
        if not request.user.has_perm("accounts.view_audit"):
            return AuthorizationAudit.objects.none()
        return super().get_queryset(request).select_related("actor")

    def has_view_permission(self, request, obj=None) -> bool:
        return is_superadmin(request.user) and request.user.has_perm(
            "accounts.view_audit"
        )


@admin.register(Group)
class ReadOnlyGroupAdmin(ReadOnlyAdmin):
    list_display = ("name",)
    search_fields = ("name",)
    ordering = ("name",)

    def get_queryset(self, request):
        if not is_superadmin(request.user):
            return Group.objects.none()
        return super().get_queryset(request).prefetch_related("permissions")


@admin.register(Permission)
class ReadOnlyPermissionAdmin(ReadOnlyAdmin):
    list_display = ("name", "content_type", "codename")
    search_fields = ("name", "codename")
    ordering = ("content_type", "codename")

    def get_queryset(self, request):
        if not is_superadmin(request.user):
            return Permission.objects.none()
        return super().get_queryset(request).select_related("content_type")
