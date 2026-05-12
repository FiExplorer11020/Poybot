"""Unit tests for the R11 additions to :mod:`src.profiler.feature_store`:

  * :func:`get_microstructure_features_asof`
  * :func:`get_wallet_microstructure_signature_asof`

Both follow the existing AS-OF contract — return None when no row is
within ``lookback_*`` of asof_ts, return the row dict otherwise.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from src.profiler.feature_store import (
    get_microstructure_features_asof,
    get_wallet_microstructure_signature_asof,
)


@pytest.fixture
def asof_ts():
    return datetime(2026, 5, 12, 10, 0, 0, tzinfo=timezone.utc)


# --------------------------------------------------------------------------- #
# 1. get_microstructure_features_asof                                          #
# --------------------------------------------------------------------------- #


class TestMicrostructureFeaturesAsof:
    @pytest.mark.asyncio
    async def test_returns_row_when_present(self, asof_ts):
        conn = AsyncMock()
        bucket_ts = asof_ts - timedelta(seconds=30)
        conn.fetchrow = AsyncMock(
            return_value={
                "bucket_ts": bucket_ts,
                "iceberg_orders_count": 2,
                "iceberg_total_size": 250.0,
                "spoof_orders_count": 1,
                "spoof_total_size": 5000.0,
                "ofi_mean": 0.15,
                "ofi_max": 0.4,
                "ofi_min": -0.1,
                "ofi_std": 0.12,
            }
        )
        result = await get_microstructure_features_asof(
            conn, "m1", "t1", asof_ts
        )
        assert result is not None
        assert result["bucket_ts"] == bucket_ts
        assert result["iceberg_orders_count"] == 2
        assert result["spoof_orders_count"] == 1
        assert result["ofi_mean"] == 0.15
        assert "feature_age_s" in result
        assert result["feature_age_s"] == pytest.approx(30.0)

    @pytest.mark.asyncio
    async def test_returns_none_when_no_row(self, asof_ts):
        conn = AsyncMock()
        conn.fetchrow = AsyncMock(return_value=None)
        result = await get_microstructure_features_asof(
            conn, "m1", "t1", asof_ts
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_query_uses_market_token_and_lookback(self, asof_ts):
        captured = []
        conn = AsyncMock()

        async def _fetchrow(sql, *args):
            captured.append((sql, args))
            return None

        conn.fetchrow = _fetchrow
        await get_microstructure_features_asof(
            conn, "m_target", "t_target", asof_ts, lookback_s=120
        )
        assert len(captured) == 1
        _sql, args = captured[0]
        # args = (market_id, token_id, asof, floor)
        assert args[0] == "m_target"
        assert args[1] == "t_target"
        assert args[2] == asof_ts
        # floor = asof - 120s
        assert args[3] == asof_ts - timedelta(seconds=120)


# --------------------------------------------------------------------------- #
# 2. get_wallet_microstructure_signature_asof                                  #
# --------------------------------------------------------------------------- #


class TestWalletSignatureAsof:
    @pytest.mark.asyncio
    async def test_returns_row_when_present(self, asof_ts):
        rollup_at = asof_ts - timedelta(hours=2)
        conn = AsyncMock()
        conn.fetchrow = AsyncMock(
            return_value={
                "rollup_at": rollup_at,
                "cancel_to_fill_ratio_30d": 2.5,
                "iceberg_score_30d": 0.1,
                "spoof_score_30d": 0.05,
                "place_to_fill_seconds_p50": 30.0,
                "place_to_fill_seconds_p99": 600.0,
                "n_orders_30d": 200,
                "n_fills_30d": 80,
            }
        )
        result = await get_wallet_microstructure_signature_asof(
            conn, "0xabc", asof_ts
        )
        assert result is not None
        assert result["cancel_to_fill_ratio_30d"] == 2.5
        assert result["n_orders_30d"] == 200
        assert "signature_age_s" in result

    @pytest.mark.asyncio
    async def test_returns_none_when_no_signature(self, asof_ts):
        conn = AsyncMock()
        conn.fetchrow = AsyncMock(return_value=None)
        result = await get_wallet_microstructure_signature_asof(
            conn, "0xabc", asof_ts
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_query_caps_at_lookback_days(self, asof_ts):
        captured = []
        conn = AsyncMock()

        async def _fetchrow(sql, *args):
            captured.append((sql, args))
            return None

        conn.fetchrow = _fetchrow
        await get_wallet_microstructure_signature_asof(
            conn, "0xabc", asof_ts, lookback_days=30
        )
        _sql, args = captured[0]
        # args = (wallet, asof, floor)
        assert args[0] == "0xabc"
        assert args[1] == asof_ts
        assert args[2] == asof_ts - timedelta(days=30)

    @pytest.mark.asyncio
    async def test_failure_returns_none_not_raise(self, asof_ts):
        conn = AsyncMock()
        conn.fetchrow = AsyncMock(side_effect=RuntimeError("DB error"))
        # Must not raise — readers prefer None to a crash so the R8
        # extractor degrades to nan gracefully.
        result = await get_wallet_microstructure_signature_asof(
            conn, "0xabc", asof_ts
        )
        assert result is None
