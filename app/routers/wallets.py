import asyncio
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, HTTPException, Query, Response, status
from fastapi.concurrency import run_in_threadpool
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.core.config import get_settings
from app.log import get_logger
from app.db.models.telegram import TelegramAccount
from app.db.models.wallet import Wallet
from app.db.models.wallet_group import WalletGroup
from app.deps import CurrentUser, SessionDep
from app.models import PortfolioSummary
from app.metrics import FIRST_WALLET_ADDED
from app.schemas.manual_balance import ManualBalancesPut, ManualBalancesRead
from app.schemas.snapshot import SnapshotJobRead
from app.schemas.wallet import (
    WalletCreate,
    WalletDetailSummary,
    WalletRead,
    WalletSnapshotRead,
    WalletSummaryRead,
    WalletUpdate,
    normalize_wallet_address,
    validate_wallet_network_state,
)
from app.routes import lookup_live_assets, resolve_assets_address
from app.services.manual_balance import (
    delete_manual_balance,
    get_manual_balances,
    upsert_manual_balances,
)
from app.services.snapshot_jobs import SnapshotServiceError, create_snapshot_job
from app.services.wallet_view import (
    build_latest_wallet_assets_summary,
    build_wallet_detail_summary,
    build_wallet_summaries,
    list_wallet_snapshots,
)

router = APIRouter(prefix="/wallets", tags=["wallets"])
logger = get_logger(__name__)
ClientChannelValue = Literal["web", "telegram"]


def _is_active_onchain_duplicate(exc: IntegrityError) -> bool:
    current: BaseException | None = exc
    while current is not None:
        constraint_name = getattr(current, "constraint_name", None)
        error_text = str(current)
        for known_constraint in (
            "uq_wallets_active_evm_address",
            "uq_wallets_active_solana_address",
        ):
            if constraint_name == known_constraint or known_constraint in error_text:
                return True
        current = current.__cause__ or current.__context__
    return False


def _duplicate_address_detail(wallet_type: str) -> str:
    network = "Solana" if wallet_type == "solana" else "EVM"
    return f"An active wallet with this {network} address already exists"


async def _trigger_wallet_snapshot_background(
    *,
    user_id: int,
    wallet_id: int,
    activation_channel: ClientChannelValue | None,
) -> None:
    settings = get_settings()
    try:
        await run_in_threadpool(
            create_snapshot_job,
            settings,
            user_id=user_id,
            scope_type="wallet",
            wallet_id=wallet_id,
            trigger_type="auto",
            activation_channel=activation_channel,
        )
    except SnapshotServiceError:
        # Wallet creation must not fail due to snapshot service issues.
        logger.warning(
            "Auto snapshot trigger failed for wallet_id=%s user_id=%s",
            wallet_id,
            user_id,
        )
        return


async def _get_owned_wallet(
    session: SessionDep, user_id: int, wallet_id: int
) -> Wallet | None:
    return await session.scalar(
        select(Wallet).where(Wallet.id == wallet_id, Wallet.user_id == user_id)
    )


async def _get_owned_group(
    session: SessionDep, user_id: int, group_id: int
) -> WalletGroup | None:
    return await session.scalar(
        select(WalletGroup).where(
            WalletGroup.id == group_id,
            WalletGroup.user_id == user_id,
        )
    )


async def _validate_group_id(
    session: SessionDep, user_id: int, group_id: int | None
) -> None:
    if group_id is None:
        return
    group = await _get_owned_group(session, user_id, group_id)
    if group is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Wallet group not found",
        )


async def _get_server_activation_channel(
    session: SessionDep, user_id: int
) -> ClientChannelValue:
    telegram_account_id = await session.scalar(
        select(TelegramAccount.id).where(TelegramAccount.user_id == user_id)
    )
    return "telegram" if telegram_account_id is not None else "web"


async def _ensure_unique_active_onchain_address(
    session: SessionDep,
    *,
    user_id: int,
    wallet_type: str,
    address: str | None,
    exclude_wallet_id: int | None = None,
) -> None:
    """Reject duplicate active on-chain wallets for the same owner.

    EVM addresses are hexadecimal and case-insensitive. Solana public keys are
    base58 strings and remain case-sensitive.
    """
    if address is None or wallet_type not in {"evm", "solana"}:
        return

    normalized_address = address.strip()
    query = select(Wallet.id).where(
        Wallet.user_id == user_id,
        Wallet.wallet_type == wallet_type,
        Wallet.is_active.is_(True),
    )
    if wallet_type == "evm":
        query = query.where(
            func.lower(func.trim(Wallet.address)) == normalized_address.lower()
        )
    else:
        query = query.where(func.trim(Wallet.address) == normalized_address)
    if exclude_wallet_id is not None:
        query = query.where(Wallet.id != exclude_wallet_id)

    if await session.scalar(query) is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_duplicate_address_detail(wallet_type),
        )


@router.post("", response_model=WalletRead, status_code=status.HTTP_201_CREATED)
async def create_wallet(
    payload: WalletCreate,
    current_user: CurrentUser,
    session: SessionDep,
) -> Wallet:
    # Serialize creates for one owner so two concurrent first-wallet requests
    # cannot both emit the activation event.
    await session.execute(select(func.pg_advisory_xact_lock(current_user.id)))
    has_wallet = (
        await session.scalar(
            select(Wallet.id).where(Wallet.user_id == current_user.id).limit(1)
        )
        is not None
    )
    activation_channel = (
        await _get_server_activation_channel(session, current_user.id)
        if not has_wallet
        else None
    )
    await _validate_group_id(session, current_user.id, payload.group_id)
    if payload.wallet_type in {"evm", "solana"}:
        await _ensure_unique_active_onchain_address(
            session,
            user_id=current_user.id,
            wallet_type=payload.wallet_type,
            address=payload.address,
        )

    wallet = Wallet(
        user_id=current_user.id,
        label=payload.label,
        address=payload.address,
        chain_type=payload.chain_type,
        wallet_type=payload.wallet_type,
        group_id=payload.group_id,
        notes=payload.notes,
    )
    session.add(wallet)
    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        if not _is_active_onchain_duplicate(exc):
            raise
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_duplicate_address_detail(payload.wallet_type),
        ) from None
    await session.refresh(wallet)
    if not has_wallet:
        FIRST_WALLET_ADDED.labels(
            channel=activation_channel,
            wallet_type=wallet.wallet_type,
        ).inc()

    settings = get_settings()
    if settings.snapshot_auto_on_wallet_create and wallet.is_active:
        asyncio.create_task(
            _trigger_wallet_snapshot_background(
                user_id=current_user.id,
                wallet_id=wallet.id,
                activation_channel=activation_channel,
            )
        )
    return wallet


@router.get("", response_model=list[WalletSummaryRead])
async def list_wallets(
    current_user: CurrentUser,
    session: SessionDep,
    active_only: bool = Query(default=True),
    group_id: int | None = Query(default=None),
    wallet_type: str | None = Query(default=None),
    chain_type: str | None = Query(default=None),
) -> list[WalletSummaryRead]:
    query = select(Wallet).where(Wallet.user_id == current_user.id)
    if active_only:
        query = query.where(Wallet.is_active.is_(True))
    if group_id is not None:
        query = query.where(Wallet.group_id == group_id)
    if wallet_type is not None:
        query = query.where(Wallet.wallet_type == wallet_type)
    if chain_type is not None:
        query = query.where(Wallet.chain_type == chain_type)
    query = query.order_by(Wallet.id)
    wallets = list(await session.scalars(query))
    return await build_wallet_summaries(session, wallets)


@router.get("/{wallet_id}", response_model=WalletRead)
async def get_wallet(
    wallet_id: int,
    current_user: CurrentUser,
    session: SessionDep,
) -> Wallet:
    wallet = await _get_owned_wallet(session, current_user.id, wallet_id)
    if wallet is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Wallet not found")
    return wallet


@router.get("/{wallet_id}/summary", response_model=WalletDetailSummary)
async def get_wallet_summary(
    wallet_id: int,
    current_user: CurrentUser,
    session: SessionDep,
) -> WalletDetailSummary:
    wallet = await _get_owned_wallet(session, current_user.id, wallet_id)
    if wallet is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Wallet not found")
    return await build_wallet_detail_summary(session, wallet)


@router.get("/{wallet_id}/snapshots", response_model=list[WalletSnapshotRead])
async def get_wallet_snapshots(
    wallet_id: int,
    current_user: CurrentUser,
    session: SessionDep,
    limit: int = Query(default=30, ge=1, le=100),
) -> list[WalletSnapshotRead]:
    wallet = await _get_owned_wallet(session, current_user.id, wallet_id)
    if wallet is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Wallet not found")
    return await list_wallet_snapshots(session, wallet.id, limit=limit)


@router.post(
    "/{wallet_id}/snapshots",
    response_model=SnapshotJobRead,
    response_model_exclude_defaults=True,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_wallet_snapshot(
    wallet_id: int,
    current_user: CurrentUser,
    session: SessionDep,
) -> SnapshotJobRead:
    wallet = await _get_owned_wallet(session, current_user.id, wallet_id)
    if wallet is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Wallet not found")
    if not wallet.is_active:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Wallet is inactive")

    try:
        job = await run_in_threadpool(
            create_snapshot_job,
            get_settings(),
            user_id=current_user.id,
            scope_type="wallet",
            wallet_id=wallet.id,
        )
    except SnapshotServiceError as exc:
        raise HTTPException(exc.status_code, exc.detail) from exc

    return SnapshotJobRead(job_id=job.job_id, status=job.status, reused=job.reused)


@router.patch("/{wallet_id}", response_model=WalletRead)
async def update_wallet(
    wallet_id: int,
    payload: WalletUpdate,
    current_user: CurrentUser,
    session: SessionDep,
) -> Wallet:
    wallet = await _get_owned_wallet(session, current_user.id, wallet_id)
    if wallet is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Wallet not found")

    updates = payload.model_dump(exclude_unset=True)
    if "address" in updates:
        try:
            updates["address"] = normalize_wallet_address(
                wallet.wallet_type,
                updates["address"],
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=str(exc),
            ) from exc
    address_changed = "address" in updates and (
        (updates["address"] or "").strip().lower()
        if wallet.wallet_type == "evm"
        else (updates["address"] or "").strip()
    ) != (
        (wallet.address or "").strip().lower()
        if wallet.wallet_type == "evm"
        else (wallet.address or "").strip()
    )
    if "group_id" in updates:
        await _validate_group_id(session, current_user.id, updates["group_id"])

    if "chain_type" in updates or "address" in updates:
        try:
            validate_wallet_network_state(
                wallet.wallet_type,
                updates.get("chain_type", wallet.chain_type),
                updates.get("address", wallet.address),
            )
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=str(exc),
            ) from exc

    resulting_is_active = updates.get("is_active", wallet.is_active)
    if wallet.wallet_type in {"evm", "solana"} and resulting_is_active:
        await _ensure_unique_active_onchain_address(
            session,
            user_id=current_user.id,
            wallet_type=wallet.wallet_type,
            address=updates.get("address", wallet.address),
            exclude_wallet_id=wallet.id,
        )

    for field, value in updates.items():
        setattr(wallet, field, value)
    if address_changed:
        wallet.address_updated_at = datetime.now(timezone.utc)

    try:
        await session.commit()
    except IntegrityError as exc:
        await session.rollback()
        if not _is_active_onchain_duplicate(exc):
            raise
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_duplicate_address_detail(wallet.wallet_type),
        ) from None
    await session.refresh(wallet)
    return wallet


@router.delete("/{wallet_id}", response_model=WalletRead)
async def delete_wallet(
    wallet_id: int,
    current_user: CurrentUser,
    session: SessionDep,
) -> Wallet:
    wallet = await _get_owned_wallet(session, current_user.id, wallet_id)
    if wallet is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Wallet not found")

    if wallet.is_active:
        wallet.is_active = False
        await session.commit()
        await session.refresh(wallet)

    return wallet


@router.post("/{wallet_id}/restore", response_model=WalletRead)
async def restore_wallet(
    wallet_id: int,
    current_user: CurrentUser,
    session: SessionDep,
) -> Wallet:
    wallet = await _get_owned_wallet(session, current_user.id, wallet_id)
    if wallet is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Wallet not found")

    if not wallet.is_active:
        if wallet.wallet_type in {"evm", "solana"}:
            await _ensure_unique_active_onchain_address(
                session,
                user_id=current_user.id,
                wallet_type=wallet.wallet_type,
                address=wallet.address,
                exclude_wallet_id=wallet.id,
            )
        wallet.is_active = True
        try:
            await session.commit()
        except IntegrityError as exc:
            await session.rollback()
            if not _is_active_onchain_duplicate(exc):
                raise
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=_duplicate_address_detail(wallet.wallet_type),
            ) from None
        await session.refresh(wallet)

    return wallet


@router.get("/{wallet_id}/assets", response_model=PortfolioSummary)
async def get_wallet_assets(
    wallet_id: int,
    current_user: CurrentUser,
    session: SessionDep,
) -> PortfolioSummary:
    wallet = await _get_owned_wallet(session, current_user.id, wallet_id)
    if wallet is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Wallet not found")
    if wallet.wallet_type != "evm":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Multi-chain assets are available for EVM wallets only",
        )
    if not wallet.address:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Wallet has no address",
        )
    resolved_address = resolve_assets_address(wallet.address)
    persisted = await build_latest_wallet_assets_summary(
        session,
        wallet,
        address=resolved_address,
    )
    if persisted is not None:
        return persisted
    return PortfolioSummary.model_validate(await lookup_live_assets(resolved_address))


@router.get("/{wallet_id}/balances", response_model=ManualBalancesRead)
async def list_wallet_balances(
    wallet_id: int,
    current_user: CurrentUser,
    session: SessionDep,
) -> ManualBalancesRead:
    wallet = await _get_owned_wallet(session, current_user.id, wallet_id)
    if wallet is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Wallet not found")
    return await get_manual_balances(session, wallet)


@router.put("/{wallet_id}/balances", response_model=ManualBalancesRead)
async def put_wallet_balances(
    wallet_id: int,
    payload: ManualBalancesPut,
    current_user: CurrentUser,
    session: SessionDep,
) -> ManualBalancesRead:
    wallet = await _get_owned_wallet(session, current_user.id, wallet_id)
    if wallet is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Wallet not found")
    return await upsert_manual_balances(session, wallet, payload)


@router.delete(
    "/{wallet_id}/balances/{asset_id}", status_code=status.HTTP_204_NO_CONTENT
)
async def remove_wallet_balance(
    wallet_id: int,
    asset_id: int,
    current_user: CurrentUser,
    session: SessionDep,
) -> Response:
    wallet = await _get_owned_wallet(session, current_user.id, wallet_id)
    if wallet is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Wallet not found")
    return await delete_manual_balance(session, wallet, asset_id)
