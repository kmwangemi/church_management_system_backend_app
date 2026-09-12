from datetime import datetime
from typing import Optional

from pydantic import BaseModel

from app.models.event import EventStatus


class EventBase(BaseModel):
    name: str
    church_id: str
    start_time: datetime
    end_time: Optional[datetime] = None
    location: Optional[str] = None
    description: Optional[str] = None
    status: EventStatus = EventStatus.ACTIVE


class EventCreate(EventBase):
    pass


class EventResponse(EventBase):
    id: str
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True
