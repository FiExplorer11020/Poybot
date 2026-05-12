"""
Tests for FollowerVolumeDaemon — Round 9 (The Web).

Coverage: daemon shape (graceful cancel, empty-leader pass).
"""

from __future__ import annotations

import asyncio

import pytest

from src.follower_volume.daemon import FollowerVolumeDaemon


@pytest.mark.asyncio
async def test_daemon_stop_is_idempotent():
    """Calling stop() twice doesn't blow up."""
    daemon = FollowerVolumeDaemon()
    await daemon.stop()
    await daemon.stop()


@pytest.mark.asyncio
async def test_daemon_empty_load_leaders_short_circuits(monkeypatch):
    """When _load_leaders returns no leaders, run_one_pass returns a
    zeroed summary dict without crashing."""
    daemon = FollowerVolumeDaemon()

    async def _no_leaders(self):
        return []

    monkeypatch.setattr(
        FollowerVolumeDaemon, "_load_leaders", _no_leaders
    )
    result = await daemon.run_one_pass()
    assert isinstance(result, dict)
    assert result["refit"] == 0


@pytest.mark.asyncio
async def test_daemon_start_then_stop_completes(monkeypatch):
    """start() runs at least one pass, then stop() unblocks it."""
    daemon = FollowerVolumeDaemon(refresh_interval_s=3600.0)

    async def _no_leaders(self):
        return []

    monkeypatch.setattr(
        FollowerVolumeDaemon, "_load_leaders", _no_leaders
    )

    task = asyncio.create_task(daemon.start())
    # Let the daemon run one pass then stop it.
    await asyncio.sleep(0.05)
    await daemon.stop()
    try:
        await asyncio.wait_for(task, timeout=2.0)
    except asyncio.TimeoutError:
        task.cancel()
        pytest.fail("daemon failed to stop within timeout")
