"""SocialDaemon + SocialClassifierLoop composition tests.

Coverage:
  * SocialClassifierLoop reads from streams, classifies, persists.
  * Graceful stop cancels in-flight tasks without raising.
  * Empty stream → run_once returns 0.
  * Source-isolated failure does NOT take down the classifier loop
    (None'd sources are simply skipped).
"""
from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import fakeredis.aioredis as fakeredis_async
import pytest

from src.social.daemon import SocialClassifierLoop, SocialDaemon
from src.social.nlp_classifier import HeuristicTweetClassifier
from src.social.x_firehose import SocialPost


@pytest.fixture
async def redis_client():
    client = fakeredis_async.FakeRedis(decode_responses=True)
    yield client
    await client.aclose()


def _mock_get_db():
    """Patcher for get_db. Returns the mock conn + the patch context."""
    conn = AsyncMock()
    conn.execute = AsyncMock()

    @asynccontextmanager
    async def _ctx():
        yield conn

    return _ctx, conn


class TestClassifierLoopBasic:
    @pytest.mark.asyncio
    async def test_empty_streams_yield_zero(self, redis_client):
        loop = SocialClassifierLoop(
            redis_client,
            classifier=HeuristicTweetClassifier(),
            streams=["test:x:stream"],
        )
        await loop._ensure_groups()  # type: ignore[attr-defined]
        n = await loop.run_once()
        assert n == 0

    @pytest.mark.asyncio
    async def test_classifies_and_persists(self, redis_client):
        loop = SocialClassifierLoop(
            redis_client,
            classifier=HeuristicTweetClassifier(),
            streams=["test:x:stream-2"],
        )
        await loop.start()
        # Publish two posts to the stream BEFORE the consumer group is
        # listening from 'now', so we'd miss them; instead emit AFTER
        # ensuring the group exists.
        post = SocialPost(
            source="x",
            author_handle="alice",
            text="just entered YES at 0.42",
            posted_at=datetime(2026, 5, 12, 10, 0, 0, tzinfo=timezone.utc),
            market_urls=[],
            raw_payload={},
        )
        await redis_client.xadd("test:x:stream-2", post.to_stream_fields())
        ctx, conn = _mock_get_db()
        with patch("src.social.daemon.get_db", side_effect=ctx):
            n = await loop.run_once()
        assert n == 1
        # Verify the persist was called with the expected intent.
        assert conn.execute.await_count == 1
        args = conn.execute.await_args.args
        # args[6] is intent — index it in the parameter order.
        # The insert order is:
        #   source, author_handle, resolved_wallet, posted_at, text,
        #   intent, intent_confidence, parsed_market, parsed_direction,
        #   raw_payload
        intent_value = args[6]
        assert intent_value == "entry_signal"


class TestSocialDaemonComposite:
    @pytest.mark.asyncio
    async def test_start_stop_clean(self, redis_client):
        # Daemon with NO subscribers (no X key, no Telegram, no Discord)
        # — only the classifier loop runs.
        daemon = SocialDaemon(
            redis_client=redis_client,
            http_session=None,
            classifier_loop=SocialClassifierLoop(
                redis_client,
                streams=["test:x:stream-3"],
                classifier=HeuristicTweetClassifier(),
            ),
        )
        await daemon.start()
        # Composite has one classifier task; no X / TG / Discord.
        assert len(daemon._tasks) == 1
        await daemon.stop()
        # All tasks cancelled cleanly.
        assert all(t.done() for t in daemon._tasks) or daemon._tasks == []

    @pytest.mark.asyncio
    async def test_no_subscribers_when_no_keys(self, redis_client):
        # Without X_API_KEY / Telegram / Discord env, no subscribers
        # should spawn — daemon stays online with classifier only.
        daemon = SocialDaemon(
            redis_client=redis_client,
            http_session=None,
        )
        # _maybe_build_* all return None when keys are empty.
        assert daemon._maybe_build_x() is None
        assert daemon._maybe_build_discord() is None
