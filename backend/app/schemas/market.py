from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel


class TokenOut(BaseModel):
    id: str
    outcome: str


class MarketOut(BaseModel):
    id: str
    event_id: str
    condition_id: str | None
    slug: str | None
    question: str
    outcomes: list[str]
    tags: list[str]
    status: str
    active: bool
    resolved: bool
    tokens: list[TokenOut] = []


class EventOut(BaseModel):
    id: str
    slug: str | None
    title: str
    active: bool
    resolved: bool


class TradeOut(BaseModel):
    id: str
    market_id: str
    token_id: str
    side: str
    price: Decimal
    size: Decimal
    traded_at: datetime


class TopOfBookOut(BaseModel):
    market_id: str
    token_id: str
    best_bid: Decimal | None
    best_ask: Decimal | None
    mid_price: Decimal | None
    spread: Decimal | None
    observed_at: datetime


class MarketSummary(BaseModel):
    market_id: str
    latest_mid_price: Decimal | None
    avg_spread: Decimal | None
    volume_24h: Decimal
    implied_probability: float | None
    consistency_flag: bool


class SyncStatusOut(BaseModel):
    job_name: str
    status: str
    last_success_at: datetime | None
    last_error: str | None
    meta_info: dict | None


class BotTradeOut(BaseModel):
    id: str
    market_id: str
    market_title: str
    outcome: str
    side: str
    price: Decimal
    size: Decimal
    notional: Decimal
    pnl_abs: Decimal
    pnl_pct: Decimal
    status: str
    executed_at: datetime


class PortfolioSnapshotOut(BaseModel):
    total_equity: Decimal
    capital_in_trade: Decimal
    pnl_abs: Decimal
    pnl_pct: Decimal
    observed_at: datetime
