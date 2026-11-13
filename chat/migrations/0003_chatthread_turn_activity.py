"""Add per-turn records and the short run lease.

Existing threads keep their conversation untouched: no turn rows are invented,
so a legacy exchange simply has no metadata or actions. The lease columns start
empty, which means "not running".
"""

from __future__ import annotations

import uuid

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("chat", "0002_remove_legacy_chat_session_keys"),
    ]

    operations = [
        migrations.CreateModel(
            name="ChatTurn",
            fields=[
                (
                    "id",
                    models.UUIDField(
                        default=uuid.uuid4,
                        editable=False,
                        primary_key=True,
                        serialize=False,
                    ),
                ),
                ("turn_index", models.PositiveIntegerField()),
                ("pre_history_json", models.TextField(editable=False, null=True)),
                (
                    "requested_model",
                    models.CharField(max_length=200, null=True),
                ),
                ("resolved_model", models.CharField(max_length=200, null=True)),
                ("completed_at", models.DateTimeField(null=True)),
                ("duration_ms", models.PositiveIntegerField(null=True)),
                ("output_tokens", models.PositiveIntegerField(null=True)),
                ("activity", models.JSONField(default=None, null=True)),
                (
                    "thread",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="turns",
                        to="chat.chatthread",
                    ),
                ),
            ],
            options={
                "ordering": ("turn_index",),
            },
        ),
        migrations.AddConstraint(
            model_name="chatturn",
            constraint=models.UniqueConstraint(
                fields=("thread", "turn_index"),
                name="unique_thread_turn_index",
            ),
        ),
        migrations.AddField(
            model_name="chatthread",
            name="active_run_token",
            field=models.UUIDField(default=None, editable=False, null=True),
        ),
        migrations.AddField(
            model_name="chatthread",
            name="active_run_expires_at",
            field=models.DateTimeField(default=None, editable=False, null=True),
        ),
    ]
