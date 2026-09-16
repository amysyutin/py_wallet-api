from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class PortfolioHistorySources(BaseModel):
    onchain_usd: Decimal
    cex_usd: Decimal | None
    manual_usd: Decimal


class PortfolioPoint(BaseModel):
    snapshot_at: datetime
    total_usd: Decimal
    sources: PortfolioHistorySources


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


class AllocationTargetItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    asset_key: str = Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9:_\-.]+$")
    symbol: str = Field(min_length=1, max_length=32)
    target_pct: Decimal = Field(gt=0, le=100)

    @field_validator("asset_key", "symbol")
    @classmethod
    def strip_text(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("value cannot be blank")
        return stripped

    @field_validator("symbol")
    @classmethod
    def normalize_symbol(cls, value: str) -> str:
        return value.upper()

    @field_validator("target_pct")
    @classmethod
    def require_percentage_precision(cls, value: Decimal) -> Decimal:
        if value.as_tuple().exponent < -2:
            raise ValueError("target_pct supports at most two decimal places")
        return value.quantize(Decimal("0.01"))


class PortfolioAllocationTargets(BaseModel):
    items: list[AllocationTargetItem]


class PortfolioAllocationTargetsUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[AllocationTargetItem] = Field(max_length=50)

    @model_validator(mode="after")
    def validate_distribution(self) -> "PortfolioAllocationTargetsUpdate":
        keys = [item.asset_key for item in self.items]
        if len(keys) != len(set(keys)):
            raise ValueError("asset_key values must be unique")
        if "__other__" in keys:
            raise ValueError("the synthetic Other allocation cannot be targeted")
        total = sum((item.target_pct for item in self.items), Decimal("0"))
        if self.items and total != Decimal("100.00"):
            raise ValueError("allocation targets must add up to 100.00")
        return self


class PortfolioRebalancingItem(BaseModel):
    asset_key: str
    symbol: str
    current_usd: Decimal
    current_pct: float
    target_pct: float
    deviation_pct: float
    suggested_usd: Decimal
    action: Literal["increase", "reduce", "within_target"]


class PortfolioRebalancing(BaseModel):
    status: Literal["not_applicable", "not_configured", "empty", "ready", "incomplete"]
    tolerance_pct: float
    items: list[PortfolioRebalancingItem]


class PortfolioAllScope(BaseModel):
    mode: Literal["all"]


class PortfolioSelectionScope(BaseModel):
    mode: Literal["selection"]
    group_ids: list[int]
    include_ungrouped: bool


class PortfolioAllocationQuality(BaseModel):
    state: Literal["complete", "estimated", "incomplete", "unknown", "empty"]
    sources: list[
        Literal[
            "coingecko",
            "binance_usdt",
            "frankfurter",
            "manual",
            "static_dev",
            "unknown",
        ]
    ]
    assets_priced: int
    assets_total: int


class PortfolioAllocation(BaseModel):
    scope: PortfolioAllScope | PortfolioSelectionScope
    wallets_count: int
    total_usd: Decimal
    items: list[AllocationAssetShare]
    available_assets: list[AllocationAssetShare] = Field(default_factory=list)
    targets: list[AllocationTargetItem] = Field(default_factory=list)
    rebalancing: PortfolioRebalancing = Field(
        default_factory=lambda: PortfolioRebalancing(
            status="not_configured",
            tolerance_pct=1.0,
            items=[],
        )
    )
    data_quality: PortfolioAllocationQuality


class PortfolioChainIssue(BaseModel):
    chain: str
    status: str
    error_type: str | None = None
    wallets_count: int


class PortfolioPriceQuality(BaseModel):
    state: Literal["complete", "estimated", "incomplete", "unknown"]
    sources: list[
        Literal[
            "coingecko",
            "binance_usdt",
            "frankfurter",
            "manual",
            "static_dev",
            "unknown",
        ]
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
