from datetime import datetime
from typing import TYPE_CHECKING, List, Optional

from app.db.base_class import Base, TimestampMixin, UUIDMixin
from sqlalchemy import Column, DateTime, ForeignKey, String, Table
from sqlalchemy.orm import Mapped, mapped_column, relationship

if TYPE_CHECKING:
    from app.models.church import Church
    from app.models.user import User

# Association table for Many-to-Many between Role and Permission
role_permission = Table(
    "role_permission",
    Base.metadata,
    Column("role_id", ForeignKey("role.id", ondelete="CASCADE"), primary_key=True),
    Column(
        "permission_id",
        ForeignKey("permission.id", ondelete="CASCADE"),
        primary_key=True,
    ),
)


class Permission(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "permission"

    name: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    description: Mapped[Optional[str]] = mapped_column(String(500))
    # relationships
    roles: Mapped[List["Role"]] = relationship(
        secondary=role_permission, back_populates="permissions"
    )


class Role(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "role"

    name: Mapped[str] = mapped_column(String(255), index=True)
    description: Mapped[Optional[str]] = mapped_column(String(500))
    # If null, it's a global role (e.g., SuperAdmin). Otherwise, org-specific.
    church_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("church.id", ondelete="CASCADE")
    )
    # relationships
    permissions: Mapped[List["Permission"]] = relationship(
        secondary=role_permission, back_populates="roles"
    )
    user_roles: Mapped[List["UserRole"]] = relationship(
        "UserRole", back_populates="role"
    )


class UserRole(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "user_role"

    user_id: Mapped[str] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"))
    role_id: Mapped[str] = mapped_column(ForeignKey("role.id", ondelete="CASCADE"))
    # Scope this role to an church if necessary
    church_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("church.id", ondelete="CASCADE")
    )
    # relationships
    user: Mapped["User"] = relationship("User", back_populates="roles")
    role: Mapped["Role"] = relationship("Role", back_populates="user_roles")


class Account(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "account"

    account_id: Mapped[str] = mapped_column(String(255))
    provider_id: Mapped[str] = mapped_column(String(255))
    user_id: Mapped[str] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"))
    access_token: Mapped[Optional[str]] = mapped_column(String(2000))
    refresh_token: Mapped[Optional[str]] = mapped_column(String(2000))
    id_token: Mapped[Optional[str]] = mapped_column(String(2000))
    access_token_expires_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True)
    )
    refresh_token_expires_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True)
    )
    scope: Mapped[Optional[str]] = mapped_column(String(500))
    password: Mapped[Optional[str]] = mapped_column(String(255))
    # relationships
    user: Mapped["User"] = relationship("User", back_populates="accounts")


class Verification(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "verification"

    identifier: Mapped[str] = mapped_column(String(255), index=True)
    value: Mapped[str] = mapped_column(String(255))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Invitation(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "invitation"

    church_id: Mapped[str] = mapped_column(ForeignKey("church.id", ondelete="CASCADE"))
    email: Mapped[str] = mapped_column(String(255), index=True)
    role: Mapped[Optional[str]] = mapped_column(String(50))
    status: Mapped[str] = mapped_column(String(50))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    inviter_id: Mapped[str] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"))
    branch_id: Mapped[Optional[str]] = mapped_column(String(255))  # References Branch
    # relationships
    church: Mapped["Church"] = relationship("Church", back_populates="invitations")
    inviter: Mapped["User"] = relationship("User", back_populates="invitations_sent")
