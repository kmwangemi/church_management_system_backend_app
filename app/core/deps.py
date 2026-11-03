from dataclasses import dataclass
from typing import Annotated, Optional

from app.core.security import decode_access_token
from app.db.session import get_db
from app.enums import AccountStatus, MembershipStatus, PlatformRole
from app.models.auth import MemberRoleAssignment, Permission, role_permission
from app.models.church import Church
from app.models.member import Member
from app.models.user import User
from fastapi import Depends, Header, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from jwt.exceptions import PyJWTError
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

# auto_error=False so a missing/invalid header is always a 401 (older FastAPI
# versions answered 403 for a missing header).
security = HTTPBearer(auto_error=False)

DbSession = Annotated[AsyncSession, Depends(get_db)]
TokenDep = Annotated[Optional[HTTPAuthorizationCredentials], Depends(security)]


# --------------------------------------------------------------------------- #
# Who is the login?  (User = identity)
# --------------------------------------------------------------------------- #
async def get_current_user(session: DbSession, token: TokenDep) -> User:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if token is None:
        raise credentials_exception
    try:
        # Pins the algorithm, requires exp + sub, rejects non-access tokens.
        payload = decode_access_token(token.credentials)
    except PyJWTError:
        raise credentials_exception
    user = (
        await session.execute(select(User).where(User.id == payload["sub"]))
    ).scalar_one_or_none()
    # A soft-deleted account is treated like one that doesn't exist.
    if user is None or user.is_deleted:
        raise credentials_exception
    if user.status != AccountStatus.ACTIVE:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail="Account is not active"
        )
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


async def get_current_superadmin(current_user: CurrentUser) -> User:
    # Platform-wide admin only. Church roles are checked via permissions.
    if current_user.platform_role != PlatformRole.SUPER_ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="The user does not have superadmin privileges",
        )
    return current_user


CurrentSuperAdmin = Annotated[User, Depends(get_current_superadmin)]


# --------------------------------------------------------------------------- #
# Which church / person is this request for?  (Member = church-scoped profile)
# --------------------------------------------------------------------------- #
async def resolve_member(
    session: AsyncSession, user: User, church_id: Optional[str] = None
) -> Member:
    """
    The user's active Member row in a church. A user has one per church, so the
    church must be named (X-Church-Id header / organizationId); it may be omitted
    only when the user belongs to exactly one church.
    """
    stmt = (
        select(Member)
        .join(Church, Church.id == Member.church_id)
        .where(
            Member.user_id == user.id,
            Member.deleted_at.is_(None),
            Member.status == MembershipStatus.ACTIVE,
            Church.deleted_at.is_(None),
            Church.is_active.is_(True),
            Church.is_suspended.is_(False),
        )
    )
    if church_id:
        stmt = stmt.where(Member.church_id == church_id)
    members = (await session.execute(stmt)).scalars().all()
    if not members:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No active membership in this church",
        )
    if len(members) > 1:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="X-Church-Id header is required (you belong to several churches)",
        )
    return members[0]


async def get_current_member(
    session: DbSession,
    current_user: CurrentUser,
    x_church_id: Annotated[Optional[str], Header(alias="X-Church-Id")] = None,
) -> Member:
    return await resolve_member(session, current_user, x_church_id)


CurrentMember = Annotated[Member, Depends(get_current_member)]


async def member_has_permission(
    session: AsyncSession,
    member_id: str,
    permission: str,
    branch_id: Optional[str] = None,
) -> bool:
    """
    True if one of the member's roles grants `permission`.
    - Church-wide role assignments (branch_id NULL) always count.
    - Branch-scoped assignments count only when `branch_id` is that branch.
    """
    scope = MemberRoleAssignment.branch_id.is_(None)
    if branch_id is not None:
        scope = or_(scope, MemberRoleAssignment.branch_id == branch_id)
    stmt = (
        select(Permission.id)
        .join(role_permission, role_permission.c.permission_id == Permission.id)
        .join(
            MemberRoleAssignment,
            MemberRoleAssignment.role_id == role_permission.c.role_id,
        )
        .where(
            MemberRoleAssignment.member_id == member_id,
            Permission.name == permission,
            scope,
        )
        .limit(1)
    )
    return (await session.execute(stmt)).first() is not None


def require_permission(permission: str):
    """Dependency form: church-wide permission check using the X-Church-Id header."""
    async def _dependency(session: DbSession, member: CurrentMember) -> Member:
        if not await member_has_permission(session, member.id, permission):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Missing permission: {permission}",
            )
        return member
    return _dependency


# --------------------------------------------------------------------------- #
# Inline authorization for routes that take organizationId in query/body
# --------------------------------------------------------------------------- #
@dataclass
class Actor:
    user: User
    member: Optional[Member]  # None for platform super-admins
    church_id: str


def church_id_from(request: Request, explicit: Optional[str] = None) -> Optional[str]:
    """organizationId (query/body) wins, then the X-Church-Id header."""
    value = (explicit or "").strip() or (
        request.headers.get("x-church-id") or ""
    ).strip()
    return value or None


async def resolve_actor(
    session: AsyncSession, user: User, church_id: Optional[str]
) -> Actor:
    if user.platform_role == PlatformRole.SUPER_ADMIN:
        if not church_id:
            raise HTTPException(
                status_code=400, detail="organizationId (or X-Church-Id) is required"
            )
        church = (
            await session.execute(
                select(Church).where(
                    Church.id == church_id, Church.deleted_at.is_(None)
                )
            )
        ).scalar_one_or_none()
        if not church:
            raise HTTPException(status_code=404, detail="Church not found")
        return Actor(user, None, church.id)
    member = await resolve_member(session, user, church_id)
    return Actor(user, member, member.church_id)


async def require(
    session: AsyncSession,
    actor: Actor,
    permission: str,
    branch_id: Optional[str] = None,
) -> None:
    if actor.member is None:  # platform super-admin
        return
    if not await member_has_permission(session, actor.member.id, permission, branch_id):
        raise HTTPException(status_code=403, detail=f"Missing permission: {permission}")


async def authorize(
    session: AsyncSession,
    user: User,
    church_id: Optional[str],
    permission: str,
    branch_id: Optional[str] = None,
) -> Actor:
    actor = await resolve_actor(session, user, church_id)
    await require(session, actor, permission, branch_id)
    return actor
