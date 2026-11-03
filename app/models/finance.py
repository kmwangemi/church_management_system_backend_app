from datetime import datetime, timezone
from decimal import Decimal
from typing import List, Optional

from app.db.base_class import Base, SoftDeleteMixin, TimestampMixin, UUIDMixin
from app.enums import (
    GiftStatus,
    OfferingType,
    PaymentMethod,
    PaymentSchedule,
    PledgeStatus,
)
from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Numeric,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

DEFAULT_CURRENCY = "KES"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Fund(Base, UUIDMixin, TimestampMixin):
    """Giving category: tithe, building fund, missions, ..."""

    __tablename__ = "fund"
    __table_args__ = (UniqueConstraint("church_id", "name", name="uq_fund_church_name"),)

    church_id: Mapped[str] = mapped_column(
        ForeignKey("church.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(255))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class Offering(Base, UUIDMixin, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "offering"
    __table_args__ = (Index("ix_offering_church_received", "church_id", "received_at"),)

    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    currency: Mapped[str] = mapped_column(String(3), default=DEFAULT_CURRENCY)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
    type: Mapped[OfferingType] = mapped_column(
        Enum(OfferingType), default=OfferingType.OFFERING
    )
    payment_method: Mapped[PaymentMethod] = mapped_column(
        Enum(PaymentMethod), default=PaymentMethod.CASH
    )
    church_id: Mapped[str] = mapped_column(
        ForeignKey("church.id", ondelete="CASCADE"), index=True
    )
    # Nullable so reporting by branch is optional for legacy rows.
    branch_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("branch.id", ondelete="SET NULL"), index=True
    )
    # NULL = anonymous giving.
    member_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("member.id", ondelete="SET NULL"), index=True
    )
    fund_id: Mapped[str] = mapped_column(ForeignKey("fund.id"), index=True)
    # Which pledge this offering counts toward (optional).
    pledge_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("pledge.id", ondelete="SET NULL"), index=True
    )

    fund: Mapped["Fund"] = relationship("Fund")
    pledge: Mapped[Optional["Pledge"]] = relationship(
        "Pledge", back_populates="offerings"
    )


class Pledge(Base, UUIDMixin, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "pledge"

    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    currency: Mapped[str] = mapped_column(String(3), default=DEFAULT_CURRENCY)
    purpose: Mapped[Optional[str]] = mapped_column(String(255))
    status: Mapped[PledgeStatus] = mapped_column(
        Enum(PledgeStatus), default=PledgeStatus.ACTIVE
    )
    start_date: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    end_date: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    church_id: Mapped[str] = mapped_column(
        ForeignKey("church.id", ondelete="CASCADE"), index=True
    )
    branch_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("branch.id", ondelete="SET NULL"), index=True
    )
    member_id: Mapped[str] = mapped_column(
        ForeignKey("member.id", ondelete="CASCADE"), index=True
    )
    fund_id: Mapped[str] = mapped_column(ForeignKey("fund.id"), index=True)

    fund: Mapped["Fund"] = relationship("Fund")
    offerings: Mapped[List["Offering"]] = relationship(
        "Offering", back_populates="pledge"
    )


class RecurringGift(Base, UUIDMixin, TimestampMixin):
    """A member's scheduled recurring giving (tithes etc.). Separate from UserSubscription (SaaS billing)."""

    __tablename__ = "recurring_gift"

    member_id: Mapped[str] = mapped_column(
        ForeignKey("member.id", ondelete="CASCADE"), index=True
    )
    fund_id: Mapped[str] = mapped_column(ForeignKey("fund.id"), index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    currency: Mapped[str] = mapped_column(String(3), default=DEFAULT_CURRENCY)
    interval: Mapped[PaymentSchedule] = mapped_column(
        Enum(PaymentSchedule), default=PaymentSchedule.MONTHLY
    )
    status: Mapped[GiftStatus] = mapped_column(
        Enum(GiftStatus), default=GiftStatus.ACTIVE
    )
    next_charge_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))

    fund: Mapped["Fund"] = relationship("Fund")


class Expense(Base, UUIDMixin, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "expense"
    __table_args__ = (Index("ix_expense_church_incurred", "church_id", "incurred_at"),)

    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    currency: Mapped[str] = mapped_column(String(3), default=DEFAULT_CURRENCY)
    category: Mapped[str] = mapped_column(String(255))
    description: Mapped[Optional[str]] = mapped_column(String(1000))
    incurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow
    )
    church_id: Mapped[str] = mapped_column(
        ForeignKey("church.id", ondelete="CASCADE"), index=True
    )
    branch_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("branch.id", ondelete="SET NULL"), index=True
    )
    approved_by_member_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("member.id", ondelete="SET NULL")
    )
