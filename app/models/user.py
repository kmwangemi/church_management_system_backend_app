from datetime import datetime
from typing import TYPE_CHECKING, List, Optional

from app.db.base_class import Base, TimestampMixin, UUIDMixin
from app.enums import AccountStatus, GlobalRole
from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

if TYPE_CHECKING:
    from app.models.auth import Account, Invitation, UserRole
    from app.models.detail import (
        Address,
        AdminDetails,
        BishopDetails,
        EmergencyContact,
        MemberDetails,
        PastorDetails,
        StaffDetails,
        VisitorDetails,
        VolunteerDetails,
    )
    from app.models.member import Member


class User(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "user"

    first_name: Mapped[str] = mapped_column(String(255), nullable=False)
    last_name: Mapped[str] = mapped_column(String(255), nullable=False)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    is_email_verified: Mapped[bool] = mapped_column(Boolean, default=False)
    hashed_password: Mapped[Optional[str]] = mapped_column(String(255))
    profile_picture_url: Mapped[Optional[str]] = mapped_column(String(500))
    phone_number: Mapped[Optional[str]] = mapped_column(String(255), unique=True)
    global_role: Mapped[GlobalRole] = mapped_column(
        Enum(GlobalRole), default=GlobalRole.MEMBER
    )
    status: Mapped[AccountStatus] = mapped_column(
        Enum(AccountStatus), default=AccountStatus.ACTIVE
    )
    last_login: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False)

    # Relationships
    roles: Mapped[List["UserRole"]] = relationship("UserRole", back_populates="user")
    memberships: Mapped[List["Member"]] = relationship("Member", back_populates="user")
    sessions: Mapped[List["UserSession"]] = relationship(
        "UserSession", back_populates="user"
    )
    accounts: Mapped[List["Account"]] = relationship("Account", back_populates="user")
    invitations_sent: Mapped[List["Invitation"]] = relationship(
        "Invitation", back_populates="inviter"
    )
    address: Mapped[Optional["Address"]] = relationship(
        "Address", back_populates="user", uselist=False
    )
    emergency_contact: Mapped[Optional["EmergencyContact"]] = relationship(
        "EmergencyContact", back_populates="user", uselist=False
    )
    bishop_details: Mapped[Optional["BishopDetails"]] = relationship(
        "BishopDetails", back_populates="user", uselist=False
    )
    admin_details: Mapped[Optional["AdminDetails"]] = relationship(
        "AdminDetails", back_populates="user", uselist=False
    )
    visitor_details: Mapped[Optional["VisitorDetails"]] = relationship(
        "VisitorDetails", back_populates="user", uselist=False
    )
    member_details: Mapped[Optional["MemberDetails"]] = relationship(
        "MemberDetails", back_populates="user", uselist=False
    )
    pastor_details: Mapped[Optional["PastorDetails"]] = relationship(
        "PastorDetails", back_populates="user", uselist=False
    )
    volunteer_details: Mapped[Optional["VolunteerDetails"]] = relationship(
        "VolunteerDetails", back_populates="user", uselist=False
    )
    staff_details: Mapped[Optional["StaffDetails"]] = relationship(
        "StaffDetails", back_populates="user", uselist=False
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
