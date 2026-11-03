from datetime import date
from typing import TYPE_CHECKING, List, Optional

from app.db.base_class import Base, SoftDeleteMixin, TimestampMixin, UUIDMixin
from app.enums import Gender, HouseholdRole, MembershipStatus, MemberType
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    Enum,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

if TYPE_CHECKING:
    from app.models.auth import MemberRoleAssignment
    from app.models.church import Branch, Church
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
    from app.models.user import User


class Household(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "household"

    church_id: Mapped[str] = mapped_column(
        ForeignKey("church.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(255))

    members: Mapped[List["Member"]] = relationship(
        "Member", back_populates="household"
    )


class Member(Base, UUIDMixin, TimestampMixin, SoftDeleteMixin):
    """
    A person within ONE church and ONE branch.
    `user_id` is nullable: visitors and children don't need a login.
    A User who belongs to several churches has one Member row per church.
    """

    __tablename__ = "member"
    __table_args__ = (
        UniqueConstraint(
            "church_id", "membership_number", name="uq_member_church_membership_number"
        ),
        UniqueConstraint("church_id", "user_id", name="uq_member_church_user"),
        Index("ix_member_church_branch", "church_id", "branch_id"),
    )

    church_id: Mapped[str] = mapped_column(
        ForeignKey("church.id", ondelete="CASCADE"), index=True
    )
    # Exactly one branch per member (required).
    branch_id: Mapped[str] = mapped_column(ForeignKey("branch.id"), index=True)
    # NULL for visitors/children without a login.
    user_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("user.id", ondelete="SET NULL"), index=True
    )
    household_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("household.id", ondelete="SET NULL"), index=True
    )
    address_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("address.id", ondelete="SET NULL")
    )

    member_type: Mapped[MemberType] = mapped_column(
        Enum(MemberType), default=MemberType.MEMBER, index=True
    )
    first_name: Mapped[str] = mapped_column(String(255))
    last_name: Mapped[str] = mapped_column(String(255))
    date_of_birth: Mapped[Optional[date]] = mapped_column(Date)
    gender: Mapped[Optional[Gender]] = mapped_column(Enum(Gender))
    phone_number: Mapped[Optional[str]] = mapped_column(String(255))
    email: Mapped[Optional[str]] = mapped_column(String(255))  # not unique
    photo_url: Mapped[Optional[str]] = mapped_column(String(500))

    membership_number: Mapped[Optional[str]] = mapped_column(String(50), index=True)
    position: Mapped[Optional[str]] = mapped_column(String(255), default="member")
    household_role: Mapped[Optional[HouseholdRole]] = mapped_column(
        Enum(HouseholdRole)
    )
    status: Mapped[MembershipStatus] = mapped_column(
        Enum(MembershipStatus), default=MembershipStatus.ACTIVE
    )
    joined_at: Mapped[Optional[date]] = mapped_column(Date)

    # Relations
    church: Mapped["Church"] = relationship("Church", back_populates="members")
    branch: Mapped["Branch"] = relationship("Branch", back_populates="members")
    user: Mapped[Optional["User"]] = relationship("User", back_populates="memberships")
    household: Mapped[Optional["Household"]] = relationship(
        "Household", back_populates="members"
    )
    address: Mapped[Optional["Address"]] = relationship("Address")
    member_roles: Mapped[List["MemberRoleAssignment"]] = relationship(
        "MemberRoleAssignment", back_populates="member"
    )

    # Guardians (this member is the child) / dependents (this member is the guardian)
    guardian_links: Mapped[List["Guardian"]] = relationship(
        "Guardian",
        foreign_keys="Guardian.child_member_id",
        back_populates="child",
    )
    dependent_links: Mapped[List["Guardian"]] = relationship(
        "Guardian",
        foreign_keys="Guardian.guardian_member_id",
        back_populates="guardian",
    )

    # Role-specific profiles (church-scoped)
    emergency_contacts: Mapped[List["EmergencyContact"]] = relationship(
        "EmergencyContact", back_populates="member"
    )
    member_details: Mapped[Optional["MemberDetails"]] = relationship(
        "MemberDetails", back_populates="member", uselist=False
    )
    visitor_details: Mapped[Optional["VisitorDetails"]] = relationship(
        "VisitorDetails",
        foreign_keys="VisitorDetails.member_id",
        back_populates="member",
        uselist=False,
    )
    bishop_details: Mapped[Optional["BishopDetails"]] = relationship(
        "BishopDetails", back_populates="member", uselist=False
    )
    pastor_details: Mapped[Optional["PastorDetails"]] = relationship(
        "PastorDetails", back_populates="member", uselist=False
    )
    staff_details: Mapped[Optional["StaffDetails"]] = relationship(
        "StaffDetails", back_populates="member", uselist=False
    )
    admin_details: Mapped[Optional["AdminDetails"]] = relationship(
        "AdminDetails", back_populates="member", uselist=False
    )
    volunteer_details: Mapped[Optional["VolunteerDetails"]] = relationship(
        "VolunteerDetails", back_populates="member", uselist=False
    )

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()


class Guardian(Base, UUIDMixin, TimestampMixin):
    """Links a child Member to a parent/guardian Member (supports 2+ guardians)."""

    __tablename__ = "guardian"
    __table_args__ = (
        UniqueConstraint(
            "child_member_id", "guardian_member_id", name="uq_guardian_child_guardian"
        ),
        CheckConstraint(
            "child_member_id <> guardian_member_id", name="ck_guardian_not_self"
        ),
    )

    child_member_id: Mapped[str] = mapped_column(
        ForeignKey("member.id", ondelete="CASCADE"), index=True
    )
    guardian_member_id: Mapped[str] = mapped_column(
        ForeignKey("member.id", ondelete="CASCADE"), index=True
    )
    relationship_type: Mapped[str] = mapped_column(String(100))  # mother, father...
    is_primary: Mapped[bool] = mapped_column(Boolean, default=False)
    can_pickup: Mapped[bool] = mapped_column(Boolean, default=True)

    child: Mapped["Member"] = relationship(
        "Member", foreign_keys=[child_member_id], back_populates="guardian_links"
    )
    guardian: Mapped["Member"] = relationship(
        "Member", foreign_keys=[guardian_member_id], back_populates="dependent_links"
    )
