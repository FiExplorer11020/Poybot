"""Tests for CausalDaemon — R10 nightly 2SLS batch.

Coverage: daemon shape (graceful cancel, empty-pair pass, start-then-
stop). Matches the R9 test_daemon pattern.
"""

from __future__ import annotations

import asyncio

import pytest

from src.causal.daemon import CausalDaemon


@pytest.mark.asyncio
async def test_daemon_stop_is_idempotent():
    """Calling stop() twice doesn't blow up."""
    daemon = CausalDaemon()
    await daemon.stop()
    await daemon.stop()


@pytest.mark.asyncio
async def test_daemon_empty_pairs_short_circuits(monkeypatch):
    """When _load_pairs returns no leaders, run_one_pass returns 0-counts."""
    daemon = CausalDaemon()

    async def _no_pairs(self):
        return []

    monkeypatch.setattr(CausalDaemon, "_load_pairs", _no_pairs)
    result = await daemon.run_one_pass()
    assert isinstance(result, dict)
    assert result["estimated"] == 0


@pytest.mark.asyncio
async def test_daemon_start_then_stop_completes(monkeypatch):
    """start() runs at least one pass, then stop() unblocks it."""
    daemon = CausalDaemon(refresh_interval_s=3600.0)

    async def _no_pairs(self):
        return []

    monkeypatch.setattr(CausalDaemon, "_load_pairs", _no_pairs)

    task = asyncio.create_task(daemon.start())
    await asyncio.sleep(0.05)
    await daemon.stop()
    try:
        await asyncio.wait_for(task, timeout=2.0)
    except asyncio.TimeoutError:
        task.cancel()
        pytest.fail("daemon failed to stop within timeout")
