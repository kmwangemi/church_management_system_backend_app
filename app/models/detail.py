from datetime import datetime, time
from typing import TYPE_CHECKING, List, Optional

from app.db.base_class import Base, TimestampMixin, UUIDMixin
from app.enums import AssignmentStatus, DayOfWeek, MaritalStatus
from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    Time,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

if TYPE_CHECKING:
    from app.models.church import Branch
    from app.models.event import Event
    from app.models.member import Member


class Address(Base, UUIDMixin, TimestampMixin):
    """Plain address. Owners (Church, Branch, Member) hold an `address_id` FK."""

    __tablename__ = "address"

    street: Mapped[Optional[str]] = mapped_column(String(255))
    city: Mapped[Optional[str]] = mapped_column(String(255))
    state: Mapped[Optional[str]] = mapped_column(String(255))
    zip_code: Mapped[Optional[str]] = mapped_column(String(255))
    country: Mapped[str] = mapped_column(String(255), default="Kenya")


# --------------------------------------------------------------------------- #
# Member-scoped profile tables (were User-scoped)
# --------------------------------------------------------------------------- #
class MemberDetails(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "member_details"

    member_id: Mapped[str] = mapped_column(
        ForeignKey("member.id", ondelete="CASCADE"), unique=True
    )
    membership_date: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True)
    )
    baptism_date: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    marital_status: Mapped[Optional[MaritalStatus]] = mapped_column(
        Enum(MaritalStatus)
    )
    occupation: Mapped[Optional[str]] = mapped_column(String(255))
    member: Mapped["Member"] = relationship("Member", back_populates="member_details")


class EmergencyContact(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "emergency_contact"

    full_name: Mapped[str] = mapped_column(String(255))
    email: Mapped[Optional[str]] = mapped_column(String(255))
    phone_number: Mapped[str] = mapped_column(String(255))
    relationship_type: Mapped[str] = mapped_column(String(255))
    address: Mapped[Optional[str]] = mapped_column(String(255))
    notes: Mapped[Optional[str]] = mapped_column(String(1000))
    # One-to-many now (no longer unique): a member can have several contacts.
    member_id: Mapped[str] = mapped_column(
        ForeignKey("member.id", ondelete="CASCADE"), index=True
    )
    member: Mapped["Member"] = relationship(
        "Member", back_populates="emergency_contacts"
    )


class VisitorDetails(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "visitor_details"

    member_id: Mapped[str] = mapped_column(
        ForeignKey("member.id", ondelete="CASCADE"), unique=True
    )
    visit_date: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    how_did_you_hear: Mapped[str] = mapped_column(String(50), default="OTHER")
    follow_up_status: Mapped[str] = mapped_column(String(50), default="PENDING")
    follow_up_date: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True)
    )
    follow_up_notes: Mapped[Optional[str]] = mapped_column(String(1000))
    interested_in_membership: Mapped[bool] = mapped_column(Boolean, default=False)
    # Was a free-text string; now links to the inviting member.
    invited_by_member_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("member.id", ondelete="SET NULL")
    )
    # Set when the visitor becomes a regular member.
    converted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    member: Mapped["Member"] = relationship(
        "Member", foreign_keys=[member_id], back_populates="visitor_details"
    )
    invited_by: Mapped[Optional["Member"]] = relationship(
        "Member", foreign_keys=[invited_by_member_id]
    )


class BishopDetails(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "bishop_details"

    member_id: Mapped[str] = mapped_column(
        ForeignKey("member.id", ondelete="CASCADE"), unique=True
    )
    appointment_date: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True)
    )
    jurisdiction_area: Mapped[Optional[str]] = mapped_column(String(255))
    biography: Mapped[Optional[str]] = mapped_column(String(1000))
    member: Mapped["Member"] = relationship("Member", back_populates="bishop_details")


class PastorDetails(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "pastor_details"

    member_id: Mapped[str] = mapped_column(
        ForeignKey("member.id", ondelete="CASCADE"), unique=True
    )
    ordination_date: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True)
    )
    sermon_count: Mapped[int] = mapped_column(Integer, default=0)
    biography: Mapped[Optional[str]] = mapped_column(String(1000))
    assignments: Mapped[List["PastorAssignment"]] = relationship(
        "PastorAssignment", back_populates="pastor_details"
    )
    member: Mapped["Member"] = relationship("Member", back_populates="pastor_details")


class PastorAssignment(Base, UUIDMixin, TimestampMixin):
    """Assigns a pastor to a branch for a period (position = assignment type)."""

    __tablename__ = "pastor_assignment"

    position: Mapped[str] = mapped_column(String(255))
    start_date: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end_date: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    branch_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("branch.id", ondelete="CASCADE"), index=True
    )
    pastor_details_id: Mapped[str] = mapped_column(
        ForeignKey("pastor_details.id", ondelete="CASCADE"), index=True
    )
    # relationships
    pastor_details: Mapped["PastorDetails"] = relationship(
        "PastorDetails", back_populates="assignments"
    )
    branch: Mapped[Optional["Branch"]] = relationship(
        "Branch", back_populates="pastor_assignments"
    )


class StaffDetails(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "staff_details"

    member_id: Mapped[str] = mapped_column(
        ForeignKey("member.id", ondelete="CASCADE"), unique=True
    )
    job_title: Mapped[str] = mapped_column(String(255))
    department: Mapped[str] = mapped_column(String(255))
    start_date: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end_date: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    member: Mapped["Member"] = relationship("Member", back_populates="staff_details")


class AdminDetails(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "admin_details"

    member_id: Mapped[str] = mapped_column(
        ForeignKey("member.id", ondelete="CASCADE"), unique=True
    )
    access_level: Mapped[str] = mapped_column(String(50), default="NATIONAL")
    member: Mapped["Member"] = relationship("Member", back_populates="admin_details")


# --------------------------------------------------------------------------- #
# Volunteers
# --------------------------------------------------------------------------- #
class VolunteerDetails(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "volunteer_details"

    member_id: Mapped[str] = mapped_column(
        ForeignKey("member.id", ondelete="CASCADE"), unique=True
    )
    hours_contributed: Mapped[int] = mapped_column(Integer, default=0)
    assignments: Mapped[List["VolunteerAssignment"]] = relationship(
        "VolunteerAssignment", back_populates="volunteer_details"
    )
    availability_schedule: Mapped[List["AvailabilitySchedule"]] = relationship(
        "AvailabilitySchedule", back_populates="volunteer_details"
    )
    background_checks: Mapped[List["BackgroundCheck"]] = relationship(
        "BackgroundCheck", back_populates="volunteer_details"
    )
    member: Mapped["Member"] = relationship(
        "Member", back_populates="volunteer_details"
    )


class VolunteerRole(Base, UUIDMixin, TimestampMixin):
    """Lookup of volunteer roles a church offers (usher, choir, ...)."""

    __tablename__ = "volunteer_role"

    church_id: Mapped[str] = mapped_column(
        ForeignKey("church.id", ondelete="CASCADE"), index=True
    )
    role: Mapped[str] = mapped_column(String(255))
    department: Mapped[Optional[str]] = mapped_column(String(255))
    description: Mapped[Optional[str]] = mapped_column(String(1000))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    assignments: Mapped[List["VolunteerAssignment"]] = relationship(
        "VolunteerAssignment", back_populates="volunteer_role"
    )


class VolunteerAssignment(Base, UUIDMixin, TimestampMixin):
    """A volunteer filling a role, optionally for a specific event."""

    __tablename__ = "volunteer_assignment"

    volunteer_details_id: Mapped[str] = mapped_column(
        ForeignKey("volunteer_details.id", ondelete="CASCADE"), index=True
    )
    volunteer_role_id: Mapped[str] = mapped_column(
        ForeignKey("volunteer_role.id", ondelete="CASCADE"), index=True
    )
    event_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("event.id", ondelete="CASCADE"), index=True
    )
    status: Mapped[AssignmentStatus] = mapped_column(
        Enum(AssignmentStatus), default=AssignmentStatus.PENDING
    )
    start_date: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    end_date: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    volunteer_details: Mapped["VolunteerDetails"] = relationship(
        "VolunteerDetails", back_populates="assignments"
    )
    volunteer_role: Mapped["VolunteerRole"] = relationship(
        "VolunteerRole", back_populates="assignments"
    )
    event: Mapped[Optional["Event"]] = relationship(
        "Event", back_populates="volunteer_assignments"
    )


class AvailabilitySchedule(Base, UUIDMixin, TimestampMixin):
    """One row per available time slot."""

    __tablename__ = "availability_schedule"

    volunteer_details_id: Mapped[str] = mapped_column(
        ForeignKey("volunteer_details.id", ondelete="CASCADE"), index=True
    )
    day_of_week: Mapped[DayOfWeek] = mapped_column(Enum(DayOfWeek))
    start_time: Mapped[time] = mapped_column(Time)
    end_time: Mapped[time] = mapped_column(Time)
    volunteer_details: Mapped["VolunteerDetails"] = relationship(
        "VolunteerDetails", back_populates="availability_schedule"
    )


class BackgroundCheck(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "background_check"

    completed: Mapped[bool] = mapped_column(Boolean, default=False)
    completed_date: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True)
    )
    expiry_date: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    clearance_level: Mapped[Optional[str]] = mapped_column(String(50))
    # One-to-many now (no longer unique) so renewals keep history.
    volunteer_details_id: Mapped[str] = mapped_column(
        ForeignKey("volunteer_details.id", ondelete="CASCADE"), index=True
    )
    volunteer_details: Mapped["VolunteerDetails"] = relationship(
        "VolunteerDetails", back_populates="background_checks"
    )
