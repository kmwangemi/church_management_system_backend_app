from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

from app.db.base_class import Base, TimestampMixin, UUIDMixin
from app.enums import OfferingType, PaymentMethod, PledgeStatus
from sqlalchemy import DateTime, Enum, ForeignKey, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column


class Offering(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "offering"

    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    date: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    type: Mapped[OfferingType] = mapped_column(
        Enum(OfferingType), default=OfferingType.OFFERING
    )
    payment_method: Mapped[PaymentMethod] = mapped_column(
        Enum(PaymentMethod), default=PaymentMethod.CASH
    )
    user_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("user.id", ondelete="SET NULL")
    )
    church_id: Mapped[str] = mapped_column(ForeignKey("church.id", ondelete="CASCADE"))


class Pledge(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "pledge"

    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    purpose: Mapped[Optional[str]] = mapped_column(String(255))
    status: Mapped[PledgeStatus] = mapped_column(
        Enum(PledgeStatus), default=PledgeStatus.ACTIVE
    )
    start_date: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end_date: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    user_id: Mapped[str] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"))
    church_id: Mapped[str] = mapped_column(ForeignKey("church.id", ondelete="CASCADE"))


class Expense(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "expense"

    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    category: Mapped[str] = mapped_column(String(255))
    description: Mapped[Optional[str]] = mapped_column(String(1000))
    date: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    church_id: Mapped[str] = mapped_column(ForeignKey("church.id", ondelete="CASCADE"))
