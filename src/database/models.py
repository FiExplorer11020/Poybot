"""
Dataclasses mapping to the 9 DB tables defined in CLAUDE.md section 5.
Each has from_row(record) and to_dict() methods.
"""

from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any


@dataclass
class Market:
    market_id: str
    question: str
    category: str | None = None
    token_yes: str | None = None
    token_no: str | None = None
    end_date: datetime | None = None
    last_price_yes: Decimal | None = None
    last_price_no: Decimal | None = None
    volume_24h: Decimal = Decimal("0")
    active: bool = True
    created_at: datetime | None = None
    updated_at: datetime | None = None

    @classmethod
    def from_row(cls, record: Any) -> "Market":
        return cls(
            market_id=record["market_id"],
            question=record["question"],
            category=record["category"],
            token_yes=record["token_yes"],
            token_no=record["token_no"],
            end_date=record["end_date"],
            last_price_yes=record["last_price_yes"],
            last_price_no=record["last_price_no"],
            volume_24h=record["volume_24h"] or Decimal("0"),
            active=record["active"],
            created_at=record["created_at"],
            updated_at=record["updated_at"],
        )

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("created_at", None)
        d.pop("updated_at", None)
        return d


@dataclass
class Wallet:
    address: str
    first_seen: datetime | None = None
    last_active: datetime | None = None
    leaderboard_rank: int | None = None
    leaderboard_pnl: Decimal | None = None
    leaderboard_volume: Decimal | None = None
    whale_flag: bool = False
    leader_score: Decimal = Decimal("0")
    leader_type: str | None = None
    on_watchlist: bool = False

    @classmethod
    def from_row(cls, record: Any) -> "Wallet":
        return cls(
            address=record["address"],
            first_seen=record["first_seen"],
            last_active=record["last_active"],
            leaderboard_rank=record["leaderboard_rank"],
            leaderboard_pnl=record["leaderboard_pnl"],
            leaderboard_volume=record["leaderboard_volume"],
            whale_flag=record["whale_flag"],
            leader_score=record["leader_score"] or Decimal("0"),
            leader_type=record["leader_type"],
            on_watchlist=record["on_watchlist"],
        )

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Trade:
    time: datetime
    market_id: str
    token_id: str
    price: Decimal
    size: Decimal
    trade_id: str | None = None
    wallet_address: str | None = None
    side: str | None = None
    fee_rate_bps: int | None = None
    maker_order_id: str | None = None
    taker_order_id: str | None = None

    @classmethod
    def from_row(cls, record: Any) -> "Trade":
        return cls(
            time=record["time"],
            market_id=record["market_id"],
            token_id=record["token_id"],
            price=record["price"],
            size=record["size"],
            trade_id=record["trade_id"],
            wallet_address=record["wallet_address"],
            side=record["side"],
            fee_rate_bps=record["fee_rate_bps"],
            maker_order_id=record["maker_order_id"],
            taker_order_id=record["taker_order_id"],
        )

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class OrderbookSnapshot:
    time: datetime
    market_id: str
    token_id: str
    bids: list | None = None
    asks: list | None = None
    best_bid: Decimal | None = None
    best_ask: Decimal | None = None
    spread: Decimal | None = None
    mid_price: Decimal | None = None

    @classmethod
    def from_row(cls, record: Any) -> "OrderbookSnapshot":
        return cls(
            time=record["time"],
            market_id=record["market_id"],
            token_id=record["token_id"],
            bids=record["bids"],
            asks=record["asks"],
            best_bid=record["best_bid"],
            best_ask=record["best_ask"],
            spread=record["spread"],
            mid_price=record["mid_price"],
        )

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class VolumeSpike:
    time: datetime
    market_id: str
    token_id: str | None = None
    volume_window_s: int | None = None
    volume_spike: Decimal | None = None
    volume_baseline: Decimal | None = None
    z_score: Decimal | None = None
    attributed: bool = False

    @classmethod
    def from_row(cls, record: Any) -> "VolumeSpike":
        return cls(
            time=record["time"],
            market_id=record["market_id"],
            token_id=record["token_id"],
            volume_window_s=record["volume_window_s"],
            volume_spike=record["volume_spike"],
            volume_baseline=record["volume_baseline"],
            z_score=record["z_score"],
            attributed=record["attributed"],
        )

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class LeaderEvent:
    time: datetime
    market_id: str
    initiator_wallet: str
    id: int | None = None
    order_size: Decimal | None = None
    induced_volume: Decimal | None = None
    follower_count: int | None = None
    delay_p50_ms: int | None = None
    event_type: str | None = None
    spike_z_score: Decimal | None = None

    @classmethod
    def from_row(cls, record: Any) -> "LeaderEvent":
        return cls(
            id=record["id"],
            time=record["time"],
            market_id=record["market_id"],
            initiator_wallet=record["initiator_wallet"],
            order_size=record["order_size"],
            induced_volume=record["induced_volume"],
            follower_count=record["follower_count"],
            delay_p50_ms=record["delay_p50_ms"],
            event_type=record["event_type"],
            spike_z_score=record["spike_z_score"],
        )

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("id", None)
        return d


@dataclass
class WalletCluster:
    detected_at: datetime
    market_id: str
    id: int | None = None
    leader_wallet: str | None = None
    follower_wallets: list | None = None
    cluster_size: int | None = None
    total_volume: Decimal | None = None
    window_s: int | None = None
    confidence: Decimal | None = None

    @classmethod
    def from_row(cls, record: Any) -> "WalletCluster":
        return cls(
            id=record["id"],
            detected_at=record["detected_at"],
            market_id=record["market_id"],
            leader_wallet=record["leader_wallet"],
            follower_wallets=record["follower_wallets"],
            cluster_size=record["cluster_size"],
            total_volume=record["total_volume"],
            window_s=record["window_s"],
            confidence=record["confidence"],
        )

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("id", None)
        return d


@dataclass
class LeaderScore:
    time: datetime
    wallet_address: str
    score_total: Decimal | None = None
    score_volume_impact: Decimal | None = None
    score_frequency: Decimal | None = None
    score_follower_magnitude: Decimal | None = None
    score_repeatability: Decimal | None = None
    score_leaderboard: Decimal | None = None
    events_7d: int | None = None
    induced_volume_7d: Decimal | None = None

    @classmethod
    def from_row(cls, record: Any) -> "LeaderScore":
        return cls(
            time=record["time"],
            wallet_address=record["wallet_address"],
            score_total=record["score_total"],
            score_volume_impact=record["score_volume_impact"],
            score_frequency=record["score_frequency"],
            score_follower_magnitude=record["score_follower_magnitude"],
            score_repeatability=record["score_repeatability"],
            score_leaderboard=record["score_leaderboard"],
            events_7d=record["events_7d"],
            induced_volume_7d=record["induced_volume_7d"],
        )

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class PaperTrade:
    opened_at: datetime
    market_id: str
    token_id: str
    direction: str
    entry_price: Decimal
    size_usdc: Decimal
    id: int | None = None
    closed_at: datetime | None = None
    exit_price: Decimal | None = None
    pnl_usdc: Decimal | None = None
    signal_type: str | None = None
    leader_wallet: str | None = None
    status: str = "open"
    close_reason: str | None = None

    @classmethod
    def from_row(cls, record: Any) -> "PaperTrade":
        return cls(
            id=record["id"],
            opened_at=record["opened_at"],
            closed_at=record["closed_at"],
            market_id=record["market_id"],
            token_id=record["token_id"],
            direction=record["direction"],
            entry_price=record["entry_price"],
            exit_price=record["exit_price"],
            size_usdc=record["size_usdc"],
            pnl_usdc=record["pnl_usdc"],
            signal_type=record["signal_type"],
            leader_wallet=record["leader_wallet"],
            status=record["status"],
            close_reason=record["close_reason"],
        )

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("id", None)
        return d
