from typing import TYPE_CHECKING, List, Optional

from app.db.base_class import Base, TimestampMixin, UUIDMixin
from sqlalchemy import JSON, Boolean, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

if TYPE_CHECKING:
    from app.models.auth import Invitation
    from app.models.detail import (
        Address,
    )
    from app.models.member import BranchMember, Member


class Church(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "church"

    name: Mapped[str] = mapped_column(String(255))
    slug: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    logo: Mapped[Optional[str]] = mapped_column(String(500))
    denomination: Mapped[Optional[str]] = mapped_column(String(255))
    description: Mapped[Optional[str]] = mapped_column(String(1000))
    email: Mapped[Optional[str]] = mapped_column(String(255))
    phone_number: Mapped[Optional[str]] = mapped_column(String(255))
    website: Mapped[Optional[str]] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_suspended: Mapped[bool] = mapped_column(Boolean, default=False)
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    metadata_json: Mapped[Optional[dict]] = mapped_column(JSON)

    # Relations
    branches: Mapped[List["Branch"]] = relationship("Branch", back_populates="church")
    members: Mapped[List["Member"]] = relationship("Member", back_populates="church")
    invitations: Mapped[List["Invitation"]] = relationship(
        "Invitation", back_populates="church"
    )
    address: Mapped[Optional["Address"]] = relationship(
        "Address", back_populates="church", uselist=False
    )


class Branch(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "branch"

    name: Mapped[str] = mapped_column(String(255))
    church_id: Mapped[str] = mapped_column(ForeignKey("church.id", ondelete="CASCADE"))
    email: Mapped[Optional[str]] = mapped_column(String(255))
    phone_number: Mapped[Optional[str]] = mapped_column(String(255))
    capacity: Mapped[Optional[int]] = mapped_column(Integer)
    member_count: Mapped[int] = mapped_column(Integer, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_deleted: Mapped[bool] = mapped_column(Boolean, default=False)
    metadata_json: Mapped[Optional[dict]] = mapped_column(JSON)

    # Relations
    church: Mapped["Church"] = relationship("Church", back_populates="branches")
    branch_members: Mapped[List["BranchMember"]] = relationship(
        "BranchMember", back_populates="branch"
    )
    members: Mapped[List["Member"]] = relationship("Member", back_populates="branch")
    address: Mapped[Optional["Address"]] = relationship(
        "Address", back_populates="branch", uselist=False
    )
