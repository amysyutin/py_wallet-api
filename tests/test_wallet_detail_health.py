from datetime import datetime, timezone
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


async def _create_wallet_snapshot(
    db_session: AsyncSession,
    *,
    wallet: Wallet,
    failed_chain: str | None = None,
    price_source: str = "coingecko",
) -> SnapshotRun:
    now = datetime.now(timezone.utc)
    run = SnapshotRun(
        user_id=wallet.user_id,
        wallet_id=wallet.id,
        trigger_type="manual",
        scope_type="wallet",
        status="partial_success" if failed_chain else "success",
        finished_at=now,
    )
    db_session.add(run)
    await db_session.flush()
    wallet_snapshot = WalletSnapshot(
        snapshot_run_id=run.id,
        wallet_id=wallet.id,
        wallet_type="evm",
        status="partial_success" if failed_chain else "success",
        total_usd=Decimal("150"),
        finished_at=now,
    )
    db_session.add(wallet_snapshot)
    await db_session.flush()
    success_chain = ChainSnapshot(
        wallet_snapshot_id=wallet_snapshot.id,
        chain="mainnet",
        status="success",
        total_usd=Decimal("150"),
        finished_at=now,
    )
    db_session.add(success_chain)
    await db_session.flush()
    db_session.add(
        SnapshotBalanceSnapshot(
            chain_snapshot_id=success_chain.id,
            asset_symbol="ETH",
            amount=Decimal("1"),
            price_usd=Decimal("150"),
            value_usd=Decimal("150"),
            price_source=price_source,
        )
    )
    if failed_chain:
        db_session.add(
            ChainSnapshot(
                wallet_snapshot_id=wallet_snapshot.id,
                chain=failed_chain,
                status="failed",
                error_type="rpc_unavailable",
                total_usd=Decimal("0"),
                finished_at=now,
                error_message="https://provider.example/private-token",
            )
        )
    await db_session.flush()
    return run


async def test_wallet_detail_exposes_partial_health_without_provider_details(
    client: AsyncClient,
    auth_headers: dict,
    db_session: AsyncSession,
):
    wallet_body = (
        await client.post(
            "/wallets",
            headers=auth_headers,
            json={
                "label": "Health wallet",
                "address": "0x0000000000000000000000000000000000000a01",
                "chain_type": "all",
            },
        )
    ).json()
    wallet = await db_session.get(Wallet, wallet_body["id"])
    assert wallet is not None
    await _create_wallet_snapshot(
        db_session,
        wallet=wallet,
        failed_chain="arbitrum",
    )

    response = await client.get(
        f"/wallets/{wallet.id}/summary",
        headers=auth_headers,
    )

    assert response.status_code == 200
    body = response.json()
    assert body["data_health"] == {
        "state": "partial",
        "freshness": "fresh",
        "as_of": body["last_snapshot_at"],
        "source": "latest_snapshot",
        "refresh_in_progress": False,
        "chain_issues": [
            {
                "chain": "arbitrum",
                "status": "failed",
                "error_type": "rpc_unavailable",
            }
        ],
        "price_quality": {
            "state": "complete",
            "sources": ["coingecko"],
            "assets_priced": 1,
            "assets_total": 1,
        },
    }
    assert "provider.example" not in response.text
    assert "private-token" not in response.text


async def test_wallet_detail_marks_successful_saved_snapshot_as_updating(
    client: AsyncClient,
    auth_headers: dict,
    db_session: AsyncSession,
):
    wallet_body = (
        await client.post(
            "/wallets",
            headers=auth_headers,
            json={
                "label": "Updating wallet",
                "address": "0x0000000000000000000000000000000000000a02",
                "chain_type": "all",
            },
        )
    ).json()
    wallet = await db_session.get(Wallet, wallet_body["id"])
    assert wallet is not None
    await _create_wallet_snapshot(db_session, wallet=wallet)
    db_session.add(
        SnapshotRun(
            user_id=wallet.user_id,
            trigger_type="manual",
            scope_type="all",
            status="running",
        )
    )
    await db_session.flush()

    response = await client.get(
        f"/wallets/{wallet.id}/summary",
        headers=auth_headers,
    )

    assert response.status_code == 200
    health = response.json()["data_health"]
    assert health["state"] == "updating"
    assert health["refresh_in_progress"] is True
    assert health["source"] == "latest_snapshot"


async def test_wallet_detail_recognizes_frankfurter_price_source(
    client: AsyncClient,
    auth_headers: dict,
    db_session: AsyncSession,
):
    wallet_body = (
        await client.post(
            "/wallets",
            headers=auth_headers,
            json={
                "label": "Fiat wallet",
                "address": "0x0000000000000000000000000000000000000a03",
                "chain_type": "all",
            },
        )
    ).json()
    wallet = await db_session.get(Wallet, wallet_body["id"])
    assert wallet is not None
    await _create_wallet_snapshot(
        db_session,
        wallet=wallet,
        price_source="frankfurter",
    )

    response = await client.get(
        f"/wallets/{wallet.id}/summary",
        headers=auth_headers,
    )

    assert response.status_code == 200
    assert response.json()["data_health"]["price_quality"] == {
        "state": "complete",
        "sources": ["frankfurter"],
        "assets_priced": 1,
        "assets_total": 1,
    }
