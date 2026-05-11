"""
Order-book imbalance feature pipeline — per-minute rollup of
``book_quality_snapshots`` into ``orderbook_features_minute``.

Phase 3 Round 2 Agent Z. See:
  * docs/audit/05_ml_pipeline.md (summary: "highest-ROI new data source")
  * docs/audit/phase3/round2_Z_orderbook_imbalance.md
  * docs/migrations/018_orderbook_features_minute.sql

What this module does (and does NOT do):

1.  **No raw snapshot writer here.** ``trade_observer._record_book_metrics``
    already writes one ``book_quality_snapshots`` row per WS book update
    (see ``trade_observer.py:1315`` → ``_persist_book_quality_snapshot``).
    Duplicating that writer would double the write load. This module
    treats the raw table as a read-only source.
2.  **Rollup loop.** A long-running coroutine (``OrderBookObserver.start``)
    that wakes every ``ORDERBOOK_ROLLUP_INTERVAL_S`` (default 60 s),
    aggregates the last ``ORDERBOOK_ROLLUP_LOOKBACK_S`` (default 70 s)
    of raw snapshots into per-(market_id, token_id, minute) feature rows,
    and INSERTs them with ``ON CONFLICT DO UPDATE`` so re-runs are
    idempotent.

Rollup math (per (market_id, token_id, minute) group):

    depth_imbalance  = (bid_depth_at_best - ask_depth_at_best)
                       / (bid_depth_at_best + ask_depth_at_best)
                                                       ∈ [-1, +1]
    spread_bps       = (best_ask - best_bid) / midprice * 10000
    microprice       = (best_bid * ask_depth + best_ask * bid_depth)
                       / (bid_depth + ask_depth)
    micro_deviation  = |microprice - midprice|

    *_mean = arithmetic mean across the minute's snapshots
    *_max  = max across the minute (for imbalance: max |x| keeping sign)

Microprice intuition: weights the price toward the THIN side of the book.
A book with bid_depth=100 and ask_depth=10 has microprice closer to
best_ask, signalling the next trade is likely to lift the offer.

Best-effort semantics: if a minute is missed (rollup runs late, DB hiccup,
etc.) the row is missed. Backfill is operator-driven via
``scripts/orderbook_backfill.py`` (not implemented in this round).

See ``src/observer/CLAUDE.md`` for the broader observer module overview.
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from loguru import logger

from src.config import settings
from src.database.connection import get_db

# Optional metrics — gracefully skip if monitoring stack unavailable.
try:  # pragma: no cover — import is exercised in production paths
    from src.monitoring.metrics import (
        orderbook_features_lookup_total,  # noqa: F401  (re-exported here)
        orderbook_rollup_rows_per_run,
        orderbook_rollup_runs_total,
        orderbook_snapshots_ingested_total,  # noqa: F401  (re-exported here)
    )

    _METRICS_AVAILABLE = True
except Exception:  # pragma: no cover
    _METRICS_AVAILABLE = False

# Watchdog heartbeat — write under this name so it matches the registry key.
HEARTBEAT_NAME = "orderbook_observer"

# Config knobs. We resolve via getattr so the module imports cleanly on
# fresh checkouts where the .env hasn't been updated yet — the defaults
# below are the audit-recommended values.
_DEFAULT_ROLLUP_INTERVAL_S = 60
_DEFAULT_ROLLUP_LOOKBACK_S = 70


@dataclass(frozen=True)
class OrderbookRollupRow:
    """In-memory representation of a single per-minute aggregate row.

    Mirrors the columns of ``orderbook_features_minute`` so tests and the
    DB writer share a contract. ``depth_imbalance_max`` is the signed
    value of the snapshot whose |imbalance| was largest in the minute.
    """

    market_id: str
    token_id: str
    bucket_ts: datetime
    depth_imbalance_mean: float | None
    depth_imbalance_max: float | None
    spread_bps_mean: float | None
    spread_bps_max: float | None
    microprice_mean: float | None
    microprice_deviation_mean: float | None
    n_snapshots: int


# --------------------------------------------------------------------------- #
# Pure helpers (unit-testable without DB / asyncio)                            #
# --------------------------------------------------------------------------- #


def _coerce_price_size(level: Any) -> tuple[float, float] | None:
    """Coerce one raw book level into (price, size).

    Accepts both dict form ``{"price": "0.62", "size": "1000"}`` and
    list/tuple form ``["0.62", "1000"]`` — the WS feed uses dicts but
    historical replays and the rare REST snapshot use the tuple form.
    Returns None for malformed / unparseable inputs (size 0 is dropped).
    """
    if level is None:
        return None
    try:
        if isinstance(level, dict):
            raw_price = level.get("price", level.get("p"))
            raw_size = level.get("size", level.get("s"))
        elif isinstance(level, (list, tuple)) and len(level) >= 2:
            raw_price, raw_size = level[0], level[1]
        else:
            return None
        if raw_price is None or raw_size is None:
            return None
        price = float(Decimal(str(raw_price)))
        size = float(Decimal(str(raw_size)))
        if size <= 0:
            return None
        return price, size
    except (InvalidOperation, ValueError, TypeError):
        return None


def _features_from_snapshot(
    bids: list[Any] | None,
    asks: list[Any] | None,
    best_bid: float | None,
    best_ask: float | None,
) -> tuple[float | None, float | None, float | None, float | None]:
    """Compute (depth_imbalance, spread_bps, microprice, micro_deviation)
    for one raw snapshot.

    Returns (None, None, None, None) when the book is one-sided or
    crossed (best_bid >= best_ask) — these are edge cases we don't want
    to feed into the mean/max aggregates.
    """
    if best_bid is None or best_ask is None:
        return (None, None, None, None)
    if best_bid <= 0 or best_ask <= 0 or best_bid >= best_ask:
        return (None, None, None, None)

    bid_top = _coerce_price_size((bids or [None])[0])
    ask_top = _coerce_price_size((asks or [None])[0])
    if bid_top is None or ask_top is None:
        return (None, None, None, None)
    _, bid_size = bid_top
    _, ask_size = ask_top
    total_depth = bid_size + ask_size
    if total_depth <= 0:
        return (None, None, None, None)

    midprice = (best_bid + best_ask) / 2.0
    spread_bps = (best_ask - best_bid) / midprice * 10_000.0
    depth_imbalance = (bid_size - ask_size) / total_depth
    # Microprice weights toward the THIN side: bid * ask_depth + ask * bid_depth
    microprice = (best_bid * ask_size + best_ask * bid_size) / total_depth
    micro_deviation = abs(microprice - midprice)
    return (depth_imbalance, spread_bps, microprice, micro_deviation)


def _truncate_to_minute(ts: datetime) -> datetime:
    """Floor a timestamp to the start of its minute, preserving tz."""
    return ts.replace(second=0, microsecond=0)


def _aggregate_snapshots(
    rows: list[dict[str, Any]],
) -> dict[tuple[str, str, datetime], OrderbookRollupRow]:
    """Aggregate a flat list of raw snapshot dicts into per-bucket rows.

    Each input dict must have: market_id, token_id, observed_at (datetime),
    best_bid (Decimal/float/None), best_ask (Decimal/float/None),
    depth_top_levels (dict or JSON string).

    Snapshots whose features come out as None are counted in n_snapshots
    only via the bucket existing — but per-feature means / maxes are
    computed only over the non-None subset, so a minute that contained
    one crossed book and 59 healthy ones still reports the 59-snapshot
    mean correctly. The bucket itself is dropped if EVERY snapshot was
    unusable.
    """
    # bucket key → running accumulator
    accum: dict[tuple[str, str, datetime], dict[str, Any]] = {}

    for row in rows:
        market_id = row.get("market_id")
        token_id = row.get("token_id")
        observed_at = row.get("observed_at")
        if not market_id or not token_id or observed_at is None:
            continue

        bucket = _truncate_to_minute(observed_at)
        key = (market_id, token_id, bucket)

        depth = row.get("depth_top_levels")
        if isinstance(depth, str):
            try:
                depth = json.loads(depth)
            except (TypeError, ValueError):
                depth = {}
        if not isinstance(depth, dict):
            depth = {}
        bids = depth.get("bids")
        asks = depth.get("asks")

        bb = row.get("best_bid")
        ba = row.get("best_ask")
        best_bid = float(bb) if bb is not None else None
        best_ask = float(ba) if ba is not None else None

        di, sp, mp, md = _features_from_snapshot(bids, asks, best_bid, best_ask)

        slot = accum.setdefault(
            key,
            {
                "di_sum": 0.0, "di_n": 0, "di_signed_abs_max": None,
                "sp_sum": 0.0, "sp_n": 0, "sp_max": None,
                "mp_sum": 0.0, "mp_n": 0,
                "md_sum": 0.0, "md_n": 0,
                "n_total": 0,
            },
        )
        slot["n_total"] += 1

        if di is not None:
            slot["di_sum"] += di
            slot["di_n"] += 1
            cur = slot["di_signed_abs_max"]
            if cur is None or abs(di) > abs(cur):
                slot["di_signed_abs_max"] = di
        if sp is not None:
            slot["sp_sum"] += sp
            slot["sp_n"] += 1
            if slot["sp_max"] is None or sp > slot["sp_max"]:
                slot["sp_max"] = sp
        if mp is not None:
            slot["mp_sum"] += mp
            slot["mp_n"] += 1
        if md is not None:
            slot["md_sum"] += md
            slot["md_n"] += 1

    out: dict[tuple[str, str, datetime], OrderbookRollupRow] = {}
    for key, slot in accum.items():
        if slot["n_total"] == 0:
            continue
        market_id, token_id, bucket = key
        out[key] = OrderbookRollupRow(
            market_id=market_id,
            token_id=token_id,
            bucket_ts=bucket,
            depth_imbalance_mean=(slot["di_sum"] / slot["di_n"]) if slot["di_n"] else None,
            depth_imbalance_max=slot["di_signed_abs_max"],
            spread_bps_mean=(slot["sp_sum"] / slot["sp_n"]) if slot["sp_n"] else None,
            spread_bps_max=slot["sp_max"],
            microprice_mean=(slot["mp_sum"] / slot["mp_n"]) if slot["mp_n"] else None,
            microprice_deviation_mean=(slot["md_sum"] / slot["md_n"]) if slot["md_n"] else None,
            n_snapshots=slot["n_total"],
        )
    return out


# --------------------------------------------------------------------------- #
# OrderBookObserver — long-running rollup loop                                 #
# --------------------------------------------------------------------------- #


class OrderBookObserver:
    """Per-minute rollup loop. Owned by the engine ``Watchdog``.

    Public API:
        * ``start()``   — long-running coroutine, exits on stop_event.
        * ``stop()``    — signal the loop to exit cleanly.
        * ``run_once()`` — execute one rollup pass (used by tests & ops).

    The constructor takes ``interval_s`` and ``lookback_s`` so tests can
    override without monkey-patching ``settings``. Production callers
    pass nothing and get the env-driven defaults.
    """

    def __init__(
        self,
        *,
        redis_client: Any = None,
        interval_s: int | None = None,
        lookback_s: int | None = None,
    ) -> None:
        self._redis = redis_client
        self._interval_s = int(
            interval_s
            if interval_s is not None
            else getattr(settings, "ORDERBOOK_ROLLUP_INTERVAL_S", _DEFAULT_ROLLUP_INTERVAL_S)
        )
        self._lookback_s = int(
            lookback_s
            if lookback_s is not None
            else getattr(settings, "ORDERBOOK_ROLLUP_LOOKBACK_S", _DEFAULT_ROLLUP_LOOKBACK_S)
        )
        if self._interval_s <= 0:
            self._interval_s = _DEFAULT_ROLLUP_INTERVAL_S
        if self._lookback_s < self._interval_s:
            # Always read slightly more than we sleep so we never miss
            # a boundary snapshot due to clock skew.
            self._lookback_s = self._interval_s + 10
        self._stop_event = asyncio.Event()
        self._running = False

    # ------------------------------------------------------------------ #
    # Lifecycle                                                          #
    # ------------------------------------------------------------------ #

    async def start(self) -> None:
        """Long-running coroutine. Re-entrant — calling twice is a no-op."""
        if self._running:
            logger.warning("OrderBookObserver.start called while already running")
            return
        self._running = True
        self._stop_event.clear()
        logger.info(
            "OrderBookObserver started "
            f"(interval={self._interval_s}s, lookback={self._lookback_s}s)"
        )
        try:
            while not self._stop_event.is_set():
                started_at = time.monotonic()
                try:
                    await self.run_once()
                except Exception:
                    logger.exception("OrderBookObserver rollup raised; will retry")
                    if _METRICS_AVAILABLE:
                        try:
                            orderbook_rollup_runs_total.labels(result="error").inc()
                        except Exception:
                            pass
                await self._heartbeat()
                elapsed = time.monotonic() - started_at
                sleep_s = max(1.0, float(self._interval_s) - elapsed)
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=sleep_s)
                except asyncio.TimeoutError:
                    pass
        finally:
            self._running = False
            logger.info("OrderBookObserver stopped")

    async def stop(self) -> None:
        self._stop_event.set()

    async def _heartbeat(self) -> None:
        if self._redis is None:
            return
        try:
            await self._redis.set(
                f"heartbeat:{HEARTBEAT_NAME}",
                str(time.time()),
                ex=4 * self._interval_s,
            )
        except Exception:
            logger.debug("OrderBookObserver heartbeat write failed", exc_info=True)

    # ------------------------------------------------------------------ #
    # Core: one rollup pass                                              #
    # ------------------------------------------------------------------ #

    async def run_once(self, *, now: datetime | None = None) -> int:
        """Execute one rollup pass. Returns the number of (market, token,
        minute) rows written.

        ``now`` is injectable so tests can pin the rollup window without
        freezing wall-clock time globally.
        """
        now_ts = now or datetime.now(tz=timezone.utc)
        window_start = now_ts - timedelta(seconds=self._lookback_s)

        rows = await self._fetch_snapshots(window_start, now_ts)
        if not rows:
            logger.debug(
                "OrderBookObserver: no snapshots in window "
                f"[{window_start.isoformat()}, {now_ts.isoformat()}]"
            )
            if _METRICS_AVAILABLE:
                try:
                    orderbook_rollup_runs_total.labels(result="empty").inc()
                    orderbook_rollup_rows_per_run.observe(0)
                except Exception:
                    pass
            return 0

        aggregates = _aggregate_snapshots(rows)
        if not aggregates:
            if _METRICS_AVAILABLE:
                try:
                    orderbook_rollup_runs_total.labels(result="empty").inc()
                    orderbook_rollup_rows_per_run.observe(0)
                except Exception:
                    pass
            return 0

        written = await self._upsert_rollup_rows(list(aggregates.values()))
        if _METRICS_AVAILABLE:
            try:
                orderbook_rollup_runs_total.labels(result="ok").inc()
                orderbook_rollup_rows_per_run.observe(written)
            except Exception:
                pass
        logger.debug(
            f"OrderBookObserver: rolled up {len(rows)} raw snapshots → "
            f"{written} (market,token,minute) rows"
        )
        return written

    # ------------------------------------------------------------------ #
    # DB I/O                                                              #
    # ------------------------------------------------------------------ #

    async def _fetch_snapshots(
        self, window_start: datetime, window_end: datetime
    ) -> list[dict[str, Any]]:
        """Pull raw book_quality_snapshots between [window_start, window_end).

        We select only the columns the rollup needs; depth_top_levels is
        decoded to a dict by asyncpg (JSONB → dict) so no parsing is
        needed on the Python side.
        """
        try:
            async with get_db() as conn:
                records = await conn.fetch(
                    """
                    SELECT market_id, token_id, observed_at,
                           best_bid, best_ask, depth_top_levels
                    FROM book_quality_snapshots
                    WHERE observed_at >= $1 AND observed_at < $2
                    """,
                    window_start,
                    window_end,
                )
        except Exception:
            logger.exception("OrderBookObserver: fetch_snapshots failed")
            return []
        return [dict(r) for r in records]

    async def _upsert_rollup_rows(self, rows: list[OrderbookRollupRow]) -> int:
        if not rows:
            return 0
        # executemany via asyncpg is the right primitive: per-row ON CONFLICT
        # DO UPDATE makes the operation idempotent and tolerant of partial
        # buckets (a follow-up minute's rollup harmlessly overwrites the
        # previous one with a more-complete count).
        payload = [
            (
                r.market_id,
                r.token_id,
                r.bucket_ts,
                r.depth_imbalance_mean,
                r.depth_imbalance_max,
                r.spread_bps_mean,
                r.spread_bps_max,
                r.microprice_mean,
                r.microprice_deviation_mean,
                r.n_snapshots,
            )
            for r in rows
        ]
        try:
            async with get_db() as conn:
                await conn.executemany(
                    """
                    INSERT INTO orderbook_features_minute
                        (market_id, token_id, bucket_ts,
                         depth_imbalance_mean, depth_imbalance_max,
                         spread_bps_mean, spread_bps_max,
                         microprice_mean, microprice_deviation_mean,
                         n_snapshots)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
                    ON CONFLICT (market_id, token_id, bucket_ts) DO UPDATE SET
                        depth_imbalance_mean      = EXCLUDED.depth_imbalance_mean,
                        depth_imbalance_max       = EXCLUDED.depth_imbalance_max,
                        spread_bps_mean           = EXCLUDED.spread_bps_mean,
                        spread_bps_max            = EXCLUDED.spread_bps_max,
                        microprice_mean           = EXCLUDED.microprice_mean,
                        microprice_deviation_mean = EXCLUDED.microprice_deviation_mean,
                        n_snapshots               = EXCLUDED.n_snapshots
                    """,
                    payload,
                )
        except Exception:
            logger.exception("OrderBookObserver: upsert failed")
            return 0
        return len(payload)
