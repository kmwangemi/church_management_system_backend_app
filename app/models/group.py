from typing import List, Optional

from sqlalchemy import Boolean, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base_class import Base, TimestampMixin, UUIDMixin


class Department(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "department"

    name: Mapped[str] = mapped_column(String(255))
    church_id: Mapped[str] = mapped_column(
        ForeignKey("church.id", ondelete="CASCADE")
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

    department_id: Mapped[str] = mapped_column(
        ForeignKey("department.id", ondelete="CASCADE")
    )
    user_id: Mapped[str] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"))
    # Relations
    department: Mapped["Department"] = relationship(
        "Department", back_populates="members"
    )
    # user relationship can be added to User model later if needed


class Group(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "group"

    name: Mapped[str] = mapped_column(String(255))
    church_id: Mapped[str] = mapped_column(
        ForeignKey("church.id", ondelete="CASCADE")
    )
    department_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("department.id", ondelete="SET NULL")
    )
    leader_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("user.id", ondelete="SET NULL")
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

    group_id: Mapped[str] = mapped_column(ForeignKey("group.id", ondelete="CASCADE"))
    user_id: Mapped[str] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"))
    # Relations
    group: Mapped["Group"] = relationship("Group", back_populates="members")
