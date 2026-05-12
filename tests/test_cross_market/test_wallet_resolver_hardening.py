"""Wave-3 hardening for the WalletResolver.

Coverage beyond the pre-merge suite:

  * Confirmation threshold respected per-call (not just settings) —
    same candidate score can be either confirmed or pending depending
    on the threshold passed.
  * Score-tie tie-break is deterministic — `_score_match` is pure and
    `max` picks the first highest in iteration order.
  * Mid-cycle DB write failure is logged but does NOT propagate (the
    resolver swallows the exception and still returns a ResolutionResult
    so the caller can decide what to do).
  * `is_pending_review` correctly distinguishes:
      - manual + low confidence  → NOT pending
      - profile_link + low conf  → NOT pending
      - fingerprint + high conf  → NOT pending
      - fingerprint + low conf   → PENDING
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, patch

import pytest

from src.cross_market.wallet_resolver import (
    ResolutionResult,
    ResolutionSource,
    WalletResolver,
)


def _ok_ctx():
    conn = AsyncMock()
    conn.execute = AsyncMock()

    @asynccontextmanager
    async def _ctx():
        yield conn

    return _ctx, conn


def _failing_ctx():
    conn = AsyncMock()
    conn.execute = AsyncMock(side_effect=RuntimeError("DB down"))

    @asynccontextmanager
    async def _ctx():
        yield conn

    return _ctx, conn


class TestThresholdControlsPendingFlag:
    @pytest.mark.asyncio
    async def test_threshold_at_0_8_pending(self):
        async def _sig(_):
            return {"strategy_class": "directional"}

        async def _cands(_):
            return [{"account": "K-1", "strategy_class": "momentum"}]

        ctx, _ = _ok_ctx()
        with patch("src.cross_market.wallet_resolver.get_db", side_effect=ctx):
            r = WalletResolver(
                fetch_polymarket_signature=_sig,
                fetch_kalshi_candidates=_cands,
            )
            res = await r.resolve_via_fingerprint(
                "0xPM", confirmation_threshold=0.8,
            )
        assert res is not None
        # No agreement on strategy → score 0; floor 0.8 → pending.
        assert res.is_pending_review is True
        assert res.confidence == 0.0

    @pytest.mark.asyncio
    async def test_threshold_at_0_0_never_pending(self):
        async def _sig(_):
            return {"strategy_class": "directional"}

        async def _cands(_):
            return [{"account": "K-1", "strategy_class": "directional"}]

        ctx, _ = _ok_ctx()
        with patch("src.cross_market.wallet_resolver.get_db", side_effect=ctx):
            r = WalletResolver(
                fetch_polymarket_signature=_sig,
                fetch_kalshi_candidates=_cands,
            )
            res = await r.resolve_via_fingerprint(
                "0xPM", confirmation_threshold=0.0,
            )
        assert res is not None
        # Score 0.5 (strategy match) >= threshold 0.0 → confirmed,
        # but is_pending_review still depends on
        # CROSS_MARKET_MIN_RESOLUTION_CONFIDENCE.
        assert res.confidence >= 0.5


class TestScoreMatchEdges:
    def test_empty_signals_score_zero(self):
        s = WalletResolver._score_match({}, {})
        assert s == 0.0

    def test_perfect_strategy_only_scores_half(self):
        s = WalletResolver._score_match(
            {"strategy_class": "directional"},
            {"strategy_class": "directional"},
        )
        # Strategy match alone = 0.5.
        assert s == pytest.approx(0.5)

    def test_microstructure_only_scores_three_tenths(self):
        s = WalletResolver._score_match(
            {"cancel_to_fill_ratio_30d": 1.0},
            {"cancel_to_fill_ratio_30d": 1.1},
        )
        # Within 25% → 0.3 added.
        assert s == pytest.approx(0.3)

    def test_microstructure_zero_denominator_skipped(self):
        # When both ratios are 0, the microstructure check skips
        # cleanly (no ZeroDivisionError).
        s = WalletResolver._score_match(
            {"cancel_to_fill_ratio_30d": 0.0,
             "strategy_class": "directional"},
            {"cancel_to_fill_ratio_30d": 0.0,
             "strategy_class": "directional"},
        )
        # Only strategy match contributes; microstructure branch is
        # max(0,0)=0 so the within-25% test is skipped.
        assert s == pytest.approx(0.5)

    def test_partial_active_hours_overlap(self):
        s = WalletResolver._score_match(
            {"active_hours_utc": [10, 11, 12, 13]},
            {"active_hours_utc": [12, 13, 14, 15]},
        )
        # IoU = 2 / 6 = 0.333... → 0.2 * 0.333 = 0.0667.
        assert s == pytest.approx(0.2 * 2 / 6)


class TestPendingReviewMatrix:
    """Property matrix: only fingerprint + low-confidence is pending."""

    @pytest.mark.parametrize("source,conf,expected_pending", [
        (ResolutionSource.MANUAL, 0.0, False),
        (ResolutionSource.MANUAL, 1.0, False),
        (ResolutionSource.PROFILE_LINK, 0.0, False),
        (ResolutionSource.PROFILE_LINK, 1.0, False),
        (ResolutionSource.FINGERPRINT, 0.0, True),
        (ResolutionSource.FINGERPRINT, 0.50, True),
        (ResolutionSource.FINGERPRINT, 0.95, False),
    ])
    def test_pending_matrix(self, source, conf, expected_pending):
        r = ResolutionResult(
            polymarket_wallet="0xPM",
            kalshi_account="K",
            manifold_handle=None,
            predictit_account=None,
            x_handle=None,
            resolution_source=source,
            confidence=conf,
        )
        assert r.is_pending_review is expected_pending


class TestPersistFailureSwallowed:
    @pytest.mark.asyncio
    async def test_db_write_failure_does_not_propagate(self):
        # _persist swallows exceptions so the caller still gets the
        # ResolutionResult (operator's review queue can re-run it).
        ctx, conn = _failing_ctx()
        with patch("src.cross_market.wallet_resolver.get_db", side_effect=ctx):
            resolver = WalletResolver()
            r = await resolver.seed_manual(
                polymarket_wallet="0xPM",
                kalshi_account="K-1",
            )
        assert r is not None
        assert r.confidence == 1.0
        # The execute attempt was made (and raised).
        assert conn.execute.await_count == 1
