"""Tests for manual wallet balances API."""

from decimal import Decimal

from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.models.asset import Asset
from app.services.asset import get_or_create_manual_asset


async def _register_and_login(client: AsyncClient, email: str) -> dict[str, str]:
    await client.post(
        "/auth/register",
        json={"email": email, "password": "password12"},
    )
    login = await client.post(
        "/auth/login",
        json={"email": email, "password": "password12"},
    )
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


async def _create_manual_wallet(client: AsyncClient, headers: dict) -> dict:
    r = await client.post(
        "/wallets",
        headers=headers,
        json={
            "label": "Manual BTC",
            "wallet_type": "manual",
            "chain_type": "manual",
        },
    )
    assert r.status_code == 201
    return r.json()


async def test_add_btc_balance_to_manual_wallet(
    client: AsyncClient, auth_headers: dict
):
    wallet = await _create_manual_wallet(client, auth_headers)
    r = await client.put(
        f"/wallets/{wallet['id']}/balances",
        headers=auth_headers,
        json={
            "balances": [
                {"symbol": "BTC", "amount": "0.125", "price_usd": "68000"},
            ]
        },
    )
    assert r.status_code == 200
    data = r.json()
    assert len(data["balances"]) == 1
    assert data["balances"][0]["symbol"] == "BTC"
    assert Decimal(data["balances"][0]["amount"]) == Decimal("0.125")
    assert Decimal(data["total_usd"]) == Decimal("8500")


async def test_update_existing_btc_balance(client: AsyncClient, auth_headers: dict):
    wallet = await _create_manual_wallet(client, auth_headers)
    url = f"/wallets/{wallet['id']}/balances"
    await client.put(
        url,
        headers=auth_headers,
        json={"balances": [{"symbol": "BTC", "amount": "0.125", "price_usd": "68000"}]},
    )
    r = await client.put(
        url,
        headers=auth_headers,
        json={"balances": [{"symbol": "BTC", "amount": "0.2", "price_usd": "70000"}]},
    )
    assert r.status_code == 200
    data = r.json()
    assert len(data["balances"]) == 1
    assert Decimal(data["balances"][0]["amount"]) == Decimal("0.2")
    assert Decimal(data["total_usd"]) == Decimal("14000")


async def test_list_balances(client: AsyncClient, auth_headers: dict):
    wallet = await _create_manual_wallet(client, auth_headers)
    await client.put(
        f"/wallets/{wallet['id']}/balances",
        headers=auth_headers,
        json={
            "balances": [
                {"symbol": "BTC", "amount": "1", "price_usd": "50000"},
                {"symbol": "ETH", "amount": "2", "price_usd": "3000"},
            ]
        },
    )
    r = await client.get(f"/wallets/{wallet['id']}/balances", headers=auth_headers)
    assert r.status_code == 200
    data = r.json()
    assert data["wallet_id"] == wallet["id"]
    assert data["wallet_label"] == "Manual BTC"
    assert data["wallet_type"] == "manual"
    assert len(data["balances"]) == 2
    assert Decimal(data["total_usd"]) == Decimal("56000")


async def test_total_usd_null_price_is_zero(client: AsyncClient, auth_headers: dict):
    wallet = await _create_manual_wallet(client, auth_headers)
    await client.put(
        f"/wallets/{wallet['id']}/balances",
        headers=auth_headers,
        json={"balances": [{"symbol": "BTC", "amount": "1"}]},
    )
    r = await client.get(f"/wallets/{wallet['id']}/balances", headers=auth_headers)
    assert r.status_code == 200
    data = r.json()
    assert Decimal(data["balances"][0]["value_usd"]) == Decimal("0")
    assert Decimal(data["total_usd"]) == Decimal("0")


async def test_delete_one_balance(client: AsyncClient, auth_headers: dict):
    wallet = await _create_manual_wallet(client, auth_headers)
    put = await client.put(
        f"/wallets/{wallet['id']}/balances",
        headers=auth_headers,
        json={"balances": [{"symbol": "BTC", "amount": "0.125", "price_usd": "68000"}]},
    )
    asset_id = put.json()["balances"][0]["asset_id"]
    r = await client.delete(
        f"/wallets/{wallet['id']}/balances/{asset_id}",
        headers=auth_headers,
    )
    assert r.status_code == 204
    listing = await client.get(
        f"/wallets/{wallet['id']}/balances", headers=auth_headers
    )
    assert listing.json()["balances"] == []
    assert Decimal(listing.json()["total_usd"]) == Decimal("0")


async def test_cannot_add_balance_to_other_users_wallet(client: AsyncClient):
    h1 = await _register_and_login(client, "bal-owner@example.com")
    h2 = await _register_and_login(client, "bal-other@example.com")
    wallet = await _create_manual_wallet(client, h1)
    r = await client.put(
        f"/wallets/{wallet['id']}/balances",
        headers=h2,
        json={"balances": [{"symbol": "BTC", "amount": "1", "price_usd": "50000"}]},
    )
    assert r.status_code == 404


async def test_cannot_get_balances_of_other_users_wallet(client: AsyncClient):
    h1 = await _register_and_login(client, "bal-get-owner@example.com")
    h2 = await _register_and_login(client, "bal-get-other@example.com")
    wallet = await _create_manual_wallet(client, h1)
    r = await client.get(f"/wallets/{wallet['id']}/balances", headers=h2)
    assert r.status_code == 404


async def test_cannot_add_manual_balance_to_evm_wallet(
    client: AsyncClient, auth_headers: dict
):
    wallet = (
        await client.post(
            "/wallets",
            headers=auth_headers,
            json={
                "label": "EVM",
                "address": "0x00000000000000000000000000000000000000aa",
                "chain_type": "mainnet",
            },
        )
    ).json()
    r = await client.put(
        f"/wallets/{wallet['id']}/balances",
        headers=auth_headers,
        json={"balances": [{"symbol": "BTC", "amount": "1", "price_usd": "50000"}]},
    )
    assert r.status_code == 400
    assert "manual wallets" in r.json()["detail"].lower()


async def test_negative_amount_fails(client: AsyncClient, auth_headers: dict):
    wallet = await _create_manual_wallet(client, auth_headers)
    r = await client.put(
        f"/wallets/{wallet['id']}/balances",
        headers=auth_headers,
        json={"balances": [{"symbol": "BTC", "amount": "-1", "price_usd": "50000"}]},
    )
    assert r.status_code == 422


async def test_negative_price_usd_fails(client: AsyncClient, auth_headers: dict):
    wallet = await _create_manual_wallet(client, auth_headers)
    r = await client.put(
        f"/wallets/{wallet['id']}/balances",
        headers=auth_headers,
        json={"balances": [{"symbol": "BTC", "amount": "1", "price_usd": "-100"}]},
    )
    assert r.status_code == 422


async def test_symbol_normalization(client: AsyncClient, auth_headers: dict):
    wallet = await _create_manual_wallet(client, auth_headers)
    r = await client.put(
        f"/wallets/{wallet['id']}/balances",
        headers=auth_headers,
        json={"balances": [{"symbol": "btc", "amount": "1", "price_usd": "50000"}]},
    )
    assert r.status_code == 200
    assert r.json()["balances"][0]["symbol"] == "BTC"


async def test_blank_symbol_is_rejected(client: AsyncClient, auth_headers: dict):
    wallet = await _create_manual_wallet(client, auth_headers)
    response = await client.put(
        f"/wallets/{wallet['id']}/balances",
        headers=auth_headers,
        json={"balances": [{"symbol": "   ", "amount": "1", "price_usd": "1"}]},
    )

    assert response.status_code == 422


async def test_overlong_chain_is_rejected(client: AsyncClient, auth_headers: dict):
    wallet = await _create_manual_wallet(client, auth_headers)
    response = await client.put(
        f"/wallets/{wallet['id']}/balances",
        headers=auth_headers,
        json={
            "balances": [
                {
                    "symbol": "BTC",
                    "chain": "x" * 33,
                    "amount": "1",
                    "price_usd": "1",
                }
            ]
        },
    )

    assert response.status_code == 422


async def test_duplicate_normalized_assets_are_rejected(
    client: AsyncClient, auth_headers: dict
):
    wallet = await _create_manual_wallet(client, auth_headers)
    response = await client.put(
        f"/wallets/{wallet['id']}/balances",
        headers=auth_headers,
        json={
            "balances": [
                {"symbol": "btc", "amount": "1", "price_usd": "1"},
                {"symbol": " BTC ", "amount": "2", "price_usd": "1"},
            ]
        },
    )

    assert response.status_code == 422


async def test_asset_get_or_create_no_duplicates(
    client: AsyncClient, auth_headers: dict, db_session: AsyncSession
):
    wallet = await _create_manual_wallet(client, auth_headers)
    url = f"/wallets/{wallet['id']}/balances"
    await client.put(
        url,
        headers=auth_headers,
        json={"balances": [{"symbol": "BTC", "amount": "1", "price_usd": "50000"}]},
    )
    await client.put(
        url,
        headers=auth_headers,
        json={"balances": [{"symbol": "btc", "amount": "2", "price_usd": "51000"}]},
    )
    count = await db_session.scalar(
        select(func.count())
        .select_from(Asset)
        .where(Asset.symbol == "BTC", Asset.chain == "manual")
    )
    assert count == 1


async def test_same_symbol_reused_across_users(
    client: AsyncClient, db_session: AsyncSession
):
    h1 = await _register_and_login(client, "asset-user1@example.com")
    h2 = await _register_and_login(client, "asset-user2@example.com")
    w1 = await _create_manual_wallet(client, h1)
    w2 = await _create_manual_wallet(client, h2)
    await client.put(
        f"/wallets/{w1['id']}/balances",
        headers=h1,
        json={"balances": [{"symbol": "ETH", "amount": "1", "price_usd": "3000"}]},
    )
    await client.put(
        f"/wallets/{w2['id']}/balances",
        headers=h2,
        json={"balances": [{"symbol": "ETH", "amount": "2", "price_usd": "3100"}]},
    )
    count = await db_session.scalar(
        select(func.count())
        .select_from(Asset)
        .where(Asset.symbol == "ETH", Asset.chain == "manual")
    )
    assert count == 1


async def test_manual_asset_service_normalizes_defaults_and_reuses(db_session):
    created = await get_or_create_manual_asset(
        db_session,
        symbol="  maintenance_coin  ",
        chain="   ",
    )
    reused = await get_or_create_manual_asset(
        db_session,
        symbol="MAINTENANCE_COIN",
        chain="manual",
    )

    assert reused.id == created.id
    assert created.symbol == "MAINTENANCE_COIN"
    assert created.name == "MAINTENANCE_COIN"
    assert created.chain == "manual"
    assert created.contract_address is None
    assert created.decimals == 18


async def test_manual_asset_service_does_not_reuse_contract_asset(db_session):
    contract_asset = Asset(
        symbol="CONTRACT_COIN",
        name="Contract coin",
        contract_address="manual:contract_coin",
        chain="manual",
        decimals=6,
    )
    db_session.add(contract_asset)
    await db_session.flush()

    manual_asset = await get_or_create_manual_asset(
        db_session,
        symbol="contract_coin",
        chain="MANUAL",
    )

    assert manual_asset.id != contract_asset.id
    assert manual_asset.contract_address is None
    assert manual_asset.decimals == 18


async def test_delete_missing_balance_returns_404(
    client: AsyncClient, auth_headers: dict
):
    wallet = await _create_manual_wallet(client, auth_headers)
    r = await client.delete(
        f"/wallets/{wallet['id']}/balances/99999",
        headers=auth_headers,
    )
    assert r.status_code == 404


async def test_empty_balances_list_returns_400(client: AsyncClient, auth_headers: dict):
    wallet = await _create_manual_wallet(client, auth_headers)
    r = await client.put(
        f"/wallets/{wallet['id']}/balances",
        headers=auth_headers,
        json={"balances": []},
    )
    assert r.status_code == 400
