from datetime import datetime
from typing import List, Optional

from sqlalchemy import DateTime, Enum, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base_class import Base, TimestampMixin, UUIDMixin

from app.enums import AttendanceStatus, EventStatus

class Event(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "event"

    name: Mapped[str] = mapped_column(String(255))
    church_id: Mapped[str] = mapped_column(ForeignKey("church.id", ondelete="CASCADE"))
    start_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end_time: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    location: Mapped[Optional[str]] = mapped_column(String(500))
    description: Mapped[Optional[str]] = mapped_column(String(1000))
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


class Activity(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "activity"

    event_id: Mapped[str] = mapped_column(ForeignKey("event.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(255))
    start_time: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end_time: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    # relationships
    event: Mapped["Event"] = relationship("Event", back_populates="activities")


class Attendance(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "attendance"

    event_id: Mapped[str] = mapped_column(ForeignKey("event.id", ondelete="CASCADE"))
    user_id: Mapped[str] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"))
    status: Mapped[AttendanceStatus] = mapped_column(
        Enum(AttendanceStatus), default=AttendanceStatus.PRESENT
    )
    # relationships
    event: Mapped["Event"] = relationship("Event", back_populates="attendances")
