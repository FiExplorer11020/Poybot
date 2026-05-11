"""Unit tests for src/crawler/depth_tiers.py — AdaptiveDepth + policy.

Covers:
  * expected_tier (Wave-1 policy): boundary conditions at exactly the
    FULL and PERIODIC thresholds.
  * review_tiers: synthetic wallet set → bulk-update pattern (3 grouped
    UPDATEs by target tier), promotion counter fires for transitions
    only.
  * Performance sanity: 100-wallet review completes in <1s.
  * run_daemon_loop: starts, processes once, exits cleanly on
    CancelledError.

asyncpg is mocked the same way as test_universe.
"""

import asyncio
import time
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import src.crawler.depth_tiers as depth_mod
from src.config import settings
from src.crawler.depth_tiers import AdaptiveDepth, DepthTier, expected_tier

# ---------------------------------------------------------------------- #
# expected_tier policy — boundary conditions                              #
# ---------------------------------------------------------------------- #


def test_expected_tier_below_periodic_threshold_returns_light():
    stats = {"volume_30d_usdc": settings.WALLET_UNIVERSE_PERIODIC_TIER_VOLUME_THRESHOLD_USDC - 1.0}
    assert expected_tier(stats) == DepthTier.LIGHT


def test_expected_tier_at_periodic_threshold_returns_periodic():
    # The implementation uses `>=` for the boundary — wallet at exactly
    # the threshold sits in PERIODIC.
    stats = {"volume_30d_usdc": settings.WALLET_UNIVERSE_PERIODIC_TIER_VOLUME_THRESHOLD_USDC}
    assert expected_tier(stats) == DepthTier.PERIODIC


def test_expected_tier_between_thresholds_returns_periodic():
    stats = {"volume_30d_usdc": settings.WALLET_UNIVERSE_PERIODIC_TIER_VOLUME_THRESHOLD_USDC + 1.0}
    assert expected_tier(stats) == DepthTier.PERIODIC


def test_expected_tier_at_full_threshold_returns_full():
    stats = {"volume_30d_usdc": settings.WALLET_UNIVERSE_FULL_TIER_VOLUME_THRESHOLD_USDC}
    assert expected_tier(stats) == DepthTier.FULL


def test_expected_tier_far_above_full_threshold_returns_full():
    stats = {"volume_30d_usdc": settings.WALLET_UNIVERSE_FULL_TIER_VOLUME_THRESHOLD_USDC * 10}
    assert expected_tier(stats) == DepthTier.FULL


def test_expected_tier_missing_keys_defaults_to_light():
    assert expected_tier({}) == DepthTier.LIGHT
    assert expected_tier({"volume_30d_usdc": None}) == DepthTier.LIGHT


def test_class_alias_matches_module_function():
    a = AdaptiveDepth()
    stats = {"volume_30d_usdc": 1.0}
    assert a.expected_tier(stats) == expected_tier(stats)


# ---------------------------------------------------------------------- #
# Mock fixtures                                                           #
# ---------------------------------------------------------------------- #


def _make_conn():
    conn = AsyncMock()
    conn.execute = AsyncMock()
    conn.fetch = AsyncMock(return_value=[])

    @asynccontextmanager
    async def _tx():
        yield None

    conn.transaction = MagicMock(side_effect=lambda *a, **kw: _tx())
    return conn


def _patch_get_db(conn):
    @asynccontextmanager
    async def _fake_get_db():
        yield conn

    return patch("src.crawler.depth_tiers.get_db", _fake_get_db)


# ---------------------------------------------------------------------- #
# review_tiers                                                            #
# ---------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_review_tiers_classifies_synthetic_wallet_set():
    """Mix of FULL/PERIODIC/LIGHT-class wallets — verify post-sweep
    counts match expected_tier outputs."""
    full_v = settings.WALLET_UNIVERSE_FULL_TIER_VOLUME_THRESHOLD_USDC
    periodic_v = settings.WALLET_UNIVERSE_PERIODIC_TIER_VOLUME_THRESHOLD_USDC

    rows = [
        # current tier 2, but volume above FULL → promote to 0
        {"wallet_address": "0xfull", "depth_tier": 2,
         "volume_30d_usdc": full_v + 1.0, "trades_30d": 100},
        # current tier 2, volume above PERIODIC but below FULL → promote to 1
        {"wallet_address": "0xperiodic", "depth_tier": 2,
         "volume_30d_usdc": periodic_v + 1.0, "trades_30d": 50},
        # current tier 1, volume now near zero → demote to 2
        {"wallet_address": "0xstaledemote", "depth_tier": 1,
         "volume_30d_usdc": 0.0, "trades_30d": 0},
        # current tier 2, volume tiny → stays at 2 (no transition)
        {"wallet_address": "0xstay", "depth_tier": 2,
         "volume_30d_usdc": 100.0, "trades_30d": 1},
    ]
    conn = _make_conn()
    conn.fetch = AsyncMock(return_value=rows)

    a = AdaptiveDepth()
    with _patch_get_db(conn):
        counts = await a.review_tiers()

    # Post-sweep counts: 1 FULL, 1 PERIODIC, 2 LIGHT.
    assert counts[DepthTier.FULL] == 1
    assert counts[DepthTier.PERIODIC] == 1
    assert counts[DepthTier.LIGHT] == 2


@pytest.mark.asyncio
async def test_review_tiers_uses_bulk_update_pattern_one_per_target_tier():
    """Each target tier with transitions must produce exactly one
    UPDATE — the bulk pattern that makes 1.5M wallets feasible."""
    full_v = settings.WALLET_UNIVERSE_FULL_TIER_VOLUME_THRESHOLD_USDC
    periodic_v = settings.WALLET_UNIVERSE_PERIODIC_TIER_VOLUME_THRESHOLD_USDC

    # Two wallets promoting to FULL, three to PERIODIC, one to LIGHT.
    rows = [
        {"wallet_address": "0xf1", "depth_tier": 2,
         "volume_30d_usdc": full_v + 1, "trades_30d": 0},
        {"wallet_address": "0xf2", "depth_tier": 1,
         "volume_30d_usdc": full_v + 1, "trades_30d": 0},
        {"wallet_address": "0xp1", "depth_tier": 2,
         "volume_30d_usdc": periodic_v + 1, "trades_30d": 0},
        {"wallet_address": "0xp2", "depth_tier": 2,
         "volume_30d_usdc": periodic_v + 1, "trades_30d": 0},
        {"wallet_address": "0xp3", "depth_tier": 0,
         "volume_30d_usdc": periodic_v + 1, "trades_30d": 0},
        {"wallet_address": "0xl1", "depth_tier": 1,
         "volume_30d_usdc": 0.0, "trades_30d": 0},
    ]
    conn = _make_conn()
    conn.fetch = AsyncMock(return_value=rows)

    a = AdaptiveDepth()
    with _patch_get_db(conn):
        await a.review_tiers()

    # 3 grouped UPDATEs (one per target tier with at least one transition).
    updates = [
        c for c in conn.execute.await_args_list
        if c.args and "UPDATE wallet_universe" in c.args[0]
    ]
    assert len(updates) == 3
    # Each update binds (target_tier:int, wallet_list:list).
    target_to_wallets = {c.args[1]: c.args[2] for c in updates}
    assert sorted(target_to_wallets[int(DepthTier.FULL)]) == ["0xf1", "0xf2"]
    assert sorted(target_to_wallets[int(DepthTier.PERIODIC)]) == ["0xp1", "0xp2", "0xp3"]
    assert sorted(target_to_wallets[int(DepthTier.LIGHT)]) == ["0xl1"]


@pytest.mark.asyncio
async def test_review_tiers_emits_promotion_counter_only_for_transitions():
    full_v = settings.WALLET_UNIVERSE_FULL_TIER_VOLUME_THRESHOLD_USDC
    rows = [
        # transition 2 → 0
        {"wallet_address": "0xa", "depth_tier": 2,
         "volume_30d_usdc": full_v + 1, "trades_30d": 0},
        # no transition — already at correct tier (LIGHT)
        {"wallet_address": "0xb", "depth_tier": 2,
         "volume_30d_usdc": 0.0, "trades_30d": 0},
    ]
    conn = _make_conn()
    conn.fetch = AsyncMock(return_value=rows)

    counter = MagicMock()
    counter.labels = MagicMock(return_value=MagicMock(inc=MagicMock()))

    a = AdaptiveDepth()
    with _patch_get_db(conn), patch.object(
        depth_mod, "wallet_universe_promotions_total", counter
    ):
        await a.review_tiers()

    # Exactly one increment — only the 2→0 transition counts.
    counter.labels.assert_called_once_with(from_tier="2", to_tier="0")
    counter.labels.return_value.inc.assert_called_once()


@pytest.mark.asyncio
async def test_review_tiers_no_update_when_nothing_changed():
    """If every wallet is already at its expected tier, we still issue
    the SELECT but skip the UPDATE round-trips."""
    rows = [
        # LIGHT wallet sitting at LIGHT — no transition.
        {"wallet_address": "0xb", "depth_tier": 2,
         "volume_30d_usdc": 0.0, "trades_30d": 0},
    ]
    conn = _make_conn()
    conn.fetch = AsyncMock(return_value=rows)

    a = AdaptiveDepth()
    with _patch_get_db(conn):
        await a.review_tiers()

    updates = [
        c for c in conn.execute.await_args_list
        if c.args and "UPDATE wallet_universe" in c.args[0]
    ]
    assert updates == []


@pytest.mark.asyncio
async def test_review_tiers_refreshes_tier_count_gauge():
    full_v = settings.WALLET_UNIVERSE_FULL_TIER_VOLUME_THRESHOLD_USDC
    periodic_v = settings.WALLET_UNIVERSE_PERIODIC_TIER_VOLUME_THRESHOLD_USDC
    rows = [
        {"wallet_address": "0xa", "depth_tier": 0,
         "volume_30d_usdc": full_v + 1, "trades_30d": 0},
        {"wallet_address": "0xb", "depth_tier": 1,
         "volume_30d_usdc": periodic_v + 1, "trades_30d": 0},
        {"wallet_address": "0xc", "depth_tier": 2,
         "volume_30d_usdc": 0.0, "trades_30d": 0},
    ]
    conn = _make_conn()
    conn.fetch = AsyncMock(return_value=rows)

    gauge = MagicMock()
    gauge.labels = MagicMock(return_value=MagicMock(set=MagicMock()))

    a = AdaptiveDepth()
    with _patch_get_db(conn), patch.object(
        depth_mod, "wallet_universe_tier_count", gauge
    ):
        await a.review_tiers()

    # One .set() per tier (3 calls).
    label_args = {c.kwargs.get("tier") for c in gauge.labels.call_args_list}
    assert label_args == {"0", "1", "2"}


@pytest.mark.asyncio
async def test_review_tiers_perf_sanity_100_wallets_under_1s():
    """Sanity check — 100-wallet review completes well under 1 s. The
    actual production target is <60 s for 1.5M wallets, which exercises
    the same code path."""
    full_v = settings.WALLET_UNIVERSE_FULL_TIER_VOLUME_THRESHOLD_USDC
    periodic_v = settings.WALLET_UNIVERSE_PERIODIC_TIER_VOLUME_THRESHOLD_USDC
    rows = []
    for i in range(100):
        # Cycle through all three tiers so the bucket logic gets exercised.
        if i % 3 == 0:
            vol = full_v + 1.0
        elif i % 3 == 1:
            vol = periodic_v + 1.0
        else:
            vol = 0.0
        rows.append(
            {
                "wallet_address": f"0x{i:040x}",
                "depth_tier": 2,
                "volume_30d_usdc": vol,
                "trades_30d": 0,
            }
        )
    conn = _make_conn()
    conn.fetch = AsyncMock(return_value=rows)

    a = AdaptiveDepth()
    start = time.perf_counter()
    with _patch_get_db(conn):
        await a.review_tiers()
    elapsed = time.perf_counter() - start
    assert elapsed < 1.0, f"100-wallet review took {elapsed:.3f}s"


# ---------------------------------------------------------------------- #
# run_daemon_loop                                                         #
# ---------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_run_daemon_loop_processes_once_then_exits_on_cancel(monkeypatch):
    """The loop must (a) call review_tiers at least once, (b) honour
    CancelledError cleanly."""
    a = AdaptiveDepth()

    call_count = 0

    async def _fake_review():
        nonlocal call_count
        call_count += 1
        return {DepthTier.FULL: 0, DepthTier.PERIODIC: 0, DepthTier.LIGHT: 0}

    a.review_tiers = _fake_review  # type: ignore[method-assign]

    # Force the loop's sleep to be effectively forever so the test
    # controls when to cancel.
    monkeypatch.setattr(
        depth_mod.settings, "WALLET_UNIVERSE_REVIEW_INTERVAL_S", 3600
    )

    task = asyncio.create_task(a.run_daemon_loop())
    # Yield enough for the first review to run before we cancel.
    for _ in range(5):
        await asyncio.sleep(0)
        if call_count >= 1:
            break

    assert call_count >= 1
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_run_daemon_loop_continues_on_review_exception(monkeypatch):
    """A failed review_tiers must NOT crash the daemon — log + retry."""
    a = AdaptiveDepth()
    call_count = 0

    async def _flaky_review():
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("boom")
        return {DepthTier.FULL: 0, DepthTier.PERIODIC: 0, DepthTier.LIGHT: 0}

    a.review_tiers = _flaky_review  # type: ignore[method-assign]
    monkeypatch.setattr(
        depth_mod.settings, "WALLET_UNIVERSE_REVIEW_INTERVAL_S", 0
    )

    task = asyncio.create_task(a.run_daemon_loop())
    # With interval=0 sleeps return immediately; give the loop a few
    # turns to land both calls.
    for _ in range(20):
        await asyncio.sleep(0)
        if call_count >= 2:
            break

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert call_count >= 2
