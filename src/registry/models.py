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
