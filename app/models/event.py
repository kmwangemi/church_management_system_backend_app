from datetime import datetime
from typing import TYPE_CHECKING, List, Optional

from app.db.base_class import Base, TimestampMixin, UUIDMixin
from app.enums import AttendanceStatus, EventStatus
from sqlalchemy import DateTime, Enum, ForeignKey, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

if TYPE_CHECKING:
    from app.models.detail import VolunteerAssignment
    from app.models.member import Member


class Event(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "event"

    name: Mapped[str] = mapped_column(String(255))
    church_id: Mapped[str] = mapped_column(
        ForeignKey("church.id", ondelete="CASCADE"), index=True
    )
    # NULL = church-wide event.
    branch_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("branch.id", ondelete="CASCADE"), index=True
    )
    start_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end_time: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    location: Mapped[Optional[str]] = mapped_column(String(500))
    description: Mapped[Optional[str]] = mapped_column(String(1000))
    # iCal RRULE, e.g. "FREQ=WEEKLY;BYDAY=SU"
    recurrence_rule: Mapped[Optional[str]] = mapped_column(String(500))
    status: Mapped[EventStatus] = mapped_column(
        Enum(EventStatus), default=EventStatus.ACTIVE
    )
    # relationships
    activities: Mapped[List["Activity"]] = relationship(
        "Activity", back_populates="event"
    )
    attendances: Mapped[List["Attendance"]] = relationship(
        "Attendance", back_populates="event"
    )
    volunteer_assignments: Mapped[List["VolunteerAssignment"]] = relationship(
        "VolunteerAssignment", back_populates="event"
    )


class Activity(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "activity"

    event_id: Mapped[str] = mapped_column(
        ForeignKey("event.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(255))
    start_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end_time: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    # relationships
    event: Mapped["Event"] = relationship("Event", back_populates="activities")


class Attendance(Base, UUIDMixin, TimestampMixin):
    """Works for members, visitors and children alike (all are Members)."""

    __tablename__ = "attendance"
    __table_args__ = (
        UniqueConstraint("event_id", "member_id", name="uq_attendance_event_member"),
    )

    event_id: Mapped[str] = mapped_column(
        ForeignKey("event.id", ondelete="CASCADE"), index=True
    )
    member_id: Mapped[str] = mapped_column(
        ForeignKey("member.id", ondelete="CASCADE"), index=True
    )
    checked_in_by_member_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("member.id", ondelete="SET NULL")
    )
    checked_in_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    status: Mapped[AttendanceStatus] = mapped_column(
        Enum(AttendanceStatus), default=AttendanceStatus.PRESENT
    )
    # relationships
    event: Mapped["Event"] = relationship("Event", back_populates="attendances")
    member: Mapped["Member"] = relationship("Member", foreign_keys=[member_id])
    checked_in_by: Mapped[Optional["Member"]] = relationship(
        "Member", foreign_keys=[checked_in_by_member_id]
    )
