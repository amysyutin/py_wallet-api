from decimal import Decimal

from httpx import AsyncClient


async def _manual_portfolio(
    client: AsyncClient,
    headers: dict[str, str],
) -> None:
    wallet = await client.post(
        "/wallets",
        headers=headers,
        json={
            "label": "Allocation plan",
            "wallet_type": "manual",
            "chain_type": "manual",
        },
    )
    assert wallet.status_code == 201
    balances = await client.put(
        f"/wallets/{wallet.json()['id']}/balances",
        headers=headers,
        json={
            "balances": [
                {"symbol": "BTC", "amount": "1", "price_usd": "60"},
                {"symbol": "ETH", "amount": "1", "price_usd": "40"},
            ]
        },
    )
    assert balances.status_code == 200


async def _register(client: AsyncClient, email: str) -> dict[str, str]:
    registered = await client.post(
        "/auth/register",
        json={"email": email, "password": "password12"},
    )
    assert registered.status_code == 201
    login = await client.post(
        "/auth/login",
        json={"email": email, "password": "password12"},
    )
    assert login.status_code == 200
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


async def test_allocation_targets_drive_rebalancing_hints(
    client: AsyncClient,
    auth_headers: dict[str, str],
):
    await _manual_portfolio(client, auth_headers)
    saved = await client.put(
        "/portfolio/allocation/targets",
        headers=auth_headers,
        json={
            "items": [
                {"asset_key": "manual:BTC", "symbol": "btc", "target_pct": "50"},
                {"asset_key": "manual:ETH", "symbol": "ETH", "target_pct": "50"},
            ]
        },
    )

    assert saved.status_code == 200
    assert {item["symbol"] for item in saved.json()["items"]} == {"BTC", "ETH"}

    allocation = await client.get("/portfolio/allocation", headers=auth_headers)

    assert allocation.status_code == 200
    body = allocation.json()
    assert len(body["available_assets"]) == 2
    assert body["rebalancing"]["status"] == "ready"
    assert [item["asset_key"] for item in body["rebalancing"]["items"]] == [
        "manual:BTC",
        "manual:ETH",
    ]
    hints = {item["asset_key"]: item for item in body["rebalancing"]["items"]}
    assert hints["manual:BTC"]["action"] == "reduce"
    assert hints["manual:BTC"]["deviation_pct"] == 10.0
    assert Decimal(hints["manual:BTC"]["suggested_usd"]) == Decimal("-10")
    assert hints["manual:ETH"]["action"] == "increase"
    assert hints["manual:ETH"]["deviation_pct"] == -10.0
    assert Decimal(hints["manual:ETH"]["suggested_usd"]) == Decimal("10")


async def test_allocation_targets_are_owner_scoped_and_can_be_cleared(
    client: AsyncClient,
    auth_headers: dict[str, str],
):
    other_headers = await _register(client, "targets-other@example.com")
    other_saved = await client.put(
        "/portfolio/allocation/targets",
        headers=other_headers,
        json={
            "items": [{"asset_key": "manual:SOL", "symbol": "SOL", "target_pct": "100"}]
        },
    )
    assert other_saved.status_code == 200

    own = await client.get("/portfolio/allocation/targets", headers=auth_headers)
    assert own.status_code == 200
    assert own.json() == {"items": []}

    cleared = await client.put(
        "/portfolio/allocation/targets",
        headers=other_headers,
        json={"items": []},
    )
    assert cleared.status_code == 200
    assert cleared.json() == {"items": []}


async def test_allocation_targets_require_one_precise_distribution(
    client: AsyncClient,
    auth_headers: dict[str, str],
):
    invalid_payloads = [
        {
            "items": [
                {"asset_key": "manual:BTC", "symbol": "BTC", "target_pct": "60"},
                {"asset_key": "manual:ETH", "symbol": "ETH", "target_pct": "30"},
            ]
        },
        {
            "items": [
                {"asset_key": "manual:BTC", "symbol": "BTC", "target_pct": "50"},
                {"asset_key": "manual:BTC", "symbol": "BTC", "target_pct": "50"},
            ]
        },
        {
            "items": [
                {
                    "asset_key": "manual:BTC",
                    "symbol": "BTC",
                    "target_pct": "100.001",
                }
            ]
        },
        {"items": [{"asset_key": "__other__", "symbol": "Other", "target_pct": "100"}]},
    ]

    for payload in invalid_payloads:
        response = await client.put(
            "/portfolio/allocation/targets",
            headers=auth_headers,
            json=payload,
        )
        assert response.status_code == 422


async def test_group_allocation_does_not_apply_global_targets(
    client: AsyncClient,
    auth_headers: dict[str, str],
):
    group = await client.post(
        "/wallet-groups",
        headers=auth_headers,
        json={"name": "Scoped"},
    )
    assert group.status_code == 201
    await client.put(
        "/portfolio/allocation/targets",
        headers=auth_headers,
        json={
            "items": [{"asset_key": "manual:BTC", "symbol": "BTC", "target_pct": "100"}]
        },
    )

    allocation = await client.get(
        f"/portfolio/allocation?mode=selection&group_id={group.json()['id']}",
        headers=auth_headers,
    )

    assert allocation.status_code == 200
    assert allocation.json()["targets"] == []
    assert allocation.json()["rebalancing"]["status"] == "not_applicable"
