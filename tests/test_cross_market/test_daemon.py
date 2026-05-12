"""CrossMarketDaemon tests."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.cross_market.daemon import CrossMarketDaemon


class _NoOpClient:
    venue = "noop"

    async def fetch_wallet_positions(self, _):
        return []


def _mock_get_db():
    conn = AsyncMock()

    async def _fetch(query, *args):
        return []

    conn.fetch = _fetch
    conn.execute = AsyncMock()

    @asynccontextmanager
    async def _ctx():
        yield conn

    return _ctx


class TestRunOnce:
    @pytest.mark.asyncio
    async def test_run_once_with_no_operators(self):
        ctx = _mock_get_db()
        with patch(
            "src.cross_market.position_aggregator.get_db", side_effect=ctx
        ):
            daemon = CrossMarketDaemon(
                kalshi=_NoOpClient(),
                manifold=_NoOpClient(),
                predictit=_NoOpClient(),
                poll_interval_s=1,
            )
            summary = await daemon.run_once()
        assert summary["n_operators"] == 0
        assert summary["n_rows_written"] == 0


class TestStop:
    @pytest.mark.asyncio
    async def test_stop_event_releases_run_forever(self):
        ctx = _mock_get_db()
        with patch(
            "src.cross_market.position_aggregator.get_db", side_effect=ctx
        ):
            daemon = CrossMarketDaemon(
                kalshi=_NoOpClient(),
                manifold=_NoOpClient(),
                predictit=_NoOpClient(),
                poll_interval_s=1,
            )
            task = asyncio.create_task(daemon.run_forever())
            # Give the daemon a chance to start.
            await asyncio.sleep(0.01)
            await daemon.stop()
            await asyncio.wait_for(task, timeout=2.0)
