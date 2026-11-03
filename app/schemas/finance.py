from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.enums import OfferingType, PaymentMethod
from app.schemas.common import Money


class FundBase(BaseModel):
    name: str
    is_active: bool = True


class FundCreate(FundBase):
    church_id: str


class FundResponse(FundBase):
    id: str
    church_id: str

    model_config = ConfigDict(from_attributes=True)


class OfferingBase(BaseModel):
    amount: Money = Field(gt=0)
    currency: str = Field(default="KES", min_length=3, max_length=3)
    type: OfferingType = OfferingType.OFFERING
    payment_method: PaymentMethod = PaymentMethod.CASH
    church_id: str
    branch_id: Optional[str] = None
    # Member who gave; leave empty for anonymous giving (was user_id).
    member_id: Optional[str] = None
    fund_id: str  # required: which fund the money is designated to
    pledge_id: Optional[str] = None  # counts toward this pledge, if any

    @field_validator("currency")
    @classmethod
    def _upper_currency(cls, v: str) -> str:
        return v.upper()


class OfferingCreate(OfferingBase):
    # Defaults to "now" on the server when omitted.
    received_at: Optional[datetime] = None


class OfferingResponse(OfferingBase):
    id: str
    received_at: datetime  # was: date
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)
