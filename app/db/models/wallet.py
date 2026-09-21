from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    String,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.db.models.manual_balance import ManualBalance
    from app.db.models.snapshot import Snapshot
    from app.db.models.transaction import Transaction
    from app.db.models.user import User
    from app.db.models.wallet_group import WalletGroup


class Wallet(Base):
    __tablename__ = "wallets"
    __table_args__ = (
        Index("ix_wallets_user_id_is_active", "user_id", "is_active"),
        Index(
            "uq_wallets_active_evm_address",
            "user_id",
            text("lower(btrim(address))"),
            unique=True,
            postgresql_where=text(
                "wallet_type = 'evm' AND is_active IS TRUE AND address IS NOT NULL"
            ),
        ),
        Index(
            "uq_wallets_active_solana_address",
            "user_id",
            text("btrim(address)"),
            unique=True,
            postgresql_where=text(
                "wallet_type = 'solana' AND is_active IS TRUE "
                "AND address IS NOT NULL"
            ),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    group_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("wallet_groups.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    label: Mapped[str] = mapped_column(String(100))
    address: Mapped[str | None] = mapped_column(String(128), nullable=True)
    chain_type: Mapped[str] = mapped_column(String(32))
    wallet_type: Mapped[str] = mapped_column(String(32), nullable=False, default="evm")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    notes: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    address_updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    user: Mapped[User] = relationship(back_populates="wallets")
    group: Mapped[WalletGroup | None] = relationship(back_populates="wallets")
    snapshots: Mapped[list[Snapshot]] = relationship(
        back_populates="wallet", cascade="all, delete-orphan"
    )
    transactions: Mapped[list[Transaction]] = relationship(
        back_populates="wallet", cascade="all, delete-orphan"
    )
    manual_balances: Mapped[list[ManualBalance]] = relationship(
        back_populates="wallet", cascade="all, delete-orphan"
    )
