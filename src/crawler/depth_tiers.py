"""Adaptive depth-tier policy for the wallet universe.

WAVE-1 ARCHITECT SKELETON for the I/O surfaces; the policy function
``expected_tier`` is implemented here because it's a *specification*,
not infra. Wave 2 wires it in to the nightly review loop. See
docs/ROUND_6_THE_SPINE.md § 3.4.

Tier 0 — FULL ENRICHMENT (currently top ~200):
  * All Falcon agents on a daily refresh.
  * Strategy classifier (Round 7).
  * Hawkes pairwise fit (Round 8).
  * Daily decision flow.

Tier 1 — PERIODIC REFRESH (top ~2000 by recent 30d volume):
  * Falcon 581 (Wallet360) + 569 (PnL) weekly.
  * Strategy classifier monthly.
  * Coarse Hawkes against the leader pool.

Tier 2 — LIGHT TRACKING (everyone else, ~1.5M):
  * Just timestamps + sizes + markets from on-chain.
  * No Falcon calls — Falcon would be the bottleneck if we tried.
  * Promoted to Tier 1 if 7-day volume crosses threshold.

Promotion/demotion runs nightly. The bot's compute spend per wallet
is automatically inversely proportional to wallet count per tier.
"""

from __future__ import annotations

import asyncio
from enum import IntEnum
from typing import TYPE_CHECKING, Any

from loguru import logger

from src.config import settings
from src.database.connection import get_db

try:  # pragma: no cover — metrics import is best-effort.
    from src.monitoring.metrics import (  # type: ignore[attr-defined]
        wallet_universe_promotions_total,
        wallet_universe_tier_count,
    )
except Exception:  # pragma: no cover
    wallet_universe_promotions_total = None  # type: ignore[assignment]
    wallet_universe_tier_count = None  # type: ignore[assignment]

if TYPE_CHECKING:
    from src.crawler.universe import WalletUniverse


class DepthTier(IntEnum):
    """One of the three tiers a wallet can sit in.

    IntEnum because the underlying ``wallet_universe.depth_tier``
    column is SMALLINT — int comparison is convenient and zero-cost.
    """

    FULL = 0
    PERIODIC = 1
    LIGHT = 2


def expected_tier(wallet_stats: dict[str, Any]) -> DepthTier:
    """Pure policy function: given a wallet's recent activity stats,
    return the tier it *should* sit in.

    This is intentionally simple and readable so a Wave-2 reviewer can
    eyeball the policy without chasing through call sites. The actual
    promotion/demotion logic (``AdaptiveDepth.review_tiers``) compares
    the result of this function against the wallet's current tier and
    issues UPDATE statements + promotion-counter increments.

    Required keys on ``wallet_stats`` (Wave 2 builds the dict from a
    single SQL roll-up across wallet_universe + a recent-trades view):

      * ``volume_30d_usdc``: float — total USDC traded over the last
        30 days. The primary tier-decision input.
      * ``volume_7d_usdc``: float (optional) — sanity-check input;
        used in tie-breaking near the threshold.
      * ``trades_30d``: int (optional) — fallback when volume is
        nearly zero but trade count is high (a sign of small swing
        trading rather than a dormant wallet).

    Thresholds live in ``src/config.py``:
      * ``WALLET_UNIVERSE_FULL_TIER_VOLUME_THRESHOLD_USDC``
      * ``WALLET_UNIVERSE_PERIODIC_TIER_VOLUME_THRESHOLD_USDC``

    Args:
        wallet_stats: Dict shaped as described above. Missing optional
            keys are treated as zero.

    Returns:
        :class:`DepthTier` the wallet should be promoted/demoted to.
    """
    # Use .get with a 0 default so a stats dict that omits the optional
    # fields still produces a sensible decision rather than KeyError-ing
    # mid-nightly-loop.
    volume_30d = float(wallet_stats.get("volume_30d_usdc", 0.0) or 0.0)
    full_threshold = float(
        getattr(settings, "WALLET_UNIVERSE_FULL_TIER_VOLUME_THRESHOLD_USDC", 1_000_000.0)
    )
    periodic_threshold = float(
        getattr(
            settings,
            "WALLET_UNIVERSE_PERIODIC_TIER_VOLUME_THRESHOLD_USDC",
            50_000.0,
        )
    )

    if volume_30d >= full_threshold:
        return DepthTier.FULL
    if volume_30d >= periodic_threshold:
        return DepthTier.PERIODIC
    return DepthTier.LIGHT


class AdaptiveDepth:
    """Decides how deeply each wallet gets enriched.

    Lifecycle: instantiated once in the crawler daemon
    (``polymarket-crawler.service``). The ``review_tiers`` method is
    invoked once nightly by an APScheduler job; in between, the class
    is essentially dormant.
    """

    def __init__(self, universe: "WalletUniverse | None" = None) -> None:
        """
        Args:
            universe: Optional :class:`WalletUniverse` handle. The
                review loop calls into it to refresh the
                ``wallet_universe_tier_count`` gauge after each sweep.
                Tests can pass None and the loop still functions — the
                bulk SQL bypasses the universe object entirely.
        """
        self._universe = universe

    async def review_tiers(self) -> dict[DepthTier, int]:
        """Nightly promotion/demotion sweep.

        Performance contract: must run in <60 s against ~1.5M wallets.

        Algorithm (the bulk-update pattern):

          1. Single LEFT JOIN aggregation pulls every wallet's current
             tier + 30d activity stats in one round-trip:

               SELECT wu.wallet_address, wu.depth_tier,
                      COALESCE(t.volume_30d, 0)  AS volume_30d_usdc,
                      COALESCE(t.trades_30d, 0)  AS trades_30d
               FROM   wallet_universe wu
               LEFT JOIN (
                   SELECT wallet_address,
                          SUM(size_usdc) AS volume_30d,
                          COUNT(*)       AS trades_30d
                     FROM trades_observed
                    WHERE time > NOW() - INTERVAL '30 days'
                      AND source IS DISTINCT FROM 'onchain'
                    GROUP BY wallet_address
               ) t USING (wallet_address);

          2. Classify every row in Python via ``expected_tier`` and
             bucket the *transitions* by target tier so that we issue
             at most 3 grouped UPDATEs (one per target tier):

               UPDATE wallet_universe
                  SET depth_tier = $1,
                      last_tier_review = NOW()
                WHERE wallet_address = ANY($2::text[]);

             A per-wallet UPDATE at 1.5M rows is hopeless (~5 ms each
             ≈ 2 hours); ``= ANY(text[])`` keeps the loop at 3 SQL
             round-trips regardless of how many wallets transitioned.

          3. Emit
             ``polybot_wallet_universe_promotions_total{from_tier, to_tier}``
             counter increments for each transition (one increment per
             wallet, but they're cheap counter ops in-process).

          4. Refresh the ``polybot_wallet_universe_tier_count{tier}``
             gauge from the post-sweep counts.

        Returns:
            Per-tier wallet counts AFTER the sweep.
        """
        # Step 1 — single roll-up across wallet_universe × trades_observed.
        # Exclude source='onchain' rows: their `market_id = token_id`
        # placeholder and price=0 (CLAUDE.md § 15, pending Wave-3
        # economic decoder) skew the 30-day volume estimate that drives
        # the FULL / PERIODIC / LIGHT tier promotion. Older rows
        # without a source value still flow through.
        select_sql = """
            SELECT wu.wallet_address,
                   wu.depth_tier,
                   COALESCE(t.volume_30d_usdc, 0)::FLOAT8 AS volume_30d_usdc,
                   COALESCE(t.trades_30d, 0)::BIGINT     AS trades_30d
              FROM wallet_universe wu
              LEFT JOIN (
                  SELECT wallet_address,
                         SUM(size_usdc) AS volume_30d_usdc,
                         COUNT(*)       AS trades_30d
                    FROM trades_observed
                   WHERE time > NOW() - INTERVAL '30 days'
                     AND source IS DISTINCT FROM 'onchain'
                   GROUP BY wallet_address
              ) t USING (wallet_address)
        """

        # Step 2 — bucket transitions by target tier.
        buckets: dict[DepthTier, list[str]] = {
            DepthTier.FULL: [],
            DepthTier.PERIODIC: [],
            DepthTier.LIGHT: [],
        }
        # Track promotions: list of (wallet, from_tier, to_tier) for the
        # Prometheus counter increments after the UPDATEs land.
        promotions: list[tuple[str, int, int]] = []
        # Running post-sweep totals — we know the target tier of every
        # row so we can compute the result counts without a second
        # GROUP BY.
        post_counts: dict[DepthTier, int] = {
            DepthTier.FULL: 0,
            DepthTier.PERIODIC: 0,
            DepthTier.LIGHT: 0,
        }

        async with get_db() as conn:
            rows = await conn.fetch(select_sql)
            for row in rows:
                wallet = row["wallet_address"]
                current_tier = DepthTier(int(row["depth_tier"]))
                stats = {
                    "volume_30d_usdc": float(row["volume_30d_usdc"] or 0.0),
                    "trades_30d": int(row["trades_30d"] or 0),
                }
                target = expected_tier(stats)
                post_counts[target] = post_counts.get(target, 0) + 1
                if target != current_tier:
                    buckets[target].append(wallet)
                    promotions.append(
                        (wallet, int(current_tier), int(target))
                    )

            if any(buckets.values()):
                update_sql = """
                    UPDATE wallet_universe
                       SET depth_tier = $1,
                           last_tier_review = NOW()
                     WHERE wallet_address = ANY($2::text[])
                """
                async with conn.transaction():
                    for target_tier, wallets in buckets.items():
                        if not wallets:
                            continue
                        await conn.execute(
                            update_sql, int(target_tier), wallets
                        )

        # Step 3 — emit promotion counters (out of the DB tx).
        if wallet_universe_promotions_total is not None and promotions:
            for _wallet, from_tier, to_tier in promotions:
                try:
                    wallet_universe_promotions_total.labels(
                        from_tier=str(from_tier), to_tier=str(to_tier)
                    ).inc()
                except Exception:  # pragma: no cover
                    logger.debug(
                        "wallet_universe_promotions_total.inc() failed",
                        exc_info=True,
                    )

        # Step 4 — refresh the tier_count gauge.
        if wallet_universe_tier_count is not None:
            for tier_value, count in post_counts.items():
                try:
                    wallet_universe_tier_count.labels(
                        tier=str(int(tier_value))
                    ).set(count)
                except Exception:  # pragma: no cover
                    logger.debug(
                        "wallet_universe_tier_count.set() failed",
                        exc_info=True,
                    )

        logger.info(
            "review_tiers: {} transitions, post-sweep counts={}",
            len(promotions),
            {int(k): v for k, v in post_counts.items()},
        )
        return post_counts

    async def run_daemon_loop(self) -> None:
        """Periodic review loop for the crawler daemon.

        Wakes every ``WALLET_UNIVERSE_REVIEW_INTERVAL_S`` seconds, runs
        one ``review_tiers`` sweep, then sleeps. Exits cleanly on
        :class:`asyncio.CancelledError` so the systemd unit can do a
        graceful shutdown.
        """
        interval_s = float(
            getattr(settings, "WALLET_UNIVERSE_REVIEW_INTERVAL_S", 86_400)
        )
        logger.info(
            "AdaptiveDepth.run_daemon_loop: starting (interval={}s)",
            interval_s,
        )
        try:
            while True:
                try:
                    counts = await self.review_tiers()
                    logger.info(
                        "AdaptiveDepth nightly sweep done: counts={}",
                        {int(k): v for k, v in counts.items()},
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.exception(
                        f"AdaptiveDepth.review_tiers raised: {exc}"
                    )
                await asyncio.sleep(interval_s)
        except asyncio.CancelledError:
            logger.info("AdaptiveDepth.run_daemon_loop: cancelled")
            raise

    def expected_tier(self, wallet_stats: dict[str, Any]) -> DepthTier:
        """Instance-method alias for the module-level ``expected_tier``.

        Exposed on the class for unit-test convenience and for the
        review loop's call site readability."""
        return expected_tier(wallet_stats)
