from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest
import requests
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.db.models.snapshot_service import SnapshotRun, WalletSnapshot
from app.db.models.wallet import Wallet
from app.services.exchange_portfolio import (
    ExchangeAssetPosition,
    ExchangeHistoryPoint,
    ExchangePortfolioHistory,
    ExchangePortfolioSnapshot,
    fetch_exchange_history,
    fetch_exchange_portfolio,
)


def _settings(**overrides) -> Settings:
    return Settings(
        _env_file=None,
        app_env="test",
        exchange_internal_api_token="exchange-token",
        **overrides,
    )


def _payload(*, user_id: int = 7, balances: list[dict] | None = None) -> dict:
    return {
        "id": 11,
        "user_id": user_id,
        "exchange": "binance",
        "status": "success",
        "created_at": "2026-08-15T06:00:00Z",
        "completed_at": "2026-08-15T06:00:02Z",
        "balances": balances
        or [
            {"asset": "BTC", "free": "0.5", "locked": "0.25", "total": "0.75"},
            {"asset": "NEW", "free": "2", "locked": "0", "total": "2"},
        ],
    }


@patch("app.services.exchange_portfolio.get_coin_price_usd_cached")
@patch("app.services.exchange_portfolio.requests.get")
def test_fetch_exchange_portfolio_is_user_scoped_and_values_known_assets(
    mock_get,
    mock_price,
):
    response = MagicMock(status_code=200)
    response.json.return_value = _payload()
    mock_get.return_value = response
    mock_price.return_value = 100000.0

    result = fetch_exchange_portfolio(_settings(), user_id=7)

    assert result.status == "success"
    assert result.total_usd == Decimal("75000.000")
    assert [(asset.symbol, asset.price_usd) for asset in result.assets] == [
        ("BTC", Decimal("100000.0")),
        ("NEW", None),
    ]
    mock_get.assert_called_once_with(
        "http://localhost:8002/internal/exchange-snapshots/latest",
        params={"user_id": 7},
        headers={"X-Internal-Token": "exchange-token"},
        timeout=5.0,
    )
    mock_price.assert_called_once_with("bitcoin")


@patch("app.services.exchange_portfolio.get_coin_price_usd_cached")
@patch("app.services.exchange_portfolio.requests.get")
def test_fetch_exchange_portfolio_prefers_timestamped_stored_valuation(
    mock_get,
    mock_price,
):
    response = MagicMock(status_code=200)
    response.json.return_value = _payload(
        balances=[
            {
                "asset": "BTC",
                "free": "0.5",
                "locked": "0",
                "total": "0.5",
                "price_usd": "60000",
                "usd_value": "30000",
                "price_source": "binance_usdt",
            }
        ]
    )
    mock_get.return_value = response

    result = fetch_exchange_portfolio(_settings(), user_id=7)

    assert result.status == "success"
    assert result.total_usd == Decimal("30000")
    mock_price.assert_not_called()


def test_fetch_exchange_history_keeps_only_complete_timestamped_values():
    response = MagicMock(status_code=200)
    response.json.return_value = {
        "snapshots": [
            _payload(),
            _payload(
                balances=[
                    {
                        "asset": "BTC",
                        "free": "0.5",
                        "locked": "0",
                        "total": "0.5",
                        "price_usd": "60000",
                        "usd_value": "30000",
                        "price_source": "binance_usdt",
                    },
                    {
                        "asset": "USDT",
                        "free": "125",
                        "locked": "0",
                        "total": "125",
                        "price_usd": "1",
                        "usd_value": "125",
                        "price_source": "binance_usdt",
                    },
                ]
            ),
        ]
    }
    since = datetime(2026, 8, 14, tzinfo=timezone.utc)
    with patch(
        "app.services.exchange_portfolio.requests.get",
        return_value=response,
    ) as mock_get:
        result = fetch_exchange_history(_settings(), user_id=7, since=since)

    assert result.status == "success"
    assert result.points == (
        ExchangeHistoryPoint(
            as_of=datetime(2026, 8, 15, 6, 0, 2, tzinfo=timezone.utc),
            total_usd=Decimal("30125"),
        ),
    )
    mock_get.assert_called_once_with(
        "http://localhost:8002/internal/exchange-snapshots/history",
        params={"user_id": "7", "since": since.isoformat(), "limit": "5000"},
        headers={"X-Internal-Token": "exchange-token"},
        timeout=5.0,
    )


def test_fetch_exchange_portfolio_does_not_call_service_without_token():
    with patch("app.services.exchange_portfolio.requests.get") as mock_get:
        result = fetch_exchange_portfolio(
            Settings(_env_file=None, app_env="test"),
            user_id=7,
        )

    assert result.status == "not_configured"
    assert result.configured is False
    mock_get.assert_not_called()


@pytest.mark.parametrize(
    ("effect", "status_code", "expected"),
    [
        (requests.Timeout("secret provider detail"), None, "timeout"),
        (requests.ConnectionError("secret provider detail"), None, "unavailable"),
        (None, 404, "not_found"),
        (None, 503, "unavailable"),
    ],
)
def test_fetch_exchange_portfolio_returns_bounded_failures(
    effect,
    status_code,
    expected,
):
    with patch("app.services.exchange_portfolio.requests.get") as mock_get:
        if effect is not None:
            mock_get.side_effect = effect
        else:
            mock_get.return_value = MagicMock(status_code=status_code)

        result = fetch_exchange_portfolio(_settings(), user_id=7)

    assert result.status == expected
    assert result.error_type == expected
    assert "secret" not in repr(result)


@pytest.mark.parametrize(
    "payload",
    [
        _payload(user_id=8),
        _payload(
            balances=[
                {"asset": "BTC", "free": "1", "locked": "0", "total": "1"},
                {"asset": "BTC", "free": "1", "locked": "0", "total": "1"},
            ]
        ),
        _payload(
            balances=[
                {"asset": "BTC", "free": "1", "locked": "1", "total": "1"},
            ]
        ),
    ],
)
def test_fetch_exchange_portfolio_rejects_untrusted_contract_data(payload):
    response = MagicMock(status_code=200)
    response.json.return_value = payload
    with patch("app.services.exchange_portfolio.requests.get", return_value=response):
        result = fetch_exchange_portfolio(_settings(), user_id=7)

    assert result.status == "invalid_response"


def test_fetch_exchange_portfolio_treats_invalid_price_as_unpriced():
    response = MagicMock(status_code=200)
    response.json.return_value = _payload(
        balances=[
            {"asset": "BTC", "free": "1", "locked": "0", "total": "1"},
        ]
    )
    with (
        patch("app.services.exchange_portfolio.requests.get", return_value=response),
        patch(
            "app.services.exchange_portfolio.get_coin_price_usd_cached",
            return_value=float("inf"),
        ),
    ):
        result = fetch_exchange_portfolio(_settings(), user_id=7)

    assert result.status == "success"
    assert result.assets[0].price_usd is None
    assert result.total_usd == Decimal("0")


def _exchange_snapshot(
    *,
    status="success",
    as_of=None,
    assets=(),
    error_type=None,
) -> ExchangePortfolioSnapshot:
    return ExchangePortfolioSnapshot(
        provider="binance",
        status=status,
        as_of=as_of,
        assets=assets,
        error_type=error_type,
    )


async def test_summary_includes_exchange_source_health_and_value(
    client: AsyncClient,
    auth_headers: dict,
):
    snapshot = _exchange_snapshot(
        as_of=datetime.now(timezone.utc) - timedelta(minutes=1),
        assets=(
            ExchangeAssetPosition(
                "BTC",
                Decimal("0.01"),
                Decimal("100000"),
                Decimal("1000"),
                "coingecko",
            ),
            ExchangeAssetPosition(
                "USDT", Decimal("50"), Decimal("1"), Decimal("50"), "coingecko"
            ),
        ),
    )
    with patch("app.routers.portfolio.fetch_exchange_portfolio", return_value=snapshot):
        response = await client.get("/portfolio/summary", headers=auth_headers)

    assert response.status_code == 200
    body = response.json()
    assert Decimal(body["total_usd"]) == Decimal("1050")
    assert body["top_assets"][0]["symbol"] == "BTC"
    assert body["sources"][0]["status"] == "unavailable"
    assert body["sources"][1] == {
        "source": "exchange",
        "provider": "binance",
        "status": "fresh",
        "total_usd": "1050",
        "assets_count": 2,
        "as_of": snapshot.as_of.isoformat().replace("+00:00", "Z"),
    }
    assert body["data_health"]["exchange"]["source"] == "exchange"
    assert body["data_health"]["exchange"]["assets_priced"] == 2
    assert body["data_health"]["price_quality"]["sources"] == ["coingecko"]
    assert body["change_24h"]["reason_codes"] == [
        "current_source_has_no_historical_counterpart"
    ]


async def test_summary_marks_unknown_exchange_price_as_partial(
    client: AsyncClient,
    auth_headers: dict,
):
    snapshot = _exchange_snapshot(
        as_of=datetime.now(timezone.utc) - timedelta(minutes=1),
        assets=(ExchangeAssetPosition("NEW", Decimal("2"), None, Decimal("0")),),
    )
    with patch("app.routers.portfolio.fetch_exchange_portfolio", return_value=snapshot):
        response = await client.get("/portfolio/summary", headers=auth_headers)

    health = response.json()["data_health"]
    assert health["state"] == "partial"
    assert health["exchange"]["state"] == "partial"
    assert health["price_quality"] == {
        "state": "incomplete",
        "sources": ["unknown"],
        "assets_priced": 0,
        "assets_total": 1,
    }


async def test_summary_uses_exchange_snapshot_for_staleness(
    client: AsyncClient,
    auth_headers: dict,
):
    snapshot = _exchange_snapshot(
        as_of=datetime.now(timezone.utc) - timedelta(minutes=31),
        assets=(
            ExchangeAssetPosition(
                "USDT", Decimal("25"), Decimal("1"), Decimal("25"), "coingecko"
            ),
        ),
    )
    with patch("app.routers.portfolio.fetch_exchange_portfolio", return_value=snapshot):
        response = await client.get("/portfolio/summary", headers=auth_headers)

    health = response.json()["data_health"]
    assert health["state"] == "stale"
    assert health["freshness"] == "stale"
    assert health["exchange"]["state"] == "stale"
    assert response.json()["sources"][1]["status"] == "stale"


async def test_summary_keeps_wallet_contract_available_when_exchange_is_down(
    client: AsyncClient,
    auth_headers: dict,
):
    snapshot = _exchange_snapshot(status="timeout", error_type="timeout")
    with patch("app.routers.portfolio.fetch_exchange_portfolio", return_value=snapshot):
        response = await client.get("/portfolio/summary", headers=auth_headers)

    assert response.status_code == 200
    body = response.json()
    assert Decimal(body["total_usd"]) == Decimal("0")
    assert body["data_health"]["state"] == "partial"
    assert body["data_health"]["exchange"]["error_type"] == "timeout"
    assert body["sources"][1]["status"] == "unavailable"


async def test_allocation_includes_exchange_only_in_all_scope(
    client: AsyncClient,
    auth_headers: dict,
):
    snapshot = _exchange_snapshot(
        as_of=datetime.now(timezone.utc),
        assets=(
            ExchangeAssetPosition(
                "BTC",
                Decimal("0.01"),
                Decimal("100000"),
                Decimal("1000"),
                "coingecko",
            ),
        ),
    )
    with patch("app.routers.portfolio.fetch_exchange_portfolio", return_value=snapshot):
        response = await client.get("/portfolio/allocation", headers=auth_headers)

    assert response.status_code == 200
    body = response.json()
    assert Decimal(body["total_usd"]) == Decimal("1000")
    assert body["items"][0]["asset_key"] == "exchange:binance:BTC"
    assert body["data_quality"]["sources"] == ["coingecko"]


async def test_history_combines_manual_and_timestamped_cex_source_totals(
    client: AsyncClient,
    auth_headers: dict,
    db_session: AsyncSession,
):
    created = await client.post(
        "/wallets",
        headers=auth_headers,
        json={
            "label": "Cash",
            "wallet_type": "manual",
            "chain_type": "manual",
        },
    )
    wallet = await db_session.get(Wallet, created.json()["id"])
    assert wallet is not None
    now = datetime.now(timezone.utc)
    run = SnapshotRun(
        user_id=wallet.user_id,
        wallet_id=wallet.id,
        trigger_type="manual",
        scope_type="wallet",
        status="success",
        finished_at=now - timedelta(minutes=2),
    )
    db_session.add(run)
    await db_session.flush()
    db_session.add(
        WalletSnapshot(
            snapshot_run_id=run.id,
            wallet_id=wallet.id,
            wallet_type="manual",
            status="success",
            total_usd=Decimal("25"),
        )
    )
    await db_session.flush()
    exchange = ExchangePortfolioHistory(
        status="success",
        points=(
            ExchangeHistoryPoint(
                as_of=now - timedelta(minutes=1),
                total_usd=Decimal("100"),
            ),
        ),
    )

    with patch("app.routers.portfolio.fetch_exchange_history", return_value=exchange):
        response = await client.get("/portfolio/history?days=30", headers=auth_headers)

    assert response.status_code == 200
    points = response.json()["points"]
    assert Decimal(points[0]["sources"]["onchain_usd"]) == Decimal("0")
    assert points[0]["sources"]["cex_usd"] is None
    assert Decimal(points[0]["sources"]["manual_usd"]) == Decimal("25")
    assert Decimal(points[1]["sources"]["onchain_usd"]) == Decimal("0")
    assert Decimal(points[1]["sources"]["cex_usd"]) == Decimal("100")
    assert Decimal(points[1]["sources"]["manual_usd"]) == Decimal("25")
    assert Decimal(points[1]["total_usd"]) == Decimal("125")
