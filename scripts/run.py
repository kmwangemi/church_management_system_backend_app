"""
Church Management System — Database Seeder

Idempotent seed script: safe to run multiple times.
  - Permissions are created if they don't exist, updated if they do.
  - Roles are created if they don't exist.
  - Role→permission links are reconciled (adds new, leaves existing).
  - Default superuser is created only if no superuser exists.
  - Superuser always gets the 'SUPER_ADMIN' role assigned (backfilled if missing).
  - Existing data is never deleted.

Run:
    python -m scripts.run            (from backend root)
"""

import asyncio
import os
import sys

# Ensure backend directory is in path
sys.path.insert(
    0, os.path.dirname(os.path.dirname(__file__))
)

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db.session import AsyncSessionLocal
from app.core.security import get_password_hash
from app.models.auth import Permission, Role, UserRole
from app.models.user import User
from app.enums import GlobalRole, AccountStatus
from scripts.seed_data import (
    DEFAULT_SUPERUSER,
    PERMISSIONS,
    ROLE_PERMISSION_MAP,
    ROLES,
)

# ── Colour helpers ────────────────────────────────────────────────────────────

def green(s):
    return f"\033[92m{s}\033[0m"

def yellow(s):
    return f"\033[93m{s}\033[0m"

def cyan(s):
    return f"\033[96m{s}\033[0m"

def bold(s):
    return f"\033[1m{s}\033[0m"

# ── Step functions ────────────────────────────────────────────────────────────

async def seed_permissions(db: AsyncSession) -> dict[str, Permission]:
    """
    Upsert all permissions from PERMISSIONS into the database.
    Returns a name → Permission ORM object mapping for role wiring.
    """
    print(f"\n{bold('[ 1/3 ] Permissions')}")
    perm_map: dict[str, Permission] = {}
    created = updated = 0
    for name, description in PERMISSIONS.items():
        result = await db.execute(select(Permission).filter(Permission.name == name))
        existing = result.scalars().first()
        if existing:
            changed = False
            if existing.description != description:
                existing.description = description
                changed = True
            perm_map[name] = existing
            if changed:
                print(f"  {yellow('~')} {name:<35} (updated)")
                updated += 1
            else:
                print(f"  {cyan('·')} {name:<35} (already exists)")
        else:
            perm = Permission(name=name, description=description)
            db.add(perm)
            await db.flush()
            perm_map[name] = perm
            print(f"  {green('+')} {name:<35} (created)")
            created += 1
    await db.commit()
    print(
        f"  → {green(f'{created} created')}, {yellow(f'{updated} updated')}, "
        f"{len(PERMISSIONS) - created - updated} unchanged"
    )
    return perm_map


async def seed_roles(db: AsyncSession, perm_map: dict[str, Permission]) -> None:
    """Upsert all roles and wire them to their permissions."""
    print(f"\n{bold('[ 2/3 ] Roles & permission assignments')}")
    roles_created = links_added = 0
    for role_name, description in ROLES.items():
        # Eagerly load permissions
        result = await db.execute(
            select(Role)
            .options(selectinload(Role.permissions))
            .filter(Role.name == role_name)
        )
        role = result.scalars().first()
        if not role:
            role = Role(
                name=role_name,
                description=description,
            )
            db.add(role)
            await db.flush()
            # Re-fetch with selectinload after flush for new roles
            result = await db.execute(
                select(Role)
                .options(selectinload(Role.permissions))
                .filter(Role.name == role_name)
            )
            role = result.scalars().first()
            print(f"  {green('+')} {role_name}")
            roles_created += 1
        else:
            print(f"  {cyan('·')} {role_name} (exists)")
        
        expected_perm_names: list[str] = ROLE_PERMISSION_MAP.get(role_name, [])
        current_perm_names: set[str] = {p.name for p in role.permissions}
        for perm_name in expected_perm_names:
            if perm_name not in perm_map:
                print(f"    {yellow('!')} Unknown permission '{perm_name}' — skipped")
                continue
            if perm_name not in current_perm_names:
                role.permissions.append(perm_map[perm_name])
                print(f"    {green('+')} linked → {perm_name}")
                links_added += 1
            else:
                print(f"    {cyan('·')} linked · {perm_name}")
    await db.commit()
    print(
        f"  → {green(f'{roles_created} roles created')}, "
        f"{green(f'{links_added} permission links added')}"
    )


async def seed_superuser(db: AsyncSession) -> None:
    """
    Create the default superuser if none exists, and ensure they always
    have the 'SUPER_ADMIN' role assigned via UserRole.
    """
    print(f"\n{bold('[ 3/3 ] Default superuser')}")

    # Eagerly load permissions on admin_role
    role_result = await db.execute(
        select(Role)
        .options(selectinload(Role.permissions))
        .filter(Role.name == DEFAULT_SUPERUSER["role"])
    )
    admin_role = role_result.scalars().first()
    if not admin_role:
        print(f"  {yellow('!')} '{DEFAULT_SUPERUSER['role']}' role not found — check roles setup")
        return

    # Eagerly load roles on existing superuser by email
    user_result = await db.execute(
        select(User)
        .options(selectinload(User.roles).selectinload(UserRole.role))
        .filter(User.email == DEFAULT_SUPERUSER["email"])
    )
    existing_super = user_result.scalars().first()
    if existing_super:
        # Backfill: assign admin role if missing
        current_role_names = {ur.role.name for ur in existing_super.roles if ur.role}
        if DEFAULT_SUPERUSER["role"] not in current_role_names:
            user_role = UserRole(user_id=existing_super.id, role_id=admin_role.id)
            db.add(user_role)
            await db.commit()
            print(f"  {yellow('~')} {existing_super.email} — backfilled '{DEFAULT_SUPERUSER['role']}' role")
        else:
            print(f"  {cyan('·')} {existing_super.email} — already has '{DEFAULT_SUPERUSER['role']}' role")
        return

    # Create the user
    user = User(
        first_name=DEFAULT_SUPERUSER["first_name"],
        last_name=DEFAULT_SUPERUSER["last_name"],
        email=DEFAULT_SUPERUSER["email"],
        hashed_password=get_password_hash(DEFAULT_SUPERUSER["password"]),
        global_role=GlobalRole.SUPER_ADMIN,
        status=AccountStatus.ACTIVE,
        is_email_verified=True,
    )
    db.add(user)
    await db.flush()  # Flush to get user.id

    # Map role
    user_role = UserRole(user_id=user.id, role_id=admin_role.id)
    db.add(user_role)

    await db.commit()
    print(f"  {green('+')} Created superuser  : {user.email}")
    print(
        f"  {green('+')} Role assigned       : {DEFAULT_SUPERUSER['role']} ({len(admin_role.permissions)} permissions)"
    )
    print(f"  {yellow('⚠')}  Default password   : {DEFAULT_SUPERUSER['password']}")


# ── Main ──────────────────────────────────────────────────────────────────────

async def run_seed() -> None:
    print(bold("\n══════════════════════════════════════════"))
    print(bold("  Church Management System — Seeder"))
    print(bold("══════════════════════════════════════════"))
    async with AsyncSessionLocal() as db:
        try:
            perm_map = await seed_permissions(db)
            await seed_roles(db, perm_map)
            await seed_superuser(db)
            print(f"\n{green(bold('✓ Seeding complete.'))}\n")
        except Exception as e:
            await db.rollback()
            print(f"\n\033[91m✗ Seeding failed: {e}\033[0m\n")
            raise

if __name__ == "__main__":
    asyncio.run(run_seed())
