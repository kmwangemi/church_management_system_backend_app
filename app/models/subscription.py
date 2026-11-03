from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, List, Optional

from app.db.base_class import Base, TimestampMixin, UUIDMixin
from app.enums import BillingInterval, SubscriptionStatus
from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Numeric, String
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column, relationship

if TYPE_CHECKING:
    from app.models.church import Church
    from app.models.user import User


class Plan(Base, UUIDMixin, TimestampMixin):
    """SaaS plan catalogue. Seed from the old ChurchPlan enum values."""

    __tablename__ = "plan"

    name: Mapped[str] = mapped_column(String(255), unique=True)
    description: Mapped[Optional[str]] = mapped_column(String(1000))
    price: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0.00"))
    billing_interval: Mapped[BillingInterval] = mapped_column(
        Enum(BillingInterval), default=BillingInterval.MONTHLY
    )
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    subscriptions: Mapped[List["ChurchSubscription"]] = relationship(
        "ChurchSubscription", back_populates="plan"
    )


class ChurchSubscription(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "church_subscription"

    church_id: Mapped[str] = mapped_column(
        ForeignKey("church.id", ondelete="CASCADE"), unique=True
    )
    plan_id: Mapped[str] = mapped_column(ForeignKey("plan.id"), index=True)
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
    features: Mapped[List[str]] = mapped_column(ARRAY(String), default=list)

    church: Mapped["Church"] = relationship("Church", back_populates="subscription")
    plan: Mapped["Plan"] = relationship("Plan", back_populates="subscriptions")



class UserSubscription(Base, UUIDMixin, TimestampMixin):
    """
    Per-user SaaS billing for dashboard access. Tied to the login identity (User),
    not to a church-scoped Member. Unchanged from the original model apart from
    the index on user_id and the relationship below.
    """

    __tablename__ = "user_subscription"

    user_id: Mapped[str] = mapped_column(
        ForeignKey("user.id", ondelete="CASCADE"), index=True
    )
    plan: Mapped[str] = mapped_column(String(255))
    status: Mapped[SubscriptionStatus] = mapped_column(
        Enum(SubscriptionStatus), default=SubscriptionStatus.TRIAL
    )
    is_paid: Mapped[bool] = mapped_column(Boolean, default=False)
    start_date: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end_date: Mapped[datetime] = mapped_column(DateTime(timezone=True))

    user: Mapped["User"] = relationship("User", back_populates="subscriptions")
