"""Shared HTTP plumbing for the management and self-service APIs.

The chat application already owns the one JSON error envelope and the strict
body parser; these helpers adapt management failures onto them instead of
introducing a second wire contract.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Final

from django.http import HttpRequest, JsonResponse

from chat.service import ChatError
from chat.views import (
    _json_response,
    _method_not_allowed,
    _read_json_object,
    error_response,
)

from .authorization import ManagementError

MAX_PAGE_SIZE: Final = 100
DEFAULT_PAGE_SIZE: Final = 25
MAX_QUERY_LENGTH: Final = 200
MAX_REPLACEMENT_ENTRIES: Final = 1000

_DECIMAL: Final = re.compile(r"^(?:0|[1-9][0-9]*)$")
_LIST_KEYS: Final = frozenset({"q", "page", "page_size"})


@dataclass(frozen=True)
class ListParams:
    """Validated list query values shared by every paginated endpoint."""

    q: str
    page: int
    page_size: int


def read_object(
    request: HttpRequest,
    *,
    keys: frozenset[str] | set[str],
    optional_keys: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """Parse a strict JSON body, mapping every failure onto one public code."""
    try:
        return _read_json_object(
            request,
            code="invalid_management_request",
            keys=keys,
            optional_keys=optional_keys,
        )
    except ChatError as exc:
        raise ManagementError(exc.code) from None


def failure(exc: ManagementError) -> JsonResponse:
    """Adapt a management failure onto the application's error envelope."""
    return error_response(exc.code)


def method_response(request: HttpRequest, allow: str) -> JsonResponse | None:
    """Return the fixed 405 when the method is not one of ``allow``."""
    if request.method in {entry.strip() for entry in allow.split(",")}:
        return None
    return _method_not_allowed(allow)


def authenticated(request: HttpRequest) -> JsonResponse | None:
    """Require an authenticated principal before any management work."""
    if not request.user.is_authenticated:
        return error_response("authentication_required")
    return None


def positive_int(raw: object) -> int | None:
    """Parse canonical decimal digits into a positive integer."""
    if not isinstance(raw, str) or not _DECIMAL.match(raw):
        return None
    value = int(raw)
    return value if value >= 1 else None


def list_params(
    request: HttpRequest, *, extra: frozenset[str] = frozenset()
) -> ListParams:
    """Validate ``q``/``page``/``page_size`` and reject unknown query keys."""
    query = request.GET
    if set(query.keys()) - (_LIST_KEYS | set(extra)):
        raise ManagementError("invalid_management_request")
    q = (query.get("q") or "").strip()
    if len(q) > MAX_QUERY_LENGTH:
        raise ManagementError("invalid_management_request")

    page_raw = query.get("page")
    page = 1 if page_raw in (None, "") else positive_int(page_raw)
    if page is None:
        raise ManagementError("invalid_management_request")
    size_raw = query.get("page_size")
    page_size = DEFAULT_PAGE_SIZE if size_raw in (None, "") else positive_int(size_raw)
    if page_size is None or page_size > MAX_PAGE_SIZE:
        raise ManagementError("invalid_management_request")
    return ListParams(q=q, page=page, page_size=page_size)


def page_rows(rows: list[Any], *, params: ListParams) -> list[Any]:
    start = (params.page - 1) * params.page_size
    return rows[start : start + params.page_size]


def list_response(
    items: list[dict[str, Any]],
    *,
    params: ListParams,
    total: int,
    revision: int,
    epoch: int,
) -> JsonResponse:
    return _json_response(
        {
            "items": items,
            "page": params.page,
            "page_size": params.page_size,
            "total": total,
            "revision": revision,
            "epoch": epoch,
        }
    )


STATE_KEYS: Final = frozenset({"revision", "epoch"})


def entity_response(key: str, payload: dict[str, Any]) -> JsonResponse:
    """Split one service payload into its public entity and state values."""
    data = {name: value for name, value in payload.items() if name not in STATE_KEYS}
    return _json_response(
        {
            key: data,
            "revision": payload.get("revision", 0),
            "epoch": payload.get("epoch", 0),
        }
    )


def mutation_response(
    key: str, payload: dict[str, Any], *, status: int = 200
) -> JsonResponse:
    data = {name: value for name, value in payload.items() if name not in STATE_KEYS}
    return _json_response(
        {
            key: data,
            "revision": payload.get("revision", 0),
            "epoch": payload.get("epoch", 0),
        },
        status=status,
    )
