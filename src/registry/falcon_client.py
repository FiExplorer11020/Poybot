"""
Async client for the Falcon API (Heisenberg Narrative platform).

Handles rate limiting, retries with exponential backoff, and Redis caching.
"""

import asyncio
import hashlib
import json
import time
from typing import Any

import aiohttp
from loguru import logger

from src.config import settings
from src.registry.models import FalconLeaderEntry, MarketInsights, PnlLeaderEntry, WalletMetrics

# Phase 1 Task F (audit HP-2): Prometheus instrumentation. The metrics module
# is the single source of truth for metric names + labels (see
# src/monitoring/metrics.py). If it imports cleanly we use it; if not (older
# checkout, prometheus_client missing in some test env) we fall back to no-op
# stubs so production code paths don't break. Same pattern as Phase 1 Task O.
try:
    from src.monitoring.metrics import (
        falcon_call_latency_seconds,
        falcon_calls_total,
        falcon_concurrency,
    )
except Exception:  # pragma: no cover — defensive fallback
    class _NoOpLabel:
        def labels(self, *_args, **_kwargs):
            return self

        def inc(self, *_args, **_kwargs):
            return None

        def dec(self, *_args, **_kwargs):
            return None

        def observe(self, *_args, **_kwargs):
            return None

    falcon_call_latency_seconds = _NoOpLabel()  # type: ignore[assignment]
    falcon_calls_total = _NoOpLabel()  # type: ignore[assignment]
    falcon_concurrency = _NoOpLabel()  # type: ignore[assignment]


class FalconAPIError(Exception):
    pass


class FalconClient:
    def __init__(
        self,
        api_key: str = "",
        api_url: str = "",
        redis_client: Any = None,
        cache_ttl_s: int = 172800,
        max_rpm: int | None = None,
    ):
        self._api_key = api_key or settings.FALCON_API_KEY
        self._api_url = api_url or settings.FALCON_API_URL
        self._redis = redis_client
        self._cache_ttl = cache_ttl_s
        self._max_rpm = int(max_rpm or settings.FALCON_MAX_REQUESTS_PER_MINUTE)
        # HP-2 fix: 1→8, real cap is the 60 RPM rate limiter.
        # The previous Semaphore(1) serialised every Falcon HTTP call across
        # the whole process — no matter how much concurrency the caller
        # wanted. Bumping to FALCON_MAX_CONCURRENCY (default 8) lets parallel
        # callers (sync_markets, enrich_leaders, _backfill_wallet_trades)
        # actually overlap; the 60 RPM token bucket in `_throttle()` below
        # remains the true sustained-throughput bound.
        self._sem = asyncio.Semaphore(int(settings.FALCON_MAX_CONCURRENCY))
        self._rate_lock = asyncio.Lock()
        self._last_request_at = 0.0
        self._session: aiohttp.ClientSession | None = None

    def _session_or_new(self) -> aiohttp.ClientSession:
        if not self._api_key:
            raise FalconAPIError("FALCON_API_KEY is not configured")
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                headers={"Authorization": f"Bearer {self._api_key}"}
            )
        return self._session

    def _cache_key(self, agent_id: int, params: dict, limit: int, offset: int) -> str:
        h = hashlib.md5(
            json.dumps(
                {"params": params, "limit": limit, "offset": offset}, sort_keys=True
            ).encode()
        ).hexdigest()
        return f"falcon:{agent_id}:{h}"

    async def _throttle(self) -> None:
        if self._max_rpm <= 0:
            return
        min_interval_s = 60.0 / float(self._max_rpm)
        async with self._rate_lock:
            now = time.monotonic()
            wait_for = self._last_request_at + min_interval_s - now
            if wait_for > 0:
                await asyncio.sleep(wait_for)
                now = time.monotonic()
            self._last_request_at = now

    async def query(
        self, agent_id: int, params: dict, limit: int = 100, offset: int = 0
    ) -> list[dict]:
        cache_key = self._cache_key(agent_id, params, limit, offset)

        # Cache check
        if self._redis is not None:
            try:
                cached = await self._redis.get(cache_key)
                if cached:
                    return json.loads(cached)
            except Exception as exc:
                logger.warning(f"Redis cache read failed: {exc}")

        # API call with retry
        requested_limit = max(1, int(limit))
        api_limit = min(200, max(5, requested_limit))
        body = {
            "agent_id": agent_id,
            "params": params,
            "pagination": {
                "limit": api_limit,
                "offset": max(0, int(offset)),
            },
            "formatter_config": {"format_type": "raw"},
        }
        session = self._session_or_new()
        last_error: Exception | None = None
        agent_label = str(agent_id)

        for attempt in range(3):
            # Phase 1 Task F: track in-flight Falcon concurrency. The gauge
            # is inc'd at semaphore-acquire and dec'd at release so /metrics
            # reflects the true real-time fan-out under the new
            # Semaphore(8). Wrapped in a try/finally inside the `async with`
            # so any exception path (HTTP error, parse error, raise) still
            # decrements.
            async with self._sem:
                falcon_concurrency.inc()
                attempt_start = time.monotonic()
                attempt_result: str | None = None  # ok|empty|rate_limited|error|timeout
                try:
                    await self._throttle()
                    try:
                        async with session.post(self._api_url, json=body) as resp:
                            if resp.status in (400, 404, 422):
                                # Non-transient errors — don't retry
                                text = await resp.text()
                                attempt_result = "error"
                                raise FalconAPIError(f"Falcon {resp.status}: {text[:200]}")
                            if resp.status == 429 or resp.status >= 500:
                                wait = 2**attempt
                                logger.warning(
                                    f"Falcon HTTP {resp.status}, retrying in {wait}s"
                                )
                                last_error = FalconAPIError(f"HTTP {resp.status}")
                                attempt_result = (
                                    "rate_limited" if resp.status == 429 else "error"
                                )
                                await asyncio.sleep(wait)
                                continue
                            resp.raise_for_status()
                            data = await resp.json()
                            if isinstance(data, list):
                                results: list[dict] = data
                            else:
                                nested = (
                                    data.get("data", {})
                                    if isinstance(data.get("data"), dict)
                                    else {}
                                )
                                results = []
                                for candidate in (
                                    nested.get("results"),
                                    nested.get("groups"),
                                    data.get("results"),
                                    data.get("groups"),
                                ):
                                    if isinstance(candidate, list):
                                        results = candidate
                                        break
                            attempt_result = "ok" if results else "empty"
                    except FalconAPIError:
                        if attempt_result is None:
                            attempt_result = "error"
                        raise
                    except asyncio.TimeoutError as exc:
                        wait = 2**attempt
                        logger.warning(
                            f"Falcon request timed out: {exc}, retry in {wait}s"
                        )
                        last_error = exc
                        attempt_result = "timeout"
                        await asyncio.sleep(wait)
                        continue
                    except Exception as exc:
                        wait = 2**attempt
                        logger.warning(f"Falcon request failed: {exc}, retry in {wait}s")
                        last_error = exc
                        attempt_result = "error"
                        await asyncio.sleep(wait)
                        continue
                finally:
                    falcon_call_latency_seconds.labels(agent=agent_label).observe(
                        time.monotonic() - attempt_start
                    )
                    if attempt_result is not None:
                        falcon_calls_total.labels(
                            agent=agent_label, result=attempt_result
                        ).inc()
                    falcon_concurrency.dec()

            # Cache successful result
            if self._redis is not None:
                try:
                    await self._redis.set(
                        cache_key, json.dumps(results[:requested_limit]), ex=self._cache_ttl
                    )
                except Exception as exc:
                    logger.warning(f"Redis cache write failed: {exc}")

            return results[:requested_limit]

        raise FalconAPIError(f"All retries failed for agent {agent_id}: {last_error}")

    async def get_leaderboard(self, limit: int = 200) -> list[FalconLeaderEntry]:
        rows = await self.query(
            584,
            {
                "min_win_rate_15d": "0.45",
                "max_win_rate_15d": "0.92",
                "min_roi_15d": "0",
                "min_pnl_15d": "0",
                "min_total_trades_15d": "30",
                "max_total_trades_15d": "5000",
                "sort_by": "roi",
            },
            limit=limit,
        )
        entries = []
        for r in rows:
            try:
                entries.append(FalconLeaderEntry.model_validate(r))
            except Exception:
                pass
        return entries

    async def get_wallet360(self, wallet: str, window_days: str = "15") -> WalletMetrics | None:
        rows = await self.query(
            581,
            {"proxy_wallet": wallet, "window_days": window_days},
            limit=1,
        )
        if not rows:
            return None
        try:
            return WalletMetrics.model_validate(rows[0])
        except Exception:
            return None

    async def get_market_insights(self, condition_id: str) -> MarketInsights | None:
        """Fetch normalized 0–1 liquidity score from agent 575 (Market Insights).

        This is the documented source for `markets.liquidity_score`
        (master CLAUDE.md §6, `src/profiler/CLAUDE.md:172`,
        `src/profiler/error_model.py:83`). Returns None when agent 575
        has no data for the market — the caller (`sync_markets`) should
        fall back to agent 574's `liquidity` field with a provenance
        tag of `falcon_574` so the source mismatch is auditable in the
        DB.

        The returned `liquidity_score` is clamped to `[0.0, 1.0]`:
        agent 575 may surface raw USD-denominated depth on some markets
        rather than the documented normalized signal, and the
        downstream feature is expected to live in `[0, 1]`
        (`error_model._build_features` slot [4]; `confidence_engine`
        gates compare against `0.35` thresholds).
        """
        try:
            rows = await self.query(575, {"condition_id": condition_id}, limit=1)
            if not rows:
                # Some Falcon agents key on slug rather than condition_id;
                # mirror the agent-574 fallback in sync_markets.
                rows = await self.query(575, {"market_slug": condition_id}, limit=1)
        except FalconAPIError as exc:
            logger.debug(f"Market Insights (agent 575) unavailable for {condition_id}: {exc}")
            return None
        if not rows:
            return None
        try:
            insights = MarketInsights.model_validate(rows[0])
        except Exception as exc:
            logger.debug(f"Market Insights parse failed for {condition_id}: {exc}")
            return None
        # Clamp to [0,1] — see docstring; raw USD depth would explode
        # `_build_features` slot [4] otherwise.
        raw = float(insights.liquidity_score or 0.0)
        if raw != raw or raw < 0.0:  # NaN check or negative
            raw = 0.0
        if raw > 1.0:
            # Heuristic: agent 575 sometimes returns depth in USD; squash
            # via tanh so a $100k market lands near 1.0 without overflow.
            import math

            raw = math.tanh(raw / 100_000.0)
        insights.liquidity_score = max(0.0, min(1.0, raw))
        return insights

    async def get_pnl_leaderboard(self, limit: int = 200) -> list[PnlLeaderEntry]:
        rows = await self.query(
            579,
            {"wallet_address": "ALL", "leaderboard_period": "7d"},
            limit=limit,
        )
        entries = []
        for r in rows:
            try:
                entries.append(PnlLeaderEntry.model_validate(r))
            except Exception:
                pass
        return entries

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()
