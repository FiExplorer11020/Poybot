"""Round 12 — public Discord channel listener (spec § 3.3).

We deliberately do NOT pull in ``discord.py`` (heavy dep tree). Instead
the listener polls Discord's REST API directly via aiohttp using the
operator's read-only bot token. The polling cadence is operator-tunable
via ``settings.DISCORD_POLL_INTERVAL_S`` (default 30s).

Per-channel cursor: we keep an in-memory ``last_message_id`` per channel
so each poll fetches only the messages after the cursor. The cursor is
NOT persisted across restarts — the daemon recovers by skipping the
backlog (Discord's free tier holds up to ~100 recent messages per
channel anyway, which is more than enough for the periphery use case).

Public channels only (spec § 9).
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any

from loguru import logger

from src.config import settings
from src.social.x_firehose import SocialPost

# Defensive metric imports.
try:
    from src.monitoring.metrics import (  # type: ignore[attr-defined]
        social_tweets_ingested_total,
    )
except Exception:  # pragma: no cover
    class _NoOp:
        def labels(self, *_a, **_kw):
            return self

        def inc(self, *_a, **_kw):
            return None

    social_tweets_ingested_total = _NoOp()  # type: ignore[assignment]


class DiscordPublicChannelListener:
    """Polls Discord channels via REST. One ``aiohttp.ClientSession``
    is injected by the daemon; tests inject a mock.

    Per-poll fetches /channels/<id>/messages?after=<cursor> and publishes
    each new message to the Redis Stream. Cursors live in memory.
    """

    source: str = "discord"
    _DISCORD_API_BASE: str = "https://discord.com/api/v10"

    def __init__(
        self,
        redis_client: Any,
        http_session: Any,
        *,
        bot_token: str | None = None,
        channels: list[str] | None = None,
        poll_interval_s: float | None = None,
        stream_name: str | None = None,
    ) -> None:
        self._redis = redis_client
        self._http = http_session
        self._token = bot_token if bot_token is not None else settings.DISCORD_BOT_TOKEN_READ
        if channels is not None:
            self._channels = [c for c in channels if c]
        else:
            self._channels = [
                c.strip()
                for c in settings.DISCORD_PUBLIC_CHANNELS.split(",")
                if c.strip()
            ]
        self._poll_interval_s = float(
            poll_interval_s
            if poll_interval_s is not None
            else settings.DISCORD_POLL_INTERVAL_S
        )
        self._stream_name = stream_name or settings.SOCIAL_DISCORD_STREAM_NAME
        self._cursors: dict[str, str] = {}
        self._running = False
        self._stop_event = asyncio.Event()
        self.posts_published: int = 0

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bot {self._token}" if self._token else "",
            "User-Agent": "polymarket-bot-r12 (+social-listener)",
        }

    def _message_to_post(self, channel_id: str, msg: dict[str, Any]) -> SocialPost | None:
        try:
            content = str(msg.get("content") or "")
            if not content:
                return None
            author = msg.get("author") or {}
            handle = str(author.get("username") or "").lower()
            ts_raw = msg.get("timestamp")
            if isinstance(ts_raw, str):
                posted_at = datetime.fromisoformat(ts_raw.replace("Z", "+00:00"))
            else:
                posted_at = datetime.now(tz=timezone.utc)
            return SocialPost(
                source=self.source,
                author_handle=handle,
                text=content,
                posted_at=posted_at,
                market_urls=[],
                raw_payload={
                    "discord_channel_id": channel_id,
                    "discord_message_id": str(msg.get("id") or ""),
                },
            )
        except Exception:
            return None

    async def _fetch_messages(self, channel_id: str) -> list[dict[str, Any]]:
        if self._http is None or not self._token:
            return []
        url = f"{self._DISCORD_API_BASE}/channels/{channel_id}/messages"
        params = {"limit": 50}
        cursor = self._cursors.get(channel_id)
        if cursor:
            params["after"] = cursor
        try:
            async with self._http.get(
                url, headers=self._headers(), params=params
            ) as resp:
                if resp.status == 429:
                    logger.warning(
                        f"DiscordPublicChannelListener: 429 for {channel_id}; "
                        f"pausing {self._poll_interval_s}s"
                    )
                    await asyncio.sleep(self._poll_interval_s)
                    return []
                if resp.status >= 400:
                    body = await resp.text()
                    logger.warning(
                        f"DiscordPublicChannelListener: HTTP {resp.status} "
                        f"({body[:200]})"
                    )
                    return []
                data = await resp.json()
                if not isinstance(data, list):
                    return []
                return data
        except Exception as exc:
            logger.warning(
                f"DiscordPublicChannelListener: fetch failed for "
                f"channel={channel_id}: {exc}"
            )
            return []

    async def _publish(self, post: SocialPost) -> None:
        try:
            await self._redis.xadd(
                self._stream_name,
                post.to_stream_fields(),
                maxlen=settings.SOCIAL_STREAM_MAXLEN,
                approximate=True,
            )
            try:
                social_tweets_ingested_total.labels(source=self.source).inc()
            except Exception:  # pragma: no cover
                pass
            self.posts_published += 1
        except Exception as exc:
            logger.warning(
                f"DiscordPublicChannelListener: redis xadd failed: {exc}"
            )

    async def run_once(self) -> int:
        """Poll every configured channel ONCE. Returns total messages
        published this iteration."""
        if not self._channels:
            return 0
        total = 0
        for channel_id in self._channels:
            messages = await self._fetch_messages(channel_id)
            if not messages:
                continue
            # Discord returns most-recent first; we want oldest-first for
            # cursor advancement.
            ordered = list(reversed(messages))
            latest_id: str | None = None
            for msg in ordered:
                post = self._message_to_post(channel_id, msg)
                if post is not None:
                    await self._publish(post)
                    total += 1
                mid = msg.get("id")
                if mid:
                    latest_id = str(mid)
            if latest_id:
                self._cursors[channel_id] = latest_id
        return total

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
                        f"DiscordPublicChannelListener: run_once "
                        f"raised: {exc}"
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


__all__ = ["DiscordPublicChannelListener"]
