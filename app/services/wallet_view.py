from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Literal

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import CHAIN_RPC, NATIVE_SYMBOL
from app.core.config import get_settings
from app.db.models.asset import Asset
from app.db.models.balance_snapshot import BalanceSnapshot
from app.db.models.manual_balance import ManualBalance
from app.db.models.snapshot import Snapshot
from app.db.models.snapshot_service import (
    ChainSnapshot,
    SnapshotBalanceSnapshot,
    SnapshotRun,
    WalletSnapshot,
)
from app.db.models.wallet import Wallet
from app.db.models.wallet_group import WalletGroup
from app.metrics import observe_wallet_balance
from app.models import ChainSummary, PortfolioSummary, TokenBalance
from app.schemas.wallet import (
    WalletAssetDetail,
    WalletChainIssue,
    WalletDataHealth,
    WalletDetailSummary,
    WalletPriceQuality,
    WalletRead,
    WalletSnapshotRead,
    WalletSummaryRead,
    WalletTopAsset,
)

SNAPSHOT_READ_STATUSES = ("success", "partial_success")
TOP_ASSETS_LIMIT = 5
ACTIVE_SNAPSHOT_STATUSES = ("pending", "running")
BalanceSource = Literal["latest_snapshot", "manual", "none"]
PriceSource = Literal["coingecko", "frankfurter", "manual", "static_dev", "unknown"]
PriceQualityState = Literal["complete", "estimated", "incomplete", "unknown"]
WalletFreshness = Literal["fresh", "aging", "stale", "unknown"]
WalletHealthState = Literal["fresh", "updating", "partial", "stale"]
PRICE_SOURCE_ORDER: tuple[PriceSource, ...] = (
    "coingecko",
    "frankfurter",
    "manual",
    "static_dev",
    "unknown",
)


def _normalize_price_source(source: str | None) -> PriceSource:
    if source == "coingecko":
        return "coingecko"
    if source == "frankfurter":
        return "frankfurter"
    if source == "manual":
        return "manual"
    if source == "static_dev":
        return "static_dev"
    return "unknown"


@dataclass
class _AssetAgg:
    amount: Decimal = Decimal("0")
    usd_value: Decimal = Decimal("0")
    price_usd: Decimal | None = None
    chain: str = "manual"


@dataclass
class _WalletBalanceInfo:
    balance_usd: Decimal = Decimal("0")
    balance_source: BalanceSource = "none"
    last_snapshot_at: datetime | None = None
    balances_count: int = 0
    top_assets: list[WalletTopAsset] = field(default_factory=list)
    assets: list[WalletAssetDetail] = field(default_factory=list)
    wallet_snapshot_id: int | None = None
    legacy_snapshot_id: int | None = None


def _manual_value(amount: Decimal, price_usd: Decimal | None) -> Decimal:
    return amount * (price_usd if price_usd is not None else Decimal("0"))


async def _latest_wallet_snapshot_ids(
    session: AsyncSession, wallets: list[Wallet]
) -> dict[int, int]:
    """Return wallet_id -> newest-started readable WalletSnapshot.id."""
    wallet_ids = [wallet.id for wallet in wallets]
    if not wallet_ids:
        return {}
    snapshot_rank = func.row_number().over(
        partition_by=WalletSnapshot.wallet_id,
        order_by=(SnapshotRun.created_at.desc(), WalletSnapshot.id.desc()),
    )
    ranked = (
        select(
            WalletSnapshot.wallet_id,
            WalletSnapshot.id.label("snapshot_id"),
            snapshot_rank.label("snapshot_rank"),
        )
        .join(SnapshotRun, SnapshotRun.id == WalletSnapshot.snapshot_run_id)
        .join(Wallet, Wallet.id == WalletSnapshot.wallet_id)
        .where(
            WalletSnapshot.wallet_id.in_(wallet_ids),
            WalletSnapshot.status.in_(SNAPSHOT_READ_STATUSES),
            SnapshotRun.created_at >= Wallet.address_updated_at,
        )
        .subquery()
    )
    rows = await session.execute(
        select(ranked.c.wallet_id, ranked.c.snapshot_id).where(
            ranked.c.snapshot_rank == 1
        )
    )
    return {wallet_id: snapshot_id for wallet_id, snapshot_id in rows}


async def _load_snapshot_assets(
    session: AsyncSession, snapshot_ids: list[int]
) -> dict[int, list[tuple[str, str, Decimal, Decimal, Decimal | None]]]:
    """wallet_snapshot_id -> list of (symbol, chain, amount, value_usd, price_usd)."""
    if not snapshot_ids:
        return {}
    rows = await session.execute(
        select(
            ChainSnapshot.wallet_snapshot_id,
            SnapshotBalanceSnapshot.asset_symbol,
            ChainSnapshot.chain,
            SnapshotBalanceSnapshot.amount,
            SnapshotBalanceSnapshot.value_usd,
            SnapshotBalanceSnapshot.price_usd,
        )
        .join(
            SnapshotBalanceSnapshot,
            SnapshotBalanceSnapshot.chain_snapshot_id == ChainSnapshot.id,
        )
        .where(ChainSnapshot.wallet_snapshot_id.in_(snapshot_ids))
    )
    by_snapshot: dict[int, list[tuple[str, str, Decimal, Decimal, Decimal | None]]] = (
        defaultdict(list)
    )
    for row in rows:
        by_snapshot[row.wallet_snapshot_id].append(
            (
                row.asset_symbol,
                row.chain,
                row.amount,
                row.value_usd,
                row.price_usd,
            )
        )
    return by_snapshot


async def _load_snapshot_meta(
    session: AsyncSession, snapshot_ids: list[int]
) -> dict[int, tuple[Decimal, datetime | None]]:
    """wallet_snapshot_id -> (total_usd, snapshot_at)."""
    if not snapshot_ids:
        return {}
    snapshot_at = func.coalesce(
        WalletSnapshot.finished_at,
        SnapshotRun.finished_at,
        SnapshotRun.created_at,
    )
    rows = await session.execute(
        select(
            WalletSnapshot.id,
            WalletSnapshot.total_usd,
            snapshot_at.label("snapshot_at"),
        )
        .join(SnapshotRun, SnapshotRun.id == WalletSnapshot.snapshot_run_id)
        .where(WalletSnapshot.id.in_(snapshot_ids))
    )
    return {row.id: (row.total_usd, row.snapshot_at) for row in rows}


async def _latest_legacy_snapshots(
    session: AsyncSession, wallets: list[Wallet]
) -> dict[int, tuple[int, Decimal, datetime]]:
    """Return the most recent legacy snapshot for each requested wallet."""
    wallet_ids = [wallet.id for wallet in wallets]
    if not wallet_ids:
        return {}

    snapshot_rank = func.row_number().over(
        partition_by=Snapshot.wallet_id,
        order_by=(Snapshot.snapshot_at.desc(), Snapshot.id.desc()),
    )
    ranked = (
        select(
            Snapshot.id.label("snapshot_id"),
            Snapshot.wallet_id,
            Snapshot.total_usd,
            Snapshot.snapshot_at,
            snapshot_rank.label("snapshot_rank"),
        )
        .join(Wallet, Wallet.id == Snapshot.wallet_id)
        .where(
            Snapshot.wallet_id.in_(wallet_ids),
            Snapshot.snapshot_at >= Wallet.address_updated_at,
        )
        .subquery()
    )
    rows = await session.execute(
        select(
            ranked.c.snapshot_id,
            ranked.c.wallet_id,
            ranked.c.total_usd,
            ranked.c.snapshot_at,
        ).where(ranked.c.snapshot_rank == 1)
    )
    return {
        row.wallet_id: (row.snapshot_id, row.total_usd, row.snapshot_at) for row in rows
    }


async def _load_legacy_snapshot_assets(
    session: AsyncSession, snapshot_ids: list[int]
) -> dict[int, list[tuple[str, str, Decimal, Decimal, Decimal | None]]]:
    """Legacy snapshot id -> assets in the wallet-view tuple format."""
    if not snapshot_ids:
        return {}
    rows = await session.execute(
        select(
            BalanceSnapshot.snapshot_id,
            Asset.symbol,
            Asset.chain,
            BalanceSnapshot.amount,
            BalanceSnapshot.usd_value,
        )
        .join(Asset, Asset.id == BalanceSnapshot.asset_id)
        .where(BalanceSnapshot.snapshot_id.in_(snapshot_ids))
    )
    by_snapshot: dict[int, list[tuple[str, str, Decimal, Decimal, Decimal | None]]] = (
        defaultdict(list)
    )
    for row in rows:
        price_usd = row.usd_value / row.amount if row.amount != Decimal("0") else None
        by_snapshot[row.snapshot_id].append(
            (row.symbol, row.chain, row.amount, row.usd_value, price_usd)
        )
    return by_snapshot


async def _load_manual_assets(
    session: AsyncSession, wallet_ids: list[int]
) -> dict[int, list[tuple[str, str, Decimal, Decimal, Decimal | None]]]:
    """wallet_id -> list of (symbol, chain, amount, value_usd, price_usd)."""
    if not wallet_ids:
        return {}
    rows = await session.execute(
        select(ManualBalance)
        .options(selectinload(ManualBalance.asset))
        .where(ManualBalance.wallet_id.in_(wallet_ids))
    )
    by_wallet: dict[int, list[tuple[str, str, Decimal, Decimal, Decimal | None]]] = (
        defaultdict(list)
    )
    for balance in rows.scalars():
        asset = balance.asset
        value = _manual_value(balance.amount, balance.price_usd)
        by_wallet[balance.wallet_id].append(
            (
                asset.symbol,
                asset.chain,
                balance.amount,
                value,
                balance.price_usd,
            )
        )
    return by_wallet


def _aggregate_symbol_assets(
    items: list[tuple[str, str, Decimal, Decimal, Decimal | None]],
) -> dict[str, _AssetAgg]:
    is_agg: dict[str, _AssetAgg] = {}
    for symbol, chain, amount, value_usd, price_usd in items:
        current = is_agg.get(symbol)
        if current is None:
            is_agg[symbol] = _AssetAgg(
                amount=amount,
                usd_value=value_usd,
                price_usd=price_usd,
                chain=chain,
            )
        else:
            current.amount += amount
            current.usd_value += value_usd
            if current.price_usd is None:
                current.price_usd = price_usd
    return is_agg


def _top_assets_from_agg(
    agg: dict[str, _AssetAgg], limit: int = TOP_ASSETS_LIMIT
) -> list[WalletTopAsset]:
    ordered = sorted(agg.items(), key=lambda item: item[1].usd_value, reverse=True)
    return [
        WalletTopAsset(
            symbol=symbol,
            amount=data.amount,
            usd_value=data.usd_value,
        )
        for symbol, data in ordered[:limit]
    ]


def _detail_assets_from_items(
    items: list[tuple[str, str, Decimal, Decimal, Decimal | None]],
) -> list[WalletAssetDetail]:
    return [
        WalletAssetDetail(
            symbol=symbol,
            chain=chain,
            amount=amount,
            usd_value=value_usd,
            price_usd=price_usd,
        )
        for symbol, chain, amount, value_usd, price_usd in sorted(
            items, key=lambda item: item[3], reverse=True
        )
    ]


def _chain_summary_from_snapshot_items(
    *,
    chain: str,
    status: str,
    error_type: str | None,
    error_message: str | None,
    items: list[tuple[str, Decimal, Decimal]],
) -> ChainSummary:
    native_symbol = NATIVE_SYMBOL.get(chain, "NATIVE")
    native_amount = Decimal("0")
    usdt_amount = Decimal("0")
    usdc_amount = Decimal("0")
    token_totals: dict[str, tuple[Decimal, Decimal]] = {}

    for symbol, amount, value_usd in items:
        if symbol == native_symbol:
            native_amount += amount
        elif symbol == "USDT":
            usdt_amount += amount
        elif symbol == "USDC":
            usdc_amount += amount
        else:
            token_amount, token_usd = token_totals.get(
                symbol, (Decimal("0"), Decimal("0"))
            )
            token_totals[symbol] = (
                token_amount + amount,
                token_usd + value_usd,
            )

    return ChainSummary(
        chain=chain,
        native_symbol=native_symbol,
        native_amount=float(native_amount),
        usdt_amount=float(usdt_amount),
        usdc_amount=float(usdc_amount),
        tokens=[
            TokenBalance(symbol=symbol, amount=float(amount), usd=float(value_usd))
            for symbol, (amount, value_usd) in sorted(token_totals.items())
        ],
        status=status,
        error_type=error_type,
        error_message=error_message,
    )


def _chain_sort_key(summary: ChainSummary) -> tuple[int, str]:
    configured_order = {chain: index for index, chain in enumerate(CHAIN_RPC)}
    return configured_order.get(summary.chain, len(configured_order)), summary.chain


async def build_latest_wallet_assets_summary(
    session: AsyncSession,
    wallet: Wallet,
    *,
    address: str,
) -> PortfolioSummary | None:
    """Build the existing assets response from the current address revision."""
    wallet_snapshot = await session.scalar(
        select(WalletSnapshot)
        .join(SnapshotRun, SnapshotRun.id == WalletSnapshot.snapshot_run_id)
        .options(
            selectinload(WalletSnapshot.chain_snapshots).selectinload(
                ChainSnapshot.balance_snapshots
            )
        )
        .where(
            WalletSnapshot.wallet_id == wallet.id,
            WalletSnapshot.status.in_(SNAPSHOT_READ_STATUSES),
            SnapshotRun.created_at >= wallet.address_updated_at,
        )
        .order_by(SnapshotRun.created_at.desc(), WalletSnapshot.id.desc())
        .limit(1)
    )
    if wallet_snapshot is not None:
        chains = []
        for chain_snapshot in wallet_snapshot.chain_snapshots:
            items = [
                (
                    balance.asset_symbol,
                    balance.amount,
                    balance.value_usd,
                )
                for balance in chain_snapshot.balance_snapshots
            ]
            chains.append(
                _chain_summary_from_snapshot_items(
                    chain=chain_snapshot.chain,
                    status=chain_snapshot.status,
                    error_type=chain_snapshot.error_type,
                    # Persisted provider errors are not part of the public
                    # contract and may contain endpoint or credential details.
                    error_message=(
                        "Snapshot collection failed"
                        if chain_snapshot.error_message
                        else None
                    ),
                    items=items,
                )
            )
        chains.sort(key=_chain_sort_key)
        return PortfolioSummary(
            address=address,
            chains=chains,
            total_usd=float(wallet_snapshot.total_usd),
        )
    return None


async def build_wallet_balance_info(
    session: AsyncSession, wallets: list[Wallet]
) -> dict[int, _WalletBalanceInfo]:
    latest_by_wallet = await _latest_wallet_snapshot_ids(session, wallets)
    snapshot_ids = list(latest_by_wallet.values())
    snapshot_meta = await _load_snapshot_meta(session, snapshot_ids)
    snapshot_assets = await _load_snapshot_assets(session, snapshot_ids)

    needs_legacy = [w for w in wallets if w.id not in latest_by_wallet]
    legacy_by_wallet = await _latest_legacy_snapshots(session, needs_legacy)
    legacy_assets = await _load_legacy_snapshot_assets(
        session, [snapshot[0] for snapshot in legacy_by_wallet.values()]
    )

    needs_manual = [
        w.id
        for w in wallets
        if w.id not in latest_by_wallet
        and w.id not in legacy_by_wallet
        and w.wallet_type == "manual"
    ]
    manual_assets = await _load_manual_assets(session, needs_manual)

    result: dict[int, _WalletBalanceInfo] = {}
    for wallet in wallets:
        info = _WalletBalanceInfo()
        snapshot_id = latest_by_wallet.get(wallet.id)
        if snapshot_id is not None:
            total_usd, snapshot_at = snapshot_meta[snapshot_id]
            items = snapshot_assets.get(snapshot_id, [])
            agg = _aggregate_symbol_assets(items)
            info.balance_usd = total_usd
            info.balance_source = "latest_snapshot"
            info.last_snapshot_at = snapshot_at
            info.balances_count = len(items)
            info.top_assets = _top_assets_from_agg(agg)
            info.assets = _detail_assets_from_items(items)
            info.wallet_snapshot_id = snapshot_id
        elif wallet.id in legacy_by_wallet:
            legacy_snapshot_id, total_usd, snapshot_at = legacy_by_wallet[wallet.id]
            items = legacy_assets.get(legacy_snapshot_id, [])
            agg = _aggregate_symbol_assets(items)
            info.balance_usd = total_usd
            # Keep the public API contract stable: this is still the latest
            # persisted snapshot, only stored in the legacy schema.
            info.balance_source = "latest_snapshot"
            info.last_snapshot_at = snapshot_at
            info.balances_count = len(items)
            info.top_assets = _top_assets_from_agg(agg)
            info.assets = _detail_assets_from_items(items)
            info.legacy_snapshot_id = legacy_snapshot_id
        elif wallet.wallet_type == "manual":
            items = manual_assets.get(wallet.id, [])
            if items:
                agg = _aggregate_symbol_assets(items)
                info.balance_usd = sum(
                    (item.usd_value for item in agg.values()), Decimal("0")
                )
                info.balance_source = "manual"
                info.balances_count = len(items)
                info.top_assets = _top_assets_from_agg(agg)
                info.assets = _detail_assets_from_items(items)
        result[wallet.id] = info
        observe_wallet_balance(
            source=info.balance_source,
            snapshot_at=info.last_snapshot_at,
        )
    return result


async def _group_names(session: AsyncSession, wallets: list[Wallet]) -> dict[int, str]:
    group_ids = {w.group_id for w in wallets if w.group_id is not None}
    if not group_ids:
        return {}
    rows = await session.execute(
        select(WalletGroup.id, WalletGroup.name).where(WalletGroup.id.in_(group_ids))
    )
    return {group_id: name for group_id, name in rows}


async def build_wallet_summaries(
    session: AsyncSession, wallets: list[Wallet]
) -> list[WalletSummaryRead]:
    if not wallets:
        return []
    balance_info = await build_wallet_balance_info(session, wallets)
    groups = await _group_names(session, wallets)
    summaries: list[WalletSummaryRead] = []
    for wallet in wallets:
        info = balance_info[wallet.id]
        summaries.append(
            WalletSummaryRead(
                id=wallet.id,
                label=wallet.label,
                wallet_type=wallet.wallet_type,
                chain_type=wallet.chain_type,
                address=wallet.address,
                group_id=wallet.group_id,
                group_name=groups.get(wallet.group_id) if wallet.group_id else None,
                is_active=wallet.is_active,
                balance_usd=info.balance_usd,
                balance_source=info.balance_source,
                last_snapshot_at=info.last_snapshot_at,
                balances_count=info.balances_count,
                top_assets=info.top_assets,
                created_at=wallet.created_at,
                updated_at=wallet.updated_at,
            )
        )
    return summaries


async def build_wallet_detail_summary(
    session: AsyncSession, wallet: Wallet
) -> WalletDetailSummary:
    info = (await build_wallet_balance_info(session, [wallet]))[wallet.id]
    chain_issues: list[WalletChainIssue] = []
    price_observations: list[tuple[Decimal, Decimal | None, str | None]] = []
    if info.wallet_snapshot_id is not None:
        issue_rows = await session.execute(
            select(
                ChainSnapshot.chain,
                ChainSnapshot.status,
                ChainSnapshot.error_type,
            )
            .where(
                ChainSnapshot.wallet_snapshot_id == info.wallet_snapshot_id,
                ChainSnapshot.status != "success",
            )
            .order_by(ChainSnapshot.chain)
        )
        chain_issues = [
            WalletChainIssue(
                chain=row.chain,
                status=row.status,
                error_type=row.error_type,
            )
            for row in issue_rows
        ]
        price_rows = await session.execute(
            select(
                SnapshotBalanceSnapshot.amount,
                SnapshotBalanceSnapshot.price_usd,
                SnapshotBalanceSnapshot.price_source,
            )
            .join(
                ChainSnapshot,
                ChainSnapshot.id == SnapshotBalanceSnapshot.chain_snapshot_id,
            )
            .where(ChainSnapshot.wallet_snapshot_id == info.wallet_snapshot_id)
        )
        price_observations = [
            (row.amount, row.price_usd, row.price_source) for row in price_rows
        ]
    else:
        manual_source = "manual" if info.balance_source == "manual" else None
        price_observations = [
            (asset.amount, asset.price_usd, manual_source) for asset in info.assets
        ]

    relevant_prices = [
        (amount, price_usd, source)
        for amount, price_usd, source in price_observations
        if amount != Decimal("0")
    ]
    price_sources = {
        _normalize_price_source(source) for _, _, source in relevant_prices
    }
    assets_priced = sum(price_usd is not None for _, price_usd, _ in relevant_prices)
    assets_total = len(relevant_prices)
    if assets_total == 0:
        price_state: PriceQualityState = "unknown"
    elif assets_priced < assets_total:
        price_state = "incomplete"
    elif "static_dev" in price_sources:
        price_state = "estimated"
    elif "unknown" in price_sources:
        price_state = "unknown"
    else:
        price_state = "complete"
    price_quality = WalletPriceQuality(
        state=price_state,
        sources=[source for source in PRICE_SOURCE_ORDER if source in price_sources],
        assets_priced=assets_priced,
        assets_total=assets_total,
    )

    refresh_scope = [
        (SnapshotRun.scope_type == "wallet") & (SnapshotRun.wallet_id == wallet.id),
        SnapshotRun.scope_type == "all",
    ]
    if wallet.group_id is not None:
        refresh_scope.append(
            (SnapshotRun.scope_type == "group")
            & (SnapshotRun.group_id == wallet.group_id)
        )
    refresh_in_progress = bool(
        await session.scalar(
            select(func.count())
            .select_from(SnapshotRun)
            .where(
                SnapshotRun.user_id == wallet.user_id,
                SnapshotRun.status.in_(ACTIVE_SNAPSHOT_STATUSES),
                or_(*refresh_scope),
            )
        )
    )

    as_of = info.last_snapshot_at
    if as_of is None:
        freshness: WalletFreshness = "unknown"
    else:
        if as_of.tzinfo is None:
            as_of = as_of.replace(tzinfo=timezone.utc)
        age_seconds = max(
            0.0,
            (datetime.now(timezone.utc) - as_of).total_seconds(),
        )
        settings = get_settings()
        if age_seconds <= settings.portfolio_fresh_seconds:
            freshness = "fresh"
        elif age_seconds <= settings.portfolio_stale_seconds:
            freshness = "aging"
        else:
            freshness = "stale"

    if chain_issues or price_quality.state in {"estimated", "incomplete"}:
        health_state: WalletHealthState = "partial"
    elif refresh_in_progress:
        health_state = "updating"
    elif info.balance_source == "none":
        health_state = "partial"
    elif freshness == "stale":
        health_state = "stale"
    else:
        health_state = "fresh"
    return WalletDetailSummary(
        wallet=WalletRead.model_validate(wallet),
        balance_usd=info.balance_usd,
        last_snapshot_at=info.last_snapshot_at,
        assets=info.assets,
        data_health=WalletDataHealth(
            state=health_state,
            freshness=freshness,
            as_of=as_of,
            source=info.balance_source,
            refresh_in_progress=refresh_in_progress,
            chain_issues=chain_issues,
            price_quality=price_quality,
        ),
    )


async def list_wallet_snapshots(
    session: AsyncSession,
    wallet_id: int,
    *,
    limit: int = 30,
) -> list[WalletSnapshotRead]:
    snapshot_at = func.coalesce(
        WalletSnapshot.finished_at,
        SnapshotRun.finished_at,
        SnapshotRun.created_at,
    )
    rows = await session.execute(
        select(
            WalletSnapshot.id,
            WalletSnapshot.snapshot_run_id,
            WalletSnapshot.status,
            WalletSnapshot.total_usd,
            snapshot_at.label("snapshot_at"),
        )
        .join(SnapshotRun, SnapshotRun.id == WalletSnapshot.snapshot_run_id)
        .where(WalletSnapshot.wallet_id == wallet_id)
        .order_by(SnapshotRun.created_at.desc(), WalletSnapshot.id.desc())
        .limit(limit)
    )
    return [
        WalletSnapshotRead(
            id=row.id,
            snapshot_run_id=row.snapshot_run_id,
            status=row.status,
            total_usd=row.total_usd,
            snapshot_at=row.snapshot_at,
        )
        for row in rows
    ]
