from datetime import datetime
from typing import TYPE_CHECKING, List, Optional

from app.db.base_class import Base, SoftDeleteMixin, TimestampMixin, UUIDMixin
from app.enums import AccountStatus, PlatformRole
from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

if TYPE_CHECKING:
    from app.models.auth import Account
    from app.models.member import Member
    from app.models.subscription import UserSubscription


class User(Base, UUIDMixin, TimestampMixin, SoftDeleteMixin):
    """Login identity only. Personal/church data lives on Member."""

    __tablename__ = "user"

    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    phone_number: Mapped[Optional[str]] = mapped_column(String(255), unique=True)
    display_name: Mapped[Optional[str]] = mapped_column(String(255))
    is_email_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    hashed_password: Mapped[Optional[str]] = mapped_column(String(255))
    profile_picture_url: Mapped[Optional[str]] = mapped_column(String(500))
    # NONE for everyone except platform super-admins. Church roles live in MemberRoleAssignment.
    platform_role: Mapped[PlatformRole] = mapped_column(
        Enum(PlatformRole), default=PlatformRole.NONE
    )
    status: Mapped[AccountStatus] = mapped_column(
        Enum(AccountStatus), default=AccountStatus.ACTIVE
    )
    last_login: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    # Relationships
    # One User can have one Member profile per church.
    memberships: Mapped[List["Member"]] = relationship("Member", back_populates="user")
    sessions: Mapped[List["UserSession"]] = relationship(
        "UserSession", back_populates="user"
    )
    accounts: Mapped[List["Account"]] = relationship("Account", back_populates="user")
    subscriptions: Mapped[List["UserSubscription"]] = relationship(
        "UserSubscription", back_populates="user"
    )


class UserSession(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "user_session"

    user_id: Mapped[str] = mapped_column(
        ForeignKey("user.id", ondelete="CASCADE"), index=True
    )
    refresh_token: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    is_valid: Mapped[bool] = mapped_column(Boolean, default=True)
    ip_address: Mapped[Optional[str]] = mapped_column(String(45))
    user_agent: Mapped[Optional[str]] = mapped_column(String(500))

    user: Mapped["User"] = relationship("User", back_populates="sessions")
