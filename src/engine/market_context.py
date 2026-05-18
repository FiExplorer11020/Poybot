"""Plan 2026-05-19 P2 — market-context feature aggregator.

Wires the existing microstructure + social + cross-market feature
stores (under ``src/profiler/feature_store.py``) into the confidence
engine's per-decision context dict. These features were derived by
the standalone daemons (microstructure/daemon.py, social/daemon.py,
cross_market/daemon.py) but NEVER consumed by the engine — the audit
agent confirmed ``grep "from src.social|microstructure|cross_market"
src/engine/`` returned zero results.

This module bridges that gap. ``fetch_market_context`` returns a flat
dict of features that:
  1. confidence_engine.evaluate stamps onto ``trade_context``
  2. sizing_penalties consumes for additional penalty contributors

The reads are best-effort. A failed DB lookup returns an empty dict
rather than raising — the bot must keep deciding even when the
microstructure rollup is behind.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from loguru import logger

from src.database.connection import get_db
from src.profiler.feature_store import (
    get_cross_market_features_asof,
    get_microstructure_features_asof,
    get_social_signals_asof,
)

# Soft cap on the total time we'll spend fetching context features per
# decision. The engine's 100ms decision budget needs to come first.
_FETCH_TIMEOUT_S = 0.8


async def _safe_microstructure(
    market_id: str, token_id: str, asof: datetime
) -> dict[str, Any]:
    if not market_id or not token_id:
        return {}
    try:
        async with get_db() as conn:
            row = await get_microstructure_features_asof(
                conn, market_id, token_id, asof, lookback_s=300
            )
    except Exception as exc:
        logger.debug(f"market_context: microstructure read failed: {exc}")
        return {}
    if not row:
        return {}
    return {
        "ofi_mean": row.get("ofi_mean"),
        "ofi_max": row.get("ofi_max"),
        "ofi_min": row.get("ofi_min"),
        "ofi_std": row.get("ofi_std"),
        "iceberg_count": row.get("iceberg_orders_count"),
        "spoof_count": row.get("spoof_orders_count"),
        "microstructure_feature_age_s": row.get("feature_age_s"),
    }


async def _safe_social(wallet: str, asof: datetime) -> dict[str, Any]:
    if not wallet:
        return {}
    try:
        async with get_db() as conn:
            agg = await get_social_signals_asof(
                conn, wallet, asof, lookback_days=14
            )
    except Exception as exc:
        logger.debug(f"market_context: social read failed: {exc}")
        return {}
    if not agg:
        return {}
    return {
        "social_entry_signal_count_14d": agg.get("entry_signal_count"),
        "social_exit_signal_count_14d": agg.get("exit_signal_count"),
        "social_last_signal_age_s": agg.get("last_signal_age_s"),
        "social_last_intent": agg.get("last_intent"),
    }


async def _safe_cross_market(wallet: str, asof: datetime) -> dict[str, Any]:
    if not wallet:
        return {}
    try:
        async with get_db() as conn:
            agg = await get_cross_market_features_asof(
                conn, wallet, asof, lookback_days=7
            )
    except Exception as exc:
        logger.debug(f"market_context: cross_market read failed: {exc}")
        return {}
    if not agg:
        return {}
    return {
        "cross_venue_position_count_7d": agg.get("position_count"),
        "cross_venue_total_size_usdc_7d": agg.get("total_size_usdc"),
        "cross_venue_correlation": agg.get("correlation"),
        "cross_venue_lag_s": agg.get("lag_s"),
    }


async def fetch_market_context(
    market_id: str,
    token_id: str,
    wallet: str,
    *,
    asof: datetime | None = None,
) -> dict[str, Any]:
    """Aggregate microstructure + social + cross-market features for one
    leader trade. Best-effort: never raises, returns whatever the DB
    layer makes available within the timeout window.
    """
    asof = asof or datetime.now(tz=timezone.utc)

    try:
        ms, social, xm = await asyncio.wait_for(
            asyncio.gather(
                _safe_microstructure(market_id, token_id, asof),
                _safe_social(wallet, asof),
                _safe_cross_market(wallet, asof),
                return_exceptions=False,
            ),
            timeout=_FETCH_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        logger.debug(
            f"market_context: timeout fetching features for "
            f"wallet={wallet} market={market_id}"
        )
        return {}
    except Exception as exc:
        logger.debug(f"market_context: gather failed: {exc}")
        return {}

    return {**ms, **social, **xm}
