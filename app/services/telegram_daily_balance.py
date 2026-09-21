from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
from zoneinfo import ZoneInfo

import requests
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.db.models.telegram import (
    TelegramAccount,
    TelegramDigestDelivery,
    TelegramNotificationSettings,
)
from app.metrics import TELEGRAM_DIGEST
from app.schemas.portfolio import PortfolioDataHealth, PortfolioValueChange24h
from app.services.portfolio_change import portfolio_change_24h
from app.services.portfolio_health import (
    active_canonical_wallets,
    build_portfolio_data_health,
)
from app.services.wallet_view import build_wallet_balance_info

DELIVERY_LEASE_SECONDS = 10 * 60
MAX_DELIVERY_ATTEMPTS = 3
MAX_RETRY_AFTER_SECONDS = 24 * 60 * 60


@dataclass(frozen=True)
class PersistedPortfolioDigest:
    total_usd: Decimal
    data_health: PortfolioDataHealth
    change_24h: PortfolioValueChange24h


class TelegramSendError(RuntimeError):
    """A sanitized Telegram delivery failure safe to persist and log."""

    def __init__(
        self,
        code: str,
        *,
        status_code: int | None = None,
        retry_after_seconds: int | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.status_code = status_code
        self.retry_after_seconds = retry_after_seconds


def resolve_telegram_webhook_secret(settings: Settings) -> str:
    if settings.telegram_webhook_secret:
        return settings.telegram_webhook_secret
    if not settings.telegram_bot_token:
        return ""
    return sha256(
        f"pywallet-telegram-webhook-v1:{settings.telegram_bot_token}".encode()
    ).hexdigest()


def format_daily_balance(
    total_usd: Decimal,
    *,
    language: str,
    as_of: datetime | None,
    health_state: str = "fresh",
    wallets_covered: int | None = None,
    wallets_total: int | None = None,
    manual_wallets: int = 0,
    change_24h: PortfolioValueChange24h | None = None,
    alert_threshold_percent: float | None = None,
) -> str:
    amount = f"${total_usd:,.2f}"
    timestamp = (
        as_of.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC") if as_of else None
    )
    alert_percent = (
        change_24h.percent
        if change_24h is not None
        and change_24h.status == "complete"
        and change_24h.percent is not None
        and alert_threshold_percent is not None
        and abs(change_24h.percent) >= alert_threshold_percent
        else None
    )
    if alert_percent is not None:
        assert alert_threshold_percent is not None
    if language == "ru":
        lines = ["Ваш портфель", "", f"Общая стоимость: {amount}"]
        if alert_percent is not None:
            lines.append(
                f"⚠️ Изменение за 24 ч: {alert_percent:+.2f}% "
                f"(порог {alert_threshold_percent:.2f}%)"
            )
        health_labels = {
            "fresh": "Актуальные",
            "updating": "Обновляются",
            "partial": "Частичные",
            "stale": "Устарели",
        }
        if as_of is not None:
            lines.append(f"Данные на: {timestamp}")
        elif manual_wallets:
            lines.append("Время снимка: нет — учтены введённые вручную данные")
        else:
            lines.append("Сохранённых данных пока нет")
        if wallets_covered is not None and wallets_total is not None:
            lines.append(f"Покрытие: {wallets_covered}/{wallets_total} кошельков")
        if wallets_total:
            lines.append(
                f"Состояние данных: {health_labels.get(health_state, 'Частичные')}"
            )
        if health_state == "partial":
            lines.append("Итог может быть неполным — подробности доступны в портфеле.")
        elif health_state == "stale":
            lines.append("Данные устарели — откройте портфель, чтобы обновить их.")
        elif health_state == "updating":
            lines.append("Обновление выполняется; показан последний сохранённый итог.")
    else:
        lines = ["Your portfolio", "", f"Total value: {amount}"]
        if alert_percent is not None:
            lines.append(
                f"⚠️ 24h change: {alert_percent:+.2f}% "
                f"(threshold {alert_threshold_percent:.2f}%)"
            )
        health_labels = {
            "fresh": "Fresh",
            "updating": "Updating",
            "partial": "Partial",
            "stale": "Stale",
        }
        if as_of is not None:
            lines.append(f"As of: {timestamp}")
        elif manual_wallets:
            lines.append("Snapshot time: unavailable — manual data is included")
        else:
            lines.append("No saved data yet")
        if wallets_covered is not None and wallets_total is not None:
            lines.append(f"Coverage: {wallets_covered}/{wallets_total} wallets")
        if wallets_total:
            lines.append(f"Data health: {health_labels.get(health_state, 'Partial')}")
        if health_state == "partial":
            lines.append(
                "The total may be incomplete — open the portfolio for details."
            )
        elif health_state == "stale":
            lines.append("Data is stale — open the portfolio to refresh it.")
        elif health_state == "updating":
            lines.append("An update is running; this is the last saved total.")
    return "\n".join(lines)


class TelegramBotClient:
    def __init__(self, settings: Settings):
        self._token = settings.telegram_bot_token
        self._base_url = settings.telegram_api_base_url.rstrip("/")
        self._timeout = settings.telegram_request_timeout_seconds

    def _send_message(
        self, chat_id: int, text: str, mini_app_url: str, button_text: str
    ) -> None:
        try:
            response = requests.post(
                f"{self._base_url}/bot{self._token}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": text,
                    "reply_markup": {
                        "inline_keyboard": [
                            [
                                {
                                    "text": button_text,
                                    "web_app": {"url": mini_app_url},
                                }
                            ]
                        ]
                    },
                },
                timeout=self._timeout,
            )
        except requests.RequestException:
            # Request URLs contain the bot token; do not retain the original
            # exception as a chained cause that a traceback could expose.
            raise TelegramSendError("telegram_network_error") from None

        try:
            payload = response.json()
        except (requests.JSONDecodeError, ValueError):
            payload = {}
        if response.ok and payload.get("ok"):
            return

        parameters = payload.get("parameters")
        retry_after = (
            parameters.get("retry_after") if isinstance(parameters, dict) else None
        )
        if not isinstance(retry_after, int) or retry_after < 0:
            retry_after = None
        status_code = response.status_code
        raise TelegramSendError(
            f"telegram_http_{status_code}",
            status_code=status_code,
            retry_after_seconds=retry_after,
        )

    def send_daily_balance(
        self, chat_id: int, text: str, mini_app_url: str, button_text: str
    ) -> None:
        self._send_message(chat_id, text, mini_app_url, button_text)

    def configure_webhook(self, webhook_url: str, secret_token: str) -> None:
        try:
            response = requests.post(
                f"{self._base_url}/bot{self._token}/setWebhook",
                json={
                    "url": webhook_url,
                    "secret_token": secret_token,
                    "allowed_updates": ["message"],
                },
                timeout=self._timeout,
            )
        except requests.RequestException:
            raise TelegramSendError("telegram_webhook_network_error") from None

        try:
            payload = response.json()
        except (requests.JSONDecodeError, ValueError):
            payload = {}
        if response.ok and payload.get("ok"):
            return
        raise TelegramSendError(
            f"telegram_webhook_http_{response.status_code}",
            status_code=response.status_code,
        )

    def send_start_message(
        self, chat_id: int, *, language: str, mini_app_url: str
    ) -> None:
        if language == "ru":
            text = (
                "👋 Добро пожаловать в PyWallet!\n\n"
                "Чтобы перейти к своим кошелькам и портфелю, "
                "нажми «Открыть PyWallet»."
            )
            button_text = "Открыть PyWallet"
        else:
            text = (
                "👋 Welcome to PyWallet!\n\n"
                "To view your wallets and portfolio, tap “Open PyWallet”."
            )
            button_text = "Open PyWallet"
        self._send_message(chat_id, text, mini_app_url, button_text)


async def persisted_portfolio_digest(
    session: AsyncSession,
    user_id: int,
    *,
    now: datetime | None = None,
) -> PersistedPortfolioDigest:
    wallets = await active_canonical_wallets(session, user_id=user_id)
    balance_info = await build_wallet_balance_info(session, wallets)
    total = sum(
        (balance_info[wallet.id].balance_usd for wallet in wallets), Decimal("0")
    )
    data_health = await build_portfolio_data_health(
        session,
        user_id=user_id,
        wallets=wallets,
        balance_info=balance_info,
        now=now,
    )
    change_24h = await portfolio_change_24h(
        session,
        wallets,
        current_total=total,
        balance_info=balance_info,
        price_quality=data_health.price_quality,
        health_state=data_health.state,
        freshness=data_health.freshness,
        chain_issues=data_health.chain_issues,
        reference_at=now,
    )
    return PersistedPortfolioDigest(
        total_usd=total,
        data_health=data_health,
        change_24h=change_24h,
    )


def _is_due(settings: TelegramNotificationSettings, now: datetime) -> date | None:
    local_now = now.astimezone(ZoneInfo(settings.timezone))
    if local_now.time().replace(tzinfo=None) < settings.daily_at:
        return None
    return local_now.date()


async def send_due_daily_balances(
    session: AsyncSession,
    settings: Settings,
    *,
    now: datetime | None = None,
    client: TelegramBotClient | None = None,
) -> tuple[int, int]:
    """Send due opt-in digests. Returns (sent, failed)."""
    if not settings.telegram_daily_balance_enabled or not settings.telegram_bot_token:
        return 0, 0
    current = now or datetime.now(timezone.utc)
    rows = list(
        await session.execute(
            select(TelegramAccount, TelegramNotificationSettings)
            .join(
                TelegramNotificationSettings,
                TelegramNotificationSettings.telegram_account_id == TelegramAccount.id,
            )
            .where(
                TelegramNotificationSettings.enabled.is_(True),
            )
        )
    )
    bot = client or TelegramBotClient(settings)
    sent = failed = 0
    for account, notification in rows:
        local_date = _is_due(notification, current)
        if local_date is None:
            continue
        delivery = await session.scalar(
            select(TelegramDigestDelivery)
            .where(
                TelegramDigestDelivery.telegram_account_id == account.id,
                TelegramDigestDelivery.local_date == local_date,
            )
            .with_for_update()
        )
        if delivery is not None:
            if delivery.status == "sent":
                continue
            if delivery.retry_after is not None and current < delivery.retry_after:
                continue
            if delivery.attempts >= MAX_DELIVERY_ATTEMPTS:
                continue
            if (
                delivery.status == "pending"
                and delivery.attempted_at is not None
                and current - delivery.attempted_at
                < timedelta(seconds=DELIVERY_LEASE_SECONDS)
            ):
                continue
        digest = await persisted_portfolio_digest(
            session,
            account.user_id,
            now=current,
        )
        total = digest.total_usd
        if delivery is None:
            delivery_id = await session.scalar(
                insert(TelegramDigestDelivery)
                .values(
                    telegram_account_id=account.id,
                    local_date=local_date,
                    status="pending",
                    total_usd=str(total),
                    attempts=1,
                    attempted_at=current,
                )
                .on_conflict_do_nothing(constraint="uq_telegram_digest_local_date")
                .returning(TelegramDigestDelivery.id)
            )
            if delivery_id is None:
                await session.rollback()
                continue
            delivery = await session.get(TelegramDigestDelivery, delivery_id)
            if delivery is None:
                await session.rollback()
                continue
        else:
            delivery.status = "pending"
            delivery.total_usd = str(total)
            delivery.error = None
            delivery.attempts += 1
            delivery.attempted_at = current
            delivery.retry_after = None
        await session.commit()
        health = digest.data_health
        text = format_daily_balance(
            total,
            language=notification.language,
            as_of=health.as_of,
            health_state=health.state,
            wallets_covered=health.wallets_covered,
            wallets_total=health.wallets_total,
            manual_wallets=health.manual_wallets,
            change_24h=digest.change_24h,
            alert_threshold_percent=(
                float(notification.alert_threshold_percent)
                if notification.alert_threshold_percent is not None
                else None
            ),
        )
        try:
            bot.send_daily_balance(
                account.telegram_user_id,
                text,
                settings.telegram_mini_app_url,
                (
                    "Открыть портфель"
                    if notification.language == "ru"
                    else "Open portfolio"
                ),
            )
        except TelegramSendError as exc:
            delivery.status = "failed"
            delivery.error = exc.code
            if exc.retry_after_seconds is not None:
                delivery.retry_after = current + timedelta(
                    seconds=min(exc.retry_after_seconds, MAX_RETRY_AFTER_SECONDS)
                )
            if exc.status_code == 403:
                notification.enabled = False
            failed += 1
        except Exception:
            delivery.status = "failed"
            delivery.error = "telegram_unexpected_error"
            failed += 1
        else:
            delivery.status = "sent"
            delivery.sent_at = datetime.now(timezone.utc)
            sent += 1
        TELEGRAM_DIGEST.labels(
            language=notification.language,
            outcome=delivery.status,
            health_state=health.state,
        ).inc()
        await session.commit()
    return sent, failed
