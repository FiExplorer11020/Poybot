"""
Regression tests for the Strategy Upgrade 2026-05-17 round 2 changes
to the confidence engine — Falcon prior integration (Lever B) +
tier-based thresholds (Lever C).

Each test pins a load-bearing behaviour of the Falcon-prior path so
a future refactor of `_compute_effective_metrics`,
`_classify_leader_tier`, or the `leader_quality_gate` SKIP reasoning
can't silently regress the 5,247-leader unlock the migration delivered.

Covered:
- `_compute_effective_metrics` math (effective_resolved + Laplace-smoothed winrate)
- `_classify_leader_tier` (A/B/C with falcon_score OR follower fallback)
- Tier-specific resolved + winrate gates fire correctly
- Falcon-only leader (internal=0, external=200) passes Tier A
- Internal-only leader (no Falcon) still works under Tier C
- SKIP reason includes the new `tier=A|effective=8|min=10` shape

The fixture style mirrors
`tests/test_engine/test_confidence_engine_strategy.py` — the
existing patcher pattern handles `_get_readiness` and DB writes the
same way so the diff stays focused on the new logic.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.config import settings
from src.engine.confidence_engine import ConfidenceEngine


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #


def _make_engine() -> ConfidenceEngine:
    redis = MagicMock()
    redis.publish = AsyncMock()
    redis.get = AsyncMock(return_value=None)
    return ConfidenceEngine(redis_client=redis)


def _patch_db_with_rows(rows_by_query: dict) -> tuple:
    """Patch `src.engine.confidence_engine.get_db` so fetchrow returns
    the first row whose SQL substring matches the executed statement.
    `conn.fetchval` returns 42 so the decision_log INSERT path returns
    a non-None decision_id (matches test_confidence_engine.py)."""

    conn = AsyncMock()

    async def _fetchrow(sql, *args):
        for needle, value in rows_by_query.items():
            if needle in sql:
                return value
        return None

    conn.fetchrow = AsyncMock(side_effect=_fetchrow)
    conn.execute = AsyncMock()
    conn.fetchval = AsyncMock(return_value=42)

    @asynccontextmanager
    async def _ctx():
        yield conn

    return patch("src.engine.confidence_engine.get_db", side_effect=_ctx), conn


def _bypass_liquidity_gate() -> dict:
    """Volume row with $100k so the liquidity gate doesn't fire early."""
    return {
        "SELECT volume_24h FROM markets WHERE market_id = $1": {
            "volume_24h": 100_000.0
        },
    }


def _readiness(**overrides) -> dict:
    """Build a readiness dict with sensible defaults; override per test."""
    base = {
        "trades_observed": 100,
        "positions_resolved": 0,
        "confirmed_followers": 0,
        "external_resolved_count": 0,
        "external_wins": 0,
        "external_losses": 0,
        "falcon_score": 0.0,
    }
    base.update(overrides)
    return base


# --------------------------------------------------------------------------- #
# Pure-helper math                                                            #
# --------------------------------------------------------------------------- #


class TestComputeEffectiveMetrics:
    """`_compute_effective_metrics` is the Bayesian fusion core. Pure
    function — no DB, no async — so we test it directly."""

    def test_internal_only_matches_internal(self):
        """No Falcon prior: effective_* must equal internal_*."""
        profile = {"accuracy": {"overall": 0.6, "resolved_count": 50}}
        readiness = _readiness(positions_resolved=50)
        resolved, winrate = ConfidenceEngine._compute_effective_metrics(
            profile, readiness, discount=0.5
        )
        # MAX(50, int(0 * 0.5)) = 50
        assert resolved == 50
        # Laplace smoothing: (30 + 0 + 1) / (50 + 0 + 2) = 31/52 ≈ 0.596
        assert abs(winrate - (31.0 / 52.0)) < 1e-6

    def test_external_only_passes_through_discount(self):
        """Falcon-only leader (internal=0): effective_resolved =
        int(external * discount). Winrate from external wins only."""
        profile = {"accuracy": {"overall": 0.0, "resolved_count": 0}}
        readiness = _readiness(
            positions_resolved=0,
            external_resolved_count=200,
            external_wins=140,
            external_losses=60,
        )
        resolved, winrate = ConfidenceEngine._compute_effective_metrics(
            profile, readiness, discount=0.5
        )
        # MAX(0, int(200 * 0.5)) = 100
        assert resolved == 100
        # (0 + 0.5*140 + 1) / (0 + 0.5*200 + 2) = 71 / 102 ≈ 0.696
        assert abs(winrate - (71.0 / 102.0)) < 1e-6

    def test_internal_dominates_when_larger(self):
        """When internal_resolved > discounted external, the MAX picks
        internal — we don't double-count overlapping evidence."""
        profile = {"accuracy": {"overall": 0.6, "resolved_count": 200}}
        readiness = _readiness(
            positions_resolved=200,
            external_resolved_count=100,  # 100 * 0.5 = 50 < 200
            external_wins=60,
            external_losses=40,
        )
        resolved, _ = ConfidenceEngine._compute_effective_metrics(
            profile, readiness, discount=0.5
        )
        assert resolved == 200

    def test_discount_zero_disables_external(self):
        """discount=0 reproduces the internal-only behaviour."""
        profile = {"accuracy": {"overall": 0.5, "resolved_count": 10}}
        readiness = _readiness(
            positions_resolved=10,
            external_resolved_count=500,
            external_wins=400,
            external_losses=100,
        )
        resolved, winrate = ConfidenceEngine._compute_effective_metrics(
            profile, readiness, discount=0.0
        )
        assert resolved == 10
        # (5 + 0 + 1) / (10 + 0 + 2) = 6/12 = 0.5
        assert abs(winrate - 0.5) < 1e-6

    def test_empty_profile_returns_laplace_neutral(self):
        """No internal, no external → (0, 0.5) — uninformed prior."""
        resolved, winrate = ConfidenceEngine._compute_effective_metrics(
            {}, _readiness(), discount=0.5
        )
        assert resolved == 0
        assert abs(winrate - 0.5) < 1e-6


# --------------------------------------------------------------------------- #
# Tier classification                                                         #
# --------------------------------------------------------------------------- #


class TestClassifyLeaderTier:
    def test_high_falcon_score_is_tier_a(self):
        assert ConfidenceEngine._classify_leader_tier(80.0, 0) == "A"

    def test_high_follower_count_is_tier_a(self):
        """A leader with 0 falcon_score but 10 confirmed followers
        still qualifies for Tier A (the OR is by design — social
        validation is an alternative path)."""
        assert ConfidenceEngine._classify_leader_tier(0.0, 10) == "A"

    def test_mid_falcon_is_tier_b(self):
        assert ConfidenceEngine._classify_leader_tier(30.0, 0) == "B"

    def test_mid_followers_is_tier_b(self):
        assert ConfidenceEngine._classify_leader_tier(0.0, 3) == "B"

    def test_unknown_leader_is_tier_c(self):
        assert ConfidenceEngine._classify_leader_tier(0.0, 0) == "C"

    def test_none_inputs_degrade_to_tier_c(self):
        """A leader with NULL falcon_score (Falcon doesn't recognise
        the wallet) and NULL followers must land in Tier C cleanly."""
        assert ConfidenceEngine._classify_leader_tier(None, None) == "C"

    def test_a_wins_ties_over_b(self):
        """A leader who clears BOTH A's and B's bar lands in A (the
        classifier returns the first matching tier)."""
        # falcon_score=50 clears A's default; we should not fall through
        # to B even though B is also satisfied.
        assert ConfidenceEngine._classify_leader_tier(50.0, 5) == "A"

    def test_custom_thresholds_honored(self):
        """The kwargs let RuntimeConfig overrides re-shape the bands
        at runtime without a code change."""
        # With a 100-point falcon bar for A, a 50-score leader drops to B.
        assert (
            ConfidenceEngine._classify_leader_tier(
                50.0, 0, tier_a_falcon=100.0, tier_b_falcon=20.0
            )
            == "B"
        )


# --------------------------------------------------------------------------- #
# Tier-specific gates inside the live evaluate() path                         #
# --------------------------------------------------------------------------- #


class TestTierSpecificGates:
    """End-to-end: drive `evaluate()` with crafted readiness and verify
    the new tier-aware SKIP reasoning."""

    @pytest.mark.asyncio
    async def test_falcon_only_leader_passes_tier_a(self):
        """A leader with 0 internal resolved but 200 external resolved
        (and a Falcon score that puts them in Tier A) must NOT SKIP on
        the `leader_resolved_too_low` reason — the Falcon prior unlocks
        them."""
        engine = _make_engine()
        wallet = "0xFALCONONLY"
        engine._thompson[wallet] = {
            "follow": [50.0, 10.0],  # high follow posterior
            "fade": [1.0, 1.0],
        }
        engine._get_readiness = AsyncMock(
            return_value=_readiness(
                trades_observed=10,  # FOLLOW readiness uses default 25
                positions_resolved=0,
                confirmed_followers=10,  # → Tier A by followers
                external_resolved_count=200,
                external_wins=150,
                external_losses=50,
                falcon_score=80.0,  # → Tier A by falcon too
            )
        )
        engine._get_profile_snapshot = AsyncMock(
            return_value={"accuracy": {"overall": 0.0, "resolved_count": 0}}
        )
        engine._build_trade_context = AsyncMock(
            return_value={"process_score": 0.9, "category": "sports"}
        )
        engine._build_signal_audit = AsyncMock(return_value={"accepted": True})
        engine._log_decision = AsyncMock()
        engine._emit = AsyncMock()

        patcher, _ = _patch_db_with_rows(_bypass_liquidity_gate())
        with patcher, patch("numpy.random.random", return_value=1.0), \
             patch("numpy.random.beta", side_effect=[0.95, 0.05]):
            await engine.evaluate(
                {
                    "wallet_address": wallet,
                    "market_id": "mkt-falcon",
                    "token_id": "tok-falcon",
                    "is_leader": True,
                }
            )

        skip_calls = [
            c for c in engine._log_decision.await_args_list
            if len(c[0]) >= 3 and c[0][2] == "skip"
        ]
        skip_reasons = [c[0][7] for c in skip_calls]
        # The tier-A gate (min_resolved=10) must NOT skip on
        # resolved_too_low — effective_resolved is int(200*0.5)=100 >> 10.
        assert not any(
            "leader_resolved_too_low" in r for r in skip_reasons
        ), (
            f"Tier-A Falcon-only leader incorrectly rejected on resolved "
            f"floor; SKIP reasons: {skip_reasons}"
        )

    @pytest.mark.asyncio
    async def test_internal_only_leader_passes_tier_c(self):
        """A cold-start leader with no Falcon data but 30+ internal
        resolved must still clear the legacy Tier-C gate (back-compat:
        the new code path must not break the existing flow)."""
        engine = _make_engine()
        wallet = "0xINTERNALONLY"
        engine._thompson[wallet] = {
            "follow": [25.0, 5.0],
            "fade": [1.0, 1.0],
        }
        engine._get_readiness = AsyncMock(
            return_value=_readiness(
                trades_observed=100,
                positions_resolved=30,  # >= TIER_C_MIN_RESOLVED
                confirmed_followers=0,  # not enough for Tier A/B
                falcon_score=0.0,  # → Tier C
                external_resolved_count=0,
            )
        )
        engine._get_profile_snapshot = AsyncMock(
            return_value={"accuracy": {"overall": 0.70, "resolved_count": 30}}
        )
        engine._build_trade_context = AsyncMock(
            return_value={"process_score": 0.9, "category": "sports"}
        )
        engine._build_signal_audit = AsyncMock(return_value={"accepted": True})
        engine._log_decision = AsyncMock()
        engine._emit = AsyncMock()

        patcher, _ = _patch_db_with_rows(_bypass_liquidity_gate())
        with patcher, patch("numpy.random.random", return_value=1.0), \
             patch("numpy.random.beta", side_effect=[0.95, 0.05]):
            await engine.evaluate(
                {
                    "wallet_address": wallet,
                    "market_id": "mkt-internal",
                    "token_id": "tok-internal",
                    "is_leader": True,
                }
            )

        skip_calls = [
            c for c in engine._log_decision.await_args_list
            if len(c[0]) >= 3 and c[0][2] == "skip"
        ]
        skip_reasons = [c[0][7] for c in skip_calls]
        # Must not skip on the new gate's resolved/winrate reasons.
        assert not any(
            "leader_resolved_too_low" in r or "leader_winrate_too_low" in r
            for r in skip_reasons
        ), (
            f"Tier-C internal-only leader unjustly rejected; "
            f"SKIP reasons: {skip_reasons}"
        )

    @pytest.mark.asyncio
    async def test_tier_c_with_too_few_resolved_is_skipped(self):
        """A cold-start leader (Tier C) with 25 resolved must SKIP
        with the new tier-tagged reason: 25 clears the FADE readiness
        floor (25) but is below Tier C's `tier_c_min_resolved=30`
        leader-quality gate. The reason MUST include `tier=C` and the
        `effective=N|min=M` shape so log analysis can group by tier.

        We use 2 confirmed_followers (below tier_b_follower_count=3)
        and falcon_score=0.0 so the leader lands cleanly in Tier C —
        not Tier B from a follower-count overlap."""
        engine = _make_engine()
        wallet = "0xNEW"
        engine._get_readiness = AsyncMock(
            return_value=_readiness(
                trades_observed=100,  # passes FOLLOW trade-count gate
                positions_resolved=25,  # >= FADE_MIN_RESOLVED, < tier_c_min_resolved
                confirmed_followers=2,  # < Tier B's 3 → Tier C
                falcon_score=0.0,  # → Tier C
                external_resolved_count=0,
            )
        )
        engine._get_profile_snapshot = AsyncMock(
            return_value={"accuracy": {"overall": 0.80, "resolved_count": 5}}
        )
        engine._log_decision = AsyncMock()

        patcher, _ = _patch_db_with_rows(_bypass_liquidity_gate())
        with patcher:
            decision = await engine.evaluate(
                {
                    "wallet_address": wallet,
                    "market_id": "mkt-new",
                    "token_id": "tok-new",
                    "is_leader": True,
                }
            )

        assert decision is None
        skip_calls = [
            c for c in engine._log_decision.await_args_list
            if len(c[0]) >= 3 and c[0][2] == "skip"
        ]
        assert skip_calls, "Expected at least one SKIP log_decision call"
        last_reason = skip_calls[-1][0][7]
        # Spec format: `leader_resolved_too_low|tier=C|effective=5|min=30`.
        assert "leader_resolved_too_low" in last_reason, last_reason
        assert "tier=C" in last_reason, last_reason
        assert "effective=" in last_reason, last_reason
        assert "min=" in last_reason, last_reason

    @pytest.mark.asyncio
    async def test_tier_a_with_8_effective_skipped_with_explicit_min(self):
        """A Tier-A leader with effective_resolved=8 must SKIP because
        Tier A's min is 10. The reason must call out tier=A so the
        operator can spot that even Falcon-validated leaders need a
        minimum (the gate isn't degenerate)."""
        engine = _make_engine()
        wallet = "0xLOWA"
        engine._get_readiness = AsyncMock(
            return_value=_readiness(
                trades_observed=100,
                positions_resolved=8,  # below tier_a_min_resolved=10
                confirmed_followers=10,  # → Tier A
                external_resolved_count=0,
                falcon_score=80.0,
            )
        )
        engine._get_profile_snapshot = AsyncMock(
            return_value={"accuracy": {"overall": 0.80, "resolved_count": 8}}
        )
        engine._log_decision = AsyncMock()

        patcher, _ = _patch_db_with_rows(_bypass_liquidity_gate())
        with patcher:
            decision = await engine.evaluate(
                {
                    "wallet_address": wallet,
                    "market_id": "mkt-lowa",
                    "token_id": "tok-lowa",
                    "is_leader": True,
                }
            )

        assert decision is None
        skip_calls = [
            c for c in engine._log_decision.await_args_list
            if len(c[0]) >= 3 and c[0][2] == "skip"
        ]
        assert skip_calls
        last_reason = skip_calls[-1][0][7]
        assert "tier=A" in last_reason, last_reason
        assert "effective=8" in last_reason, last_reason
        assert "min=10" in last_reason, last_reason

    @pytest.mark.asyncio
    async def test_tier_b_winrate_gate_blocks_follow(self):
        """A Tier-B leader with enough resolved but a 0.30 winrate
        must NOT produce a FOLLOW action — the tier_b_min_winrate=0.55
        gate must be load-bearing for FOLLOW. (FADE may still fire,
        which is correct — we're betting against losers.)"""
        engine = _make_engine()
        wallet = "0xLOSERTIERB"
        engine._get_readiness = AsyncMock(
            return_value=_readiness(
                trades_observed=100,
                positions_resolved=25,  # passes tier_b_min_resolved=20
                confirmed_followers=3,  # → Tier B
                falcon_score=0.0,
                external_resolved_count=0,
            )
        )
        engine._get_profile_snapshot = AsyncMock(
            return_value={"accuracy": {"overall": 0.30, "resolved_count": 25}}
        )
        engine._build_trade_context = AsyncMock(
            return_value={"process_score": 0.9, "category": "sports"}
        )
        engine._build_signal_audit = AsyncMock(return_value={"accepted": True})
        engine._log_decision = AsyncMock()
        engine._emit = AsyncMock()

        patcher, _ = _patch_db_with_rows(_bypass_liquidity_gate())
        with patcher, patch("numpy.random.random", return_value=1.0), \
             patch("numpy.random.beta", side_effect=[0.5, 0.9]):
            decision = await engine.evaluate(
                {
                    "wallet_address": wallet,
                    "market_id": "mkt-loser",
                    "token_id": "tok-loser",
                    "is_leader": True,
                }
            )

        # FOLLOW must not have fired on a 0.30-winrate leader: either
        # SKIP, or FADE (FADE is by design exempt from the winrate gate).
        assert decision is None or decision.action != "follow", (
            f"FOLLOW fired on a 0.30-winrate Tier-B leader; gate is not "
            f"wired correctly. decision={decision!r}"
        )


# --------------------------------------------------------------------------- #
# Sanity: new config + runtime_config keys exist                              #
# --------------------------------------------------------------------------- #


def test_falcon_prior_constants_present():
    """Catch a rename that would silently break the runtime knob wiring."""
    assert getattr(settings, "FALCON_EXTERNAL_DISCOUNT", None) is not None
    assert getattr(settings, "TIER_A_MIN_RESOLVED", None) is not None
    assert getattr(settings, "TIER_A_MIN_WINRATE", None) is not None
    assert getattr(settings, "TIER_B_MIN_RESOLVED", None) is not None
    assert getattr(settings, "TIER_B_MIN_WINRATE", None) is not None
    assert getattr(settings, "TIER_C_MIN_RESOLVED", None) is not None
    assert getattr(settings, "TIER_C_MIN_WINRATE", None) is not None
    assert getattr(settings, "TIER_A_FALCON_THRESHOLD", None) is not None
    assert getattr(settings, "TIER_B_FALCON_THRESHOLD", None) is not None
    assert getattr(settings, "TIER_A_FOLLOWER_COUNT", None) is not None
    assert getattr(settings, "TIER_B_FOLLOWER_COUNT", None) is not None


def test_runtime_config_promotes_all_new_knobs():
    """All 11 new constants must be mutable knobs (ALLOWED_KEYS) so the
    dashboard cockpit can flip them without a redeploy."""
    from src.control.runtime_config import ALLOWED_KEYS, BOUNDS

    new_keys = [
        "falcon_external_discount",
        "tier_a_min_resolved",
        "tier_a_min_winrate",
        "tier_b_min_resolved",
        "tier_b_min_winrate",
        "tier_c_min_resolved",
        "tier_c_min_winrate",
        "tier_a_falcon_threshold",
        "tier_b_falcon_threshold",
        "tier_a_follower_count",
        "tier_b_follower_count",
    ]
    for key in new_keys:
        assert key in ALLOWED_KEYS, f"{key} not promoted to ALLOWED_KEYS"
        assert key in BOUNDS, f"{key} missing BOUNDS entry"
