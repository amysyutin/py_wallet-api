from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from decimal import Decimal
from typing import Literal, Protocol

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import get_settings
from app.db.models.snapshot_service import (
    ChainSnapshot,
    SnapshotBalanceSnapshot,
    SnapshotRun,
    WalletSnapshot,
)
from app.db.models.wallet import Wallet
from app.schemas.portfolio import (
    PortfolioChainIssue,
    PortfolioDataHealth,
    PortfolioPriceQuality,
)

ACTIVE_SNAPSHOT_STATUSES = ("pending", "running")
PriceSource = Literal[
    "coingecko",
    "binance_usdt",
    "frankfurter",
    "manual",
    "static_dev",
    "unknown",
]
PriceQualityState = Literal["complete", "estimated", "incomplete", "unknown"]
PortfolioFreshness = Literal["fresh", "aging", "stale", "unknown"]
PortfolioHealthState = Literal["fresh", "updating", "partial", "stale"]
PRICE_SOURCE_ORDER: tuple[PriceSource, ...] = (
    "coingecko",
    "binance_usdt",
    "frankfurter",
    "manual",
    "static_dev",
    "unknown",
)


def _normalize_price_source(source: str | None) -> PriceSource:
    if source == "coingecko":
        return "coingecko"
    if source == "binance_usdt":
        return "binance_usdt"
    if source == "frankfurter":
        return "frankfurter"
    if source == "manual":
        return "manual"
    if source == "static_dev":
        return "static_dev"
    return "unknown"


class PricedAssetInfo(Protocol):
    @property
    def amount(self) -> Decimal: ...

    @property
    def price_usd(self) -> Decimal | None: ...


class WalletBalanceInfo(Protocol):
    @property
    def balance_source(self) -> str: ...

    @property
    def last_snapshot_at(self) -> datetime | None: ...

    @property
    def wallet_snapshot_id(self) -> int | None: ...

    @property
    def assets(self) -> Sequence[PricedAssetInfo]: ...


async def active_canonical_wallets(
    session: AsyncSession,
    *,
    user_id: int,
    group_ids: list[int] | None = None,
    include_ungrouped: bool = True,
) -> list[Wallet]:
    """Load active wallets without counting duplicate legacy EVM rows twice."""
    normalized_address = func.lower(func.trim(Wallet.address))
    canonical_active_evm_ids = (
        select(func.min(Wallet.id))
        .where(
            Wallet.user_id == user_id,
            Wallet.wallet_type == "evm",
            Wallet.is_active.is_(True),
            Wallet.address.is_not(None),
        )
        .group_by(normalized_address)
    )
    query = select(Wallet).where(
        Wallet.user_id == user_id,
        Wallet.is_active.is_(True),
        or_(
            Wallet.wallet_type != "evm",
            Wallet.id.in_(canonical_active_evm_ids),
        ),
    )
    if group_ids is not None:
        scope_filters = []
        if group_ids:
            scope_filters.append(Wallet.group_id.in_(group_ids))
        if include_ungrouped:
            scope_filters.append(Wallet.group_id.is_(None))
        query = query.where(or_(*scope_filters))
    return list(await session.scalars(query.order_by(Wallet.id)))


def portfolio_freshness(
    as_of: datetime | None,
    *,
    fresh_seconds: int,
    stale_seconds: int,
    now: datetime | None = None,
) -> PortfolioFreshness:
    if as_of is None:
        return "unknown"
    if as_of.tzinfo is None:
        as_of = as_of.replace(tzinfo=timezone.utc)
    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    age_seconds = max(0.0, (current - as_of).total_seconds())
    if age_seconds <= fresh_seconds:
        return "fresh"
    if age_seconds <= stale_seconds:
        return "aging"
    return "stale"


def portfolio_price_quality(
    observations: list[tuple[Decimal, Decimal | None, str | None]],
) -> PortfolioPriceQuality:
    relevant = [
        (amount, price_usd, source)
        for amount, price_usd, source in observations
        if amount != Decimal("0")
    ]
    sources = {_normalize_price_source(source) for _, _, source in relevant}
    assets_priced = sum(price_usd is not None for _, price_usd, _ in relevant)
    assets_total = len(relevant)

    if assets_total == 0:
        quality_state: PriceQualityState = "unknown"
    elif assets_priced < assets_total:
        quality_state = "incomplete"
    elif "static_dev" in sources:
        quality_state = "estimated"
    elif "unknown" in sources:
        quality_state = "unknown"
    else:
        quality_state = "complete"

    return PortfolioPriceQuality(
        state=quality_state,
        sources=[source for source in PRICE_SOURCE_ORDER if source in sources],
        assets_priced=assets_priced,
        assets_total=assets_total,
    )


async def build_portfolio_data_health(
    session: AsyncSession,
    *,
    user_id: int,
    wallets: list[Wallet],
    balance_info: Mapping[int, WalletBalanceInfo],
    now: datetime | None = None,
) -> PortfolioDataHealth:
    """Build one persisted portfolio-health contract for API and Telegram."""
    snapshot_dates = [
        snapshot_at
        for wallet in wallets
        if (snapshot_at := balance_info[wallet.id].last_snapshot_at) is not None
    ]
    # The oldest automated source is the honest timestamp for the whole total.
    as_of = min(snapshot_dates) if snapshot_dates else None
    snapshot_wallets = sum(
        balance_info[wallet.id].balance_source == "latest_snapshot"
        for wallet in wallets
    )
    manual_wallets = sum(
        balance_info[wallet.id].balance_source == "manual" for wallet in wallets
    )
    missing_wallets = sum(
        balance_info[wallet.id].balance_source == "none" for wallet in wallets
    )
    wallets_covered = snapshot_wallets + manual_wallets

    refresh_in_progress = bool(
        await session.scalar(
            select(func.count())
            .select_from(SnapshotRun)
            .where(
                SnapshotRun.user_id == user_id,
                SnapshotRun.status.in_(ACTIVE_SNAPSHOT_STATUSES),
            )
        )
    )

    latest_snapshot_ids = [
        balance_info[wallet.id].wallet_snapshot_id
        for wallet in wallets
        if balance_info[wallet.id].wallet_snapshot_id is not None
    ]
    issue_rows = []
    if latest_snapshot_ids:
        issue_rows = list(
            await session.execute(
                select(
                    ChainSnapshot.chain,
                    ChainSnapshot.status,
                    ChainSnapshot.error_type,
                    ChainSnapshot.wallet_snapshot_id,
                    WalletSnapshot.snapshot_run_id,
                )
                .join(
                    WalletSnapshot,
                    WalletSnapshot.id == ChainSnapshot.wallet_snapshot_id,
                )
                .where(
                    ChainSnapshot.wallet_snapshot_id.in_(latest_snapshot_ids),
                    ChainSnapshot.status != "success",
                )
                .order_by(ChainSnapshot.chain, ChainSnapshot.wallet_snapshot_id)
            )
        )
    issue_by_chain: dict[str, dict[str, set]] = {}
    retryable_run_ids: set[int] = set()
    for row in issue_rows:
        issue = issue_by_chain.setdefault(
            row.chain,
            {"statuses": set(), "error_types": set(), "wallet_ids": set()},
        )
        issue["statuses"].add(row.status)
        if row.error_type:
            issue["error_types"].add(row.error_type)
        issue["wallet_ids"].add(row.wallet_snapshot_id)
        if row.status == "failed":
            retryable_run_ids.add(row.snapshot_run_id)
    chain_issues = []
    for chain, issue in sorted(issue_by_chain.items()):
        statuses = issue["statuses"]
        error_types = issue["error_types"]
        chain_issues.append(
            PortfolioChainIssue(
                chain=chain,
                status="failed" if "failed" in statuses else sorted(statuses)[0],
                error_type=(
                    next(iter(error_types))
                    if len(error_types) == 1
                    else ("multiple_errors" if error_types else None)
                ),
                wallets_count=len(issue["wallet_ids"]),
            )
        )

    price_observations: list[tuple[Decimal, Decimal | None, str | None]] = []
    if latest_snapshot_ids:
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
            .where(ChainSnapshot.wallet_snapshot_id.in_(latest_snapshot_ids))
        )
        price_observations.extend(
            (row.amount, row.price_usd, row.price_source) for row in price_rows
        )

    for wallet in wallets:
        info = balance_info[wallet.id]
        if info.wallet_snapshot_id is not None:
            continue
        source = "manual" if info.balance_source == "manual" else None
        price_observations.extend(
            (asset.amount, asset.price_usd, source) for asset in info.assets
        )
    price_quality = portfolio_price_quality(price_observations)

    settings = get_settings()
    freshness = portfolio_freshness(
        as_of,
        fresh_seconds=settings.portfolio_fresh_seconds,
        stale_seconds=settings.portfolio_stale_seconds,
        now=now,
    )
    if chain_issues or price_quality.state in {"estimated", "incomplete"}:
        health_state: PortfolioHealthState = "partial"
    elif refresh_in_progress:
        health_state = "updating"
    elif missing_wallets:
        health_state = "partial"
    elif freshness == "stale":
        health_state = "stale"
    else:
        health_state = "fresh"

    return PortfolioDataHealth(
        state=health_state,
        freshness=freshness,
        as_of=as_of,
        wallets_covered=wallets_covered,
        wallets_total=len(wallets),
        snapshot_wallets=snapshot_wallets,
        manual_wallets=manual_wallets,
        missing_wallets=missing_wallets,
        refresh_in_progress=refresh_in_progress,
        retryable_job_id=(
            next(iter(retryable_run_ids)) if len(retryable_run_ids) == 1 else None
        ),
        chain_issues=chain_issues,
        price_quality=price_quality,
    )
