from typing import TYPE_CHECKING, List, Optional

from app.db.base_class import Base, SoftDeleteMixin, TimestampMixin, UUIDMixin
from sqlalchemy import JSON, Boolean, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

if TYPE_CHECKING:
    from app.models.auth import Invitation
    from app.models.detail import Address, PastorAssignment
    from app.models.member import Member
    from app.models.subscription import ChurchSubscription


class Church(Base, UUIDMixin, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "church"

    name: Mapped[str] = mapped_column(String(255))
    slug: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    logo: Mapped[Optional[str]] = mapped_column(String(500))
    denomination: Mapped[Optional[str]] = mapped_column(String(255))
    description: Mapped[Optional[str]] = mapped_column(String(1000))
    email: Mapped[Optional[str]] = mapped_column(String(255))
    phone_number: Mapped[Optional[str]] = mapped_column(String(255))
    website: Mapped[Optional[str]] = mapped_column(String(255))
    address_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("address.id", ondelete="SET NULL")
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    is_suspended: Mapped[bool] = mapped_column(Boolean, default=False)
    metadata_json: Mapped[Optional[dict]] = mapped_column(JSON)

    # Relations
    branches: Mapped[List["Branch"]] = relationship("Branch", back_populates="church")
    members: Mapped[List["Member"]] = relationship("Member", back_populates="church")
    invitations: Mapped[List["Invitation"]] = relationship(
        "Invitation", back_populates="church"
    )
    address: Mapped[Optional["Address"]] = relationship("Address")
    subscription: Mapped[Optional["ChurchSubscription"]] = relationship(
        "ChurchSubscription", back_populates="church", uselist=False
    )


class Branch(Base, UUIDMixin, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "branch"

    name: Mapped[str] = mapped_column(String(255))
    church_id: Mapped[str] = mapped_column(
        ForeignKey("church.id", ondelete="CASCADE"), index=True
    )
    address_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("address.id", ondelete="SET NULL")
    )
    email: Mapped[Optional[str]] = mapped_column(String(255))
    phone_number: Mapped[Optional[str]] = mapped_column(String(255))
    capacity: Mapped[Optional[int]] = mapped_column(Integer)
    # NOTE: member_count removed. Compute it:
    #   select count(*) from member where branch_id = :id and deleted_at is null
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    metadata_json: Mapped[Optional[dict]] = mapped_column(JSON)

    # Relations
    church: Mapped["Church"] = relationship("Church", back_populates="branches")
    members: Mapped[List["Member"]] = relationship("Member", back_populates="branch")
    address: Mapped[Optional["Address"]] = relationship("Address")
    pastor_assignments: Mapped[List["PastorAssignment"]] = relationship(
        "PastorAssignment", back_populates="branch"
    )
