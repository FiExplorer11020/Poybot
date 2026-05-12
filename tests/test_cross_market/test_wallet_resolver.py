"""WalletResolver tests.

Coverage:
  * seed_manual writes a row with confidence=1.0 + source='manual'.
  * resolve_via_profile_link writes a row with confidence=1.0 +
    source='profile_link'.
  * resolve_via_fingerprint scores candidates + picks the best.
  * Auto-match below CROSS_MARKET_MIN_RESOLUTION_CONFIDENCE is flagged
    is_pending_review=True.
  * fingerprint path requires both injectors — falls back to None
    without them.
  * _score_match honors strategy class + microstructure overlap.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from src.config import settings
from src.cross_market.wallet_resolver import (
    ResolutionResult,
    ResolutionSource,
    WalletResolver,
)


def _mock_get_db():
    conn = AsyncMock()
    conn.execute = AsyncMock()

    @asynccontextmanager
    async def _ctx():
        yield conn

    return _ctx, conn


class TestSeedManual:
    @pytest.mark.asyncio
    async def test_writes_row_with_confidence_one(self):
        ctx, conn = _mock_get_db()
        with patch("src.cross_market.wallet_resolver.get_db", side_effect=ctx):
            resolver = WalletResolver()
            r = await resolver.seed_manual(
                polymarket_wallet="0xPM",
                kalshi_account="KK-1",
                manifold_handle="alice",
                notes="hand-curated",
            )
        assert r.confidence == 1.0
        assert r.resolution_source is ResolutionSource.MANUAL
        assert not r.is_pending_review
        # SQL execute called once.
        assert conn.execute.await_count == 1


class TestProfileLink:
    @pytest.mark.asyncio
    async def test_writes_row_with_source_profile_link(self):
        ctx, conn = _mock_get_db()
        with patch("src.cross_market.wallet_resolver.get_db", side_effect=ctx):
            resolver = WalletResolver()
            r = await resolver.resolve_via_profile_link(
                polymarket_wallet="0xPM",
                x_handle="alice",
                notes="https://twitter.com/alice/status/123",
            )
        assert r.resolution_source is ResolutionSource.PROFILE_LINK
        assert r.confidence == 1.0


class TestFingerprintMatching:
    @pytest.mark.asyncio
    async def test_no_injectors_returns_none(self):
        resolver = WalletResolver()
        out = await resolver.resolve_via_fingerprint("0xPM")
        assert out is None

    @pytest.mark.asyncio
    async def test_high_score_match_is_confirmed(self):
        async def _fetch_pm_sig(_wallet):
            return {
                "strategy_class": "directional",
                "cancel_to_fill_ratio_30d": 2.5,
                "active_hours_utc": [10, 11, 12, 13],
            }

        async def _fetch_candidates(_wallet):
            return [
                {
                    "account": "kalshi-good",
                    "strategy_class": "directional",
                    "cancel_to_fill_ratio_30d": 2.4,  # within 25%
                    "active_hours_utc": [10, 11, 12, 13],
                },
                {
                    "account": "kalshi-bad",
                    "strategy_class": "momentum",
                    "cancel_to_fill_ratio_30d": 0.1,
                    "active_hours_utc": [23, 0, 1],
                },
            ]

        ctx, conn = _mock_get_db()
        with patch("src.cross_market.wallet_resolver.get_db", side_effect=ctx):
            resolver = WalletResolver(
                fetch_polymarket_signature=_fetch_pm_sig,
                fetch_kalshi_candidates=_fetch_candidates,
            )
            r = await resolver.resolve_via_fingerprint(
                "0xPM", confirmation_threshold=0.8,
            )
        assert r is not None
        # Best candidate: strategy_class match (0.5) + c2f within 25%
        # (0.3) + active hours overlap (0.2) = ~1.0.
        assert r.kalshi_account == "kalshi-good"
        assert r.confidence >= 0.9
        assert not r.is_pending_review

    @pytest.mark.asyncio
    async def test_low_score_match_is_pending_review(self):
        async def _fetch_pm_sig(_wallet):
            return {
                "strategy_class": "directional",
                "cancel_to_fill_ratio_30d": 5.0,
                "active_hours_utc": [10, 11],
            }

        async def _fetch_candidates(_wallet):
            return [
                {
                    "account": "kalshi-weak",
                    "strategy_class": "momentum",  # mismatch
                    "cancel_to_fill_ratio_30d": 10.0,  # 100% off
                    "active_hours_utc": [23, 0],  # zero overlap
                },
            ]

        ctx, conn = _mock_get_db()
        with patch("src.cross_market.wallet_resolver.get_db", side_effect=ctx):
            resolver = WalletResolver(
                fetch_polymarket_signature=_fetch_pm_sig,
                fetch_kalshi_candidates=_fetch_candidates,
            )
            r = await resolver.resolve_via_fingerprint(
                "0xPM", confirmation_threshold=0.8,
            )
        assert r is not None
        assert r.confidence < 0.8
        assert r.is_pending_review is True

    @pytest.mark.asyncio
    async def test_no_signature_returns_none(self):
        async def _fetch_pm_sig(_wallet):
            return None

        async def _fetch_candidates(_wallet):
            return [{"account": "x"}]

        resolver = WalletResolver(
            fetch_polymarket_signature=_fetch_pm_sig,
            fetch_kalshi_candidates=_fetch_candidates,
        )
        out = await resolver.resolve_via_fingerprint("0xPM")
        assert out is None

    @pytest.mark.asyncio
    async def test_no_candidates_returns_none(self):
        async def _fetch_pm_sig(_wallet):
            return {"strategy_class": "directional"}

        async def _fetch_candidates(_wallet):
            return []

        resolver = WalletResolver(
            fetch_polymarket_signature=_fetch_pm_sig,
            fetch_kalshi_candidates=_fetch_candidates,
        )
        out = await resolver.resolve_via_fingerprint("0xPM")
        assert out is None


class TestScoreMatch:
    def test_perfect_match_scores_one(self):
        pm = {
            "strategy_class": "directional",
            "cancel_to_fill_ratio_30d": 2.5,
            "active_hours_utc": [10, 11, 12, 13],
        }
        cand = dict(pm)
        cand["account"] = "x"
        s = WalletResolver._score_match(pm, cand)
        assert s == pytest.approx(1.0)

    def test_complete_mismatch_scores_zero(self):
        s = WalletResolver._score_match(
            {"strategy_class": "directional"},
            {"strategy_class": "momentum"},
        )
        assert s == 0.0


class TestPendingReviewProperty:
    def test_high_confidence_fingerprint_not_pending(self):
        r = ResolutionResult(
            polymarket_wallet="0x",
            kalshi_account="k",
            manifold_handle=None,
            predictit_account=None,
            x_handle=None,
            resolution_source=ResolutionSource.FINGERPRINT,
            confidence=settings.CROSS_MARKET_MIN_RESOLUTION_CONFIDENCE + 0.1,
        )
        assert r.is_pending_review is False

    def test_low_confidence_fingerprint_pending(self):
        r = ResolutionResult(
            polymarket_wallet="0x",
            kalshi_account="k",
            manifold_handle=None,
            predictit_account=None,
            x_handle=None,
            resolution_source=ResolutionSource.FINGERPRINT,
            confidence=0.5,
        )
        assert r.is_pending_review is True

    def test_manual_never_pending(self):
        r = ResolutionResult(
            polymarket_wallet="0x",
            kalshi_account="k",
            manifold_handle=None,
            predictit_account=None,
            x_handle=None,
            resolution_source=ResolutionSource.MANUAL,
            confidence=0.0,
        )
        assert r.is_pending_review is False
