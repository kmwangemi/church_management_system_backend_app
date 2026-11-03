from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, model_validator

from app.enums import EventStatus


class EventBase(BaseModel):
    name: str
    church_id: str
    branch_id: Optional[str] = None  # None = church-wide event
    start_time: datetime
    end_time: Optional[datetime] = None
    location: Optional[str] = None
    description: Optional[str] = None
    recurrence_rule: Optional[str] = None  # iCal RRULE, e.g. "FREQ=WEEKLY;BYDAY=SU"
    status: EventStatus = EventStatus.ACTIVE

    @model_validator(mode="after")
    def _end_after_start(self):
        if self.end_time and self.end_time < self.start_time:
            raise ValueError("end_time must not be before start_time")
        return self


class EventCreate(EventBase):
    pass


class EventResponse(EventBase):
    id: str
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class EventUpdate(BaseModel):
    """Partial update. Send status=CANCELLED to cancel an event that already has attendance."""

    name: Optional[str] = None
    branch_id: Optional[str] = None
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None
    location: Optional[str] = None
    description: Optional[str] = None
    recurrence_rule: Optional[str] = None
    status: Optional[EventStatus] = None
