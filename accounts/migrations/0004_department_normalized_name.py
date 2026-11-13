from __future__ import annotations

import unicodedata

from django.db import migrations, models


def backfill_names(apps, schema_editor):
    # Preflight every row before changing any legacy name identity.
    Department = apps.get_model("accounts", "Department")
    rows = list(Department.objects.using(schema_editor.connection.alias).all())
    keys = {}
    invalid = []
    for row in rows:
        display = " ".join(unicodedata.normalize("NFKC", row.name).split())
        folded = unicodedata.normalize("NFKC", display).casefold()
        parts = []
        separator = False
        for character in folded:
            if character.isalnum():
                if separator and parts:
                    parts.append("-")
                parts.append(character)
                separator = False
            else:
                separator = True
        key = "".join(parts)
        if not key:
            invalid.append(row)
        keys.setdefault(key, []).append(row)
    conflicts = invalid + [
        row for key, group in keys.items() if key and len(group) > 1 for row in group
    ]
    if conflicts:
        details = ", ".join(f"{row.pk}: {row.name!r}" for row in conflicts)
        raise RuntimeError(
            f"Rename conflicting or empty department names before migrating: {details}"
        )
    for key, group in keys.items():
        Department.objects.using(schema_editor.connection.alias).filter(
            pk=group[0].pk
        ).update(normalized_name=key)


class Migration(migrations.Migration):
    dependencies = [("accounts", "0003_backfill_staff")]

    operations = [
        migrations.AddField(
            model_name="department",
            name="normalized_name",
            field=models.CharField(editable=False, max_length=450, null=True),
        ),
        migrations.RunPython(backfill_names, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="department",
            name="normalized_name",
            field=models.CharField(editable=False, max_length=450, unique=True),
        ),
        migrations.AlterField(
            model_name="department",
            name="code",
            field=models.SlugField(allow_unicode=True, max_length=64, unique=True),
        ),
    ]
