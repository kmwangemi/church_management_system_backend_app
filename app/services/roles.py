from typing import Dict, Iterable, List, Optional

from app.core.seed import ADMIN_ROLE_NAMES
from app.enums import MemberType, PlatformRole
from app.models.auth import MemberRoleAssignment, Role
from fastapi import HTTPException
from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession


def default_role_names(member_type: Optional[MemberType]) -> List[str]:
    return ["VISITOR"] if member_type == MemberType.VISITOR else ["MEMBER"]


async def resolve_roles(
    session: AsyncSession, church_id: str, names: Iterable[str]
) -> List[Role]:
    """Role rows for the given names. A church's own role beats the system role of the same name."""
    wanted = {n.strip().upper() for n in names if n and n.strip()}
    if not wanted:
        return []
    rows = (
        (
            await session.execute(
                select(Role).where(
                    or_(Role.church_id == church_id, Role.church_id.is_(None))
                )
            )
        )
        .scalars()
        .all()
    )
    by_name: Dict[str, Role] = {}
    for r in sorted(rows, key=lambda r: r.church_id is None):  # church-specific first
        by_name.setdefault(r.name.upper(), r)
    missing = sorted(wanted - set(by_name))
    if missing:
        raise HTTPException(
            status_code=422, detail=f"Unknown role(s): {', '.join(missing)}"
        )
    return [by_name[n] for n in sorted(wanted)]


async def set_member_roles(
    session: AsyncSession,
    member_id: str,
    roles: List[Role],
    branch_id: Optional[str] = None,
    replace: bool = True,
) -> None:
    """Church-wide (branch_id None) or branch-scoped assignments. `replace` clears that scope first."""
    scope = (
        MemberRoleAssignment.branch_id.is_(None)
        if branch_id is None
        else MemberRoleAssignment.branch_id == branch_id
    )
    if replace:
        await session.execute(
            delete(MemberRoleAssignment).where(
                MemberRoleAssignment.member_id == member_id, scope
            )
        )
        have: set = set()
    else:
        have = set(
            (
                await session.execute(
                    select(MemberRoleAssignment.role_id).where(
                        MemberRoleAssignment.member_id == member_id, scope
                    )
                )
            )
            .scalars()
            .all()
        )
    for role in roles:
        if role.id not in have:
            session.add(
                MemberRoleAssignment(
                    member_id=member_id, role_id=role.id, branch_id=branch_id
                )
            )


async def role_names_for_members(
    session: AsyncSession, member_ids: Iterable[str]
) -> Dict[str, List[str]]:
    ids = list(member_ids)
    out: Dict[str, List[str]] = {i: [] for i in ids}
    if not ids:
        return out
    rows = (
        await session.execute(
            select(MemberRoleAssignment.member_id, Role.name)
            .join(Role, Role.id == MemberRoleAssignment.role_id)
            .where(MemberRoleAssignment.member_id.in_(ids))
            .order_by(Role.name)
        )
    ).all()
    for mid, name in rows:
        if name not in out[mid]:
            out[mid].append(name)
    return out


def legacy_global_role(platform_role: PlatformRole, role_names: Iterable[str]) -> str:
    """Keeps the old frontend contract: SUPER_ADMIN / CHURCH_ADMIN / MEMBER."""
    if platform_role == PlatformRole.SUPER_ADMIN:
        return "SUPER_ADMIN"
    if any(r.upper() in ADMIN_ROLE_NAMES for r in role_names):
        return "CHURCH_ADMIN"
    return "MEMBER"
