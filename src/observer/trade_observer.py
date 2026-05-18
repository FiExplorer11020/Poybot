"""
Trade Observer — dual-source trade ingestion: WebSocket + data-api.polymarket.com backfill.
Deduplicates trades using Redis. Stores to trades_observed. Publishes to Redis pub/sub.

Note: Polymarket CLOB WebSocket market channel sends orderbook/price_change events only
(no wallet addresses). Leader trade attribution comes exclusively from data-api backfill.

HP-1 (Phase 1 Task O): producer/consumer pipeline. The WS + REST coroutines act as
producers and only ever do Redis-fast dedup before enqueuing onto a bounded
`asyncio.Queue`. A dedicated `_db_writer_loop` drains the queue in batches and
performs all DB writes inside one transaction per batch. This decouples ingestion
latency from Postgres RTT and gives us visible backpressure metrics.
"""

import asyncio
import hashlib
import json
import time
from collections import OrderedDict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Iterable, Iterator

import aiohttp
import asyncpg
from loguru import logger

from src.config import settings
from src.database.connection import get_db
from src.observer.websocket_client import PolymarketWSClient
from src.registry.falcon_client import FalconClient

# Phase 1 Task M contract import. If Task M hasn't landed yet (early test
# runs), fall back to no-op metrics so trade_observer still imports cleanly.
# In production Task M MUST land before this module — the no-op path is a
# build-system concession, not a behaviour we want to ship.
try:
    from src.monitoring.metrics import (  # type: ignore[attr-defined]
        db_write_batch_size,
        db_write_latency_seconds,
        observer_queue_depth,
        observer_queue_drops_total,
        polling_cursor_lag_seconds,
        redis_publishes_total,
        trade_ingestion_latency_seconds,
        trades_ingested_total,
        ws_disconnects_total,  # noqa: F401  (re-exported for websocket_client)
    )
except ImportError:  # pragma: no cover — fallback for early CI before Task M
    class _NoopMetric:
        def labels(self, *a, **kw):  # noqa: ANN001
            return self

        def inc(self, *a, **kw):  # noqa: ANN001
            return None

        def observe(self, *a, **kw):  # noqa: ANN001
            return None

        def set(self, *a, **kw):  # noqa: ANN001
            return None

    trades_ingested_total = _NoopMetric()
    trade_ingestion_latency_seconds = _NoopMetric()
    db_write_batch_size = _NoopMetric()
    db_write_latency_seconds = _NoopMetric()
    observer_queue_depth = _NoopMetric()
    observer_queue_drops_total = _NoopMetric()
    redis_publishes_total = _NoopMetric()
    ws_disconnects_total = _NoopMetric()
    polling_cursor_lag_seconds = _NoopMetric()

# Phase 3 Task D: ingest-health heartbeats. Defensive import so older
# checkouts still load this module.
try:
    from src.monitoring.ingest_health import (  # type: ignore[attr-defined]
        SOURCE_FALCON_TRADES,
        SOURCE_REST_DATA_API,
        get_health_monitor,
    )

    def _heartbeat_rest() -> None:
        try:
            get_health_monitor().heartbeat(SOURCE_REST_DATA_API)
        except Exception:
            pass

    def _heartbeat_falcon_trades() -> None:
        try:
            get_health_monitor().heartbeat(SOURCE_FALCON_TRADES)
        except Exception:
            pass
except Exception:  # pragma: no cover
    def _heartbeat_rest() -> None:
        return None

    def _heartbeat_falcon_trades() -> None:
        return None

REDIS_TRADES_CHANNEL = "trades:observed"
DEDUP_KEY_PREFIX = "seen_trades"
DEDUP_TTL_S = 7 * 86400  # 7 days
MARKET_META_TTL_S = 3600
SOURCE_API_WALLET = "api_wallet"
SOURCE_API_MARKET = "api_market"

# Phase 3 Round 1 (Agent A) — continuous-cursor REST polling. The cursor
# is a monotonic ``(timestamp_s, tx_hash)`` tuple, persisted in Redis at
# ``observer:cursor:trades:<source>``. Time-window queries
# (``?from=…&to=…``) are gone — they had edge gaps where a trade
# landing at boundary +/- 1 ms was silently skipped. The cursor pattern
# is "fetch a fixed-size window of the latest trades, dedupe against
# the cursor in client code". The data-api response ships sorted-by-
# time DESC so the *first* row of each response is the new cursor head.
CURSOR_KEY_PREFIX = "observer:cursor:trades"
# Per-channel "last message at" tracker used by the WS freshness
# watchdog. Owned by trade_observer.py (we control _handle_ws_message)
# and consumed by websocket_client.py (watchdog reads these keys).
WS_LAST_MSG_KEY_PREFIX = "observer:ws:last_msg"
# Map raw WS event types into bounded channel labels for the freshness
# watchdog. Prometheus rejects unbounded cardinality.
_WS_CHANNEL_LABEL_BY_EVENT: dict[str, str] = {
    "book": "book",
    "price_change": "price_change",
    "trade": "trade",
}


def _cursor_key(source: str, scope: str = "") -> str:
    """Build the Redis key for a cursor. ``scope`` is used for the
    per-wallet path (one cursor per wallet) so the cursor doesn't move
    backward when a different wallet's pagination returns an older max.
    """
    if scope:
        return f"{CURSOR_KEY_PREFIX}:{source}:{scope.lower()}"
    return f"{CURSOR_KEY_PREFIX}:{source}"


def _ws_last_msg_key(channel: str) -> str:
    return f"{WS_LAST_MSG_KEY_PREFIX}:{channel}"

# HP-1 (Phase 1 Task O): how long a producer will wait to enqueue before giving
# up and counting a queue-full drop. 1 s is generous enough that a brief writer
# stall doesn't bleed into the WS pong loop, but tight enough that we don't
# starve the producer's other work (dedup, Redis pubsub).
QUEUE_PUT_TIMEOUT_S = 1.0

# Cache caps — sized for an Oracle Free 24GB ARM VM. Polymarket has ~thousands of
# active markets at any time and a few hundred leaders, so these are well above
# the working set while still capping unbounded growth that would OOM a long-lived
# observer process.
MARKET_META_CACHE_MAXSIZE = 10_000
LEADER_CONDITION_IDS_MAXSIZE = 2_000


class _BoundedTTLCache:
    """Tiny in-memory LRU+TTL cache (no dep) used to bound observer state.

    - `maxsize` LRU eviction when capacity is reached.
    - `ttl` (seconds) eviction on read for expired entries.
    Designed for the small surface used by TradeObserver: __contains__, get,
    __setitem__, __getitem__. NOT thread-safe; observer is single-asyncio-loop.
    """

    __slots__ = ("_data", "_maxsize", "_ttl")

    def __init__(self, *, maxsize: int, ttl: float):
        self._data: OrderedDict[str, tuple[float, float]] = OrderedDict()
        self._maxsize = max(1, int(maxsize))
        self._ttl = float(ttl)

    def __len__(self) -> int:  # for tests / introspection
        return len(self._data)

    def __contains__(self, key: str) -> bool:
        return self.get(key) is not None

    def get(self, key: str, default: float | None = None) -> float | None:
        entry = self._data.get(key)
        if entry is None:
            return default
        value, expires_at = entry
        if expires_at <= time.time():
            self._data.pop(key, None)
            return default
        # Refresh LRU recency on read.
        self._data.move_to_end(key)
        return value

    def __getitem__(self, key: str) -> float:
        v = self.get(key)
        if v is None:
            raise KeyError(key)
        return v

    def __setitem__(self, key: str, value: float) -> None:
        expires_at = time.time() + self._ttl
        if key in self._data:
            self._data.move_to_end(key)
        self._data[key] = (float(value), expires_at)
        while len(self._data) > self._maxsize:
            self._data.popitem(last=False)


class _BoundedSet:
    """A FIFO-bounded set-like container (insertion-ordered, capped size).

    Used for `_leader_condition_ids` so the observer cannot accumulate every
    market a leader has ever touched. Oldest entries fall out first; periodic
    DB rehydration in `_get_recent_leader_market_ids()` re-warms the working
    set from `trades_observed`.
    """

    __slots__ = ("_data", "_maxsize")

    def __init__(self, *, maxsize: int, initial: Iterable[str] | None = None):
        self._data: OrderedDict[str, None] = OrderedDict()
        self._maxsize = max(1, int(maxsize))
        if initial:
            self.update(initial)

    def __len__(self) -> int:
        return len(self._data)

    def __bool__(self) -> bool:
        return bool(self._data)

    def __contains__(self, key: str) -> bool:
        return key in self._data

    def __iter__(self) -> Iterator[str]:
        return iter(self._data)

    def add(self, key: str) -> None:
        if key in self._data:
            # Re-adding a hot key refreshes its recency, protecting it from
            # FIFO eviction. Important for `_leader_condition_ids` where
            # actively-traded markets must not fall out just because they
            # were inserted long ago.
            self._data.move_to_end(key)
            return
        self._data[key] = None
        while len(self._data) > self._maxsize:
            self._data.popitem(last=False)

    def update(self, keys: Iterable[str]) -> None:
        for k in keys:
            self.add(k)

    def replace(self, keys: Iterable[str]) -> None:
        """Atomically rebuild the set from a fresh source (e.g. DB rehydrate)."""
        self._data.clear()
        self.update(keys)


def _row_value(row: Any, key: str, default: Any = None) -> Any:
    if row is None:
        return default
    try:
        return row[key]
    except Exception:
        return default


def _json_dict(raw: Any) -> dict:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            return {}
    if raw is None:
        return {}
    try:
        return dict(raw)
    except Exception:
        return {}


def _market_type_label(category: Any, question: Any = None) -> str:
    """Infer a market's thematic category from its category hint + question text.

    Order matters: weather is checked before sports because some weather
    questions also contain "win" or location names that overlap with sports
    tokens. Crypto wins over politics/macro because "btc" is unambiguous.
    """
    category_text = str(category or "").strip()
    text = f"{category_text} {question or ''}".lower()

    # Weather first — many of these would match "win on 20" otherwise.
    weather_tokens = (
        "highest temperature", "lowest temperature", "high temp", "low temp",
        "°c", "°f", "celsius", "fahrenheit",
        "rainfall", "snowfall", "snow on", "rain on",
        "hurricane", "typhoon", "tropical storm",
    )
    if any(token in text for token in weather_tokens):
        return "weather"

    crypto_tokens = (
        "bitcoin", "btc", "ethereum", "eth ", "crypto", "solana", "sol ",
        "xrp", "doge", "ada", "cardano", "altcoin", "halving", "stablecoin",
        "usdc", "usdt",
    )
    if any(token in text for token in crypto_tokens):
        return "crypto"

    sports_tokens = (
        " vs ", " vs.", " o/u ", "map ", "map handicap", "set ", "handicap",
        "grand prix", "formula 1", "f1 ", "nascar",
        "premier league", "champions league", "europa league", "world cup",
        "uefa", "fifa", "epl", "la liga", "bundesliga", "serie a", "ligue 1",
        "ipl", "ncaa", "march madness",
        "tennis", "atp", "wta", "wimbledon", "us open",
        "soccer", "football", "basketball", "baseball", "hockey",
        "nba", "nfl", "mlb", "nhl", "wnba", "mls",
        "boxing", "ufc", "mma",
        "cup", " fc ", "fc.", "fc?",
        "winner", "to win", " win on 20",
    )
    if any(token in text for token in sports_tokens):
        return "sports"

    politics_tokens = (
        "election", "president", "senate", "house of representatives",
        "parliament", "mayor", "vote", "ballot", "congress",
        "primary", "caucus", "candidate", "governor",
        "trump", "biden", "harris", "putin", "xi jinping",
    )
    if any(token in text for token in politics_tokens):
        return "politics"

    macro_tokens = (
        "fed ", "fomc", "inflation", "cpi", "ppi",
        "rate cut", "rate hike", "interest rate", "recession",
        "gdp", "unemployment", "jobless", "nonfarm",
        "tariff", "trade war",
    )
    if any(token in text for token in macro_tokens):
        return "macro"

    entertainment_tokens = (
        "movie", "film", "album", "oscar", "grammy", "emmy",
        " tv ", " show ", "netflix", "spotify",
        "billboard", "box office", "season finale",
    )
    if any(token in text for token in entertainment_tokens):
        return "entertainment"

    # Last resort: trust an explicit non-unknown category hint.
    if category_text and category_text.lower() not in {"unknown", "none", "null"}:
        return category_text

    return "unknown"


def _infer_market_category(question: Any = None, slug: Any = None) -> str:
    return _market_type_label(None, f"{question or ''} {slug or ''}")


def _normalize_token_ids(raw: Any) -> list[str]:
    tokens = raw
    try:
        if isinstance(tokens, str):
            tokens = json.loads(tokens)
    except Exception:
        tokens = [tokens]
    if not isinstance(tokens, list):
        tokens = [tokens]
    return [str(token) for token in tokens if token]


def _token_hints_from_trade(
    token_id: str,
    outcome_index: Any = None,
    outcome_name: Any = None,
) -> tuple[str | None, str | None]:
    if not token_id:
        return None, None
    try:
        idx = int(outcome_index) if outcome_index is not None else None
    except (TypeError, ValueError):
        idx = None
    outcome = str(outcome_name or "").strip().lower()
    if idx == 0:
        return token_id, None
    if idx == 1:
        return None, token_id
    if outcome in {"yes", "up"}:
        return token_id, None
    if outcome in {"no", "down"}:
        return None, token_id
    return None, None


def _gamma_market_matches_request(market: dict, market_id: str, token_id: str) -> bool:
    condition_id = str(market.get("conditionId") or market.get("condition_id") or "").strip()
    if market_id and condition_id and condition_id != market_id:
        return False

    tokens = set(_normalize_token_ids(market.get("clobTokenIds")))
    single_token = str(market.get("clobTokenId") or "").strip()
    if single_token:
        tokens.add(single_token)
    if token_id and tokens and token_id not in tokens:
        return False

    if market_id and not condition_id and not tokens:
        return False
    return True


@dataclass(slots=True)
class _TradeRecord:
    """In-memory record handed from `_process_trade` (producer) to the
    `_db_writer_loop` (consumer). Carries everything the writer needs so the
    writer never has to call back into the producer for context.

    `event_ts_s` is the wall-clock time when the upstream event was *first*
    observed (WS message arrival or REST response). It feeds
    `trade_ingestion_latency_seconds` so we can prove HP-1 actually delivers
    the median 16 s → 2-3 s freshness cut.
    """

    market_id: str
    token_id: str
    wallet_address: str
    side: str
    price: Decimal
    size_usdc: Decimal
    trade_time: datetime
    source: str
    is_leader: bool
    dedup_key: str
    event_ts_s: float
    market_question_hint: str | None = None
    market_slug_hint: str | None = None
    outcome_hint: str | None = None
    outcome_index: int | None = None
    # Pure-Python category inference, computed inside `_process_trade` so the
    # writer never has to re-parse the question text. The writer only refines
    # this against the markets row when DB content suggests a better label.
    inferred_category: str = "unknown"


class TradeObserver:
    def __init__(
        self,
        falcon_client: FalconClient,
        redis_client,  # redis.asyncio.Redis
        leader_wallets: set[str] | None = None,
        leader_markets: set[str] | None = None,
    ):
        self._falcon = falcon_client
        self._redis = redis_client
        self._leader_wallets: set[str] = leader_wallets or set()
        self._leader_markets: set[str] = leader_markets or set()
        self._leader_condition_ids: _BoundedSet = _BoundedSet(
            maxsize=LEADER_CONDITION_IDS_MAXSIZE,
        )
        self._running = False
        self._stop_event = asyncio.Event()
        self._ws_client: PolymarketWSClient | None = None
        # Sprint 1 Day 1.1 (WS sharding): when WS_SHARD_COUNT > 1 we fan
        # out the leader-market set across N WebSocket connections to
        # stay inside the per-connection token limit. ``_ws_client``
        # remains as the legacy single-conn handle so tests + the public
        # API surface keep working; ``_ws_clients`` is the full list
        # (always at least one entry, equal to _ws_client when present).
        self._ws_clients: list[PolymarketWSClient] = []
        self._inserted: int = 0
        self._market_meta_cache: _BoundedTTLCache = _BoundedTTLCache(
            maxsize=MARKET_META_CACHE_MAXSIZE,
            ttl=MARKET_META_TTL_S,
        )
        self._book_age_samples: deque[float] = deque(maxlen=512)

        # HP-1 fix #3: bounded write queue + dedicated DB writer task. The
        # queue is allocated lazily on `start()` so unit tests that build a
        # TradeObserver and call `_process_trade` directly without a running
        # event loop still work — `_process_trade` lazy-creates the queue too.
        self._write_queue: asyncio.Queue[_TradeRecord] | None = None
        self._writer_task: asyncio.Task | None = None
        self._writer_drain_event: asyncio.Event = asyncio.Event()

        # HP-1 fix #1 supplement: ETag / If-Modified-Since state for the
        # global market sweep. `None` means "no cached validator yet" — we
        # send the request without conditional headers. After the first 200
        # response we capture whatever ETag/Last-Modified the server sent;
        # subsequent requests echo it back as `If-None-Match` /
        # `If-Modified-Since`. Lost on restart by design (Phase 1 scope —
        # cold start does at most one wasted full poll).
        self._last_etag: str | None = None
        self._last_modified: str | None = None
        # If the server confirms it ships ETag/Last-Modified at least once,
        # flip this to True so we don't keep emitting the
        # "no validators on response" debug log every 5 s.
        self._etag_observed: bool = False

    @property
    def inserted_count(self) -> int:
        return self._inserted

    # ------------------------------------------------------------------ #
    # Phase 3 Round 1 (Agent A) — continuous-cursor helpers               #
    # ------------------------------------------------------------------ #

    async def _load_cursor(self, source: str, scope: str = "") -> tuple[float, str]:
        """Return the persisted ``(timestamp_s, tx_hash)`` cursor for a
        source. On miss falls back to "now minus
        OBSERVER_CURSOR_BOOTSTRAP_LOOKBACK_S" with an explicit log;
        subsequent polls will advance the cursor from there.

        Cursor format on the wire: JSON ``{"ts": <float>, "tx": <hex>}``.
        Older string-only cursors (``"<ts>"``) are accepted for forward
        compatibility — a fresh deploy never sees them, but a partial
        rollback shouldn't crash the observer.
        """
        if self._redis is None:
            return self._cursor_bootstrap()
        key = _cursor_key(source, scope)
        try:
            raw = await self._redis.get(key)
        except Exception as exc:
            logger.debug(
                f"_load_cursor({source}, {scope}): Redis GET failed: {exc}; "
                "falling back to bootstrap lookback"
            )
            return self._cursor_bootstrap(missing_log_source=source)
        if not raw:
            return self._cursor_bootstrap(missing_log_source=source)
        try:
            if isinstance(raw, (bytes, bytearray)):
                raw = raw.decode("utf-8", errors="ignore")
            data = json.loads(raw)
            return float(data.get("ts") or 0.0), str(data.get("tx") or "")
        except Exception:
            try:
                if isinstance(raw, str) and ":" in raw:
                    ts_str, tx_str = raw.split(":", 1)
                    return float(ts_str), tx_str
                return float(raw), ""
            except Exception:
                return self._cursor_bootstrap(missing_log_source=source)

    def _cursor_bootstrap(
        self, *, missing_log_source: str | None = None
    ) -> tuple[float, str]:
        """Build the boot-time fallback cursor: now minus the configured
        bootstrap lookback window. Logs once per source so a missing
        cursor is auditable in ops.
        """
        lookback = max(0, int(settings.OBSERVER_CURSOR_BOOTSTRAP_LOOKBACK_S))
        ts = time.time() - lookback
        if missing_log_source:
            logger.info(
                f"observer cursor missing for source={missing_log_source}; "
                f"bootstrapping at now - {lookback}s "
                f"(ts={ts:.0f}, tx='')"
            )
        return ts, ""

    async def _save_cursor(
        self,
        source: str,
        ts_s: float,
        tx_hash: str,
        *,
        scope: str = "",
    ) -> None:
        """Atomically persist the cursor head AFTER the batch has
        committed to PG.

        Atomicity guarantee: trade_observer commits the batch via
        ``_write_batch`` (single PG transaction) BEFORE calling
        ``_save_cursor`` from the poll loop. If the process crashes
        between the commit and this SET, the next boot replays from
        the previous cursor — the DB UNIQUE INDEX absorbs the
        duplicates, zero data loss. If the process crashes during the
        commit (PG tx rollback), the cursor was never advanced; the
        next poll re-reads from the same starting point.
        """
        if self._redis is None:
            return
        key = _cursor_key(source, scope)
        ttl = max(60, int(settings.OBSERVER_CURSOR_TTL_S))
        payload = json.dumps({"ts": float(ts_s), "tx": str(tx_hash or "")})
        try:
            await self._redis.set(key, payload, ex=ttl)
        except Exception as exc:
            # Cursor persistence is best-effort. Worst case on Redis
            # failure: the next poll bootstraps from "now - lookback"
            # and the UNIQUE INDEX dedupes anything we re-fetch.
            logger.debug(f"_save_cursor({source}): Redis SET failed: {exc}")

    @staticmethod
    def _trade_cursor_tuple(trade: dict) -> tuple[float, str]:
        """Extract the cursor coordinates from a data-api trade row.

        Falls back to an empty ``tx_hash`` if the response shape doesn't
        ship one; the cursor degrades to "timestamp only", and the DB
        UNIQUE INDEX still prevents double-ingestion.
        """
        ts_raw = trade.get("timestamp", 0)
        try:
            ts_int = int(ts_raw)
        except (TypeError, ValueError):
            return 0.0, ""
        ts_s = ts_int / 1000.0 if ts_int > 1_000_000_000_000 else float(ts_int)
        tx_hash = str(
            trade.get("transactionHash")
            or trade.get("transaction_hash")
            or trade.get("tx_hash")
            or ""
        ).lower()
        return ts_s, tx_hash

    @staticmethod
    def _cursor_filter_new(
        trades: list[dict],
        cursor_ts: float,
        cursor_tx: str,
    ) -> list[dict]:
        """Return only the trades strictly newer than the cursor.

        Tiebreak rule on equal timestamps: a different ``tx_hash``
        counts as new (two trades from the same wallet in the same
        millisecond are legal — they have different tx hashes), the
        same hash is the cursor head itself and is dropped.

        Hash comparison is case-insensitive — ``_trade_cursor_tuple``
        lowercases tx hashes on extract so an uppercase cursor passed
        in (e.g. from a test) is normalised here too.
        """
        cursor_tx_norm = (cursor_tx or "").lower()
        out: list[dict] = []
        for tr in trades:
            ts_s, tx_hash = TradeObserver._trade_cursor_tuple(tr)
            if ts_s > cursor_ts:
                out.append(tr)
                continue
            if (
                ts_s == cursor_ts
                and tx_hash
                and tx_hash != cursor_tx_norm
            ):
                out.append(tr)
        return out

    @staticmethod
    def _cursor_head(trades: list[dict]) -> tuple[float, str]:
        """Find the maximum ``(ts, tx_hash)`` across a batch.

        We don't assume server-side ordering — the data-api returns
        most-recent-first today, but a single misordered row would
        otherwise rewind our cursor and re-process trades.
        """
        max_ts = 0.0
        max_tx = ""
        for tr in trades:
            ts_s, tx_hash = TradeObserver._trade_cursor_tuple(tr)
            if ts_s > max_ts or (ts_s == max_ts and tx_hash > max_tx):
                max_ts = ts_s
                max_tx = tx_hash
        return max_ts, max_tx

    def update_leaders(self, wallets: set[str], markets: set[str]) -> None:
        """Dynamically update leader wallets and markets."""
        self._leader_wallets = wallets
        self._leader_markets = markets
        if self._ws_clients:
            for shard_markets, client in zip(
                self._shard_markets(markets), self._ws_clients
            ):
                client.update_markets(shard_markets)
        elif self._ws_client:
            self._ws_client.update_markets(markets)

    @staticmethod
    def _shard_markets(markets: set[str]) -> list[set[str]]:
        """Split a market-id set into ``WS_SHARD_COUNT`` deterministic
        shards. Sprint 1 Day 1.1.

        Uses ``hash(market_id) % N`` so a single market always lands on
        the same shard across restarts (avoids the same token being
        torn between two reconnect loops on transient hiccups).
        Returns exactly ``WS_SHARD_COUNT`` lists (some may be empty if
        the universe is smaller than the shard count).
        """
        n = max(1, int(settings.WS_SHARD_COUNT))
        buckets: list[set[str]] = [set() for _ in range(n)]
        for mid in markets:
            buckets[hash(mid) % n].add(mid)
        return buckets

    def _ensure_write_queue(self) -> asyncio.Queue:
        """Lazy-init the bounded write queue. Called from both producer and
        consumer paths so unit tests that exercise `_process_trade` without
        going through `start()` still see a real queue.
        """
        if self._write_queue is None:
            self._write_queue = asyncio.Queue(
                maxsize=max(1, int(settings.TRADE_OBSERVER_QUEUE_MAX))
            )
        return self._write_queue

    async def start(self) -> None:
        self._running = True
        self._stop_event.clear()
        self._ensure_write_queue()
        # Sprint 1 Day 1.1: spawn one PolymarketWSClient per shard. With
        # WS_SHARD_COUNT=1 the behaviour is byte-identical to the
        # pre-sharding single-client path.
        shard_count = max(1, int(settings.WS_SHARD_COUNT))
        shard_buckets = self._shard_markets(self._leader_markets)
        self._ws_clients = [
            PolymarketWSClient(
                on_message=self._handle_ws_message,
                markets=shard_buckets[i],
                # Phase 3 Round 1 (Agent A): WS client uses Redis for
                # the per-channel freshness watchdog. Sharing is safe —
                # watchdog only reads. Note: all shards write the same
                # `observer:ws:last_msg:<channel>` keys; if one shard is
                # stale but another is alive the watchdog will not fire
                # (intended — silence is per-channel-across-shards).
                redis_client=self._redis,
            )
            for i in range(shard_count)
        ]
        # Keep `_ws_client` pointing at shard 0 for backwards compat
        # (tests + diagnostic surfaces reach in via this attribute).
        self._ws_client = self._ws_clients[0]
        # HP-1 fix #3: dedicated DB writer task drains the queue in batches.
        # Started BEFORE the producers so the very first enqueued record has
        # somewhere to land. `gather` so any one crashing surfaces in the
        # supervisor.
        self._writer_task = asyncio.create_task(self._db_writer_loop())
        tasks: list = [self._writer_task]
        tasks.extend(asyncio.create_task(c.start()) for c in self._ws_clients)
        tasks.append(asyncio.create_task(self._backfill_loop()))
        try:
            await asyncio.gather(*tasks)
        except asyncio.CancelledError:
            pass

    async def stop(self) -> None:
        self._running = False
        self._stop_event.set()
        for client in self._ws_clients:
            try:
                await client.stop()
            except Exception as exc:
                logger.warning(f"ws_client.stop raised: {exc}")
        # Legacy single-client path (no shards spawned, e.g. unit tests
        # that constructed _ws_client manually).
        if not self._ws_clients and self._ws_client:
            await self._ws_client.stop()
        # Drain anything left in the queue before tearing down the writer task
        # so we don't lose trades that were enqueued but not yet committed.
        if self._writer_task is not None:
            try:
                await asyncio.wait_for(self._drain_writer(), timeout=5.0)
            except asyncio.TimeoutError:
                logger.warning(
                    "DB writer drain timed out after 5 s; "
                    f"{self._write_queue.qsize() if self._write_queue else 0} "
                    "records may be lost"
                )
            self._writer_task.cancel()
            try:
                await self._writer_task
            except (asyncio.CancelledError, Exception):
                pass
            self._writer_task = None

    async def _drain_writer(self) -> None:
        """Wait until the queue is empty AND the writer is idle. Used by
        `stop()` and by tests that need a deterministic flush.
        """
        if self._write_queue is None:
            return
        while self._write_queue.qsize() > 0:
            await asyncio.sleep(0.01)

    async def _db_writer_loop(self) -> None:
        """Drain the write queue in batches and commit them as one tx each.

        Loop body: collect up to TRADE_OBSERVER_BATCH_MAX records or wait
        TRADE_OBSERVER_BATCH_FLUSH_MS milliseconds for the queue to fill,
        whichever comes first. Empty drains just sleep on `queue.get()` so
        we don't spin.
        """
        while self._running and not self._stop_event.is_set():
            try:
                await self._writer_run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                # The per-batch path already logs on failure and falls back
                # to per-row insert. Anything that escapes that is a
                # programmer error — log loudly and keep going so a single
                # bad batch doesn't take down ingestion.
                logger.exception("DB writer iteration crashed; continuing")

    async def _writer_run_once(self) -> int:
        """Drain at most one batch from the queue and commit it.

        Returns the number of records the writer attempted to insert (i.e.
        batch size, not committed-row count). Used by `_db_writer_loop` and
        by unit tests that need a deterministic flush.
        """
        queue = self._ensure_write_queue()
        observer_queue_depth.set(queue.qsize())
        batch_max = max(1, int(settings.TRADE_OBSERVER_BATCH_MAX))
        flush_ms = max(1, int(settings.TRADE_OBSERVER_BATCH_FLUSH_MS))
        flush_deadline = time.monotonic() + (flush_ms / 1000.0)

        # Block on the first record so an idle writer doesn't spin.
        try:
            first = await asyncio.wait_for(
                queue.get(), timeout=flush_ms / 1000.0
            )
        except asyncio.TimeoutError:
            return 0

        batch: list[_TradeRecord] = [first]
        # Then opportunistically pull more without blocking, capped at
        # batch_max OR by the flush deadline.
        while len(batch) < batch_max and time.monotonic() < flush_deadline:
            try:
                batch.append(queue.get_nowait())
                continue
            except asyncio.QueueEmpty:
                pass
            remaining = flush_deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                nxt = await asyncio.wait_for(
                    queue.get(), timeout=min(remaining, 0.005)
                )
            except asyncio.TimeoutError:
                break
            batch.append(nxt)

        observer_queue_depth.set(queue.qsize())
        await self._write_batch(batch)
        return len(batch)

    async def _write_batch(self, batch: list[_TradeRecord]) -> None:
        """Commit one batch of trades. All-or-nothing transaction; on
        UniqueViolationError for the whole batch (rare — usually intra-batch
        dupes from WS+REST overlap), fall back to per-row inserts with
        ON CONFLICT DO NOTHING so partial progress is preserved.
        """
        if not batch:
            return

        t0 = time.monotonic()
        committed: list[tuple[_TradeRecord, int | None, dict | None, dict | None]] = []
        try:
            async with get_db() as conn:
                async with conn.transaction():
                    committed = await self._insert_batch_atomic(conn, batch)
        except asyncpg.UniqueViolationError:
            # Multi-row VALUES with ON CONFLICT DO NOTHING shouldn't raise
            # this — but if it does (intra-batch dupes that bypassed the
            # Redis dedup, or some other constraint), recover row-by-row so
            # we don't lose the whole batch.
            logger.warning(
                f"batch insert hit UniqueViolation; falling back to "
                f"per-row insert for {len(batch)} records"
            )
            committed = await self._insert_batch_per_row_fallback(batch)
        except Exception as exc:
            # Any other DB failure: clear dedup keys so retries can succeed,
            # log, and abandon the batch. We do NOT keep the records on the
            # queue — that would block the writer indefinitely if Postgres
            # is hard-down. Trade durability is best-effort here.
            for rec in batch:
                await self._clear_dedup_key(rec.dedup_key)
            logger.error(f"batch insert failed ({len(batch)} records): {exc}")
            db_write_batch_size.observe(len(batch))
            db_write_latency_seconds.observe(time.monotonic() - t0)
            return

        elapsed = time.monotonic() - t0
        db_write_batch_size.observe(len(batch))
        db_write_latency_seconds.observe(elapsed)

        n_inserted = sum(1 for c in committed if c[1] is not None)
        if n_inserted:
            self._inserted += n_inserted

        # Group source-wise for accurate per-source counters (Prometheus
        # rejects unbounded label cardinality, so we map to {ws,rest,backfill}).
        inserted_by_source: dict[str, int] = {}
        deduped_by_source: dict[str, int] = {}
        for rec, inserted_id, _, _ in committed:
            src = self._metric_source_label(rec.source)
            if inserted_id is not None:
                inserted_by_source[src] = inserted_by_source.get(src, 0) + 1
            else:
                deduped_by_source[src] = deduped_by_source.get(src, 0) + 1
        for src, n in inserted_by_source.items():
            trades_ingested_total.labels(source=src, result="inserted").inc(n)
        for src, n in deduped_by_source.items():
            trades_ingested_total.labels(source=src, result="deduped").inc(n)

        # End-to-end latency: from event observation to committed transaction.
        now_s = time.time()
        for rec, inserted_id, _, _ in committed:
            if inserted_id is None:
                continue
            trade_ingestion_latency_seconds.labels(
                source=self._metric_source_label(rec.source)
            ).observe(max(0.0, now_s - rec.event_ts_s))

        # Publish AFTER commit (Phase 0 ordering invariant — pub/sub never
        # advertises an uncommitted state).
        for rec, inserted_id, market_row, leader_row in committed:
            if inserted_id is None:
                continue
            await self._publish_trade_event(rec, market_row, leader_row)

        # Batch 2 / A7 fix: maintain `metrics:trades_observed:zset` —
        # a sliding 24h zset of trade_ids keyed by ingestion timestamp.
        # The /api/portfolio/pipeline_status endpoint reads
        # `metrics:trades_observed_24h` (cached counter) which is
        # populated below as `ZCARD` after the prune. Without this
        # producer the counter is permanently null and the dashboard's
        # observed-trades card stays stuck at "—". Per-wallet zsets
        # let downstream features (wallet activity widget, telegram
        # /trades) drill down without scanning the global one.
        await self._update_trades_observed_metric(committed)

    @staticmethod
    def _metric_source_label(source: str) -> str:
        """Map the observer's internal `source` strings to the Prometheus
        contract labels (ws | rest | backfill). Unknown sources default to
        'rest' so labels stay bounded (prom-client rejects unbounded
        cardinality).
        """
        if source == "websocket":
            return "ws"
        if source in (SOURCE_API_WALLET, SOURCE_API_MARKET):
            return "rest"
        if source == "falcon":
            return "backfill"
        return "rest"

    async def _update_trades_observed_metric(
        self,
        committed: list,
    ) -> None:
        """Maintain the trades-observed sliding-window metric (Batch 2 / A7).

        Two Redis surfaces are kept in sync here:

        * ``metrics:trades_observed:zset`` — a global zset where each
          freshly-inserted trade is added with its ingestion timestamp as
          the score. We trim entries older than 24h on every batch so
          ``ZCARD`` is the canonical "observed_trades_24h" counter.
        * ``metrics:trades_observed_24h`` — a derived integer counter
          (``ZCARD`` of the zset) cached with a short TTL so the
          /api/portfolio/pipeline_status read path stays a single GET.

        Per-wallet sub-zsets (``metrics:trades_observed:wallet:<addr>:zset``)
        are also maintained so downstream features (telegram /leaders,
        wallet activity widget) can answer "how many trades from wallet X
        in the last 24h" without scanning the global structure.

        Best-effort: redis failures are swallowed so a redis hiccup
        never corrupts the DB-write hot path.

        TODO(backfill): on first deploy the zset starts empty so the
        24h counter ramps up over a day. A one-shot
        ``scripts/backfill_trades_metric.py`` can prime it from the DB.
        """
        if self._redis is None or not committed:
            return
        now_ts = time.time()
        cutoff = now_ts - 86400
        global_zset = "metrics:trades_observed:zset"
        per_wallet: dict[str, dict[str, float]] = {}
        global_members: dict[str, float] = {}
        for entry in committed:
            try:
                rec, inserted_id, _market_row, _leader_row = entry
            except Exception:
                continue
            if inserted_id is None:
                continue
            # Score = ingestion time (now), member = trade pk so the same
            # row can never be counted twice (ZADD is set-semantics).
            member = f"id:{inserted_id}"
            global_members[member] = float(now_ts)
            wallet = getattr(rec, "wallet_address", None)
            if wallet:
                per_wallet.setdefault(wallet, {})[member] = float(now_ts)

        if not global_members:
            return

        try:
            # ZADD takes mapping in redis-py 4+/5+.
            await self._redis.zadd(global_zset, global_members)
            await self._redis.zremrangebyscore(global_zset, 0, cutoff)
            zcard = await self._redis.zcard(global_zset)
            # The counter is the canonical read for pipeline_status — 90s
            # TTL is generous since the producer runs every batch.
            await self._redis.set(
                "metrics:trades_observed_24h",
                str(int(zcard)),
                ex=90,
            )
            # Per-wallet zsets (sliding 24h). The TTL is set on the zset
            # itself so dead wallets don't pile up forever; the prune
            # inside the window keeps it correct.
            for wallet, members in per_wallet.items():
                key = f"metrics:trades_observed:wallet:{wallet}:zset"
                await self._redis.zadd(key, members)
                await self._redis.zremrangebyscore(key, 0, cutoff)
                # 7d TTL = re-armed any time the wallet trades again.
                await self._redis.expire(key, 7 * 86400)
        except Exception as exc:
            # Never break the ingest hot path on a redis stutter.
            logger.debug(f"trades_observed metric update failed: {exc}")

    async def _insert_batch_atomic(
        self,
        conn,
        batch: list[_TradeRecord],
    ) -> list[tuple[_TradeRecord, int | None, dict | None, dict | None]]:
        """The happy path: one tx, batched markets-stub upsert, batched
        trades_observed insert with RETURNING, then per-row enrichment
        (markets repair + leader fetch) inside the same tx.

        Returns a list of (record, inserted_id_or_None, market_row, leader_row)
        in input order. inserted_id=None means the row was deduped at the
        DB layer.
        """
        # 1. Markets stub upsert — one row per unique market_id in the batch.
        unique_markets: dict[str, str] = {}
        for rec in batch:
            unique_markets.setdefault(
                rec.market_id,
                rec.market_question_hint or f"Market {rec.market_id[:30]}…",
            )
        await conn.executemany(
            """
            INSERT INTO markets (market_id, question, category)
            VALUES ($1, $2, 'unknown')
            ON CONFLICT (market_id) DO NOTHING
            """,
            list(unique_markets.items()),
        )

        # 2. Resolve initial category per unique market_id. Done as a separate
        # batched SELECT rather than inlining a subquery in step 3's INSERT
        # because the multi-row form would force asyncpg to deduce one type
        # for $market_id used in two contexts (VALUES + WHERE in the
        # subquery), which Postgres rejects with "inconsistent types deduced
        # for parameter $N". One extra round-trip on the same conn is cheap.
        category_rows = await conn.fetch(
            """
            SELECT market_id, NULLIF(category, 'unknown') AS category
            FROM markets
            WHERE market_id = ANY($1::text[])
            """,
            list(unique_markets.keys()),
        )
        initial_category_by_market: dict[str, str] = {
            row["market_id"]: (row["category"] or "unknown") for row in category_rows
        }

        # 3. Batched trades_observed insert — multi-row VALUES with RETURNING.
        # asyncpg's executemany() doesn't return rows; a multi-row VALUES
        # INSERT does. We RETURN the natural-key tuple so we can correlate
        # the response set back to input records (some may have been
        # ON-CONFLICT-dropped).
        params: list = []
        placeholders: list[str] = []
        for i, rec in enumerate(batch):
            base = i * 10
            placeholders.append(
                f"(${base + 1}, ${base + 2}, ${base + 3}, ${base + 4}, "
                f"${base + 5}, ${base + 6}, ${base + 7}, ${base + 8}, "
                f"${base + 9}, ${base + 10})"
            )
            params.extend([
                rec.trade_time,
                rec.market_id,
                rec.token_id,
                rec.wallet_address,
                rec.side,
                rec.price,
                rec.size_usdc,
                rec.source,
                rec.is_leader,
                initial_category_by_market.get(rec.market_id, "unknown"),
            ])
        sql = (
            "INSERT INTO trades_observed "
            "(time, market_id, token_id, wallet_address, side, price, "
            "size_usdc, source, is_leader, category) VALUES "
            + ", ".join(placeholders)
            + " ON CONFLICT (wallet_address, market_id, time, side, price, size_usdc) "
            "DO NOTHING RETURNING id, wallet_address, market_id, time, side, price, size_usdc"
        )
        returned = await conn.fetch(sql, *params)

        # Sprint 1 Day 2.2 (EXECUTION_PLAN § 6): organically grow the
        # wallet_universe table from every observed trade so leader
        # tracking no longer depends on the (broken) R6 onchain crawler
        # seed. UPSERT increments aggregates on subsequent hits; insert
        # on first-seen with default tier=2 (the crawler's depth-tier
        # daemon will re-tier offline based on volume).
        wallet_rows: dict[str, dict] = {}
        for rec in batch:
            w = rec.wallet_address
            cur = wallet_rows.get(w)
            if cur is None:
                wallet_rows[w] = {
                    "first_seen": rec.trade_time,
                    "last_active": rec.trade_time,
                    "trades": 1,
                    "volume": float(rec.size_usdc or 0),
                }
            else:
                if rec.trade_time < cur["first_seen"]:
                    cur["first_seen"] = rec.trade_time
                if rec.trade_time > cur["last_active"]:
                    cur["last_active"] = rec.trade_time
                cur["trades"] += 1
                cur["volume"] += float(rec.size_usdc or 0)
        if wallet_rows:
            wu_params: list = []
            wu_placeholders: list[str] = []
            for i, (wallet, agg) in enumerate(wallet_rows.items()):
                base = i * 5
                wu_placeholders.append(
                    f"(${base + 1}, ${base + 2}, ${base + 3}, "
                    f"${base + 4}, ${base + 5})"
                )
                wu_params.extend([
                    wallet,
                    agg["first_seen"],
                    agg["last_active"],
                    agg["trades"],
                    agg["volume"],
                ])
            await conn.execute(
                "INSERT INTO wallet_universe "
                "(wallet_address, first_seen, last_active, "
                "total_trades_ever, total_volume_usdc_ever) VALUES "
                + ", ".join(wu_placeholders)
                + " ON CONFLICT (wallet_address) DO UPDATE SET "
                "last_active = GREATEST(wallet_universe.last_active, "
                "EXCLUDED.last_active), "
                "total_trades_ever = wallet_universe.total_trades_ever "
                "+ EXCLUDED.total_trades_ever, "
                "total_volume_usdc_ever = wallet_universe.total_volume_usdc_ever "
                "+ EXCLUDED.total_volume_usdc_ever",
                *wu_params,
            )

        nk_to_id: dict[tuple, int] = {}
        for row in returned:
            key = (
                row["wallet_address"],
                row["market_id"],
                row["time"],
                row["side"],
                row["price"],
                row["size_usdc"],
            )
            nk_to_id[key] = row["id"]

        # 4. Batched leaders fetch — one query per unique leader wallet.
        leader_wallets_to_fetch: set[str] = {
            rec.wallet_address for rec in batch if rec.is_leader
        }
        leader_rows: dict[str, dict] = {}
        if leader_wallets_to_fetch:
            rows = await conn.fetch(
                """
                SELECT wallet_address, classification_json, excluded, on_watchlist
                FROM leaders
                WHERE wallet_address = ANY($1::text[])
                """,
                list(leader_wallets_to_fetch),
            )
            for row in rows:
                leader_rows[row["wallet_address"]] = dict(row)

        # 5. Per-row enrichment (markets repair + category refine UPDATE)
        # INSIDE the same tx so we still pay only one commit per batch.
        out: list[tuple[_TradeRecord, int | None, dict | None, dict | None]] = []
        for rec in batch:
            key = (
                rec.wallet_address,
                rec.market_id,
                rec.trade_time,
                rec.side,
                rec.price,
                rec.size_usdc,
            )
            inserted_id = nk_to_id.get(key)
            if inserted_id is None:
                logger.debug(
                    "trades_observed dupe blocked at DB layer: "
                    f"wallet={rec.wallet_address[:10]}… "
                    f"market={rec.market_id[:10]}… "
                    f"time={rec.trade_time.isoformat()}"
                )
                out.append((rec, None, None, None))
                continue

            market_row = await conn.fetchrow(
                """
                SELECT question, category, token_yes, token_no, end_date
                FROM markets
                WHERE market_id = $1
                """,
                rec.market_id,
            )
            market_row = await self._repair_market_from_trade_hint(
                conn=conn,
                market_id=rec.market_id,
                token_id=rec.token_id,
                trade_time=rec.trade_time,
                market_row=market_row,
                market_question_hint=rec.market_question_hint,
                market_slug_hint=rec.market_slug_hint,
                outcome_hint=rec.outcome_hint,
                outcome_index=rec.outcome_index,
            )
            refined_category = (_row_value(market_row, "category") or "").strip()
            if refined_category and refined_category.lower() not in {
                "", "unknown", "none", "null"
            }:
                await conn.execute(
                    """
                    UPDATE trades_observed
                    SET category = $2
                    WHERE id = $1 AND (category IS NULL OR category = 'unknown')
                    """,
                    inserted_id,
                    refined_category,
                )
            leader_row = leader_rows.get(rec.wallet_address)
            out.append((rec, inserted_id, market_row, leader_row))

        return out

    async def _insert_batch_per_row_fallback(
        self,
        batch: list[_TradeRecord],
    ) -> list[tuple[_TradeRecord, int | None, dict | None, dict | None]]:
        """Per-row fallback for when the atomic batch path raises a
        UniqueViolation. Each row gets its own tx so partial progress is
        preserved.
        """
        out: list[tuple[_TradeRecord, int | None, dict | None, dict | None]] = []
        for rec in batch:
            try:
                async with get_db() as conn:
                    async with conn.transaction():
                        single = await self._insert_batch_atomic(conn, [rec])
                        out.extend(single)
            except Exception as exc:
                logger.error(
                    f"per-row fallback failed for "
                    f"wallet={rec.wallet_address[:10]}… "
                    f"market={rec.market_id[:10]}…: {exc}"
                )
                await self._clear_dedup_key(rec.dedup_key)
                out.append((rec, None, None, None))
        return out

    async def _publish_trade_event(
        self,
        rec: _TradeRecord,
        market_row: dict | None,
        leader_row: dict | None,
    ) -> None:
        """Build the `trades:observed` payload and publish to Redis.

        Out-of-tx by design (Phase 0 invariant — pub/sub never advertises
        an uncommitted state). Also drives the Gamma-enrichment market-fetch
        path when the markets row is too thin to publish a useful payload.
        """
        if self._needs_market_enrichment(rec.market_id, market_row):
            try:
                enriched = await self._fetch_market_metadata_from_gamma(
                    rec.market_id, rec.token_id
                )
            except Exception as exc:
                logger.debug(f"Gamma market lookup failed for {rec.market_id}: {exc}")
                enriched = None
            if enriched:
                try:
                    async with get_db() as conn:
                        async with conn.transaction():
                            await conn.execute(
                                """
                                INSERT INTO markets
                                    (market_id, question, category, token_yes, token_no,
                                     end_date, volume_24h, liquidity_score, fee_rate_pct, updated_at)
                                VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,NOW())
                                ON CONFLICT (market_id) DO UPDATE SET
                                    question       = EXCLUDED.question,
                                    category       = EXCLUDED.category,
                                    token_yes      = COALESCE(EXCLUDED.token_yes, markets.token_yes),
                                    token_no       = COALESCE(EXCLUDED.token_no, markets.token_no),
                                    end_date       = COALESCE(EXCLUDED.end_date, markets.end_date),
                                    volume_24h     = COALESCE(EXCLUDED.volume_24h, markets.volume_24h),
                                    liquidity_score= COALESCE(
                                        EXCLUDED.liquidity_score,
                                        markets.liquidity_score
                                    ),
                                    fee_rate_pct   = COALESCE(
                                        EXCLUDED.fee_rate_pct,
                                        markets.fee_rate_pct
                                    ),
                                    updated_at     = NOW()
                                """,
                                rec.market_id,
                                enriched["question"],
                                enriched["category"],
                                enriched["token_yes"],
                                enriched["token_no"],
                                enriched["end_date"],
                                enriched["volume_24h"],
                                enriched["liquidity_score"],
                                enriched["fee_rate_pct"],
                            )
                    market_row = {
                        "question": enriched["question"],
                        "category": enriched["category"],
                    }
                    self._market_meta_cache[rec.market_id] = datetime.now(
                        tz=timezone.utc
                    ).timestamp()
                except Exception as exc:
                    logger.debug(
                        f"Failed to upsert Gamma market metadata for {rec.market_id}: {exc}"
                    )

        classification = _json_dict(_row_value(leader_row, "classification_json", {}))
        market_question = (
            _row_value(market_row, "question")
            or rec.market_question_hint
            or f"Market {rec.market_id[:30]}…"
        )
        market_category = _row_value(market_row, "category") or "unknown"
        market_type = _market_type_label(market_category, market_question)
        wallet_status = "market_participant"
        if rec.is_leader:
            if bool(_row_value(leader_row, "excluded", False)):
                wallet_status = "excluded"
            elif bool(_row_value(leader_row, "on_watchlist", False)):
                wallet_status = "active"
            else:
                wallet_status = "watching"

        # Typed event — see src/events/schemas.py. The pydantic model
        # enforces the producer/consumer contract; any new field MUST be
        # added to TradeObserved or the consumer side will reject it.
        from src.events.schemas import TradeObserved

        try:
            event_model = TradeObserved(
                time=rec.trade_time,
                market_id=rec.market_id,
                wallet_address=rec.wallet_address,
                side=rec.side,  # validator uppercases legacy "buy"/"sell"
                price=float(rec.price),
                size_usdc=float(rec.size_usdc),
                is_leader=rec.is_leader,
                source=rec.source,
                token_id=rec.token_id,
                market_question=market_question,
                market_category=market_category,
                market_type=market_type,
                wallet_type=(
                    "leader" if rec.is_leader else "market_participant"
                ),
                wallet_status=wallet_status,
                wallet_strategy=classification.get("strategy"),
                wallet_horizon=classification.get("horizon"),
                wallet_influence=classification.get("influence"),
            )
            await self._redis.publish(
                REDIS_TRADES_CHANNEL, event_model.model_dump_json()
            )
            redis_publishes_total.labels(
                channel=REDIS_TRADES_CHANNEL, result="ok"
            ).inc()
        except Exception as e:
            redis_publishes_total.labels(
                channel=REDIS_TRADES_CHANNEL, result="error"
            ).inc()
            logger.warning(f"Failed to publish trade event: {e}")

    async def _handle_ws_message(self, msg: dict) -> None:
        """Process a single WebSocket market message.

        The CLOB market channel sends orderbook snapshots (event_type='book') and
        price_change events. Neither includes wallet addresses. We log price changes
        to Redis for market monitoring and ignore the rest.
        """
        event_type = msg.get("event_type", "")
        if self._redis:
            try:
                now_ts = time.time()
                await self._redis.set("ws:market:last_message_ts", str(now_ts), ex=300)
                # Per-minute sliding counter so the dashboard ingestion source
                # for "CLOB WebSocket msgs/min" reflects the *real* WS throughput
                # (price_change + book + trade events), not just trades that
                # ended up in trades_observed (which under-counts by ~99x).
                minute_bucket = int(now_ts // 60)
                await self._redis.incrby(f"ws:msgs:minute:{minute_bucket}", 1)
                await self._redis.expire(f"ws:msgs:minute:{minute_bucket}", 180)
                # Phase 3 Round 1 (Agent A) — per-channel last-message
                # tracking for the WS freshness watchdog. The watchdog in
                # PolymarketWSClient reads these keys to decide whether a
                # specific channel has gone silent (vs the whole socket
                # which we already detect via ping/pong). 5 min TTL = if
                # we crash, the watchdog reads "stale" and triggers a
                # reconnect on next tick.
                channel = _WS_CHANNEL_LABEL_BY_EVENT.get(event_type, "other")
                await self._redis.set(
                    _ws_last_msg_key(channel), str(now_ts), ex=300
                )
                # Aggregate "any message" key — useful for the global
                # watchdog (Agent D) which doesn't care about per-channel
                # details, just "are we hearing anything at all".
                await self._redis.set(
                    _ws_last_msg_key("any"), str(now_ts), ex=300
                )
            except Exception:
                pass

        if event_type == "trade":
            await self._process_legacy_ws_trade(msg)
        elif event_type == "price_change":
            market_id = msg.get("market", "")
            changes = msg.get("price_changes", [])
            if market_id and changes and self._redis:
                try:
                    await self._redis.publish(
                        "market:price_changes",
                        json.dumps(
                            {
                                "market": market_id,
                                "changes": changes,
                                "ts": msg.get("timestamp"),
                            }
                        ),
                    )
                except Exception:
                    pass
                # FIX 7: Cache latest price per token in Redis (300s TTL)
                for change in changes:
                    token_id = change.get("asset_id", "")
                    price = change.get("price")
                    if token_id and price is not None:
                        try:
                            await self._redis.setex(
                                f"price:{market_id}:{token_id}", 300, str(price)
                            )
                        except Exception:
                            pass
        elif event_type == "book":
            await self._record_book_metrics(msg)

    @staticmethod
    def _event_timestamp_s(raw_ts: Any) -> float | None:
        if raw_ts is None:
            return None
        try:
            ts = float(raw_ts)
        except (TypeError, ValueError):
            return None
        if ts > 10_000_000_000:
            ts /= 1000.0
        return ts

    @staticmethod
    def _percentile(values: list[float], pct: float) -> float:
        if not values:
            return 0.0
        ordered = sorted(values)
        idx = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * pct)))
        return ordered[idx]

    @staticmethod
    def _level_price(levels: list | None) -> Decimal | None:
        if not levels:
            return None
        level = levels[0]
        try:
            if isinstance(level, dict):
                raw = level.get("price") or level.get("p")
            elif isinstance(level, (list, tuple)) and level:
                raw = level[0]
            else:
                raw = None
            return Decimal(str(raw)) if raw is not None else None
        except Exception:
            return None

    async def _persist_book_quality_snapshot(
        self,
        *,
        msg: dict,
        market_id: str,
        token_id: str,
        age_s: float,
        now_s: float,
        bids: list,
        asks: list,
        best_bid: Decimal | None,
        best_ask: Decimal | None,
        source_ts_s: float | None,
    ) -> None:
        mid_price = None
        spread_bps = None
        if best_bid is not None and best_ask is not None:
            mid_price = (best_bid + best_ask) / Decimal("2")
            if mid_price > 0:
                spread_bps = ((best_ask - best_bid) / mid_price) * Decimal("10000")

        source_timestamp = (
            datetime.fromtimestamp(source_ts_s, tz=timezone.utc) if source_ts_s is not None else None
        )
        observed_at = datetime.fromtimestamp(now_s, tz=timezone.utc)
        depth = {
            "bids": bids[:5],
            "asks": asks[:5],
            "bid_levels": len(bids),
            "ask_levels": len(asks),
        }
        raw_reference = {
            "event_type": msg.get("event_type"),
            "source_timestamp": msg.get("timestamp") or msg.get("time") or msg.get("ts"),
            "market": market_id,
            "asset_id": token_id,
        }
        try:
            async with get_db() as conn:
                await conn.execute(
                    """
                    INSERT INTO book_quality_snapshots
                        (market_id, token_id, book_age_ms, spread_bps,
                         best_bid, best_ask, mid_price, depth_top_levels,
                         gap_detected, source_timestamp, observed_at, raw_reference,
                         economic_model_version)
                    VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb,
                            $9, $10, $11, $12::jsonb, 'v1.0.0')
                    """,
                    market_id,
                    token_id,
                    int(round(age_s * 1000)),
                    spread_bps,
                    best_bid,
                    best_ask,
                    mid_price,
                    json.dumps(depth),
                    False,
                    source_timestamp,
                    observed_at,
                    json.dumps(raw_reference),
                )
        except Exception:
            logger.debug("Failed to persist book quality snapshot", exc_info=True)

    async def _record_book_metrics(self, msg: dict) -> None:
        if not self._redis:
            return
        now_s = time.time()
        ts_s = self._event_timestamp_s(msg.get("timestamp") or msg.get("time") or msg.get("ts"))
        age_s = 0.0 if ts_s is None else max(0.0, now_s - ts_s)
        self._book_age_samples.append(age_s)
        p95_s = self._percentile(list(self._book_age_samples), 0.95)
        try:
            await self._redis.setex("metrics:book_age_p95_s", 300, f"{p95_s:.3f}")
            market_id = str(msg.get("market") or msg.get("market_id") or "")
            token_id = str(msg.get("asset_id") or msg.get("token_id") or msg.get("asset") or "")
            if market_id and token_id:
                bids = msg.get("bids") or []
                asks = msg.get("asks") or []
                best_bid = self._level_price(bids)
                best_ask = self._level_price(asks)
                await self._redis.setex(
                    f"book:last:{market_id}:{token_id}",
                    300,
                    json.dumps(
                        {
                            "market_id": market_id,
                            "token_id": token_id,
                            "age_s": round(age_s, 3),
                            "book_age_p95_s": round(p95_s, 3),
                            "observed_ts": now_s,
                            "source_timestamp": msg.get("timestamp")
                            or msg.get("time")
                            or msg.get("ts"),
                            "best_bid": str(best_bid) if best_bid is not None else None,
                            "best_ask": str(best_ask) if best_ask is not None else None,
                            "bid_levels": len(bids),
                            "ask_levels": len(asks),
                            "source": "polymarket_market_ws",
                        }
                    ),
                )
                await self._persist_book_quality_snapshot(
                    msg=msg,
                    market_id=market_id,
                    token_id=token_id,
                    age_s=age_s,
                    now_s=now_s,
                    bids=bids,
                    asks=asks,
                    best_bid=best_bid,
                    best_ask=best_ask,
                    source_ts_s=ts_s,
                )
        except Exception:
            logger.debug("Failed to update book quality Redis metrics", exc_info=True)

    async def _process_legacy_ws_trade(self, msg: dict) -> None:
        """Handle legacy trade-shaped WS events when wallet attribution is present.

        The current market channel is primarily book/price data, but older tests and
        some feeds can still emit trade-shaped payloads. We accept them only when a
        wallet address is present.
        """
        maker = str(msg.get("maker_address") or "")
        taker = str(msg.get("taker_address") or "")
        wallet = maker if maker in self._leader_wallets else taker
        if not wallet:
            wallet = maker or taker
        if not wallet:
            return

        try:
            market_id = str(msg.get("market") or msg.get("market_id") or "")
            token_id = str(msg.get("asset_id") or msg.get("token_id") or msg.get("asset") or "")
            side = str(msg.get("side") or "").upper()
            price = Decimal(str(msg.get("price", 0)))
            size_shares = Decimal(str(msg.get("size", 0)))
            size_usdc = (size_shares * price).quantize(Decimal("0.01"))
            ts_int = int(msg.get("timestamp", 0))
            trade_time = datetime.fromtimestamp(
                ts_int / 1000 if ts_int > 1_000_000_000_000 else ts_int,
                tz=timezone.utc,
            )
        except (ValueError, TypeError) as exc:
            logger.debug(f"Bad legacy WS trade: {exc} | msg={msg}")
            return

        await self._process_trade(
            market_id=market_id,
            token_id=token_id,
            wallet_address=wallet,
            side=side,
            price=price,
            size_usdc=size_usdc,
            trade_time=trade_time,
            source="websocket",
            # WS arrival = "now" — the WS dispatch already happened, so
            # time.time() here is a tight proxy for "event observed". The
            # writer subtracts this from its own time.time() when it
            # observes `trade_ingestion_latency_seconds`.
            event_ts_s=time.time(),
        )

    async def _backfill_loop(self) -> None:
        """Poll data-api.polymarket.com every TRADE_OBSERVER_POLL_INTERVAL_S
        seconds for leader trades.

        HP-1 fix #1: cadence dropped from 30 s → 5 s by default. The interval
        is bounded by [TRADE_OBSERVER_POLL_INTERVAL_S_MIN,
        TRADE_OBSERVER_POLL_INTERVAL_S_MAX] in `settings` (validated at
        load), so the previous `max(5, …)` floor is no longer needed — the
        config layer enforces sane bounds and we honour the configured
        value verbatim.
        """
        while self._running and not self._stop_event.is_set():
            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=int(settings.TRADE_OBSERVER_POLL_INTERVAL_S),
                )
                break
            except asyncio.TimeoutError:
                pass
            if not self._running:
                break
            await self._backfill_from_data_api()

    async def _backfill_from_data_api(self) -> None:
        """Fetch leader and market activity from data-api.polymarket.com."""
        if not self._leader_wallets:
            return
        wallet_records = 0
        market_records = 0
        async with aiohttp.ClientSession() as session:
            wallet_records = await self._backfill_wallet_trades(session)
            market_records = await self._backfill_market_activity(session)
        total = wallet_records + market_records
        if total:
            logger.debug(
                "data-api backfill: processed "
                f"{wallet_records} leader-wallet trades + {market_records} market trades"
            )

    async def _backfill_from_falcon(self) -> None:
        """Compatibility backfill for older Falcon trade fixtures."""
        for wallet in list(self._leader_wallets):
            try:
                trades = await self._falcon.query(
                    556,
                    {"wallet_proxy": wallet},
                    limit=100,
                )
            except TypeError:
                trades = await self._falcon.query(556, {"wallet_proxy": wallet})
            except Exception as exc:
                logger.debug(f"Falcon compatibility backfill failed for {wallet}: {exc}")
                continue
            # Phase 3 Task D: a successful Falcon agent 556 fetch
            # heartbeats the `falcon_trades` source. The agent-id-based
            # heartbeat in FalconClient also fires, but call it here
            # too so a test that mocks `falcon.query` (skipping the
            # client's own wrapper) still proves liveness.
            _heartbeat_falcon_trades()
            for trade in trades or []:
                await self._process_falcon_trade(trade, wallet)

    async def _process_falcon_trade(self, trade: dict, wallet_address: str) -> None:
        """Compatibility parser for Falcon trade rows used by legacy tests."""
        try:
            market_id = str(trade.get("market_id") or trade.get("condition_id") or "")
            token_id = str(trade.get("token_id") or trade.get("asset") or "")
            side = str(trade.get("side") or "").upper()
            price = Decimal(str(trade.get("price", 0)))
            size_shares = Decimal(str(trade.get("size", 0)))
            size_usdc = (size_shares * price).quantize(Decimal("0.01"))
            ts_int = int(trade.get("timestamp", 0))
            trade_time = datetime.fromtimestamp(
                ts_int / 1000 if ts_int > 1_000_000_000_000 else ts_int,
                tz=timezone.utc,
            )
        except (ValueError, TypeError) as exc:
            logger.debug(f"Bad Falcon compatibility trade: {exc} | trade={trade}")
            return

        await self._process_trade(
            market_id=market_id,
            token_id=token_id,
            wallet_address=wallet_address,
            side=side,
            price=price,
            size_usdc=size_usdc,
            trade_time=trade_time,
            source="falcon",
            market_question_hint=trade.get("title") or trade.get("slug"),
            market_slug_hint=trade.get("slug"),
            outcome_hint=trade.get("outcome"),
            outcome_index=trade.get("outcome_index"),
        )

    async def _backfill_wallet_trades(self, session: aiohttp.ClientSession) -> int:
        """Per-wallet REST backfill, parallelised under a bounded semaphore.

        Phase 1 Task F (audit HP-1 fix #2) fanned this out to
        ``REGISTRY_BACKFILL_CONCURRENCY`` workers; Phase 3 Round 1
        (Agent A) further replaces the implicit "fetch latest 100 and
        Redis-dedupe" pattern with an explicit per-wallet **cursor**.
        Each wallet's cursor lives at
        ``observer:cursor:trades:api_wallet:<wallet>``. Two consequences:

        * If a wallet has been quiet, we still fetch its latest 100
          trades but immediately filter to "newer than the cursor head",
          so we don't re-enqueue old trades just to have the DB UNIQUE
          INDEX deduplicate them again.
        * If a wallet has been bursting, the same response shape catches
          the burst tail-end via natural cursor advancement.

        The cursor is updated AFTER the batch hands off to the queue
        (the writer's PG commit is the atomic boundary; we save the
        cursor head only after we've enqueued every new trade in the
        response). On crash mid-batch the next poll re-fetches and the
        UNIQUE INDEX absorbs the duplicates — zero data loss.
        """
        wallets = [w for w in self._leader_wallets if w]
        if not wallets:
            return 0
        if not self._running:
            return 0

        max_concurrency = max(1, int(settings.REGISTRY_BACKFILL_CONCURRENCY))
        sem = asyncio.Semaphore(max_concurrency)

        async def _backfill_one(wallet: str) -> int:
            if not self._running:
                return 0
            # Per-wallet cursor — even when the global market sweep is
            # running concurrently, a wallet's history is partitioned
            # by `proxyWallet=...` so the cursor is monotonic per wallet.
            cursor_ts, cursor_tx = await self._load_cursor(
                SOURCE_API_WALLET, scope=wallet
            )
            polling_cursor_lag_seconds.labels(source=SOURCE_API_WALLET).observe(
                max(0.0, time.time() - cursor_ts)
            )
            url = f"https://data-api.polymarket.com/trades?user={wallet}&limit=100"
            async with sem:
                if not self._running:
                    return 0
                try:
                    async with session.get(
                        url, timeout=aiohttp.ClientTimeout(total=8)
                    ) as resp:
                        if resp.status != 200:
                            return 0
                        _heartbeat_rest()
                        trades = await resp.json()
                except asyncio.TimeoutError:
                    logger.debug(f"data-api wallet backfill timeout for {wallet}")
                    return 0
                except Exception as e:
                    logger.debug(f"data-api wallet backfill failed for {wallet}: {e}")
                    return 0
            # Cursor filter — drop trades we've already seen. Saves
            # work in both _process_trade (Redis SET NX) and the writer
            # (DB tx + UNIQUE INDEX collision overhead).
            if isinstance(trades, list):
                new_trades = self._cursor_filter_new(
                    trades, cursor_ts, cursor_tx
                )
            else:
                new_trades = []
            response_ts_s = time.time()
            for trade in new_trades:
                await self._process_data_api_trade(
                    trade, source=SOURCE_API_WALLET, event_ts_s=response_ts_s
                )
            # Advance the cursor AFTER enqueueing every record. The
            # writer commits its PG transaction asynchronously; on
            # crash between enqueue and commit, the cursor IS advanced
            # but the next poll's UNIQUE INDEX dedupes the replay.
            if new_trades:
                head_ts, head_tx = self._cursor_head(new_trades)
                if head_ts > cursor_ts or (
                    head_ts == cursor_ts and head_tx and head_tx != cursor_tx
                ):
                    await self._save_cursor(
                        SOURCE_API_WALLET, head_ts, head_tx, scope=wallet
                    )
            return len(new_trades)

        results = await asyncio.gather(
            *(_backfill_one(w) for w in wallets), return_exceptions=True
        )
        processed = 0
        errors = 0
        for outcome in results:
            if isinstance(outcome, BaseException):
                errors += 1
                continue
            processed += int(outcome or 0)
        if errors:
            logger.debug(
                f"data-api wallet backfill: {processed} trades from "
                f"{len(wallets) - errors}/{len(wallets)} wallets ({errors} failed)"
            )
        return processed

    async def _backfill_market_activity(self, session: aiohttp.ClientSession) -> int:
        """Global market sweep against `data-api.polymarket.com/trades`.

        Two complementary mechanisms gate the work this method does on
        each poll:

        1. **ETag / If-Modified-Since** (Phase 1 HP-1 fix #1 supplement).
           Saves bandwidth on no-op polls.
        2. **Continuous cursor** (Phase 3 Round 1 / Agent A). Replaces
           the implicit "rely on Redis dedup" pattern with an explicit
           ``(ts, tx_hash)`` cursor at
           ``observer:cursor:trades:api_market``. Even when the server
           skips the 304 path and returns a 200 full body, we filter to
           trades strictly newer than the cursor head before doing any
           DB work. This eliminates the time-window edge cases where a
           trade landing exactly at the previous poll's boundary was
           silently dropped.

        If the server never returns ETag/Last-Modified, we log once at
        DEBUG and stop trying. The cursor is the real correctness
        primitive here; ETag is the bandwidth optimisation.
        """
        target_markets = await self._get_recent_leader_market_ids()
        if not target_markets:
            return 0

        cursor_ts, cursor_tx = await self._load_cursor(SOURCE_API_MARKET)
        polling_cursor_lag_seconds.labels(source=SOURCE_API_MARKET).observe(
            max(0.0, time.time() - cursor_ts)
        )

        processed = 0
        url = (
            "https://data-api.polymarket.com/trades"
            f"?limit={max(50, int(settings.DATA_API_GLOBAL_TRADES_LIMIT))}"
        )
        # Build conditional headers from cached validators. data-api may
        # return either ETag or Last-Modified (or neither); we send
        # whichever we have.
        headers: dict[str, str] = {}
        if self._last_etag:
            headers["If-None-Match"] = self._last_etag
        if self._last_modified:
            headers["If-Modified-Since"] = self._last_modified

        try:
            async with session.get(
                url,
                timeout=aiohttp.ClientTimeout(total=10),
                headers=headers or None,
            ) as resp:
                if resp.status == 304:
                    # No new trades — server confirms our cached snapshot
                    # is still current. Skip body parsing entirely.
                    trades_ingested_total.labels(
                        source="rest", result="not_modified"
                    ).inc()
                    # Phase 3 Task D: a 304 also proves liveness — the
                    # endpoint is responsive even if there's no new
                    # payload. Without this heartbeat, a quiet market
                    # period would flap the gap watchdog.
                    _heartbeat_rest()
                    logger.debug(
                        f"data-api market activity 304 Not Modified "
                        f"(etag={self._last_etag!r} last_mod={self._last_modified!r})"
                    )
                    return 0
                if resp.status != 200:
                    return 0
                # Phase 3 Task D: ingest-health heartbeat on 200.
                _heartbeat_rest()
                # Capture validators BEFORE consuming the body so a
                # mid-response error doesn't poison the cache with a bad
                # ETag.
                etag = resp.headers.get("ETag")
                last_modified = resp.headers.get("Last-Modified")
                if etag or last_modified:
                    self._etag_observed = True
                    self._last_etag = etag or self._last_etag
                    self._last_modified = last_modified or self._last_modified
                elif not self._etag_observed:
                    logger.debug(
                        "data-api: no ETag/Last-Modified on response; "
                        "conditional polling disabled"
                    )
                    # Mark observed=True so we only log this once.
                    self._etag_observed = True
                trades = await resp.json()
        except Exception as exc:
            logger.debug(f"data-api market activity fetch failed: {exc}")
            return 0

        # All trades in this response share the same observation timestamp
        # (the moment we received the response). Threading it through the
        # producer lets the writer measure end-to-end latency from "REST
        # response received" to "DB committed".
        response_ts_s = time.time()
        # Cursor filter first — the cheap path. Eliminates any trade
        # we've already enqueued in a previous poll. The DB UNIQUE
        # INDEX still backs us up if a crash interleaved cursor save
        # with batch commit, but in steady state this filter drops the
        # work the dedup path used to do.
        if isinstance(trades, list):
            new_trades = self._cursor_filter_new(trades, cursor_ts, cursor_tx)
        else:
            new_trades = []
        for trade in new_trades:
            market_id = str(trade.get("conditionId") or "")
            if not market_id or market_id not in target_markets:
                continue
            await self._process_data_api_trade(
                trade, source=SOURCE_API_MARKET, event_ts_s=response_ts_s
            )
            processed += 1
        # Advance the cursor across the WHOLE response, not just the
        # subset that matched target_markets — that guarantees the next
        # poll's filter doesn't keep re-finding the same off-watchlist
        # trades. If the response had nothing newer than the cursor,
        # _cursor_head returns (0.0, "") which we ignore.
        if new_trades:
            head_ts, head_tx = self._cursor_head(new_trades)
            if head_ts > cursor_ts or (
                head_ts == cursor_ts and head_tx and head_tx != cursor_tx
            ):
                await self._save_cursor(SOURCE_API_MARKET, head_ts, head_tx)
        return processed

    async def _get_recent_leader_market_ids(self) -> set[str]:
        if self._leader_condition_ids:
            try:
                async with get_db() as conn:
                    rows = await conn.fetch(
                        """
                        SELECT market_id
                        FROM (
                            SELECT market_id, MAX(time) AS last_seen
                            FROM trades_observed
                            WHERE is_leader = TRUE
                              AND source IS DISTINCT FROM 'onchain'
                            GROUP BY market_id
                            ORDER BY last_seen DESC
                            LIMIT $1
                        ) recent
                        """,
                        max(25, int(settings.DATA_API_RECENT_LEADER_MARKETS)),
                    )
                    recent = {str(r["market_id"]) for r in rows if r["market_id"]}
                    if recent:
                        self._leader_condition_ids.update(recent)
            except Exception as exc:
                logger.debug(f"Recent leader market lookup failed: {exc}")
            return set(self._leader_condition_ids)

        try:
            async with get_db() as conn:
                rows = await conn.fetch(
                    """
                    SELECT market_id
                    FROM (
                        SELECT market_id, MAX(time) AS last_seen
                        FROM trades_observed
                        WHERE is_leader = TRUE
                          AND source IS DISTINCT FROM 'onchain'
                        GROUP BY market_id
                        ORDER BY last_seen DESC
                        LIMIT $1
                    ) recent
                    """,
                    max(25, int(settings.DATA_API_RECENT_LEADER_MARKETS)),
                )
                self._leader_condition_ids.replace(
                    str(r["market_id"]) for r in rows if r["market_id"]
                )
        except Exception as exc:
            logger.debug(f"Leader market bootstrap failed: {exc}")
        return set(self._leader_condition_ids)

    async def _process_data_api_trade(
        self,
        trade: dict,
        source: str = SOURCE_API_WALLET,
        event_ts_s: float | None = None,
    ) -> None:
        """Parse and store a trade from data-api.polymarket.com.

        Response shape:
          {proxyWallet, side, asset (token_id), conditionId (market_id),
           size (shares), price, timestamp (seconds or ms)}

        `event_ts_s` is the wall-clock time the REST response was received
        (set by the caller, shared across all trades in one response). It
        feeds the `trade_ingestion_latency_seconds` histogram so we can
        prove HP-1 actually delivers the median 16 s → 2-3 s freshness cut.
        """
        try:
            wallet = trade.get("proxyWallet", "")
            if not wallet:
                return
            market_id = trade.get("conditionId", "")
            token_id = trade.get("asset", "")
            side = (trade.get("side") or "").upper()
            price = Decimal(str(trade.get("price", 0)))
            size_shares = float(trade.get("size", 0))
            size_usdc = Decimal(str(round(size_shares * float(price), 2)))
            ts_raw = trade.get("timestamp", 0)
            ts_int = int(ts_raw)
            # If > 1e10 it's milliseconds, otherwise seconds
            trade_time = datetime.fromtimestamp(
                ts_int / 1000 if ts_int > 1_000_000_000_000 else ts_int,
                tz=timezone.utc,
            )
        except (ValueError, TypeError) as e:
            logger.debug(f"Bad data-api trade: {e} | trade={trade}")
            return

        await self._process_trade(
            market_id=market_id,
            token_id=token_id,
            wallet_address=wallet,
            side=side,
            price=price,
            size_usdc=size_usdc,
            trade_time=trade_time,
            source=source,
            market_question_hint=trade.get("title"),
            market_slug_hint=trade.get("slug") or trade.get("eventSlug"),
            outcome_hint=trade.get("outcome"),
            outcome_index=trade.get("outcomeIndex"),
            event_ts_s=event_ts_s,
        )

    def _dedup_key(
        self,
        wallet: str,
        market_id: str,
        trade_time: datetime,
        side: str,
        price: Decimal,
        size_usdc: Decimal,
    ) -> str:
        day = trade_time.strftime("%Y%m%d")
        bucket = int(trade_time.timestamp() // 1)  # 1-second buckets
        raw = f"{bucket}:{side}:{price}:{size_usdc}"
        h = hashlib.md5(raw.encode()).hexdigest()[:12]
        return f"{DEDUP_KEY_PREFIX}:{wallet}:{market_id}:{day}:{h}"

    async def _is_duplicate(self, key: str) -> bool:
        result = await self._redis.set(key, "1", ex=DEDUP_TTL_S, nx=True)
        return result is None  # nx=True returns None if key already exists

    async def _clear_dedup_key(self, key: str) -> None:
        deleter = getattr(self._redis, "delete", None)
        if not callable(deleter):
            return
        try:
            await deleter(key)
        except Exception:
            pass

    async def _trade_exists(
        self,
        market_id: str,
        wallet_address: str,
        trade_time: datetime,
        side: str,
        price: Decimal,
        size_usdc: Decimal,
    ) -> bool:
        try:
            async with get_db() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT 1
                    FROM trades_observed
                    WHERE market_id = $1
                      AND wallet_address = $2
                      AND time = $3
                      AND side = $4
                      AND price = $5
                      AND size_usdc = $6
                    LIMIT 1
                    """,
                    market_id,
                    wallet_address,
                    trade_time,
                    side,
                    price,
                    size_usdc,
                )
                return row is not None
        except Exception:
            return False

    async def _process_trade(
        self,
        market_id: str,
        token_id: str,
        wallet_address: str,
        side: str,
        price: Decimal,
        size_usdc: Decimal,
        trade_time: datetime,
        source: str,
        market_question_hint: str | None = None,
        market_slug_hint: str | None = None,
        outcome_hint: str | None = None,
        outcome_index: int | None = None,
        event_ts_s: float | None = None,
    ) -> None:
        """Producer entry point.

        HP-1 fix #3: previously this method ran 3-7 DB roundtrips synchronously
        for every observed trade, serialising ingestion through the same
        coroutine that owned the WS / REST loop. We now do only Redis-fast
        work here (input validation, dedup, leader-set lookup, pure-Python
        category inference) and hand the rest to a bounded queue drained by
        `_db_writer_loop`. The queue's bound (`TRADE_OBSERVER_QUEUE_MAX`)
        plus a 1 s `wait_for` timeout on `put()` give us visible
        backpressure: under sustained DB stalls we drop trades and bump
        `observer_queue_drops_total` rather than block the WS pong loop and
        cascade-disconnect.
        """
        if not market_id or not wallet_address:
            return

        dedup_key = self._dedup_key(wallet_address, market_id, trade_time, side, price, size_usdc)
        if await self._is_duplicate(dedup_key):
            # NOTE (audit HP-1 fix #5 deferred): the previous implementation
            # ran a `_trade_exists` DB probe here for SOURCE_API_MARKET hits
            # to recover from cold-Redis false positives. That probe is
            # exactly the kind of synchronous DB call the new producer must
            # avoid — and the unique index on `trades_observed` already
            # protects us at write time. We therefore short-circuit on a
            # Redis hit unconditionally; the cold-start window is bounded
            # to one poll cycle (5 s), which is acceptable. If the probe
            # ever needs to come back, do it on a background task — never
            # in the producer.
            return

        is_leader = wallet_address in self._leader_wallets
        if is_leader:
            self._leader_condition_ids.add(market_id)

        # Pure-Python category refine. The writer can still upgrade this if
        # the markets row contains a better label after the fact.
        inferred_category = _infer_market_category(
            market_question_hint, market_slug_hint
        )

        record = _TradeRecord(
            market_id=market_id,
            token_id=token_id,
            wallet_address=wallet_address,
            side=side,
            price=price,
            size_usdc=size_usdc,
            trade_time=trade_time,
            source=source,
            is_leader=is_leader,
            dedup_key=dedup_key,
            event_ts_s=float(event_ts_s) if event_ts_s is not None else time.time(),
            market_question_hint=market_question_hint,
            market_slug_hint=market_slug_hint,
            outcome_hint=outcome_hint,
            outcome_index=outcome_index,
            inferred_category=inferred_category,
        )

        queue = self._ensure_write_queue()
        try:
            await asyncio.wait_for(queue.put(record), timeout=QUEUE_PUT_TIMEOUT_S)
        except asyncio.TimeoutError:
            # Backpressure: writer can't keep up. We've already burned the
            # Redis dedup slot (NX SET succeeded), so to keep idempotency
            # honest we clear it — otherwise a retry within DEDUP_TTL_S
            # would silently swallow this trade.
            await self._clear_dedup_key(dedup_key)
            observer_queue_drops_total.labels(reason="queue_full").inc()
            logger.warning(
                f"observer queue full ({queue.qsize()}/{queue.maxsize}); "
                f"dropping trade wallet={wallet_address[:10]}… "
                f"market={market_id[:10]}… source={source}"
            )

    async def _repair_market_from_trade_hint(
        self,
        conn,
        market_id: str,
        token_id: str,
        trade_time: datetime,
        market_row: Any,
        market_question_hint: str | None = None,
        market_slug_hint: str | None = None,
        outcome_hint: str | None = None,
        outcome_index: int | None = None,
    ) -> dict:
        row = {
            "question": _row_value(market_row, "question"),
            "category": _row_value(market_row, "category"),
            "token_yes": _row_value(market_row, "token_yes"),
            "token_no": _row_value(market_row, "token_no"),
            "end_date": _row_value(market_row, "end_date"),
        }
        if not market_question_hint and outcome_index is None and not outcome_hint:
            return row

        question_hint = str(market_question_hint or "").strip()
        current_question = str(row.get("question") or "").strip()
        current_category = str(row.get("category") or "").strip() or "unknown"
        current_end_date = row.get("end_date")
        inferred_category = _infer_market_category(
            question_hint or current_question,
            market_slug_hint,
        )
        token_yes_hint, token_no_hint = _token_hints_from_trade(
            token_id=token_id,
            outcome_index=outcome_index,
            outcome_name=outcome_hint,
        )

        stale_end_date = bool(
            current_end_date and trade_time > current_end_date + timedelta(minutes=5)
        )
        should_refresh_question = bool(
            question_hint
            and (
                not current_question
                or current_question.startswith("Market ")
                or current_question != question_hint
            )
        )
        should_refresh_category = inferred_category != "unknown" and (
            current_category.lower() in {"", "unknown", "none", "null"}
            or stale_end_date
            or should_refresh_question
        )
        should_refresh_tokens = bool(
            (token_yes_hint and row.get("token_yes") != token_yes_hint)
            or (token_no_hint and row.get("token_no") != token_no_hint)
        )

        should_skip_refresh = not any(
            (
                should_refresh_question,
                should_refresh_category,
                should_refresh_tokens,
                stale_end_date,
            )
        )
        if should_skip_refresh:
            return row

        question_value = question_hint or current_question or f"Market {market_id[:30]}…"
        category_value = (
            inferred_category if should_refresh_category else current_category or "unknown"
        )
        token_yes_value = token_yes_hint or row.get("token_yes")
        token_no_value = token_no_hint or row.get("token_no")

        await conn.execute(
            """
            INSERT INTO markets (
                market_id, question, category, token_yes, token_no, end_date, updated_at
            )
            VALUES ($1, $2, $3, $4, $5, NULL, NOW())
            ON CONFLICT (market_id) DO UPDATE SET
                question = EXCLUDED.question,
                category = CASE
                    WHEN EXCLUDED.category IS NOT NULL AND EXCLUDED.category <> 'unknown'
                    THEN EXCLUDED.category
                    ELSE markets.category
                END,
                token_yes = COALESCE(EXCLUDED.token_yes, markets.token_yes),
                token_no = COALESCE(EXCLUDED.token_no, markets.token_no),
                end_date = CASE WHEN $6 THEN NULL ELSE markets.end_date END,
                updated_at = NOW()
            """,
            market_id,
            question_value,
            category_value,
            token_yes_value,
            token_no_value,
            stale_end_date,
        )
        self._market_meta_cache[market_id] = datetime.now(tz=timezone.utc).timestamp()
        return {
            "question": question_value,
            "category": category_value,
            "token_yes": token_yes_value,
            "token_no": token_no_value,
            "end_date": None if stale_end_date else current_end_date,
        }

    def _needs_market_enrichment(self, market_id: str, market_row: Any) -> bool:
        question = str(_row_value(market_row, "question", "") or "").strip()
        category = str(_row_value(market_row, "category", "") or "").strip().lower()
        token_yes = str(_row_value(market_row, "token_yes", "") or "").strip()
        token_no = str(_row_value(market_row, "token_no", "") or "").strip()
        now_ts = datetime.now(tz=timezone.utc).timestamp()
        last_fetch = float(self._market_meta_cache.get(market_id, 0.0) or 0.0)
        if now_ts - last_fetch < MARKET_META_TTL_S:
            return False
        # Question/category placeholders → always enrich.
        if not question or question.startswith("Market "):
            return True
        if category in {"", "unknown", "none", "null"}:
            return True
        # Token mapping is required for the market scanner to display the
        # market by name and compute YES/NO direction. Without it the row
        # shows up in hex and the decision engine can't price-anchor.
        if not token_yes or not token_no:
            return True
        return False

    async def _fetch_market_metadata_from_gamma(self, market_id: str, token_id: str) -> dict | None:
        url = "https://gamma-api.polymarket.com/markets"
        params_options = [{"conditionId": market_id, "limit": 1}]
        if token_id:
            params_options.append({"clobTokenIds": token_id, "limit": 1})
        async with aiohttp.ClientSession() as session:
            for params in params_options:
                async with session.get(
                    url,
                    params=params,
                    timeout=aiohttp.ClientTimeout(total=8),
                ) as resp:
                    if resp.status != 200:
                        continue
                    items = await resp.json()
                    if not isinstance(items, list) or not items:
                        continue
                    market = items[0]
                    if not _gamma_market_matches_request(market, market_id, token_id):
                        logger.debug(
                            f"Gamma market mismatch for {market_id}: "
                            f"returned conditionId={market.get('conditionId')}"
                        )
                        continue
                    tokens = _normalize_token_ids(market.get("clobTokenIds"))
                    end_date_raw = market.get("endDateIso") or market.get("endDate")
                    try:
                        end_date = (
                            datetime.fromisoformat(end_date_raw.replace("Z", "+00:00"))
                            if end_date_raw
                            else None
                        )
                    except Exception:
                        end_date = None
                    question = (
                        market.get("question") or market.get("title") or f"Market {market_id[:30]}…"
                    )
                    # R-12 fix (audit 01_data_inventory.md): previously this
                    # block read `makerBaseFee` and stored it as the market's
                    # generic `fee_rate_pct`, which is then consumed by
                    # `calculate_polymarket_fee(..., liquidity_role=TAKER)` in
                    # `paper_trader.open_trade/close_trade` and
                    # `position_tracker._close_position`. Polymarket's gamma
                    # API documents `makerBaseFee` as the MAKER fee and
                    # `takerBaseFee` as the TAKER fee (both in bps). Using
                    # the maker value as the taker rate systematically
                    # under-estimates trade costs and over-states PnL.
                    # Prefer `takerBaseFee`; fall back to `makerBaseFee` only
                    # so existing rows don't suddenly read NULL. TODO: once
                    # `fee_snapshots` (migration 003) is wired up
                    # (audit R-1) and the CLOB taker fee is sourced
                    # directly, this fallback can go away.
                    taker_fee_raw = (
                        market.get("takerBaseFee")
                        or market.get("taker_base_fee")
                    )
                    maker_fee_raw = (
                        market.get("makerBaseFee")
                        or market.get("maker_base_fee")
                    )
                    if taker_fee_raw is not None:
                        gamma_taker_fee_bps = float(taker_fee_raw)
                    elif maker_fee_raw is not None:
                        # Documented degradation: maker fee used as a proxy
                        # for taker until fee_snapshots lands. Log so we can
                        # see the coverage gap in observability.
                        logger.debug(
                            f"Gamma market {market_id}: takerBaseFee missing, "
                            f"falling back to makerBaseFee={maker_fee_raw} as TAKER proxy"
                        )
                        gamma_taker_fee_bps = float(maker_fee_raw)
                    else:
                        gamma_taker_fee_bps = float(
                            market.get("baseFee") or market.get("fee") or 0.0
                        )
                    # Plan 2026-05-19 P4-1 — bps↔decimal unit fix.
                    # Gamma's taker_base_fee field is in BASIS POINTS
                    # (156 = 1.56%). Downstream consumers
                    # (calculate_polymarket_fee in src/economics/fees.py)
                    # expect a DECIMAL fraction (0.0156 for 1.56%). The
                    # legacy column stored bps directly, multiplying paper-
                    # trade fees by 10000× on every crypto/sport-fee
                    # market. Convert at write time so all DB readers see
                    # a consistent decimal value. Migration 054 normalises
                    # pre-existing rows. Bound at [0, 1] to defend against
                    # a degenerate Gamma payload.
                    gamma_taker_fee_decimal = max(
                        0.0, min(1.0, gamma_taker_fee_bps / 10_000.0)
                    )
                    return {
                        "question": question,
                        "category": market.get("category") or "unknown",
                        "token_yes": tokens[0] if len(tokens) > 0 else None,
                        "token_no": tokens[1] if len(tokens) > 1 else None,
                        "end_date": end_date,
                        "volume_24h": float(market.get("volume24hr") or 0.0),
                        "liquidity_score": float(market.get("liquidity") or 0.0),
                        # Stored in `markets.fee_rate_pct` as a DECIMAL
                        # fraction (Plan P4-1 fix). Previously stored bps
                        # directly — the column name predates the unit
                        # standardisation and is kept for compatibility.
                        "fee_rate_pct": gamma_taker_fee_decimal,
                    }
        return None
