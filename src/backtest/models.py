from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from src.economics.models import ECONOMIC_MODEL_VERSION, FeeSnapshot, StrategyTrack


@dataclass(frozen=True)
class BacktestTrade:
    leader_wallet: str
    market_id: str
    token_id: str
    side: str
    outcome: str
    price: Decimal
    size_shares: Decimal
    event_ts: datetime
    observed_ts: datetime
    tx_hash: str
    source: str = "fixture"
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class BacktestMarket:
    market_id: str
    question: str
    category: str
    yes_token_id: str
    no_token_id: str
    volume_usdc: Decimal
    fee_snapshot: FeeSnapshot
    liquidity_score: Decimal = Decimal("0.5")

    def fee_snapshot_for(self, token_id: str) -> FeeSnapshot:
        if self.fee_snapshot.token_id == token_id:
            return self.fee_snapshot
        return FeeSnapshot(
            market_id=self.fee_snapshot.market_id,
            token_id=token_id,
            fee_enabled=self.fee_snapshot.fee_enabled,
            fee_rate=self.fee_snapshot.fee_rate,
            source=self.fee_snapshot.source,
            captured_at=self.fee_snapshot.captured_at,
            maker_fee_rate=self.fee_snapshot.maker_fee_rate,
            compatibility=dict(self.fee_snapshot.compatibility),
            economic_model_version=self.fee_snapshot.economic_model_version,
        )

    def opposite_token_id(self, token_id: str) -> str:
        if token_id == self.yes_token_id:
            return self.no_token_id
        if token_id == self.no_token_id:
            return self.yes_token_id
        return self.no_token_id


@dataclass(frozen=True)
class BacktestBookSnapshot:
    market_id: str
    token_id: str
    best_bid: Decimal
    best_ask: Decimal
    ts: datetime
    source: str


@dataclass(frozen=True)
class BacktestCandle:
    market_id: str
    token_id: str
    start_ts: datetime
    end_ts: datetime
    high: Decimal
    low: Decimal
    source: str


@dataclass(frozen=True)
class BacktestFill:
    strategy_track: StrategyTrack
    action: str
    market_id: str
    token_id: str
    leader_wallet: str
    entry_tx_hash: str
    exit_tx_hash: str
    entry_ts: datetime
    exit_ts: datetime
    entry_price: Decimal
    exit_price: Decimal
    size_shares: Decimal
    notional_usdc: Decimal
    gross_pnl_usdc: Decimal
    net_pnl_usdc: Decimal
    fee_usdc: Decimal
    spread_cost_usdc: Decimal
    slippage_usdc: Decimal
    signal_audit: dict[str, Any]
    cost_sources: dict[str, str]
    economic_model_version: str = ECONOMIC_MODEL_VERSION


@dataclass(frozen=True)
class BacktestRun:
    strategy_track: StrategyTrack
    policy: str
    fills: list[BacktestFill]
    metrics: dict[str, Any]
    economic_model_version: str = ECONOMIC_MODEL_VERSION
