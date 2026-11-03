from datetime import datetime
from typing import List, Optional

from pydantic import AliasChoices, BaseModel, ConfigDict, EmailStr, Field

from app.enums import AccountStatus, MemberType, MembershipStatus, PlatformRole


class Token(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str
    user: Optional[dict] = None


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class UserBase(BaseModel):
    """Login identity only. Personal details (first/last name...) live on Member."""

    model_config = ConfigDict(populate_by_name=True)

    email: EmailStr
    # Older clients send `name`; it is accepted as an alias.
    display_name: Optional[str] = Field(
        default=None, validation_alias=AliasChoices("display_name", "name")
    )
    phone_number: Optional[str] = None


class UserCreate(UserBase):
    password: str = Field(min_length=8)


class UserResponse(UserBase):
    id: str
    platform_role: PlatformRole  # was: global_role
    status: AccountStatus
    is_email_verified: bool = False
    profile_picture_url: Optional[str] = None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True, populate_by_name=True)


class MembershipSummary(BaseModel):
    """One church profile of a user (a user has one Member row per church)."""

    id: str
    church_id: str
    branch_id: str
    member_type: MemberType
    first_name: str
    last_name: str
    status: MembershipStatus
    # Filled in by the service from MemberRoleAssignment -> Role.name
    role_names: List[str] = []

    model_config = ConfigDict(from_attributes=True)


class UserWithMembershipsResponse(UserResponse):
    """For /me and login. Load with selectinload(User.memberships) first (async-safe)."""
    memberships: List[MembershipSummary] = []


class RefreshRequest(BaseModel):
    refresh_token: str
