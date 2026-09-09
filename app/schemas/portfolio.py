from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel


class PortfolioPoint(BaseModel):
    snapshot_at: datetime
    total_usd: Decimal


class PortfolioHistory(BaseModel):
    wallet_id: int | None = None
    group_id: int | None = None
    days: int
    points: list[PortfolioPoint]


class AssetShare(BaseModel):
    symbol: str
    usd_value: Decimal
    share_pct: float


class AllocationAssetShare(AssetShare):
    asset_key: str


class PortfolioAllScope(BaseModel):
    mode: Literal["all"]


class PortfolioSelectionScope(BaseModel):
    mode: Literal["selection"]
    group_ids: list[int]
    include_ungrouped: bool


class PortfolioAllocationQuality(BaseModel):
    state: Literal["complete", "estimated", "incomplete", "unknown", "empty"]
    sources: list[
        Literal["coingecko", "frankfurter", "manual", "static_dev", "unknown"]
    ]
    assets_priced: int
    assets_total: int


class PortfolioAllocation(BaseModel):
    scope: PortfolioAllScope | PortfolioSelectionScope
    wallets_count: int
    total_usd: Decimal
    items: list[AllocationAssetShare]
    data_quality: PortfolioAllocationQuality


class PortfolioChainIssue(BaseModel):
    chain: str
    status: str
    error_type: str | None = None
    wallets_count: int


class PortfolioPriceQuality(BaseModel):
    state: Literal["complete", "estimated", "incomplete", "unknown"]
    sources: list[
        Literal["coingecko", "frankfurter", "manual", "static_dev", "unknown"]
    ]
    assets_priced: int
    assets_total: int


class PortfolioExchangeHealth(BaseModel):
    source: Literal["exchange"] = "exchange"
    provider: Literal["binance"]
    state: Literal["fresh", "partial", "stale", "unavailable"]
    freshness: Literal["fresh", "aging", "stale", "unknown"]
    as_of: datetime | None = None
    assets_priced: int
    assets_total: int
    error_type: (
        Literal["not_found", "timeout", "unavailable", "invalid_response"] | None
    ) = None


class PortfolioDataHealth(BaseModel):
    state: Literal["fresh", "updating", "partial", "stale"]
    freshness: Literal["fresh", "aging", "stale", "unknown"]
    as_of: datetime | None = None
    wallets_covered: int
    wallets_total: int
    snapshot_wallets: int
    manual_wallets: int
    missing_wallets: int
    refresh_in_progress: bool
    retryable_job_id: int | None = None
    chain_issues: list[PortfolioChainIssue]
    price_quality: PortfolioPriceQuality
    exchange: PortfolioExchangeHealth | None = None


class PortfolioSourceSummary(BaseModel):
    source: Literal["wallet", "exchange"]
    provider: Literal["binance"] | None = None
    status: Literal["fresh", "aging", "updating", "partial", "stale", "unavailable"]
    total_usd: Decimal
    assets_count: int
    as_of: datetime | None = None


class PortfolioValueChange24h(BaseModel):
    status: Literal["complete", "incomplete", "unavailable"]
    kind: Literal["value_change"] = "value_change"
    start_usd: Decimal | None = None
    end_usd: Decimal | None = None
    absolute_usd: Decimal | None = None
    percent: float | None = None
    reference_at: datetime
    cutoff_at: datetime
    start_observed_from: datetime | None = None
    start_observed_to: datetime | None = None
    end_observed_from: datetime | None = None
    end_observed_to: datetime | None = None
    reason_codes: list[str]


class PortfolioSummary(BaseModel):
    total_usd: Decimal
    wallets_count: int
    active_wallets_count: int
    last_snapshot_at: datetime | None = None
    top_assets: list[AssetShare]
    sources: list[PortfolioSourceSummary]
    data_health: PortfolioDataHealth
    change_24h: PortfolioValueChange24h
