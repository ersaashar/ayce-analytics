"""HTTP APIs for the discovered dataset catalog and its local controls."""

from __future__ import annotations

import contextlib
import functools
from collections.abc import Callable
from typing import Any, Final
from uuid import UUID

from asgiref.sync import async_to_sync
from django.db.models import Q
from django.http import HttpRequest, HttpResponse
from django.views.decorators.csrf import csrf_protect

from accounts import services as account_services
from accounts.authorization import (
    ManagementError,
    is_superadmin,
    led_department_ids,
    require_permission,
)
from accounts.http import (
    entity_response,
    list_params,
    list_response,
    method_response,
    mutation_response,
    read_object,
)
from accounts.models import Department
from chat.views import _json_response, error_response

from .discovery import configured_source_key
from .models import Dataset
from .permissions import MANAGE_CATALOG
from .services import (
    catalog_status as get_catalog_status,
)
from .services import (
    dataset_payload,
    refresh_catalog,
    update_dataset,
)

DENIAL_CODES: Final = frozenset({"permission_denied", "resource_not_found"})


def _endpoint(action: str) -> Callable:
    def decorate(view: Callable) -> Callable:
        @functools.wraps(view)
        def wrapper(request: HttpRequest, *args: Any, **kwargs: Any) -> HttpResponse:
            try:
                return view(request, *args, **kwargs)
            except ManagementError as exc:
                if exc.code in DENIAL_CODES and request.user.is_authenticated:
                    # A failed audit append must never mask the denial itself.
                    with contextlib.suppress(Exception):
                        account_services.record_denial(
                            actor_id=request.user.pk,
                            action=action,
                            reason_code=exc.code,
                        )
                return error_response(exc.code)

        return wrapper

    return decorate


def _revision(payload: dict[str, Any]) -> int:
    value = account_services.valid_state_value(payload.get("revision"))
    if value is None:
        raise ManagementError("invalid_management_request")
    return value


def _require_catalog_admin(request: HttpRequest) -> None:
    _require_superadmin(request)
    require_permission(actor=request.user, permission=MANAGE_CATALOG)


def _require_superadmin(request: HttpRequest) -> None:
    if not is_superadmin(request.user):
        raise ManagementError("permission_denied")


def _led_department(request: HttpRequest, department_id: UUID) -> Department:
    department = Department.objects.filter(pk=department_id).first()
    led = led_department_ids(request.user).filter(department_id=department_id).exists()
    if department is None or not led:
        raise ManagementError("resource_not_found")
    return department


@csrf_protect
@_endpoint("datasets.collection")
def datasets_collection(request: HttpRequest) -> HttpResponse:
    """List discovered models within the caller's current source and scope."""
    if not request.user.is_authenticated:
        return error_response("authentication_required")
    rejected = method_response(request, "GET")
    if rejected is not None:
        return rejected

    params = list_params(request, extra=frozenset({"department_id"}))
    department: Department | None = None
    if is_superadmin(request.user):
        _require_catalog_admin(request)
    else:
        raw = request.GET.get("department_id")
        if not raw:
            raise ManagementError("invalid_management_request")
        department = _led_department(request, account_services.clean_uuid(raw))

    source_key = configured_source_key()
    rows = Dataset.objects.none()
    if source_key is not None:
        rows = Dataset.objects.filter(
            source_key=source_key,
            is_available=True,
        )
        if department is not None:
            rows = rows.filter(
                department_grants__department=department,
                is_active=True,
                department_grants__needs_review=False,
            )
    rows = rows.order_by("code", "id")
    if params.q:
        rows = rows.filter(
            Q(code__icontains=params.q) | Q(display_name__icontains=params.q)
        )
    total = rows.count()
    window = rows[(params.page - 1) * params.page_size : params.page * params.page_size]
    items = []
    for dataset in window:
        payload = dataset_payload(dataset)
        if department is not None:
            payload["department_ids"] = [str(department.pk)]
        items.append(payload)
    state = account_services.current_state()
    return list_response(
        items,
        params=params,
        total=total,
        revision=state["revision"],
        epoch=state["epoch"],
    )


@csrf_protect
@_endpoint("datasets.catalog_status")
def catalog_status_view(request: HttpRequest) -> HttpResponse:
    if not request.user.is_authenticated:
        return error_response("authentication_required")
    rejected = method_response(request, "GET")
    if rejected is not None:
        return rejected
    _require_catalog_admin(request)
    if request.GET:
        raise ManagementError("invalid_management_request")
    status = get_catalog_status(actor_id=request.user.pk)
    return _json_response(
        {
            "result": {
                key: status[key]
                for key in (
                    "configured",
                    "last_attempt_at",
                    "last_success_at",
                    "error_code",
                )
            },
            "revision": status["revision"],
            "epoch": status["epoch"],
        }
    )


@csrf_protect
@_endpoint("datasets.catalog_refresh")
def catalog_refresh(request: HttpRequest) -> HttpResponse:
    if not request.user.is_authenticated:
        return error_response("authentication_required")
    rejected = method_response(request, "POST")
    if rejected is not None:
        return rejected
    _require_catalog_admin(request)
    if request.GET:
        raise ManagementError("invalid_management_request")
    payload = read_object(request, keys={"revision"})
    result = async_to_sync(refresh_catalog)(
        actor_id=request.user.pk,
        revision=_revision(payload),
    )
    return mutation_response("result", result)


@csrf_protect
@_endpoint("datasets.detail")
def dataset_detail(request: HttpRequest, dataset_id: UUID) -> HttpResponse:
    """Read or change the local enabled state of one discovered model."""
    if not request.user.is_authenticated:
        return error_response("authentication_required")
    rejected = method_response(request, "GET, PATCH")
    if rejected is not None:
        return rejected
    _require_catalog_admin(request)

    source_key = configured_source_key()
    dataset = (
        Dataset.objects.filter(
            pk=dataset_id,
            source_key=source_key,
            is_available=True,
        ).first()
        if source_key is not None
        else None
    )
    if dataset is None:
        raise ManagementError("resource_not_found")

    if request.method == "GET":
        state = account_services.current_state()
        return entity_response(
            "dataset",
            {
                **dataset_payload(dataset),
                "revision": state["revision"],
                "epoch": state["epoch"],
            },
        )

    payload = read_object(request, keys={"revision", "is_active"})
    if not isinstance(payload["is_active"], bool):
        raise ManagementError("invalid_management_request")
    updated = update_dataset(
        actor_id=request.user.pk,
        revision=_revision(payload),
        dataset_id=dataset.pk,
        changes={"is_active": payload["is_active"]},
    )
    return mutation_response("dataset", updated)
