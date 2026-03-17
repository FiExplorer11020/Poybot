from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import Enum

from sqlalchemy import JSON, DateTime, Enum as SAEnum, ForeignKey, Index, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class MarketStatus(str, Enum):
    active = "active"
    closed = "closed"
    resolved = "resolved"


class Event(Base):
    __tablename__ = "events"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    slug: Mapped[str | None] = mapped_column(String(255), index=True)
    title: Mapped[str] = mapped_column(String(512))
    description: Mapped[str | None] = mapped_column(Text())
    active: Mapped[bool] = mapped_column(default=True, index=True)
    resolved: Mapped[bool] = mapped_column(default=False, index=True)
    starts_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ends_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow
    )

    markets: Mapped[list[Market]] = relationship(back_populates="event", cascade="all,delete")


class Market(Base):
    __tablename__ = "markets"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    event_id: Mapped[str] = mapped_column(ForeignKey("events.id", ondelete="CASCADE"), index=True)
    condition_id: Mapped[str | None] = mapped_column(String(128), index=True)
    slug: Mapped[str | None] = mapped_column(String(255), index=True)
    question: Mapped[str] = mapped_column(String(512))
    outcomes: Mapped[list[str]] = mapped_column(JSON, default=list)
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    status: Mapped[MarketStatus] = mapped_column(SAEnum(MarketStatus), default=MarketStatus.active, index=True)
    active: Mapped[bool] = mapped_column(default=True, index=True)
    resolved: Mapped[bool] = mapped_column(default=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=datetime.utcnow, onupdate=datetime.utcnow
    )

    event: Mapped[Event] = relationship(back_populates="markets")
    tokens: Mapped[list[Token]] = relationship(back_populates="market", cascade="all,delete")


class Token(Base):
    __tablename__ = "tokens"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    market_id: Mapped[str] = mapped_column(ForeignKey("markets.id", ondelete="CASCADE"), index=True)
    outcome: Mapped[str] = mapped_column(String(64))

    market: Mapped[Market] = relationship(back_populates="tokens")


class RawMetadataSnapshot(Base):
    __tablename__ = "raw_metadata_snapshots"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(64), index=True)
    payload: Mapped[dict] = mapped_column(JSON)
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow, index=True)


class RawWebsocketMessage(Base):
    __tablename__ = "raw_websocket_messages"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    channel: Mapped[str] = mapped_column(String(64), index=True)
    market_id: Mapped[str | None] = mapped_column(String(128), index=True)
    payload: Mapped[dict] = mapped_column(JSON)
    ingested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=datetime.utcnow, index=True)


class Trade(Base):
    __tablename__ = "trades"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    market_id: Mapped[str] = mapped_column(ForeignKey("markets.id", ondelete="CASCADE"), index=True)
    token_id: Mapped[str] = mapped_column(String(128), index=True)
    side: Mapped[str] = mapped_column(String(16))
    price: Mapped[Decimal] = mapped_column(Numeric(18, 8))
    size: Mapped[Decimal] = mapped_column(Numeric(18, 8))
    traded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class TopOfBook(Base):
    __tablename__ = "top_of_book"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    market_id: Mapped[str] = mapped_column(ForeignKey("markets.id", ondelete="CASCADE"), index=True)
    token_id: Mapped[str] = mapped_column(String(128), index=True)
    best_bid: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    best_ask: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    mid_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 8), index=True)
    spread: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    observed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)


class SyncJobStatus(Base):
    __tablename__ = "sync_job_status"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    job_name: Mapped[str] = mapped_column(String(128), unique=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text())
    meta_info: Mapped[dict | None] = mapped_column("metadata", JSON)


Index("ix_top_of_book_market_time", TopOfBook.market_id, TopOfBook.observed_at)
Index("ix_trades_market_time", Trade.market_id, Trade.traded_at)
