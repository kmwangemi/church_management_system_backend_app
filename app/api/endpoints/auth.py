from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from sqlalchemy import select, update

from app.api.deps import CurrentUser, DbSession
from app.core.security import (
    create_access_token,
    create_refresh_token,
    get_password_hash,
    verify_password,
)
from app.models.user import User, UserSession
from app.schemas.user import LoginRequest, Token, UserCreate, UserResponse
from app.services.audit import log_action

router = APIRouter()


@router.post("/login", response_model=Token)
async def login_access_token(
    request: Request,
    background_tasks: BackgroundTasks,
    session: DbSession,
    payload: LoginRequest,
):
    stmt = select(User).where(User.email == payload.email)
    result = await session.execute(stmt)
    user = result.scalar_one_or_none()
    if not user or not user.hashed_password:
        raise HTTPException(status_code=400, detail="Incorrect email or password")
    if not verify_password(payload.password, user.hashed_password):
        raise HTTPException(status_code=400, detail="Incorrect email or password")
    if user.is_deleted:
        raise HTTPException(status_code=400, detail="Inactive user")
    access_token = create_access_token(subject=user.id)
    refresh_token = create_refresh_token()
    # Create session
    user_session = UserSession(
        user_id=user.id,
        refresh_token=refresh_token,
        expires_at=datetime.now(timezone.utc) + timedelta(days=30),
        ip_address=request.client.host,
        user_agent=request.headers.get("user-agent"),
    )
    session.add(user_session)
    user.last_login = datetime.now(timezone.utc)
    await session.commit()
    # Background audit log
    background_tasks.add_task(
        log_action,
        action="USER_LOGIN",
        resource_type="USER",
        resource_id=user.id,
        user_id=user.id,
        ip_address=request.client.host,
        user_agent=request.headers.get("user-agent"),
    )
    user_data = {
        "id": str(user.id),
        "sub": str(user.id),
        "first_name": user.first_name or "",
        "last_name": user.last_name or "",
        "firstName": user.first_name or "",
        "lastName": user.last_name or "",
        "name": f"{user.first_name or ''} {user.last_name or ''}".strip(),
        "fullName": f"{user.first_name or ''} {user.last_name or ''}".strip(),
        "email": user.email,
        "profile_picture_url": user.profile_picture_url or "",
        "profilePictureUrl": user.profile_picture_url or "",
        "role": (
            user.global_role.value
            if hasattr(user.global_role, "value")
            else str(user.global_role)
        ),
        "globalRole": (
            user.global_role.value
            if hasattr(user.global_role, "value")
            else str(user.global_role)
        ),
        "churchId": "",
        "branchId": "",
    }
    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": "bearer",
        "user": user_data,
    }


@router.post("/refresh", response_model=Token)
async def refresh_access_token(
    request: Request, refresh_token: str, session: DbSession
):
    """
    Refresh access token using a valid refresh token.
    """
    stmt = select(UserSession).where(
        UserSession.refresh_token == refresh_token,
        UserSession.is_valid == True,
        UserSession.expires_at > datetime.now(timezone.utc),
    )
    result = await session.execute(stmt)
    user_session = result.scalar_one_or_none()
    if not user_session:
        raise HTTPException(status_code=401, detail="Invalid or expired refresh token")
    # Rotate token
    new_refresh_token = create_refresh_token()
    user_session.refresh_token = new_refresh_token
    user_session.expires_at = datetime.now(timezone.utc) + timedelta(days=30)
    user_session.ip_address = request.client.host
    user_session.user_agent = request.headers.get("user-agent")
    await session.commit()
    new_access_token = create_access_token(subject=user_session.user_id)
    user_stmt = select(User).where(User.id == user_session.user_id)
    user_res = await session.execute(user_stmt)
    user = user_res.scalar_one_or_none()
    user_data = None
    if user:
        user_data = {
            "id": str(user.id),
            "sub": str(user.id),
            "first_name": user.first_name or "",
            "last_name": user.last_name or "",
            "firstName": user.first_name or "",
            "lastName": user.last_name or "",
            "name": f"{user.first_name or ''} {user.last_name or ''}".strip(),
            "fullName": f"{user.first_name or ''} {user.last_name or ''}".strip(),
            "email": user.email,
            "profile_picture_url": user.profile_picture_url or "",
            "profilePictureUrl": user.profile_picture_url or "",
            "role": (
                user.global_role.value
                if hasattr(user.global_role, "value")
                else str(user.global_role)
            ),
            "globalRole": (
                user.global_role.value
                if hasattr(user.global_role, "value")
                else str(user.global_role)
            ),
            "churchId": "",
            "branchId": "",
        }
    return {
        "access_token": new_access_token,
        "refresh_token": new_refresh_token,
        "token_type": "bearer",
        "user": user_data,
    }


@router.post("/logout")
async def logout(refresh_token: str, session: DbSession):
    """
    Revoke a specific session.
    """
    stmt = (
        update(UserSession)
        .where(UserSession.refresh_token == refresh_token)
        .values(is_valid=False)
    )
    await session.execute(stmt)
    await session.commit()
    return {"message": "Successfully logged out"}


@router.post("/logout-all")
async def logout_all(current_user: CurrentUser, session: DbSession):
    """
    Revoke all sessions for the current user.
    """
    stmt = (
        update(UserSession)
        .where(UserSession.user_id == current_user.id)
        .values(is_valid=False)
    )
    await session.execute(stmt)
    await session.commit()
    return {"message": "Successfully logged out from all devices"}


@router.post("/signup", response_model=UserResponse)
async def signup(
    request: Request,
    background_tasks: BackgroundTasks,
    session: DbSession,
    user_in: UserCreate,
):
    stmt = select(User).where(User.email == user_in.email)
    result = await session.execute(stmt)
    if result.scalar_one_or_none():
        raise HTTPException(
            status_code=400,
            detail="The user with this email already exists in the system",
        )
    user = User(
        email=user_in.email,
        first_name=user_in.first_name,
        last_name=user_in.last_name,
        phone_number=user_in.phone_number,
        hashed_password=get_password_hash(user_in.password),
    )
    session.add(user)
    await session.commit()
    await session.refresh(user)
    # Background audit log
    background_tasks.add_task(
        log_action,
        action="USER_SIGNUP",
        resource_type="USER",
        resource_id=user.id,
        user_id=user.id,
        ip_address=request.client.host,
        user_agent=request.headers.get("user-agent"),
    )
    return user


@router.get("/verify")
async def verify_auth(current_user: CurrentUser):
    """
    Verify current session and return user data for frontend auth state.
    """
    return {
        "user": {
            "sub": str(current_user.id),
            "first_name": (current_user.first_name if current_user.first_name else ""),
            "last_name": (current_user.last_name if current_user.last_name else ""),
            "email": current_user.email,
            "profile_picture_url": current_user.profile_picture_url or "",
            "role": (
                current_user.global_role.value if current_user.global_role else "USER"
            ),
            "globalRole": (
                current_user.global_role.value if current_user.global_role else "USER"
            ),
            "churchId": "",
            "branchId": "",
        }
    }
