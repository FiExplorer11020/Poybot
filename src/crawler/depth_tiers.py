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

from enum import IntEnum
from typing import Any

from src.config import settings


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

    def __init__(self) -> None:
        """No external dependencies — uses the project-wide DB pool +
        the ``settings`` thresholds."""
        # TODO: implement in Wave 2 — see ROUND_6_THE_SPINE.md § 3.4
        raise NotImplementedError("Wave 2 — see ROUND_6_THE_SPINE.md § 3.4")

    async def review_tiers(self) -> dict[DepthTier, int]:
        """Nightly promotion/demotion sweep.

        Algorithm (Wave 2):
          1. Aggregate per-wallet 30d activity stats:
             ``SELECT wallet_address, SUM(size_usdc) AS volume_30d_usdc,
                       COUNT(*) AS trades_30d
                FROM trades_observed
                WHERE time > NOW() - INTERVAL '30 days'
                GROUP BY wallet_address``
          2. For each wallet, compute ``expected = expected_tier(stats)``.
          3. Compare against ``wallet_universe.depth_tier`` (current);
             UPDATE if different.
          4. Emit ``polybot_wallet_universe_promotions_total{from_tier, to_tier}``
             counter increments for each transition.
          5. Update ``wallet_universe.last_tier_review = NOW()``.

        Returns:
            Per-tier wallet counts AFTER the sweep, suitable for
            logging and the ``polybot_wallet_universe_tier_count{tier}``
            gauge update.
        """
        raise NotImplementedError("Wave 2 — see ROUND_6_THE_SPINE.md § 3.4")

    def expected_tier(self, wallet_stats: dict[str, Any]) -> DepthTier:
        """Instance-method alias for the module-level ``expected_tier``.

        Exposed on the class for unit-test convenience and for the
        review loop's call site readability."""
        return expected_tier(wallet_stats)
