"""
Church Management System — Seed Data Definitions

Single source of truth for all permissions, roles, and their mappings.
Used by both the seeder and tests.
"""

# ── Permissions ───────────────────────────────────────────────────────────────
# Format: { "name": "human-readable description" }

PERMISSIONS: dict[str, str] = {
    # Users & Roles
    "manage_users": "Can create, update, delete users",
    "view_users": "Can view user directories and profiles",
    "manage_roles": "Can assign and manage roles and permissions",
    # Churches
    "manage_churches": "Can manage top level churches",
    "view_churches": "Can view church details",
    # Audit Logs
    "view_audit_logs": "Can view system audit logs",
    # Finances
    "manage_finances": "Can view and manage financial records, tithes, and offerings",
    "view_finances": "Can view financial summaries and reports",
    # Events
    "manage_events": "Can schedule, update, and delete church events",
    "view_events": "Can view the church calendar and event details",
    # Groups
    "manage_groups": "Can create and manage small groups or ministries",
    "view_groups": "Can view existing groups and their members",
    # Content & Communication
    "manage_content": "Can manage sermons, announcements, and content",
    "send_communications": "Can send bulk emails and SMS to members",
}

# ── Roles ─────────────────────────────────────────────────────────────────────
# Format: { "name": "description" }

ROLES: dict[str, str] = {
    "SUPER_ADMIN": "Global Super Administrator with full system access across all churches",
    "CHURCH_ADMIN": "Administrator for a specific church",
    "MEMBER": "Regular church member with basic access",
}

# ── Role → Permission Mapping ─────────────────────────────────────────────────

ROLE_PERMISSION_MAP: dict[str, list[str]] = {
    "SUPER_ADMIN": [
        *PERMISSIONS.keys(),
    ],
    "CHURCH_ADMIN": [
        "manage_users",
        "view_users",
        "manage_roles",
        "view_churches",
        "view_audit_logs",
        "manage_finances",
        "view_finances",
        "manage_events",
        "view_events",
        "manage_groups",
        "view_groups",
        "manage_content",
        "send_communications",
    ],
    "MEMBER": [
        "view_users",
        "view_churches",
        "view_events",
        "view_groups",
    ],
}

# ── Validation (runs at import time) ──────────────────────────────────────────

_all_permission_names = set(PERMISSIONS.keys())
_all_role_names = set(ROLES.keys())

for _role, _perms in ROLE_PERMISSION_MAP.items():
    if _role not in _all_role_names:
        raise ValueError(
            f"ROLE_PERMISSION_MAP references unknown role: '{_role}'. "
            f"Add it to ROLES first."
        )
    _unknown = set(_perms) - _all_permission_names
    if _unknown:
        raise ValueError(
            f"Role '{_role}' references unknown permission(s): {_unknown}. "
            f"Add them to PERMISSIONS first."
        )

# ── Default Superuser ──────────────────────────────────────────────────────────

from app.core.config import settings

_email = settings.SUPERADMIN_EMAIL
_name = "Super Admin"
_password = settings.SUPERADMIN_PASSWORD

_missing = [
    k
    for k, v in {
        "SUPERADMIN_EMAIL": _email,
        "SUPERADMIN_PASSWORD": _password,
    }.items()
    if not v
]

if _missing:
    raise EnvironmentError(
        f"Missing required environment variable(s): {', '.join(_missing)}\n"
        "Add them to your .env file before running the seeder."
    )

DEFAULT_SUPERUSER = {
    "email": _email,
    "first_name": _name.split(" ", maxsplit=1)[0],
    "last_name": _name.split(" ", maxsplit=1)[1],
    "password": _password,
    "is_superuser": True,
    "role": "SUPER_ADMIN",
}
