"""Durable chat storage: one row owns one conversation."""

from __future__ import annotations

import uuid
from typing import ClassVar

from django.conf import settings
from django.db import models


class ChatThread(models.Model):
    """A single conversation owned by exactly one account.

    The row holds both the canonical pydantic-ai history and the visible
    completed pairs so a transcript is read and committed as one unit. The
    revision is bumped by every completed turn and every rename; writers use it
    as a compare-and-swap guard so a stale tab cannot overwrite newer content.

    ``active_run_token`` and ``active_run_expires_at`` form a short lease so two
    tabs cannot generate the same turn at once. They carry no conversation data
    and are cleared by whichever writer wins.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="chat_threads",
    )
    # Immutable after creation: a thread keeps the department context it was
    # started in, so access can be re-evaluated for every read and run.
    department = models.ForeignKey(
        "accounts.Department",
        on_delete=models.PROTECT,
        related_name="chat_threads",
        null=True,
        blank=True,
        default=None,
    )
    # Set by the legacy classification migration when stored provenance cannot
    # be proven safe. Locked histories never reach a model or the browser.
    quarantined = models.BooleanField(default=False, editable=False)
    title = models.CharField(max_length=80, default="New chat")
    history_json = models.TextField(default="[]")
    display = models.JSONField(default=list)
    revision = models.PositiveIntegerField(default=0)
    active_run_token = models.UUIDField(null=True, default=None, editable=False)
    active_run_expires_at = models.DateTimeField(
        null=True, default=None, editable=False
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-updated_at", "-id")
        indexes: ClassVar[list[models.Index]] = [
            models.Index(
                fields=("owner", "-updated_at", "-id"),
                name="chat_thread_owner_recency_idx",
            ),
        ]
        permissions: ClassVar[list[tuple[str, str]]] = [
            ("use_analytics", "Can request analytics chat turns"),
        ]

    def __str__(self) -> str:
        return f"{self.title} ({self.id})"


class ChatTurn(models.Model):
    """Private per-turn metadata for one completed assistant response.

    ``pre_history_json`` is the canonical history as it stood before this turn,
    which is what makes the latest exchange removable and regenerable without
    re-deriving boundaries from display offsets. It is never serialized to a
    client and is deleted with its thread.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    thread = models.ForeignKey(
        ChatThread,
        on_delete=models.CASCADE,
        related_name="turns",
    )
    turn_index = models.PositiveIntegerField()
    # A legacy exchange has no checkpoint; NULL is the meaningful "unknown".
    pre_history_json = models.TextField(null=True, editable=False)  # noqa: DJ001
    requested_model = models.CharField(max_length=200, null=True)  # noqa: DJ001
    resolved_model = models.CharField(max_length=200, null=True)  # noqa: DJ001
    completed_at = models.DateTimeField(null=True)
    duration_ms = models.PositiveIntegerField(null=True)
    output_tokens = models.PositiveIntegerField(null=True)
    activity = models.JSONField(null=True, default=None)
    # True only when this turn's own run observed an output-limit finish; the
    # default says nothing about how older runs ended.
    output_limit_reached = models.BooleanField(default=False)

    class Meta:
        ordering = ("turn_index",)
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=("thread", "turn_index"),
                name="unique_thread_turn_index",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.thread_id}#{self.turn_index}"


class ChatTurnDataset(models.Model):
    """Private dataset provenance for one completed warehouse turn."""

    turn = models.ForeignKey(
        ChatTurn, on_delete=models.CASCADE, related_name="warehouse_dependencies"
    )
    dataset = models.ForeignKey("datasets.Dataset", on_delete=models.PROTECT)
    definition_sha256 = models.CharField(max_length=64)
    structure_sha256 = models.CharField(max_length=64)

    class Meta:
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=("turn", "dataset"), name="unique_turn_warehouse_dataset"
            ),
        ]

    def __str__(self) -> str:
        return f"{self.turn_id}:{self.dataset_id}"


class ChatAttachment(models.Model):
    """One uploaded file owned by the turn that introduced it.

    The payload shares the turn's lifetime: deleting a thread, an account or
    the latest exchange removes the bytes with it, and a turn only ever reads
    the attachments of its own conversation.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    turn = models.ForeignKey(
        ChatTurn,
        on_delete=models.CASCADE,
        related_name="attachments",
    )
    position = models.PositiveIntegerField()
    name = models.CharField(max_length=255)
    media_type = models.CharField(max_length=128)
    content_media_type = models.CharField(max_length=128)
    size = models.PositiveIntegerField()
    data = models.BinaryField(editable=False)

    class Meta:
        ordering = ("position",)
        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=("turn", "position"),
                name="unique_turn_attachment_position",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.turn_id}#{self.position}"
