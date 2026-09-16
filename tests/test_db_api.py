import asyncio
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import patch

import bcrypt
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import SessionLocal
from app.db.models.user import User
from app.db.models.snapshot_service import (
    ChainSnapshot,
    SnapshotBalanceSnapshot,
    SnapshotRun,
    WalletSnapshot,
)
from app.db.models.wallet import Wallet
from app.services.admin_promote import PromoteAdminStatus, promote_admin_by_email
from app.services.snapshot_jobs import SnapshotJobResult
from app.main import app


async def test_register_and_me(client: AsyncClient):
    r = await client.post(
        "/auth/register",
        json={"email": "u1@example.com", "password": "password12"},
    )
    assert r.status_code == 201
    assert r.json()["email"] == "u1@example.com"

    login = await client.post(
        "/auth/login",
        json={"email": "u1@example.com", "password": "password12"},
    )
    token = login.json()["access_token"]

    me = await client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    assert me.json()["email"] == "u1@example.com"


async def test_register_saves_email_lowercase(client: AsyncClient):
    r = await client.post(
        "/auth/register",
        json={"email": "MixedCase@Example.COM", "password": "password12"},
    )
    assert r.status_code == 201
    assert r.json()["email"] == "mixedcase@example.com"


async def test_concurrent_same_email_registration_returns_created_and_conflict():
    email = "concurrent-register@example.com"
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as api_client:
        responses = await asyncio.gather(
            api_client.post(
                "/auth/register",
                json={"email": email, "password": "password12"},
            ),
            api_client.post(
                "/auth/register",
                json={"email": email, "password": "password12"},
            ),
        )

    assert sorted(response.status_code for response in responses) == [201, 409]
    async with SessionLocal() as cleanup_session:
        await cleanup_session.execute(delete(User).where(User.email == email))
        await cleanup_session.commit()


async def test_login_accepts_email_with_different_case(client: AsyncClient):
    await client.post(
        "/auth/register",
        json={"email": "case-login@example.com", "password": "password12"},
    )
    login = await client.post(
        "/auth/login",
        json={"email": "CASE-LOGIN@Example.COM", "password": "password12"},
    )
    assert login.status_code == 200
    assert login.json()["access_token"]


async def test_change_password_replaces_credentials(client: AsyncClient):
    email = "change-password@example.com"
    old_password = "old-password12"
    new_password = "new-password34"
    await client.post("/auth/register", json={"email": email, "password": old_password})
    login = await client.post(
        "/auth/login", json={"email": email, "password": old_password}
    )
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    changed = await client.post(
        "/auth/change-password",
        headers=headers,
        json={"current_password": old_password, "new_password": new_password},
    )

    assert changed.status_code == 204
    assert (
        await client.post(
            "/auth/login", json={"email": email, "password": old_password}
        )
    ).status_code == 401
    assert (
        await client.post(
            "/auth/login", json={"email": email, "password": new_password}
        )
    ).status_code == 200


async def test_change_password_rejects_wrong_current_password(
    client: AsyncClient, auth_headers: dict[str, str]
):
    response = await client.post(
        "/auth/change-password",
        headers=auth_headers,
        json={
            "current_password": "not-the-current-password",
            "new_password": "new-password34",
        },
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Current password is incorrect"


async def test_change_password_requires_authentication(client: AsyncClient):
    response = await client.post(
        "/auth/change-password",
        json={
            "current_password": "old-password12",
            "new_password": "new-password34",
        },
    )

    assert response.status_code == 401


async def test_change_password_requires_a_different_password(
    client: AsyncClient, auth_headers: dict[str, str]
):
    response = await client.post(
        "/auth/change-password",
        headers=auth_headers,
        json={
            "current_password": "testpass123",
            "new_password": "testpass123",
        },
    )

    assert response.status_code == 422


async def test_login_upgrades_legacy_bcrypt_hash(
    client: AsyncClient, db_session: AsyncSession
):
    password = "legacy-password"
    user = User(
        email="legacy-login@example.com",
        auth_hash=bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode(),
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)

    login = await client.post(
        "/auth/login",
        json={"email": user.email, "password": password},
    )

    assert login.status_code == 200
    await db_session.refresh(user)
    assert user.auth_hash.startswith("$argon2id$")


async def test_promote_admin_accepts_registered_email_with_different_case(
    client: AsyncClient, db_session: AsyncSession
):
    await client.post(
        "/auth/register",
        json={"email": "promote-registered@example.com", "password": "password12"},
    )
    result = await promote_admin_by_email(
        db_session,
        "Promote-Registered@Example.COM",
    )
    assert result.status == PromoteAdminStatus.promoted


async def test_register_duplicate_409(client: AsyncClient):
    payload = {"email": "dup@example.com", "password": "password12"}
    assert (await client.post("/auth/register", json=payload)).status_code == 201
    assert (await client.post("/auth/register", json=payload)).status_code == 409


async def test_register_duplicate_409_with_different_case(client: AsyncClient):
    assert (
        await client.post(
            "/auth/register",
            json={"email": "dupcase@example.com", "password": "password12"},
        )
    ).status_code == 201
    assert (
        await client.post(
            "/auth/register",
            json={"email": "DupCase@Example.COM", "password": "password12"},
        )
    ).status_code == 409


async def test_wallets_create_and_list(client: AsyncClient, auth_headers: dict):
    r = await client.post(
        "/wallets",
        headers=auth_headers,
        json={
            "label": "Test",
            "address": "0x0000000000000000000000000000000000000001",
            "chain_type": "mainnet",
        },
    )
    assert r.status_code == 201
    wallet_id = r.json()["id"]

    lst = await client.get("/wallets", headers=auth_headers)
    assert lst.status_code == 200
    assert wallet_id in [w["id"] for w in lst.json()]


@patch(
    "app.routers.snapshots.create_snapshot_job",
    return_value=SnapshotJobResult(job_id=123, status="pending"),
)
async def test_snapshot_empty_balances(
    mock_create_job, client: AsyncClient, auth_headers: dict
):
    await client.post(
        "/wallets",
        headers=auth_headers,
        json={
            "label": "Snap",
            "address": "0x0000000000000000000000000000000000000002",
            "chain_type": "mainnet",
        },
    )
    r = await client.post("/snapshot", headers=auth_headers, json={})
    assert r.status_code == 202
    assert r.json() == {"job_id": 123, "status": "pending"}
    assert mock_create_job.call_args.kwargs["wallet_id"] is None


async def test_portfolio_history_and_summary(
    client: AsyncClient,
    auth_headers: dict,
    db_session: AsyncSession,
):
    w = (
        await client.post(
            "/wallets",
            headers=auth_headers,
            json={
                "label": "Hist",
                "address": "0x0000000000000000000000000000000000000003",
                "chain_type": "mainnet",
            },
        )
    ).json()

    wallet = await db_session.get(Wallet, w["id"])
    assert wallet is not None
    now = datetime.now(timezone.utc)

    run1 = SnapshotRun(
        user_id=wallet.user_id,
        wallet_id=wallet.id,
        trigger_type="manual",
        scope_type="wallet",
        status="success",
        finished_at=now - timedelta(minutes=2),
    )
    run2 = SnapshotRun(
        user_id=wallet.user_id,
        wallet_id=wallet.id,
        trigger_type="manual",
        scope_type="wallet",
        status="success",
        finished_at=now - timedelta(minutes=1),
    )
    db_session.add_all([run1, run2])
    await db_session.flush()

    wallet_snapshot1 = WalletSnapshot(
        snapshot_run_id=run1.id,
        wallet_id=wallet.id,
        wallet_type=wallet.wallet_type,
        status="success",
        total_usd=Decimal("100"),
    )
    wallet_snapshot2 = WalletSnapshot(
        snapshot_run_id=run2.id,
        wallet_id=wallet.id,
        wallet_type=wallet.wallet_type,
        status="success",
        total_usd=Decimal("200"),
    )
    db_session.add_all([wallet_snapshot1, wallet_snapshot2])
    await db_session.flush()

    chain_snapshot = ChainSnapshot(
        wallet_snapshot_id=wallet_snapshot2.id,
        chain="mainnet",
        status="success",
        total_usd=Decimal("200"),
    )
    db_session.add(chain_snapshot)
    await db_session.flush()
    db_session.add(
        SnapshotBalanceSnapshot(
            chain_snapshot_id=chain_snapshot.id,
            asset_symbol="ETH",
            amount=Decimal("1"),
            price_usd=Decimal("200"),
            value_usd=Decimal("200"),
            price_source="test",
        )
    )
    await db_session.flush()

    hist = await client.get(
        f"/portfolio?wallet_id={w['id']}&days=30",
        headers=auth_headers,
    )
    assert hist.status_code == 200
    assert len(hist.json()["points"]) == 2

    summary = await client.get("/portfolio/summary", headers=auth_headers)
    assert summary.status_code == 200
    body = summary.json()
    assert body["wallets_count"] >= 1
    assert body["active_wallets_count"] >= 1
    assert body["last_snapshot_at"] is not None
    assert Decimal(body["total_usd"]) == Decimal("200")
    assert body["top_assets"][0]["symbol"] == "ETH"

    hist_named = await client.get(
        f"/portfolio/history?wallet_id={w['id']}&days=30",
        headers=auth_headers,
    )
    assert hist_named.status_code == 200
    assert len(hist_named.json()["points"]) == 2


async def test_group_portfolio_history_aggregates_wallet_values_over_time(
    client: AsyncClient,
    auth_headers: dict,
    db_session: AsyncSession,
):
    group = (
        await client.post(
            "/wallet-groups",
            headers=auth_headers,
            json={"name": "Aggregated history"},
        )
    ).json()
    wallet_one = (
        await client.post(
            "/wallets",
            headers=auth_headers,
            json={
                "label": "History one",
                "address": "0x00000000000000000000000000000000000000d1",
                "chain_type": "mainnet",
                "group_id": group["id"],
            },
        )
    ).json()
    wallet_two = (
        await client.post(
            "/wallets",
            headers=auth_headers,
            json={
                "label": "History two",
                "address": "0x00000000000000000000000000000000000000d2",
                "chain_type": "mainnet",
                "group_id": group["id"],
            },
        )
    ).json()
    first_wallet = await db_session.get(Wallet, wallet_one["id"])
    second_wallet = await db_session.get(Wallet, wallet_two["id"])
    assert first_wallet is not None
    assert second_wallet is not None
    now = datetime.now(timezone.utc)
    first_wallet.address_updated_at = now - timedelta(minutes=10)
    second_wallet.address_updated_at = now - timedelta(minutes=10)
    await db_session.flush()
    runs = [
        SnapshotRun(
            user_id=first_wallet.user_id,
            wallet_id=first_wallet.id,
            trigger_type="manual",
            scope_type="wallet",
            status="success",
            created_at=now - timedelta(minutes=3),
        ),
        SnapshotRun(
            user_id=second_wallet.user_id,
            wallet_id=second_wallet.id,
            trigger_type="manual",
            scope_type="wallet",
            status="success",
            created_at=now - timedelta(minutes=2),
        ),
        SnapshotRun(
            user_id=first_wallet.user_id,
            wallet_id=first_wallet.id,
            trigger_type="manual",
            scope_type="wallet",
            status="success",
            created_at=now - timedelta(minutes=1),
        ),
    ]
    db_session.add_all(runs)
    await db_session.flush()
    db_session.add_all(
        [
            WalletSnapshot(
                snapshot_run_id=runs[0].id,
                wallet_id=first_wallet.id,
                wallet_type="evm",
                status="success",
                total_usd=Decimal("100"),
            ),
            WalletSnapshot(
                snapshot_run_id=runs[1].id,
                wallet_id=second_wallet.id,
                wallet_type="evm",
                status="success",
                total_usd=Decimal("200"),
            ),
            WalletSnapshot(
                snapshot_run_id=runs[2].id,
                wallet_id=first_wallet.id,
                wallet_type="evm",
                status="success",
                total_usd=Decimal("150"),
            ),
        ]
    )
    await db_session.flush()

    history = await client.get(
        f"/portfolio/history?group_id={group['id']}&days=30",
        headers=auth_headers,
    )

    assert history.status_code == 200
    assert [Decimal(point["total_usd"]) for point in history.json()["points"]] == [
        Decimal("100"),
        Decimal("300"),
        Decimal("350"),
    ]
    assert [
        Decimal(point["sources"]["onchain_usd"]) for point in history.json()["points"]
    ] == [Decimal("100"), Decimal("300"), Decimal("350")]
    assert all(
        point["sources"]["cex_usd"] is None
        and Decimal(point["sources"]["manual_usd"]) == Decimal("0")
        for point in history.json()["points"]
    )


async def test_health_ok(client: AsyncClient):
    r = await client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "healthy"


async def test_register_me_includes_role_user(client: AsyncClient):
    await client.post(
        "/auth/register",
        json={"email": "role@example.com", "password": "password12"},
    )
    login = await client.post(
        "/auth/login",
        json={"email": "role@example.com", "password": "password12"},
    )
    me = await client.get(
        "/auth/me",
        headers={"Authorization": f"Bearer {login.json()['access_token']}"},
    )
    assert me.status_code == 200
    assert me.json()["role"] == "user"
