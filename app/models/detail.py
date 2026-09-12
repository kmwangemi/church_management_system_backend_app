from datetime import datetime
from typing import TYPE_CHECKING, List, Optional

from app.db.base_class import Base, TimestampMixin, UUIDMixin
from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

if TYPE_CHECKING:
    from app.models.church import Branch, Church
    from app.models.user import User


class MemberDetails(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "member_details"

    user_id: Mapped[str] = mapped_column(
        ForeignKey("user.id", ondelete="CASCADE"), unique=True
    )
    membership_date: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True)
    )
    baptism_date: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    user: Mapped["User"] = relationship("User", back_populates="member_details")


class PastorDetails(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "pastor_details"

    user_id: Mapped[str] = mapped_column(
        ForeignKey("user.id", ondelete="CASCADE"), unique=True
    )
    ordination_date: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True)
    )
    sermon_count: Mapped[int] = mapped_column(Integer, default=0)
    biography: Mapped[Optional[str]] = mapped_column(String(1000))
    assignments: Mapped[List["PastorAssignment"]] = relationship(
        "PastorAssignment", back_populates="pastor_details"
    )
    user: Mapped["User"] = relationship("User", back_populates="pastor_details")


class VolunteerDetails(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "volunteer_details"

    user_id: Mapped[str] = mapped_column(
        ForeignKey("user.id", ondelete="CASCADE"), unique=True
    )
    hours_contributed: Mapped[int] = mapped_column(Integer, default=0)
    roles: Mapped[List["VolunteerRole"]] = relationship(
        "VolunteerRole", back_populates="volunteer_details"
    )
    availability_schedule: Mapped[Optional["AvailabilitySchedule"]] = relationship(
        "AvailabilitySchedule", back_populates="volunteer_details", uselist=False
    )
    background_check: Mapped[Optional["BackgroundCheck"]] = relationship(
        "BackgroundCheck", back_populates="volunteer_details", uselist=False
    )
    user: Mapped["User"] = relationship("User", back_populates="volunteer_details")


class StaffDetails(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "staff_details"

    user_id: Mapped[str] = mapped_column(
        ForeignKey("user.id", ondelete="CASCADE"), unique=True
    )
    job_title: Mapped[str] = mapped_column(String(255))
    department: Mapped[str] = mapped_column(String(255))
    start_date: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end_date: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    user: Mapped["User"] = relationship("User", back_populates="staff_details")


class Address(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "address"

    street: Mapped[Optional[str]] = mapped_column(String(255))
    city: Mapped[Optional[str]] = mapped_column(String(255))
    state: Mapped[Optional[str]] = mapped_column(String(255))
    zip_code: Mapped[Optional[str]] = mapped_column(String(255))
    country: Mapped[str] = mapped_column(String(255), default="Kenya")
    user_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("user.id", ondelete="CASCADE"), unique=True
    )
    church_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("church.id", ondelete="CASCADE"), unique=True
    )
    branch_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("branch.id", ondelete="CASCADE"), unique=True
    )
    # relationships
    user: Mapped[Optional["User"]] = relationship("User", back_populates="address")
    church: Mapped[Optional["Church"]] = relationship(
        "Church", back_populates="address"
    )
    branch: Mapped[Optional["Branch"]] = relationship(
        "Branch", back_populates="address"
    )


class EmergencyContact(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "emergency_contact"

    full_name: Mapped[str] = mapped_column(String(255))
    email: Mapped[Optional[str]] = mapped_column(String(255))
    phone_number: Mapped[str] = mapped_column(String(255))
    relationship_type: Mapped[str] = mapped_column(String(255))
    address: Mapped[Optional[str]] = mapped_column(String(255))
    notes: Mapped[Optional[str]] = mapped_column(String(1000))
    user_id: Mapped[str] = mapped_column(
        ForeignKey("user.id", ondelete="CASCADE"), unique=True
    )
    # relationships
    user: Mapped["User"] = relationship("User", back_populates="emergency_contact")


class PastorAssignment(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "pastor_assignment"

    position: Mapped[str] = mapped_column(String(255))
    start_date: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end_date: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    branch_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("branch.id", ondelete="CASCADE")
    )
    pastor_details_id: Mapped[str] = mapped_column(
        ForeignKey("pastor_details.id", ondelete="CASCADE")
    )
    # relationships
    pastor_details: Mapped["PastorDetails"] = relationship(
        "PastorDetails", back_populates="assignments"
    )


class BishopDetails(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "bishop_details"

    user_id: Mapped[str] = mapped_column(
        ForeignKey("user.id", ondelete="CASCADE"), unique=True
    )
    appointment_date: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True)
    )
    jurisdiction_area: Mapped[Optional[str]] = mapped_column(String(255))
    biography: Mapped[Optional[str]] = mapped_column(String(1000))
    # relationships
    user: Mapped["User"] = relationship("User", back_populates="bishop_details")


class AvailabilitySchedule(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "availability_schedule"

    volunteer_details_id: Mapped[str] = mapped_column(
        ForeignKey("volunteer_details.id", ondelete="CASCADE"), unique=True
    )
    # relationships
    volunteer_details: Mapped["VolunteerDetails"] = relationship(
        "VolunteerDetails", back_populates="availability_schedule"
    )


class VolunteerRole(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "volunteer_role"

    role: Mapped[str] = mapped_column(String(255))
    department: Mapped[Optional[str]] = mapped_column(String(255))
    start_date: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end_date: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    volunteer_details_id: Mapped[str] = mapped_column(
        ForeignKey("volunteer_details.id", ondelete="CASCADE")
    )
    # relationships
    volunteer_details: Mapped["VolunteerDetails"] = relationship(
        "VolunteerDetails", back_populates="roles"
    )


class BackgroundCheck(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "background_check"

    completed: Mapped[bool] = mapped_column(Boolean, default=False)
    completed_date: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True)
    )
    expiry_date: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True)
    )
    clearance_level: Mapped[Optional[str]] = mapped_column(String(50))
    volunteer_details_id: Mapped[str] = mapped_column(
        ForeignKey("volunteer_details.id", ondelete="CASCADE"), unique=True
    )
    # relationships
    volunteer_details: Mapped["VolunteerDetails"] = relationship(
        "VolunteerDetails", back_populates="background_check"
    )


class AdminDetails(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "admin_details"

    user_id: Mapped[str] = mapped_column(
        ForeignKey("user.id", ondelete="CASCADE"), unique=True
    )
    access_level: Mapped[str] = mapped_column(String(50), default="NATIONAL")
    # relationships
    user: Mapped["User"] = relationship("User", back_populates="admin_details")


class VisitorDetails(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "visitor_details"

    user_id: Mapped[str] = mapped_column(
        ForeignKey("user.id", ondelete="CASCADE"), unique=True
    )
    visit_date: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    how_did_you_hear: Mapped[str] = mapped_column(String(50), default="OTHER")
    follow_up_status: Mapped[str] = mapped_column(String(50), default="PENDING")
    follow_up_date: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True)
    )
    follow_up_notes: Mapped[Optional[str]] = mapped_column(String(1000))
    interested_in_membership: Mapped[bool] = mapped_column(Boolean, default=False)
    invited_by: Mapped[Optional[str]] = mapped_column(String(255))
    # relationships
    user: Mapped["User"] = relationship("User", back_populates="visitor_details")
