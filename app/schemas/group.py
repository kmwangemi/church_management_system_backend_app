from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class GroupBase(BaseModel):
    name: str
    church_id: str
    department_id: Optional[str] = None
    leader_id: Optional[str] = None
    description: Optional[str] = None
    is_active: bool = True


class GroupCreate(GroupBase):
    pass


class GroupResponse(GroupBase):
    id: str
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True
