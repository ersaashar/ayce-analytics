"""Backfill existing ordinary accounts into the fixed Staff group.

Superusers keep their native flags and gain no group: they are effective
administrators without one. No department, membership, grant or assignment is
invented for anybody, so every existing account starts with an empty dataset
set.
"""

from __future__ import annotations

from django.conf import settings
from django.db import migrations

STAFF_GROUP = "AYCE Staff"


def backfill_staff(apps, schema_editor):
    Group = apps.get_model("auth", "Group")
    User = apps.get_model(*settings.AUTH_USER_MODEL.split("."))
    AuthorizationAudit = apps.get_model("accounts", "AuthorizationAudit")

    staff, _created = Group.objects.get_or_create(name=STAFF_GROUP)
    ordinary = User.objects.filter(is_superuser=False)
    added = 0
    for user in ordinary.iterator():
        if not user.groups.filter(pk=staff.pk).exists():
            user.groups.add(staff)
            added += 1
    AuthorizationAudit.objects.create(
        actor=None,
        action="roles.seeded",
        target_type="roles",
        target_id="staff-backfill",
        allowed=True,
        reason_code="migration",
        before={},
        after={"staff_backfilled": added},
    )


def unbackfill(apps, schema_editor):
    """No reverse: the Staff role is a safe baseline for every account."""


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0002_seed_roles"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.RunPython(backfill_staff, unbackfill),
    ]
