from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal
from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Time,
    UniqueConstraint,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

if TYPE_CHECKING:
    from app.db.models.user import User


class TelegramAccount(Base):
    __tablename__ = "telegram_accounts"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("users.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )
    telegram_user_id: Mapped[int] = mapped_column(
        BigInteger, unique=True, nullable=False
    )
    username: Mapped[str | None] = mapped_column(String(64), nullable=True)
    first_name: Mapped[str] = mapped_column(String(128), nullable=False)
    last_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    language_code: Mapped[str | None] = mapped_column(String(16), nullable=True)
    allows_write_to_pm: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )
    last_authenticated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    user: Mapped[User] = relationship(back_populates="telegram_account")
    notification_settings: Mapped[TelegramNotificationSettings | None] = relationship(
        back_populates="telegram_account", cascade="all, delete-orphan", uselist=False
    )
    deliveries: Mapped[list[TelegramDigestDelivery]] = relationship(
        back_populates="telegram_account", cascade="all, delete-orphan"
    )


class TelegramNotificationSettings(Base):
    __tablename__ = "telegram_notification_settings"

    telegram_account_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("telegram_accounts.id", ondelete="CASCADE"),
        primary_key=True,
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false"
    )
    timezone: Mapped[str] = mapped_column(
        String(64), nullable=False, server_default="UTC"
    )
    daily_at: Mapped[time] = mapped_column(
        Time(timezone=False), nullable=False, server_default="09:00:00"
    )
    language: Mapped[str] = mapped_column(
        String(2), nullable=False, server_default="en"
    )
    alert_threshold_percent: Mapped[Decimal | None] = mapped_column(
        Numeric(7, 2), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    telegram_account: Mapped[TelegramAccount] = relationship(
        back_populates="notification_settings"
    )


class TelegramDigestDelivery(Base):
    __tablename__ = "telegram_digest_deliveries"
    __table_args__ = (
        UniqueConstraint(
            "telegram_account_id", "local_date", name="uq_telegram_digest_local_date"
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    telegram_account_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("telegram_accounts.id", ondelete="CASCADE"),
        nullable=False,
    )
    local_date: Mapped[date] = mapped_column(Date, nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    total_usd: Mapped[str] = mapped_column(String(64), nullable=False)
    error: Mapped[str | None] = mapped_column(String(250), nullable=True)
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    attempted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    retry_after: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    telegram_account: Mapped[TelegramAccount] = relationship(
        back_populates="deliveries"
    )
