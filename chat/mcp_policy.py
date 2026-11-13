"""Synthetic Jaffle DEV eligibility. No network calls."""

from __future__ import annotations

import json
from dataclasses import dataclass
from uuid import UUID

_CATALOG = "wren"
_SCHEMA = "public"
_DATA_SOURCE = "duckdb"
_MODELS = frozenset({"customers", "orders"})
_PHYSICAL = {
    "customers": ("jaffle_shop", "main", "customers"),
    "orders": ("jaffle_shop", "main", "orders"),
}
_RELATIONSHIP = "orders_customers"
_JOIN = "orders.customer_id = customers.customer_id"


@dataclass(frozen=True)
class DatasetUse:
    dataset_id: UUID
    source_key: str
    project_key: str
    model_name: str
    definition_sha256: str
    structure_sha256: str


@dataclass(frozen=True)
class DevScope:
    endpoint: str
    source_key: str
    project_key: str
    snapshot_sha256: str
    structure_sha256: str
    datasets: tuple[DatasetUse, ...]


@dataclass(frozen=True)
class AnalyticsRun:
    owner_id: int
    thread_id: UUID
    department_id: UUID | None
    authorization_epoch: int | None
    run_token: UUID


def _catalog_success(catalog: object) -> bool:
    return bool(
        catalog.last_success_at
        and not catalog.error_code
        and catalog.snapshot_sha256
        and catalog.structure_sha256
        and catalog.execution_metadata_json
    )


def _synthetic_snapshot(source_key: str, raw: str):
    from datasets.discovery import DiscoveryError, catalog_snapshot

    try:
        stored = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(stored, dict):
        return None
    project = stored.get("project")
    mdl = stored.get("mdl")
    if not isinstance(project, dict) or not isinstance(mdl, dict):
        return None
    try:
        project_text = json.dumps(project, allow_nan=False)
    except (TypeError, ValueError):
        return None
    try:
        return catalog_snapshot(source_key, project_text, mdl, project_text)
    except (DiscoveryError, ValueError, TypeError):
        return None


def _jaffle_identity(snapshot: object) -> bool:
    from config.settings import DEV_JAFFLE_PROJECT

    if snapshot.project_key != DEV_JAFFLE_PROJECT:
        return False
    if len(snapshot.models) != len(_MODELS):
        return False
    if {model.model_name for model in snapshot.models} != _MODELS:
        return False
    for model in snapshot.models:
        physical = (model.catalog, model.schema, model.physical_table)
        if physical != _PHYSICAL[model.model_name] or not model.definition_sha256:
            return False
    verified = json.loads(snapshot.execution_metadata_json)
    project = verified["project"]
    mdl = verified["mdl"]
    if (
        project.get("name"),
        project.get("catalog"),
        project.get("schema"),
        project.get("data_source"),
    ) != (DEV_JAFFLE_PROJECT, _CATALOG, _SCHEMA, _DATA_SOURCE):
        return False
    if (mdl.get("catalog"), mdl.get("schema"), mdl.get("dataSource")) != (
        _CATALOG,
        _SCHEMA,
        _DATA_SOURCE,
    ):
        return False
    relationships = mdl.get("relationships")
    if not isinstance(relationships, list):
        return False
    matched = [
        item
        for item in relationships
        if isinstance(item, dict) and item.get("name") == _RELATIONSHIP
    ]
    if len(matched) != 1:
        return False
    relation = matched[0]
    models = relation.get("models")
    if not isinstance(models, list):
        return False
    if not all(isinstance(item, str) for item in models):
        return False
    return (
        sorted(models) == ["customers", "orders"]
        and relation.get("joinType") == "MANY_TO_ONE"
        and relation.get("condition") == _JOIN
    )


def _dataset_uses(snapshot: object) -> tuple[DatasetUse, ...] | None:
    from datasets.models import Dataset

    rows = list(
        Dataset.objects.filter(
            source_key=snapshot.source_key,
            project_key=snapshot.project_key,
            model_name__in=_MODELS,
        )
    )
    by_name: dict[str, list[Dataset]] = {}
    for row in rows:
        by_name.setdefault(row.model_name, []).append(row)
    uses: list[DatasetUse] = []
    for model in snapshot.models:
        matches = by_name.get(model.model_name, [])
        if len(matches) != 1:
            return None
        row = matches[0]
        if (
            not row.is_active
            or not row.is_available
            or row.validation_status != "metadata_discovered"
            or not row.definition_sha256
            or row.definition_sha256 != model.definition_sha256
            or row.catalog_version != snapshot.snapshot_sha256
        ):
            return None
        uses.append(
            DatasetUse(
                dataset_id=row.pk,
                source_key=row.source_key,
                project_key=row.project_key,
                model_name=row.model_name,
                definition_sha256=row.definition_sha256,
                structure_sha256=snapshot.structure_sha256,
            )
        )
    uses.sort(key=lambda item: item.model_name)
    return tuple(uses)


def _assigned(user_id: int, department_id: UUID, uses: tuple[DatasetUse, ...]) -> bool:
    from accounts.authorization import active_dataset_queryset

    eligible = set(
        active_dataset_queryset(
            user_id=user_id, department_id=department_id
        ).values_list("pk", flat=True)
    )
    return all(item.dataset_id in eligible for item in uses)


def resolve_dev_scope(*, user_id: int, department_id: UUID | None) -> DevScope | None:
    """Return the full synthetic scope, or None when this department is ineligible."""
    from config.settings import dev_jaffle_endpoint
    from datasets.discovery import configured_source_key
    from datasets.models import DatasetCatalogState

    endpoint = dev_jaffle_endpoint()
    if endpoint is None or department_id is None:
        return None
    source_key = configured_source_key()
    if source_key is None:
        return None
    catalog = DatasetCatalogState.objects.filter(pk=source_key).first()
    if catalog is None or not _catalog_success(catalog):
        return None
    snapshot = _synthetic_snapshot(source_key, catalog.execution_metadata_json)
    if snapshot is None:
        return None
    if (
        snapshot.snapshot_sha256 != catalog.snapshot_sha256
        or snapshot.structure_sha256 != catalog.structure_sha256
        or not _jaffle_identity(snapshot)
    ):
        return None
    uses = _dataset_uses(snapshot)
    if uses is None or not _assigned(user_id, department_id, uses):
        return None
    return DevScope(
        endpoint,
        source_key,
        snapshot.project_key,
        snapshot.snapshot_sha256,
        snapshot.structure_sha256,
        uses,
    )


def eligible_dev_department_ids(*, user_id: int) -> tuple[UUID, ...]:
    """Active memberships whose department currently admits the full DEV scope."""
    from accounts.authorization import list_user_departments

    return tuple(
        membership.department_id
        for membership in list_user_departments(user_id=user_id)
        if resolve_dev_scope(user_id=user_id, department_id=membership.department_id)
        is not None
    )


def _scope_current(*, run: AnalyticsRun, scope: DevScope) -> bool:
    from config.settings import DEV_JAFFLE_PROJECT, dev_jaffle_endpoint
    from datasets.discovery import configured_source_key
    from datasets.models import Dataset, DatasetCatalogState

    if run.department_id is None or not scope.datasets:
        return False
    if scope.project_key != DEV_JAFFLE_PROJECT:
        return False
    if dev_jaffle_endpoint() != scope.endpoint:
        return False
    if configured_source_key() != scope.source_key:
        return False
    catalog = DatasetCatalogState.objects.filter(pk=scope.source_key).first()
    if catalog is None or not _catalog_success(catalog):
        return False
    if (
        catalog.snapshot_sha256 != scope.snapshot_sha256
        or catalog.structure_sha256 != scope.structure_sha256
    ):
        return False
    rows = {
        row.pk: row
        for row in Dataset.objects.filter(
            pk__in=[item.dataset_id for item in scope.datasets]
        )
    }
    if len(rows) != len(scope.datasets):
        return False
    for item in scope.datasets:
        row = rows[item.dataset_id]
        if (
            item.structure_sha256 != scope.structure_sha256
            or item.source_key != scope.source_key
            or item.project_key != scope.project_key
            or row.source_key != item.source_key
            or row.project_key != item.project_key
            or row.model_name != item.model_name
            or not row.definition_sha256
            or row.definition_sha256 != item.definition_sha256
            or row.catalog_version != scope.snapshot_sha256
            or row.validation_status != "metadata_discovered"
            or not row.is_active
            or not row.is_available
        ):
            return False
    return _assigned(run.owner_id, run.department_id, scope.datasets)


def require_scope_current(*, run: AnalyticsRun, scope: DevScope) -> None:
    """Reject a run whose lease or captured synthetic scope moved.

    Compares stored versions only. Admission already parsed the catalog.
    """
    from chat import threads
    from chat.service import ChatError

    threads.require_run_current(
        owner_id=run.owner_id,
        thread_id=run.thread_id,
        department_id=run.department_id,
        authorization_epoch=run.authorization_epoch,
        run_token=run.run_token,
    )
    if not _scope_current(run=run, scope=scope):
        raise ChatError("authorization_changed")
