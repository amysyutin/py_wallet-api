import hashlib
import hmac
import json
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal
from urllib.parse import urlencode

import pytest
from sqlalchemy import select

from app.core.config import Settings, get_settings
from app.core.security import decode_access_token
from app.db.models.telegram import (
    TelegramAccount,
    TelegramDigestDelivery,
    TelegramNotificationSettings,
)
from app.db.models.snapshot_service import (
    ChainSnapshot,
    SnapshotBalanceSnapshot,
    SnapshotRun,
    WalletSnapshot,
)
from app.db.models.user import User
from app.db.models.wallet import Wallet
from app.db.models.wallet_group import WalletGroup
from app.metrics import REGISTRATION_COMPLETED, TELEGRAM_DIGEST
from app.schemas.portfolio import PortfolioValueChange24h
from app.services.telegram_auth import TelegramInitDataError, validate_init_data
from app.services.telegram_daily_balance import (
    TelegramBotClient,
    TelegramSendError,
    format_daily_balance,
    resolve_telegram_webhook_secret,
    send_due_daily_balances,
)

BOT_TOKEN = "123456:test-token"


def signed_init_data(
    user_id: int = 10001,
    *,
    auth_date: int | None = None,
    allows_write: bool = True,
) -> str:
    user = {
        "id": user_id,
        "first_name": "Alex",
        "username": "alex_wallet",
        "language_code": "ru",
    }
    if allows_write:
        user["allows_write_to_pm"] = True
    values = {
        "auth_date": str(auth_date or int(datetime.now(timezone.utc).timestamp())),
        "query_id": "AAHdF6IQAAAAAN0XohDhrOrc",
        "user": json.dumps(user, separators=(",", ":")),
    }
    check = "\n".join(f"{key}={values[key]}" for key in sorted(values))
    secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
    values["hash"] = hmac.new(secret, check.encode(), hashlib.sha256).hexdigest()
    return urlencode(values)


def configure_telegram(monkeypatch) -> None:
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", BOT_TOKEN)
    get_settings.cache_clear()


def configure_telegram_webhook(monkeypatch) -> None:
    configure_telegram(monkeypatch)
    monkeypatch.setenv("TELEGRAM_WEBHOOK_SECRET", "test-webhook-secret")
    get_settings.cache_clear()


def test_webhook_secret_is_derived_when_only_bot_token_is_configured():
    config = Settings(
        app_env="test",
        jwt_secret="ci-test-secret",
        telegram_bot_token=BOT_TOKEN,
    )

    first = resolve_telegram_webhook_secret(config)
    second = resolve_telegram_webhook_secret(config)

    assert first == second
    assert len(first) == 64
    assert BOT_TOKEN not in first


def test_validate_init_data_rejects_tampering_and_expiry():
    now = datetime(2026, 7, 19, 12, 0, tzinfo=timezone.utc)
    valid = signed_init_data(auth_date=int(now.timestamp()))
    assert (
        validate_init_data(valid, BOT_TOKEN, max_age_seconds=300, now=now).id == 10001
    )
    with pytest.raises(TelegramInitDataError, match="signature"):
        validate_init_data(
            valid.replace("Alex", "Mallory"), BOT_TOKEN, max_age_seconds=300, now=now
        )
    expired = signed_init_data(auth_date=int(now.timestamp()) - 301)
    with pytest.raises(TelegramInitDataError, match="Expired"):
        validate_init_data(expired, BOT_TOKEN, max_age_seconds=300, now=now)


def test_validate_init_data_rejects_duplicate_fields():
    valid = signed_init_data()
    with pytest.raises(TelegramInitDataError, match="Duplicate"):
        validate_init_data(f"auth_date=1&{valid}", BOT_TOKEN, max_age_seconds=300)


@pytest.mark.asyncio
async def test_start_webhook_sends_localized_mini_app_intro(client, monkeypatch):
    configure_telegram_webhook(monkeypatch)
    sent = []

    def capture_start(self, chat_id, *, language, mini_app_url):
        sent.append((chat_id, language, mini_app_url))

    monkeypatch.setattr(TelegramBotClient, "send_start_message", capture_start)
    update = {
        "update_id": 1,
        "message": {
            "text": "/start referral",
            "chat": {"id": 4242},
            "from": {"language_code": "ru-RU"},
        },
    }
    forbidden = await client.post("/telegram/webhook", json=update)
    assert forbidden.status_code == 403

    response = await client.post(
        "/telegram/webhook",
        json=update,
        headers={"X-Telegram-Bot-Api-Secret-Token": "test-webhook-secret"},
    )
    assert response.status_code == 204
    assert sent == [(4242, "ru", "https://pywallet.dev/telegram")]

    ignored = await client.post(
        "/telegram/webhook",
        json={"message": {"text": "/help", "chat": {"id": 4242}}},
        headers={"X-Telegram-Bot-Api-Secret-Token": "test-webhook-secret"},
    )
    assert ignored.status_code == 204
    assert len(sent) == 1
    get_settings.cache_clear()


def test_start_message_opens_mini_app_without_repeating_profile_copy(monkeypatch):
    config = Settings(
        app_env="test",
        jwt_secret="ci-test-secret",
        telegram_bot_token=BOT_TOKEN,
    )
    sent = []
    monkeypatch.setattr(
        TelegramBotClient,
        "_send_message",
        lambda self, chat_id, text, mini_app_url, button_text: sent.append(
            (chat_id, text, mini_app_url, button_text)
        ),
    )

    TelegramBotClient(config).send_start_message(
        42, language="ru", mini_app_url="https://pywallet.dev/telegram"
    )

    assert sent[0][0] == 42
    assert sent[0][1] == (
        "👋 Добро пожаловать в PyWallet!\n\n"
        "Чтобы перейти к своим кошелькам и портфелю, нажми «Открыть PyWallet»."
    )
    assert sent[0][2] == "https://pywallet.dev/telegram"
    assert sent[0][3] == "Открыть PyWallet"


def test_configure_webhook_registers_message_updates(monkeypatch):
    config = Settings(
        app_env="test",
        jwt_secret="ci-test-secret",
        telegram_bot_token=BOT_TOKEN,
    )
    requests = []

    class Response:
        ok = True
        status_code = 200

        @staticmethod
        def json():
            return {"ok": True, "result": True}

    def capture_post(url, *, json, timeout):
        requests.append((url, json, timeout))
        return Response()

    monkeypatch.setattr(
        "app.services.telegram_daily_balance.requests.post", capture_post
    )
    TelegramBotClient(config).configure_webhook(
        "https://pywallet.dev/api/telegram/webhook", "webhook-secret"
    )

    assert requests == [
        (
            f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook",
            {
                "url": "https://pywallet.dev/api/telegram/webhook",
                "secret_token": "webhook-secret",
                "allowed_updates": ["message"],
            },
            10.0,
        )
    ]


def test_manual_webhook_setup_uses_derived_secret(monkeypatch):
    from scripts.configure_telegram_webhook import main

    config = Settings(
        app_env="test",
        jwt_secret="ci-test-secret",
        telegram_bot_token=BOT_TOKEN,
    )
    configured = []
    monkeypatch.setattr(
        "scripts.configure_telegram_webhook.get_settings", lambda: config
    )
    monkeypatch.setattr(
        TelegramBotClient,
        "configure_webhook",
        lambda self, url, secret: configured.append((url, secret)),
    )

    main()

    assert configured == [
        (
            "https://pywallet.dev/api/telegram/webhook",
            resolve_telegram_webhook_secret(config),
        )
    ]


@pytest.mark.asyncio
async def test_telegram_login_is_stable_and_notifications_are_opt_in(
    client, db_session, monkeypatch
):
    configure_telegram(monkeypatch)
    registration_before = REGISTRATION_COMPLETED.labels(channel="telegram")._value.get()
    payload = {"init_data": signed_init_data()}
    first = await client.post("/auth/telegram", json=payload)
    assert first.status_code == 200
    assert first.json()["is_new_user"] is True
    assert first.json()["email_linked"] is False
    user_id = int(decode_access_token(first.json()["access_token"]))

    second = await client.post("/auth/telegram", json=payload)
    assert second.status_code == 200
    assert second.json()["is_new_user"] is False
    assert int(decode_access_token(second.json()["access_token"])) == user_id
    assert (
        REGISTRATION_COMPLETED.labels(channel="telegram")._value.get()
        == registration_before + 1
    )

    headers = {"Authorization": f"Bearer {first.json()['access_token']}"}
    settings = await client.get("/telegram/settings", headers=headers)
    assert settings.json() == {
        "enabled": False,
        "timezone": "UTC",
        "daily_at": "09:00:00",
        "language": "ru",
        "alert_threshold_percent": None,
        "allows_write_to_pm": True,
    }
    updated = await client.patch(
        "/telegram/settings",
        headers=headers,
        json={
            "enabled": True,
            "timezone": "Asia/Ho_Chi_Minh",
            "daily_at": "13:00:00",
            "language": "en",
            "alert_threshold_percent": 7.5,
        },
    )
    assert updated.status_code == 200
    assert updated.json()["enabled"] is True
    assert updated.json()["timezone"] == "Asia/Ho_Chi_Minh"
    assert updated.json()["alert_threshold_percent"] == 7.5

    users = list(await db_session.scalars(select(User).where(User.id == user_id)))
    assert len(users) == 1
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_user_can_opt_in_before_write_access_is_reflected(client, monkeypatch):
    configure_telegram(monkeypatch)
    login = await client.post(
        "/auth/telegram",
        json={"init_data": signed_init_data(10002, allows_write=False)},
    )
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    response = await client.patch(
        "/telegram/settings", headers=headers, json={"enabled": True}
    )
    assert response.status_code == 200
    assert response.json()["enabled"] is True
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_telegram_settings_reject_explicit_null(client, monkeypatch):
    configure_telegram(monkeypatch)
    login = await client.post(
        "/auth/telegram",
        json={"init_data": signed_init_data(10005)},
    )
    response = await client.patch(
        "/telegram/settings",
        headers={"Authorization": f"Bearer {login.json()['access_token']}"},
        json={"enabled": None},
    )
    assert response.status_code == 422
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_telegram_threshold_can_be_disabled_with_null(client, monkeypatch):
    configure_telegram(monkeypatch)
    login = await client.post(
        "/auth/telegram",
        json={"init_data": signed_init_data(10006)},
    )
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    enabled = await client.patch(
        "/telegram/settings",
        headers=headers,
        json={"alert_threshold_percent": 12.5},
    )
    assert enabled.status_code == 200
    assert enabled.json()["alert_threshold_percent"] == 12.5

    disabled = await client.patch(
        "/telegram/settings",
        headers=headers,
        json={"alert_threshold_percent": None},
    )
    assert disabled.status_code == 200
    assert disabled.json()["alert_threshold_percent"] is None

    too_small = await client.patch(
        "/telegram/settings",
        headers=headers,
        json={"alert_threshold_percent": 0},
    )
    too_large = await client.patch(
        "/telegram/settings",
        headers=headers,
        json={"alert_threshold_percent": 1000.1},
    )
    assert too_small.status_code == 422
    assert too_large.status_code == 422
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_link_email_moves_telegram_identity(client, db_session, monkeypatch):
    configure_telegram(monkeypatch)
    registered = await client.post(
        "/auth/register", json={"email": "owner@example.com", "password": "secretpass"}
    )
    target_id = registered.json()["id"]
    login = await client.post(
        "/auth/telegram", json={"init_data": signed_init_data(10003)}
    )
    telegram_user_id = int(decode_access_token(login.json()["access_token"]))
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    linked = await client.post(
        "/auth/telegram/link-email",
        headers=headers,
        json={"email": "OWNER@example.com", "password": "secretpass"},
    )
    assert linked.status_code == 200
    assert int(decode_access_token(linked.json()["access_token"])) == target_id
    assert await db_session.get(User, telegram_user_id) is None
    account = await db_session.scalar(
        select(TelegramAccount).where(TelegramAccount.telegram_user_id == 10003)
    )
    assert account.user_id == target_id
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_link_email_refuses_to_orphan_portfolio_group(
    client, db_session, monkeypatch
):
    configure_telegram(monkeypatch)
    await client.post(
        "/auth/register",
        json={"email": "group-owner@example.com", "password": "secretpass"},
    )
    login = await client.post(
        "/auth/telegram", json={"init_data": signed_init_data(10004)}
    )
    telegram_user_id = int(decode_access_token(login.json()["access_token"]))
    db_session.add(WalletGroup(user_id=telegram_user_id, name="Telegram group"))
    await db_session.commit()

    linked = await client.post(
        "/auth/telegram/link-email",
        headers={"Authorization": f"Bearer {login.json()['access_token']}"},
        json={
            "email": "group-owner@example.com",
            "password": "secretpass",
        },
    )
    assert linked.status_code == 409
    assert await db_session.get(User, telegram_user_id) is not None
    get_settings.cache_clear()


class FakeTelegramClient:
    def __init__(self):
        self.messages = []

    def send_daily_balance(self, chat_id, text, mini_app_url, button_text):
        self.messages.append((chat_id, text, mini_app_url, button_text))


class FailingTelegramClient:
    def __init__(self, error):
        self.error = error
        self.calls = 0

    def send_daily_balance(self, chat_id, text, mini_app_url, button_text):
        self.calls += 1
        raise self.error


async def _add_digest_snapshot(
    db_session,
    wallet: Wallet,
    *,
    observed_at: datetime,
    total_usd: Decimal,
    priced_asset: bool = False,
) -> None:
    run = SnapshotRun(
        user_id=wallet.user_id,
        wallet_id=wallet.id,
        trigger_type="scheduler",
        scope_type="wallet",
        status="success",
        finished_at=observed_at,
    )
    db_session.add(run)
    await db_session.flush()
    snapshot = WalletSnapshot(
        snapshot_run_id=run.id,
        wallet_id=wallet.id,
        wallet_type="evm",
        status="success",
        total_usd=total_usd,
        finished_at=observed_at,
    )
    db_session.add(snapshot)
    await db_session.flush()
    chain_snapshot = ChainSnapshot(
        wallet_snapshot_id=snapshot.id,
        chain="base",
        status="success",
        total_usd=total_usd,
        finished_at=observed_at,
    )
    db_session.add(chain_snapshot)
    await db_session.flush()
    if priced_asset:
        db_session.add(
            SnapshotBalanceSnapshot(
                chain_snapshot_id=chain_snapshot.id,
                asset_symbol="ETH",
                asset_type="native",
                amount=Decimal("1"),
                price_usd=total_usd,
                value_usd=total_usd,
                price_source="coingecko",
            )
        )


@pytest.mark.asyncio
async def test_daily_balance_is_due_and_idempotent(db_session):
    user = User(email=None, auth_hash=None)
    db_session.add(user)
    await db_session.flush()
    account = TelegramAccount(
        user_id=user.id,
        telegram_user_id=20001,
        first_name="Test",
        allows_write_to_pm=True,
    )
    db_session.add(account)
    await db_session.flush()
    db_session.add(
        TelegramNotificationSettings(
            telegram_account_id=account.id,
            enabled=True,
            timezone="Asia/Ho_Chi_Minh",
            daily_at=time(9, 0),
            language="ru",
        )
    )
    await db_session.commit()
    config = Settings(
        app_env="test",
        jwt_secret="ci-test-secret",
        telegram_bot_token=BOT_TOKEN,
        telegram_daily_balance_enabled=True,
    )
    fake = FakeTelegramClient()
    now = datetime(2026, 7, 19, 3, 0, tzinfo=timezone.utc)
    assert await send_due_daily_balances(db_session, config, now=now, client=fake) == (
        1,
        0,
    )
    assert await send_due_daily_balances(db_session, config, now=now, client=fake) == (
        0,
        0,
    )
    assert len(fake.messages) == 1
    assert "Общая стоимость: $0.00" in fake.messages[0][1]
    assert fake.messages[0][3] == "Открыть портфель"
    delivery = await db_session.scalar(select(TelegramDigestDelivery))
    assert delivery.status == "sent"


@pytest.mark.asyncio
async def test_daily_balance_emits_configured_complete_24h_threshold_alert(
    db_session,
):
    user = User(email=None, auth_hash=None)
    db_session.add(user)
    await db_session.flush()
    account = TelegramAccount(
        user_id=user.id,
        telegram_user_id=20005,
        first_name="Test",
        allows_write_to_pm=True,
    )
    db_session.add(account)
    await db_session.flush()
    db_session.add(
        TelegramNotificationSettings(
            telegram_account_id=account.id,
            enabled=True,
            timezone="UTC",
            daily_at=time(9, 0),
            language="en",
            alert_threshold_percent=Decimal("10"),
        )
    )
    now = datetime.now(timezone.utc).replace(hour=10, minute=0, second=0, microsecond=0)
    wallet = Wallet(
        user_id=user.id,
        label="Threshold wallet",
        address="0x0000000000000000000000000000000000000206",
        chain_type="mainnet",
        wallet_type="evm",
        is_active=True,
        address_updated_at=now - timedelta(days=3),
    )
    db_session.add(wallet)
    await db_session.flush()
    await _add_digest_snapshot(
        db_session,
        wallet,
        observed_at=now - timedelta(hours=24, minutes=1),
        total_usd=Decimal("100"),
        priced_asset=True,
    )
    await _add_digest_snapshot(
        db_session,
        wallet,
        observed_at=now - timedelta(minutes=1),
        total_usd=Decimal("120"),
        priced_asset=True,
    )
    await db_session.commit()
    config = Settings(
        app_env="test",
        jwt_secret="ci-test-secret",
        telegram_bot_token=BOT_TOKEN,
        telegram_daily_balance_enabled=True,
    )
    fake = FakeTelegramClient()

    result = await send_due_daily_balances(
        db_session,
        config,
        now=now,
        client=fake,
    )

    assert result == (1, 0)
    assert len(fake.messages) == 1
    assert "⚠️ 24h change: +20.00% (threshold 10.00%)" in fake.messages[0][1]


@pytest.mark.asyncio
async def test_daily_balance_uses_oldest_source_and_reports_stale_health(db_session):
    user = User(email=None, auth_hash=None)
    db_session.add(user)
    await db_session.flush()
    account = TelegramAccount(
        user_id=user.id,
        telegram_user_id=20004,
        first_name="Test",
        allows_write_to_pm=True,
    )
    db_session.add(account)
    await db_session.flush()
    db_session.add(
        TelegramNotificationSettings(
            telegram_account_id=account.id,
            enabled=True,
            timezone="UTC",
            daily_at=time(9, 0),
            language="en",
        )
    )
    wallets = [
        Wallet(
            user_id=user.id,
            label="Older source",
            address="0x0000000000000000000000000000000000000204",
            chain_type="mainnet",
            wallet_type="evm",
            is_active=True,
        ),
        Wallet(
            user_id=user.id,
            label="Fresh source",
            address="0x0000000000000000000000000000000000000205",
            chain_type="mainnet",
            wallet_type="evm",
            is_active=True,
        ),
    ]
    db_session.add_all(wallets)
    await db_session.flush()
    now = datetime(2026, 7, 19, 11, 0, tzinfo=timezone.utc)
    await _add_digest_snapshot(
        db_session,
        wallets[0],
        observed_at=now - timedelta(hours=2),
        total_usd=Decimal("10"),
    )
    await _add_digest_snapshot(
        db_session,
        wallets[1],
        observed_at=now - timedelta(minutes=5),
        total_usd=Decimal("20"),
    )
    await db_session.commit()
    config = Settings(
        app_env="test",
        jwt_secret="ci-test-secret",
        telegram_bot_token=BOT_TOKEN,
        telegram_daily_balance_enabled=True,
    )
    fake = FakeTelegramClient()
    labels = {"language": "en", "outcome": "sent", "health_state": "stale"}
    metric_before = TELEGRAM_DIGEST.labels(**labels)._value.get()

    result = await send_due_daily_balances(
        db_session,
        config,
        now=now,
        client=fake,
    )

    assert result == (1, 0)
    message = fake.messages[0][1]
    assert "Total value: $30.00" in message
    assert "As of: 2026-07-19 09:00 UTC" in message
    assert "Coverage: 2/2 wallets" in message
    assert "Data health: Stale" in message
    assert "2026-07-19 10:55 UTC" not in message
    assert TELEGRAM_DIGEST.labels(**labels)._value.get() == metric_before + 1


@pytest.mark.asyncio
async def test_daily_balance_sanitizes_errors_and_disables_only_forbidden(db_session):
    user = User(email=None, auth_hash=None)
    db_session.add(user)
    await db_session.flush()
    account = TelegramAccount(
        user_id=user.id,
        telegram_user_id=20002,
        first_name="Test",
        allows_write_to_pm=True,
    )
    db_session.add(account)
    await db_session.flush()
    notification = TelegramNotificationSettings(
        telegram_account_id=account.id,
        enabled=True,
        timezone="UTC",
        daily_at=time(9, 0),
        language="en",
    )
    db_session.add(notification)
    await db_session.commit()
    config = Settings(
        app_env="test",
        jwt_secret="ci-test-secret",
        telegram_bot_token=BOT_TOKEN,
        telegram_daily_balance_enabled=True,
    )
    failure = FailingTelegramClient(
        TelegramSendError("telegram_http_403", status_code=403)
    )

    result = await send_due_daily_balances(
        db_session,
        config,
        now=datetime(2026, 7, 19, 10, 0, tzinfo=timezone.utc),
        client=failure,
    )

    assert result == (0, 1)
    delivery = await db_session.scalar(select(TelegramDigestDelivery))
    assert delivery.error == "telegram_http_403"
    assert BOT_TOKEN not in delivery.error
    assert notification.enabled is False


@pytest.mark.asyncio
async def test_daily_balance_honors_retry_after_and_reclaims_stale_pending(db_session):
    user = User(email=None, auth_hash=None)
    db_session.add(user)
    await db_session.flush()
    account = TelegramAccount(
        user_id=user.id,
        telegram_user_id=20003,
        first_name="Test",
        allows_write_to_pm=True,
    )
    db_session.add(account)
    await db_session.flush()
    db_session.add(
        TelegramNotificationSettings(
            telegram_account_id=account.id,
            enabled=True,
            timezone="UTC",
            daily_at=time(9, 0),
            language="en",
        )
    )
    await db_session.commit()
    config = Settings(
        app_env="test",
        jwt_secret="ci-test-secret",
        telegram_bot_token=BOT_TOKEN,
        telegram_daily_balance_enabled=True,
    )
    now = datetime(2026, 7, 19, 10, 0, tzinfo=timezone.utc)
    throttled = FailingTelegramClient(
        TelegramSendError("telegram_http_429", status_code=429, retry_after_seconds=60)
    )
    assert await send_due_daily_balances(
        db_session, config, now=now, client=throttled
    ) == (0, 1)
    assert await send_due_daily_balances(
        db_session, config, now=now + timedelta(seconds=30), client=throttled
    ) == (0, 0)
    assert throttled.calls == 1

    delivery = await db_session.scalar(select(TelegramDigestDelivery))
    delivery.status = "pending"
    delivery.retry_after = None
    delivery.attempted_at = now - timedelta(minutes=11)
    await db_session.commit()
    recovered = FakeTelegramClient()
    assert await send_due_daily_balances(
        db_session, config, now=now, client=recovered
    ) == (1, 0)
    assert delivery.attempts == 2


def test_daily_balance_formatter_supports_both_languages():
    russian = format_daily_balance(
        Decimal("1234.5"),
        language="ru",
        as_of=None,
        health_state="partial",
        wallets_covered=1,
        wallets_total=2,
    )
    english = format_daily_balance(
        Decimal("1234.5"),
        language="en",
        as_of=None,
        health_state="partial",
        wallets_covered=1,
        wallets_total=2,
    )
    assert "Общая стоимость: $1,234.50" in russian
    assert "Покрытие: 1/2 кошельков" in russian
    assert "Состояние данных: Частичные" in russian
    assert "Total value: $1,234.50" in english
    assert "Coverage: 1/2 wallets" in english
    assert "Data health: Partial" in english

    below_threshold = format_daily_balance(
        Decimal("105"),
        language="en",
        as_of=None,
        change_24h=PortfolioValueChange24h(
            status="complete",
            start_usd=Decimal("100"),
            end_usd=Decimal("105"),
            absolute_usd=Decimal("5"),
            percent=5.0,
            reference_at=datetime(2026, 9, 11, tzinfo=timezone.utc),
            cutoff_at=datetime(2026, 9, 10, tzinfo=timezone.utc),
            reason_codes=[],
        ),
        alert_threshold_percent=10,
    )
    assert "24h change" not in below_threshold


def test_telegram_client_does_not_chain_token_bearing_network_error(monkeypatch):
    import requests

    config = Settings(
        app_env="test",
        jwt_secret="ci-test-secret",
        telegram_bot_token=BOT_TOKEN,
    )

    def fail_request(*args, **kwargs):
        raise requests.ConnectionError(f"failed /bot{BOT_TOKEN}/sendMessage")

    monkeypatch.setattr(requests, "post", fail_request)
    with pytest.raises(TelegramSendError) as caught:
        TelegramBotClient(config).send_daily_balance(
            123, "Balance", "https://pywallet.dev/telegram", "Open portfolio"
        )
    assert str(caught.value) == "telegram_network_error"
    assert caught.value.__cause__ is None
    assert BOT_TOKEN not in str(caught.value)
