from datetime import datetime, timezone
from typing import TYPE_CHECKING, List, Optional

from app.db.base_class import Base, TimestampMixin, UUIDMixin
from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    String,
    Table,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

if TYPE_CHECKING:
    from app.models.church import Church
    from app.models.member import Member
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
    __table_args__ = (
        UniqueConstraint("church_id", "name", name="uq_role_church_name"),
        # Postgres treats NULLs as distinct, so system roles need their own rule.
        Index(
            "uq_role_system_name",
            "name",
            unique=True,
            postgresql_where=text("church_id IS NULL"),
            sqlite_where=text("church_id IS NULL"),
        ),
    )

    name: Mapped[str] = mapped_column(String(255), index=True)
    description: Mapped[Optional[str]] = mapped_column(String(500))
    # NULL => system-default role shared by all churches. Otherwise church-specific.
    church_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("church.id", ondelete="CASCADE"), index=True
    )
    is_system: Mapped[bool] = mapped_column(Boolean, default=False)
    # relationships
    permissions: Mapped[List["Permission"]] = relationship(
        secondary=role_permission, back_populates="roles"
    )
    member_roles: Mapped[List["MemberRoleAssignment"]] = relationship(
        "MemberRoleAssignment", back_populates="role"
    )


class MemberRoleAssignment(Base, UUIDMixin, TimestampMixin):
    """
    Single source of truth for who holds which role (replaces UserRole and
    Member.roles). branch_id NULL = role applies church-wide.
    Class is named MemberRoleAssignment because app.enums already has an enum
    called MemberRole (LEADER / COORDINATOR / ...); the table is still member_role.
    """

    __tablename__ = "member_role"
    __table_args__ = (
        UniqueConstraint(
            "member_id", "role_id", "branch_id", name="uq_member_role_member_role_branch"
        ),
        Index(
            "uq_member_role_churchwide",
            "member_id",
            "role_id",
            unique=True,
            postgresql_where=text("branch_id IS NULL"),
            sqlite_where=text("branch_id IS NULL"),
        ),
    )

    member_id: Mapped[str] = mapped_column(
        ForeignKey("member.id", ondelete="CASCADE"), index=True
    )
    role_id: Mapped[str] = mapped_column(
        ForeignKey("role.id", ondelete="CASCADE"), index=True
    )
    branch_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("branch.id", ondelete="CASCADE"), index=True
    )
    assigned_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    # relationships
    member: Mapped["Member"] = relationship("Member", back_populates="member_roles")
    role: Mapped["Role"] = relationship("Role", back_populates="member_roles")


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

    church_id: Mapped[str] = mapped_column(
        ForeignKey("church.id", ondelete="CASCADE"), index=True
    )
    branch_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("branch.id", ondelete="CASCADE")
    )
    # Replaces the old free-text `role` string.
    role_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("role.id", ondelete="SET NULL")
    )
    # Nullable so a platform super-admin (who has no Member row) can invite.
    invited_by_member_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("member.id", ondelete="SET NULL")
    )
    email: Mapped[str] = mapped_column(String(255), index=True)
    token_hash: Mapped[Optional[str]] = mapped_column(
        String(255), unique=True, index=True
    )
    status: Mapped[str] = mapped_column(String(50))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    # relationships
    church: Mapped["Church"] = relationship("Church", back_populates="invitations")
    role: Mapped[Optional["Role"]] = relationship("Role")
    invited_by: Mapped[Optional["Member"]] = relationship("Member")
