"""Store per-turn attachments and the observed output-limit flag.

File payloads live beside the turn that introduced them, so deleting a thread,
account or the latest exchange removes the bytes with it. Existing turns keep
an empty attachment list and the new flag stays false: nothing is inferred
about runs that finished before the flag existed.
"""

from __future__ import annotations

import uuid

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("chat", "0003_chatthread_turn_activity"),
    ]

    operations = [
        migrations.AddField(
            model_name="chatturn",
            name="output_limit_reached",
            field=models.BooleanField(default=False),
        ),
        migrations.CreateModel(
            name="ChatAttachment",
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
                ("position", models.PositiveIntegerField()),
                ("name", models.CharField(max_length=255)),
                ("media_type", models.CharField(max_length=128)),
                ("content_media_type", models.CharField(max_length=128)),
                ("size", models.PositiveIntegerField()),
                ("data", models.BinaryField(editable=False)),
                (
                    "turn",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="attachments",
                        to="chat.chatturn",
                    ),
                ),
            ],
            options={
                "ordering": ("position",),
            },
        ),
        migrations.AddConstraint(
            model_name="chatattachment",
            constraint=models.UniqueConstraint(
                fields=("turn", "position"),
                name="unique_turn_attachment_position",
            ),
        ),
    ]
