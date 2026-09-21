from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import TYPE_CHECKING

from sqlalchemy import BigInteger, DateTime, Enum as SAEnum, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.db.models.allocation_target import AllocationTarget
    from app.db.models.telegram import TelegramAccount
    from app.db.models.wallet import Wallet
    from app.db.models.wallet_group import WalletGroup


class UserRole(str, Enum):
    user = "user"
    admin = "admin"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    email: Mapped[str | None] = mapped_column(
        String(320), unique=True, index=True, nullable=True
    )
    auth_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    role: Mapped[UserRole] = mapped_column(
        SAEnum(
            UserRole,
            name="user_role",
            native_enum=False,
            validate_strings=True,
            values_callable=lambda x: [e.value for e in x],
        ),
        nullable=False,
        server_default=UserRole.user.value,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    wallets: Mapped[list[Wallet]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    wallet_groups: Mapped[list[WalletGroup]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    allocation_targets: Mapped[list[AllocationTarget]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    telegram_account: Mapped[TelegramAccount | None] = relationship(
        back_populates="user", cascade="all, delete-orphan", uselist=False
    )
