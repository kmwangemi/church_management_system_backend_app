from typing import Optional

from app.db.base_class import Base, TimestampMixin, UUIDMixin
from sqlalchemy import JSON, ForeignKey, Index, String
from sqlalchemy.orm import Mapped, mapped_column


class AuditLog(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "audit_log"
    __table_args__ = (
        Index("ix_audit_log_church_created", "church_id", "created_at"),
        Index("ix_audit_log_entity", "entity_type", "entity_id"),
    )

    action: Mapped[str] = mapped_column(String(255), index=True)
    # renamed from resource_type / resource_id
    entity_type: Mapped[Optional[str]] = mapped_column(String(255))
    entity_id: Mapped[Optional[str]] = mapped_column(String(255))
    # renamed from user_id
    actor_user_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("user.id", ondelete="SET NULL"), index=True
    )
    # SET NULL (was CASCADE) so audit history survives a church being removed.
    church_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("church.id", ondelete="SET NULL"), index=True
    )
    old_value: Mapped[Optional[dict]] = mapped_column(JSON)
    new_value: Mapped[Optional[dict]] = mapped_column(JSON)
    metadata_json: Mapped[Optional[dict]] = mapped_column(JSON)
    ip_address: Mapped[Optional[str]] = mapped_column(String(45))
    user_agent: Mapped[Optional[str]] = mapped_column(String(500))
