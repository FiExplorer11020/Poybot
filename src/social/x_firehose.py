"""Round 12 — X (Twitter) v2 filtered-stream subscriber (spec § 3.1).

The production path consumes X's filtered stream API; tests + smoke
runs use :class:`FixtureXSubscriber` which replays a JSON fixture
file. Both subclasses share :class:`_BaseXSubscriber`'s Redis Stream
publish path so the classifier loop in the daemon doesn't care which
source it's reading from.

What this module owns:

  * Filter rule management — POST `from:<handle>` and URL filters to
    X's rules endpoint on startup, refresh every
    ``X_API_RULES_REFRESH_INTERVAL_S``.
  * The streaming HTTP read loop with adaptive 429 handling (graceful
    pause; never crash).
  * Per-tweet schema normalisation into the daemon's row contract.
  * Redis Stream publish on ``social:x:stream``.

What it does NOT own:

  * NLP classification — runs in the daemon, downstream of the stream.
  * Wallet resolution — separate ``wallet_resolver`` step.
  * Any DB writes — purely an ingest-into-redis surface.

External dependency: the X API subscription itself is operator-
deliverable per spec § 7 (basic tier ~$100/mo).
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, AsyncIterator

from loguru import logger

from src.config import settings


# Defensive metric imports.
try:
    from src.monitoring.metrics import (  # type: ignore[attr-defined]
        social_tweets_ingested_total,
        social_x_quota_remaining,
    )
except Exception:  # pragma: no cover
    class _NoOp:
        def labels(self, *_a, **_kw):
            return self

        def inc(self, *_a, **_kw):
            return None

        def set(self, *_a, **_kw):
            return None

    social_tweets_ingested_total = _NoOp()  # type: ignore[assignment]
    social_x_quota_remaining = _NoOp()  # type: ignore[assignment]


# Stream payload schema — every X-source row matches this exactly so
# the classifier loop is source-agnostic.
@dataclass
class SocialPost:
    """One normalised post coming out of an X / Telegram / Discord source."""

    source: str  # x|telegram|discord
    author_handle: str
    text: str
    posted_at: datetime
    market_urls: list[str] = field(default_factory=list)
    raw_payload: dict[str, Any] = field(default_factory=dict)

    def to_stream_fields(self) -> dict[str, str]:
        """Redis Stream needs a flat str→str map. We pack everything
        into a single 'data' JSON blob — symmetric with the
        clob_book_events stream pattern (R11 daemon)."""
        return {
            "data": json.dumps(
                {
                    "source": self.source,
                    "author_handle": self.author_handle,
                    "text": self.text,
                    "posted_at": self.posted_at.isoformat(),
                    "market_urls": list(self.market_urls),
                    "raw_payload": self.raw_payload,
                },
                default=str,
            )
        }


def decode_stream_fields(fields: dict[str, Any]) -> SocialPost | None:
    """Reverse of :meth:`SocialPost.to_stream_fields`. Returns None on
    malformed payload — the consumer logs + skips."""
    raw = fields.get("data") if isinstance(fields, dict) else None
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except Exception:
        return None
    try:
        posted_at_raw = payload.get("posted_at")
        if isinstance(posted_at_raw, str):
            posted_at = datetime.fromisoformat(posted_at_raw.replace("Z", "+00:00"))
        elif isinstance(posted_at_raw, datetime):
            posted_at = posted_at_raw
        else:
            posted_at = datetime.now(tz=timezone.utc)
        return SocialPost(
            source=str(payload.get("source", "")),
            author_handle=str(payload.get("author_handle", "")).lower(),
            text=str(payload.get("text", "")),
            posted_at=posted_at,
            market_urls=list(payload.get("market_urls") or []),
            raw_payload=payload.get("raw_payload") or {},
        )
    except Exception:
        return None


class _BaseXSubscriber:
    """Shared plumbing: rule serialisation, Redis publish, run loop hooks.

    Subclasses override :meth:`_iterate_tweets` with the actual data
    source. The base never knows about the network.
    """

    source: str = "x"

    def __init__(
        self,
        redis_client: Any,
        *,
        stream_name: str | None = None,
        rate_limit_pause_s: float = 60.0,
    ) -> None:
        self._redis = redis_client
        self._stream_name = stream_name or settings.SOCIAL_X_STREAM_NAME
        self._rate_limit_pause_s = float(rate_limit_pause_s)
        self._running = False
        self._stop_event = asyncio.Event()
        self.posts_published: int = 0

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
                f"XFirehoseSubscriber: redis xadd failed for "
                f"@{post.author_handle}: {exc}"
            )

    async def _iterate_tweets(self) -> AsyncIterator[SocialPost]:
        """Implemented by subclasses; yields :class:`SocialPost` objects."""
        if False:  # pragma: no cover — pure-type sentinel
            yield  # type: ignore[unreachable]
        raise NotImplementedError

    async def run_once(self) -> int:
        """Pull-then-publish for ONE batch of posts. Public so tests can
        drive the subscriber without spinning up the full loop.

        Returns the number of posts published this iteration.
        """
        n = 0
        async for post in self._iterate_tweets():
            await self._publish(post)
            n += 1
        return n

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
                except Exception as exc:
                    logger.warning(
                        f"XFirehoseSubscriber: run_once raised "
                        f"({exc!r}); pausing {self._rate_limit_pause_s}s"
                    )
                    await asyncio.sleep(self._rate_limit_pause_s)
                    continue
                # Tight loop is fine for fixture mode (returns []); the
                # production subclass paces itself on the streaming
                # connection's read.
                await asyncio.sleep(0)
        finally:
            self._running = False


class XFirehoseSubscriber(_BaseXSubscriber):
    """The production-path subscriber.

    Wires the X v2 filtered stream. Rule management:
      * On startup, build a rules payload from
        ``settings.X_TRACKED_HANDLES`` + market-URL filters and POST it.
      * Refresh every ``settings.X_API_RULES_REFRESH_INTERVAL_S`` seconds.

    Rate-limit handling (spec § 3.1):
      * On 429, pause for ``self._rate_limit_pause_s`` and re-issue.
      * Set ``social_x_quota_remaining`` from response headers.
      * Never crash — the daemon stays up.

    Injection: ``http_session`` is an aiohttp.ClientSession provided
    by the daemon. Tests pass a mocked session.
    """

    def __init__(
        self,
        redis_client: Any,
        http_session: Any,
        *,
        api_key: str | None = None,
        api_base_url: str | None = None,
        tracked_handles: list[str] | None = None,
        stream_name: str | None = None,
        rate_limit_pause_s: float = 60.0,
    ) -> None:
        super().__init__(
            redis_client,
            stream_name=stream_name,
            rate_limit_pause_s=rate_limit_pause_s,
        )
        self._http = http_session
        self._api_key = api_key if api_key is not None else settings.X_API_KEY
        self._base_url = (api_base_url or settings.X_API_BASE_URL).rstrip("/")
        if tracked_handles is not None:
            self._handles = [h.lower().lstrip("@") for h in tracked_handles if h]
        else:
            self._handles = [
                h.strip().lower().lstrip("@")
                for h in settings.X_TRACKED_HANDLES.split(",")
                if h.strip()
            ]
        self._rules_synced_at: float = 0.0

    def _rule_payload(self) -> dict[str, Any]:
        """Compose the X v2 rules POST body from tracked handles +
        market-URL filters. We keep each rule under X's 512-char tag
        limit by chunking handles into ORs.
        """
        rules: list[dict[str, str]] = []
        # Handles → batched OR.
        if self._handles:
            # Chunk so no single rule string exceeds 480 chars (headroom
            # under X's 512-char per-rule limit).
            chunk: list[str] = []
            current_len = 0
            for h in self._handles:
                tok = f"from:{h}"
                if current_len + len(tok) + 4 > 480 and chunk:
                    rules.append({"value": " OR ".join(chunk), "tag": "leader_handles"})
                    chunk = []
                    current_len = 0
                chunk.append(tok)
                current_len += len(tok) + 4
            if chunk:
                rules.append({"value": " OR ".join(chunk), "tag": "leader_handles"})
        # URL filter for polymarket market URLs.
        rules.append(
            {"value": "url:polymarket.com/market OR url:polymarket.com/event",
             "tag": "polymarket_urls"}
        )
        return {"add": rules}

    async def sync_rules(self) -> bool:
        """POST the filter rules to X. Returns True on success.

        Idempotent in spec terms: X dedupes by tag. We expose this as
        public so the test suite can drive it without `run_once`.
        """
        if self._http is None or not self._api_key:
            return False
        payload = self._rule_payload()
        url = f"{self._base_url}/tweets/search/stream/rules"
        headers = {"Authorization": f"Bearer {self._api_key}"}
        try:
            async with self._http.post(url, json=payload, headers=headers) as resp:
                if resp.status >= 400:
                    body = await resp.text()
                    logger.warning(
                        f"XFirehoseSubscriber.sync_rules: HTTP {resp.status} "
                        f"({body[:200]})"
                    )
                    return False
            self._rules_synced_at = time.time()
            return True
        except Exception as exc:
            logger.warning(f"XFirehoseSubscriber.sync_rules failed: {exc}")
            return False

    async def _iterate_tweets(self) -> AsyncIterator[SocialPost]:
        """Read one batch from the streaming endpoint.

        We don't try to be smart: each iteration opens a streaming GET,
        reads up to a per-batch tweet count, then closes the response.
        The outer run_forever loop reconnects continuously. This keeps
        the test surface small (one request → list of payloads).
        """
        if self._http is None or not self._api_key:
            logger.debug(
                "XFirehoseSubscriber: no http_session or API key; "
                "returning empty (operator-deliverable subscription)."
            )
            return
        # Rules refresh.
        now = time.time()
        if now - self._rules_synced_at > settings.X_API_RULES_REFRESH_INTERVAL_S:
            await self.sync_rules()
        url = (
            f"{self._base_url}/tweets/search/stream"
            f"?tweet.fields=created_at,author_id,entities"
            f"&expansions=author_id"
            f"&user.fields=username"
        )
        headers = {"Authorization": f"Bearer {self._api_key}"}
        try:
            async with self._http.get(url, headers=headers) as resp:
                # Quota header — X uses x-rate-limit-remaining for the
                # streaming endpoint; we surface whatever the operator
                # gave us.
                remaining = resp.headers.get("x-rate-limit-remaining")
                if remaining is not None:
                    try:
                        social_x_quota_remaining.set(int(remaining))
                    except Exception:  # pragma: no cover
                        pass
                if resp.status == 429:
                    logger.warning(
                        "XFirehoseSubscriber: 429 Too Many Requests — "
                        f"pausing {self._rate_limit_pause_s}s"
                    )
                    await asyncio.sleep(self._rate_limit_pause_s)
                    return
                if resp.status >= 400:
                    body = await resp.text()
                    logger.warning(
                        f"XFirehoseSubscriber: HTTP {resp.status} ({body[:200]})"
                    )
                    return
                # Read line-delimited JSON. We bound the batch size at
                # 500 lines so a single bad burst doesn't starve other
                # daemon work.
                lines_read = 0
                async for raw_line in resp.content:
                    if not raw_line:
                        continue
                    line = raw_line.decode("utf-8", "replace").strip()
                    if not line:
                        continue
                    try:
                        payload = json.loads(line)
                    except Exception:
                        continue
                    post = self._payload_to_post(payload)
                    if post is not None:
                        yield post
                    lines_read += 1
                    if lines_read >= 500:
                        break
        except Exception as exc:
            logger.warning(f"XFirehoseSubscriber: stream read failed: {exc}")

    def _payload_to_post(self, payload: dict[str, Any]) -> SocialPost | None:
        try:
            data = payload.get("data") or {}
            includes = payload.get("includes") or {}
            users = {u.get("id"): u for u in (includes.get("users") or [])}
            author_id = data.get("author_id")
            user = users.get(author_id, {}) if author_id else {}
            handle = (user.get("username") or "").lower()
            text = str(data.get("text") or "")
            created = data.get("created_at")
            if isinstance(created, str):
                posted_at = datetime.fromisoformat(created.replace("Z", "+00:00"))
            else:
                posted_at = datetime.now(tz=timezone.utc)
            entities = data.get("entities") or {}
            urls = entities.get("urls") or []
            market_urls = [u.get("expanded_url") or u.get("url") for u in urls]
            market_urls = [u for u in market_urls if u]
            return SocialPost(
                source=self.source,
                author_handle=handle,
                text=text,
                posted_at=posted_at,
                market_urls=market_urls,
                raw_payload=payload,
            )
        except Exception as exc:
            logger.debug(f"XFirehoseSubscriber: payload decode failed: {exc}")
            return None


# ---------------------------------------------------------------------------
# Fixture replay subscriber — for tests + offline smoke runs.
# ---------------------------------------------------------------------------


class FixtureXSubscriber(_BaseXSubscriber):
    """Deterministic test subscriber. Reads a JSON list of posts and
    yields them on the next ``run_once`` call. After the file is
    exhausted, subsequent calls return [].
    """

    source: str = "x"

    def __init__(
        self,
        redis_client: Any,
        fixture_path: str | Path,
        *,
        stream_name: str | None = None,
    ) -> None:
        super().__init__(redis_client, stream_name=stream_name)
        self._path = Path(fixture_path)
        self._exhausted = False

    async def _iterate_tweets(self) -> AsyncIterator[SocialPost]:
        if self._exhausted or not self._path.exists():
            return
        try:
            raw = self._path.read_text(encoding="utf-8")
            entries = json.loads(raw)
        except Exception as exc:
            logger.warning(f"FixtureXSubscriber: failed to read {self._path}: {exc}")
            self._exhausted = True
            return
        for entry in entries:
            try:
                posted_at = entry.get("posted_at")
                if isinstance(posted_at, str):
                    posted_at_dt = datetime.fromisoformat(
                        posted_at.replace("Z", "+00:00")
                    )
                else:
                    posted_at_dt = datetime.now(tz=timezone.utc)
                yield SocialPost(
                    source=self.source,
                    author_handle=str(entry.get("author_handle", "")).lower(),
                    text=str(entry.get("text", "")),
                    posted_at=posted_at_dt,
                    market_urls=list(entry.get("market_urls") or []),
                    raw_payload=entry,
                )
            except Exception as exc:
                logger.debug(
                    f"FixtureXSubscriber: skipping bad entry: {exc}"
                )
        self._exhausted = True


__all__ = [
    "FixtureXSubscriber",
    "SocialPost",
    "XFirehoseSubscriber",
    "decode_stream_fields",
]
