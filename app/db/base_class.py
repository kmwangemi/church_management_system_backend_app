import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import DateTime
from sqlalchemy.ext.declarative import declared_attr
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    id: Any
    __name__: str

    type_annotation_map = {
        datetime: DateTime(timezone=True),
    }

    # Generate __tablename__ automatically
    @declared_attr.directive
    def __tablename__(self, cls) -> str:
        return cls.__name__.lower()


class UUIDMixin:
    id: Mapped[str] = mapped_column(
        primary_key=True, default=lambda: str(uuid.uuid4()), index=True
    )


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )
