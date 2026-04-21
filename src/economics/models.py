from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any

ECONOMIC_MODEL_VERSION = "v1.0.0"


class StrategyTrack(str, Enum):
    LEADER_SWING = "leader_swing"
    MICRO_REACTIVE = "micro_reactive"


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class LiquidityRole(str, Enum):
    MAKER = "maker"
    TAKER = "taker"


@dataclass(frozen=True)
class FeeSnapshot:
    market_id: str
    token_id: str
    fee_enabled: bool
    fee_rate: Decimal
    source: str
    captured_at: datetime
    maker_fee_rate: Decimal = Decimal("0")
    compatibility: dict[str, Any] = field(default_factory=dict)
    economic_model_version: str = ECONOMIC_MODEL_VERSION


@dataclass(frozen=True)
class CanonicalTrade:
    market_id: str
    token_id: str
    side: OrderSide
    price: Decimal
    size_shares: Decimal
    notional_usdc: Decimal
    exchange_ts: datetime
    observed_ts: datetime
    source: str
    raw_ref: dict[str, Any] = field(default_factory=dict)
    economic_model_version: str = ECONOMIC_MODEL_VERSION


@dataclass(frozen=True)
class CanonicalFill:
    strategy_track: StrategyTrack
    market_id: str
    token_id: str
    side: OrderSide
    liquidity_role: LiquidityRole
    price: Decimal
    size_shares: Decimal
    notional_usdc: Decimal
    fee_usdc: Decimal
    spread_cost_usdc: Decimal = Decimal("0")
    slippage_usdc: Decimal = Decimal("0")
    audit: dict[str, Any] = field(default_factory=dict)
    economic_model_version: str = ECONOMIC_MODEL_VERSION
