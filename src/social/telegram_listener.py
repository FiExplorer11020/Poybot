"""Round 12 — public Telegram channel listener (spec § 3.3).

Public-channel only. Read-only. Uses the ``python-telegram-bot``
library if it's installed; otherwise the listener gracefully no-ops so
the daemon doesn't crash in environments where the dep isn't wired
(notably tests and bootstrap CI).

We deliberately keep this paper-thin so the X-firehose contract owns
the schema. Telegram + Discord just produce :class:`SocialPost` and
publish on their own stream — every downstream consumer sees the same
shape.

Why no DMs / private groups: spec § 9 — ethical + legal floor.
"""
from __future__ import annotations

import asyncio
import json
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


# Lazy import of python-telegram-bot to keep tests deterministic when
# the dep isn't installed. The listener degrades to a graceful no-op.
def _maybe_import_ptb():  # pragma: no cover — exercised by integration
    try:
        from telegram.ext import (  # type: ignore[import-not-found]
            Application,
            MessageHandler,
            filters,
        )
        return Application, MessageHandler, filters
    except Exception:
        return None


class TelegramPublicChannelListener:
    """Subscribes to public Telegram channels and publishes each message
    onto the social:telegram:stream Redis Stream.

    Operator contract:
      * ``settings.TELEGRAM_BOT_TOKEN_READ`` — read-only bot token
        (bot must be added to public channels by their owners).
      * ``settings.TELEGRAM_PUBLIC_CHANNELS`` — comma-separated channel
        ids / usernames.

    Tests inject a mock python-telegram-bot Application (``application``)
    so we never touch the network.
    """

    source: str = "telegram"

    def __init__(
        self,
        redis_client: Any,
        *,
        bot_token: str | None = None,
        channels: list[str] | None = None,
        application: Any | None = None,  # injectable for tests
        stream_name: str | None = None,
    ) -> None:
        self._redis = redis_client
        self._token = bot_token if bot_token is not None else settings.TELEGRAM_BOT_TOKEN_READ
        if channels is not None:
            self._channels = [c for c in channels if c]
        else:
            self._channels = [
                c.strip()
                for c in settings.TELEGRAM_PUBLIC_CHANNELS.split(",")
                if c.strip()
            ]
        self._application = application
        self._stream_name = stream_name or settings.SOCIAL_TELEGRAM_STREAM_NAME
        self._running = False
        self.posts_published: int = 0

    def _build_application(self) -> Any:
        """Bind the operator's python-telegram-bot Application if the
        library is installed; return None otherwise. Tests pass an
        application directly into the constructor and skip this path."""
        ptb = _maybe_import_ptb()
        if ptb is None:
            logger.info(
                "TelegramPublicChannelListener: python-telegram-bot not "
                "installed — listener will no-op. Install + set "
                "TELEGRAM_BOT_TOKEN_READ to enable."
            )
            return None
        if not self._token:
            return None
        Application, MessageHandler, filters = ptb
        try:
            app = Application.builder().token(self._token).build()
            handler = MessageHandler(filters.ALL, self._on_message)
            app.add_handler(handler)
            return app
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning(
                f"TelegramPublicChannelListener: build failed: {exc}"
            )
            return None

    async def _on_message(self, update: Any, _context: Any) -> None:
        """python-telegram-bot handler. Extracts the channel post +
        publishes onto the Redis Stream."""
        try:
            post = self._update_to_post(update)
            if post is None:
                return
            # Filter to allowed channels.
            chan = post.author_handle
            if self._channels and chan not in self._channels:
                return
            await self._publish(post)
        except Exception as exc:  # pragma: no cover — defensive
            logger.debug(f"TelegramPublicChannelListener: handler error: {exc}")

    def _update_to_post(self, update: Any) -> SocialPost | None:
        try:
            msg = (
                getattr(update, "channel_post", None)
                or getattr(update, "message", None)
            )
            if msg is None:
                return None
            text = getattr(msg, "text", None) or ""
            if not text:
                return None
            chat = getattr(msg, "chat", None)
            chan = (
                getattr(chat, "username", None)
                or str(getattr(chat, "id", "") or "")
            )
            posted_at_raw = getattr(msg, "date", None)
            if isinstance(posted_at_raw, datetime):
                posted_at = posted_at_raw
            else:
                posted_at = datetime.now(tz=timezone.utc)
            return SocialPost(
                source=self.source,
                author_handle=str(chan).lstrip("@").lower(),
                text=str(text),
                posted_at=posted_at,
                market_urls=[],
                raw_payload={"telegram_message_id": getattr(msg, "message_id", None)},
            )
        except Exception:
            return None

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
                f"TelegramPublicChannelListener: redis xadd failed for "
                f"@{post.author_handle}: {exc}"
            )

    async def process_update(self, update: Any) -> int:
        """Public hook so tests can drive the listener with synthetic
        Update objects without spinning up python-telegram-bot.

        Returns 1 if the update was published, 0 otherwise.
        """
        post = self._update_to_post(update)
        if post is None:
            return 0
        chan = post.author_handle
        if self._channels and chan not in self._channels:
            return 0
        await self._publish(post)
        return 1

    async def start(self) -> None:
        """Boot the application loop. If the dep isn't available or no
        token is set, this stays idle — the daemon survives."""
        if self._application is None:
            self._application = self._build_application()
        if self._application is None:
            return
        try:
            await self._application.initialize()
            await self._application.start()
            # We don't run a polling loop — the application's polling
            # tasks are managed internally. The daemon's run_forever
            # awaits a stop event independently.
            self._running = True
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning(
                f"TelegramPublicChannelListener: start failed: {exc}"
            )
            self._running = False

    async def stop(self) -> None:
        if self._application is None or not self._running:
            return
        try:
            await self._application.stop()
            await self._application.shutdown()
        except Exception:  # pragma: no cover — defensive
            pass
        self._running = False


__all__ = ["TelegramPublicChannelListener"]
