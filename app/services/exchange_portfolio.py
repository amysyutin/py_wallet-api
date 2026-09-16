from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Literal

import requests
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.connectors.price.coingecko import get_coin_price_usd_cached
from app.core.config import Settings

ExchangePortfolioStatus = Literal[
    "not_configured",
    "success",
    "not_found",
    "timeout",
    "unavailable",
    "invalid_response",
]

EXCHANGE_COIN_IDS = {
    "BTC": "bitcoin",
    "ETH": "ethereum",
    "BNB": "binancecoin",
    "USDT": "tether",
    "USDC": "usd-coin",
    "DAI": "dai",
}


class _ExchangeBalancePayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asset: str = Field(min_length=1, max_length=32, pattern=r"^[A-Z0-9]+$")
    free: Decimal = Field(ge=0)
    locked: Decimal = Field(ge=0)
    total: Decimal = Field(gt=0)
    price_usd: Decimal | None = Field(default=None, gt=0)
    usd_value: Decimal | None = Field(default=None, ge=0)
    price_source: Literal["binance_usdt"] | None = None

    @field_validator("total")
    @classmethod
    def total_must_be_finite(cls, value: Decimal) -> Decimal:
        if not value.is_finite():
            raise ValueError("total must be finite")
        return value


class _ExchangeSnapshotPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: int = Field(gt=0)
    user_id: int = Field(gt=0)
    exchange: Literal["binance"]
    status: Literal["success"]
    created_at: datetime
    completed_at: datetime
    balances: list[_ExchangeBalancePayload] = Field(max_length=500)


class _ExchangeSnapshotHistoryPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    snapshots: list[_ExchangeSnapshotPayload] = Field(max_length=5000)


@dataclass(frozen=True)
class ExchangeAssetPosition:
    symbol: str
    amount: Decimal
    price_usd: Decimal | None
    usd_value: Decimal
    price_source: Literal["coingecko", "binance_usdt"] | None = None


@dataclass(frozen=True)
class ExchangePortfolioSnapshot:
    provider: Literal["binance"]
    status: ExchangePortfolioStatus
    as_of: datetime | None = None
    assets: tuple[ExchangeAssetPosition, ...] = ()
    error_type: str | None = None

    @property
    def configured(self) -> bool:
        return self.status != "not_configured"

    @property
    def total_usd(self) -> Decimal:
        return sum((asset.usd_value for asset in self.assets), Decimal("0"))


@dataclass(frozen=True)
class ExchangeHistoryPoint:
    as_of: datetime
    total_usd: Decimal


@dataclass(frozen=True)
class ExchangePortfolioHistory:
    status: ExchangePortfolioStatus
    points: tuple[ExchangeHistoryPoint, ...] = ()


def _failed(status: ExchangePortfolioStatus) -> ExchangePortfolioSnapshot:
    return ExchangePortfolioSnapshot(
        provider="binance",
        status=status,
        error_type=None if status == "not_configured" else status,
    )


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _payload_is_consistent(payload: _ExchangeSnapshotPayload, *, user_id: int) -> bool:
    if payload.user_id != user_id:
        return False
    if _aware(payload.completed_at) < _aware(payload.created_at):
        return False
    if len({balance.asset for balance in payload.balances}) != len(payload.balances):
        return False
    return all(
        balance.total == balance.free + balance.locked for balance in payload.balances
    )


def _stored_valuation(balance: _ExchangeBalancePayload) -> Decimal | None:
    fields = (balance.price_usd, balance.usd_value, balance.price_source)
    if all(value is None for value in fields):
        return None
    if any(value is None for value in fields):
        raise ValueError("partial stored valuation")
    assert balance.price_usd is not None
    assert balance.usd_value is not None
    if not balance.price_usd.is_finite() or not balance.usd_value.is_finite():
        raise ValueError("non-finite stored valuation")
    if balance.usd_value != balance.total * balance.price_usd:
        raise ValueError("inconsistent stored valuation")
    return balance.usd_value


def fetch_exchange_portfolio(
    settings: Settings,
    *,
    user_id: int,
) -> ExchangePortfolioSnapshot:
    """Load and value a user's latest CEX snapshot without exposing provider details."""
    if not settings.exchange_internal_api_token:
        return _failed("not_configured")

    try:
        response = requests.get(
            f"{settings.exchange_service_url.rstrip('/')}/internal/exchange-snapshots/latest",
            params={"user_id": user_id},
            headers={"X-Internal-Token": settings.exchange_internal_api_token},
            timeout=settings.exchange_service_timeout_seconds,
        )
    except requests.Timeout:
        return _failed("timeout")
    except requests.RequestException:
        return _failed("unavailable")

    if response.status_code == 404:
        return _failed("not_found")
    if response.status_code != 200:
        return _failed("unavailable")

    try:
        payload = _ExchangeSnapshotPayload.model_validate(response.json())
    except (ValueError, ValidationError):
        return _failed("invalid_response")
    if not _payload_is_consistent(payload, user_id=user_id):
        return _failed("invalid_response")
    completed_at = _aware(payload.completed_at)

    assets = []
    for balance in payload.balances:
        try:
            stored_value = _stored_valuation(balance)
        except ValueError:
            return _failed("invalid_response")
        price_usd = balance.price_usd
        price_source: Literal["coingecko", "binance_usdt"] | None = balance.price_source
        coin_id = EXCHANGE_COIN_IDS.get(balance.asset)
        if stored_value is None and coin_id:
            try:
                price_usd = Decimal(str(get_coin_price_usd_cached(coin_id)))
                if not price_usd.is_finite() or price_usd <= 0:
                    price_usd = None
                else:
                    price_source = "coingecko"
            except Exception:
                price_usd = None
                price_source = None
        assets.append(
            ExchangeAssetPosition(
                symbol=balance.asset,
                amount=balance.total,
                price_usd=price_usd,
                usd_value=(
                    stored_value
                    if stored_value is not None
                    else (
                        balance.total * price_usd
                        if price_usd is not None
                        else Decimal("0")
                    )
                ),
                price_source=price_source,
            )
        )

    return ExchangePortfolioSnapshot(
        provider=payload.exchange,
        status="success",
        as_of=completed_at,
        assets=tuple(assets),
    )


def fetch_exchange_history(
    settings: Settings,
    *,
    user_id: int,
    since: datetime,
) -> ExchangePortfolioHistory:
    """Load complete stored CEX valuations without repricing old balances."""
    if not settings.exchange_internal_api_token:
        return ExchangePortfolioHistory(status="not_configured")

    try:
        response = requests.get(
            f"{settings.exchange_service_url.rstrip('/')}/internal/exchange-snapshots/history",
            params={
                "user_id": str(user_id),
                "since": _aware(since).isoformat(),
                "limit": "5000",
            },
            headers={"X-Internal-Token": settings.exchange_internal_api_token},
            timeout=settings.exchange_service_timeout_seconds,
        )
    except requests.Timeout:
        return ExchangePortfolioHistory(status="timeout")
    except requests.RequestException:
        return ExchangePortfolioHistory(status="unavailable")

    if response.status_code == 404:
        return ExchangePortfolioHistory(status="not_found")
    if response.status_code != 200:
        return ExchangePortfolioHistory(status="unavailable")

    try:
        payload = _ExchangeSnapshotHistoryPayload.model_validate(response.json())
    except (ValueError, ValidationError):
        return ExchangePortfolioHistory(status="invalid_response")

    points: list[ExchangeHistoryPoint] = []
    previous_at: datetime | None = None
    for snapshot in payload.snapshots:
        if not _payload_is_consistent(snapshot, user_id=user_id):
            return ExchangePortfolioHistory(status="invalid_response")
        completed_at = _aware(snapshot.completed_at)
        if previous_at is not None and completed_at < previous_at:
            return ExchangePortfolioHistory(status="invalid_response")
        previous_at = completed_at
        try:
            values = [_stored_valuation(balance) for balance in snapshot.balances]
        except ValueError:
            return ExchangePortfolioHistory(status="invalid_response")
        if any(value is None for value in values):
            continue
        points.append(
            ExchangeHistoryPoint(
                as_of=completed_at,
                total_usd=sum(
                    (value for value in values if value is not None),
                    Decimal("0"),
                ),
            )
        )
    return ExchangePortfolioHistory(status="success", points=tuple(points))
