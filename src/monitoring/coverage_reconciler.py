"""Cross-source coverage observability (Round 6 / The Spine § 3.7).

Every ``COVERAGE_RECONCILER_WINDOW_S`` seconds the reconciler wakes
up and compares trade ingestion across every source writing to
``trades_observed``:

  * ``onchain``     — canonical truth (the chain itself, via
                       ``src/onchain/clob_listener.py``)
  * ``api_market``  — REST poll of data-api.polymarket.com per market
  * ``api_wallet``  — REST poll of data-api.polymarket.com per wallet
  * ``websocket``   — Phase 1 informational stream (not wallet-attributed)

For each tick:

  1. A half-open window ``[now - window - buffer, now - buffer)`` is
     chosen. The 30 s trailing buffer guards against comparing against
     trades that haven't fully replicated to ``trades_observed`` yet —
     async commits from the WS / REST / chain ingest paths can land
     out-of-order over a few-second window.
  2. ``SELECT source, COUNT(*) FROM trades_observed`` grouped by source
     in the window.
  3. ``coverage_ratio[source] = count[source] / count['onchain']`` is
     emitted as a gauge for ``api_market``, ``api_wallet`` and
     ``websocket``. The on-chain ratio is 1.0 by definition.
  4. If ``count['onchain'] == 0`` we skip emission entirely — dividing
     by zero would either crash or produce ``inf``, neither of which
     Prometheus wants. We log INFO once per skip so an operator can
     see "we tried but the chain ingest hadn't caught up".
  5. For each (primary, missed_by) pair in
     ``{(api_market, onchain), (api_wallet, onchain),
       (onchain, api_market), (onchain, api_wallet)}``,
     count trades present in ``primary`` but not in ``missed_by``
     (compared via the natural key
     ``(wallet, market, time, side, price, size_usdc)`` — the same
     shape as ``uq_trades_observed_natural_key``) and increment
     ``polybot_coverage_disagreement_total{primary, missed_by}``.

Alerting (see ``docs/monitoring/alerts.yml``):
  * ``coverage_ratio < 0.95`` for 10 m → critical (a real hole in
    REST polling, or data-api is dropping trades).
  * ``rate(coverage_disagreement_total) > 0.5/s`` for 5 m → warning
    (sustained cross-source disagreement = real ingestion bug, not
    timing noise).

Runtime: the reconciler is a lightweight asyncio task. It runs inside
``polymarket-engine.service`` alongside the existing APScheduler jobs —
no new systemd unit is needed.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any

from loguru import logger

from src.config import settings
from src.database.connection import get_db
from src.monitoring.metrics import (
    coverage_disagreement_total,
    coverage_ratio,
)

# --------------------------------------------------------------------------- #
# Constants                                                                    #
# --------------------------------------------------------------------------- #

# Canonical source names. Match what the WS / REST / chain ingest paths
# write to ``trades_observed.source``. ``onchain`` is the truth.
SOURCE_ONCHAIN = "onchain"
SOURCE_API_MARKET = "api_market"
SOURCE_API_WALLET = "api_wallet"
SOURCE_WEBSOCKET = "websocket"

# Sources whose coverage ratio we publish each cycle. ``onchain`` is
# excluded because its ratio is 1.0 by definition (it IS the denominator).
RATIO_SOURCES: tuple[str, ...] = (SOURCE_API_MARKET, SOURCE_API_WALLET, SOURCE_WEBSOCKET)

# Pairs (primary, missed_by) we compare for disagreement. Bi-directional
# for the wallet-attributed REST paths vs onchain — these are the
# critical comparisons (websocket is informational only, not in the
# disagreement set per § 3.7).
DISAGREEMENT_PAIRS: tuple[tuple[str, str], ...] = (
    (SOURCE_API_MARKET, SOURCE_ONCHAIN),
    (SOURCE_API_WALLET, SOURCE_ONCHAIN),
    (SOURCE_ONCHAIN, SOURCE_API_MARKET),
    (SOURCE_ONCHAIN, SOURCE_API_WALLET),
)

# Trailing buffer: don't compare against the most recent N seconds —
# async commits across the three ingest paths can land out-of-order
# inside a few-second window, and a healthy system would flap warning
# every cycle if we included the trailing edge.
DEFAULT_TRAILING_BUFFER_S = 30


# --------------------------------------------------------------------------- #
# CoverageReconciler                                                           #
# --------------------------------------------------------------------------- #


class CoverageReconciler:
    """Periodic cross-source comparison.

    Lifecycle: ``run_periodic()`` is a long-lived coroutine that the
    engine's APScheduler launches on boot. ``reconcile_window()`` is
    the unit of work — exposed as a separate method so tests and
    ad-hoc tooling can call it without driving the loop.
    """

    def __init__(
        self,
        window_s: int | None = None,
        alert_threshold: float | None = None,
        trailing_buffer_s: int | None = None,
    ) -> None:
        """
        Args:
            window_s: Width of each reconciliation window. ``None`` =
                read ``settings.COVERAGE_RECONCILER_WINDOW_S``.
            alert_threshold: Minimum acceptable
                ``coverage_ratio{source}`` before the
                ``TradeIngestionCoverageLow`` alert fires. ``None`` =
                ``settings.COVERAGE_ALERT_THRESHOLD``. (Kept for the
                introspection API; the actual alert is evaluated by
                Prometheus, not in-process.)
            trailing_buffer_s: Seconds to trim from the trailing edge
                of each window. ``None`` = ``DEFAULT_TRAILING_BUFFER_S``.
        """
        self.window_s = int(
            window_s if window_s is not None else settings.COVERAGE_RECONCILER_WINDOW_S
        )
        self.alert_threshold = float(
            alert_threshold
            if alert_threshold is not None
            else settings.COVERAGE_ALERT_THRESHOLD
        )
        self.trailing_buffer_s = int(
            trailing_buffer_s
            if trailing_buffer_s is not None
            else DEFAULT_TRAILING_BUFFER_S
        )

    # ------------------------------------------------------------------ #
    # Core unit of work                                                   #
    # ------------------------------------------------------------------ #

    async def reconcile_window(
        self,
        window_start: datetime,
        window_end: datetime,
    ) -> dict[str, Any]:
        """Compare every source over ``[window_start, window_end)``.

        Args:
            window_start: Inclusive lower bound.
            window_end: Exclusive upper bound.

        Returns:
            ``{
                "window": (start, end),
                "counts": {source: int, ...},
                "ratios": {source: float, ...},
                "disagreements": {(primary, missed_by): int, ...},
              }``
        """
        counts = await self._count_by_source(window_start, window_end)
        onchain_count = counts.get(SOURCE_ONCHAIN, 0)

        ratios: dict[str, float] = {}
        if onchain_count == 0:
            # No chain ingestion yet — divide-by-zero guard. We log
            # INFO (once per window) rather than WARNING because
            # quiet markets are a normal regime, not a fault.
            logger.info(
                "coverage_reconciler: onchain count is 0 in window "
                f"[{window_start.isoformat()}, {window_end.isoformat()}); "
                "skipping ratio emission"
            )
        else:
            for source in RATIO_SOURCES:
                ratio = counts.get(source, 0) / onchain_count
                ratios[source] = ratio
                try:
                    coverage_ratio.labels(source=source).set(ratio)
                except Exception:
                    # Metric emission must NEVER break the reconciler
                    # — Prometheus registry collisions in tests, etc.
                    logger.exception(
                        f"coverage_reconciler: failed to emit coverage_ratio for {source!r}"
                    )

        # Disagreements: count trades visible to `primary` but not
        # `missed_by`. Bi-directional for the wallet-attributed REST
        # paths vs onchain.
        disagreements: dict[tuple[str, str], int] = {}
        for primary, missed_by in DISAGREEMENT_PAIRS:
            n_missed = await self.find_missed_trades(
                window_start, window_end, primary, missed_by
            )
            disagreements[(primary, missed_by)] = n_missed
            if n_missed > 0:
                try:
                    coverage_disagreement_total.labels(
                        primary=primary, missed_by=missed_by
                    ).inc(n_missed)
                except Exception:
                    logger.exception(
                        "coverage_reconciler: failed to emit coverage_disagreement_total "
                        f"({primary!r} → {missed_by!r})"
                    )

        logger.info(
            "coverage_reconciler: window reconciled",
            window_start=window_start.isoformat(),
            window_end=window_end.isoformat(),
            counts=counts,
            ratios=ratios,
            disagreements={f"{a}->{b}": n for (a, b), n in disagreements.items()},
        )

        return {
            "window": (window_start, window_end),
            "counts": counts,
            "ratios": ratios,
            "disagreements": disagreements,
        }

    # ------------------------------------------------------------------ #
    # Periodic loop                                                       #
    # ------------------------------------------------------------------ #

    async def run_periodic(self) -> None:
        """Long-lived loop.

        Every ``window_s`` seconds: reconcile the window
        ``[now - window_s - buffer, now - buffer)``.

        Robustness contract:
          * Exceptions inside ``reconcile_window`` are logged but never
            break the loop.
          * On shutdown (``asyncio.CancelledError``), exit cleanly
            without re-raising.
        """
        logger.info(
            "coverage_reconciler: starting periodic loop "
            f"(window={self.window_s}s, buffer={self.trailing_buffer_s}s)"
        )
        try:
            while True:
                # Anchor the window relative to wall-clock NOW. The
                # trailing buffer means we never compare against
                # not-yet-fully-replicated trades.
                now = datetime.now(tz=timezone.utc)
                window_end = now - timedelta(seconds=self.trailing_buffer_s)
                window_start = window_end - timedelta(seconds=self.window_s)
                try:
                    await self.reconcile_window(window_start, window_end)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    # Single bad cycle must not kill the loop — this is
                    # the only thing detecting coverage holes.
                    logger.exception(
                        "coverage_reconciler: reconcile_window raised; "
                        "continuing to next cycle"
                    )
                await asyncio.sleep(self.window_s)
        except asyncio.CancelledError:
            logger.info("coverage_reconciler: cancelled, exiting cleanly")
            return

    # ------------------------------------------------------------------ #
    # Internals                                                           #
    # ------------------------------------------------------------------ #

    async def _count_by_source(
        self,
        window_start: datetime,
        window_end: datetime,
    ) -> dict[str, int]:
        """SELECT source, COUNT(*) FROM trades_observed in the window.

        Half-open interval: ``time >= window_start AND time < window_end``.
        Returns a dict with EVERY known source key populated (0 if
        nothing matched) so downstream code doesn't have to special-case
        missing keys.
        """
        counts: dict[str, int] = {
            SOURCE_ONCHAIN: 0,
            SOURCE_API_MARKET: 0,
            SOURCE_API_WALLET: 0,
            SOURCE_WEBSOCKET: 0,
        }
        async with get_db() as conn:
            rows = await conn.fetch(
                """
                SELECT source, COUNT(*) AS n
                  FROM trades_observed
                 WHERE time >= $1
                   AND time <  $2
                 GROUP BY source
                """,
                window_start,
                window_end,
            )
        for row in rows:
            src = row["source"]
            counts[src] = int(row["n"])
        return counts

    async def find_missed_trades(
        self,
        window_start: datetime,
        window_end: datetime,
        source_a: str,
        source_b: str,
    ) -> int:
        """Count trades visible to ``source_a`` but missing from ``source_b``.

        Comparison is the natural-key tuple
        ``(wallet_address, market_id, time, side, price, size_usdc)`` —
        the same shape as ``uq_trades_observed_natural_key``. Half-open
        interval ``[window_start, window_end)``.

        Implementation note: a single ``EXCEPT`` query is both more
        readable and faster than two roundtrips + Python set difference,
        and lets Postgres use the natural-key index for the hash
        aggregate on each side.
        """
        async with get_db() as conn:
            n = await conn.fetchval(
                """
                SELECT COUNT(*) FROM (
                    SELECT wallet_address, market_id, time, side, price, size_usdc
                      FROM trades_observed
                     WHERE source = $1
                       AND time  >= $2
                       AND time  <  $3
                    EXCEPT
                    SELECT wallet_address, market_id, time, side, price, size_usdc
                      FROM trades_observed
                     WHERE source = $4
                       AND time  >= $2
                       AND time  <  $3
                ) AS diff
                """,
                source_a,
                window_start,
                window_end,
                source_b,
            )
        return int(n or 0)
