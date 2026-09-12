from datetime import datetime
from decimal import Decimal
from typing import Optional

from app.db.base_class import Base, TimestampMixin, UUIDMixin
from app.enums import AssetCondition, AssetStatus, PrayerStatus
from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column


class Asset(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "asset"

    name: Mapped[str] = mapped_column(String(255))
    church_id: Mapped[str] = mapped_column(ForeignKey("church.id", ondelete="CASCADE"))
    value: Mapped[Optional[Decimal]] = mapped_column(Numeric(12, 2))
    purchase_date: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    status: Mapped[AssetStatus] = mapped_column(
        Enum(AssetStatus), default=AssetStatus.ACTIVE
    )
    condition: Mapped[AssetCondition] = mapped_column(
        Enum(AssetCondition), default=AssetCondition.GOOD
    )
    description: Mapped[Optional[str]] = mapped_column(String(1000))


class MaintenanceRecord(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "maintenance_record"

    asset_id: Mapped[str] = mapped_column(ForeignKey("asset.id", ondelete="CASCADE"))
    cost: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    date: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    description: Mapped[Optional[str]] = mapped_column(String(1000))


class Announcement(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "announcement"

    title: Mapped[str] = mapped_column(String(255))
    content: Mapped[str] = mapped_column(String)
    church_id: Mapped[str] = mapped_column(ForeignKey("church.id", ondelete="CASCADE"))
    author_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("user.id", ondelete="SET NULL")
    )
    publish_date: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class PrayerRequest(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "prayer_request"

    title: Mapped[str] = mapped_column(String(255))
    content: Mapped[str] = mapped_column(String)
    status: Mapped[PrayerStatus] = mapped_column(
        Enum(PrayerStatus), default=PrayerStatus.ACTIVE
    )
    user_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("user.id", ondelete="CASCADE")
    )
    church_id: Mapped[str] = mapped_column(ForeignKey("church.id", ondelete="CASCADE"))
