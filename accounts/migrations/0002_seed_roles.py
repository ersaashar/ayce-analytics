"""Seed the fixed AYCE role groups, their permissions and the state singleton.

The definitions are application-owned constants. This migration runs after the
models whose ``Meta.permissions`` declare them, and creates the ContentType and
Permission rows itself so the groups are correct before ``post_migrate`` adds
anything. Unrelated groups and permissions are never touched.
"""

from __future__ import annotations

from django.db import migrations

SUPERADMIN_GROUP = "AYCE Superadmin"
LEAD_GROUP = "AYCE Lead"
STAFF_GROUP = "AYCE Staff"

# (app_label, model, codename, name) for every fixed native permission.
PERMISSIONS = (
    (
        "accounts",
        "authorizationstate",
        "manage_users",
        "Can create, update and deactivate accounts",
    ),
    (
        "accounts",
        "authorizationstate",
        "manage_roles",
        "Can change fixed application roles",
    ),
    (
        "accounts",
        "authorizationstate",
        "view_audit",
        "Can read the authorization audit log",
    ),
    (
        "accounts",
        "department",
        "manage_departments",
        "Can create, update and deactivate departments",
    ),
    (
        "accounts",
        "department",
        "manage_department_staff",
        "Can manage staff memberships of a led department",
    ),
    (
        "datasets",
        "dataset",
        "manage_catalog",
        "Can edit reviewed dataset metadata",
    ),
    (
        "datasets",
        "dataset",
        "manage_grants",
        "Can grant datasets to departments and members",
    ),
    (
        "chat",
        "chatthread",
        "use_analytics",
        "Can request analytics chat turns",
    ),
)

GROUP_PERMISSIONS = {
    SUPERADMIN_GROUP: (
        "accounts.manage_users",
        "accounts.manage_roles",
        "accounts.manage_departments",
        "accounts.manage_department_staff",
        "accounts.view_audit",
        "datasets.manage_catalog",
        "datasets.manage_grants",
        "chat.use_analytics",
    ),
    LEAD_GROUP: (
        "accounts.manage_department_staff",
        "chat.use_analytics",
    ),
    STAFF_GROUP: ("chat.use_analytics",),
}


def seed_roles(apps, schema_editor):
    ContentType = apps.get_model("contenttypes", "ContentType")
    Permission = apps.get_model("auth", "Permission")
    Group = apps.get_model("auth", "Group")
    AuthorizationState = apps.get_model("accounts", "AuthorizationState")
    AuthorizationAudit = apps.get_model("accounts", "AuthorizationAudit")

    resolved: dict[str, object] = {}
    for app_label, model, codename, name in PERMISSIONS:
        content_type, _created = ContentType.objects.get_or_create(
            app_label=app_label, model=model
        )
        permission, _created = Permission.objects.get_or_create(
            content_type_id=content_type.pk,
            codename=codename,
            defaults={"name": name},
        )
        resolved[f"{app_label}.{codename}"] = permission

    for name, labels in GROUP_PERMISSIONS.items():
        group, _created = Group.objects.get_or_create(name=name)
        group.permissions.set([resolved[label].pk for label in labels])

    AuthorizationState.objects.get_or_create(pk=1, defaults={"revision": 0, "epoch": 0})
    AuthorizationAudit.objects.create(
        actor=None,
        action="roles.seeded",
        target_type="roles",
        target_id="fixed",
        allowed=True,
        reason_code="migration",
        before={},
        after={"groups": sorted(GROUP_PERMISSIONS)},
    )


def unseed_roles(apps, schema_editor):
    """Leave the rows in place: groups may already carry real memberships."""


class Migration(migrations.Migration):
    dependencies = [
        ("accounts", "0001_initial"),
        ("datasets", "0001_initial"),
        ("chat", "0005_alter_chatthread_options_chatthread_department_and_more"),
        ("contenttypes", "0002_remove_content_type_name"),
        ("auth", "0012_alter_user_first_name_max_length"),
    ]

    operations = [
        migrations.RunPython(seed_roles, unseed_roles),
    ]
