from datetime import datetime
from decimal import Decimal
from typing import List

from app.db.base_class import Base, TimestampMixin, UUIDMixin
from app.enums import ChurchPlan, SubscriptionStatus
from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Numeric, String
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column


class ChurchSubscription(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "church_subscription"

    church_id: Mapped[str] = mapped_column(
        ForeignKey("church.id", ondelete="CASCADE"), unique=True
    )
    plan: Mapped[ChurchPlan] = mapped_column(Enum(ChurchPlan), default=ChurchPlan.BASIC)
    status: Mapped[SubscriptionStatus] = mapped_column(
        Enum(SubscriptionStatus), default=SubscriptionStatus.TRIAL
    )
    is_paid: Mapped[bool] = mapped_column(Boolean, default=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=False)
    invoice_amount: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), default=Decimal("0.00")
    )
    start_date: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end_date: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    features: Mapped[List[str]] = mapped_column(ARRAY(String), default=[])


class UserSubscription(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "user_subscription"

    user_id: Mapped[str] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"))
    plan: Mapped[str] = mapped_column(String(255))
    status: Mapped[SubscriptionStatus] = mapped_column(
        Enum(SubscriptionStatus), default=SubscriptionStatus.TRIAL
    )
    is_paid: Mapped[bool] = mapped_column(Boolean, default=False)
    start_date: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end_date: Mapped[datetime] = mapped_column(DateTime(timezone=True))
