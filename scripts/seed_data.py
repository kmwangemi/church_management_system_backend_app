"""
Church Management System — Seed Data Definitions

The definitions live in app/core/seed.py (the application itself needs them, e.g. ADMIN_ROLE_NAMES),
and are re-exported here under the names the seeder and the tests already use.
"""

from app.core.config import settings
from app.core.seed import DEFAULT_ROLE_PERMISSIONS as ROLE_PERMISSION_MAP  # noqa: F401
from app.core.seed import PERMISSIONS  # noqa: F401
from app.core.seed import ROLE_DESCRIPTIONS as ROLES  # noqa: F401

# ── Validation (runs at import time) ──────────────────────────────────────────
_all_permission_names = set(PERMISSIONS.keys())
_all_role_names = set(ROLES.keys())

for _role, _perms in ROLE_PERMISSION_MAP.items():
    if _role not in _all_role_names:
        raise ValueError(
            f"ROLE_PERMISSION_MAP references unknown role: '{_role}'. Add it to ROLES first."
        )
    _unknown = set(_perms) - _all_permission_names
    if _unknown:
        raise ValueError(
            f"Role '{_role}' references unknown permission(s): {_unknown}. Add them to PERMISSIONS first."
        )

# ── Default Superuser ──────────────────────────────────────────────────────────
# SUPERADMIN_EMAIL / SUPERADMIN_PASSWORD are required settings (app/core/config.py): the app and
# the seeder refuse to start without them, and the password must be at least 8 characters.
DEFAULT_SUPERUSER = {
    "email": settings.SUPERADMIN_EMAIL,
    "display_name": "Super Admin",
    "password": settings.SUPERADMIN_PASSWORD,
}