"""Round 12 — Social daemon composing X + Telegram + Discord + NLP.

Topology (each task isolated so one failure doesn't take down the rest):

  ┌─────────────────────────────────────────────────────┐
  │  X firehose ─┐                                       │
  │  Telegram   ─┼──→ Redis Streams (per source)         │
  │  Discord    ─┘                                       │
  │                                                      │
  │  classifier loop:                                    │
  │    XREADGROUP every stream → NLP classify →          │
  │      INSERT INTO social_signals                      │
  └─────────────────────────────────────────────────────┘

Per spec § 3.2 the NLP classifier is rule-based heuristic by default;
operator can deliver a sklearn pipeline at ``NLP_CLASSIFIER_MODEL_PATH``
and the LoadableTweetClassifier will pick it up.

The daemon runs under ``polymarket-social.service`` (300 MB envelope).
"""
from __future__ import annotations

import asyncio
import json
import signal
from datetime import datetime, timezone
from typing import Any

import redis.asyncio as redis_async
from loguru import logger

from src.config import settings
from src.database.connection import close_pool, get_db, initialize_pool
from src.logging_setup import configure_logging
from src.social.discord_listener import DiscordPublicChannelListener
from src.social.nlp_classifier import (
    HeuristicTweetClassifier,
    LoadableTweetClassifier,
    TweetIntentClassifier,
)
from src.social.telegram_listener import TelegramPublicChannelListener
from src.social.x_firehose import (
    SocialPost,
    XFirehoseSubscriber,
    decode_stream_fields,
)


# Defensive metric imports.
try:
    from src.monitoring.metrics import (  # type: ignore[attr-defined]
        social_tweets_classified_total,
        social_unresolved_authors,
    )
except Exception:  # pragma: no cover
    class _NoOp:
        def labels(self, *_a, **_kw):
            return self

        def inc(self, *_a, **_kw):
            return None

        def set(self, *_a, **_kw):
            return None

    social_tweets_classified_total = _NoOp()  # type: ignore[assignment]
    social_unresolved_authors = _NoOp()  # type: ignore[assignment]


CONSUMER_GROUP = "social_classifier"
CONSUMER_NAME = "classifier-1"


class SocialClassifierLoop:
    """Reads from each per-source Redis Stream, runs the NLP classifier,
    writes to ``social_signals``.

    Each source has its own consumer group so a slow downstream consumer
    on one stream doesn't starve the others (R6 daemon-split principle).
    """

    def __init__(
        self,
        redis_client: Any,
        *,
        classifier: TweetIntentClassifier | None = None,
        streams: list[str] | None = None,
    ) -> None:
        self._redis = redis_client
        self._classifier = classifier or self._build_default_classifier()
        self._streams = streams or [
            settings.SOCIAL_X_STREAM_NAME,
            settings.SOCIAL_TELEGRAM_STREAM_NAME,
            settings.SOCIAL_DISCORD_STREAM_NAME,
        ]
        self._running = False
        self._stop_event = asyncio.Event()
        self._last_ids: dict[str, str] = {s: "$" for s in self._streams}
        self.classified_count: int = 0

    @staticmethod
    def _build_default_classifier() -> TweetIntentClassifier:
        path = (settings.NLP_CLASSIFIER_MODEL_PATH or "").strip()
        if path:
            return LoadableTweetClassifier(model_path=path)
        return HeuristicTweetClassifier()

    async def _ensure_groups(self) -> None:
        for stream in self._streams:
            try:
                await self._redis.xgroup_create(
                    stream, CONSUMER_GROUP, id="$", mkstream=True
                )
            except Exception as exc:
                # BUSYGROUP is fine.
                msg = str(exc).lower()
                if "busygroup" not in msg and "already" not in msg:
                    logger.debug(
                        f"SocialClassifierLoop: xgroup_create({stream}) "
                        f"raised {exc!r}"
                    )

    async def _persist(self, post: SocialPost, classification: Any) -> None:
        """Write one classified row to ``social_signals``."""
        try:
            async with get_db() as conn:
                await conn.execute(
                    """
                    INSERT INTO social_signals (
                        source, author_handle, resolved_wallet,
                        posted_at, text, intent, intent_confidence,
                        parsed_market, parsed_direction, raw_payload
                    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10::jsonb)
                    """,
                    post.source,
                    post.author_handle,
                    None,  # resolved_wallet — filled by a separate
                           # backfill job that joins social_signals.author_handle
                           # against cross_market_operators.x_handle.
                    post.posted_at,
                    post.text,
                    classification.intent.value,
                    float(classification.confidence),
                    classification.parsed_market,
                    classification.parsed_direction,
                    json.dumps(post.raw_payload, default=str),
                )
        except Exception as exc:
            logger.warning(
                f"SocialClassifierLoop: persist failed for "
                f"@{post.author_handle}: {exc}"
            )

    async def run_once(self) -> int:
        """One XREADGROUP cycle across all configured streams. Public so
        tests can drive without the loop."""
        processed = 0
        try:
            response = await self._redis.xreadgroup(
                CONSUMER_GROUP,
                CONSUMER_NAME,
                {s: ">" for s in self._streams},
                count=100,
                block=500,  # ms — fast turnaround keeps SIGTERM responsive
            )
        except Exception as exc:
            logger.debug(f"SocialClassifierLoop: xreadgroup failed: {exc}")
            return 0
        if not response:
            return 0
        for stream_key, entries in response:
            stream_name = (
                stream_key.decode() if isinstance(stream_key, bytes) else stream_key
            )
            for entry_id, fields in entries:
                post = decode_stream_fields(fields)
                if post is None:
                    try:
                        await self._redis.xack(stream_name, CONSUMER_GROUP, entry_id)
                    except Exception:  # pragma: no cover
                        pass
                    continue
                try:
                    classification = self._classifier.classify(post.text)
                except Exception as exc:
                    logger.debug(
                        f"SocialClassifierLoop: classify failed: {exc}"
                    )
                    try:
                        await self._redis.xack(stream_name, CONSUMER_GROUP, entry_id)
                    except Exception:  # pragma: no cover
                        pass
                    continue
                await self._persist(post, classification)
                try:
                    social_tweets_classified_total.labels(
                        intent=classification.intent.value
                    ).inc()
                except Exception:  # pragma: no cover
                    pass
                self.classified_count += 1
                processed += 1
                try:
                    await self._redis.xack(stream_name, CONSUMER_GROUP, entry_id)
                except Exception:  # pragma: no cover
                    pass
        return processed

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._stop_event.clear()
        await self._ensure_groups()

    async def stop(self) -> None:
        self._running = False
        self._stop_event.set()

    async def run_forever(self) -> None:
        await self.start()
        try:
            while self._running and not self._stop_event.is_set():
                try:
                    await self.run_once()
                except Exception as exc:  # pragma: no cover
                    logger.warning(
                        f"SocialClassifierLoop: run_once raised: {exc}"
                    )
        finally:
            self._running = False


class SocialDaemon:
    """Composite daemon: spawns X + Telegram + Discord subscribers +
    classifier loop. Each task is shielded — failure of one source
    doesn't take down the others (spec § 3.3).
    """

    def __init__(
        self,
        redis_client: Any,
        http_session: Any | None = None,
        *,
        x_subscriber: Any | None = None,
        telegram_listener: Any | None = None,
        discord_listener: Any | None = None,
        classifier_loop: SocialClassifierLoop | None = None,
    ) -> None:
        self._redis = redis_client
        self._http = http_session
        self.x_subscriber = x_subscriber
        self.telegram_listener = telegram_listener
        self.discord_listener = discord_listener
        self.classifier_loop = classifier_loop or SocialClassifierLoop(redis_client)
        self._tasks: list[asyncio.Task] = []
        self._stop_event = asyncio.Event()

    def _maybe_build_x(self) -> Any | None:
        if self.x_subscriber is not None:
            return self.x_subscriber
        if self._http is None or not settings.X_API_KEY:
            return None
        return XFirehoseSubscriber(self._redis, self._http)

    def _maybe_build_telegram(self) -> Any | None:
        if self.telegram_listener is not None:
            return self.telegram_listener
        if not settings.TELEGRAM_BOT_TOKEN_READ or not settings.TELEGRAM_PUBLIC_CHANNELS:
            return None
        return TelegramPublicChannelListener(self._redis)

    def _maybe_build_discord(self) -> Any | None:
        if self.discord_listener is not None:
            return self.discord_listener
        if (
            self._http is None
            or not settings.DISCORD_BOT_TOKEN_READ
            or not settings.DISCORD_PUBLIC_CHANNELS
        ):
            return None
        return DiscordPublicChannelListener(self._redis, self._http)

    async def start(self) -> None:
        self._stop_event.clear()
        # Compose the per-source workers — None'd ones simply don't spawn.
        x = self._maybe_build_x()
        if x is not None:
            self.x_subscriber = x
            self._tasks.append(
                asyncio.create_task(self._run_shielded(x.run_forever, "x"))
            )
        tg = self._maybe_build_telegram()
        if tg is not None:
            self.telegram_listener = tg
            await tg.start()
        dis = self._maybe_build_discord()
        if dis is not None:
            self.discord_listener = dis
            self._tasks.append(
                asyncio.create_task(self._run_shielded(dis.run_forever, "discord"))
            )
        self._tasks.append(
            asyncio.create_task(
                self._run_shielded(
                    self.classifier_loop.run_forever, "classifier"
                )
            )
        )

    async def _run_shielded(self, coro_fn, label: str) -> None:
        try:
            await coro_fn()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover — defensive
            logger.warning(
                f"SocialDaemon[{label}]: task crashed: {exc}; "
                f"daemon will continue running other sources."
            )

    async def stop(self) -> None:
        self._stop_event.set()
        # Stop the sources cleanly first.
        for src in (self.x_subscriber, self.discord_listener):
            if src is not None:
                try:
                    await src.stop()
                except Exception:  # pragma: no cover
                    pass
        if self.telegram_listener is not None:
            try:
                await self.telegram_listener.stop()
            except Exception:  # pragma: no cover
                pass
        await self.classifier_loop.stop()
        # Cancel any still-running tasks.
        for t in self._tasks:
            if not t.done():
                t.cancel()
        # Wait briefly so cleanup runs.
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    async def run_forever(self) -> None:
        await self.start()
        try:
            await self._stop_event.wait()
        finally:
            await self.stop()


# ---------------------------------------------------------------------------
# systemd entrypoint
# ---------------------------------------------------------------------------


async def main() -> None:  # pragma: no cover — boot path
    level = configure_logging()
    logger.info(f"Starting social daemon (log_level={level})")
    await initialize_pool(
        dsn=settings.DATABASE_URL,
        min_size=settings.DB_POOL_MIN,
        max_size=settings.DB_POOL_MAX,
    )
    redis_client = redis_async.from_url(settings.REDIS_URL, decode_responses=True)

    import aiohttp
    http_session = aiohttp.ClientSession()

    daemon = SocialDaemon(redis_client=redis_client, http_session=http_session)
    stop_event = asyncio.Event()

    def _handle_signal(*_):
        logger.info("Shutting down social daemon")
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
            await redis_client.aclose()
        except Exception:
            pass
        logger.info("Social daemon stopped")


if __name__ == "__main__":  # pragma: no cover
    asyncio.run(main())
