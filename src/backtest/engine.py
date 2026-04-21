from collections import defaultdict
from decimal import Decimal
from statistics import mean, pstdev
from typing import Iterable

from src.backtest.costs.slippage import estimate_slippage_usdc
from src.backtest.costs.spread import CandleRange, estimate_spread_cost
from src.backtest.models import (
    BacktestBookSnapshot,
    BacktestCandle,
    BacktestFill,
    BacktestMarket,
    BacktestRun,
    BacktestTrade,
)
from src.economics.fees import calculate_polymarket_fee
from src.economics.models import ECONOMIC_MODEL_VERSION, LiquidityRole, StrategyTrack
from src.economics.pnl import calculate_long_pnl, shares_from_notional


class LeaderSwingBacktester:
    def __init__(
        self,
        *,
        size_usdc: Decimal = Decimal("100"),
        observation_lag_s: float = 120.0,
    ) -> None:
        self.size_usdc = Decimal(str(size_usdc))
        self.observation_lag_s = observation_lag_s

    def run(
        self,
        *,
        markets: Iterable[BacktestMarket],
        trades: Iterable[BacktestTrade],
        books: Iterable[BacktestBookSnapshot],
        candles: Iterable[BacktestCandle] = (),
        policy: str,
    ) -> BacktestRun:
        market_map = {market.market_id: market for market in markets}
        book_rows = list(books)
        candle_rows = list(candles)
        fills: list[BacktestFill] = []
        for entry, exit_ in self._position_pairs(trades):
            market = market_map.get(entry.market_id)
            if market is None:
                continue
            if policy == "liquid_markets_only" and market.volume_usdc < Decimal("50000"):
                continue
            action = self._action_for_policy(policy, len(fills))
            if action is None:
                continue
            fills.append(self._simulate_fill(entry, exit_, market, book_rows, candle_rows, action))
        return BacktestRun(
            strategy_track=StrategyTrack.LEADER_SWING,
            policy=policy,
            fills=fills,
            metrics=_compute_metrics(fills),
        )

    def _position_pairs(
        self, trades: Iterable[BacktestTrade]
    ) -> list[tuple[BacktestTrade, BacktestTrade]]:
        grouped: dict[tuple[str, str, str], list[BacktestTrade]] = defaultdict(list)
        for trade in trades:
            grouped[(trade.leader_wallet, trade.market_id, trade.token_id)].append(trade)

        pairs: list[tuple[BacktestTrade, BacktestTrade]] = []
        for rows in grouped.values():
            ordered = sorted(
                rows, key=lambda trade: (trade.observed_ts, trade.event_ts, trade.tx_hash)
            )
            open_trade: BacktestTrade | None = None
            for trade in ordered:
                if trade.side.upper() == "BUY" and open_trade is None:
                    open_trade = trade
                elif trade.side.upper() == "SELL" and open_trade is not None:
                    if trade.observed_ts >= open_trade.observed_ts:
                        pairs.append((open_trade, trade))
                    open_trade = None
        return pairs

    def _action_for_policy(self, policy: str, index: int) -> str | None:
        if policy in {"follow_all", "liquid_markets_only"}:
            return "follow"
        if policy == "fade_all":
            return "fade"
        if policy == "random_seeded":
            return "follow" if index % 2 == 0 else "fade"
        raise ValueError(f"unknown backtest policy: {policy}")

    def _simulate_fill(
        self,
        entry: BacktestTrade,
        exit_: BacktestTrade,
        market: BacktestMarket,
        books: list[BacktestBookSnapshot],
        candles: list[BacktestCandle],
        action: str,
    ) -> BacktestFill:
        if action == "follow":
            token_id = entry.token_id
            entry_price = entry.price
            exit_price = exit_.price
        else:
            token_id = market.opposite_token_id(entry.token_id)
            entry_price = Decimal("1") - entry.price
            exit_price = Decimal("1") - exit_.price

        size_shares = shares_from_notional(self.size_usdc, entry_price)
        fee_snapshot = market.fee_snapshot_for(token_id)
        entry_fee = calculate_polymarket_fee(
            shares=size_shares,
            price=entry_price,
            fee_rate=fee_snapshot.fee_rate,
            liquidity_role=LiquidityRole.TAKER,
            fees_enabled=fee_snapshot.fee_enabled,
        )
        exit_fee = calculate_polymarket_fee(
            shares=size_shares,
            price=exit_price,
            fee_rate=fee_snapshot.fee_rate,
            liquidity_role=LiquidityRole.TAKER,
            fees_enabled=fee_snapshot.fee_enabled,
        )
        entry_spread = estimate_spread_cost(
            price=entry_price,
            size_shares=size_shares,
            category=market.category,
            book=self._nearest_book(books, entry.market_id, entry.token_id, entry.event_ts),
            candle=self._nearest_candle(candles, entry.market_id, entry.token_id, entry.event_ts),
        )
        exit_spread = estimate_spread_cost(
            price=exit_price,
            size_shares=size_shares,
            category=market.category,
            book=self._nearest_book(books, exit_.market_id, exit_.token_id, exit_.event_ts),
            candle=self._nearest_candle(candles, exit_.market_id, exit_.token_id, exit_.event_ts),
        )
        entry_slippage = estimate_slippage_usdc(
            size_usdc=self.size_usdc,
            volume_24h_usdc=market.volume_usdc,
            volatility_24h=Decimal("0.20"),
        )
        exit_slippage = estimate_slippage_usdc(
            size_usdc=self.size_usdc,
            volume_24h_usdc=market.volume_usdc,
            volatility_24h=Decimal("0.20"),
        )
        spread_cost = entry_spread.cost_usdc + exit_spread.cost_usdc
        slippage_cost = entry_slippage.cost_usdc + exit_slippage.cost_usdc
        pnl = calculate_long_pnl(
            entry_price=entry_price,
            exit_price=exit_price,
            size_shares=size_shares,
            entry_fee_usdc=entry_fee,
            exit_fee_usdc=exit_fee,
            spread_cost_usdc=spread_cost,
            slippage_usdc=slippage_cost,
        )
        return BacktestFill(
            strategy_track=StrategyTrack.LEADER_SWING,
            action=action,
            market_id=entry.market_id,
            token_id=token_id,
            leader_wallet=entry.leader_wallet,
            entry_tx_hash=entry.tx_hash,
            exit_tx_hash=exit_.tx_hash,
            entry_ts=entry.observed_ts,
            exit_ts=exit_.observed_ts,
            entry_price=entry_price,
            exit_price=exit_price,
            size_shares=size_shares,
            notional_usdc=pnl.notional_usdc,
            gross_pnl_usdc=pnl.gross_pnl_usdc,
            net_pnl_usdc=pnl.net_pnl_usdc,
            fee_usdc=entry_fee + exit_fee,
            spread_cost_usdc=spread_cost,
            slippage_usdc=slippage_cost,
            signal_audit={
                "accepted": True,
                "strategy_track": StrategyTrack.LEADER_SWING.value,
                "economic_model_version": ECONOMIC_MODEL_VERSION,
                "observation_lag_s": self.observation_lag_s,
            },
            cost_sources={
                "entry_spread": entry_spread.source,
                "exit_spread": exit_spread.source,
                "entry_slippage": entry_slippage.source,
                "exit_slippage": exit_slippage.source,
                "fee": fee_snapshot.source,
            },
        )

    def _nearest_book(
        self,
        books: list[BacktestBookSnapshot],
        market_id: str,
        token_id: str,
        ts,
    ) -> BacktestBookSnapshot | None:
        candidates = [
            book for book in books if book.market_id == market_id and book.token_id == token_id
        ]
        if not candidates:
            return None
        return min(candidates, key=lambda book: abs((book.ts - ts).total_seconds()))

    def _nearest_candle(
        self,
        candles: list[BacktestCandle],
        market_id: str,
        token_id: str,
        ts,
    ) -> CandleRange | None:
        candidates = [
            candle
            for candle in candles
            if candle.market_id == market_id and candle.token_id == token_id
        ]
        if not candidates:
            return None
        containing = [candle for candle in candidates if candle.start_ts <= ts <= candle.end_ts]
        selected = (
            min(containing, key=lambda candle: candle.end_ts - candle.start_ts)
            if containing
            else min(
                candidates,
                key=lambda candle: abs(
                    ((candle.start_ts + (candle.end_ts - candle.start_ts) / 2) - ts).total_seconds()
                ),
            )
        )
        return CandleRange(high=selected.high, low=selected.low)


def _compute_metrics(fills: list[BacktestFill]) -> dict:
    net_values = [fill.net_pnl_usdc for fill in fills]
    total_net = sum(net_values, Decimal("0"))
    total_gross = sum((fill.gross_pnl_usdc for fill in fills), Decimal("0"))
    wins = sum(1 for value in net_values if value > 0)
    losses = sum(1 for value in net_values if value < 0)
    sharpe = Decimal("0")
    if len(net_values) == 1:
        sharpe = Decimal("1") if net_values[0] > 0 else Decimal("-1")
    elif len(net_values) > 1:
        stdev = Decimal(str(pstdev([float(value) for value in net_values])))
        if stdev > 0:
            sharpe = Decimal(str(mean([float(value) for value in net_values]))) / stdev
    return {
        "total_trades": len(fills),
        "wins": wins,
        "losses": losses,
        "net_pnl_usdc": total_net,
        "gross_pnl_usdc": total_gross,
        "win_rate": Decimal(wins) / Decimal(len(fills)) if fills else Decimal("0"),
        "sharpe_net": sharpe,
        "max_drawdown_usdc": _max_drawdown(net_values),
    }


def _max_drawdown(values: list[Decimal]) -> Decimal:
    equity = Decimal("0")
    peak = Decimal("0")
    worst = Decimal("0")
    for value in values:
        equity += value
        peak = max(peak, equity)
        worst = min(worst, equity - peak)
    return abs(worst)
