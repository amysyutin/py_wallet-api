from datetime import datetime
from decimal import Decimal
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.config import CHAIN_RPC

EVM_AGGREGATE_CHAIN = "all"
SOLANA_CHAIN = "solana"
SUPPORTED_CHAINS = set(CHAIN_RPC) | {EVM_AGGREGATE_CHAIN}
_EVM_ADDRESS_RE = re.compile(r"^0[xX][a-fA-F0-9]{40}$")
_BASE58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_BASE58_VALUES = {character: index for index, character in enumerate(_BASE58_ALPHABET)}


def normalize_evm_address(address: str | None) -> str | None:
    if address is None:
        return None
    normalized = address.strip()
    if not normalized:
        return normalized
    if _EVM_ADDRESS_RE.fullmatch(normalized) is None:
        raise ValueError("address must be a 20-byte 0x-prefixed EVM address")
    return f"0x{normalized[2:]}"


def normalize_solana_address(address: str | None) -> str | None:
    if address is None:
        return None
    normalized = address.strip()
    if not normalized:
        return normalized

    try:
        number = 0
        for character in normalized:
            number = number * 58 + _BASE58_VALUES[character]
    except KeyError:
        raise ValueError("address must be a base58-encoded Solana public key") from None

    decoded = (
        number.to_bytes((number.bit_length() + 7) // 8, byteorder="big")
        if number
        else b""
    )
    leading_zeroes = len(normalized) - len(normalized.lstrip("1"))
    if len(b"\0" * leading_zeroes + decoded) != 32:
        raise ValueError("address must decode to a 32-byte Solana public key")
    return normalized


def normalize_wallet_address(wallet_type: str, address: str | None) -> str | None:
    if wallet_type == "evm":
        return normalize_evm_address(address)
    if wallet_type == "solana":
        return normalize_solana_address(address)
    return address


def validate_wallet_network_state(
    wallet_type: str, chain_type: str, address: str | None
) -> None:
    if wallet_type == "evm":
        if not address or not address.strip():
            raise ValueError("address is required for EVM wallets")
        if chain_type == "manual":
            raise ValueError("chain_type cannot be 'manual' for EVM wallets")
        if chain_type not in SUPPORTED_CHAINS:
            raise ValueError(f"chain_type must be one of {sorted(SUPPORTED_CHAINS)}")
    elif wallet_type == "solana":
        if not address or not address.strip():
            raise ValueError("address is required for Solana wallets")
        if chain_type != SOLANA_CHAIN:
            raise ValueError("chain_type must be 'solana' for Solana wallets")
    elif wallet_type == "manual":
        if chain_type != "manual":
            raise ValueError("chain_type must be 'manual' for manual wallets")
        if address is not None:
            raise ValueError("manual wallets cannot have an address")


class WalletCreate(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    label: str = Field(min_length=1, max_length=100)
    wallet_type: Literal["evm", "solana", "manual"] = "evm"
    address: str | None = Field(default=None, max_length=44)
    chain_type: str = Field(
        default=EVM_AGGREGATE_CHAIN,
        min_length=1,
        max_length=32,
    )
    group_id: int | None = None
    notes: str | None = Field(default=None, max_length=500)

    @field_validator("chain_type")
    @classmethod
    def normalize_chain_type(cls, value: str) -> str:
        return value.lower()

    @model_validator(mode="after")
    def validate_wallet_type_rules(self) -> "WalletCreate":
        self.address = normalize_wallet_address(self.wallet_type, self.address)
        validate_wallet_network_state(self.wallet_type, self.chain_type, self.address)
        return self


class WalletUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    label: str | None = Field(default=None, min_length=1, max_length=100)
    address: str | None = Field(default=None, max_length=44)
    chain_type: str | None = Field(default=None, min_length=1, max_length=32)
    group_id: int | None = None
    is_active: bool | None = None
    notes: str | None = Field(default=None, max_length=500)

    @field_validator("address")
    @classmethod
    def normalize_address_whitespace(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else None

    @field_validator("chain_type")
    @classmethod
    def normalize_chain_type(cls, value: str | None) -> str | None:
        return value.lower() if value is not None else None

    @model_validator(mode="after")
    def reject_null_for_required_fields(self) -> "WalletUpdate":
        for field_name in ("label", "chain_type", "is_active"):
            if (
                field_name in self.model_fields_set
                and getattr(self, field_name) is None
            ):
                raise ValueError(f"{field_name} cannot be null")
        return self


class WalletRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    label: str
    address: str | None
    chain_type: str
    wallet_type: str
    group_id: int | None
    is_active: bool
    notes: str | None
    created_at: datetime
    updated_at: datetime


class WalletTopAsset(BaseModel):
    symbol: str
    amount: Decimal
    usd_value: Decimal


class WalletAssetDetail(BaseModel):
    symbol: str
    chain: str
    amount: Decimal
    usd_value: Decimal
    price_usd: Decimal | None = None


class WalletSummaryRead(BaseModel):
    id: int
    label: str
    wallet_type: str
    chain_type: str
    address: str | None
    group_id: int | None
    group_name: str | None = None
    is_active: bool
    balance_usd: Decimal
    balance_source: Literal["latest_snapshot", "manual", "none"]
    last_snapshot_at: datetime | None
    balances_count: int
    top_assets: list[WalletTopAsset]
    created_at: datetime
    updated_at: datetime


class WalletChainIssue(BaseModel):
    chain: str
    status: str
    error_type: str | None = None


class WalletPriceQuality(BaseModel):
    state: Literal["complete", "estimated", "incomplete", "unknown"]
    sources: list[
        Literal["coingecko", "frankfurter", "manual", "static_dev", "unknown"]
    ]
    assets_priced: int
    assets_total: int


class WalletDataHealth(BaseModel):
    state: Literal["fresh", "updating", "partial", "stale"]
    freshness: Literal["fresh", "aging", "stale", "unknown"]
    as_of: datetime | None = None
    source: Literal["latest_snapshot", "manual", "none"]
    refresh_in_progress: bool
    chain_issues: list[WalletChainIssue]
    price_quality: WalletPriceQuality


class WalletDetailSummary(BaseModel):
    wallet: WalletRead
    balance_usd: Decimal
    last_snapshot_at: datetime | None
    assets: list[WalletAssetDetail]
    data_health: WalletDataHealth


class WalletSnapshotRead(BaseModel):
    id: int
    snapshot_run_id: int
    status: str
    total_usd: Decimal
    snapshot_at: datetime
