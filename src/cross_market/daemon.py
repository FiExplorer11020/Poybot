"""Round 12 — Cross-market daemon (spec § 4 + § 8.B).

Hourly cadence:
  1. WalletResolver.resolve_via_fingerprint over every unresolved
     Polymarket wallet (cheap; the fingerprint match only runs when
     R8/R11 signatures exist).
  2. CrossMarketPositionAggregator.run_once over every confirmed
     operator.

Runs under ``polymarket-crossmarket.service`` (300 MB envelope).
"""
from __future__ import annotations

import asyncio
import signal
from typing import Any

import redis.asyncio as redis_async
from loguru import logger

from src.config import settings
from src.cross_market.kalshi_client import KalshiClient
from src.cross_market.manifold_client import ManifoldClient
from src.cross_market.position_aggregator import CrossMarketPositionAggregator
from src.cross_market.predictit_client import PredictItClient
from src.cross_market.wallet_resolver import WalletResolver
from src.database.connection import close_pool, initialize_pool
from src.logging_setup import configure_logging

# Defensive metric imports.
try:
    from src.monitoring.metrics import (  # type: ignore[attr-defined]
        crossmarket_resolved_operators,
    )
except Exception:  # pragma: no cover
    class _NoOp:
        def labels(self, *_a, **_kw):
            return self

        def set(self, *_a, **_kw):
            return None

    crossmarket_resolved_operators = _NoOp()  # type: ignore[assignment]


class CrossMarketDaemon:
    """Composes the wallet resolver + position aggregator."""

    def __init__(
        self,
        *,
        kalshi: KalshiClient | None,
        manifold: ManifoldClient | None,
        predictit: PredictItClient | None,
        wallet_resolver: WalletResolver | None = None,
        position_aggregator: CrossMarketPositionAggregator | None = None,
        poll_interval_s: int | None = None,
    ) -> None:
        self.kalshi = kalshi
        self.manifold = manifold
        self.predictit = predictit
        self.wallet_resolver = wallet_resolver or WalletResolver()
        self.position_aggregator = (
            position_aggregator
            or CrossMarketPositionAggregator(
                kalshi=kalshi, manifold=manifold, predictit=predictit
            )
        )
        self._poll_interval_s = int(
            poll_interval_s
            if poll_interval_s is not None
            else settings.CROSS_MARKET_POLL_INTERVAL_H * 3600
        )
        self._running = False
        self._stop_event = asyncio.Event()

    async def run_once(self) -> dict[str, Any]:
        """One cycle. Exposed so tests can drive without the loop."""
        # Position aggregator does the bulk of the per-cycle work; the
        # wallet resolver runs are driven separately via the resolver's
        # public methods (manual seeds, fingerprint sweeps) — they are
        # not on the hourly hot path because each fingerprint run needs
        # a candidate-set query that the operator's batch script does.
        summary = await self.position_aggregator.run_once()
        # Surface gauge: distinct confirmed operators seen this cycle.
        try:
            crossmarket_resolved_operators.set(int(summary.get("n_operators", 0)))
        except Exception:  # pragma: no cover
            pass
        return summary

    async def stop(self) -> None:
        self._running = False
        self._stop_event.set()

    async def run_forever(self) -> None:
        self._running = True
        self._stop_event.clear()
        try:
            while self._running and not self._stop_event.is_set():
                try:
                    await self.run_once()
                except Exception as exc:  # pragma: no cover — defensive
                    logger.warning(
                        f"CrossMarketDaemon: run_once raised: {exc}"
                    )
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(),
                        timeout=self._poll_interval_s,
                    )
                except asyncio.TimeoutError:
                    continue
        finally:
            self._running = False


async def main() -> None:  # pragma: no cover — boot path
    level = configure_logging()
    logger.info(f"Starting cross-market daemon (log_level={level})")
    await initialize_pool(
        dsn=settings.DATABASE_URL,
        min_size=settings.DB_POOL_MIN,
        max_size=settings.DB_POOL_MAX,
    )
    _redis_client = redis_async.from_url(settings.REDIS_URL, decode_responses=True)

    import aiohttp
    http_session = aiohttp.ClientSession()

    kalshi = (
        KalshiClient(http_session) if settings.KALSHI_API_KEY else None
    )
    manifold = ManifoldClient(http_session)
    predictit = PredictItClient(http_session)

    daemon = CrossMarketDaemon(
        kalshi=kalshi,
        manifold=manifold,
        predictit=predictit,
    )

    stop_event = asyncio.Event()

    def _handle_signal(*_):
        logger.info("Shutting down cross-market daemon")
        stop_event.set()
        daemon._stop_event.set()  # noqa: SLF001

    loop = asyncio.get_event_loop()
    try:
        loop.add_signal_handler(signal.SIGTERM, _handle_signal)
        loop.add_signal_handler(signal.SIGINT, _handle_signal)
    except (NotImplementedError, RuntimeError):
        pass

    try:
        await daemon.run_forever()
    finally:
        await close_pool()
        try:
            await http_session.close()
        except Exception:
            pass
        try:
            await _redis_client.aclose()
        except Exception:
            pass
        logger.info("Cross-market daemon stopped")


if __name__ == "__main__":  # pragma: no cover
    asyncio.run(main())
