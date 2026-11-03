"""
Permissions, system roles, plans and the first super admin.

Single source of truth used by scripts/run.py (verbose, interactive seeding), scripts/seed_data.py
(re-exports) and tests (`await seed_all(session)`). Every function is idempotent and never
deletes anything: things that are no longer used are reported as "legacy", not removed.

Without these rows nobody except platform super-admins can do anything: every church-level
endpoint checks a permission through MemberRoleAssignment -> Role -> role_permission.
"""

from typing import Callable, Dict, List, Optional

from app.core.security import get_password_hash
from app.enums import AccountStatus, ChurchPlan, PlatformRole
from app.models.auth import Permission, Role, role_permission
from app.models.subscription import Plan
from app.models.user import User
from sqlalchemy import func, insert, select
from sqlalchemy.ext.asyncio import AsyncSession

# report(kind, action, name): kind = permission | role | link | plan | superuser,
# action = created | updated | exists | renamed | legacy | warning | skipped
Report = Optional[Callable[[str, str, str], None]]


def _say(report: Report, kind: str, action: str, name: str) -> None:
    if report:
        report(kind, action, name)


PERMISSIONS: Dict[str, str] = {
    # Members & branches
    "member.read": "View members",
    "member.create": "Add members",
    "member.update": "Edit members",
    "member.delete": "Remove members",
    "branch.read": "View branches",
    "branch.create": "Create branches",
    "branch.update": "Edit branches",
    "branch.delete": "Delete branches",
    "role.assign": "Assign roles to members",
    # Events & groups
    "event.create": "Create events",
    "event.update": "Edit or cancel events",
    "event.delete": "Delete events",
    "group.create": "Create groups",
    "group.delete": "Delete groups",
    # Finance
    "offering.read": "View offerings (financial records)",
    "offering.create": "Record offerings",
    "fund.manage": "Create and manage giving funds",
    # Not used by any endpoint yet (kept from the old seed so the roles are ready for them)
    "audit.read": "View system audit logs",
    "content.manage": "Manage sermons, announcements and content",
    "communication.send": "Send bulk emails and SMS to members",
}
_ALL: List[str] = list(PERMISSIONS)

# System roles: church_id is NULL, shared by every church. SUPER_ADMIN is NOT a role any more:
# it is User.platform_role (a platform super-admin has no church membership).
ROLE_DESCRIPTIONS: Dict[str, str] = {
    "OWNER": "Church owner: full access within their church",
    "ADMIN": "Church administrator: full access within their church",
    "PASTOR": "Pastor: manages members, events and groups",
    "BISHOP": "Bishop: manages members, events and groups",
    "STAFF": "Church staff",
    "TREASURER": "Records and reviews giving",
    "VOLUNTEER": "Ministry volunteer",
    "MEMBER": "Regular church member with basic access",
    "VISITOR": "Visitor / guest",
}

DEFAULT_ROLE_PERMISSIONS: Dict[str, List[str]] = {
    "OWNER": _ALL,
    "ADMIN": _ALL,
    "PASTOR": [
        "member.read",
        "member.update",
        "branch.read",
        "event.create",
        "event.update",
        "group.create",
    ],
    "BISHOP": [
        "member.read",
        "member.update",
        "branch.read",
        "event.create",
        "event.update",
        "group.create",
    ],
    "STAFF": [
        "member.read",
        "branch.read",
        "event.create",
        "event.update",
        "group.create",
    ],
    "TREASURER": ["member.read", "offering.read", "offering.create", "fund.manage"],
    "VOLUNTEER": [],
    "MEMBER": [],
    "VISITOR": [],
}

# Roles that make someone a "church admin" (used for the legacy `globalRole` API field).
ADMIN_ROLE_NAMES = {"ADMIN", "OWNER"}

for _role, _perms in DEFAULT_ROLE_PERMISSIONS.items():  # fail at import, not at 3am
    if _role not in ROLE_DESCRIPTIONS:
        raise ValueError(f"DEFAULT_ROLE_PERMISSIONS references unknown role '{_role}'")
    if set(_perms) - set(PERMISSIONS):
        raise ValueError(
            f"Role '{_role}' references unknown permission(s): {set(_perms) - set(PERMISSIONS)}"
        )


async def seed_permissions(
    session: AsyncSession, report: Report = None
) -> Dict[str, str]:
    """Upsert PERMISSIONS. Returns name -> id."""
    existing = {
        p.name: p for p in (await session.execute(select(Permission))).scalars().all()
    }
    ids: Dict[str, str] = {}
    for name, desc in PERMISSIONS.items():
        perm = existing.get(name)
        if perm is None:
            perm = Permission(name=name, description=desc)
            session.add(perm)
            _say(report, "permission", "created", name)
        elif perm.description != desc:
            perm.description = desc
            _say(report, "permission", "updated", name)
        else:
            _say(report, "permission", "exists", name)
        ids[name] = perm
    await session.flush()
    ids = {name: p.id for name, p in ids.items()}
    for name in sorted(set(existing) - set(PERMISSIONS)):
        _say(
            report, "permission", "legacy", name
        )  # from the old seed; no endpoint checks it
    await session.commit()
    return ids


async def _rename_legacy_roles(session: AsyncSession, report: Report) -> None:
    """The old system role CHURCH_ADMIN is now ADMIN (keeps its id, so existing links survive)."""
    legacy = (
        await session.execute(
            select(Role).where(
                Role.church_id.is_(None), func.upper(Role.name) == "CHURCH_ADMIN"
            )
        )
    ).scalar_one_or_none()
    if not legacy:
        return
    admin_exists = (
        await session.execute(
            select(Role.id).where(
                Role.church_id.is_(None), func.upper(Role.name) == "ADMIN"
            )
        )
    ).first()
    if admin_exists:
        _say(
            report,
            "role",
            "warning",
            "CHURCH_ADMIN exists next to ADMIN: move its members to ADMIN, then delete it",
        )
        return
    legacy.name = "ADMIN"
    await session.flush()
    _say(report, "role", "renamed", "CHURCH_ADMIN -> ADMIN")


async def seed_roles(
    session: AsyncSession, perm_ids: Dict[str, str], report: Report = None
) -> None:
    """Upsert the system roles and link them to their permissions (adds links, never removes)."""
    await _rename_legacy_roles(session, report)
    roles = {
        r.name.upper(): r
        for r in (await session.execute(select(Role).where(Role.church_id.is_(None))))
        .scalars()
        .all()
    }
    role_ids: Dict[str, str] = {}
    for name, desc in ROLE_DESCRIPTIONS.items():
        role = roles.get(name)
        if role is None:
            role = Role(name=name, description=desc, church_id=None, is_system=True)
            session.add(role)
            _say(report, "role", "created", name)
        else:
            changed = False
            if not role.is_system:
                role.is_system, changed = True, True
            if role.description != desc:
                role.description, changed = desc, True
            _say(report, "role", "updated" if changed else "exists", name)
        role_ids[name] = role
    await session.flush()
    role_ids = {name: r.id for name, r in role_ids.items()}
    have = {
        (rid, pid)
        for rid, pid in (
            await session.execute(
                select(role_permission.c.role_id, role_permission.c.permission_id)
            )
        ).all()
    }
    for rname, pnames in DEFAULT_ROLE_PERMISSIONS.items():
        for pname in pnames:
            key = (role_ids[rname], perm_ids[pname])
            if key not in have:
                await session.execute(
                    insert(role_permission).values(role_id=key[0], permission_id=key[1])
                )
                have.add(key)
                _say(report, "link", "created", f"{rname} -> {pname}")
    for name in sorted(set(roles) - set(ROLE_DESCRIPTIONS)):
        _say(report, "role", "legacy", name)  # e.g. SUPER_ADMIN: now User.platform_role
    await session.commit()


async def seed_plans(session: AsyncSession, report: Report = None) -> None:
    """One Plan row per ChurchPlan value (price 0: set real prices in the database)."""
    have = {
        p.name.upper() for p in (await session.execute(select(Plan))).scalars().all()
    }
    for plan in ChurchPlan:
        if plan.value in have:
            _say(report, "plan", "exists", plan.value)
        else:
            session.add(Plan(name=plan.value))
            _say(report, "plan", "created", plan.value)
    await session.commit()


async def seed_superuser(
    session: AsyncSession,
    email: str,
    password: str,
    display_name: str = "Super Admin",
    report: Report = None,
) -> None:
    """
    Make sure the configured platform super admin exists.
    - email already has an account -> it is promoted to SUPER_ADMIN (password is never changed)
    - else, if some other super admin already exists -> nothing is created
    - else the account is created.
    The password is never printed.
    """
    email = email.strip().lower()
    user = (
        await session.execute(select(User).where(func.lower(User.email) == email))
    ).scalar_one_or_none()
    if user:
        if user.is_deleted:
            _say(
                report,
                "superuser",
                "warning",
                f"{email} is soft-deleted: restore it manually (clear deleted_at)",
            )
            return
        changed = user.platform_role != PlatformRole.SUPER_ADMIN
        user.platform_role = PlatformRole.SUPER_ADMIN
        if user.status != AccountStatus.ACTIVE:
            _say(
                report,
                "superuser",
                "warning",
                f"{email} is {user.status.value}: activate it manually",
            )
        await session.commit()
        _say(report, "superuser", "updated" if changed else "exists", email)
        return
    other = (
        await session.execute(
            select(User)
            .where(
                User.platform_role == PlatformRole.SUPER_ADMIN,
                User.deleted_at.is_(None),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if other:
        _say(
            report,
            "superuser",
            "skipped",
            f"a super admin already exists ({other.email}); not creating {email}",
        )
        return
    session.add(
        User(
            email=email,
            display_name=display_name,
            hashed_password=get_password_hash(password),
            platform_role=PlatformRole.SUPER_ADMIN,
            status=AccountStatus.ACTIVE,
            is_email_verified=True,
        )
    )
    await session.commit()
    _say(report, "superuser", "created", email)


async def seed_all(session: AsyncSession) -> None:
    """Permissions + roles + plans (no super user). For startup hooks and tests."""
    await seed_roles(session, await seed_permissions(session))
    await seed_plans(session)


# Older name, kept so existing callers keep working.
async def seed_system_roles(session: AsyncSession) -> None:
    await seed_roles(session, await seed_permissions(session))
