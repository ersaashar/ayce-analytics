"""Management page and audited administration/self-service APIs."""

from __future__ import annotations

import contextlib
import functools
from collections.abc import Callable
from typing import Any, Final
from uuid import UUID

from django.contrib.auth.models import User
from django.contrib.auth.views import redirect_to_login
from django.db.models import F, Q
from django.http import Http404, HttpRequest, HttpResponse
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.csrf import csrf_protect

from chat.mcp_policy import eligible_dev_department_ids
from chat.views import _json_response, error_response
from datasets.discovery import configured_source_key
from datasets.models import DepartmentDataset
from datasets.services import (
    dataset_availability,
    replace_department_datasets,
    replace_membership_datasets,
)

from . import services
from .authorization import (
    CAPABILITIES,
    ManagementError,
    active_dataset_queryset,
    can_use_management,
    capability_map,
    fixed_roles,
    is_superadmin,
    led_department_ids,
    user_projection,
)
from .http import (
    MAX_REPLACEMENT_ENTRIES,
    ListParams,
    entity_response,
    list_params,
    list_response,
    method_response,
    mutation_response,
    read_object,
)
from .models import AuthorizationAudit, Department, DepartmentMembership
from .permissions import FIXED_GROUPS, ROLE_LABELS, SUPERADMIN_GROUP, USE_ANALYTICS

MANAGEMENT_SECTIONS: Final = ("users", "departments", "datasets", "roles", "audit")
SUPERADMIN_SECTIONS: Final = frozenset(MANAGEMENT_SECTIONS)
LEAD_SECTIONS: Final = frozenset({"departments", "datasets"})
DENIAL_CODES: Final = frozenset({"permission_denied", "resource_not_found"})


def _endpoint(action: str) -> Callable:
    """Map management failures onto the public envelope and audit denials."""

    def decorate(view: Callable) -> Callable:
        @functools.wraps(view)
        def wrapper(request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
            try:
                return view(request, *args, **kwargs)
            except ManagementError as exc:
                if exc.code in DENIAL_CODES and request.user.is_authenticated:
                    with contextlib.suppress(Exception):
                        services.record_denial(
                            actor_id=request.user.pk,
                            action=action,
                            reason_code=exc.code,
                        )
                return error_response(exc.code)

        return wrapper

    return decorate


def _revision(payload: dict[str, Any]) -> int:
    value = services.valid_state_value(payload.get("revision"))
    if value is None:
        raise ManagementError("invalid_management_request")
    return value


def _text(value: Any) -> str:
    if not isinstance(value, str):
        raise ManagementError("invalid_management_request")
    return value


def _flag(value: Any) -> bool:
    if not isinstance(value, bool):
        raise ManagementError("invalid_management_request")
    return value


def _uuid(value: Any) -> UUID:
    return services.clean_uuid(value)


def _uuid_list(value: Any) -> tuple[UUID, ...]:
    if not isinstance(value, list) or len(value) > MAX_REPLACEMENT_ENTRIES:
        raise ManagementError("invalid_management_request")
    ids = tuple(services.clean_uuid(entry) for entry in value)
    if len(set(ids)) != len(ids):
        raise ManagementError("invalid_management_request")
    return ids


def _membership_inputs(value: Any) -> list[services.MembershipInput]:
    if not isinstance(value, list) or len(value) > MAX_REPLACEMENT_ENTRIES:
        raise ManagementError("invalid_management_request")
    rows: list[services.MembershipInput] = []
    for entry in value:
        if not isinstance(entry, dict) or set(entry) != {
            "department_id",
            "membership_role",
            "is_active",
            "dataset_ids",
        }:
            raise ManagementError("invalid_management_request")
        role = entry["membership_role"]
        if role not in {"staff", "lead"}:
            raise ManagementError("invalid_management_request")
        rows.append(
            services.MembershipInput(
                department_id=_uuid(entry["department_id"]),
                membership_role=role,
                is_active=_flag(entry["is_active"]),
                dataset_ids=_uuid_list(entry["dataset_ids"]),
            )
        )
    if len({row.department_id for row in rows}) != len(rows):
        raise ManagementError("invalid_management_request")
    return rows


def _department_member_inputs(value: Any) -> list[services.DepartmentMemberInput]:
    if not isinstance(value, list) or len(value) > MAX_REPLACEMENT_ENTRIES:
        raise ManagementError("invalid_management_request")
    rows: list[services.DepartmentMemberInput] = []
    for entry in value:
        if not isinstance(entry, dict) or set(entry) != {
            "user_id",
            "membership_role",
            "is_active",
            "dataset_ids",
        }:
            raise ManagementError("invalid_management_request")
        role = entry["membership_role"]
        if role not in {"staff", "lead"}:
            raise ManagementError("invalid_management_request")
        user_id = entry["user_id"]
        if isinstance(user_id, bool) or not isinstance(user_id, int):
            raise ManagementError("invalid_management_request")
        rows.append(
            services.DepartmentMemberInput(
                user_id=user_id,
                membership_role=role,
                is_active=_flag(entry["is_active"]),
                dataset_ids=_uuid_list(entry["dataset_ids"]),
            )
        )
    if len({row.user_id for row in rows}) != len(rows):
        raise ManagementError("invalid_management_request")
    return rows


def _require_superadmin(request: HttpRequest) -> None:
    if not is_superadmin(request.user):
        raise ManagementError("permission_denied")


def _department_scope(request: HttpRequest, department_id: UUID) -> bool:
    """Authorize one department, returning whether the actor is global."""
    if is_superadmin(request.user):
        return True
    led = led_department_ids(request.user).filter(department_id=department_id).exists()
    if not led:
        raise ManagementError("resource_not_found")
    return False


def _load_department(department_id: UUID) -> Department:
    department = Department.objects.filter(pk=department_id).first()
    if department is None:
        raise ManagementError("resource_not_found")
    return department


def _load_user(user_id: int) -> User:
    user = User.objects.filter(pk=user_id).first()
    if user is None:
        raise ManagementError("resource_not_found")
    return user


def _role_definitions() -> list[dict[str, Any]]:
    return [
        {
            "name": name,
            "capabilities": [
                capability
                for capability, permission in CAPABILITIES.items()
                if permission in FIXED_GROUPS[name]
            ],
            "uses_analytics": USE_ANALYTICS in FIXED_GROUPS[name],
        }
        for name in ROLE_LABELS
    ]


def _dataset_scope_departments(*, user_id: int) -> dict[UUID, list[str]]:
    from datasets.models import MembershipDataset

    mapping: dict[UUID, list[str]] = {}
    rows = MembershipDataset.objects.filter(
        membership__user_id=user_id,
        membership__is_active=True,
        membership__department__is_active=True,
        department_grant__department__is_active=True,
        department_grant__dataset__is_active=True,
        department_grant__dataset__is_available=True,
        department_grant__dataset__source_key=configured_source_key(),
        department_grant__needs_review=False,
        department_grant__department_id=F("membership__department_id"),
    ).values_list("department_grant__dataset_id", "membership__department_id")
    for dataset_id, department_id in rows:
        mapping.setdefault(dataset_id, [])
        label = str(department_id)
        if label not in mapping[dataset_id]:
            mapping[dataset_id].append(label)
    return mapping


@csrf_protect
def me(request: HttpRequest) -> HttpResponse:
    """Current principal, fixed roles, capabilities and administration state."""
    if not request.user.is_authenticated:
        return error_response("authentication_required")
    rejected = method_response(request, "GET")
    if rejected is not None:
        return rejected
    state = services.current_state()
    eligible_ids = eligible_dev_department_ids(user_id=request.user.pk)
    return _json_response(
        {
            "user": user_projection(request.user),
            "roles": fixed_roles(request.user),
            "capabilities": capability_map(request.user),
            "analytics_enabled": bool(eligible_ids),
            "revision": state["revision"],
            "epoch": state["epoch"],
        }
    )


@csrf_protect
@_endpoint("me.departments")
def my_departments(request: HttpRequest) -> HttpResponse:
    """Active memberships of the signed-in account."""
    if not request.user.is_authenticated:
        return error_response("authentication_required")
    rejected = method_response(request, "GET")
    if rejected is not None:
        return rejected
    params = list_params(request)
    items = services.self_service_memberships(user_id=request.user.pk)
    if params.q:
        needle = params.q.lower()
        items = [
            item
            for item in items
            if needle in item["department"]["code"].lower()
            or needle in item["department"]["name"].lower()
            or needle in item["membership_role"]
        ]
    state = services.current_state()
    return list_response(
        _page(items, params),
        params=params,
        total=len(items),
        revision=state["revision"],
        epoch=state["epoch"],
    )


@csrf_protect
@_endpoint("me.datasets")
def my_datasets(request: HttpRequest) -> HttpResponse:
    """Datasets this account may currently see, from explicit state only."""
    if not request.user.is_authenticated:
        return error_response("authentication_required")
    rejected = method_response(request, "GET")
    if rejected is not None:
        return rejected
    params = list_params(request, extra=frozenset({"department_id"}))
    department_id: UUID | None = None
    raw = request.GET.get("department_id")
    if raw:
        department_id = _uuid(raw)
        membership = DepartmentMembership.objects.filter(
            user_id=request.user.pk,
            department_id=department_id,
            is_active=True,
            department__is_active=True,
        ).exists()
        if not membership:
            raise ManagementError("resource_not_found")

    rows = active_dataset_queryset(user_id=request.user.pk, department_id=department_id)
    if params.q:
        rows = rows.filter(
            Q(code__icontains=params.q)
            | Q(display_name__icontains=params.q)
            | Q(description__icontains=params.q)
        )
    total = rows.count()
    window = list(
        rows[(params.page - 1) * params.page_size : params.page * params.page_size]
    )
    scope = _dataset_scope_departments(user_id=request.user.pk)
    items = [
        {
            "id": str(dataset.pk),
            "code": dataset.code,
            "display_name": dataset.display_name,
            "description": dataset.description,
            "department_ids": scope.get(dataset.pk, []),
        }
        for dataset in window
    ]
    state = services.current_state()
    return list_response(
        items,
        params=params,
        total=total,
        revision=state["revision"],
        epoch=state["epoch"],
    )


def _page(items: list[Any], params: ListParams) -> list[Any]:
    start = (params.page - 1) * params.page_size
    return items[start : start + params.page_size]


@csrf_protect
@_endpoint("users.collection")
def admin_users(request: HttpRequest) -> HttpResponse:
    """List accounts, or create one with the Staff base role."""
    if not request.user.is_authenticated:
        return error_response("authentication_required")
    rejected = method_response(request, "GET, POST")
    if rejected is not None:
        return rejected
    _require_superadmin(request)

    if request.method == "GET":
        params = list_params(request)
        rows = User.objects.all().order_by("username", "id")
        if params.q:
            rows = rows.filter(
                Q(username__icontains=params.q)
                | Q(first_name__icontains=params.q)
                | Q(last_name__icontains=params.q)
            )
        total = rows.count()
        window = rows[
            (params.page - 1) * params.page_size : params.page * params.page_size
        ]
        state = services.current_state()
        return list_response(
            [services.user_payload(user) for user in window],
            params=params,
            total=total,
            revision=state["revision"],
            epoch=state["epoch"],
        )

    payload = read_object(
        request,
        keys={"revision", "username", "first_name", "last_name", "email", "password"},
    )
    created = services.create_user(
        actor_id=request.user.pk,
        revision=_revision(payload),
        username=_text(payload["username"]),
        first_name=_text(payload["first_name"]),
        last_name=_text(payload["last_name"]),
        email=_text(payload["email"]),
        password=_text(payload["password"]),
    )
    return mutation_response("user", created, status=201)


@csrf_protect
@_endpoint("users.detail")
def admin_user_detail(request: HttpRequest, user_id: int) -> HttpResponse:
    """Read or edit one account's safe fields."""
    if not request.user.is_authenticated:
        return error_response("authentication_required")
    rejected = method_response(request, "GET, PATCH")
    if rejected is not None:
        return rejected
    _require_superadmin(request)
    target = _load_user(user_id)
    if request.method == "GET":
        state = services.current_state()
        return entity_response(
            "user",
            {
                **services.user_payload(target),
                "memberships_detail": services.membership_rows(user_id=target.pk),
                "revision": state["revision"],
                "epoch": state["epoch"],
            },
        )

    payload = read_object(
        request,
        keys={"revision"},
        optional_keys=frozenset({"first_name", "last_name", "email", "is_active"}),
    )
    changes = {
        key: payload[key]
        for key in ("first_name", "last_name", "email", "is_active")
        if key in payload
    }
    updated = services.update_user(
        actor_id=request.user.pk,
        revision=_revision(payload),
        user_id=target.pk,
        changes=changes,
    )
    return mutation_response("user", updated)


@csrf_protect
@_endpoint("users.password")
def admin_user_password(request: HttpRequest, user_id: int) -> HttpResponse:
    """Reset one account's password after re-authenticating the actor."""
    if not request.user.is_authenticated:
        return error_response("authentication_required")
    rejected = method_response(request, "POST")
    if rejected is not None:
        return rejected
    _require_superadmin(request)
    target = _load_user(user_id)
    payload = read_object(
        request, keys={"revision", "current_password", "new_password"}
    )
    updated = services.reset_password(
        actor_id=request.user.pk,
        revision=_revision(payload),
        user_id=target.pk,
        current_password=_text(payload["current_password"]),
        new_password=_text(payload["new_password"]),
    )
    return mutation_response("user", updated)


@csrf_protect
@_endpoint("users.roles")
def admin_user_roles(request: HttpRequest, user_id: int) -> HttpResponse:
    """Read or change the fixed base role of one account."""
    if not request.user.is_authenticated:
        return error_response("authentication_required")
    rejected = method_response(request, "GET, PUT")
    if rejected is not None:
        return rejected
    _require_superadmin(request)
    target = _load_user(user_id)

    if request.method == "GET":
        state = services.current_state()
        return _json_response(
            {
                "user": services.user_payload(target),
                "roles": _role_definitions(),
                "revision": state["revision"],
                "epoch": state["epoch"],
            }
        )

    payload = read_object(request, keys={"revision", "role"})
    updated = services.set_global_role(
        actor_id=request.user.pk,
        revision=_revision(payload),
        user_id=target.pk,
        role=_text(payload["role"]),
    )
    return mutation_response("user", updated)


@csrf_protect
@_endpoint("users.memberships")
def admin_user_memberships(request: HttpRequest, user_id: int) -> HttpResponse:
    """Read or replace one account's complete membership set."""
    if not request.user.is_authenticated:
        return error_response("authentication_required")
    rejected = method_response(request, "GET, PUT")
    if rejected is not None:
        return rejected
    _require_superadmin(request)
    target = _load_user(user_id)

    if request.method == "GET":
        state = services.current_state()
        return _json_response(
            {
                "user": {
                    **services.user_payload(target),
                    "memberships_detail": services.membership_rows(user_id=target.pk),
                },
                "revision": state["revision"],
                "epoch": state["epoch"],
            }
        )

    payload = read_object(request, keys={"revision", "memberships"})
    updated = services.replace_user_memberships(
        actor_id=request.user.pk,
        revision=_revision(payload),
        user_id=target.pk,
        memberships=_membership_inputs(payload["memberships"]),
    )
    return mutation_response("user", updated)


@csrf_protect
@_endpoint("roles.collection")
def admin_roles(request: HttpRequest) -> HttpResponse:
    """The three fixed native groups and the capabilities they carry."""
    if not request.user.is_authenticated:
        return error_response("authentication_required")
    rejected = method_response(request, "GET")
    if rejected is not None:
        return rejected
    _require_superadmin(request)
    state = services.current_state()
    return _json_response(
        {
            "roles": _role_definitions(),
            "revision": state["revision"],
            "epoch": state["epoch"],
        }
    )


@csrf_protect
@_endpoint("departments.collection")
def admin_departments(request: HttpRequest) -> HttpResponse:
    """List departments (scoped for Leads) and create them for Superadmins."""
    if not request.user.is_authenticated:
        return error_response("authentication_required")
    rejected = method_response(request, "GET, POST")
    if rejected is not None:
        return rejected

    if request.method == "POST":
        _require_superadmin(request)
        payload = read_object(request, keys={"revision", "name", "description"})
        created = services.create_department(
            actor_id=request.user.pk,
            revision=_revision(payload),
            name=_text(payload["name"]),
            description=_text(payload["description"]),
        )
        return mutation_response("department", created, status=201)

    if not can_use_management(request.user):
        raise ManagementError("permission_denied")
    params = list_params(request)
    rows = Department.objects.all().order_by("code", "id")
    if not is_superadmin(request.user):
        rows = rows.filter(pk__in=led_department_ids(request.user), is_active=True)
    if params.q:
        rows = rows.filter(Q(code__icontains=params.q) | Q(name__icontains=params.q))
    total = rows.count()
    window = rows[(params.page - 1) * params.page_size : params.page * params.page_size]
    state = services.current_state()
    return list_response(
        [services.department_payload(department) for department in window],
        params=params,
        total=total,
        revision=state["revision"],
        epoch=state["epoch"],
    )


@csrf_protect
@_endpoint("departments.detail")
def admin_department_detail(request: HttpRequest, department_id: UUID) -> HttpResponse:
    """Read one department (scoped) or edit it (Superadmin only)."""
    if not request.user.is_authenticated:
        return error_response("authentication_required")
    rejected = method_response(request, "GET, PATCH")
    if rejected is not None:
        return rejected
    if request.method == "PATCH":
        _require_superadmin(request)
    else:
        _department_scope(request, department_id)
    department = _load_department(department_id)
    if request.method == "GET":
        if not is_superadmin(request.user) and not department.is_active:
            raise ManagementError("resource_not_found")
        state = services.current_state()
        return entity_response(
            "department",
            {
                **services.department_payload(department),
                "revision": state["revision"],
                "epoch": state["epoch"],
            },
        )

    payload = read_object(
        request,
        keys={"revision"},
        optional_keys=frozenset({"name", "description", "is_active"}),
    )
    changes = {
        key: payload[key]
        for key in ("name", "description", "is_active")
        if key in payload
    }
    updated = services.update_department(
        actor_id=request.user.pk,
        revision=_revision(payload),
        department_id=department.pk,
        changes=changes,
    )
    return mutation_response("department", updated)


@csrf_protect
@_endpoint("departments.members")
def admin_department_members(request: HttpRequest, department_id: UUID) -> HttpResponse:
    """Read or replace a department roster within the caller's scope."""
    if not request.user.is_authenticated:
        return error_response("authentication_required")
    rejected = method_response(request, "GET, PUT")
    if rejected is not None:
        return rejected
    _department_scope(request, department_id)
    department = _load_department(department_id)

    if request.method == "GET":
        state = services.current_state()
        return _json_response(
            {
                "department": services.department_payload(department),
                "members": services.roster_members(department_id=department.pk),
                "revision": state["revision"],
                "epoch": state["epoch"],
            }
        )

    payload = read_object(request, keys={"revision", "members"})
    updated = services.replace_department_members(
        actor_id=request.user.pk,
        revision=_revision(payload),
        department_id=department.pk,
        members=_department_member_inputs(payload["members"]),
    )
    return _json_response(
        {
            "department": updated["department"],
            "members": updated["members"],
            "revision": updated["revision"],
            "epoch": updated["epoch"],
        }
    )


@csrf_protect
@_endpoint("departments.candidates")
def admin_department_candidates(
    request: HttpRequest, department_id: UUID
) -> HttpResponse:
    """Search minimal identities that may be added to this department."""
    if not request.user.is_authenticated:
        return error_response("authentication_required")
    rejected = method_response(request, "GET")
    if rejected is not None:
        return rejected
    _department_scope(request, department_id)

    params = list_params(request)
    if len(params.q) < 2:
        raise ManagementError("invalid_management_request")
    rows = (
        User.objects.filter(is_active=True)
        .exclude(is_superuser=True)
        .exclude(groups__name=SUPERADMIN_GROUP)
        .exclude(
            department_memberships__department_id=department_id,
            department_memberships__membership_role="lead",
        )
        .filter(
            Q(username__icontains=params.q)
            | Q(first_name__icontains=params.q)
            | Q(last_name__icontains=params.q)
        )
        .distinct()
        .order_by("username", "id")
    )
    total = rows.count()
    window = rows[(params.page - 1) * params.page_size : params.page * params.page_size]
    state = services.current_state()
    return list_response(
        [
            {
                "id": user.pk,
                "username": user.get_username(),
                "first_name": user.first_name or "",
                "last_name": user.last_name or "",
            }
            for user in window
        ],
        params=params,
        total=total,
        revision=state["revision"],
        epoch=state["epoch"],
    )


@csrf_protect
@_endpoint("departments.datasets")
def admin_department_datasets(
    request: HttpRequest, department_id: UUID
) -> HttpResponse:
    """Read one department's grants, or replace them as a Superadmin."""
    if not request.user.is_authenticated:
        return error_response("authentication_required")
    rejected = method_response(request, "GET, PUT")
    if rejected is not None:
        return rejected
    _department_scope(request, department_id)
    department = _load_department(department_id)

    if request.method == "GET":
        grants = (
            DepartmentDataset.objects.filter(department=department)
            .select_related("dataset")
            .order_by("dataset__code", "id")
        )
        items = [
            {
                "id": str(grant.dataset_id),
                "display_name": (
                    "Unavailable dataset"
                    if dataset_availability(grant.dataset, grant=grant)
                    == "different_source"
                    else grant.dataset.display_name
                ),
                "availability": dataset_availability(grant.dataset, grant=grant),
            }
            for grant in grants
        ]
        state = services.current_state()
        return _json_response(
            {
                "department": services.department_payload(department),
                "datasets": items,
                "revision": state["revision"],
                "epoch": state["epoch"],
            }
        )

    payload = read_object(
        request,
        keys={"revision", "dataset_ids"},
        optional_keys=frozenset({"confirmed_dataset_ids"}),
    )
    updated = replace_department_datasets(
        actor_id=request.user.pk,
        revision=_revision(payload),
        department_id=department.pk,
        dataset_ids=_uuid_list(payload["dataset_ids"]),
        confirmed_dataset_ids=_uuid_list(payload.get("confirmed_dataset_ids", [])),
    )
    return mutation_response("result", updated)


@csrf_protect
@_endpoint("memberships.datasets")
def admin_membership_datasets(
    request: HttpRequest, department_id: UUID, user_id: int
) -> HttpResponse:
    """Read or replace one member's explicit dataset subset."""
    if not request.user.is_authenticated:
        return error_response("authentication_required")
    rejected = method_response(request, "GET, PUT")
    if rejected is not None:
        return rejected
    global_scope = _department_scope(request, department_id)
    department = _load_department(department_id)
    membership = (
        DepartmentMembership.objects.filter(
            department=department, user_id=user_id, is_active=True
        )
        .select_related("user")
        .first()
    )
    if membership is None:
        raise ManagementError("resource_not_found")
    if not global_scope:
        if membership.membership_role != "staff":
            raise ManagementError("permission_denied")
        target = membership.user
        if target.is_superuser or target.groups.filter(name=SUPERADMIN_GROUP).exists():
            raise ManagementError("resource_not_found")

    if request.method == "GET":
        from datasets.models import MembershipDataset

        dataset_ids = [
            str(value)
            for value in MembershipDataset.objects.filter(
                membership=membership,
            )
            .order_by("department_grant__dataset__code")
            .values_list("department_grant__dataset_id", flat=True)
        ]
        state = services.current_state()
        return _json_response(
            {
                "membership": {
                    "id": str(membership.pk),
                    "department": {
                        "id": str(department.pk),
                        "code": department.code,
                        "name": department.name,
                    },
                    "user": user_projection(membership.user),
                    "membership_role": membership.membership_role,
                    "dataset_ids": dataset_ids,
                },
                "revision": state["revision"],
                "epoch": state["epoch"],
            }
        )

    payload = read_object(request, keys={"revision", "dataset_ids"})
    updated = replace_membership_datasets(
        actor_id=request.user.pk,
        revision=_revision(payload),
        department_id=department.pk,
        user_id=membership.user_id,
        dataset_ids=_uuid_list(payload["dataset_ids"]),
    )
    return _json_response(
        {
            "membership": updated["membership"],
            "revision": updated["revision"],
            "epoch": updated["epoch"],
        }
    )


@csrf_protect
@_endpoint("audit.collection")
def admin_audit(request: HttpRequest) -> HttpResponse:
    """Paginated authorization audit records, newest first."""
    if not request.user.is_authenticated:
        return error_response("authentication_required")
    rejected = method_response(request, "GET")
    if rejected is not None:
        return rejected
    _require_superadmin(request)
    if not request.user.has_perm("accounts.view_audit"):
        raise ManagementError("permission_denied")
    params = list_params(request)
    rows = AuthorizationAudit.objects.select_related("actor").order_by(
        "-created_at", "-id"
    )
    if params.q:
        rows = rows.filter(
            Q(action__icontains=params.q)
            | Q(target_type__icontains=params.q)
            | Q(reason_code__icontains=params.q)
            | Q(actor__username__icontains=params.q)
        )
    total = rows.count()
    window = rows[(params.page - 1) * params.page_size : params.page * params.page_size]
    state = services.current_state()
    return list_response(
        [services.audit_payload(record) for record in window],
        params=params,
        total=total,
        revision=state["revision"],
        epoch=state["epoch"],
    )


def _denied_page() -> HttpResponse:
    return render(
        None,
        "accounts/denied.html",
        {},
        status=403,
    )


def _no_store(response: HttpResponse) -> HttpResponse:
    response["Cache-Control"] = "no-store"
    return response


def management_page(request: HttpRequest) -> HttpResponse:
    """Serve the single management HTML shell for authorized principals."""
    if not request.user.is_authenticated:
        return redirect_to_login(request.get_full_path(), reverse("login"))
    if not can_use_management(request.user):
        return _no_store(_denied_page())

    if set(request.GET.keys()) - {"section"}:
        raise Http404("No such management section.")
    section = request.GET.get("section") or (
        "departments" if not is_superadmin(request.user) else "users"
    )
    if section not in MANAGEMENT_SECTIONS:
        raise Http404("No such management section.")
    allowed = SUPERADMIN_SECTIONS if is_superadmin(request.user) else LEAD_SECTIONS
    if section not in allowed:
        return _no_store(_denied_page())

    state = services.current_state()
    bootstrap = {
        "user": user_projection(request.user),
        "capabilities": capability_map(request.user),
        "section": section,
        "revision": state["revision"],
        "epoch": state["epoch"],
        "urls": {
            "me": reverse("accounts:me"),
            "self_departments": reverse("accounts:my_departments"),
            "self_datasets": reverse("accounts:my_datasets"),
            "users": reverse("accounts:admin_users"),
            "departments": reverse("accounts:admin_departments"),
            "roles": reverse("accounts:admin_roles"),
            "audit": reverse("accounts:admin_audit"),
            "datasets": reverse("datasets:admin_datasets"),
            "chat": reverse("chat:index"),
        },
    }
    response = render(
        request,
        "accounts/management.html",
        {
            "bootstrap": bootstrap,
            "section_title": section.replace("_", " ").title(),
        },
    )
    return _no_store(response)
