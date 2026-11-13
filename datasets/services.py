"""Audited dataset catalog and grant mutations."""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager, suppress
from datetime import UTC
from typing import Any
from uuid import UUID

from asgiref.sync import sync_to_async
from django.contrib.auth.models import User
from django.db import IntegrityError, OperationalError, transaction
from django.db.models import F
from django.utils import timezone

from accounts.authorization import (
    ManagementError,
    department_label,
    is_superadmin,
    require_department_lead,
    require_permission,
    require_superadmin,
    user_projection,
)
from accounts.models import AuthorizationState, Department, DepartmentMembership
from accounts.services import (
    MAX_REPLACEMENT_ENTRIES,
    _lock_conflict,
    department_payload,
    mutation,
    record_audit,
    unique_values,
    valid_state_value,
)

from .discovery import (
    CatalogSnapshot,
    DiscoveryError,
    configured_source_key,
    discover_catalog,
)
from .models import Dataset, DatasetCatalogState, DepartmentDataset, MembershipDataset
from .permissions import MANAGE_CATALOG, MANAGE_GRANTS


def _dataset_department_ids(*, dataset_id: UUID) -> list[str]:
    return [
        str(value)
        for value in DepartmentDataset.objects.filter(dataset_id=dataset_id)
        .order_by("department__code")
        .values_list("department_id", flat=True)
    ]


def dataset_availability(
    dataset: Dataset, *, grant: DepartmentDataset | None = None
) -> str:
    if dataset.source_key != configured_source_key():
        return "different_source"
    if not dataset.is_available:
        return "missing"
    if not dataset.is_active:
        return "disabled"
    if grant is not None and grant.needs_review:
        return "needs_review"
    return "available"


def dataset_payload(dataset: Dataset) -> dict[str, Any]:
    """Public catalog projection: never credentials and never query text."""
    validated_at = dataset.validated_at
    return {
        "id": str(dataset.id),
        "code": dataset.code,
        "display_name": dataset.display_name,
        "description": dataset.description,
        "source_key": dataset.source_key,
        "project_key": dataset.project_key,
        "model_name": dataset.model_name,
        "catalog": dataset.catalog,
        "schema": dataset.schema,
        "physical_table": dataset.physical_table,
        "is_active": dataset.is_active,
        "availability": dataset_availability(dataset),
        "catalog_version": dataset.catalog_version,
        "validation_status": dataset.validation_status,
        "validated_at": (
            validated_at.astimezone(UTC).isoformat()
            if validated_at is not None
            else None
        ),
        "department_ids": _dataset_department_ids(dataset_id=dataset.pk),
    }


def _require_catalog_reader(*, actor: User) -> None:
    require_superadmin(actor=actor)
    require_permission(actor=actor, permission=MANAGE_CATALOG)


def _catalog_actor(actor_id: int) -> User:
    actor = User.objects.filter(pk=actor_id, is_active=True).first()
    if actor is None:
        raise ManagementError("permission_denied")
    _require_catalog_reader(actor=actor)
    return actor


def catalog_status(*, actor_id: int) -> dict[str, Any]:
    from accounts.services import current_state

    _catalog_actor(actor_id)
    source = configured_source_key()
    row = DatasetCatalogState.objects.filter(pk=source).first() if source else None
    return {
        "configured": source is not None,
        "last_attempt_at": row.last_attempt_at.astimezone(UTC).isoformat()
        if row and row.last_attempt_at
        else None,
        "last_success_at": row.last_success_at.astimezone(UTC).isoformat()
        if row and row.last_success_at
        else None,
        "error_code": (row.error_code or None) if row else None,
        **current_state(),
    }


@contextmanager
def _catalog_transaction(
    *, actor_id: int | None, revision: int
) -> Iterator[tuple[AuthorizationState, User | None]]:
    if valid_state_value(revision) is None:
        raise ManagementError("invalid_management_request")
    try:
        AuthorizationState.objects.get_or_create(
            pk=1, defaults={"revision": 0, "epoch": 0}
        )
        with transaction.atomic():
            if not AuthorizationState.objects.filter(pk=1, revision=revision).update(
                revision=F("revision")
            ):
                raise ManagementError("administration_conflict")
            state = AuthorizationState.objects.get(pk=1)
            actor = _catalog_actor(actor_id) if actor_id is not None else None
            yield state, actor
    except OperationalError as error:
        if _lock_conflict(error):
            raise ManagementError("administration_conflict") from None
        raise


def _apply_discovery_snapshot(
    *, snapshot: CatalogSnapshot, actor_id: int | None, revision: int
) -> dict[str, Any]:
    """Apply a complete snapshot under the authorization singleton lock."""
    try:
        with _catalog_transaction(actor_id=actor_id, revision=revision) as (
            state,
            actor,
        ):
            if configured_source_key() != snapshot.source_key:
                raise ManagementError("administration_conflict")
            now = timezone.now()
            catalog, _ = DatasetCatalogState.objects.get_or_create(
                source_key=snapshot.source_key
            )
            existing = {
                (row.project_key, row.model_name): row
                for row in Dataset.objects.filter(source_key=snapshot.source_key)
            }
            present = set()
            created = updated = missing = 0
            changed = epoch_changed = False
            review = bool(
                catalog.last_success_at
                and catalog.structure_sha256 != snapshot.structure_sha256
            )
            if review:
                changed = epoch_changed = True
            for model in snapshot.models:
                identity = (snapshot.project_key, model.model_name)
                present.add(identity)
                row = existing.get(identity)
                if row is not None and row.validation_status != "metadata_discovered":
                    raise DiscoveryError("apply_conflict")
                fields = {
                    "display_name": model.display_name,
                    "description": model.description,
                    "catalog": model.catalog,
                    "schema": model.schema,
                    "physical_table": model.physical_table,
                    "definition_sha256": model.definition_sha256,
                    "is_available": True,
                    "validation_status": "metadata_discovered",
                }
                if row is None:
                    if Dataset.objects.filter(code=model.code).exists():
                        raise DiscoveryError("apply_conflict")
                    Dataset.objects.create(
                        source_key=snapshot.source_key,
                        project_key=snapshot.project_key,
                        model_name=model.model_name,
                        code=model.code,
                        catalog_version=snapshot.snapshot_sha256,
                        validated_at=now,
                        **fields,
                    )
                    created += 1
                    changed = True
                    continue
                differences = {
                    key for key, value in fields.items() if getattr(row, key) != value
                }
                if differences:
                    updated += 1
                    changed = True
                    structural = differences & {
                        "catalog",
                        "schema",
                        "physical_table",
                        "definition_sha256",
                    }
                    if structural:
                        review = True
                    if structural or "is_available" in differences:
                        epoch_changed = True
                    for key in differences:
                        setattr(row, key, fields[key])
                row.catalog_version = snapshot.snapshot_sha256
                row.validated_at = now
                row.save(
                    update_fields=[
                        *sorted(differences),
                        "catalog_version",
                        "validated_at",
                        "updated_at",
                    ]
                )
            for identity, row in existing.items():
                if identity not in present and row.is_available:
                    row.is_available = False
                    row.save(update_fields=["is_available", "updated_at"])
                    missing += 1
                    changed = epoch_changed = review = True
            grants = DepartmentDataset.objects.filter(
                dataset__source_key=snapshot.source_key
            )
            if review and grants.filter(needs_review=False).update(needs_review=True):
                changed = epoch_changed = True
            review_required = grants.filter(needs_review=True).count()
            catalog.last_attempt_at = now
            catalog.last_success_at = now
            catalog.snapshot_sha256 = snapshot.snapshot_sha256
            catalog.structure_sha256 = snapshot.structure_sha256
            catalog.execution_metadata_json = snapshot.execution_metadata_json
            catalog.error_code = ""
            catalog.save()
            if changed:
                state.revision += 1
                if epoch_changed:
                    state.epoch += 1
                state.save(update_fields=["revision", "epoch", "updated_at"])
                record_audit(
                    actor=actor,
                    action="catalog.refreshed",
                    target_type="catalog",
                    target_id=snapshot.source_key,
                    after={
                        "source_key": snapshot.source_key,
                        "snapshot_sha256": snapshot.snapshot_sha256,
                        "created": created,
                        "updated": updated,
                        "missing": missing,
                        "review_required": review_required,
                    },
                )
            return {
                "created": created,
                "updated": updated,
                "missing": missing,
                "review_required": review_required,
                "revision": state.revision,
                "epoch": state.epoch,
            }
    except IntegrityError:
        raise DiscoveryError("apply_conflict") from None


def _record_discovery_failure(
    *, source_key: str, code: str, actor_id: int | None, revision: int
) -> None:
    with _catalog_transaction(actor_id=actor_id, revision=revision):
        if configured_source_key() != source_key:
            raise ManagementError("administration_conflict")
        DatasetCatalogState.objects.update_or_create(
            source_key=source_key,
            defaults={"last_attempt_at": timezone.now(), "error_code": code},
        )


async def refresh_catalog(*, actor_id: int, revision: int) -> dict[str, Any]:
    if valid_state_value(revision) is None:
        raise ManagementError("invalid_management_request")
    await sync_to_async(_catalog_actor, thread_sensitive=True)(actor_id)
    source_key = configured_source_key()
    if source_key is None:
        raise ManagementError("catalog_unavailable")
    try:
        snapshot = await discover_catalog()
        return await sync_to_async(_apply_discovery_snapshot, thread_sensitive=True)(
            snapshot=snapshot, actor_id=actor_id, revision=revision
        )
    except DiscoveryError as error:
        await sync_to_async(_record_discovery_failure, thread_sensitive=True)(
            source_key=source_key, code=error.code, actor_id=actor_id, revision=revision
        )
        raise ManagementError("catalog_unavailable") from None


async def refresh_catalog_on_startup() -> None:
    source_key = configured_source_key()
    if source_key is None:
        return
    from accounts.services import current_state

    revision = (await sync_to_async(current_state, thread_sensitive=True)())["revision"]
    try:
        snapshot = await discover_catalog()
        await sync_to_async(_apply_discovery_snapshot, thread_sensitive=True)(
            snapshot=snapshot, actor_id=None, revision=revision
        )
    except DiscoveryError as error:
        with suppress(ManagementError):
            await sync_to_async(_record_discovery_failure, thread_sensitive=True)(
                source_key=source_key, code=error.code, actor_id=None, revision=revision
            )
    except ManagementError:
        pass


def update_dataset(
    *, actor_id: int, revision: int, dataset_id: UUID, changes: Mapping[str, Any]
) -> dict[str, Any]:
    """Change the local enabled state of a discovered model."""
    if set(changes) != {"is_active"} or not isinstance(changes["is_active"], bool):
        raise ManagementError("invalid_management_request")
    with mutation(actor_id=actor_id, revision=revision, bump_epoch=True) as state:
        _require_catalog_reader(actor=state.actor)
        dataset = Dataset.objects.filter(
            pk=dataset_id, source_key=configured_source_key(), is_available=True
        ).first()
        if dataset is None:
            raise ManagementError("resource_not_found")
        old = dataset.is_active
        dataset.is_active = changes["is_active"]
        if old != dataset.is_active:
            dataset.save(update_fields=["is_active", "updated_at"])
            record_audit(
                actor=state.actor,
                action="dataset.updated",
                target_type="dataset",
                target_id=str(dataset.pk),
                before={"is_active": old},
                after={"is_active": dataset.is_active},
            )
        return {
            **dataset_payload(dataset),
            "revision": state.revision,
            "epoch": state.epoch,
        }


def replace_department_datasets(
    *,
    actor_id: int,
    revision: int,
    department_id: UUID,
    dataset_ids: Sequence[UUID],
    confirmed_dataset_ids: Sequence[UUID] = (),
) -> dict[str, Any]:
    """Replace a department's set of grants; revoked grants cascade assignments."""
    for values in (dataset_ids, confirmed_dataset_ids):
        if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
            raise ManagementError("invalid_management_request")
        if len(values) > MAX_REPLACEMENT_ENTRIES or any(
            not isinstance(value, UUID) for value in values
        ):
            raise ManagementError("invalid_management_request")
        unique_values(list(values))

    with mutation(actor_id=actor_id, revision=revision, bump_epoch=True) as state:
        require_superadmin(actor=state.actor)
        require_permission(actor=state.actor, permission=MANAGE_GRANTS)
        department = Department.objects.filter(pk=department_id).first()
        if department is None:
            raise ManagementError("resource_not_found")
        requested = set(dataset_ids)
        confirmed = set(confirmed_dataset_ids)
        grants = {
            grant.dataset_id: grant
            for grant in DepartmentDataset.objects.filter(
                department=department
            ).select_related("dataset")
        }
        current = set(grants)
        available = set(
            Dataset.objects.filter(
                pk__in=requested,
                source_key=configured_source_key(),
                is_active=True,
                is_available=True,
            ).values_list("pk", flat=True)
        )
        if not (requested - current) <= available:
            raise ManagementError("invalid_management_request")
        if (
            not confirmed <= requested
            or not confirmed <= available
            or any(
                value not in grants or not grants[value].needs_review
                for value in confirmed
            )
        ):
            raise ManagementError("invalid_management_request")
        confirmed_grants = [grants[value].pk for value in confirmed]
        MembershipDataset.objects.filter(
            department_grant_id__in=confirmed_grants
        ).delete()
        DepartmentDataset.objects.filter(pk__in=confirmed_grants).update(
            needs_review=False
        )
        DepartmentDataset.objects.filter(
            department=department, dataset_id__in=current - requested
        ).delete()
        DepartmentDataset.objects.bulk_create(
            [
                DepartmentDataset(
                    department=department,
                    dataset_id=dataset_id,
                    granted_by=state.actor,
                )
                for dataset_id in requested - current
            ]
        )
        record_audit(
            actor=state.actor,
            action="department.datasets_changed",
            target_type="department",
            target_id=str(department.pk),
            before={"dataset_ids": sorted(str(value) for value in current)},
            after={"dataset_ids": sorted(str(value) for value in requested)},
        )
        return {
            "department": department_payload(department),
            "dataset_ids": sorted(str(value) for value in requested),
            "revision": state.revision,
            "epoch": state.epoch,
        }


def replace_membership_datasets(
    *,
    actor_id: int,
    revision: int,
    department_id: UUID,
    user_id: int,
    dataset_ids: Sequence[UUID],
) -> dict[str, Any]:
    """Replace one membership's explicit subset inside its department grants."""
    if not isinstance(dataset_ids, Sequence) or isinstance(dataset_ids, (str, bytes)):
        raise ManagementError("invalid_management_request")
    if len(dataset_ids) > MAX_REPLACEMENT_ENTRIES:
        raise ManagementError("invalid_management_request")
    if any(not isinstance(value, UUID) for value in dataset_ids):
        raise ManagementError("invalid_management_request")
    unique_values(list(dataset_ids))

    with mutation(actor_id=actor_id, revision=revision, bump_epoch=True) as state:
        global_scope = is_superadmin(state.actor)
        if global_scope:
            require_permission(actor=state.actor, permission=MANAGE_GRANTS)
        else:
            require_permission(
                actor=state.actor, permission="accounts.manage_department_staff"
            )
        department = Department.objects.filter(pk=department_id).first()
        if department is None:
            raise ManagementError("resource_not_found")
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
            require_department_lead(actor=state.actor, department_id=department.pk)
            if membership.membership_role != "staff":
                raise ManagementError("permission_denied")
            target = membership.user
            if (
                target.is_superuser
                or target.groups.filter(name="AYCE Superadmin").exists()
            ):
                raise ManagementError("resource_not_found")

        current = set(
            MembershipDataset.objects.filter(membership=membership).values_list(
                "department_grant_id", flat=True
            )
        )
        grants = {
            grant.dataset_id: grant
            for grant in DepartmentDataset.objects.filter(
                department=department, dataset_id__in=dataset_ids
            ).select_related("dataset")
            if dataset_availability(grant.dataset, grant=grant) == "available"
            or grant.pk in current
        }
        if len(grants) != len(dataset_ids):
            raise ManagementError("invalid_management_request")
        wanted = {grants[dataset_id].pk for dataset_id in dataset_ids}
        before_ids = sorted(
            str(value)
            for value in MembershipDataset.objects.filter(membership=membership)
            .order_by("department_grant__dataset__code")
            .values_list("department_grant__dataset_id", flat=True)
        )
        MembershipDataset.objects.filter(
            membership=membership, department_grant_id__in=current - wanted
        ).delete()
        MembershipDataset.objects.bulk_create(
            [
                MembershipDataset(
                    membership=membership,
                    department_grant_id=grant_id,
                    assigned_by=state.actor,
                )
                for grant_id in wanted - current
            ]
        )
        record_audit(
            actor=state.actor,
            action="membership.datasets_changed",
            target_type="membership",
            target_id=str(membership.pk),
            before={"dataset_ids": before_ids},
            after={"dataset_ids": sorted(str(value) for value in dataset_ids)},
        )
        return {
            "membership": {
                "id": str(membership.pk),
                "department": department_label(department),
                "user": user_projection(membership.user),
                "membership_role": membership.membership_role,
                "dataset_ids": sorted(str(value) for value in dataset_ids),
            },
            "revision": state.revision,
            "epoch": state.epoch,
        }
