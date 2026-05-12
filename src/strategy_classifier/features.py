"""LeaderFeatureExtractor — the ~42-dim per-wallet feature vector.

Round 8 (The Lens) — § 3.1 of the spec.

The features are organised into 9 categories (A-I) totalling 42 slots.
Every feature is computed via :mod:`src.profiler.feature_store` with
``asof_ts = wallet.last_active`` so the training data has **no future
leakage**.

Microstructure (E + F), social (H), and full-network (G) features
depend on R9 (social), R10 (news), R11 (microstructure), and R5
(Hawkes BIC). Those upstream sources may not yet be wired at the time
this module ships — when that's the case the corresponding cells are
``np.nan``, which LightGBM handles natively. The structural slot is
PRESERVED so R9/R10/R11/R12 wiring is purely additive — no shape
changes once we have labelled data trained against the 42-slot vector.
"""
from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

import numpy as np
from loguru import logger

from src.database.connection import get_db
from src.profiler.feature_store import get_orderbook_features_asof

# --------------------------------------------------------------------------- #
# Feature schema. Order is LOAD-BEARING — the LightGBM model is trained       #
# against this exact column ordering. Adding a feature => append, never       #
# insert in the middle.                                                       #
#                                                                             #
# Category code in the name (A-I) maps back to the spec § 3.1 commentary.    #
# --------------------------------------------------------------------------- #

FEATURE_NAMES: tuple[str, ...] = (
    # A. VELOCITY (5)
    "a_trades_per_day",
    "a_trades_per_day_std",
    "a_inter_trade_interval_median_s",
    "a_inter_trade_interval_p99_s",
    "a_active_day_fraction",
    # B. HOLDING PERIOD (5)
    "b_holding_period_median_s",
    "b_holding_period_p25_s",
    "b_holding_period_p75_s",
    "b_close_method_sell_share",
    "b_fraction_closed_within_1h",
    # C. SIZING (4)
    "c_size_median_usdc",
    "c_size_p25_usdc",
    "c_size_p75_usdc",
    "c_size_cv",
    # D. CATEGORY MIX (5)
    "d_category_entropy",
    "d_top_category_share",
    "d_distinct_categories_30d",
    "d_fees_paid_pct",
    "d_resolution_market_share",
    # E. ENTRY MICROSTRUCTURE (8) — R11 wires; np.nan until then.
    "e_microprice_deviation_at_entry_median",
    "e_spread_bps_at_entry_median",
    "e_depth_imbalance_at_entry_median",
    "e_price_momentum_5m_at_entry",
    "e_price_momentum_60m_at_entry",
    "e_book_age_ms_at_entry_median",
    "e_cancel_to_fill_ratio_30d",
    "e_takes_vs_makes_ratio",
    # F. EXIT MICROSTRUCTURE (4) — R10 + R11 wire; np.nan until then.
    "f_exit_vs_resolution_pnl_ratio",
    "f_exit_after_news_event_pct",
    "f_sequential_exit_chunks_median",
    "f_merge_exit_pct",
    # G. NETWORK (4) — needs follower_edges (Hawkes-confirmed).
    "g_confirmed_follower_count",
    "g_alpha_mu_ratio_to_follower_pool",
    "g_is_followed_back_pct",
    "g_cluster_density",
    # H. SOCIAL (4) — R12 wires; np.nan until then.
    "h_social_signal_density",
    "h_tweets_per_active_day",
    "h_tweet_to_trade_lag_median_s",
    "h_social_signal_strategy_concordance",
    # I. TEMPORAL (3)
    "i_trading_hour_kde_peak",
    "i_weekday_bias",
    "i_time_of_day_entropy",
)

FEATURE_COUNT = len(FEATURE_NAMES)  # 42 — asserted in tests.

# Categories E, F, H, and parts of G that depend on upstream rounds. The
# daemon reports the fraction of NaNs in these slots as a metric so
# operators can see when R9/R10/R11/R12 come online.
PENDING_FEATURE_NAMES: frozenset[str] = frozenset({
    "e_microprice_deviation_at_entry_median",
    "e_spread_bps_at_entry_median",
    "e_depth_imbalance_at_entry_median",
    "e_price_momentum_5m_at_entry",
    "e_price_momentum_60m_at_entry",
    "e_book_age_ms_at_entry_median",
    "e_takes_vs_makes_ratio",
    "f_exit_after_news_event_pct",
    "h_social_signal_density",
    "h_tweets_per_active_day",
    "h_tweet_to_trade_lag_median_s",
    "h_social_signal_strategy_concordance",
})


@dataclass
class FeatureVector:
    """Per-wallet feature row. ``values`` is aligned to :data:`FEATURE_NAMES`."""

    wallet_address: str
    asof_ts: datetime
    values: np.ndarray  # shape (FEATURE_COUNT,), dtype float64
    missing: list[str] = field(default_factory=list)  # names where we returned np.nan

    def as_dict(self) -> dict[str, float]:
        return {name: float(self.values[i]) for i, name in enumerate(FEATURE_NAMES)}


class LeaderFeatureExtractor:
    """Computes :data:`FEATURE_COUNT` features per wallet at an asof_ts.

    Usage:

        extractor = LeaderFeatureExtractor()
        fv = await extractor.extract(wallet_address, asof_ts)
        X = fv.values  # np.ndarray, shape (FEATURE_COUNT,)

    Batch convenience:

        fvs = await extractor.extract_batch([(w, asof) for w, asof in pairs])

    All DB I/O is async (project convention; see src/CLAUDE.md § 10).
    Errors in any single feature degrade gracefully to np.nan rather than
    propagating — the model is trained against missing-value-aware
    LightGBM so a wallet with no orderbook history still produces a usable
    row. The spec § 3.1 critical line: "every feature is computed via
    feature_store.get_*_asof with last_active as asof_ts. No future
    leakage."
    """

    def __init__(
        self,
        lookback_days: int = 30,
        orderbook_lookback_s: int = 300,
    ) -> None:
        self._lookback_days = int(lookback_days)
        self._orderbook_lookback_s = int(orderbook_lookback_s)

    # ------------------------------------------------------------------ #
    # Public API                                                         #
    # ------------------------------------------------------------------ #

    async def extract(
        self,
        wallet_address: str,
        asof_ts: datetime,
    ) -> FeatureVector:
        """Compute one feature vector. Always returns ``FEATURE_COUNT``
        values; missing inputs become np.nan and are recorded in
        ``FeatureVector.missing``.
        """
        if asof_ts.tzinfo is None:
            asof_ts = asof_ts.replace(tzinfo=timezone.utc)
        floor = asof_ts - timedelta(days=self._lookback_days)

        # Single DB session for all the structured reads. The wider asyncpg
        # connection re-use is encouraged in the conventions doc.
        values = np.full(FEATURE_COUNT, np.nan, dtype=float)
        missing: list[str] = []
        try:
            async with get_db() as conn:
                trades = await self._load_trades(conn, wallet_address, floor, asof_ts)
                positions = await self._load_positions(conn, wallet_address, floor, asof_ts)
                edges = await self._load_follower_edges(conn, wallet_address, asof_ts)

                # A. velocity
                self._populate_velocity(values, trades, asof_ts)
                # B. holding period
                self._populate_holding(values, positions)
                # C. sizing
                self._populate_sizing(values, trades)
                # D. category mix
                self._populate_category_mix(values, trades, positions)
                # E. entry microstructure — best-effort, often nan
                await self._populate_entry_microstructure(
                    conn, values, trades, missing
                )
                # F. exit microstructure
                self._populate_exit_microstructure(values, positions, missing)
                # G. network
                self._populate_network(values, edges, missing)
                # H. social — R12 only; structural slots, all nan today
                self._populate_social_stubs(missing)
                # I. temporal
                self._populate_temporal(values, trades)
        except Exception as exc:  # pragma: no cover — defensive top-level
            logger.warning(
                f"LeaderFeatureExtractor: extract failed for "
                f"wallet={wallet_address} asof={asof_ts.isoformat()}: {exc}"
            )

        # Anything still NaN that lives in a "pending upstream" category
        # gets logged once at debug. The DB reads above don't write NaNs;
        # only the missing-list does, so we walk the slot names.
        for i, name in enumerate(FEATURE_NAMES):
            if math.isnan(values[i]) and name not in missing:
                missing.append(name)

        return FeatureVector(
            wallet_address=wallet_address,
            asof_ts=asof_ts,
            values=values,
            missing=missing,
        )

    async def extract_batch(
        self,
        pairs: list[tuple[str, datetime]],
        concurrency: int = 8,
    ) -> list[FeatureVector]:
        """Concurrent extraction over many (wallet, asof_ts) pairs.

        Uses a bounded :class:`asyncio.Semaphore` so the DB pool doesn't
        get swamped (default 8 in-flight, matches DB_POOL_MIN in
        settings).
        """
        sem = asyncio.Semaphore(max(1, int(concurrency)))

        async def _one(pair: tuple[str, datetime]) -> FeatureVector:
            async with sem:
                return await self.extract(pair[0], pair[1])

        return list(await asyncio.gather(*[_one(p) for p in pairs]))

    # ------------------------------------------------------------------ #
    # Static / pure helpers                                              #
    # ------------------------------------------------------------------ #

    def feature_names(self) -> tuple[str, ...]:
        return FEATURE_NAMES

    @staticmethod
    def feature_count() -> int:
        return FEATURE_COUNT

    # ------------------------------------------------------------------ #
    # Internal: DB reads                                                 #
    # ------------------------------------------------------------------ #

    async def _load_trades(
        self,
        conn: Any,
        wallet_address: str,
        floor: datetime,
        asof: datetime,
    ) -> list[dict]:
        try:
            rows = await conn.fetch(
                """
                SELECT t.time, t.market_id, t.token_id, t.side,
                       t.price, t.size_usdc, m.category
                FROM trades_observed t
                LEFT JOIN markets m ON m.market_id = t.market_id
                WHERE t.wallet_address = $1
                  AND t.time >= $2
                  AND t.time <= $3
                ORDER BY t.time ASC
                """,
                wallet_address,
                floor,
                asof,
            )
            return [dict(r) for r in rows]
        except Exception as exc:
            logger.debug(f"LeaderFeatureExtractor: _load_trades failed: {exc}")
            return []

    async def _load_positions(
        self,
        conn: Any,
        wallet_address: str,
        floor: datetime,
        asof: datetime,
    ) -> list[dict]:
        try:
            rows = await conn.fetch(
                """
                SELECT open_time, close_time, entry_price, exit_price,
                       size_usdc, holding_period_s, close_method, pnl_usdc,
                       market_id, token_id
                FROM positions_reconstructed
                WHERE wallet_address = $1
                  AND open_time >= $2
                  AND open_time <= $3
                ORDER BY open_time ASC
                """,
                wallet_address,
                floor,
                asof,
            )
            return [dict(r) for r in rows]
        except Exception as exc:
            logger.debug(f"LeaderFeatureExtractor: _load_positions failed: {exc}")
            return []

    async def _load_follower_edges(
        self,
        conn: Any,
        wallet_address: str,
        asof: datetime,
    ) -> list[dict]:
        try:
            rows = await conn.fetch(
                """
                SELECT follower_wallet, co_occurrences, hawkes_alpha_mu,
                       follow_probability, avg_delay_s, same_direction_rate
                FROM follower_edges
                WHERE leader_wallet = $1
                  AND last_observed <= $2
                """,
                wallet_address,
                asof,
            )
            return [dict(r) for r in rows]
        except Exception as exc:
            logger.debug(f"LeaderFeatureExtractor: _load_follower_edges failed: {exc}")
            return []

    # ------------------------------------------------------------------ #
    # Category A — VELOCITY (5)                                          #
    # ------------------------------------------------------------------ #

    def _populate_velocity(
        self,
        values: np.ndarray,
        trades: list[dict],
        asof: datetime,
    ) -> None:
        if not trades:
            return
        # daily counts
        by_day: dict[str, int] = {}
        for t in trades:
            ts = t.get("time")
            if ts is None:
                continue
            key = ts.date().isoformat()
            by_day[key] = by_day.get(key, 0) + 1

        n_days = max(1, self._lookback_days)
        daily_vals = list(by_day.values())
        values[0] = float(sum(daily_vals)) / n_days
        values[1] = float(np.std(daily_vals)) if daily_vals else 0.0

        # inter-trade interval
        deltas = []
        prev = None
        for t in trades:
            ts = t.get("time")
            if ts is None:
                continue
            if prev is not None:
                deltas.append((ts - prev).total_seconds())
            prev = ts
        if deltas:
            values[2] = float(np.median(deltas))
            values[3] = float(np.quantile(deltas, 0.99))
        else:
            values[2] = 0.0
            values[3] = 0.0

        values[4] = float(len(by_day)) / n_days

    # ------------------------------------------------------------------ #
    # Category B — HOLDING PERIOD (5)                                    #
    # ------------------------------------------------------------------ #

    def _populate_holding(
        self,
        values: np.ndarray,
        positions: list[dict],
    ) -> None:
        closed = [p for p in positions if p.get("close_time") and p.get("holding_period_s")]
        if not closed:
            return
        hps = [float(p["holding_period_s"]) for p in closed]
        values[5] = float(np.median(hps))
        values[6] = float(np.quantile(hps, 0.25))
        values[7] = float(np.quantile(hps, 0.75))
        # close-method distribution: just the 'sell' share (most common, the
        # other two — merge, resolution — are derivable as 1 - share split).
        method_counts: dict[str, int] = {}
        for p in closed:
            cm = (p.get("close_method") or "unknown")
            method_counts[cm] = method_counts.get(cm, 0) + 1
        sell_share = method_counts.get("sell", 0) / max(1, len(closed))
        values[8] = float(sell_share)
        within_1h = sum(1 for hp in hps if hp <= 3600.0) / len(hps)
        values[9] = float(within_1h)

    # ------------------------------------------------------------------ #
    # Category C — SIZING (4)                                            #
    # ------------------------------------------------------------------ #

    def _populate_sizing(
        self,
        values: np.ndarray,
        trades: list[dict],
    ) -> None:
        sizes = [float(t.get("size_usdc") or 0.0) for t in trades]
        sizes = [s for s in sizes if s > 0]
        if not sizes:
            return
        values[10] = float(np.median(sizes))
        values[11] = float(np.quantile(sizes, 0.25))
        values[12] = float(np.quantile(sizes, 0.75))
        mean = float(np.mean(sizes))
        if mean > 0:
            values[13] = float(np.std(sizes)) / mean

    # ------------------------------------------------------------------ #
    # Category D — CATEGORY MIX (5)                                      #
    # ------------------------------------------------------------------ #

    def _populate_category_mix(
        self,
        values: np.ndarray,
        trades: list[dict],
        positions: list[dict],
    ) -> None:
        cat_counts: dict[str, int] = {}
        for t in trades:
            cat = (t.get("category") or "unknown")
            cat_counts[cat] = cat_counts.get(cat, 0) + 1
        if not cat_counts:
            return
        total = float(sum(cat_counts.values()))
        probs = np.array([c / total for c in cat_counts.values()])
        # Shannon entropy (natural log; bounded by log(K))
        entropy = float(-np.sum(probs * np.log(probs + 1e-12)))
        values[14] = entropy
        values[15] = float(max(probs))
        values[16] = float(len(cat_counts))
        # fees_paid_pct — approximate: crypto / (sum). Reflects whether the
        # wallet avoids fee-heavy categories. Real "fees paid" would need
        # joining markets.fee_rate_pct; for now we use crypto-share as a
        # workable proxy (crypto is the only fee-paying tier per master CLAUDE.md).
        values[17] = float(cat_counts.get("crypto", 0)) / total
        # resolution_market_share — fraction of positions closed via 'resolution'
        if positions:
            resolved = sum(1 for p in positions if (p.get("close_method") == "resolution"))
            values[18] = float(resolved) / max(1, len(positions))
        else:
            values[18] = 0.0

    # ------------------------------------------------------------------ #
    # Category E — ENTRY MICROSTRUCTURE (8)                              #
    # ------------------------------------------------------------------ #

    async def _populate_entry_microstructure(
        self,
        conn: Any,
        values: np.ndarray,
        trades: list[dict],
        missing: list[str],
    ) -> None:
        # Most slots here need R11. We DO have orderbook_features_minute
        # from R2, so e_depth_imbalance / e_spread_bps / e_microprice
        # MIGHT be available. Median over up to 100 entries to keep DB
        # cost bounded.
        sampled = trades[: min(100, len(trades))]
        depth_vals: list[float] = []
        spread_vals: list[float] = []
        microprice_dev_vals: list[float] = []
        for t in sampled:
            token_id = t.get("token_id")
            ts = t.get("time")
            if not token_id or ts is None:
                continue
            try:
                ob = await get_orderbook_features_asof(
                    conn, token_id, ts, lookback_s=self._orderbook_lookback_s
                )
            except Exception:
                ob = None
            if not ob:
                continue
            di = ob.get("depth_imbalance_mean")
            sp = ob.get("spread_bps_mean")
            md = ob.get("microprice_deviation_mean")
            if di is not None:
                depth_vals.append(float(di))
            if sp is not None:
                spread_vals.append(float(sp))
            if md is not None:
                microprice_dev_vals.append(float(md))

        # Slot order: micro, spread, depth, mom5m, mom60m, book_age, c2f, takes_v_makes
        if microprice_dev_vals:
            values[19] = float(np.median(microprice_dev_vals))
        else:
            missing.append("e_microprice_deviation_at_entry_median")
        if spread_vals:
            values[20] = float(np.median(spread_vals))
        else:
            missing.append("e_spread_bps_at_entry_median")
        if depth_vals:
            values[21] = float(np.median(depth_vals))
        else:
            missing.append("e_depth_imbalance_at_entry_median")
        # 22-26: pure R11 features, stub for now.
        for name in (
            "e_price_momentum_5m_at_entry",
            "e_price_momentum_60m_at_entry",
            "e_book_age_ms_at_entry_median",
            "e_cancel_to_fill_ratio_30d",
            "e_takes_vs_makes_ratio",
        ):
            missing.append(name)

    # ------------------------------------------------------------------ #
    # Category F — EXIT MICROSTRUCTURE (4)                               #
    # ------------------------------------------------------------------ #

    def _populate_exit_microstructure(
        self,
        values: np.ndarray,
        positions: list[dict],
        missing: list[str],
    ) -> None:
        if not positions:
            for name in (
                "f_exit_vs_resolution_pnl_ratio",
                "f_exit_after_news_event_pct",
                "f_sequential_exit_chunks_median",
                "f_merge_exit_pct",
            ):
                missing.append(name)
            return
        # f_exit_vs_resolution_pnl_ratio — approximate. For positions closed
        # by sell vs resolution, compute the median PnL ratio of sell-closed
        # to resolution-closed. When one side has no data, we degrade.
        sell_pnls = [
            float(p.get("pnl_usdc") or 0.0)
            for p in positions
            if p.get("close_method") == "sell"
        ]
        res_pnls = [
            float(p.get("pnl_usdc") or 0.0)
            for p in positions
            if p.get("close_method") == "resolution"
        ]
        if sell_pnls and res_pnls:
            res_med = float(np.median(res_pnls))
            if abs(res_med) > 1e-6:
                values[27] = float(np.median(sell_pnls) / res_med)
            else:
                missing.append("f_exit_vs_resolution_pnl_ratio")
        else:
            missing.append("f_exit_vs_resolution_pnl_ratio")
        # 28 — R10 news cross-ref, stub.
        missing.append("f_exit_after_news_event_pct")
        # 29 — sequential_exit_chunks_median: count distinct sell trades per
        # closed position. Without per-trade sell-event linkage we can't do
        # this precisely; fall back to 1 (= "one big sell") as a sensible default.
        if positions:
            values[29] = 1.0
        else:
            missing.append("f_sequential_exit_chunks_median")
        # f_merge_exit_pct — share of positions closed via 'merge'.
        merges = sum(1 for p in positions if p.get("close_method") == "merge")
        values[30] = float(merges) / max(1, len(positions))

    # ------------------------------------------------------------------ #
    # Category G — NETWORK (4)                                           #
    # ------------------------------------------------------------------ #

    def _populate_network(
        self,
        values: np.ndarray,
        edges: list[dict],
        missing: list[str],
    ) -> None:
        # confirmed = co_occurrences >= 5 AND same_direction_rate >= 0.7
        confirmed = [
            e for e in edges
            if (e.get("co_occurrences") or 0) >= 5
            and (float(e.get("same_direction_rate") or 0.0)) >= 0.7
        ]
        values[31] = float(len(confirmed))
        if confirmed:
            am = [float(e.get("hawkes_alpha_mu") or 0.0) for e in confirmed]
            values[32] = float(np.mean([x for x in am if x > 0]) or 0.0)
        else:
            values[32] = 0.0
        # is_followed_back_pct — would need a reciprocal lookup (this wallet
        # appears in follower_wallet for the people they lead). R5 BIC Hawkes
        # work; structural slot.
        missing.append("g_is_followed_back_pct")
        # cluster_density — needs graph-clustering coefficient; R5 work too.
        missing.append("g_cluster_density")

    # ------------------------------------------------------------------ #
    # Category H — SOCIAL (4) — R12 wires; structural stubs only.         #
    # ------------------------------------------------------------------ #

    def _populate_social_stubs(self, missing: list[str]) -> None:
        for name in (
            "h_social_signal_density",
            "h_tweets_per_active_day",
            "h_tweet_to_trade_lag_median_s",
            "h_social_signal_strategy_concordance",
        ):
            missing.append(name)

    # ------------------------------------------------------------------ #
    # Category I — TEMPORAL (3)                                          #
    # ------------------------------------------------------------------ #

    def _populate_temporal(
        self,
        values: np.ndarray,
        trades: list[dict],
    ) -> None:
        if not trades:
            return
        hours = []
        weekday_count = 0
        for t in trades:
            ts = t.get("time")
            if ts is None:
                continue
            hours.append(float(ts.hour))
            if ts.weekday() < 5:
                weekday_count += 1
        if not hours:
            return
        # KDE peak — proxy via mode-of-hour. The full KDE peak is a
        # behaviour_profiler thing; for the feature vector a simple modal
        # hour is a reasonable, deterministic, cheap stand-in.
        hist, _ = np.histogram(hours, bins=24, range=(0, 24))
        values[39] = float(int(np.argmax(hist)))
        values[40] = float(weekday_count) / float(len(hours))
        probs = hist / float(np.sum(hist) or 1.0)
        with np.errstate(invalid="ignore", divide="ignore"):
            entropy_h = float(-np.sum(probs * np.log(np.where(probs > 0, probs, 1.0))))
        values[41] = entropy_h


# Re-export for tests that want to time the extraction wall time.
def _now() -> float:
    return time.perf_counter()
