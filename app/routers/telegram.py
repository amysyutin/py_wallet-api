from secrets import compare_digest
from typing import Annotated, Any

from fastapi import APIRouter, Header, HTTPException, Response, status
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import select

from app.core.config import get_settings
from app.db.models.telegram import TelegramAccount, TelegramNotificationSettings
from app.deps import CurrentUser, SessionDep
from app.schemas.telegram import TelegramSettingsRead, TelegramSettingsUpdate
from app.services.telegram_daily_balance import (
    TelegramBotClient,
    TelegramSendError,
    resolve_telegram_webhook_secret,
)

router = APIRouter(prefix="/telegram", tags=["telegram"])


def _telegram_start_payload(update: dict[str, Any]) -> tuple[int, str] | None:
    message = update.get("message")
    if not isinstance(message, dict):
        return None
    text = message.get("text")
    if not isinstance(text, str):
        return None
    settings = get_settings()
    bot_username = settings.telegram_bot_username.lstrip("@").lower()
    command = text.strip().split(maxsplit=1)[0].lower()
    if command not in {"/start", f"/start@{bot_username}"}:
        return None
    chat = message.get("chat")
    sender = message.get("from")
    if not isinstance(chat, dict) or not isinstance(chat.get("id"), int):
        return None
    language_code = sender.get("language_code") if isinstance(sender, dict) else None
    language = (
        "ru"
        if isinstance(language_code, str) and language_code.lower().startswith("ru")
        else "en"
    )
    return chat["id"], language


@router.post(
    "/webhook", status_code=status.HTTP_204_NO_CONTENT, include_in_schema=False
)
async def telegram_webhook(
    update: dict[str, Any],
    secret_token: Annotated[
        str | None, Header(alias="X-Telegram-Bot-Api-Secret-Token")
    ] = None,
) -> Response:
    settings = get_settings()
    webhook_secret = resolve_telegram_webhook_secret(settings)
    if not settings.telegram_bot_token or not webhook_secret:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE, "Telegram webhook is disabled"
        )
    if secret_token is None or not compare_digest(secret_token, webhook_secret):
        raise HTTPException(
            status.HTTP_403_FORBIDDEN, "Invalid Telegram webhook secret"
        )

    start = _telegram_start_payload(update)
    if start is not None:
        chat_id, language = start
        try:
            await run_in_threadpool(
                TelegramBotClient(settings).send_start_message,
                chat_id,
                language=language,
                mini_app_url=settings.telegram_mini_app_url,
            )
        except TelegramSendError:
            raise HTTPException(
                status.HTTP_502_BAD_GATEWAY, "Telegram delivery failed"
            ) from None
    return Response(status_code=status.HTTP_204_NO_CONTENT)


async def _account_and_settings(current_user: CurrentUser, session: SessionDep):
    account = await session.scalar(
        select(TelegramAccount).where(TelegramAccount.user_id == current_user.id)
    )
    if account is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Telegram account is not linked")
    settings = await session.get(TelegramNotificationSettings, account.id)
    if settings is None:
        settings = TelegramNotificationSettings(telegram_account_id=account.id)
        session.add(settings)
        await session.flush()
    return account, settings


def _response(account, settings) -> TelegramSettingsRead:
    return TelegramSettingsRead(
        enabled=settings.enabled,
        timezone=settings.timezone,
        daily_at=settings.daily_at,
        language=settings.language,
        alert_threshold_percent=(
            float(settings.alert_threshold_percent)
            if settings.alert_threshold_percent is not None
            else None
        ),
        allows_write_to_pm=account.allows_write_to_pm,
    )


@router.get("/settings", response_model=TelegramSettingsRead)
async def get_telegram_settings(
    current_user: CurrentUser, session: SessionDep
) -> TelegramSettingsRead:
    account, settings = await _account_and_settings(current_user, session)
    return _response(account, settings)


@router.patch("/settings", response_model=TelegramSettingsRead)
async def update_telegram_settings(
    payload: TelegramSettingsUpdate,
    current_user: CurrentUser,
    session: SessionDep,
) -> TelegramSettingsRead:
    account, settings = await _account_and_settings(current_user, session)
    updates = payload.model_dump(exclude_unset=True)
    for key, value in updates.items():
        setattr(settings, key, value)
    await session.commit()
    return _response(account, settings)
