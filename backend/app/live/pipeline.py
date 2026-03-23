from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.services.adaptive_strategy import PortfolioState, RiskConfig


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def iso_or_none(value: datetime | None) -> str | None:
    normalized = as_utc(value)
    return normalized.isoformat() if normalized is not None else None


def ms_between(later: datetime | None, earlier: datetime | None) -> int | None:
    if later is None or earlier is None:
        return None
    return max(0, int((later - earlier).total_seconds() * 1000))


def clip_probability(value: float) -> float:
    return max(0.01, min(0.99, float(value)))


def round_float(value: float, digits: int = 6) -> float:
    return round(float(value), digits)


def mean(values: list[float]) -> float:
    if not values:
        return 0.0
    return sum(values) / len(values)


def stddev(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    avg = mean(values)
    variance = sum((value - avg) ** 2 for value in values) / (len(values) - 1)
    return math.sqrt(max(0.0, variance))


def diff_stddev(values: list[float]) -> float:
    if len(values) < 3:
        return 0.0
    diffs = [values[idx] - values[idx - 1] for idx in range(1, len(values))]
    return stddev(diffs)


@dataclass(frozen=True, slots=True)
class QuoteObservation:
    side: str
    best_bid: float
    best_ask: float
    mid_price: float
    spread: float
    observed_at: datetime
    received_at: datetime
    source: str
    source_delay_ms: int

    def to_snapshot(self, now: datetime) -> dict:
        return {
            "side": self.side,
            "best_bid": round_float(self.best_bid, 4),
            "best_ask": round_float(self.best_ask, 4),
            "mid_price": round_float(self.mid_price, 4),
            "spread": round_float(self.spread, 4),
            "observed_at": self.observed_at.isoformat(),
            "received_at": self.received_at.isoformat(),
            "source": self.source,
            "source_delay_ms": self.source_delay_ms,
            "freshness_ms": ms_between(now, self.observed_at),
        }


@dataclass(slots=True)
class SideWindow:
    side: str
    maxlen: int = 180
    observations: deque[QuoteObservation] = field(default_factory=deque)

    def __post_init__(self) -> None:
        self.observations = deque(self.observations, maxlen=self.maxlen)

    @property
    def latest(self) -> QuoteObservation | None:
        return self.observations[-1] if self.observations else None

    def mid_prices(self) -> list[float]:
        return [observation.mid_price for observation in self.observations]

    def ingest(self, observation: QuoteObservation) -> bool:
        latest = self.latest
        if latest is not None:
            if observation.observed_at < latest.observed_at:
                return False
            if (
                observation.observed_at == latest.observed_at
                and observation.best_bid == latest.best_bid
                and observation.best_ask == latest.best_ask
                and observation.source == latest.source
            ):
                return False
        self.observations.append(observation)
        return True

    def count_since(self, threshold: datetime) -> int:
        return sum(1 for observation in self.observations if observation.received_at >= threshold)


@dataclass(slots=True)
class SourceHealth:
    name: str
    status: str = "idle"
    last_seen_at: datetime | None = None
    last_message_at: datetime | None = None
    messages: deque[datetime] = field(default_factory=lambda: deque(maxlen=512))
    note: str | None = None

    def mark_status(
        self,
        status: str,
        seen_at: datetime | None = None,
        note: str | None = None,
    ) -> None:
        current = as_utc(seen_at) or utc_now()
        self.status = status
        self.last_seen_at = current
        if note is not None:
            self.note = note

    def record_message(self, received_at: datetime, note: str | None = None) -> None:
        current = as_utc(received_at) or utc_now()
        self.status = "live"
        self.last_seen_at = current
        self.last_message_at = current
        self.messages.append(current)
        if note is not None:
            self.note = note

    def messages_last_minute(self, now: datetime) -> int:
        threshold = now.timestamp() - 60
        return sum(1 for item in self.messages if item.timestamp() >= threshold)

    def to_snapshot(self, now: datetime) -> dict:
        last_seen = ms_between(now, self.last_seen_at)
        return {
            "name": self.name,
            "status": self.status,
            "last_seen_at": iso_or_none(self.last_seen_at),
            "last_message_at": iso_or_none(self.last_message_at),
            "lag_ms": last_seen if last_seen is not None else None,
            "messages_last_minute": self.messages_last_minute(now),
            "note": self.note,
        }


@dataclass(slots=True)
class MarketIngestionState:
    market_id: str
    title: str
    end_date: str
    token_id_yes: str
    token_id_no: str
    bootstrap_only: bool = False
    yes: SideWindow = field(default_factory=lambda: SideWindow(side="YES"))
    no: SideWindow = field(default_factory=lambda: SideWindow(side="NO"))
    raw_events: deque[dict] = field(default_factory=lambda: deque(maxlen=48))
    last_message_at: datetime | None = None
    last_quote_at: datetime | None = None

    def side_state(self, side: str) -> SideWindow:
        return self.yes if side == "YES" else self.no

    def latest_mid(self, side: str) -> float:
        latest = self.side_state(side).latest
        return latest.mid_price if latest is not None else 0.0

    def latest_spread(self, side: str) -> float:
        latest = self.side_state(side).latest
        return latest.spread if latest is not None else 0.0

    def latest_source(self) -> str:
        timestamps = [
            item
            for item in (self.yes.latest, self.no.latest)
            if item is not None
        ]
        if not timestamps:
            return "seed"
        latest = max(timestamps, key=lambda item: item.observed_at)
        return latest.source

    def ingest(
        self,
        side: str,
        best_bid: float,
        best_ask: float,
        observed_at: datetime,
        received_at: datetime,
        source: str,
        raw_event: dict | None = None,
    ) -> bool:
        normalized_bid = clip_probability(best_bid)
        normalized_ask = clip_probability(best_ask)
        if normalized_ask < normalized_bid:
            normalized_ask = normalized_bid
        mid_price = (normalized_bid + normalized_ask) / 2
        spread = max(0.0, normalized_ask - normalized_bid)
        observation = QuoteObservation(
            side=side,
            best_bid=normalized_bid,
            best_ask=normalized_ask,
            mid_price=mid_price,
            spread=spread,
            observed_at=observed_at,
            received_at=received_at,
            source=source,
            source_delay_ms=ms_between(received_at, observed_at) or 0,
        )
        accepted = self.side_state(side).ingest(observation)
        if accepted:
            self.last_message_at = received_at
            latest_times = [
                entry.observed_at
                for entry in (self.yes.latest, self.no.latest)
                if entry is not None
            ]
            self.last_quote_at = max(latest_times) if latest_times else None
            if raw_event is not None:
                self.raw_events.append(raw_event)
        return accepted

    def observations(self) -> int:
        return max(len(self.yes.observations), len(self.no.observations))

    def messages_last_minute(self, now: datetime) -> int:
        threshold = now.timestamp() - 60
        return sum(
            1
            for observation in [*self.yes.observations, *self.no.observations]
            if observation.received_at.timestamp() >= threshold
        )

    def to_health_snapshot(self, now: datetime) -> dict:
        freshness_ms = ms_between(now, self.last_quote_at)
        source_delay_candidates = [
            observation.source_delay_ms
            for observation in (self.yes.latest, self.no.latest)
            if observation is not None
        ]
        return {
            "market_id": self.market_id,
            "title": self.title,
            "quote_source": self.latest_source(),
            "bootstrap_only": self.bootstrap_only,
            "last_message_at": iso_or_none(self.last_message_at),
            "last_quote_at": iso_or_none(self.last_quote_at),
            "freshness_ms": freshness_ms,
            "observations": self.observations(),
            "messages_last_minute": self.messages_last_minute(now),
            "source_delay_ms": max(source_delay_candidates) if source_delay_candidates else None,
            "yes": self.yes.latest.to_snapshot(now) if self.yes.latest is not None else None,
            "no": self.no.latest.to_snapshot(now) if self.no.latest is not None else None,
        }


class IngestionLayer:
    def __init__(self, window_size: int = 180, raw_buffer_size: int = 256) -> None:
        self.window_size = window_size
        self._raw_events: deque[dict] = deque(maxlen=raw_buffer_size)
        self._sources: dict[str, SourceHealth] = {}
        self._markets: dict[str, MarketIngestionState] = {}

    def register_market(
        self,
        *,
        market_id: str,
        title: str,
        end_date: str,
        token_id_yes: str,
        token_id_no: str,
        bootstrap_only: bool = False,
    ) -> MarketIngestionState:
        state = self._markets.get(market_id)
        if state is None:
            state = MarketIngestionState(
                market_id=market_id,
                title=title,
                end_date=end_date,
                token_id_yes=token_id_yes,
                token_id_no=token_id_no,
                bootstrap_only=bootstrap_only,
                yes=SideWindow(side="YES", maxlen=self.window_size),
                no=SideWindow(side="NO", maxlen=self.window_size),
            )
            self._markets[market_id] = state
        else:
            state.title = title
            state.end_date = end_date
            state.token_id_yes = token_id_yes
            state.token_id_no = token_id_no
            state.bootstrap_only = bootstrap_only
        return state

    def markets(self) -> list[MarketIngestionState]:
        return list(self._markets.values())

    def market(self, market_id: str) -> MarketIngestionState | None:
        return self._markets.get(market_id)

    def update_source_status(
        self,
        source: str,
        status: str,
        *,
        seen_at: datetime | None = None,
        note: str | None = None,
    ) -> None:
        health = self._sources.setdefault(source, SourceHealth(name=source))
        health.mark_status(status=status, seen_at=seen_at, note=note)

    def ingest_quote(
        self,
        *,
        market_id: str,
        side: str,
        best_bid: float,
        best_ask: float,
        observed_at: datetime | None,
        received_at: datetime | None = None,
        source: str,
        raw_event: dict | None = None,
    ) -> bool:
        market = self._markets.get(market_id)
        if market is None:
            return False

        observed = as_utc(observed_at) or utc_now()
        received = as_utc(received_at) or utc_now()
        if observed > received:
            observed = received

        accepted = market.ingest(
            side=side,
            best_bid=best_bid,
            best_ask=best_ask,
            observed_at=observed,
            received_at=received,
            source=source,
            raw_event=raw_event,
        )
        if accepted:
            health = self._sources.setdefault(source, SourceHealth(name=source))
            health.record_message(received)
            if raw_event is not None:
                self._raw_events.append(
                    {
                        "market_id": market_id,
                        "side": side,
                        "source": source,
                        "received_at": received.isoformat(),
                        "observed_at": observed.isoformat(),
                        "payload": raw_event,
                    }
                )
        return accepted

    def health_snapshot(self, now: datetime, display_limit: int) -> dict:
        latest_quotes = [market.last_quote_at for market in self._markets.values() if market.last_quote_at]
        freshness_values = [
            ms_between(now, quote_at)
            for quote_at in latest_quotes
            if quote_at is not None
        ]
        avg_freshness_ms = int(mean([value for value in freshness_values if value is not None])) if freshness_values else 0
        max_freshness_ms = max(freshness_values) if freshness_values else 0
        stale_threshold_ms = 3_000
        stale_market_count = sum(
            1
            for market in self._markets.values()
            if (freshness := ms_between(now, market.last_quote_at)) is None or freshness > stale_threshold_ms
        )
        source_delay_values = [
            observation.source_delay_ms
            for market in self._markets.values()
            for observation in (market.yes.latest, market.no.latest)
            if observation is not None
        ]
        avg_source_delay_ms = int(mean(source_delay_values)) if source_delay_values else 0
        status = "healthy"
        if stale_market_count > max(1, len(self._markets) // 4):
            status = "degraded"
        if latest_quotes and max_freshness_ms > 10_000:
            status = "stalled"

        health_rows = sorted(
            self._markets.values(),
            key=lambda market: (
                1 if not market.bootstrap_only else 0,
                -(ms_between(now, market.last_quote_at) or 9_999_999),
                market.messages_last_minute(now),
            ),
            reverse=True,
        )[:display_limit]

        return {
            "status": status,
            "total_markets": len(self._markets),
            "live_markets": sum(1 for market in self._markets.values() if not market.bootstrap_only),
            "stale_market_count": stale_market_count,
            "updates_last_minute": sum(market.messages_last_minute(now) for market in self._markets.values()),
            "raw_buffer_size": len(self._raw_events),
            "avg_freshness_ms": avg_freshness_ms,
            "max_freshness_ms": max_freshness_ms,
            "avg_source_delay_ms": avg_source_delay_ms,
            "last_message_at": iso_or_none(max(latest_quotes) if latest_quotes else None),
            "sources": [
                source.to_snapshot(now)
                for source in sorted(self._sources.values(), key=lambda item: item.name)
            ],
            "markets": [market.to_health_snapshot(now) for market in health_rows],
            "recent_raw": list(self._raw_events)[-24:],
        }


@dataclass(frozen=True, slots=True)
class AnalyticsView:
    market_id: str
    title: str
    end_date: str
    token_id_yes: str
    token_id_no: str
    best_bid: float
    best_ask: float
    mid_price: float
    no_mid_price: float
    spread: float
    no_spread: float
    volatility: float
    rolling_mean: float
    rolling_std: float
    z_score: float
    price_delta: float
    momentum: float
    imbalance: float
    pressure: float
    complement_gap: float
    freshness_ms: int
    source_delay_ms: int
    observations: int
    expected_edge: float
    entry_threshold: float
    signal_strength: float
    rank_score: float
    direction: str
    regime: str
    liquidity_score: float
    quote_source: str
    bootstrap_only: bool
    explain: list[str]

    def to_market_row(self, decision: dict | None = None, open_trade_id: str | None = None) -> dict:
        decision = decision or {}
        return {
            "market_id": self.market_id,
            "title": self.title,
            "end_date": self.end_date,
            "token_id_yes": self.token_id_yes,
            "token_id_no": self.token_id_no,
            "best_bid": round_float(self.best_bid, 4),
            "best_ask": round_float(self.best_ask, 4),
            "mid_price": round_float(self.mid_price, 4),
            "no_mid_price": round_float(self.no_mid_price, 4),
            "spread": round_float(self.spread, 4),
            "no_spread": round_float(self.no_spread, 4),
            "volatility": round_float(self.volatility, 6),
            "rolling_mean": round_float(self.rolling_mean, 6),
            "rolling_std": round_float(self.rolling_std, 6),
            "z_score": round_float(self.z_score, 4),
            "liquidity_score": round_float(self.liquidity_score, 4),
            "expected_edge": round_float(self.expected_edge, 6),
            "entry_threshold": round_float(self.entry_threshold, 6),
            "signal_strength": round_float(self.signal_strength, 4),
            "rank_score": round_float(self.rank_score, 4),
            "direction": self.direction,
            "est_profit": round_float(self.expected_edge * 100, 3),
            "detected": decision.get("action") == "OPEN",
            "observations": self.observations,
            "complement_gap": round_float(self.complement_gap, 4),
            "price_delta": round_float(self.price_delta, 6),
            "momentum": round_float(self.momentum, 6),
            "imbalance": round_float(self.imbalance, 6),
            "pressure": round_float(self.pressure, 6),
            "freshness_ms": self.freshness_ms,
            "source_delay_ms": self.source_delay_ms,
            "quote_source": self.quote_source,
            "regime": self.regime,
            "bootstrap_only": self.bootstrap_only,
            "open_trade_id": open_trade_id,
            "open_position": open_trade_id is not None,
            "decision_action": decision.get("action", "HOLD"),
            "decision_summary": decision.get("summary", ""),
            "decision_rejections": decision.get("rejections", []),
            "decision_reasons": decision.get("reasons", []),
            "explain": self.explain,
        }


class AnalyticsLayer:
    def evaluate(
        self,
        ingestion: IngestionLayer,
        cfg: RiskConfig,
        now: datetime,
    ) -> tuple[list[AnalyticsView], dict]:
        views: list[AnalyticsView] = []
        for market in ingestion.markets():
            yes_latest = market.yes.latest
            if yes_latest is None:
                continue

            no_latest = market.no.latest
            yes_series = market.yes.mid_prices()
            latest_mid = yes_latest.mid_price
            no_mid = no_latest.mid_price if no_latest is not None else clip_probability(1 - latest_mid)
            rolling_mean = mean(yes_series)
            rolling_std = stddev(yes_series)
            z_score = 0.0 if rolling_std <= 1e-9 else (latest_mid - rolling_mean) / rolling_std
            volatility = diff_stddev(yes_series)
            prev_mid = yes_series[-2] if len(yes_series) >= 2 else latest_mid
            first_mid = yes_series[0] if yes_series else latest_mid
            price_delta = latest_mid - prev_mid
            momentum = latest_mid - first_mid
            spread = yes_latest.spread
            no_spread = no_latest.spread if no_latest is not None else 0.0
            complement_gap = abs((latest_mid + no_mid) - 1)
            freshness_ms = ms_between(now, market.last_quote_at) or 0
            source_delay_ms = max(
                [
                    observation.source_delay_ms
                    for observation in (yes_latest, no_latest)
                    if observation is not None
                ]
                or [0]
            )
            trading_cost = spread + (cfg.fee_bps / 10_000)
            liquidity_score = max(0.0, 1 - min(1.0, spread / max(cfg.spread_cap, 0.0001)))
            imbalance = latest_mid - no_mid
            pressure = 0.0 if spread <= 0.0001 else price_delta / spread
            threshold = max(
                cfg.base_entry_threshold,
                trading_cost * 1.1 + (volatility * 0.35) + (complement_gap * 0.5),
            )
            raw_edge = max(
                0.0,
                abs(momentum) + abs(price_delta) + (abs(z_score) * 0.0015) - trading_cost - (complement_gap * 0.6),
            )
            expected_edge = raw_edge * liquidity_score
            signal_strength = 0.0 if threshold <= 0 else expected_edge / threshold
            direction = "HOLD"
            if momentum > 0 or z_score > 0:
                direction = "BUY_YES"
            elif momentum < 0 or z_score < 0:
                direction = "BUY_NO"

            freshness_factor = max(0.0, 1.0 - min(1.0, freshness_ms / max(cfg.signal_staleness_seconds * 1000, 1)))
            rank_score = (
                (signal_strength * 0.55)
                + (abs(z_score) * 0.18)
                + (abs(momentum) * 12)
                + (liquidity_score * 0.15)
                + (freshness_factor * 0.12)
            )

            if volatility > 0.03:
                regime = "crisis"
            elif volatility > 0.015:
                regime = "high_vol"
            elif volatility < 0.005:
                regime = "low_vol"
            else:
                regime = "normal"

            explain = [
                f"edge {expected_edge:.4%} vs threshold {threshold:.4%}",
                f"z-score {z_score:.2f} with momentum {momentum:.4f}",
                f"freshness {freshness_ms}ms and complement gap {complement_gap:.4f}",
            ]
            views.append(
                AnalyticsView(
                    market_id=market.market_id,
                    title=market.title,
                    end_date=market.end_date,
                    token_id_yes=market.token_id_yes,
                    token_id_no=market.token_id_no,
                    best_bid=yes_latest.best_bid,
                    best_ask=yes_latest.best_ask,
                    mid_price=latest_mid,
                    no_mid_price=no_mid,
                    spread=spread,
                    no_spread=no_spread,
                    volatility=volatility,
                    rolling_mean=rolling_mean,
                    rolling_std=rolling_std,
                    z_score=z_score,
                    price_delta=price_delta,
                    momentum=momentum,
                    imbalance=imbalance,
                    pressure=pressure,
                    complement_gap=complement_gap,
                    freshness_ms=freshness_ms,
                    source_delay_ms=source_delay_ms,
                    observations=len(yes_series),
                    expected_edge=expected_edge,
                    entry_threshold=threshold,
                    signal_strength=signal_strength,
                    rank_score=rank_score,
                    direction=direction,
                    regime=regime,
                    liquidity_score=liquidity_score,
                    quote_source=market.latest_source(),
                    bootstrap_only=market.bootstrap_only,
                    explain=explain,
                )
            )

        ranked = sorted(
            views,
            key=lambda item: (item.rank_score, item.signal_strength, item.expected_edge),
            reverse=True,
        )
        summary = {
            "tracked_markets": len(ranked),
            "opportunity_count": sum(1 for item in ranked if item.signal_strength >= cfg.min_signal_strength),
            "top_signal_score": round_float(ranked[0].signal_strength, 4) if ranked else 0.0,
            "top_edge": round_float(ranked[0].expected_edge, 6) if ranked else 0.0,
            "avg_freshness_ms": int(mean([item.freshness_ms for item in ranked])) if ranked else 0,
            "avg_volatility": round_float(mean([item.volatility for item in ranked]), 6) if ranked else 0.0,
        }
        return ranked, summary


@dataclass(frozen=True, slots=True)
class DecisionView:
    market_id: str
    title: str
    action: str
    executable: bool
    side: str
    confidence: float
    cooldown_remaining_ms: int
    reasons: list[str]
    rejections: list[str]
    summary: str
    analytics_refs: dict[str, float | int | str]

    def to_snapshot(self) -> dict:
        return {
            "market_id": self.market_id,
            "title": self.title,
            "action": self.action,
            "executable": self.executable,
            "side": self.side,
            "confidence": round_float(self.confidence, 4),
            "cooldown_remaining_ms": self.cooldown_remaining_ms,
            "reasons": self.reasons,
            "rejections": self.rejections,
            "summary": self.summary,
            "analytics_refs": self.analytics_refs,
        }


class DecisionEngine:
    def evaluate(
        self,
        analytics_rows: list[AnalyticsView],
        *,
        bot_status: str,
        cfg: RiskConfig,
        now_ts: float,
        portfolio: PortfolioState,
        open_positions_by_market: dict[str, dict],
        last_trade_ts_by_market: dict[str, float],
    ) -> tuple[list[DecisionView], dict]:
        max_exposure = max(0.0, portfolio.equity * cfg.max_total_exposure_pct)
        exposure_remaining = max(0.0, max_exposure - portfolio.capital_in_trade)
        open_position_count = len(open_positions_by_market)
        slots_remaining = max(0, cfg.max_concurrent_positions - open_position_count)

        decisions: list[DecisionView] = []
        for row in analytics_rows:
            has_position = row.market_id in open_positions_by_market
            cooldown_remaining_ms = max(
                0,
                int((cfg.cooldown_seconds - (now_ts - last_trade_ts_by_market.get(row.market_id, 0))) * 1000),
            )

            reasons: list[str] = []
            rejections: list[str] = []
            action = "HOLD"
            executable = False

            if bot_status != "RUNNING":
                rejections.append(f"bot status is {bot_status.lower()}")
            if row.freshness_ms > cfg.signal_staleness_seconds * 1000:
                rejections.append(
                    f"freshness {row.freshness_ms}ms exceeds {cfg.signal_staleness_seconds * 1000}ms"
                )
            if row.observations < cfg.min_observations:
                rejections.append(
                    f"observations {row.observations} below minimum {cfg.min_observations}"
                )
            if row.spread > cfg.spread_cap:
                rejections.append(f"spread {row.spread:.4f} above cap {cfg.spread_cap:.4f}")
            if row.complement_gap > 0.03:
                rejections.append(f"complement gap {row.complement_gap:.4f} too wide")

            confidence = max(
                0.0,
                min(
                    1.0,
                    (row.signal_strength / max(cfg.min_signal_strength, 0.0001)) * 0.55
                    + max(0.0, 1 - (row.freshness_ms / max(cfg.signal_staleness_seconds * 1000, 1))) * 0.25
                    + min(1.0, row.liquidity_score) * 0.2,
                ),
            )

            if has_position:
                if row.freshness_ms > cfg.signal_staleness_seconds * 2000:
                    action = "CLOSE"
                    executable = True
                    reasons.append("position exit on stale analytics")
                elif row.direction == "HOLD" or row.expected_edge <= row.entry_threshold * 0.3:
                    action = "REDUCE"
                    reasons.append("signal weakened below maintenance threshold")
                elif (
                    open_positions_by_market[row.market_id]["side"] == "BUY_YES" and row.direction == "BUY_NO"
                ) or (
                    open_positions_by_market[row.market_id]["side"] == "BUY_NO" and row.direction == "BUY_YES"
                ):
                    action = "CLOSE"
                    executable = True
                    reasons.append("signal direction reversed against open position")
                else:
                    action = "HOLD"
                    reasons.append("position remains aligned with analytics")
            else:
                noise_filtered = abs(row.z_score) < 0.35 and abs(row.momentum) < (row.entry_threshold * 0.8)
                if noise_filtered:
                    rejections.append("anti-noise filter rejected weak move")
                if cooldown_remaining_ms > 0:
                    rejections.append(f"cooldown active for {cooldown_remaining_ms}ms")
                if open_position_count >= cfg.max_concurrent_positions:
                    rejections.append("max concurrent positions reached")
                if exposure_remaining <= 0:
                    rejections.append("portfolio exposure cap reached")

                if not rejections and row.signal_strength >= cfg.min_signal_strength:
                    action = "OPEN"
                    executable = slots_remaining > 0 and exposure_remaining > 0
                    reasons.extend(
                        [
                            f"signal score {row.signal_strength:.2f} cleared threshold {cfg.min_signal_strength:.2f}",
                            f"edge {row.expected_edge:.4%} exceeds entry threshold {row.entry_threshold:.4%}",
                        ]
                    )
                elif not rejections:
                    reasons.append("analytics remain below entry threshold")

            if action == "OPEN" and not executable:
                rejections.append("risk filters removed execution slot")
                action = "HOLD"

            summary = reasons[0] if reasons else (rejections[0] if rejections else "no action")
            decisions.append(
                DecisionView(
                    market_id=row.market_id,
                    title=row.title,
                    action=action if not rejections or has_position else "REJECT",
                    executable=executable,
                    side=row.direction,
                    confidence=confidence,
                    cooldown_remaining_ms=cooldown_remaining_ms,
                    reasons=reasons,
                    rejections=rejections,
                    summary=summary,
                    analytics_refs={
                        "expected_edge": round_float(row.expected_edge, 6),
                        "entry_threshold": round_float(row.entry_threshold, 6),
                        "signal_strength": round_float(row.signal_strength, 4),
                        "z_score": round_float(row.z_score, 4),
                        "freshness_ms": row.freshness_ms,
                        "spread": round_float(row.spread, 4),
                    },
                )
            )

        ranked = sorted(
            decisions,
            key=lambda item: (
                1 if item.action == "OPEN" else 0,
                1 if item.action == "CLOSE" else 0,
                item.confidence,
            ),
            reverse=True,
        )
        summary = {
            "actionable_count": sum(
                1 for item in ranked if item.action in {"OPEN", "CLOSE", "REDUCE"} and item.executable
            ),
            "open_count": sum(1 for item in ranked if item.action == "OPEN"),
            "close_count": sum(1 for item in ranked if item.action == "CLOSE"),
            "reduce_count": sum(1 for item in ranked if item.action == "REDUCE"),
            "reject_count": sum(1 for item in ranked if item.action == "REJECT"),
            "slots_remaining": slots_remaining,
            "exposure_remaining": round_float(exposure_remaining, 2),
        }
        return ranked, summary
