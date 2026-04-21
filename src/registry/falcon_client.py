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
from src.registry.models import FalconLeaderEntry, PnlLeaderEntry, WalletMetrics


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
        self._sem = asyncio.Semaphore(1)
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

        for attempt in range(3):
            async with self._sem:
                await self._throttle()
                try:
                    async with session.post(self._api_url, json=body) as resp:
                        if resp.status in (400, 404, 422):
                            # Non-transient errors — don't retry
                            text = await resp.text()
                            raise FalconAPIError(f"Falcon {resp.status}: {text[:200]}")
                        if resp.status == 429 or resp.status >= 500:
                            wait = 2**attempt
                            logger.warning(f"Falcon HTTP {resp.status}, retrying in {wait}s")
                            last_error = FalconAPIError(f"HTTP {resp.status}")
                            await asyncio.sleep(wait)
                            continue
                        resp.raise_for_status()
                        data = await resp.json()
                        if isinstance(data, list):
                            results: list[dict] = data
                        else:
                            nested = (
                                data.get("data", {}) if isinstance(data.get("data"), dict) else {}
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
                except FalconAPIError:
                    raise
                except Exception as exc:
                    wait = 2**attempt
                    logger.warning(f"Falcon request failed: {exc}, retry in {wait}s")
                    last_error = exc
                    await asyncio.sleep(wait)
                    continue

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
