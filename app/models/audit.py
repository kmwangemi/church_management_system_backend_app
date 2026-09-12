from typing import Optional

from sqlalchemy import JSON, ForeignKey, String
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base_class import Base, TimestampMixin, UUIDMixin


class AuditLog(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "audit_log"

    action: Mapped[str] = mapped_column(String(255), index=True)
    resource_type: Mapped[Optional[str]] = mapped_column(String(255))
    resource_id: Mapped[Optional[str]] = mapped_column(String(255))
    user_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("user.id", ondelete="SET NULL"), index=True
    )
    church_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("church.id", ondelete="CASCADE"), index=True
    )
    metadata_json: Mapped[Optional[dict]] = mapped_column(JSON)
    ip_address: Mapped[Optional[str]] = mapped_column(String(45))
    user_agent: Mapped[Optional[str]] = mapped_column(String(500))
