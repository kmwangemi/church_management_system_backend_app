from pydantic import BaseModel, EmailStr
from typing import Optional
from datetime import datetime
from app.models.user import GlobalRole, AccountStatus

class Token(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str
    user: Optional[dict] = None

class LoginRequest(BaseModel):
    email: EmailStr
    password: str

class UserBase(BaseModel):
    email: EmailStr
    name: str
    phone_number: Optional[str] = None

class UserCreate(UserBase):
    password: str

class UserResponse(UserBase):
    id: str
    global_role: GlobalRole
    status: AccountStatus
    created_at: datetime
    
    class Config:
        from_attributes = True
