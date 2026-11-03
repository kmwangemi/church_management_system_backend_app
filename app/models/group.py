from datetime import datetime, timezone
from typing import List, Optional

from app.db.base_class import Base, TimestampMixin, UUIDMixin
from app.enums import MemberRole
from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship


class Department(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "department"

    name: Mapped[str] = mapped_column(String(255))
    church_id: Mapped[str] = mapped_column(
        ForeignKey("church.id", ondelete="CASCADE"), index=True
    )
    leader_member_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("member.id", ondelete="SET NULL"), index=True
    )
    description: Mapped[Optional[str]] = mapped_column(String(1000))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # Relations
    members: Mapped[List["DepartmentMember"]] = relationship(
        "DepartmentMember", back_populates="department"
    )
    groups: Mapped[List["Group"]] = relationship("Group", back_populates="department")


class DepartmentMember(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "department_member"
    __table_args__ = (
        UniqueConstraint(
            "department_id", "member_id", name="uq_department_member_dept_member"
        ),
    )

    department_id: Mapped[str] = mapped_column(
        ForeignKey("department.id", ondelete="CASCADE"), index=True
    )
    member_id: Mapped[str] = mapped_column(
        ForeignKey("member.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[MemberRole] = mapped_column(
        Enum(MemberRole), default=MemberRole.MEMBER
    )
    # Relations
    department: Mapped["Department"] = relationship(
        "Department", back_populates="members"
    )


class Group(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "group"

    name: Mapped[str] = mapped_column(String(255))
    church_id: Mapped[str] = mapped_column(
        ForeignKey("church.id", ondelete="CASCADE"), index=True
    )
    branch_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("branch.id", ondelete="SET NULL"), index=True
    )
    # Optional: small/cell groups can exist without a department.
    department_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("department.id", ondelete="SET NULL")
    )
    leader_member_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("member.id", ondelete="SET NULL")
    )
    description: Mapped[Optional[str]] = mapped_column(String(1000))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    # Relations
    department: Mapped[Optional["Department"]] = relationship(
        "Department", back_populates="groups"
    )
    members: Mapped[List["GroupMember"]] = relationship(
        "GroupMember", back_populates="group"
    )


class GroupMember(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "group_member"
    __table_args__ = (
        UniqueConstraint("group_id", "member_id", name="uq_group_member_group_member"),
    )

    group_id: Mapped[str] = mapped_column(
        ForeignKey("group.id", ondelete="CASCADE"), index=True
    )
    member_id: Mapped[str] = mapped_column(
        ForeignKey("member.id", ondelete="CASCADE"), index=True
    )
    joined_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    # Relations
    group: Mapped["Group"] = relationship("Group", back_populates="members")
