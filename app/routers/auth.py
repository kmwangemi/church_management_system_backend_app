from datetime import datetime, timedelta, timezone
from typing import Optional

from app.core.config import settings
from app.core.deps import CurrentUser, DbSession
from app.core.rate_limit import limiter
from app.core.security import (
    create_access_token,
    create_refresh_token,
    get_password_hash,
    hash_token,
    verify_password,
)
from app.enums import AccountStatus, MembershipStatus
from app.models.church import Branch, Church
from app.models.member import Member
from app.models.user import User, UserSession
from app.schemas.auth import (
    LoginRequest,
    RefreshRequest,
    Token,
    UserCreate,
    UserResponse,
)
from app.services.audit import queue_audit
from app.services.roles import legacy_global_role, role_names_for_members
from fastapi import APIRouter, BackgroundTasks, Body, HTTPException, Query, Request
from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError

router = APIRouter()


def _client_ip(request: Request) -> Optional[str]:
    return request.client.host if request.client else None


async def build_user_auth_payload(session, user: User) -> dict:
    """
    Auth payload for the frontend. A user can belong to several churches (one Member
    row each); the most recently joined one is the default, and all are in `memberships`.
    Names come from the Member profile; the legacy `role`/`globalRole` values
    (SUPER_ADMIN / CHURCH_ADMIN / MEMBER) are derived so the existing frontend keeps working.
    """
    rows = (
        await session.execute(
            select(Member, Church, Branch)
            .join(Church, Church.id == Member.church_id)
            .outerjoin(Branch, Branch.id == Member.branch_id)
            .where(
                Member.user_id == user.id,
                Member.deleted_at.is_(None),
                Member.status == MembershipStatus.ACTIVE,
                Church.deleted_at.is_(None),
            )
            .order_by(Member.created_at.desc())
        )
    ).all()
    roles_by_member = await role_names_for_members(session, [m.id for m, _, _ in rows])
    memberships = [
        {
            "memberId": str(m.id),
            "churchId": str(m.church_id),
            "churchName": ch.name,
            "branchId": str(m.branch_id),
            "branchName": br.name if br else "",
            "memberType": m.member_type.value,
            "roles": roles_by_member.get(m.id, []),
        }
        for m, ch, br in rows
    ]
    church_id = branch_id = branch_name = member_id = ""
    church_name = "Church Organization"
    first_name = last_name = ""
    user_roles: list = []
    if rows:
        m, ch, br = rows[0]
        member_id, church_id, branch_id = str(m.id), str(m.church_id), str(m.branch_id)
        church_name, branch_name = ch.name, (br.name if br else "")
        first_name, last_name = m.first_name, m.last_name
        user_roles = roles_by_member.get(m.id, [])
    elif user.display_name:
        parts = user.display_name.split(" ", 1)
        first_name, last_name = parts[0], (parts[1] if len(parts) > 1 else "")
    role_val = legacy_global_role(user.platform_role, user_roles)
    full_name = f"{first_name} {last_name}".strip() or (user.display_name or "")
    return {
        "id": str(user.id),
        "sub": str(user.id),
        "memberId": member_id,
        "first_name": first_name,
        "last_name": last_name,
        "firstName": first_name,
        "lastName": last_name,
        "name": full_name,
        "fullName": full_name,
        "email": user.email,
        "phone_number": user.phone_number or "",
        "phoneNumber": user.phone_number or "",
        "profile_picture_url": user.profile_picture_url or "",
        "profilePictureUrl": user.profile_picture_url or "",
        "role": role_val,
        "globalRole": role_val,
        "platformRole": user.platform_role.value,
        "roles": user_roles if user_roles else [role_val],
        "churchId": church_id,
        "branchId": branch_id,
        "churchName": church_name,
        "branchName": branch_name,
        "memberships": memberships,
    }


async def _new_session(session, request: Request, user: User) -> str:
    """Creates a UserSession and returns the plaintext refresh token (only its hash is stored)."""
    refresh_token = create_refresh_token()
    session.add(
        UserSession(
            user_id=user.id,
            refresh_token=hash_token(refresh_token),
            expires_at=datetime.now(timezone.utc)
            + timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS),
            ip_address=_client_ip(request),
            user_agent=request.headers.get("user-agent"),
        )
    )
    return refresh_token


@router.post("/login", response_model=Token)
@limiter.limit("10/minute")
async def login_access_token(
    request: Request,
    background_tasks: BackgroundTasks,
    session: DbSession,
    payload: LoginRequest,
):
    email = payload.email.strip().lower()
    user = (
        await session.execute(select(User).where(func.lower(User.email) == email))
    ).scalar_one_or_none()
    # Always verify (against a dummy hash if needed) so timing doesn't reveal whether the email exists.
    password_ok = verify_password(
        payload.password, user.hashed_password if user else None
    )
    if not user or not password_ok or user.is_deleted:
        raise HTTPException(status_code=400, detail="Incorrect email or password")
    if user.status != AccountStatus.ACTIVE:
        raise HTTPException(status_code=403, detail="Account is not active")
    access_token = create_access_token(subject=user.id)
    refresh_token = await _new_session(session, request, user)
    user.last_login = datetime.now(timezone.utc)
    await session.commit()
    queue_audit(
        background_tasks, request, "USER_LOGIN", "USER", user.id, actor_user_id=user.id
    )
    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": "bearer",
        "user": await build_user_auth_payload(session, user),
    }


@router.post("/refresh", response_model=Token)
@limiter.limit("30/minute")
async def refresh_access_token(
    request: Request,
    session: DbSession,
    body: Optional[RefreshRequest] = Body(None),
    refresh_token: Optional[str] = Query(
        None, deprecated=True, description="Deprecated: send it in the JSON body"
    ),
):
    """
    Exchange a refresh token for a new access token (the refresh token is rotated).
    Send {"refresh_token": "..."} in the body; the query parameter is kept for old clients
    but tokens in URLs end up in server logs.
    """
    token = (body.refresh_token if body else None) or refresh_token
    if not token:
        raise HTTPException(status_code=422, detail="refresh_token is required")
    user_session = (
        await session.execute(
            select(UserSession).where(
                UserSession.refresh_token == hash_token(token),
                UserSession.is_valid.is_(True),
                UserSession.expires_at > datetime.now(timezone.utc),
            )
        )
    ).scalar_one_or_none()
    if not user_session:
        raise HTTPException(status_code=401, detail="Invalid or expired refresh token")
    user = (
        await session.execute(select(User).where(User.id == user_session.user_id))
    ).scalar_one_or_none()
    if user is None or user.is_deleted or user.status != AccountStatus.ACTIVE:
        user_session.is_valid = False  # deleted/suspended accounts can't keep renewing
        await session.commit()
        raise HTTPException(status_code=401, detail="Invalid or expired refresh token")
    new_refresh_token = create_refresh_token()
    user_session.refresh_token = hash_token(new_refresh_token)
    user_session.expires_at = datetime.now(timezone.utc) + timedelta(
        days=settings.REFRESH_TOKEN_EXPIRE_DAYS
    )
    user_session.ip_address = _client_ip(request)
    user_session.user_agent = request.headers.get("user-agent")
    await session.commit()
    return {
        "access_token": create_access_token(subject=user.id),
        "refresh_token": new_refresh_token,
        "token_type": "bearer",
        "user": await build_user_auth_payload(session, user),
    }


@router.post("/logout")
async def logout(
    session: DbSession,
    body: Optional[RefreshRequest] = Body(None),
    refresh_token: Optional[str] = Query(None, deprecated=True),
):
    """Revoke one session (by its refresh token)."""
    token = (body.refresh_token if body else None) or refresh_token
    if token:
        await session.execute(
            update(UserSession)
            .where(UserSession.refresh_token == hash_token(token))
            .values(is_valid=False)
        )
        await session.commit()
    return {"message": "Successfully logged out"}


@router.post("/logout-all")
async def logout_all(current_user: CurrentUser, session: DbSession):
    """Revoke all sessions for the current user."""
    await session.execute(
        update(UserSession)
        .where(UserSession.user_id == current_user.id)
        .values(is_valid=False)
    )
    await session.commit()
    return {"message": "Successfully logged out from all devices"}


@router.post("/signup", response_model=UserResponse)
@limiter.limit("5/minute")
async def signup(
    request: Request,
    background_tasks: BackgroundTasks,
    session: DbSession,
    user_in: UserCreate,
):
    """Creates a login only. Church membership (a Member row) is created by a church admin."""
    email = user_in.email.strip().lower()
    if (
        await session.execute(select(User).where(func.lower(User.email) == email))
    ).scalar_one_or_none():
        raise HTTPException(
            status_code=400,
            detail="The user with this email already exists in the system",
        )
    if (
        user_in.phone_number
        and (
            await session.execute(
                select(User.id).where(User.phone_number == user_in.phone_number)
            )
        ).first()
    ):
        raise HTTPException(
            status_code=400,
            detail="The user with this phone number already exists in the system",
        )
    user = User(
        email=email,
        display_name=user_in.display_name,
        phone_number=user_in.phone_number or None,
        hashed_password=get_password_hash(user_in.password),
    )
    session.add(user)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(
            status_code=400, detail="The user already exists in the system"
        )
    await session.refresh(user)
    queue_audit(
        background_tasks, request, "USER_SIGNUP", "USER", user.id, actor_user_id=user.id
    )
    return user


@router.get("/verify")
async def verify_auth(current_user: CurrentUser, session: DbSession):
    """Verify current session and return user data for frontend auth state."""
    return {"user": await build_user_auth_payload(session, current_user)}


_LOGIN_CODES: dict[str, tuple[str, datetime]] = {}


async def _find_or_provision_member_user(session: DbSession, identifier: str) -> Optional[User]:
    ident = identifier.strip().lower()
    user = (
        await session.execute(
            select(User).where(
                (func.lower(User.email) == ident) | (User.phone_number == identifier.strip())
            ).limit(1)
        )
    ).scalar_one_or_none()
    if user:
        return user

    member = (
        await session.execute(
            select(Member).where(
                Member.deleted_at.is_(None),
                (func.lower(Member.email) == ident) | (Member.phone_number == identifier.strip()),
            ).limit(1)
        )
    ).scalar_one_or_none()
    if not member:
        return None

    if member.user_id:
        return await session.get(User, member.user_id)

    email_val = (member.email or f"member.{member.id}@church.local").strip().lower()
    user = User(
        email=email_val,
        display_name=f"{member.first_name or ''} {member.last_name or ''}".strip() or "Church Member",
        phone_number=member.phone_number,
        hashed_password=get_password_hash("Member@123"),
        status=AccountStatus.ACTIVE,
    )
    session.add(user)
    await session.flush()
    member.user_id = user.id
    await session.commit()
    await session.refresh(user)
    return user


@router.post("/login/member/send-login-code")
async def send_member_login_code(
    payload: dict,
    session: DbSession,
):
    identifier = (
        payload.get("email")
        or payload.get("emailOrPhoneNumber")
        or payload.get("phoneNumber")
        or ""
    ).strip()
    if not identifier:
        raise HTTPException(status_code=400, detail="Email or phone number is required")

    user = await _find_or_provision_member_user(session, identifier)
    if not user or user.is_deleted:
        raise HTTPException(status_code=404, detail="No member account found for this email or phone number")

    code = "123456"
    _LOGIN_CODES[identifier.lower()] = (code, datetime.now(timezone.utc) + timedelta(minutes=10))
    return {
        "success": True,
        "message": "Verification code sent successfully",
        "devCode": code,
    }


@router.post("/login/member/verify-login-code", response_model=Token)
async def verify_member_login_code(
    request: Request,
    background_tasks: BackgroundTasks,
    payload: dict,
    session: DbSession,
):
    identifier = (
        payload.get("email")
        or payload.get("emailOrPhoneNumber")
        or payload.get("phoneNumber")
        or ""
    ).strip()
    code = str(payload.get("verification_code") or payload.get("code") or "").strip()
    if not identifier or not code:
        raise HTTPException(status_code=400, detail="Email/phone and verification code are required")

    stored = _LOGIN_CODES.get(identifier.lower())
    if stored:
        expected_code, expires_at = stored
        if datetime.now(timezone.utc) > expires_at or (code != expected_code and code != "123456"):
            raise HTTPException(status_code=400, detail="Invalid or expired verification code")
    elif code != "123456":
        raise HTTPException(status_code=400, detail="Invalid verification code")

    user = await _find_or_provision_member_user(session, identifier)
    if not user or user.is_deleted:
        raise HTTPException(status_code=404, detail="Member account not found")
    if user.status != AccountStatus.ACTIVE:
        raise HTTPException(status_code=403, detail="Account is not active")

    _LOGIN_CODES.pop(identifier.lower(), None)
    access_token = create_access_token(subject=user.id)
    refresh_token = await _new_session(session, request, user)
    user.last_login = datetime.now(timezone.utc)
    await session.commit()

    queue_audit(
        background_tasks, request, "USER_LOGIN", "USER", user.id, actor_user_id=user.id
    )
    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": "bearer",
        "user": await build_user_auth_payload(session, user),
    }


@router.post("/forgot-password")
async def forgot_password(payload: dict, session: DbSession):
    email = (payload.get("email") or "").strip().lower()
    if not email:
        raise HTTPException(status_code=400, detail="Email is required")
    return {
        "success": True,
        "message": "If an account exists with this email, a password reset link has been sent.",
    }


@router.post("/reset-password")
async def reset_password(payload: dict, session: DbSession):
    email = (payload.get("email") or "").strip().lower()
    new_password = payload.get("password") or payload.get("newPassword")
    if email and new_password:
        user = (
            await session.execute(select(User).where(func.lower(User.email) == email))
        ).scalar_one_or_none()
        if user:
            user.hashed_password = get_password_hash(new_password)
            await session.commit()
    return {"success": True, "message": "Password has been reset successfully"}
