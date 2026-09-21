from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Numeric,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.db.models.user import User


class AllocationTarget(Base):
    __tablename__ = "allocation_targets"
    __table_args__ = (
        CheckConstraint(
            "target_pct > 0 AND target_pct <= 100",
            name="ck_allocation_targets_pct_range",
        ),
        UniqueConstraint(
            "user_id",
            "asset_key",
            name="uq_allocation_targets_user_asset",
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    asset_key: Mapped[str] = mapped_column(String(200), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    target_pct: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    user: Mapped[User] = relationship(back_populates="allocation_targets")
