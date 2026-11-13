"""Fixed native permissions, role groups and the capabilities they expose.

The three AYCE groups are the only application roles. Membership in a group is
never proof of department authority: department scope always comes from an
active ``DepartmentMembership`` row, and dataset access always comes from an
explicit per-membership assignment.
"""

from __future__ import annotations

from typing import Final

from datasets.permissions import MANAGE_CATALOG, MANAGE_GRANTS

MANAGE_USERS: Final = "accounts.manage_users"
MANAGE_ROLES: Final = "accounts.manage_roles"
MANAGE_DEPARTMENTS: Final = "accounts.manage_departments"
MANAGE_DEPARTMENT_STAFF: Final = "accounts.manage_department_staff"
VIEW_AUDIT: Final = "accounts.view_audit"
USE_ANALYTICS: Final = "chat.use_analytics"

SUPERADMIN_GROUP: Final = "AYCE Superadmin"
LEAD_GROUP: Final = "AYCE Lead"
STAFF_GROUP: Final = "AYCE Staff"

# The exact permission set of each fixed group, in seed order.
FIXED_GROUPS: Final[dict[str, tuple[str, ...]]] = {
    SUPERADMIN_GROUP: (
        MANAGE_USERS,
        MANAGE_ROLES,
        MANAGE_DEPARTMENTS,
        MANAGE_DEPARTMENT_STAFF,
        VIEW_AUDIT,
        MANAGE_CATALOG,
        MANAGE_GRANTS,
        USE_ANALYTICS,
    ),
    LEAD_GROUP: (MANAGE_DEPARTMENT_STAFF, USE_ANALYTICS),
    STAFF_GROUP: (USE_ANALYTICS,),
}

# Public capability names exposed by ``/api/me/`` and the chat bootstrap.
CAPABILITIES: Final[dict[str, str]] = {
    "manage_users": MANAGE_USERS,
    "manage_roles": MANAGE_ROLES,
    "manage_departments": MANAGE_DEPARTMENTS,
    "manage_department_staff": MANAGE_DEPARTMENT_STAFF,
    "manage_catalog": MANAGE_CATALOG,
    "manage_grants": MANAGE_GRANTS,
    "view_audit": VIEW_AUDIT,
}

ROLE_LABELS: Final = (SUPERADMIN_GROUP, LEAD_GROUP, STAFF_GROUP)

# Every audit action this release can append.
AUDIT_ACTIONS: Final = frozenset(
    {
        "roles.seeded",
        "user.created",
        "user.updated",
        "user.deactivated",
        "user.reactivated",
        "user.password_reset",
        "user.role_changed",
        "department.created",
        "department.updated",
        "department.memberships_changed",
        "department.datasets_changed",
        "membership.datasets_changed",
        "dataset.created",
        "dataset.updated",
        "dataset.remapped",
        "catalog.refreshed",
        "catalog.imported",
        "authorization.denied",
    }
)

# Snapshot cap for one audit before/after document.
MAX_AUDIT_SNAPSHOT_BYTES: Final = 8 * 1024
