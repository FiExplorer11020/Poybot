"""Plan 2026-05-19 P2 — tests for the market_context aggregator and
the new microstructure/social penalty contributors in sizing_penalties.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from src.engine.market_context import fetch_market_context
from src.engine.sizing_penalties import (
    _ofi_alignment_penalty,
    _social_exit_penalty,
    compute_market_context_penalty,
)


# ──────────────────────────────────────────────────────────────────────
# OFI alignment penalty                                                  #
# ──────────────────────────────────────────────────────────────────────


class TestOFIAlignmentPenalty:
    def test_buy_side_with_negative_ofi_returns_penalty(self):
        """Leader is BUY, but OFI shows selling pressure → penalty."""
        pen = _ofi_alignment_penalty(-0.4, "buy")
        assert pen == pytest.approx(0.16)

    def test_buy_side_with_positive_ofi_returns_zero(self):
        """Aligned flow — no penalty."""
        assert _ofi_alignment_penalty(0.4, "buy") == 0.0

    def test_sell_side_with_positive_ofi_returns_penalty(self):
        """Leader is SELL (rare), OFI shows buying pressure → penalty."""
        pen = _ofi_alignment_penalty(0.3, "sell")
        assert pen == pytest.approx(0.12)

    def test_clamped_at_max(self):
        """Even extreme OFI values clamp at 0.2 max."""
        assert _ofi_alignment_penalty(-1.0, "buy") == 0.2

    def test_none_inputs_return_zero(self):
        assert _ofi_alignment_penalty(None, "buy") == 0.0
        assert _ofi_alignment_penalty(0.5, None) == 0.0
        assert _ofi_alignment_penalty(None, None) == 0.0


# ──────────────────────────────────────────────────────────────────────
# Social exit penalty                                                    #
# ──────────────────────────────────────────────────────────────────────


class TestSocialExitPenalty:
    def test_recent_exit_signal_max_penalty(self):
        """Exit signal < 1h ago → max 0.4 penalty."""
        assert _social_exit_penalty("exit_signal", 1_800.0) == 0.4

    def test_medium_age_exit_signal(self):
        """1h < age < 6h → 0.2 penalty."""
        assert _social_exit_penalty("exit_signal", 7_200.0) == 0.2

    def test_old_exit_signal_no_penalty(self):
        """Age > 6h → 0.0 penalty."""
        assert _social_exit_penalty("exit_signal", 30_000.0) == 0.0

    def test_entry_signal_no_penalty(self):
        """Entry signal doesn't trigger this (it's the opposite case)."""
        assert _social_exit_penalty("entry_signal", 1_800.0) == 0.0

    def test_none_inputs_return_zero(self):
        assert _social_exit_penalty(None, 1_800.0) == 0.0
        assert _social_exit_penalty("exit_signal", None) == 0.0


# ──────────────────────────────────────────────────────────────────────
# Aggregator with P2 contributors                                        #
# ──────────────────────────────────────────────────────────────────────


class TestAggregatorWithP2:
    def test_p2_contributors_aggregate_with_p1(self):
        ctx = {
            "market_volume_24h": 2_500.0,    # liquidity_zone ~0.42
            "ofi_mean": -0.3,                # ofi_opposite 0.12
            "leader_side": "buy",
            "social_last_intent": "exit_signal",
            "social_last_signal_age_s": 1_000.0,  # social_exit_recent 0.4
        }
        pen, codes = compute_market_context_penalty(ctx)
        # 0.42 + 0.12 + 0.4 = 0.94 → clamped at 0.8
        assert pen == pytest.approx(0.8)
        assert "liquidity_zone" in codes
        assert "ofi_opposite" in codes
        assert "social_exit_recent" in codes

    def test_p2_only_no_p1_features(self):
        ctx = {
            "ofi_mean": -0.25,
            "leader_side": "buy",
        }
        pen, codes = compute_market_context_penalty(ctx)
        assert pen == pytest.approx(0.10)
        assert codes == ["ofi_opposite"]


# ──────────────────────────────────────────────────────────────────────
# fetch_market_context                                                   #
# ──────────────────────────────────────────────────────────────────────


class TestFetchMarketContext:
    @pytest.mark.asyncio
    async def test_returns_empty_when_db_unavailable(self):
        """All three readers must fail-safe to empty dict."""
        # Without DB pool initialised, the get_db context manager raises.
        # market_context wraps that in best-effort try/except.
        result = await fetch_market_context(
            market_id="mkt-1",
            token_id="tok-1",
            wallet="0xAAA",
        )
        assert isinstance(result, dict)
        # No features available — empty.
        assert result == {} or all(v is None for v in result.values())

    @pytest.mark.asyncio
    async def test_microstructure_features_propagate(self):
        """When microstructure has data, the OFI fields land in the
        returned dict with the expected keys."""
        with patch(
            "src.engine.market_context._safe_microstructure",
            AsyncMock(return_value={"ofi_mean": -0.3, "ofi_max": 0.1}),
        ), patch(
            "src.engine.market_context._safe_social",
            AsyncMock(return_value={}),
        ), patch(
            "src.engine.market_context._safe_cross_market",
            AsyncMock(return_value={}),
        ):
            result = await fetch_market_context(
                market_id="mkt-1",
                token_id="tok-1",
                wallet="0xAAA",
            )
        assert result.get("ofi_mean") == -0.3

    @pytest.mark.asyncio
    async def test_social_features_propagate(self):
        with patch(
            "src.engine.market_context._safe_microstructure",
            AsyncMock(return_value={}),
        ), patch(
            "src.engine.market_context._safe_social",
            AsyncMock(return_value={
                "social_last_intent": "exit_signal",
                "social_last_signal_age_s": 600.0,
            }),
        ), patch(
            "src.engine.market_context._safe_cross_market",
            AsyncMock(return_value={}),
        ):
            result = await fetch_market_context(
                market_id="mkt-1",
                token_id="tok-1",
                wallet="0xAAA",
            )
        assert result.get("social_last_intent") == "exit_signal"
        assert result.get("social_last_signal_age_s") == 600.0

    @pytest.mark.asyncio
    async def test_empty_wallet_skips_lookups(self):
        """An empty wallet should not crash; the social/cross-market
        helpers must early-return on empty input."""
        result = await fetch_market_context(
            market_id="mkt-1",
            token_id="tok-1",
            wallet="",
        )
        assert isinstance(result, dict)
