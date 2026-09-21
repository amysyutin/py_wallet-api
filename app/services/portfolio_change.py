from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

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
    PortfolioPriceQuality,
    PortfolioValueChange24h,
)
from app.services.portfolio_health import WalletBalanceInfo, portfolio_price_quality

ChangeStatus = Literal["complete", "incomplete", "unavailable"]


def _snapshot_observed_at() -> ColumnElement[datetime]:
    return func.coalesce(
        WalletSnapshot.finished_at,
        SnapshotRun.finished_at,
        SnapshotRun.created_at,
    )


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


async def portfolio_change_24h(
    session: AsyncSession,
    wallets: list[Wallet],
    *,
    current_total: Decimal,
    balance_info: Mapping[int, WalletBalanceInfo],
    price_quality: PortfolioPriceQuality,
    health_state: str,
    freshness: str,
    chain_issues: list[PortfolioChainIssue],
    has_exchange_assets: bool = False,
    reference_at: datetime | None = None,
) -> PortfolioValueChange24h:
    reference = reference_at or datetime.now(timezone.utc)
    cutoff_at = reference - timedelta(hours=24)
    tolerance = timedelta(seconds=get_settings().portfolio_fresh_seconds)

    def result(
        status_value: ChangeStatus,
        reasons: list[str],
        *,
        start_usd: Decimal | None = None,
        start_times: list[datetime] | None = None,
        end_times: list[datetime] | None = None,
    ) -> PortfolioValueChange24h:
        absolute = current_total - start_usd if start_usd is not None else None
        percent = (
            round(float(absolute / start_usd * 100), 2)
            if absolute is not None
            and start_usd is not None
            and start_usd != Decimal("0")
            else None
        )
        return PortfolioValueChange24h(
            status=status_value,
            start_usd=start_usd,
            end_usd=current_total if wallets or has_exchange_assets else None,
            absolute_usd=absolute,
            percent=percent,
            reference_at=reference,
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
        reference - min(end_times) > tolerance
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
    baseline_quality = portfolio_price_quality(
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

    start_total = sum((row.total_usd for row in baseline_rows), Decimal("0"))
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
