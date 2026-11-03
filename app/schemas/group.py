from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict


class GroupBase(BaseModel):
    name: str
    church_id: str
    branch_id: Optional[str] = None
    department_id: Optional[str] = None  # small/cell groups can exist without one
    leader_member_id: Optional[str] = None  # was leader_id (a user id); now a member id
    description: Optional[str] = None
    is_active: bool = True


class GroupCreate(GroupBase):
    pass


class GroupResponse(GroupBase):
    id: str
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)
