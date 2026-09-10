"""Solana wallet validation and portfolio read-model coverage."""

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from httpx import AsyncClient
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.snapshot_service import (
    ChainSnapshot,
    SnapshotBalanceSnapshot,
    SnapshotRun,
    WalletSnapshot,
)
from app.db.models.wallet import Wallet

SOLANA_ADDRESS = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
SOLANA_CASE_VARIANT = "EpjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
SOLANA_OTHER_ADDRESS = "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"


async def _create_solana_wallet(
    client: AsyncClient,
    auth_headers: dict,
    *,
    address: str = SOLANA_ADDRESS,
    label: str = "Solana",
):
    return await client.post(
        "/wallets",
        headers=auth_headers,
        json={
            "label": label,
            "wallet_type": "solana",
            "chain_type": "solana",
            "address": address,
        },
    )


async def test_create_and_filter_solana_wallet(
    client: AsyncClient,
    auth_headers: dict,
):
    response = await _create_solana_wallet(
        client,
        auth_headers,
        address=f"  {SOLANA_ADDRESS}  ",
    )

    assert response.status_code == 201
    wallet = response.json()
    assert wallet["wallet_type"] == "solana"
    assert wallet["chain_type"] == "solana"
    assert wallet["address"] == SOLANA_ADDRESS

    filtered = await client.get(
        "/wallets?wallet_type=solana&chain_type=solana",
        headers=auth_headers,
    )
    assert filtered.status_code == 200
    assert [item["id"] for item in filtered.json()] == [wallet["id"]]


@pytest.mark.parametrize(
    "payload",
    [
        {
            "label": "Missing address",
            "wallet_type": "solana",
            "chain_type": "solana",
        },
        {
            "label": "Wrong alphabet",
            "wallet_type": "solana",
            "chain_type": "solana",
            "address": "0" * 32,
        },
        {
            "label": "Wrong decoded length",
            "wallet_type": "solana",
            "chain_type": "solana",
            "address": "1" * 31,
        },
        {
            "label": "Wrong chain",
            "wallet_type": "solana",
            "chain_type": "all",
            "address": SOLANA_ADDRESS,
        },
    ],
)
async def test_create_rejects_invalid_solana_wallet_state(
    client: AsyncClient,
    auth_headers: dict,
    payload: dict,
):
    response = await client.post("/wallets", headers=auth_headers, json=payload)

    assert response.status_code == 422


async def test_patch_solana_wallet_validates_address_and_chain(
    client: AsyncClient,
    auth_headers: dict,
    db_session: AsyncSession,
):
    created = await _create_solana_wallet(client, auth_headers)
    assert created.status_code == 201
    wallet_id = created.json()["id"]
    wallet = await db_session.get(Wallet, wallet_id)
    assert wallet is not None
    previous_revision = wallet.address_updated_at

    updated = await client.patch(
        f"/wallets/{wallet_id}",
        headers=auth_headers,
        json={"address": f" {SOLANA_OTHER_ADDRESS} "},
    )

    assert updated.status_code == 200
    assert updated.json()["address"] == SOLANA_OTHER_ADDRESS
    await db_session.refresh(wallet)
    assert wallet.address_updated_at >= previous_revision

    invalid_address = await client.patch(
        f"/wallets/{wallet_id}",
        headers=auth_headers,
        json={"address": "not-a-solana-public-key"},
    )
    invalid_chain = await client.patch(
        f"/wallets/{wallet_id}",
        headers=auth_headers,
        json={"chain_type": "mainnet"},
    )
    cleared = await client.patch(
        f"/wallets/{wallet_id}",
        headers=auth_headers,
        json={"address": None},
    )

    assert invalid_address.status_code == 422
    assert invalid_chain.status_code == 422
    assert cleared.status_code == 422


async def test_solana_duplicates_are_exact_and_case_sensitive(
    client: AsyncClient,
    auth_headers: dict,
):
    first = await _create_solana_wallet(client, auth_headers)
    exact_duplicate = await _create_solana_wallet(
        client,
        auth_headers,
        label="Exact duplicate",
    )
    case_variant = await _create_solana_wallet(
        client,
        auth_headers,
        address=SOLANA_CASE_VARIANT,
        label="Case variant",
    )

    assert first.status_code == 201
    assert exact_duplicate.status_code == 409
    assert "Solana address already exists" in exact_duplicate.json()["detail"]
    assert case_variant.status_code == 201
    assert case_variant.json()["address"] == SOLANA_CASE_VARIANT


async def test_database_rejects_active_solana_duplicate(
    client: AsyncClient,
    auth_headers: dict,
    db_session: AsyncSession,
):
    created = await _create_solana_wallet(client, auth_headers)
    assert created.status_code == 201
    canonical = await db_session.get(Wallet, created.json()["id"])
    assert canonical is not None

    duplicate = Wallet(
        user_id=canonical.user_id,
        label="Concurrent duplicate",
        address=SOLANA_ADDRESS,
        chain_type="solana",
        wallet_type="solana",
        is_active=True,
    )
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(duplicate)
            await db_session.flush()


async def test_solana_snapshot_is_visible_in_wallet_and_portfolio_views(
    client: AsyncClient,
    auth_headers: dict,
    db_session: AsyncSession,
):
    created = await _create_solana_wallet(client, auth_headers)
    assert created.status_code == 201
    wallet = await db_session.get(Wallet, created.json()["id"])
    assert wallet is not None

    observed_at = datetime.now(timezone.utc) + timedelta(seconds=1)
    run = SnapshotRun(
        user_id=wallet.user_id,
        wallet_id=wallet.id,
        trigger_type="manual",
        scope_type="wallet",
        status="success",
        created_at=observed_at,
        finished_at=observed_at,
    )
    db_session.add(run)
    await db_session.flush()
    wallet_snapshot = WalletSnapshot(
        snapshot_run_id=run.id,
        wallet_id=wallet.id,
        wallet_type="solana",
        status="success",
        total_usd=Decimal("205.5"),
        started_at=observed_at,
        finished_at=observed_at,
    )
    db_session.add(wallet_snapshot)
    await db_session.flush()
    chain_snapshot = ChainSnapshot(
        wallet_snapshot_id=wallet_snapshot.id,
        chain="solana",
        status="success",
        native_balance=Decimal("2"),
        total_usd=Decimal("205.5"),
        started_at=observed_at,
        finished_at=observed_at,
    )
    db_session.add(chain_snapshot)
    await db_session.flush()
    db_session.add_all(
        [
            SnapshotBalanceSnapshot(
                chain_snapshot_id=chain_snapshot.id,
                asset_symbol="SOL",
                asset_type="native",
                amount=Decimal("2"),
                price_usd=Decimal("100"),
                value_usd=Decimal("200"),
                price_source="coingecko",
            ),
            SnapshotBalanceSnapshot(
                chain_snapshot_id=chain_snapshot.id,
                asset_symbol="USDC",
                asset_address=SOLANA_ADDRESS,
                asset_type="spl",
                amount=Decimal("5.5"),
                price_usd=Decimal("1"),
                value_usd=Decimal("5.5"),
                price_source="coingecko",
            ),
        ]
    )
    await db_session.flush()

    wallet_list = await client.get("/wallets", headers=auth_headers)
    wallet_detail = await client.get(
        f"/wallets/{wallet.id}/summary",
        headers=auth_headers,
    )
    portfolio = await client.get("/portfolio/summary", headers=auth_headers)
    allocation = await client.get("/portfolio/allocation", headers=auth_headers)

    assert wallet_list.status_code == 200
    list_item = wallet_list.json()[0]
    assert list_item["wallet_type"] == "solana"
    assert list_item["balance_source"] == "latest_snapshot"
    assert Decimal(list_item["balance_usd"]) == Decimal("205.5")
    assert [asset["symbol"] for asset in list_item["top_assets"]] == ["SOL", "USDC"]

    assert wallet_detail.status_code == 200
    detail = wallet_detail.json()
    assert detail["wallet"]["chain_type"] == "solana"
    assert [asset["chain"] for asset in detail["assets"]] == ["solana", "solana"]

    assert portfolio.status_code == 200
    portfolio_data = portfolio.json()
    assert Decimal(portfolio_data["total_usd"]) == Decimal("205.5")
    assert portfolio_data["active_wallets_count"] == 1
    assert [asset["symbol"] for asset in portfolio_data["top_assets"]] == [
        "SOL",
        "USDC",
    ]

    assert allocation.status_code == 200
    allocation_items = {
        item["asset_key"]: Decimal(item["usd_value"])
        for item in allocation.json()["items"]
    }
    assert allocation_items == {
        "native:solana:SOL": Decimal("200"),
        f"spl:solana:{SOLANA_ADDRESS.lower()}": Decimal("5.5"),
    }


def test_openapi_publishes_solana_wallet_type():
    from app.main import app

    wallet_type_schema = app.openapi()["components"]["schemas"]["WalletCreate"][
        "properties"
    ]["wallet_type"]

    assert wallet_type_schema["enum"] == ["evm", "solana", "manual"]
