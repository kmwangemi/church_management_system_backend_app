import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import DateTime
from sqlalchemy.ext.declarative import declared_attr
from sqlalchemy.ext.hybrid import hybrid_property
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


class SoftDeleteMixin:
    """
    Soft delete via a nullable `deleted_at` timestamp (NULL = not deleted).
    Replaces the old `is_deleted` boolean columns. The hybrid `is_deleted`
    keeps existing code working, both on instances (`obj.is_deleted`) and in
    queries (`.where(Church.is_deleted.is_(False))`).

    Always filter soft-deleted rows in queries on Church, Branch, Member, User,
    Offering, Pledge and Expense, e.g. `.where(Member.deleted_at.is_(None))`.
    """

    deleted_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )

    @hybrid_property
    def is_deleted(self) -> bool:
        return self.deleted_at is not None

    @is_deleted.inplace.expression
    @classmethod
    def _is_deleted_expression(cls):
        return cls.deleted_at.is_not(None)

    def soft_delete(self) -> None:
        self.deleted_at = datetime.now(timezone.utc)

    def restore(self) -> None:
        self.deleted_at = None
