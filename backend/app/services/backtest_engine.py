from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from math import sqrt
from statistics import stdev
from typing import Literal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Event, Market, Token, TopOfBook, Trade
from app.services.adaptive_strategy import AdaptiveStrategyEngine, PortfolioState, RiskConfig
from app.services.spread_arb_scanner import (
    SpreadArbScanner,
    TopOfBookData as SpreadTopOfBookData,
)

StrategyName = Literal["latency_arb", "spread_arb", "adaptive"]
SlippageModel = Literal["fixed", "spread_pct"]
PositionSide = Literal["BUY_YES", "BUY_NO", "SPREAD_ARB"]
OutcomeName = Literal["YES", "NO"]

_YES_LABELS = {"YES", "Y"}
_NO_LABELS = {"NO", "N"}
_FIXED_SLIPPAGE = 0.001
_MARKET_MARKOUT_FALLBACK = 0.5


@dataclass(slots=True)
class BacktestConfig:
    start_date: datetime
    end_date: datetime
    initial_equity: float = 1000.0
    strategy: StrategyName = "adaptive"
    risk_cfg: RiskConfig = field(default_factory=RiskConfig)
    slippage_model: SlippageModel = "spread_pct"
    fee_bps: float = 8.0
    market_ids: list[str] | None = None


@dataclass(slots=True)
class BacktestTrade:
    market_id: str
    question: str
    strategy: StrategyName
    side: PositionSide
    token_id: str | None
    entry_time: datetime
    exit_time: datetime
    entry_price: float
    exit_price: float
    size: float
    notional: float
    fees: float
    pnl: float
    pnl_pct: float
    expected_edge: float
    risk_pct: float
    duration_h: float
    resolved_outcome: OutcomeName | None
    settlement: str


@dataclass(slots=True)
class BacktestResult:
    total_trades: int
    winning_trades: int
    win_rate: float
    total_pnl: float
    total_pnl_pct: float
    max_drawdown: float
    max_drawdown_pct: float
    sharpe_ratio: float
    profit_factor: float
    avg_trade_duration_h: float
    equity_curve: list[dict]
    trades: list[BacktestTrade]


@dataclass(slots=True)
class _BookTick:
    market_id: str
    token_id: str
    outcome: OutcomeName | None
    best_bid: float | None
    best_ask: float | None
    mid_price: float | None
    spread: float | None
    observed_at: datetime

    @property
    def effective_spread(self) -> float:
        if self.spread is not None:
            return max(0.0, self.spread)
        if self.best_bid is None or self.best_ask is None:
            return 0.0
        return max(0.0, self.best_ask - self.best_bid)

    @property
    def effective_mid(self) -> float | None:
        if self.mid_price is not None:
            return self.mid_price
        if self.best_bid is None and self.best_ask is None:
            return None
        if self.best_bid is None:
            return self.best_ask
        if self.best_ask is None:
            return self.best_bid
        return (self.best_bid + self.best_ask) / 2


@dataclass(slots=True)
class _TapeTrade:
    market_id: str
    token_id: str
    outcome: OutcomeName | None
    side: str
    price: float
    size: float
    traded_at: datetime


@dataclass(slots=True)
class _MarketContext:
    market_id: str
    question: str
    resolved: bool
    ends_at: datetime | None
    yes_token_id: str | None = None
    no_token_id: str | None = None
    latest_yes: _BookTick | None = None
    latest_no: _BookTick | None = None
    latest_yes_trade: _TapeTrade | None = None
    latest_no_trade: _TapeTrade | None = None

    def update_book(self, tick: _BookTick) -> None:
        if tick.outcome == "YES":
            self.latest_yes = tick
            if self.yes_token_id is None:
                self.yes_token_id = tick.token_id
        elif tick.outcome == "NO":
            self.latest_no = tick
            if self.no_token_id is None:
                self.no_token_id = tick.token_id

    def update_trade(self, trade: _TapeTrade) -> None:
        if trade.outcome == "YES":
            self.latest_yes_trade = trade
        elif trade.outcome == "NO":
            self.latest_no_trade = trade

    def quote_for(self, outcome: OutcomeName) -> _BookTick | None:
        if outcome == "YES":
            return self.latest_yes or self._complement_of(self.latest_no, outcome="YES")
        return self.latest_no or self._complement_of(self.latest_yes, outcome="NO")

    def mark_for(self, outcome: OutcomeName) -> float | None:
        quote = self.quote_for(outcome)
        if quote is not None and quote.effective_mid is not None:
            return _clip_probability(quote.effective_mid)
        return None

    def infer_resolved_outcome(self) -> OutcomeName | None:
        yes_score = self.mark_for("YES")
        no_score = self.mark_for("NO")

        if yes_score is not None and no_score is not None:
            if yes_score == no_score:
                return None
            return "YES" if yes_score > no_score else "NO"

        if yes_score is not None:
            return "YES" if yes_score >= 0.5 else "NO"
        if no_score is not None:
            return "NO" if no_score >= 0.5 else "YES"

        if self.latest_yes_trade and self.latest_no_trade:
            if self.latest_yes_trade.price == self.latest_no_trade.price:
                return None
            return "YES" if self.latest_yes_trade.price > self.latest_no_trade.price else "NO"

        if self.latest_yes_trade:
            return "YES" if self.latest_yes_trade.price >= 0.5 else "NO"
        if self.latest_no_trade:
            return "NO" if self.latest_no_trade.price >= 0.5 else "YES"
        return None

    def has_expired(self, at_time: datetime) -> bool:
        if self.ends_at is None:
            return False
        return _ensure_utc(at_time) >= _ensure_utc(self.ends_at)

    @staticmethod
    def _complement_of(book: _BookTick | None, outcome: OutcomeName) -> _BookTick | None:
        if book is None:
            return None

        best_bid = None if book.best_ask is None else _clip_probability(1 - book.best_ask)
        best_ask = None if book.best_bid is None else _clip_probability(1 - book.best_bid)
        mid_price = (
            None if book.effective_mid is None else _clip_probability(1 - book.effective_mid)
        )
        spread = 0.0
        if best_bid is not None and best_ask is not None:
            spread = max(0.0, best_ask - best_bid)

        return _BookTick(
            market_id=book.market_id,
            token_id=book.token_id,
            outcome=outcome,
            best_bid=best_bid,
            best_ask=best_ask,
            mid_price=mid_price,
            spread=spread,
            observed_at=book.observed_at,
        )


@dataclass(slots=True)
class _PositionLeg:
    outcome: OutcomeName
    token_id: str | None
    entry_price: float


@dataclass(slots=True)
class _OpenPosition:
    market_id: str
    question: str
    strategy: StrategyName
    side: PositionSide
    entry_time: datetime
    expected_edge: float
    risk_pct: float
    size: float
    notional: float
    fees: float
    legs: tuple[_PositionLeg, ...]

    @property
    def token_id(self) -> str | None:
        if len(self.legs) == 1:
            return self.legs[0].token_id
        return None

    @property
    def entry_price(self) -> float:
        return sum(leg.entry_price for leg in self.legs)

    def mark_value(self, market: _MarketContext) -> float:
        total = 0.0
        for leg in self.legs:
            mark = market.mark_for(leg.outcome)
            if mark is None:
                mark = leg.entry_price if len(self.legs) > 1 else _MARKET_MARKOUT_FALLBACK
            total += mark * self.size
        return total

    def resolved_value(self, outcome: OutcomeName) -> float:
        return sum((1.0 if leg.outcome == outcome else 0.0) * self.size for leg in self.legs)


@dataclass(slots=True)
class _EntrySignal:
    side: PositionSide
    expected_edge: float
    legs: tuple[OutcomeName, ...]


class BacktestEngine:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def run(self, cfg: BacktestConfig) -> BacktestResult:
        cfg = self._normalize_cfg(cfg)
        ticks = await self._load_ticks(cfg)
        if not ticks:
            initial_point = self._equity_point(
                cfg.start_date,
                cfg.initial_equity,
                cfg.initial_equity,
            )
            return self.build_result(
                trades=[],
                equity_curve=[initial_point],
                initial_equity=cfg.initial_equity,
            )

        market_ids = sorted({tick.market_id for tick in ticks})
        contexts = await self._load_market_contexts(market_ids)
        trade_tape = await self._load_trade_tape(cfg, market_ids)

        adaptive_engine = AdaptiveStrategyEngine(cfg.risk_cfg)
        spread_scanner = SpreadArbScanner()

        open_positions: dict[str, _OpenPosition] = {}
        closed_trades: list[BacktestTrade] = []
        equity_curve: list[dict] = [
            self._equity_point(cfg.start_date, cfg.initial_equity, cfg.initial_equity)
        ]

        cash = float(cfg.initial_equity)
        peak_equity = float(cfg.initial_equity)
        trade_index = 0
        last_entry_by_market: dict[str, datetime] = {}
        opens_this_tick = 0
        current_tick_ts: datetime | None = None

        for tick in ticks:
            if current_tick_ts != tick.observed_at:
                current_tick_ts = tick.observed_at
                opens_this_tick = 0

            while (
                trade_index < len(trade_tape)
                and trade_tape[trade_index].traded_at <= tick.observed_at
            ):
                contexts[trade_tape[trade_index].market_id].update_trade(trade_tape[trade_index])
                trade_index += 1

            market = contexts[tick.market_id]
            market.update_book(tick)

            if open_position := open_positions.get(tick.market_id):
                if market.has_expired(tick.observed_at):
                    trade, proceeds = self._close_position(open_position, market, tick.observed_at)
                    cash += proceeds
                    closed_trades.append(trade)
                    del open_positions[tick.market_id]

            equity_before_entry = self._portfolio_equity(cash, open_positions, contexts)
            peak_equity = max(peak_equity, equity_before_entry)

            signal = self._evaluate_signal(
                cfg=cfg,
                tick=tick,
                market=market,
                adaptive_engine=adaptive_engine,
                spread_scanner=spread_scanner,
            )

            can_open = (
                signal is not None
                and tick.market_id not in open_positions
                and opens_this_tick < cfg.risk_cfg.max_positions_per_tick
                and len(open_positions) < cfg.risk_cfg.max_concurrent_positions
                and not market.has_expired(tick.observed_at)
                and not self._drawdown_stop_hit(equity_before_entry, peak_equity, cfg.risk_cfg)
                and self._cooldown_elapsed(
                    last_entry_by_market.get(tick.market_id),
                    tick.observed_at,
                    cfg.risk_cfg,
                )
            )

            if can_open and signal is not None:
                capital_in_trade = sum(position.notional for position in open_positions.values())
                portfolio = PortfolioState(
                    equity=equity_before_entry,
                    capital_in_trade=capital_in_trade,
                    total_pnl=equity_before_entry - cfg.initial_equity,
                )
                position = self._open_position(cfg, market, tick.observed_at, portfolio, signal)
                if position is not None:
                    cash -= position.notional + position.fees
                    open_positions[tick.market_id] = position
                    last_entry_by_market[tick.market_id] = tick.observed_at
                    opens_this_tick += 1

            equity_after_tick = self._portfolio_equity(cash, open_positions, contexts)
            peak_equity = max(peak_equity, equity_after_tick)
            equity_curve.append(
                self._equity_point(tick.observed_at, equity_after_tick, peak_equity)
            )

        while trade_index < len(trade_tape):
            contexts[trade_tape[trade_index].market_id].update_trade(trade_tape[trade_index])
            trade_index += 1

        final_ts = max(cfg.end_date, ticks[-1].observed_at)
        for market_id, position in list(open_positions.items()):
            trade, proceeds = self._close_position(position, contexts[market_id], final_ts)
            cash += proceeds
            closed_trades.append(trade)
            del open_positions[market_id]

        peak_equity = max(peak_equity, cash)
        equity_curve.append(self._equity_point(final_ts, cash, peak_equity))
        return self.build_result(
            trades=closed_trades,
            equity_curve=equity_curve,
            initial_equity=cfg.initial_equity,
        )

    @staticmethod
    def build_result(
        trades: list[BacktestTrade],
        equity_curve: list[dict],
        initial_equity: float,
    ) -> BacktestResult:
        winning_trades = sum(1 for trade in trades if trade.pnl > 0)
        total_trades = len(trades)
        total_pnl = sum(trade.pnl for trade in trades)
        total_pnl_pct = 0.0 if initial_equity <= 0 else (total_pnl / initial_equity) * 100
        win_rate = 0.0 if total_trades == 0 else (winning_trades / total_trades) * 100
        max_drawdown, max_drawdown_pct = BacktestEngine._max_drawdown(equity_curve)
        gross_profit = sum(trade.pnl for trade in trades if trade.pnl > 0)
        gross_loss = abs(sum(trade.pnl for trade in trades if trade.pnl < 0))
        profit_factor = 0.0 if gross_loss == 0 else gross_profit / gross_loss
        avg_trade_duration_h = (
            0.0 if total_trades == 0 else sum(trade.duration_h for trade in trades) / total_trades
        )
        sharpe_ratio = BacktestEngine._annualized_sharpe(equity_curve)

        return BacktestResult(
            total_trades=total_trades,
            winning_trades=winning_trades,
            win_rate=round(win_rate, 6),
            total_pnl=round(total_pnl, 6),
            total_pnl_pct=round(total_pnl_pct, 6),
            max_drawdown=round(max_drawdown, 6),
            max_drawdown_pct=round(max_drawdown_pct, 6),
            sharpe_ratio=round(sharpe_ratio, 6),
            profit_factor=round(profit_factor, 6),
            avg_trade_duration_h=round(avg_trade_duration_h, 6),
            equity_curve=equity_curve,
            trades=trades,
        )

    async def _load_ticks(self, cfg: BacktestConfig) -> list[_BookTick]:
        stmt = (
            select(TopOfBook, Token.outcome)
            .outerjoin(Token, Token.id == TopOfBook.token_id)
            .where(TopOfBook.observed_at >= cfg.start_date, TopOfBook.observed_at <= cfg.end_date)
            .order_by(TopOfBook.observed_at, TopOfBook.id)
        )
        if cfg.market_ids:
            stmt = stmt.where(TopOfBook.market_id.in_(cfg.market_ids))

        rows = (await self.session.execute(stmt)).all()
        return [
            _BookTick(
                market_id=book.market_id,
                token_id=book.token_id,
                outcome=_normalize_outcome(outcome),
                best_bid=_to_float(book.best_bid),
                best_ask=_to_float(book.best_ask),
                mid_price=_to_float(book.mid_price),
                spread=_to_float(book.spread),
                observed_at=_ensure_utc(book.observed_at),
            )
            for book, outcome in rows
        ]

    async def _load_trade_tape(
        self,
        cfg: BacktestConfig,
        market_ids: list[str],
    ) -> list[_TapeTrade]:
        if not market_ids:
            return []

        stmt = (
            select(Trade, Token.outcome)
            .outerjoin(Token, Token.id == Trade.token_id)
            .where(
                Trade.market_id.in_(market_ids),
                Trade.traded_at >= cfg.start_date,
                Trade.traded_at <= cfg.end_date,
            )
            .order_by(Trade.traded_at, Trade.id)
        )
        rows = (await self.session.execute(stmt)).all()
        return [
            _TapeTrade(
                market_id=trade.market_id,
                token_id=trade.token_id,
                outcome=_normalize_outcome(outcome),
                side=trade.side,
                price=float(trade.price),
                size=float(trade.size),
                traded_at=_ensure_utc(trade.traded_at),
            )
            for trade, outcome in rows
        ]

    async def _load_market_contexts(self, market_ids: list[str]) -> dict[str, _MarketContext]:
        meta_stmt = (
            select(Market.id, Market.question, Market.resolved, Event.ends_at)
            .join(Event, Event.id == Market.event_id)
            .where(Market.id.in_(market_ids))
        )
        token_stmt = (
            select(Token.market_id, Token.id, Token.outcome)
            .where(Token.market_id.in_(market_ids))
        )

        contexts = {
            market_id: _MarketContext(
                market_id=market_id,
                question=question,
                resolved=resolved,
                ends_at=_ensure_optional_utc(ends_at),
            )
            for market_id, question, resolved, ends_at in (
                await self.session.execute(meta_stmt)
            ).all()
        }
        for market_id in market_ids:
            contexts.setdefault(
                market_id,
                _MarketContext(
                    market_id=market_id,
                    question=market_id,
                    resolved=False,
                    ends_at=None,
                ),
            )

        for market_id, token_id, outcome in (await self.session.execute(token_stmt)).all():
            if market_id not in contexts:
                continue
            normalized = _normalize_outcome(outcome)
            if normalized == "YES":
                contexts[market_id].yes_token_id = token_id
            elif normalized == "NO":
                contexts[market_id].no_token_id = token_id

        return contexts

    def _evaluate_signal(
        self,
        cfg: BacktestConfig,
        tick: _BookTick,
        market: _MarketContext,
        adaptive_engine: AdaptiveStrategyEngine,
        spread_scanner: SpreadArbScanner,
    ) -> _EntrySignal | None:
        if cfg.strategy == "adaptive":
            return self._adaptive_signal(cfg, tick, market, adaptive_engine)
        if cfg.strategy == "spread_arb":
            return self._spread_signal(cfg, market, spread_scanner)
        return self._latency_signal(cfg, tick, market)

    def _adaptive_signal(
        self,
        cfg: BacktestConfig,
        tick: _BookTick,
        market: _MarketContext,
        adaptive_engine: AdaptiveStrategyEngine,
    ) -> _EntrySignal | None:
        if tick.outcome != "YES":
            return None

        quote = market.quote_for("YES")
        if quote is None or quote.best_bid is None or quote.best_ask is None:
            return None

        signal = adaptive_engine.evaluate_market(
            market.market_id,
            best_bid=quote.best_bid,
            best_ask=quote.best_ask,
        )
        if not signal["detected"]:
            return None

        side: PositionSide = signal["direction"]
        if side == "BUY_YES" and market.quote_for("YES") is None:
            return None
        if side == "BUY_NO" and market.quote_for("NO") is None:
            return None

        legs: tuple[OutcomeName, ...] = ("YES",) if side == "BUY_YES" else ("NO",)
        return _EntrySignal(side=side, expected_edge=float(signal["expected_edge"]), legs=legs)

    def _spread_signal(
        self,
        cfg: BacktestConfig,
        market: _MarketContext,
        spread_scanner: SpreadArbScanner,
    ) -> _EntrySignal | None:
        yes_quote = market.quote_for("YES")
        no_quote = market.quote_for("NO")
        if yes_quote is None or no_quote is None:
            return None
        if yes_quote.best_ask is None or no_quote.best_ask is None:
            return None

        opportunity = spread_scanner.scan(
            SpreadTopOfBookData(
                market_id=market.market_id,
                ask=yes_quote.best_ask,
                liquidity=cfg.initial_equity,
            ),
            SpreadTopOfBookData(
                market_id=market.market_id,
                ask=no_quote.best_ask,
                liquidity=cfg.initial_equity,
            ),
            fee_bps=cfg.fee_bps,
        )
        if opportunity is None:
            return None

        return _EntrySignal(
            side="SPREAD_ARB",
            expected_edge=max(opportunity.net_profit, 0.0),
            legs=("YES", "NO"),
        )

    def _latency_signal(
        self,
        cfg: BacktestConfig,
        tick: _BookTick,
        market: _MarketContext,
    ) -> _EntrySignal | None:
        if tick.outcome != "YES":
            return None

        quote = market.quote_for("YES")
        lead_trade = market.latest_yes_trade
        if quote is None or lead_trade is None or quote.effective_mid is None:
            return None

        age_seconds = (
            _ensure_utc(tick.observed_at) - _ensure_utc(lead_trade.traded_at)
        ).total_seconds()
        if age_seconds < 0 or age_seconds > cfg.risk_cfg.signal_staleness_seconds:
            return None

        trading_cost = quote.effective_spread + (cfg.fee_bps / 10_000)
        edge = abs(lead_trade.price - quote.effective_mid) - trading_cost
        threshold = max(cfg.risk_cfg.base_entry_threshold, trading_cost)
        if edge <= threshold:
            return None

        side: PositionSide = "BUY_YES" if lead_trade.price > quote.effective_mid else "BUY_NO"
        if side == "BUY_NO" and market.quote_for("NO") is None:
            return None

        legs: tuple[OutcomeName, ...] = ("YES",) if side == "BUY_YES" else ("NO",)
        return _EntrySignal(side=side, expected_edge=edge, legs=legs)

    def _open_position(
        self,
        cfg: BacktestConfig,
        market: _MarketContext,
        observed_at: datetime,
        portfolio: PortfolioState,
        signal: _EntrySignal,
    ) -> _OpenPosition | None:
        notional, risk_pct = AdaptiveStrategyEngine(cfg.risk_cfg).size_position(
            portfolio,
            signal.expected_edge,
        )
        if notional <= 0:
            return None

        legs: list[_PositionLeg] = []
        entry_price = 0.0
        for outcome in signal.legs:
            quote = market.quote_for(outcome)
            if quote is None:
                return None
            fill_price = self._fill_price(cfg, quote)
            if fill_price <= 0:
                return None
            token_id = market.yes_token_id if outcome == "YES" else market.no_token_id
            legs.append(_PositionLeg(outcome=outcome, token_id=token_id, entry_price=fill_price))
            entry_price += fill_price

        if entry_price <= 0:
            return None

        size = notional / entry_price
        fees = notional * (cfg.fee_bps / 10_000)
        return _OpenPosition(
            market_id=market.market_id,
            question=market.question,
            strategy=cfg.strategy,
            side=signal.side,
            entry_time=observed_at,
            expected_edge=signal.expected_edge,
            risk_pct=risk_pct,
            size=size,
            notional=notional,
            fees=fees,
            legs=tuple(legs),
        )

    def _close_position(
        self,
        position: _OpenPosition,
        market: _MarketContext,
        exit_time: datetime,
    ) -> tuple[BacktestTrade, float]:
        resolved_outcome = market.infer_resolved_outcome() if market.resolved else None
        if market.resolved and resolved_outcome is not None:
            exit_value = position.resolved_value(resolved_outcome)
            settlement = "resolved"
        else:
            exit_value = position.mark_value(market)
            settlement = "mtm"

        exit_price = 0.0 if position.size <= 0 else exit_value / position.size
        pnl = exit_value - position.notional - position.fees
        pnl_pct = 0.0 if position.notional <= 0 else (pnl / position.notional) * 100
        duration_h = max(
            0.0,
            (_ensure_utc(exit_time) - _ensure_utc(position.entry_time)).total_seconds() / 3600,
        )

        trade = BacktestTrade(
            market_id=position.market_id,
            question=position.question,
            strategy=position.strategy,
            side=position.side,
            token_id=position.token_id,
            entry_time=position.entry_time,
            exit_time=exit_time,
            entry_price=round(position.entry_price, 6),
            exit_price=round(exit_price, 6),
            size=round(position.size, 6),
            notional=round(position.notional, 6),
            fees=round(position.fees, 6),
            pnl=round(pnl, 6),
            pnl_pct=round(pnl_pct, 6),
            expected_edge=round(position.expected_edge, 6),
            risk_pct=round(position.risk_pct, 6),
            duration_h=round(duration_h, 6),
            resolved_outcome=resolved_outcome,
            settlement=settlement,
        )
        return trade, exit_value

    def _fill_price(self, cfg: BacktestConfig, quote: _BookTick) -> float:
        ask = quote.best_ask
        if ask is None:
            mid = quote.effective_mid
            if mid is None:
                return 0.0
            ask = mid

        slippage = self._slippage_factor(cfg, quote)
        return _clip_probability(ask * (1 + slippage))

    @staticmethod
    def _slippage_factor(cfg: BacktestConfig, quote: _BookTick) -> float:
        if cfg.slippage_model == "fixed":
            return _FIXED_SLIPPAGE

        ask = quote.best_ask
        spread = quote.effective_spread
        if ask is None or ask <= 0:
            return 0.0
        return max(0.0, min(0.25, spread / ask))

    @staticmethod
    def _portfolio_equity(
        cash: float,
        open_positions: dict[str, _OpenPosition],
        contexts: dict[str, _MarketContext],
    ) -> float:
        mark_value = sum(
            position.mark_value(contexts[market_id])
            for market_id, position in open_positions.items()
        )
        return cash + mark_value

    @staticmethod
    def _equity_point(timestamp: datetime, equity: float, peak_equity: float) -> dict:
        drawdown = max(0.0, peak_equity - equity)
        drawdown_pct = 0.0 if peak_equity <= 0 else (drawdown / peak_equity) * 100
        return {
            "timestamp": _ensure_utc(timestamp),
            "equity": round(equity, 6),
            "drawdown_pct": round(drawdown_pct, 6),
        }

    @staticmethod
    def _max_drawdown(equity_curve: list[dict]) -> tuple[float, float]:
        max_drawdown = 0.0
        max_drawdown_pct = 0.0
        peak = 0.0

        for point in equity_curve:
            equity = float(point["equity"])
            peak = max(peak, equity)
            drawdown = max(0.0, peak - equity)
            drawdown_pct = 0.0 if peak <= 0 else (drawdown / peak) * 100
            max_drawdown = max(max_drawdown, drawdown)
            max_drawdown_pct = max(max_drawdown_pct, drawdown_pct)

        return max_drawdown, max_drawdown_pct

    @staticmethod
    def _annualized_sharpe(equity_curve: list[dict]) -> float:
        daily_returns = BacktestEngine._daily_returns(equity_curve)
        if len(daily_returns) < 2:
            return 0.0

        std = stdev(daily_returns)
        if std == 0:
            return 0.0
        avg = sum(daily_returns) / len(daily_returns)
        return (avg / std) * sqrt(252)

    @staticmethod
    def _daily_returns(equity_curve: list[dict]) -> list[float]:
        if not equity_curve:
            return []

        ordered = sorted(equity_curve, key=lambda point: _ensure_utc(point["timestamp"]))
        daily_returns: list[float] = []
        current_day = _ensure_utc(ordered[0]["timestamp"]).date()
        day_open = float(ordered[0]["equity"])
        day_close = day_open

        for point in ordered:
            timestamp = _ensure_utc(point["timestamp"])
            equity = float(point["equity"])
            if timestamp.date() != current_day:
                if day_open > 0:
                    daily_returns.append((day_close - day_open) / day_open)
                current_day = timestamp.date()
                day_open = equity
            day_close = equity

        if day_open > 0:
            daily_returns.append((day_close - day_open) / day_open)
        return daily_returns

    @staticmethod
    def _drawdown_stop_hit(equity: float, peak_equity: float, risk_cfg: RiskConfig) -> bool:
        if peak_equity <= 0:
            return False
        drawdown_pct = (peak_equity - equity) / peak_equity
        return drawdown_pct >= risk_cfg.max_drawdown_stop_pct

    @staticmethod
    def _cooldown_elapsed(
        previous_entry_at: datetime | None,
        observed_at: datetime,
        risk_cfg: RiskConfig,
    ) -> bool:
        if previous_entry_at is None:
            return True
        elapsed = (_ensure_utc(observed_at) - _ensure_utc(previous_entry_at)).total_seconds()
        return elapsed >= risk_cfg.cooldown_seconds

    @staticmethod
    def _normalize_cfg(cfg: BacktestConfig) -> BacktestConfig:
        if cfg.end_date <= cfg.start_date:
            raise ValueError("end_date must be after start_date")

        return BacktestConfig(
            start_date=_ensure_utc(cfg.start_date),
            end_date=_ensure_utc(cfg.end_date),
            initial_equity=float(cfg.initial_equity),
            strategy=cfg.strategy,
            risk_cfg=cfg.risk_cfg,
            slippage_model=cfg.slippage_model,
            fee_bps=float(cfg.fee_bps),
            market_ids=cfg.market_ids,
        )


def _normalize_outcome(value: str | None) -> OutcomeName | None:
    if value is None:
        return None
    cleaned = str(value).strip().upper()
    if cleaned in _YES_LABELS:
        return "YES"
    if cleaned in _NO_LABELS:
        return "NO"
    return None


def _to_float(value: object) -> float | None:
    if value is None:
        return None
    return float(value)


def _clip_probability(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def _ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _ensure_optional_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return _ensure_utc(value)
