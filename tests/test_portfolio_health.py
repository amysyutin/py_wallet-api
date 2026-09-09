from datetime import datetime, timedelta, timezone
from decimal import Decimal

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.snapshot_service import (
    ChainSnapshot,
    SnapshotBalanceSnapshot,
    SnapshotRun,
    WalletSnapshot,
)
from app.db.models.wallet import Wallet


async def _create_evm_wallet(client: AsyncClient, headers: dict, suffix: int) -> int:
    response = await client.post(
        "/wallets",
        headers=headers,
        json={
            "label": f"Health wallet {suffix}",
            "address": f"0x{suffix:040x}",
            "chain_type": "mainnet",
        },
    )
    assert response.status_code == 201
    return response.json()["id"]


async def _add_wallet_snapshot(
    session: AsyncSession,
    wallet: Wallet,
    *,
    finished_at: datetime,
    wallet_status: str = "success",
    chain_status: str = "success",
    error_type: str | None = None,
    error_message: str | None = None,
    asset_amount: Decimal | None = None,
    price_usd: Decimal | None = None,
    price_source: str | None = None,
) -> int:
    run = SnapshotRun(
        user_id=wallet.user_id,
        wallet_id=wallet.id,
        trigger_type="manual",
        scope_type="wallet",
        status=wallet_status,
        finished_at=finished_at,
    )
    session.add(run)
    await session.flush()
    wallet_snapshot = WalletSnapshot(
        snapshot_run_id=run.id,
        wallet_id=wallet.id,
        wallet_type=wallet.wallet_type,
        status=wallet_status,
        total_usd=Decimal("42"),
    )
    session.add(wallet_snapshot)
    await session.flush()
    chain_snapshot = ChainSnapshot(
        wallet_snapshot_id=wallet_snapshot.id,
        chain="base",
        status=chain_status,
        total_usd=Decimal("42"),
        error_type=error_type,
        error_message=error_message,
    )
    session.add(chain_snapshot)
    await session.flush()
    if asset_amount is not None:
        session.add(
            SnapshotBalanceSnapshot(
                chain_snapshot_id=chain_snapshot.id,
                asset_symbol="ETH",
                amount=asset_amount,
                price_usd=price_usd,
                value_usd=asset_amount * (price_usd or Decimal("0")),
                price_source=price_source,
            )
        )
        await session.flush()
    return run.id


async def test_portfolio_health_exposes_partial_coverage_without_provider_details(
    client: AsyncClient,
    auth_headers: dict,
    db_session: AsyncSession,
):
    wallet_id = await _create_evm_wallet(client, auth_headers, 101)
    wallet = await db_session.get(Wallet, wallet_id)
    assert wallet is not None
    run_id = await _add_wallet_snapshot(
        db_session,
        wallet,
        finished_at=datetime.now(timezone.utc) - timedelta(minutes=2),
        wallet_status="partial_success",
        chain_status="failed",
        error_type="rpc_unavailable",
        error_message="https://secret-token@example.invalid failed",
    )

    response = await client.get("/portfolio/summary", headers=auth_headers)

    assert response.status_code == 200
    health = response.json()["data_health"]
    assert health["state"] == "partial"
    assert health["freshness"] == "fresh"
    assert health["wallets_covered"] == 1
    assert health["wallets_total"] == 1
    assert health["snapshot_wallets"] == 1
    assert health["manual_wallets"] == 0
    assert health["missing_wallets"] == 0
    assert health["retryable_job_id"] == run_id
    assert health["chain_issues"] == [
        {
            "chain": "base",
            "status": "failed",
            "error_type": "rpc_unavailable",
            "wallets_count": 1,
        }
    ]
    assert "secret-token" not in response.text


async def test_portfolio_health_does_not_offer_one_retry_for_multiple_parent_jobs(
    client: AsyncClient,
    auth_headers: dict,
    db_session: AsyncSession,
):
    first_wallet_id = await _create_evm_wallet(client, auth_headers, 111)
    second_wallet_id = await _create_evm_wallet(client, auth_headers, 112)
    first_wallet = await db_session.get(Wallet, first_wallet_id)
    second_wallet = await db_session.get(Wallet, second_wallet_id)
    assert first_wallet is not None
    assert second_wallet is not None
    observed_at = datetime.now(timezone.utc) - timedelta(minutes=2)
    await _add_wallet_snapshot(
        db_session,
        first_wallet,
        finished_at=observed_at,
        wallet_status="partial_success",
        chain_status="failed",
        error_type="timeout",
    )
    await _add_wallet_snapshot(
        db_session,
        second_wallet,
        finished_at=observed_at,
        wallet_status="partial_success",
        chain_status="failed",
        error_type="connection_error",
    )

    response = await client.get("/portfolio/summary", headers=auth_headers)

    assert response.status_code == 200
    health = response.json()["data_health"]
    assert health["state"] == "partial"
    assert health["retryable_job_id"] is None
    assert health["chain_issues"] == [
        {
            "chain": "base",
            "status": "failed",
            "error_type": "multiple_errors",
            "wallets_count": 2,
        }
    ]


async def test_portfolio_health_uses_oldest_source_for_staleness(
    client: AsyncClient,
    auth_headers: dict,
    db_session: AsyncSession,
):
    old_wallet_id = await _create_evm_wallet(client, auth_headers, 102)
    fresh_wallet_id = await _create_evm_wallet(client, auth_headers, 103)
    old_wallet = await db_session.get(Wallet, old_wallet_id)
    fresh_wallet = await db_session.get(Wallet, fresh_wallet_id)
    assert old_wallet is not None
    assert fresh_wallet is not None
    now = datetime.now(timezone.utc)
    await _add_wallet_snapshot(
        db_session,
        old_wallet,
        finished_at=now - timedelta(minutes=31),
    )
    await _add_wallet_snapshot(
        db_session,
        fresh_wallet,
        finished_at=now - timedelta(minutes=1),
    )

    response = await client.get("/portfolio/summary", headers=auth_headers)

    assert response.status_code == 200
    body = response.json()
    health = body["data_health"]
    assert health["state"] == "stale"
    assert health["freshness"] == "stale"
    assert health["wallets_covered"] == 2
    assert health["wallets_total"] == 2
    assert health["as_of"] < body["last_snapshot_at"]


async def test_portfolio_health_marks_first_snapshot_as_updating(
    client: AsyncClient,
    auth_headers: dict,
    db_session: AsyncSession,
):
    wallet_id = await _create_evm_wallet(client, auth_headers, 104)
    wallet = await db_session.get(Wallet, wallet_id)
    assert wallet is not None
    db_session.add(
        SnapshotRun(
            user_id=wallet.user_id,
            wallet_id=wallet.id,
            trigger_type="auto",
            scope_type="wallet",
            status="running",
        )
    )
    await db_session.flush()

    response = await client.get("/portfolio/summary", headers=auth_headers)

    assert response.status_code == 200
    health = response.json()["data_health"]
    assert health["state"] == "updating"
    assert health["freshness"] == "unknown"
    assert health["wallets_covered"] == 0
    assert health["wallets_total"] == 1
    assert health["missing_wallets"] == 1
    assert health["refresh_in_progress"] is True


async def test_portfolio_health_counts_manual_sources(
    client: AsyncClient,
    auth_headers: dict,
):
    wallet_response = await client.post(
        "/wallets",
        headers=auth_headers,
        json={
            "label": "Manual source",
            "wallet_type": "manual",
            "chain_type": "manual",
        },
    )
    wallet_id = wallet_response.json()["id"]
    balance_response = await client.put(
        f"/wallets/{wallet_id}/balances",
        headers=auth_headers,
        json={
            "balances": [
                {"symbol": "BTC", "amount": "0.1", "price_usd": "50000"},
            ]
        },
    )
    assert balance_response.status_code == 200

    response = await client.get("/portfolio/summary", headers=auth_headers)

    assert response.status_code == 200
    health = response.json()["data_health"]
    assert health["state"] == "fresh"
    assert health["freshness"] == "unknown"
    assert health["wallets_covered"] == 1
    assert health["manual_wallets"] == 1
    assert health["snapshot_wallets"] == 0
    assert health["price_quality"] == {
        "state": "complete",
        "sources": ["manual"],
        "assets_priced": 1,
        "assets_total": 1,
    }


async def test_portfolio_health_marks_static_dev_price_as_estimated(
    client: AsyncClient,
    auth_headers: dict,
    db_session: AsyncSession,
):
    wallet_id = await _create_evm_wallet(client, auth_headers, 105)
    wallet = await db_session.get(Wallet, wallet_id)
    assert wallet is not None
    await _add_wallet_snapshot(
        db_session,
        wallet,
        finished_at=datetime.now(timezone.utc) - timedelta(minutes=2),
        asset_amount=Decimal("1"),
        price_usd=Decimal("42"),
        price_source="static_dev",
    )

    response = await client.get("/portfolio/summary", headers=auth_headers)

    assert response.status_code == 200
    health = response.json()["data_health"]
    assert health["state"] == "partial"
    assert health["price_quality"] == {
        "state": "estimated",
        "sources": ["static_dev"],
        "assets_priced": 1,
        "assets_total": 1,
    }


async def test_portfolio_health_recognizes_frankfurter_fiat_price(
    client: AsyncClient,
    auth_headers: dict,
    db_session: AsyncSession,
):
    wallet_id = await _create_evm_wallet(client, auth_headers, 107)
    wallet = await db_session.get(Wallet, wallet_id)
    assert wallet is not None
    await _add_wallet_snapshot(
        db_session,
        wallet,
        finished_at=datetime.now(timezone.utc) - timedelta(minutes=2),
        asset_amount=Decimal("100"),
        price_usd=Decimal("1.1641"),
        price_source="frankfurter",
    )

    response = await client.get("/portfolio/summary", headers=auth_headers)

    assert response.status_code == 200
    health = response.json()["data_health"]
    assert health["state"] == "fresh"
    assert health["price_quality"] == {
        "state": "complete",
        "sources": ["frankfurter"],
        "assets_priced": 1,
        "assets_total": 1,
    }


async def test_portfolio_health_marks_missing_market_price_as_incomplete(
    client: AsyncClient,
    auth_headers: dict,
    db_session: AsyncSession,
):
    wallet_id = await _create_evm_wallet(client, auth_headers, 106)
    wallet = await db_session.get(Wallet, wallet_id)
    assert wallet is not None
    await _add_wallet_snapshot(
        db_session,
        wallet,
        finished_at=datetime.now(timezone.utc) - timedelta(minutes=2),
        asset_amount=Decimal("2"),
    )

    response = await client.get("/portfolio/summary", headers=auth_headers)

    assert response.status_code == 200
    health = response.json()["data_health"]
    assert health["state"] == "partial"
    assert health["price_quality"] == {
        "state": "incomplete",
        "sources": ["unknown"],
        "assets_priced": 0,
        "assets_total": 1,
    }
