from datetime import datetime
from typing import Optional

from pydantic import BaseModel

from app.models.finance import OfferingType, PaymentMethod


class OfferingBase(BaseModel):
    amount: float
    type: OfferingType = OfferingType.OFFERING
    payment_method: PaymentMethod = PaymentMethod.CASH
    user_id: Optional[str] = None
    church_id: str


class OfferingCreate(OfferingBase):
    pass


class OfferingResponse(OfferingBase):
    id: str
    date: datetime
    created_at: datetime

    class Config:
        from_attributes = True
