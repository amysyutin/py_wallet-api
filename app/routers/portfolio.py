from datetime import datetime, timedelta, timezone
from decimal import Decimal

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import func, select
from starlette.concurrency import run_in_threadpool

from app.db.models.asset import Asset
from app.db.models.balance_snapshot import BalanceSnapshot
from app.db.models.snapshot import Snapshot
from app.core.config import get_settings
from app.db.models.snapshot_service import (
    ChainSnapshot,
    SnapshotBalanceSnapshot,
    SnapshotRun,
    WalletSnapshot,
)
from app.db.models.wallet import Wallet
from app.db.models.wallet_group import WalletGroup
from app.deps import CurrentUser, SessionDep
from app.schemas.portfolio import (
    AssetShare,
    AllocationAssetShare,
    PortfolioAllocation,
    PortfolioAllocationQuality,
    PortfolioAllScope,
    PortfolioChainIssue,
    PortfolioExchangeHealth,
    PortfolioHistory,
    PortfolioHistorySources,
    PortfolioPoint,
    PortfolioPriceQuality,
    PortfolioSelectionScope,
    PortfolioSourceSummary,
    PortfolioSummary,
    PortfolioValueChange24h,
)
from app.services.exchange_portfolio import (
    ExchangePortfolioSnapshot,
    fetch_exchange_history,
    fetch_exchange_portfolio,
)
from app.services.wallet_view import build_wallet_balance_info
from app.services.portfolio_health import (
    active_canonical_wallets as _active_canonical_wallets,
    build_portfolio_data_health,
    portfolio_freshness,
    portfolio_price_quality as _portfolio_price_quality,
)

router = APIRouter(prefix="/portfolio", tags=["portfolio"])
HISTORY_READ_STATUSES = ("success",)
ALLOCATION_VISIBLE_ASSETS = 5


def _history_sources(
    balances: dict[int, Decimal],
    wallet_types: dict[int, str],
    *,
    cex_usd: Decimal | None,
) -> PortfolioHistorySources:
    onchain = Decimal("0")
    manual = Decimal("0")
    for history_wallet_id, total_usd in balances.items():
        if wallet_types[history_wallet_id] == "manual":
            manual += total_usd
        else:
            onchain += total_usd
    return PortfolioHistorySources(
        onchain_usd=onchain,
        cex_usd=cex_usd,
        manual_usd=manual,
    )


def _snapshot_observed_at():
    return func.coalesce(
        WalletSnapshot.finished_at,
        SnapshotRun.finished_at,
        SnapshotRun.created_at,
    )


def _allocation_asset_key(
    *,
    chain: str,
    symbol: str,
    asset_address: str | None,
    asset_type: str | None,
) -> str:
    normalized_type = (asset_type or "").lower()
    if normalized_type == "erc20" and asset_address:
        return f"evm:{chain.lower()}:{asset_address.strip().lower()}"
    if normalized_type == "manual" or chain.lower() == "manual":
        return f"manual:{symbol.upper()}"
    if normalized_type == "native" or not asset_address:
        return f"native:{chain.lower()}:{symbol.upper()}"
    return f"{normalized_type or 'asset'}:{chain.lower()}:{asset_address.lower()}"


def _allocation_quality(
    observations: list[tuple[Decimal, Decimal | None, str | None]],
) -> PortfolioAllocationQuality:
    if not observations:
        return PortfolioAllocationQuality(
            state="empty",
            sources=[],
            assets_priced=0,
            assets_total=0,
        )
    quality = _portfolio_price_quality(observations)
    return PortfolioAllocationQuality(**quality.model_dump())


async def _allocation_for_wallets(
    session: SessionDep,
    wallets: list[Wallet],
    *,
    scope: PortfolioAllScope | PortfolioSelectionScope,
    exchange: ExchangePortfolioSnapshot | None = None,
) -> PortfolioAllocation:
    balance_info = await build_wallet_balance_info(session, wallets)
    asset_totals: dict[str, tuple[str, Decimal]] = {}
    observations: list[tuple[Decimal, Decimal | None, str | None]] = []

    def add_asset(
        *,
        key: str,
        symbol: str,
        amount: Decimal,
        value_usd: Decimal,
        price_usd: Decimal | None,
        price_source: str | None,
    ) -> None:
        existing_symbol, existing_value = asset_totals.get(key, (symbol, Decimal("0")))
        asset_totals[key] = (existing_symbol, existing_value + value_usd)
        observations.append((amount, price_usd, price_source))

    snapshot_ids = [
        info.wallet_snapshot_id
        for info in balance_info.values()
        if info.wallet_snapshot_id is not None
    ]
    if snapshot_ids:
        rows = await session.execute(
            select(
                ChainSnapshot.chain,
                SnapshotBalanceSnapshot.asset_symbol,
                SnapshotBalanceSnapshot.asset_address,
                SnapshotBalanceSnapshot.asset_type,
                SnapshotBalanceSnapshot.amount,
                SnapshotBalanceSnapshot.value_usd,
                SnapshotBalanceSnapshot.price_usd,
                SnapshotBalanceSnapshot.price_source,
            )
            .join(
                SnapshotBalanceSnapshot,
                SnapshotBalanceSnapshot.chain_snapshot_id == ChainSnapshot.id,
            )
            .where(ChainSnapshot.wallet_snapshot_id.in_(snapshot_ids))
        )
        for row in rows:
            add_asset(
                key=_allocation_asset_key(
                    chain=row.chain,
                    symbol=row.asset_symbol,
                    asset_address=row.asset_address,
                    asset_type=row.asset_type,
                ),
                symbol=row.asset_symbol,
                amount=row.amount,
                value_usd=row.value_usd,
                price_usd=row.price_usd,
                price_source=row.price_source,
            )

    legacy_ids = [
        info.legacy_snapshot_id
        for info in balance_info.values()
        if info.legacy_snapshot_id is not None
    ]
    if legacy_ids:
        rows = await session.execute(
            select(
                Asset.chain,
                Asset.symbol,
                Asset.contract_address,
                BalanceSnapshot.amount,
                BalanceSnapshot.usd_value,
            )
            .join(Asset, Asset.id == BalanceSnapshot.asset_id)
            .where(BalanceSnapshot.snapshot_id.in_(legacy_ids))
        )
        for row in rows:
            price_usd = (
                row.usd_value / row.amount if row.amount != Decimal("0") else None
            )
            add_asset(
                key=_allocation_asset_key(
                    chain=row.chain,
                    symbol=row.symbol,
                    asset_address=row.contract_address,
                    asset_type="erc20" if row.contract_address else "native",
                ),
                symbol=row.symbol,
                amount=row.amount,
                value_usd=row.usd_value,
                price_usd=price_usd,
                price_source="unknown",
            )

    for wallet in wallets:
        info = balance_info[wallet.id]
        if info.wallet_snapshot_id is not None or info.legacy_snapshot_id is not None:
            continue
        for asset in info.assets:
            add_asset(
                key=_allocation_asset_key(
                    chain=asset.chain,
                    symbol=asset.symbol,
                    asset_address=None,
                    asset_type="manual",
                ),
                symbol=asset.symbol,
                amount=asset.amount,
                value_usd=asset.usd_value,
                price_usd=asset.price_usd,
                price_source="manual",
            )

    if exchange is not None and exchange.status == "success":
        for asset in exchange.assets:
            add_asset(
                key=f"exchange:{exchange.provider}:{asset.symbol}",
                symbol=asset.symbol,
                amount=asset.amount,
                value_usd=asset.usd_value,
                price_usd=asset.price_usd,
                price_source=asset.price_source or "unknown",
            )

    total = sum((value for _, value in asset_totals.values()), Decimal("0"))
    ordered = sorted(
        asset_totals.items(),
        key=lambda item: item[1][1],
        reverse=True,
    )
    visible = ordered[:ALLOCATION_VISIBLE_ASSETS]
    remainder = ordered[ALLOCATION_VISIBLE_ASSETS:]
    items = [
        AllocationAssetShare(
            asset_key=asset_key,
            symbol=symbol,
            usd_value=value_usd,
            share_pct=round(float(value_usd / total * 100), 2) if total else 0.0,
        )
        for asset_key, (symbol, value_usd) in visible
    ]
    other_value = sum((value for _, (_, value) in remainder), Decimal("0"))
    if other_value:
        items.append(
            AllocationAssetShare(
                asset_key="__other__",
                symbol="Other",
                usd_value=other_value,
                share_pct=round(float(other_value / total * 100), 2),
            )
        )
    return PortfolioAllocation(
        scope=scope,
        wallets_count=len(wallets),
        total_usd=total,
        items=items,
        data_quality=_allocation_quality(observations),
    )


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _wallet_source_status(data_health) -> str:
    if data_health.wallets_total == 0:
        return "unavailable"
    if data_health.state in {"updating", "partial"}:
        return data_health.state
    if data_health.freshness in {"aging", "stale"}:
        return data_health.freshness
    return "fresh"


def _merge_exchange_health(
    data_health,
    exchange: ExchangePortfolioSnapshot,
) -> PortfolioExchangeHealth | None:
    if not exchange.configured:
        return None
    if exchange.status != "success":
        data_health.state = "partial"
        return PortfolioExchangeHealth(
            provider=exchange.provider,
            state="unavailable",
            freshness="unknown",
            assets_priced=0,
            assets_total=0,
            error_type=exchange.error_type,
        )

    settings = get_settings()
    exchange_freshness = portfolio_freshness(
        exchange.as_of,
        fresh_seconds=settings.portfolio_fresh_seconds,
        stale_seconds=settings.portfolio_stale_seconds,
    )
    exchange_assets_total = len(exchange.assets)
    exchange_assets_priced = sum(
        asset.price_usd is not None for asset in exchange.assets
    )
    exchange_state = (
        "partial"
        if exchange_assets_priced < exchange_assets_total
        else ("stale" if exchange_freshness == "stale" else "fresh")
    )

    health_dates = [
        date for date in (data_health.as_of, exchange.as_of) if date is not None
    ]
    data_health.as_of = min(health_dates) if health_dates else None
    data_health.freshness = portfolio_freshness(
        data_health.as_of,
        fresh_seconds=settings.portfolio_fresh_seconds,
        stale_seconds=settings.portfolio_stale_seconds,
    )

    quality = data_health.price_quality
    existing_sources = set(quality.sources)
    if exchange.assets:
        existing_sources.update(
            asset.price_source or "unknown"
            for asset in exchange.assets
            if asset.price_usd is not None
        )
        if exchange_assets_priced < exchange_assets_total:
            existing_sources.add("unknown")
    quality.assets_priced += exchange_assets_priced
    quality.assets_total += exchange_assets_total
    quality.sources = [
        source
        for source in (
            "coingecko",
            "binance_usdt",
            "frankfurter",
            "manual",
            "static_dev",
            "unknown",
        )
        if source in existing_sources
    ]
    if quality.assets_total == 0:
        quality.state = "unknown"
    elif quality.assets_priced < quality.assets_total:
        quality.state = "incomplete"
    elif "static_dev" in existing_sources:
        quality.state = "estimated"
    elif "unknown" in existing_sources:
        quality.state = "unknown"
    else:
        quality.state = "complete"

    if data_health.state == "partial" or exchange_state == "partial":
        data_health.state = "partial"
    elif data_health.refresh_in_progress:
        data_health.state = "updating"
    elif data_health.freshness == "stale":
        data_health.state = "stale"
    else:
        data_health.state = "fresh"

    return PortfolioExchangeHealth(
        provider=exchange.provider,
        state=exchange_state,
        freshness=exchange_freshness,
        as_of=exchange.as_of,
        assets_priced=exchange_assets_priced,
        assets_total=exchange_assets_total,
    )


async def _portfolio_change_24h(
    session: SessionDep,
    wallets: list[Wallet],
    *,
    current_total: Decimal,
    balance_info,
    price_quality: PortfolioPriceQuality,
    health_state: str,
    freshness: str,
    chain_issues: list[PortfolioChainIssue],
    has_exchange_assets: bool = False,
) -> PortfolioValueChange24h:
    reference_at = datetime.now(timezone.utc)
    cutoff_at = reference_at - timedelta(hours=24)
    tolerance = timedelta(seconds=get_settings().portfolio_fresh_seconds)

    def result(
        status_value: str,
        reasons: list[str],
        *,
        start_usd: Decimal | None = None,
        start_times: list[datetime] | None = None,
        end_times: list[datetime] | None = None,
    ) -> PortfolioValueChange24h:
        absolute = current_total - start_usd if start_usd is not None else None
        percent = (
            round(float(absolute / start_usd * 100), 2)
            if absolute is not None and start_usd != Decimal("0")
            else None
        )
        return PortfolioValueChange24h(
            status=status_value,
            start_usd=start_usd,
            end_usd=current_total if wallets or has_exchange_assets else None,
            absolute_usd=absolute,
            percent=percent,
            reference_at=reference_at,
            cutoff_at=cutoff_at,
            start_observed_from=min(start_times) if start_times else None,
            start_observed_to=max(start_times) if start_times else None,
            end_observed_from=min(end_times) if end_times else None,
            end_observed_to=max(end_times) if end_times else None,
            reason_codes=reasons,
        )

    if has_exchange_assets:
        return result("unavailable", ["current_source_has_no_historical_counterpart"])
    if not wallets:
        return result("unavailable", ["no_wallets"])

    current_ids = [balance_info[wallet.id].wallet_snapshot_id for wallet in wallets]
    if any(snapshot_id is None for snapshot_id in current_ids):
        return result("unavailable", ["current_source_has_no_historical_counterpart"])

    current_rows = list(
        await session.execute(
            select(
                WalletSnapshot.id,
                WalletSnapshot.status,
                _snapshot_observed_at().label("observed_at"),
            )
            .join(SnapshotRun, SnapshotRun.id == WalletSnapshot.snapshot_run_id)
            .where(WalletSnapshot.id.in_(current_ids))
        )
    )
    end_times = [_aware(row.observed_at) for row in current_rows]
    current_reasons = []
    if len(current_rows) != len(wallets):
        current_reasons.append("current_snapshot_missing")
    if any(row.status != "success" for row in current_rows):
        current_reasons.append("current_snapshot_partial")
    if chain_issues:
        current_reasons.append("current_chain_issues")
    if price_quality.state != "complete":
        current_reasons.append("current_price_quality")
    if health_state in {"partial", "stale"} or freshness != "fresh":
        current_reasons.append("current_data_not_fresh")
    if end_times and (
        reference_at - min(end_times) > tolerance
        or max(end_times) - min(end_times) > tolerance
    ):
        current_reasons.append("current_observation_skew")
    if current_reasons:
        return result("incomplete", current_reasons, end_times=end_times)

    observed_at = _snapshot_observed_at()
    baseline_rank = func.row_number().over(
        partition_by=WalletSnapshot.wallet_id,
        order_by=(observed_at.desc(), WalletSnapshot.id.desc()),
    )
    ranked = (
        select(
            WalletSnapshot.id.label("snapshot_id"),
            WalletSnapshot.wallet_id,
            WalletSnapshot.total_usd,
            observed_at.label("observed_at"),
            baseline_rank.label("snapshot_rank"),
        )
        .join(SnapshotRun, SnapshotRun.id == WalletSnapshot.snapshot_run_id)
        .join(Wallet, Wallet.id == WalletSnapshot.wallet_id)
        .where(
            WalletSnapshot.wallet_id.in_([wallet.id for wallet in wallets]),
            WalletSnapshot.status == "success",
            observed_at <= cutoff_at,
            observed_at >= cutoff_at - tolerance,
            SnapshotRun.created_at >= Wallet.address_updated_at,
        )
        .subquery()
    )
    baseline_rows = list(
        await session.execute(
            select(
                ranked.c.snapshot_id,
                ranked.c.wallet_id,
                ranked.c.total_usd,
                ranked.c.observed_at,
            ).where(ranked.c.snapshot_rank == 1)
        )
    )
    if len(baseline_rows) != len(wallets):
        reason = (
            "wallet_address_changed"
            if any(_aware(wallet.address_updated_at) > cutoff_at for wallet in wallets)
            else "baseline_missing"
        )
        return result("unavailable", [reason], end_times=end_times)

    baseline_ids = [row.snapshot_id for row in baseline_rows]
    baseline_chain_issue = await session.scalar(
        select(func.count())
        .select_from(ChainSnapshot)
        .where(
            ChainSnapshot.wallet_snapshot_id.in_(baseline_ids),
            ChainSnapshot.status != "success",
        )
    )
    baseline_price_rows = await session.execute(
        select(
            SnapshotBalanceSnapshot.amount,
            SnapshotBalanceSnapshot.price_usd,
            SnapshotBalanceSnapshot.price_source,
        )
        .join(
            ChainSnapshot,
            ChainSnapshot.id == SnapshotBalanceSnapshot.chain_snapshot_id,
        )
        .where(ChainSnapshot.wallet_snapshot_id.in_(baseline_ids))
    )
    baseline_quality = _portfolio_price_quality(
        [(row.amount, row.price_usd, row.price_source) for row in baseline_price_rows]
    )
    start_times = [_aware(row.observed_at) for row in baseline_rows]
    baseline_reasons = []
    if baseline_chain_issue:
        baseline_reasons.append("baseline_chain_issues")
    if baseline_quality.state != "complete":
        baseline_reasons.append("baseline_price_quality")
    if max(start_times) - min(start_times) > tolerance:
        baseline_reasons.append("baseline_observation_skew")
    if baseline_reasons:
        return result(
            "incomplete",
            baseline_reasons,
            start_times=start_times,
            end_times=end_times,
        )

    start_total = sum(
        (row.total_usd for row in baseline_rows),
        Decimal("0"),
    )
    if start_total == Decimal("0"):
        return result(
            "unavailable",
            ["baseline_zero"],
            start_usd=start_total,
            start_times=start_times,
            end_times=end_times,
        )
    return result(
        "complete",
        [],
        start_usd=start_total,
        start_times=start_times,
        end_times=end_times,
    )


async def _portfolio_history(
    current_user: CurrentUser,
    session: SessionDep,
    *,
    wallet_id: int | None,
    group_id: int | None,
    days: int,
) -> PortfolioHistory:
    if wallet_id is not None and group_id is not None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "Pass either wallet_id or group_id, not both",
        )

    wallet_query = select(Wallet).where(Wallet.user_id == current_user.id)
    if wallet_id is not None:
        wallet = await session.scalar(
            select(Wallet).where(
                Wallet.id == wallet_id, Wallet.user_id == current_user.id
            )
        )
        if wallet is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Wallet not found")
        wallet_query = wallet_query.where(Wallet.id == wallet_id)
    elif group_id is not None:
        owned_group = await session.scalar(
            select(WalletGroup.id).where(
                WalletGroup.id == group_id,
                WalletGroup.user_id == current_user.id,
            )
        )
        if owned_group is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Wallet group not found")
        wallet_query = wallet_query.where(
            Wallet.group_id == group_id,
            Wallet.is_active.is_(True),
        )
    else:
        wallet_query = wallet_query.where(Wallet.is_active.is_(True))

    wallets = list(await session.scalars(wallet_query.order_by(Wallet.id)))
    if wallet_id is None:
        seen_evm_addresses: set[str] = set()
        canonical_wallets: list[Wallet] = []
        for wallet in wallets:
            if wallet.wallet_type != "evm" or not wallet.address:
                canonical_wallets.append(wallet)
                continue
            normalized = wallet.address.strip().lower()
            if normalized in seen_evm_addresses:
                continue
            seen_evm_addresses.add(normalized)
            canonical_wallets.append(wallet)
        wallets = canonical_wallets

    wallet_ids = [wallet.id for wallet in wallets]
    wallet_types = {wallet.id: wallet.wallet_type for wallet in wallets}
    since = datetime.now(timezone.utc) - timedelta(days=days)
    exchange_history = None
    if wallet_id is None and group_id is None:
        exchange_history = await run_in_threadpool(
            fetch_exchange_history,
            get_settings(),
            user_id=current_user.id,
            since=since,
        )
    exchange_points = (
        list(exchange_history.points)
        if exchange_history is not None and exchange_history.status == "success"
        else []
    )
    if not wallet_ids and not exchange_points:
        return PortfolioHistory(
            wallet_id=wallet_id,
            group_id=group_id,
            days=days,
            points=[],
        )

    observed_at = _snapshot_observed_at()
    rows: list[tuple[int, datetime, Decimal]] = []
    first_new_by_wallet: dict[int, datetime] = {}
    if wallet_ids:
        new_rows = await session.execute(
            select(
                WalletSnapshot.wallet_id,
                observed_at.label("snapshot_at"),
                WalletSnapshot.total_usd,
            )
            .join(SnapshotRun, SnapshotRun.id == WalletSnapshot.snapshot_run_id)
            .where(
                WalletSnapshot.wallet_id.in_(wallet_ids),
                WalletSnapshot.status.in_(HISTORY_READ_STATUSES),
                observed_at >= since,
            )
            .order_by(observed_at, WalletSnapshot.id)
        )
        rows.extend(
            (row.wallet_id, _aware(row.snapshot_at), row.total_usd) for row in new_rows
        )
        first_new_rows = await session.execute(
            select(
                WalletSnapshot.wallet_id,
                func.min(observed_at).label("first_snapshot_at"),
            )
            .join(SnapshotRun, SnapshotRun.id == WalletSnapshot.snapshot_run_id)
            .where(
                WalletSnapshot.wallet_id.in_(wallet_ids),
                WalletSnapshot.status.in_(HISTORY_READ_STATUSES),
            )
            .group_by(WalletSnapshot.wallet_id)
        )
        first_new_by_wallet = {
            row.wallet_id: _aware(row.first_snapshot_at) for row in first_new_rows
        }
        legacy_rows = await session.execute(
            select(Snapshot.wallet_id, Snapshot.snapshot_at, Snapshot.total_usd)
            .where(
                Snapshot.wallet_id.in_(wallet_ids),
                Snapshot.snapshot_at >= since,
            )
            .order_by(Snapshot.snapshot_at, Snapshot.id)
        )
        rows.extend(
            (row.wallet_id, _aware(row.snapshot_at), row.total_usd)
            for row in legacy_rows
            if row.wallet_id not in first_new_by_wallet
            or _aware(row.snapshot_at) < first_new_by_wallet[row.wallet_id]
        )

    rows.sort(key=lambda row: (row[1], row[0]))
    if wallet_id is not None:
        points = []
        for history_wallet_id, snapshot_at, total_usd in rows:
            sources = _history_sources(
                {history_wallet_id: total_usd},
                wallet_types,
                cex_usd=None,
            )
            points.append(
                PortfolioPoint(
                    snapshot_at=snapshot_at,
                    total_usd=total_usd,
                    sources=sources,
                )
            )
    else:
        seed_by_wallet: dict[int, tuple[datetime, Decimal]] = {}
        if wallet_ids:
            latest_before_rank = func.row_number().over(
                partition_by=WalletSnapshot.wallet_id,
                order_by=(observed_at.desc(), WalletSnapshot.id.desc()),
            )
            latest_before = (
                select(
                    WalletSnapshot.wallet_id,
                    WalletSnapshot.total_usd,
                    observed_at.label("snapshot_at"),
                    latest_before_rank.label("snapshot_rank"),
                )
                .join(SnapshotRun, SnapshotRun.id == WalletSnapshot.snapshot_run_id)
                .where(
                    WalletSnapshot.wallet_id.in_(wallet_ids),
                    WalletSnapshot.status.in_(HISTORY_READ_STATUSES),
                    observed_at < since,
                )
                .subquery()
            )
            seed_by_wallet = {
                row.wallet_id: (_aware(row.snapshot_at), row.total_usd)
                for row in await session.execute(
                    select(
                        latest_before.c.wallet_id,
                        latest_before.c.snapshot_at,
                        latest_before.c.total_usd,
                    ).where(latest_before.c.snapshot_rank == 1)
                )
            }
            legacy_before_rows = await session.execute(
                select(Snapshot.wallet_id, Snapshot.snapshot_at, Snapshot.total_usd)
                .where(
                    Snapshot.wallet_id.in_(wallet_ids),
                    Snapshot.snapshot_at < since,
                )
                .order_by(Snapshot.snapshot_at.desc(), Snapshot.id.desc())
            )
            legacy_seen: set[int] = set()
            for row in legacy_before_rows:
                if row.wallet_id in legacy_seen:
                    continue
                legacy_at = _aware(row.snapshot_at)
                first_new = first_new_by_wallet.get(row.wallet_id)
                if first_new is not None and legacy_at >= first_new:
                    continue
                legacy_seen.add(row.wallet_id)
                current_seed = seed_by_wallet.get(row.wallet_id)
                if current_seed is None or legacy_at > current_seed[0]:
                    seed_by_wallet[row.wallet_id] = (
                        legacy_at,
                        row.total_usd,
                    )

        balance_by_wallet = {
            seed_wallet_id: total_usd
            for seed_wallet_id, (_, total_usd) in seed_by_wallet.items()
        }
        cex_usd = next(
            (
                point.total_usd
                for point in reversed(exchange_points)
                if point.as_of < since
            ),
            None,
        )
        events = [
            (snapshot_at, "wallet", history_wallet_id, total_usd)
            for history_wallet_id, snapshot_at, total_usd in rows
        ]
        events.extend(
            (point.as_of, "cex", 0, point.total_usd)
            for point in exchange_points
            if point.as_of >= since
        )
        events.sort(key=lambda event: (event[0], event[1], event[2]))

        points = []
        event_index = 0
        while event_index < len(events):
            snapshot_at = events[event_index][0]
            while event_index < len(events) and events[event_index][0] == snapshot_at:
                _, source, history_wallet_id, total_usd = events[event_index]
                if source == "cex":
                    cex_usd = total_usd
                else:
                    balance_by_wallet[history_wallet_id] = total_usd
                event_index += 1
            sources = _history_sources(
                balance_by_wallet,
                wallet_types,
                cex_usd=cex_usd,
            )
            points.append(
                PortfolioPoint(
                    snapshot_at=snapshot_at,
                    total_usd=(
                        sources.onchain_usd
                        + sources.manual_usd
                        + (sources.cex_usd or Decimal("0"))
                    ),
                    sources=sources,
                )
            )

    return PortfolioHistory(
        wallet_id=wallet_id,
        group_id=group_id,
        days=days,
        points=points,
    )


@router.get("", response_model=PortfolioHistory, include_in_schema=False)
async def portfolio_history_legacy(
    current_user: CurrentUser,
    session: SessionDep,
    wallet_id: int | None = Query(default=None),
    group_id: int | None = Query(default=None),
    days: int = Query(30, ge=1, le=365),
) -> PortfolioHistory:
    return await _portfolio_history(
        current_user,
        session,
        wallet_id=wallet_id,
        group_id=group_id,
        days=days,
    )


@router.get("/history", response_model=PortfolioHistory)
async def portfolio_history(
    current_user: CurrentUser,
    session: SessionDep,
    wallet_id: int | None = Query(default=None),
    group_id: int | None = Query(default=None),
    days: int = Query(30, ge=1, le=365),
) -> PortfolioHistory:
    return await _portfolio_history(
        current_user,
        session,
        wallet_id=wallet_id,
        group_id=group_id,
        days=days,
    )


@router.get("/allocation", response_model=PortfolioAllocation)
async def portfolio_allocation(
    current_user: CurrentUser,
    session: SessionDep,
    mode: str = Query(default="all", pattern="^(all|selection)$"),
    group_id: list[int] | None = Query(default=None),
    include_ungrouped: bool = Query(default=False),
) -> PortfolioAllocation:
    requested_group_ids = list(dict.fromkeys(group_id or []))
    if mode == "all":
        if requested_group_ids or include_ungrouped:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "Group filters require mode=selection",
            )
        scope: PortfolioAllScope | PortfolioSelectionScope = PortfolioAllScope(
            mode="all"
        )
        wallets = await _active_canonical_wallets(
            session,
            user_id=current_user.id,
        )
    else:
        if not requested_group_ids and not include_ungrouped:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY,
                "Select at least one group or include ungrouped wallets",
            )
        if requested_group_ids:
            owned_ids = set(
                await session.scalars(
                    select(WalletGroup.id).where(
                        WalletGroup.user_id == current_user.id,
                        WalletGroup.id.in_(requested_group_ids),
                    )
                )
            )
            if owned_ids != set(requested_group_ids):
                raise HTTPException(
                    status.HTTP_404_NOT_FOUND,
                    "Wallet group not found",
                )
        scope = PortfolioSelectionScope(
            mode="selection",
            group_ids=requested_group_ids,
            include_ungrouped=include_ungrouped,
        )
        wallets = await _active_canonical_wallets(
            session,
            user_id=current_user.id,
            group_ids=requested_group_ids,
            include_ungrouped=include_ungrouped,
        )
    exchange = None
    if mode == "all":
        exchange = await run_in_threadpool(
            fetch_exchange_portfolio,
            get_settings(),
            user_id=current_user.id,
        )
    return await _allocation_for_wallets(
        session,
        wallets,
        scope=scope,
        exchange=exchange,
    )


@router.get("/summary", response_model=PortfolioSummary)
async def portfolio_summary(
    current_user: CurrentUser,
    session: SessionDep,
) -> PortfolioSummary:
    wallets = await _active_canonical_wallets(
        session,
        user_id=current_user.id,
    )
    balance_info = await build_wallet_balance_info(session, wallets)
    wallet_total = sum(
        (balance_info[wallet.id].balance_usd for wallet in wallets),
        Decimal("0"),
    )
    exchange = await run_in_threadpool(
        fetch_exchange_portfolio,
        get_settings(),
        user_id=current_user.id,
    )
    total = wallet_total + exchange.total_usd

    wallets_count = (
        await session.scalar(
            select(func.count())
            .select_from(Wallet)
            .where(Wallet.user_id == current_user.id)
        )
        or 0
    )

    active_wallets_count = len(wallets)
    snapshot_dates = [
        balance_info[wallet.id].last_snapshot_at
        for wallet in wallets
        if balance_info[wallet.id].last_snapshot_at is not None
    ]
    last_snapshot_at = max(snapshot_dates) if snapshot_dates else None
    data_health = await build_portfolio_data_health(
        session,
        user_id=current_user.id,
        wallets=wallets,
        balance_info=balance_info,
    )
    wallet_source_status = _wallet_source_status(data_health)
    wallet_as_of = data_health.as_of
    data_health.exchange = _merge_exchange_health(data_health, exchange)
    all_snapshot_dates = [*snapshot_dates]
    if exchange.as_of is not None:
        all_snapshot_dates.append(exchange.as_of)
    last_snapshot_at = max(all_snapshot_dates) if all_snapshot_dates else None

    asset_totals: dict[str, Decimal] = {}
    for wallet in wallets:
        for asset in balance_info[wallet.id].assets:
            asset_totals[asset.symbol] = (
                asset_totals.get(asset.symbol, Decimal("0")) + asset.usd_value
            )
    if exchange.status == "success":
        for asset in exchange.assets:
            asset_totals[asset.symbol] = (
                asset_totals.get(asset.symbol, Decimal("0")) + asset.usd_value
            )
    ordered_assets = sorted(
        asset_totals.items(),
        key=lambda item: item[1],
        reverse=True,
    )[:5]
    top_assets = [
        AssetShare(
            symbol=symbol,
            usd_value=usd_value,
            share_pct=round(float(usd_value / total * 100), 2) if total else 0.0,
        )
        for symbol, usd_value in ordered_assets
    ]

    change_24h = await _portfolio_change_24h(
        session,
        wallets,
        current_total=total,
        balance_info=balance_info,
        price_quality=data_health.price_quality,
        health_state=data_health.state,
        freshness=data_health.freshness,
        chain_issues=data_health.chain_issues,
        has_exchange_assets=bool(exchange.assets),
    )

    wallet_assets_count = sum(
        asset.amount != Decimal("0")
        for wallet in wallets
        for asset in balance_info[wallet.id].assets
    )
    sources = [
        PortfolioSourceSummary(
            source="wallet",
            status=wallet_source_status,
            total_usd=wallet_total,
            assets_count=wallet_assets_count,
            as_of=wallet_as_of,
        )
    ]
    if exchange.configured:
        if exchange.status != "success":
            exchange_status = "unavailable"
        elif (
            data_health.exchange is not None and data_health.exchange.state == "partial"
        ):
            exchange_status = "partial"
        elif data_health.exchange is not None:
            exchange_status = data_health.exchange.freshness
        else:
            exchange_status = "unavailable"
        sources.append(
            PortfolioSourceSummary(
                source="exchange",
                provider=exchange.provider,
                status=exchange_status,
                total_usd=exchange.total_usd,
                assets_count=len(exchange.assets),
                as_of=exchange.as_of,
            )
        )

    return PortfolioSummary(
        total_usd=total,
        wallets_count=wallets_count,
        active_wallets_count=active_wallets_count,
        last_snapshot_at=last_snapshot_at,
        top_assets=top_assets,
        sources=sources,
        data_health=data_health,
        change_24h=change_24h,
    )
