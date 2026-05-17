"""Unit tests for scripts.seed_decision_learning_2026_05_17 and the new
tier-based / Falcon-prior helpers in scripts.backtest_strategy_2026_05_17.

These tests are 100% in-memory — no real Postgres / Redis. We stub
asyncpg by injecting a fake connection that returns scripted rows and
records every UPSERT so we can assert what got written.

Coverage:
    1. seed idempotency (running twice doesn't double-credit)
    2. seed W/L mapping (pnl_usdc>0 -> wins, pnl_usdc<=0 -> losses)
    3. backtest tier classification (A / B / C from falcon_score + followers)
    4. backtest Falcon prior fusion (effective_resolved + effective_winrate)

A 5th smoke test on `_passes_filters` verifies the tier + Falcon flags
plumb through the filter correctly end-to-end (so we don't break the
existing single-knob behavior).
"""

from __future__ import annotations

import contextlib
import json
from typing import Any
from unittest.mock import AsyncMock

import pytest

from scripts import backtest_strategy_2026_05_17 as backtest
from scripts import seed_decision_learning_2026_05_17 as seed


# ===========================================================================
# Fake asyncpg conn + scripted answers
# ===========================================================================


class _FakeConn:
    """Records every execute call and serves scripted fetch results.

    The fake supports the very small slice of asyncpg surface that the
    seed script uses: `fetch`, `fetchrow`, `execute`, `transaction()`.
    """

    def __init__(
        self,
        *,
        external_check_row: dict | None = None,
        pass1_leaders: list[dict] | None = None,
        pass2_leaders: list[dict] | None = None,
        decisions_by_wallet: dict[str, list[dict]] | None = None,
        positions_by_wallet: dict[str, list[dict]] | None = None,
    ) -> None:
        self._external_check_row = external_check_row or {"column_name": "external_resolved_count"}
        self._pass1_leaders = pass1_leaders or []
        self._pass2_leaders = pass2_leaders or []
        self._decisions_by_wallet = decisions_by_wallet or {}
        self._positions_by_wallet = positions_by_wallet or {}
        self.executes: list[tuple[str, tuple[Any, ...]]] = []

    async def fetchrow(self, sql: str, *args: Any) -> dict | None:
        if "information_schema.columns" in sql:
            return self._external_check_row
        return None

    async def fetch(self, sql: str, *args: Any) -> list[dict]:
        # Identify which query is being run by a distinguishing substring.
        if "lp.external_resolved_count IS NOT NULL" in sql:
            return list(self._pass1_leaders)
        if "lp.positions_resolved >= $1" in sql and "external_resolved_count" not in sql:
            return list(self._pass2_leaders)
        if "INNER JOIN paper_trades pt" in sql:
            wallet = args[0]
            return list(self._decisions_by_wallet.get(wallet, []))
        if "FROM positions_reconstructed" in sql:
            wallet = args[0]
            return list(self._positions_by_wallet.get(wallet, []))
        return []

    async def execute(self, sql: str, *args: Any) -> str:
        self.executes.append((sql, args))
        return "OK"

    @contextlib.asynccontextmanager
    async def transaction(self):
        yield None

    async def close(self) -> None:
        return None


def _patch_asyncpg_connect(monkeypatch, fake: _FakeConn) -> None:
    """Replace `asyncpg.connect` with one that returns the given fake."""
    async def _fake_connect(*_args, **_kwargs):
        return fake

    monkeypatch.setattr(seed.asyncpg, "connect", _fake_connect)


# ===========================================================================
# Helpers — build canned profile rows
# ===========================================================================


def _make_leader_row(
    wallet: str,
    *,
    profile: dict | None = None,
    positions_resolved: int = 50,
    trades_observed: int = 100,
    profile_maturity: float = 0.5,
    external_resolved_count: int | None = 20,
) -> dict:
    row = {
        "wallet_address": wallet,
        "profile_json": json.dumps(profile or {}) if profile else None,
        "positions_resolved": positions_resolved,
        "trades_observed": trades_observed,
        "profile_maturity": profile_maturity,
    }
    if external_resolved_count is not None:
        row["external_resolved_count"] = external_resolved_count
    return row


def _make_decision_row(decision_id: int, *, action: str, pnl_usdc: float, confidence: float = 0.6) -> dict:
    return {
        "decision_id": decision_id,
        "action": action,
        "confidence": confidence,
        "pnl_usdc": pnl_usdc,
        "opened_at": None,
    }


def _make_position_row(position_id: int, *, pnl_usdc: float) -> dict:
    return {
        "id": position_id,
        "pnl_usdc": pnl_usdc,
        "open_time": None,
    }


def _extract_upserted_profile(executes: list[tuple[str, tuple[Any, ...]]]) -> dict | None:
    """Find the most recent UPSERT and return the JSON it carried."""
    for sql, args in reversed(executes):
        if "INSERT INTO leader_profiles" in sql:
            return json.loads(args[1])
    return None


# ===========================================================================
# 1. Idempotency — running twice must not double-credit
# ===========================================================================


@pytest.mark.asyncio
async def test_seed_idempotent_pass1_no_double_credit(monkeypatch):
    """Run pass1 twice for the same leader/decision — second run is a no-op
    on the wins/losses counters because the seed_log marker remembers the
    processed decision_log.id."""
    wallet = "0xabc"
    decisions = [
        _make_decision_row(1, action="follow", pnl_usdc=10.0),
        _make_decision_row(2, action="follow", pnl_usdc=-5.0),
        _make_decision_row(3, action="fade", pnl_usdc=20.0),
    ]

    # First run — empty profile in DB.
    fake = _FakeConn(
        pass1_leaders=[_make_leader_row(wallet)],
        pass2_leaders=[],
        decisions_by_wallet={wallet: decisions},
    )
    _patch_asyncpg_connect(monkeypatch, fake)
    result1 = await seed.run_seeding(
        dsn="postgresql://stub",
        wallet_filter=None,
        min_external_resolved=10,
        min_internal_resolved=30,
        dry_run=False,
    )
    assert result1["decisions_written_pass1"] == 3
    profile_after_run1 = _extract_upserted_profile(fake.executes)
    assert profile_after_run1 is not None
    follow = profile_after_run1["decision_learning"]["follow"]
    fade = profile_after_run1["decision_learning"]["fade"]
    # 1 win + 1 loss for FOLLOW, 1 win for FADE.
    assert follow["wins"] == 1
    assert follow["losses"] == 1
    assert fade["wins"] == 1
    assert fade["losses"] == 0
    assert sorted(
        profile_after_run1["seed_log"][seed.SEED_MARKER_KEY]["processed_decision_ids"]
    ) == [1, 2, 3]

    # Second run — feed the already-seeded profile back as the input row.
    fake2 = _FakeConn(
        pass1_leaders=[_make_leader_row(wallet, profile=profile_after_run1)],
        pass2_leaders=[],
        decisions_by_wallet={wallet: decisions},  # same 3 decisions
    )
    _patch_asyncpg_connect(monkeypatch, fake2)
    result2 = await seed.run_seeding(
        dsn="postgresql://stub",
        wallet_filter=None,
        min_external_resolved=10,
        min_internal_resolved=30,
        dry_run=False,
    )
    # Second run sees the same 3 IDs in the marker — no new credits.
    assert result2["decisions_written_pass1"] == 0
    # And — critically — no UPSERT was executed (nothing changed).
    assert not any(
        "INSERT INTO leader_profiles" in sql for sql, _ in fake2.executes
    ), "Idempotent re-run must NOT touch leader_profiles when nothing was credited."


@pytest.mark.asyncio
async def test_seed_idempotent_pass2_no_double_credit(monkeypatch):
    """Same as the pass1 test but for pass2 (positions_reconstructed)."""
    wallet = "0xdef"
    positions = [
        _make_position_row(101, pnl_usdc=5.0),
        _make_position_row(102, pnl_usdc=-2.0),
    ]

    fake = _FakeConn(
        pass1_leaders=[],
        pass2_leaders=[_make_leader_row(wallet, external_resolved_count=None)],
        positions_by_wallet={wallet: positions},
    )
    _patch_asyncpg_connect(monkeypatch, fake)
    result1 = await seed.run_seeding(
        dsn="postgresql://stub",
        wallet_filter=None,
        min_external_resolved=10,
        min_internal_resolved=30,
        dry_run=False,
    )
    assert result1["decisions_written_pass2"] == 2
    profile_after_run1 = _extract_upserted_profile(fake.executes)
    follow = profile_after_run1["decision_learning"]["follow"]
    assert follow["wins"] == 1
    assert follow["losses"] == 1

    # Re-run with the already-seeded profile.
    fake2 = _FakeConn(
        pass1_leaders=[],
        pass2_leaders=[_make_leader_row(wallet, profile=profile_after_run1, external_resolved_count=None)],
        positions_by_wallet={wallet: positions},
    )
    _patch_asyncpg_connect(monkeypatch, fake2)
    result2 = await seed.run_seeding(
        dsn="postgresql://stub",
        wallet_filter=None,
        min_external_resolved=10,
        min_internal_resolved=30,
        dry_run=False,
    )
    assert result2["decisions_written_pass2"] == 0


# ===========================================================================
# 2. W/L mapping — pnl_usdc>0 -> win, pnl_usdc<=0 -> loss
# ===========================================================================


@pytest.mark.asyncio
async def test_seed_pnl_sign_drives_win_loss(monkeypatch):
    """Verify the sign of pnl_usdc maps exactly to wins vs losses, including
    the edge case pnl_usdc == 0 (treated as a loss — strictly > 0 is a win)."""
    wallet = "0x000"
    decisions = [
        _make_decision_row(10, action="follow", pnl_usdc=0.01),     # win
        _make_decision_row(11, action="follow", pnl_usdc=-0.01),    # loss
        _make_decision_row(12, action="follow", pnl_usdc=0.0),      # loss (not > 0)
        _make_decision_row(13, action="fade", pnl_usdc=100.0),      # win
        _make_decision_row(14, action="fade", pnl_usdc=-50.0),      # loss
    ]
    fake = _FakeConn(
        pass1_leaders=[_make_leader_row(wallet)],
        decisions_by_wallet={wallet: decisions},
    )
    _patch_asyncpg_connect(monkeypatch, fake)
    await seed.run_seeding(
        dsn="postgresql://stub",
        wallet_filter=None,
        min_external_resolved=10,
        min_internal_resolved=30,
        dry_run=False,
    )
    profile = _extract_upserted_profile(fake.executes)
    assert profile is not None
    follow = profile["decision_learning"]["follow"]
    fade = profile["decision_learning"]["fade"]
    # FOLLOW: 1 win (0.01), 2 losses (-0.01 and 0.0)
    assert follow["wins"] == 1
    assert follow["losses"] == 2
    # FADE: 1 win, 1 loss
    assert fade["wins"] == 1
    assert fade["losses"] == 1
    # Beta posteriors track the same counts plus the Beta(1,1) prior.
    assert follow["beta_a"] == pytest.approx(2.0)   # 1 + 1
    assert follow["beta_b"] == pytest.approx(3.0)   # 1 + 2
    assert fade["beta_a"] == pytest.approx(2.0)
    assert fade["beta_b"] == pytest.approx(2.0)


# ===========================================================================
# 3. Backtest tier classification
# ===========================================================================


def test_backtest_tier_classification_falcon_threshold():
    """Tier A on falcon_score>=50, Tier B on falcon_score>=20, else Tier C."""
    base = {
        "falcon_score": 60.0,
        "confirmed_followers": 0,
    }
    assert backtest.classify_tier(
        base, tier_a_falcon=50, tier_a_confirmed=5, tier_b_falcon=20, tier_b_confirmed=3
    ) == "A"

    base["falcon_score"] = 25.0
    assert backtest.classify_tier(
        base, tier_a_falcon=50, tier_a_confirmed=5, tier_b_falcon=20, tier_b_confirmed=3
    ) == "B"

    base["falcon_score"] = 10.0
    assert backtest.classify_tier(
        base, tier_a_falcon=50, tier_a_confirmed=5, tier_b_falcon=20, tier_b_confirmed=3
    ) == "C"

    # Tier A wins via confirmed-followers alternative even if falcon is low.
    assert backtest.classify_tier(
        {"falcon_score": 0.0, "confirmed_followers": 7},
        tier_a_falcon=50, tier_a_confirmed=5, tier_b_falcon=20, tier_b_confirmed=3,
    ) == "A"

    # Missing falcon_score -> 0.0 -> tier C unless followers promote it.
    assert backtest.classify_tier(
        {"falcon_score": None, "confirmed_followers": 0},
        tier_a_falcon=50, tier_a_confirmed=5, tier_b_falcon=20, tier_b_confirmed=3,
    ) == "C"
    assert backtest.classify_tier(
        {"falcon_score": None, "confirmed_followers": 4},
        tier_a_falcon=50, tier_a_confirmed=5, tier_b_falcon=20, tier_b_confirmed=3,
    ) == "B"


def test_backtest_passes_filters_uses_tier_thresholds():
    """End-to-end smoke: a leader that would be REJECTED by the global
    (30, 0.55) gate is ACCEPTED by tier-A thresholds (10, 0.50)."""
    row = {
        "leader_resolved": 15,
        "leader_winrate": 0.52,
        "entry_price": 0.5,
        "holding_period_s": 3600,
        "category": "sports",
        "external_resolved": 0,
        "external_wins": 0,
        "external_losses": 0,
        "falcon_score": 75.0,   # tier A by falcon
        "confirmed_followers": 0,
    }
    tier_table = backtest.build_tier_table(
        tier_a_min_resolved=10, tier_a_min_winrate=0.50,
        tier_b_min_resolved=20, tier_b_min_winrate=0.55,
        tier_c_min_resolved=30, tier_c_min_winrate=0.55,
    )
    # Without tier-based gating — rejected (15 < 30).
    assert backtest._passes_filters(
        row,
        min_leader_resolved=30, min_leader_winrate=0.55,
        entry_min=0.40, entry_max=0.92,
        max_hold_s=86_400, categories={"sports"},
        use_tier_based=False, tier_thresholds=tier_table,
    ) is False
    # With tier-based gating — accepted (tier A: 15 >= 10 AND 0.52 >= 0.50).
    assert backtest._passes_filters(
        row,
        min_leader_resolved=30, min_leader_winrate=0.55,
        entry_min=0.40, entry_max=0.92,
        max_hold_s=86_400, categories={"sports"},
        use_tier_based=True, tier_thresholds=tier_table,
    ) is True


# ===========================================================================
# 4. Backtest Falcon prior fusion
# ===========================================================================


def test_fuse_falcon_prior_inflates_effective_resolved_and_winrate():
    """A leader with 0 internal observations but 20 external wins / 0 external
    losses should yield effective_resolved>=10 (20*0.5=10) and effective_winrate
    near 1.0 (with the +Beta(1,1) implicit prior baked into the fused alpha/beta)."""
    row = {
        "leader_resolved": 0,
        "leader_winrate": None,
        "external_resolved": 20,
        "external_wins": 20,
        "external_losses": 0,
    }
    eff_resolved, eff_winrate = backtest.fuse_falcon_prior(row, external_discount=0.5)
    # 20 * 0.5 = 10
    assert eff_resolved == 10
    # alpha_internal=0, beta_internal=0, alpha_external=20, beta_external=0
    # total_alpha = 0.5 * 20 = 10, total_beta = 0
    # → 10 / 10 = 1.0
    assert eff_winrate == pytest.approx(1.0)


def test_fuse_falcon_prior_blends_internal_and_external():
    """Internal (10 wins, 10 losses) + external (40 wins, 0 losses, discount 0.5).
    Expected effective_winrate = (10 + 0.5*40) / (10 + 0.5*40 + 10 + 0.5*0) = 30 / 40 = 0.75."""
    row = {
        "leader_resolved": 20,
        "leader_winrate": 0.5,        # 10 wins out of 20
        "external_resolved": 40,
        "external_wins": 40,
        "external_losses": 0,
    }
    eff_resolved, eff_winrate = backtest.fuse_falcon_prior(row, external_discount=0.5)
    # effective_resolved = max(20, 40*0.5=20) = 20
    assert eff_resolved == 20
    # winrate fusion: alpha = 10 + 0.5*40 = 30; beta = 10 + 0 = 10; total=40 → 0.75
    assert eff_winrate == pytest.approx(0.75)


def test_fuse_falcon_prior_no_external_is_passthrough():
    """If external counts are all zero, the fusion equals the internal winrate
    (within rounding from the alpha/beta reconstruction)."""
    row = {
        "leader_resolved": 50,
        "leader_winrate": 0.60,
        "external_resolved": 0,
        "external_wins": 0,
        "external_losses": 0,
    }
    eff_resolved, eff_winrate = backtest.fuse_falcon_prior(row, external_discount=0.5)
    assert eff_resolved == 50
    # alpha = round(0.6 * 50) = 30; beta = 20; 30/50 = 0.60.
    assert eff_winrate == pytest.approx(0.60)


def test_fuse_falcon_prior_external_only_uses_external_winrate():
    """A pure-Falcon leader (0 internal, 15 wins / 5 losses external) is
    accepted when external_resolved * discount >= min_resolved, and its
    winrate is exactly 15/20 = 0.75 regardless of the discount."""
    row = {
        "leader_resolved": 0,
        "leader_winrate": None,
        "external_resolved": 20,
        "external_wins": 15,
        "external_losses": 5,
    }
    eff_resolved, eff_winrate = backtest.fuse_falcon_prior(row, external_discount=0.5)
    # effective_resolved = 20 * 0.5 = 10 (passes a tier-A 10 floor)
    assert eff_resolved == 10
    # alpha = 0.5 * 15 = 7.5; beta = 0.5 * 5 = 2.5; total = 10; → 0.75
    assert eff_winrate == pytest.approx(0.75)
