from typing import TYPE_CHECKING, List, Optional

from sqlalchemy import ForeignKey, String
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base_class import Base, TimestampMixin, UUIDMixin

if TYPE_CHECKING:
    from app.models.church import Church, Branch
    from app.models.user import User


class Member(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "member"

    church_id: Mapped[str] = mapped_column(ForeignKey("church.id", ondelete="CASCADE"))
    user_id: Mapped[str] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"))
    # Role array (e.g. ["MEMBER", "ADMIN"])
    roles: Mapped[List[str]] = mapped_column(ARRAY(String), default=["MEMBER"])
    position: Mapped[Optional[str]] = mapped_column(String(255), default="member")
    branch_id: Mapped[Optional[str]] = mapped_column(ForeignKey("branch.id"))
    # Relations
    church: Mapped["Church"] = relationship("Church", back_populates="members")
    user: Mapped["User"] = relationship("User", back_populates="memberships")
    branch: Mapped[Optional["Branch"]] = relationship("Branch", back_populates="members")


class BranchMember(Base, UUIDMixin, TimestampMixin):
    __tablename__ = "branch_member"

    branch_id: Mapped[str] = mapped_column(ForeignKey("branch.id", ondelete="CASCADE"))
    user_id: Mapped[str] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"))
    # Relations
    branch: Mapped["Branch"] = relationship("Branch", back_populates="branch_members")
    # we don't have back_populates on user for branch_members right now, but we can add it later if needed.
    # user: Mapped["User"] = relationship("User")
