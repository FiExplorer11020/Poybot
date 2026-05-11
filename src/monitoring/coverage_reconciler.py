"""Cross-source coverage observability (Round 6 / The Spine § 3.7).

WAVE-1 ARCHITECT SKELETON. Bodies intentionally not implemented; Wave 2
fills them in. See docs/ROUND_6_THE_SPINE.md § 3.7.

Every 5 minutes, for the previous 5-minute window:
  * Count trades by source: onchain, rest_poll, ws_observer, falcon_556.
  * Compute pairwise disagreement: trades seen by source A but not B.
  * Emit metrics:
      polybot_coverage_disagreement_total{primary, missed_by}
      polybot_coverage_ratio{source}  (= trades_seen / chain_truth)

If onchain (the source of truth) shows N trades and rest_poll shows
less than 95% of N, that's the alert that fires before the operator
notices any hole. This is the actual closure of the
'data-acquisition holes' problem.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any


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
    ) -> None:
        """
        Args:
            window_s: Width of each reconciliation window. None =>
                read ``settings.COVERAGE_RECONCILER_WINDOW_S``.
            alert_threshold: Minimum acceptable
                ``coverage_ratio{source}`` before the
                ``TradeIngestionCoverageLow`` alert fires. None =>
                ``settings.COVERAGE_ALERT_THRESHOLD``.
        """
        # TODO: implement in Wave 2 — see ROUND_6_THE_SPINE.md § 3.7
        raise NotImplementedError("Wave 2 — see ROUND_6_THE_SPINE.md § 3.7")

    async def reconcile_window(
        self,
        window_start: datetime,
        window_end: datetime,
    ) -> dict[str, Any]:
        """Compare every source over ``[window_start, window_end)``.

        Algorithm (Wave 2):
          1. SELECT trades from trades_observed in the window, grouped
             by source. Group: {'onchain', 'websocket', 'api_market',
             'api_wallet', 'falcon_556'}.
          2. Build per-source identity sets (a "trade identity" is
             ``(wallet_address, market_id, time_bucket_ms, side, price,
             size_usdc)`` — the same key the natural-key UNIQUE INDEX
             uses).
          3. Compute pairwise differences: ``identities[A] -
             identities[B]`` for each (A, B). Emit
             ``polybot_coverage_disagreement_total{primary=A, missed_by=B}``
             with the cardinality.
          4. Compute the ratio relative to onchain truth:
             ``coverage_ratio[source] = len(identities[source]) /
                                       len(identities['onchain'])``
             when 'onchain' is non-empty; emit
             ``polybot_coverage_ratio{source}`` gauges.
          5. Return a summary dict for logging.

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
        raise NotImplementedError("Wave 2 — see ROUND_6_THE_SPINE.md § 3.7")

    async def run_periodic(self) -> None:
        """Long-lived loop. Every ``settings.COVERAGE_RECONCILER_WINDOW_S``
        seconds, reconcile the most recent window.

        Robustness contract (Wave 2):
          * Exceptions inside ``reconcile_window`` are logged and
            counted but never break the loop.
          * On shutdown (``asyncio.CancelledError``), exit cleanly
            without re-raising.
        """
        raise NotImplementedError("Wave 2 — see ROUND_6_THE_SPINE.md § 3.7")
