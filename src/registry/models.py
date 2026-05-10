"""
Pydantic models for Falcon API responses and internal Leader dataclass.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from pydantic import AliasChoices, BaseModel, ConfigDict, Field


class FalconLeaderEntry(BaseModel):
    """One row from agent 584 (Falcon Score Leaderboard)."""

    wallet_address: str = Field(validation_alias=AliasChoices("wallet_address", "wallet"))
    falcon_score: float = Field(
        default=0.0,
        validation_alias=AliasChoices("falcon_score", "h_score"),
    )
    model_config = ConfigDict(extra="ignore", populate_by_name=True)


class WalletMetrics(BaseModel):
    """Raw metrics from agent 581 (Wallet 360). All extra fields captured."""

    wallet_address: str = Field(validation_alias=AliasChoices("wallet_address", "proxy_wallet"))
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    def to_dict(self) -> dict:
        return {"wallet_address": self.wallet_address, **self.model_extra}


class PnlLeaderEntry(BaseModel):
    wallet_address: str = Field(validation_alias=AliasChoices("wallet_address", "address"))
    profit: float = Field(
        default=0.0,
        validation_alias=AliasChoices("profit", "total_pnl"),
    )
    model_config = ConfigDict(extra="ignore", populate_by_name=True)


class MarketInsights(BaseModel):
    """One row from agent 575 (Polymarket Market Insights).

    The audited methodology for `markets.liquidity_score` (master
    CLAUDE.md §6, `src/profiler/CLAUDE.md:172`, `error_model.py:83`)
    expects this agent's normalized 0–1 liquidity score, NOT agent
    574's raw `liquidity` field. Field aliases below cover the names
    we've seen Falcon use across agents for "liquidity"-like signals;
    any non-finite or out-of-range value is coerced to a sane 0–1
    score in `FalconClient.get_market_insights`.

    `extra="allow"` keeps the rest of the payload (concentration,
    trend, depth, etc.) on the model so a future migration can add
    new features without a model bump.
    """

    condition_id: str | None = Field(
        default=None,
        validation_alias=AliasChoices("condition_id", "market_id", "market_slug"),
    )
    liquidity_score: float = Field(
        default=0.0,
        validation_alias=AliasChoices(
            "liquidity_score",  # 575's documented field
            "normalized_liquidity",  # observed alias
            "liquidity",  # ultra-fallback if 575 reuses 574's name
        ),
    )
    model_config = ConfigDict(extra="allow", populate_by_name=True)


class LeaderClassification(BaseModel):
    strategy: str = "unknown"  # directional | structural | cognitive | unknown
    influence: str = "unknown"  # whale | top_trader | community | unknown
    horizon: str = "unknown"  # scalper | swing | holder | unknown
    copiable: bool = True
    classified_at: str = ""  # ISO timestamp


@dataclass
class Leader:
    wallet_address: str
    falcon_score: float = 0.0
    wallet360_json: dict | None = None
    classification_json: dict | None = None
    first_seen: datetime | None = None
    last_refresh: datetime | None = None
    on_watchlist: bool = True
    excluded: bool = False
    exclude_reason: str | None = None

    @classmethod
    def from_row(cls, record: Any) -> "Leader":
        return cls(
            wallet_address=record["wallet_address"],
            falcon_score=float(record["falcon_score"] or 0),
            wallet360_json=record["wallet360_json"],
            classification_json=record["classification_json"],
            first_seen=record["first_seen"],
            last_refresh=record["last_refresh"],
            on_watchlist=bool(record["on_watchlist"]),
            excluded=bool(record["excluded"]),
            exclude_reason=record["exclude_reason"],
        )
