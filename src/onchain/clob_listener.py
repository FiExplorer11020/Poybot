"""Subscribes to Polymarket CLOB contract events on Polygon.

Wave-2 implementation. See docs/ROUND_6_THE_SPINE.md § 3.3.

Lifecycle
---------

The listener is the long-lived inhabitant of the
``polymarket-onchain.service`` systemd unit. ``start()`` is called once
on boot, ``stop()`` once on SIGTERM; the subscription loop runs forever
between the two and self-heals across transient RPC drops via the
:class:`RPCClient`'s eth_subscribe reconnect logic.

Transaction discipline
----------------------

Per Phase 0 — every ``trades_observed`` insert and every
``chain_sync_state`` UPSERT runs inside ``async with conn.transaction():``.
Stream publishes happen AFTER the transaction commits so pub/sub never
advertises an uncommitted state.

Dedup contract: the chain-source ``ON CONFLICT (tx_hash, log_index)
WHERE tx_hash IS NOT NULL AND log_index IS NOT NULL DO NOTHING`` clause
relies on the partial UNIQUE INDEX from migration 021. A replayed event
is a clean no-op at the DB layer.
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING, Any

from loguru import logger

from src.config import settings
from src.control.redis_streams import StreamProducer
from src.database.connection import get_db
from src.onchain.clob_abi import TRADE_EVENT_TOPICS
from src.onchain.event_decoder import EventDecoder
from src.onchain.models import (
    TRADE_EVENT_TYPES,
    DecodedEvent,
    FeeRateUpdatedEvent,
    OrderCancelledEvent,
    OrderFilledEvent,
    OrdersMatchedEvent,
)

if TYPE_CHECKING:
    from src.rpc.client import RPCClient


CHAIN_TRADES_STREAM = "chain:trades:stream"
CHAIN_GOV_STREAM = "chain:gov:stream"

# Sentinel singleton id for chain_sync_state (migration 022).
_SYNC_STATE_SINGLETON_ID = "singleton"


def _metric(name: str) -> Any:
    """Best-effort metric lookup. Returns None if metrics module is
    missing — the listener still works without metrics.
    """
    try:
        from src.monitoring import metrics as _metrics

        return getattr(_metrics, name, None)
    except Exception:
        return None


def _safe_inc(metric: Any, **labels: str) -> None:
    if metric is None:
        return
    try:
        if labels:
            metric.labels(**labels).inc()
        else:
            metric.inc()
    except Exception:
        pass


def _safe_set(metric: Any, value: float) -> None:
    if metric is None:
        return
    try:
        metric.set(value)
    except Exception:
        pass


def _safe_observe(metric: Any, value: float) -> None:
    if metric is None:
        return
    try:
        metric.observe(value)
    except Exception:
        pass


class CLOBChainListener:
    """Subscribes to Polymarket CLOB contract events on Polygon.

    Args:
        rpc_client: Pre-constructed :class:`src.rpc.client.RPCClient`.
            Injected for testability — the listener depends only on the
            ``eth_subscribe`` async-iterator surface.
        redis_url: Redis URL for the chain:trades:stream
            :class:`StreamProducer`. The listener owns the producer's
            lifecycle (start in :meth:`start`, stop in :meth:`stop`).
        contract_address: 0x-prefixed CTF Exchange address. Defaults to
            the production Polygon mainnet address pinned in config.
        stream_producer: Test escape hatch — inject a pre-built producer
            (typically a fake) instead of constructing one from
            ``redis_url``. Production code never passes this.
        gov_stream_producer: Optional secondary stream for governance
            events (FeeRateUpdated). Defaults to a separate
            ``chain:gov:stream`` producer.
    """

    def __init__(
        self,
        rpc_client: "RPCClient",
        redis_url: str,
        contract_address: str | None = None,
        *,
        stream_producer: StreamProducer | None = None,
        gov_stream_producer: StreamProducer | None = None,
    ) -> None:
        self._rpc = rpc_client
        self._redis_url = redis_url
        self._contract_address = (
            contract_address or settings.POLYMARKET_CLOB_CONTRACT_ADDRESS
        )
        self._decoder = EventDecoder()

        # Stream producers. If the caller injected one we don't own its
        # lifecycle (test fixtures clean themselves up).
        self._trades_stream: StreamProducer | None = stream_producer
        self._owns_trades_stream = stream_producer is None
        self._gov_stream: StreamProducer | None = gov_stream_producer
        self._owns_gov_stream = gov_stream_producer is None

        # Filter for eth_subscribe. Topic-0 list is OR'd so one
        # subscription captures every fill / match / cancel event.
        self._filter = {
            "address": self._contract_address,
            "topics": [list(TRADE_EVENT_TOPICS)],
        }

        # Cursor + commit cadence state.
        self._last_processed_block: int = 0
        self._last_committed_block: int = 0
        self._last_commit_ts: float = 0.0
        self._batch_commit_blocks = int(
            getattr(settings, "CHAIN_BATCH_COMMIT_BLOCKS", 50)
        )
        self._batch_commit_interval_s = float(
            getattr(settings, "CHAIN_BATCH_COMMIT_INTERVAL_S", 5.0)
        )
        self._bootstrap_lookback = int(
            getattr(settings, "CHAIN_BOOTSTRAP_LOOKBACK_BLOCKS", 256)
        )

        # Loop state.
        self._running = False
        self._subscription_task: asyncio.Task | None = None
        self._chain_head_cache: tuple[int, float] = (0, 0.0)
        self._chain_head_cache_ttl_s = 2.0

    # ------------------------------------------------------------------ #
    # Lifecycle                                                           #
    # ------------------------------------------------------------------ #

    async def start(self) -> None:
        """Boot the listener.

        1. Open the trades-stream + gov-stream producers (if we own them).
        2. Load the last_processed_block cursor from chain_sync_state.
           If empty, fall back to head - bootstrap_lookback.
        3. Spawn the subscription loop as a task.
        """
        if self._running:
            return
        if self._trades_stream is None:
            self._trades_stream = StreamProducer(
                self._redis_url,
                CHAIN_TRADES_STREAM,
                name="onchain.trades",
            )
            await self._trades_stream.start()
        if self._gov_stream is None:
            self._gov_stream = StreamProducer(
                self._redis_url,
                CHAIN_GOV_STREAM,
                name="onchain.gov",
            )
            await self._gov_stream.start()

        self._last_processed_block = await self._load_sync_state()
        self._last_committed_block = self._last_processed_block
        self._last_commit_ts = time.monotonic()
        logger.info(
            f"CLOBChainListener: starting from block={self._last_processed_block} "
            f"contract={self._contract_address}"
        )

        self._running = True
        self._subscription_task = asyncio.create_task(
            self._run_subscription_loop(), name="onchain:subscription"
        )

    async def stop(self) -> None:
        """Graceful shutdown.

        Cancels the subscription loop, persists the final cursor, closes
        any owned stream producers. Idempotent.
        """
        if not self._running:
            return
        self._running = False
        task = self._subscription_task
        self._subscription_task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

        # Persist final cursor (best-effort — a crash here just means the
        # next boot re-processes a handful of blocks, which is safe).
        if self._last_processed_block > self._last_committed_block:
            try:
                await self._update_sync_state(self._last_processed_block)
            except Exception as exc:
                logger.warning(
                    f"CLOBChainListener: final cursor flush failed: {exc!r}"
                )

        if self._owns_trades_stream and self._trades_stream is not None:
            await self._trades_stream.stop()
        if self._owns_gov_stream and self._gov_stream is not None:
            await self._gov_stream.stop()
        self._trades_stream = None
        self._gov_stream = None
        logger.info("CLOBChainListener: stopped")

    async def __aenter__(self) -> "CLOBChainListener":
        await self.start()
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        await self.stop()

    # ------------------------------------------------------------------ #
    # Hot path                                                            #
    # ------------------------------------------------------------------ #

    async def _run_subscription_loop(self) -> None:
        """Long-lived consumer of :meth:`RPCClient.eth_subscribe`.

        For each log:
          * Decode → publish → conditionally commit cursor.
          * Always emit ``chain_blocks_processed_total`` and
            ``chain_ingestion_latency_seconds``.

        On RPCClient disconnect: the eth_subscribe iterator handles
        reconnect internally. If it raises a non-cancellation error we
        log and exit — the systemd supervisor restarts the process and
        the cursor catches up via the chain_sync_state replay path.
        """
        try:
            async for raw_log in self._rpc.eth_subscribe(self._filter):
                if not self._running:
                    break
                await self._process_log(raw_log)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                f"CLOBChainListener: subscription loop ended: {exc!r}"
            )
            # Best-effort latency-bookkeeping metric so dashboards see
            # the drop. We don't re-raise — the supervisor restarts us.
            _safe_inc(
                _metric("chain_events_failed_decode_total"),
                event_type="subscription",
                reason="disconnect",
            )

    async def _process_log(self, raw_log: dict) -> None:
        """Decode one raw log and route it through the publish path."""
        t0 = time.monotonic()
        decoded = self._decoder.decode_any(raw_log)
        if decoded is None:
            return
        try:
            await self._publish_event(decoded)
        except Exception as exc:
            # Publish failures must not kill the loop. We bump a metric
            # and continue — the cursor is NOT advanced past this block,
            # so a future replay (via crash or operator) picks it up.
            logger.warning(
                f"CLOBChainListener: publish failed for "
                f"{decoded.event_type} tx={decoded.tx_hash} "
                f"log_index={decoded.log_index}: {exc!r}"
            )
            _safe_inc(
                _metric("chain_events_failed_decode_total"),
                event_type=decoded.event_type,
                reason="publish_failed",
            )
            return

        _safe_inc(
            _metric("chain_events_decoded_total"),
            event_type=decoded.event_type,
        )

        # Block-level bookkeeping — count each block we cross at most
        # once per call (multiple events in the same block don't multi-
        # count the block, that would inflate the dashboard gauge).
        if decoded.block_number > self._last_processed_block:
            self._last_processed_block = decoded.block_number
            _safe_inc(_metric("chain_blocks_processed_total"))

        # Ingestion latency: from block timestamp to publish completion.
        if decoded.block_time:
            _safe_observe(
                _metric("chain_ingestion_latency_seconds"),
                max(0.0, time.time() - decoded.block_time),
            )

        # Commit the cursor on either block-count or wall-clock cadence.
        await self._maybe_commit_cursor()

        # Per-tick latency observation (writer-side, before-vs-after
        # publish). Useful for dashboards even when block_time is absent.
        _safe_observe(
            _metric("chain_ingestion_latency_seconds"),
            max(0.0, time.monotonic() - t0),
        )

    async def _maybe_commit_cursor(self) -> None:
        """Persist the cursor if either threshold is met."""
        blocks_since_commit = (
            self._last_processed_block - self._last_committed_block
        )
        elapsed = time.monotonic() - self._last_commit_ts
        if (
            blocks_since_commit >= self._batch_commit_blocks
            or elapsed >= self._batch_commit_interval_s
        ):
            try:
                await self._update_sync_state(self._last_processed_block)
                self._last_committed_block = self._last_processed_block
                self._last_commit_ts = time.monotonic()
            except Exception as exc:
                # Commit failure isn't fatal — we'll retry on the next
                # event. But we DO log loudly because a sustained DB
                # outage means our cursor lies behind the published
                # stream, and a crash here loses idempotency.
                logger.warning(
                    f"CLOBChainListener: cursor commit failed: {exc!r}"
                )

    # ------------------------------------------------------------------ #
    # DB writers                                                          #
    # ------------------------------------------------------------------ #

    async def _publish_event(self, decoded: DecodedEvent) -> None:
        """Two-destination publish: DB UPSERT then Redis Stream.

        Ordering invariant (Phase 0): the stream publish ONLY runs after
        the DB transaction commits. A crash between the two is safe
        because the next replay sees the row already in trades_observed
        and the ON CONFLICT path makes the stream re-publish idempotent
        (downstream consumers join on tx_hash, log_index).
        """
        # 1. DB write for trade events. Non-trade events skip the table.
        if decoded.event_type in TRADE_EVENT_TYPES:
            async with get_db() as conn:
                async with conn.transaction():
                    await self._insert_trade(conn, decoded)
        # 2. Stream publish (after commit).
        await self._publish_to_stream(decoded)

    async def _insert_trade(self, conn: Any, decoded: DecodedEvent) -> None:
        """INSERT … ON CONFLICT DO NOTHING into trades_observed.

        The chain decoder doesn't yet resolve maker_asset_id → market_id
        and the side/price/size derivation (that's Wave-3's economic
        decoder). For now we write the wallet attribution + raw amounts
        + the tx-identity tuple. Downstream Wave-3 work fills in the
        price/size economics by joining against ConditionalTokens.

        Schema columns we touch:
          time, market_id, token_id, wallet_address, side, price,
          size_usdc, source='onchain', block_number, tx_hash, log_index.

        ``time`` falls back to NOW() when block_time is 0; the partial
        UNIQUE INDEX is on (tx_hash, log_index), not on time, so a
        slightly-off timestamp doesn't break dedup.
        """
        # Pick the "wallet" depending on the event shape. For OrdersMatched
        # the taker is the active trader; for OrderFilled the maker is the
        # resting-order wallet (most attribution-relevant).
        if isinstance(decoded, OrderFilledEvent):
            wallet = decoded.maker
            # Provisional market_id/token_id: use the maker_asset_id as
            # the token_id stand-in until the Wave-3 economic decoder
            # ships the asset_id → market mapping.
            token_id = str(decoded.maker_asset_id)
            market_id = token_id
            size_raw = decoded.taker_amount_filled
            price = 0  # filled by Wave-3
            side = "buy"
        elif isinstance(decoded, OrdersMatchedEvent):
            wallet = decoded.taker_order_maker
            token_id = str(decoded.maker_asset_id)
            market_id = token_id
            size_raw = decoded.taker_amount_filled
            price = 0
            side = "buy"
        else:
            # Non-trade event slipped through — guard so we never write
            # a stub row for cancels.
            return

        # Convert size from raw uint to USDC (Polymarket uses 6-decimal
        # USDC). For provisional rows we accept a coarse approximation.
        size_usdc = float(size_raw) / 1_000_000.0 if size_raw else 0.0

        # ``time`` from block_time when available, else server NOW().
        # asyncpg accepts a Python datetime for TIMESTAMPTZ columns. We
        # build one from the unix timestamp so the partition router (the
        # table is RANGE-partitioned by time, migration 013) can place
        # the row in the right child.
        from datetime import datetime, timezone

        if decoded.block_time:
            trade_time = datetime.fromtimestamp(
                float(decoded.block_time), tz=timezone.utc
            )
        else:
            trade_time = datetime.now(tz=timezone.utc)

        await conn.execute(
            """
            INSERT INTO trades_observed
                (time, market_id, token_id, wallet_address,
                 side, price, size_usdc, source, is_leader,
                 block_number, tx_hash, log_index)
            VALUES
                ($1, $2, $3, $4, $5, $6, $7, 'onchain', FALSE,
                 $8, $9, $10)
            ON CONFLICT (tx_hash, log_index)
                WHERE tx_hash IS NOT NULL AND log_index IS NOT NULL
                DO NOTHING
            """,
            trade_time,
            market_id,
            token_id,
            wallet,
            side,
            price,
            size_usdc,
            decoded.block_number,
            decoded.tx_hash,
            decoded.log_index,
        )

    async def _publish_to_stream(self, decoded: DecodedEvent) -> None:
        """Publish the event to the appropriate Redis Stream.

        Trade events → chain:trades:stream.
        Non-trade events (FeeRateUpdated) → chain:gov:stream.

        The :class:`StreamProducer` injects ``trace_id`` and
        ``published_at_ms`` for end-to-end traceability.
        """
        producer: StreamProducer | None
        if isinstance(decoded, (OrderFilledEvent, OrdersMatchedEvent, OrderCancelledEvent)):
            producer = self._trades_stream
        elif isinstance(decoded, FeeRateUpdatedEvent):
            producer = self._gov_stream
        else:
            return
        if producer is None:
            # Producer not configured — caller chose not to wire one.
            return
        payload = decoded.to_dict()
        await producer.publish(payload)

    # ------------------------------------------------------------------ #
    # Sync-state cursor                                                   #
    # ------------------------------------------------------------------ #

    async def _load_sync_state(self) -> int:
        """Read the last-processed block from chain_sync_state.

        Returns the saved cursor on hit, or a bootstrap block on miss.
        Bootstrap = current head - CHAIN_BOOTSTRAP_LOOKBACK_BLOCKS so we
        replay a window of recent history when chain_sync_state is empty.
        """
        try:
            async with get_db() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT last_processed_block
                    FROM chain_sync_state
                    WHERE id = $1
                    """,
                    _SYNC_STATE_SINGLETON_ID,
                )
        except Exception as exc:
            logger.warning(
                f"CLOBChainListener: failed to load sync state: {exc!r}"
            )
            row = None

        if row is not None and row["last_processed_block"] is not None:
            return int(row["last_processed_block"])

        # Bootstrap path.
        head = await self._fetch_chain_head()
        return max(0, head - self._bootstrap_lookback)

    async def _update_sync_state(self, block_number: int) -> None:
        """Single-row UPSERT into chain_sync_state.

        Called from the hot loop's commit-cadence path. Wrapped in a
        transaction so a crash between the trade INSERTs and this UPDATE
        leaves the cursor pointing at the LAST durably committed batch,
        not ahead of it (replay invariant — see migration 022 docstring).
        """
        async with get_db() as conn:
            async with conn.transaction():
                await conn.execute(
                    """
                    INSERT INTO chain_sync_state
                        (id, last_processed_block, last_updated_at)
                    VALUES ($1, $2, NOW())
                    ON CONFLICT (id) DO UPDATE
                        SET last_processed_block = EXCLUDED.last_processed_block,
                            last_updated_at      = EXCLUDED.last_updated_at
                    """,
                    _SYNC_STATE_SINGLETON_ID,
                    int(block_number),
                )

    # ------------------------------------------------------------------ #
    # Chain head + blocks-behind                                          #
    # ------------------------------------------------------------------ #

    async def _fetch_chain_head(self) -> int:
        """Cache ``eth_blockNumber`` for ~2s to avoid hammering RPC."""
        now = time.monotonic()
        cached_head, cached_ts = self._chain_head_cache
        if now - cached_ts < self._chain_head_cache_ttl_s and cached_head:
            return cached_head
        try:
            block = await self._rpc.eth_getBlockByNumber("latest")
        except Exception as exc:
            logger.debug(
                f"CLOBChainListener: chain-head fetch failed: {exc!r}"
            )
            return cached_head  # may be 0 — caller treats as "unknown"
        head_raw = (
            (block or {}).get("number")
            or (block or {}).get("block_number")
            or 0
        )
        if isinstance(head_raw, str):
            try:
                head = int(head_raw, 16) if head_raw.startswith(("0x", "0X")) else int(head_raw)
            except ValueError:
                head = cached_head
        else:
            head = int(head_raw or 0)
        self._chain_head_cache = (head, now)
        return head

    async def chain_blocks_behind(self) -> int:
        """Return ``chain_head - last_processed_block``.

        Drives the ``polybot_chain_blocks_behind`` gauge. Returns 0 if
        the chain-head fetch fails (no false alarms from RPC hiccups).
        """
        head = await self._fetch_chain_head()
        if not head:
            return 0
        behind = max(0, head - self._last_processed_block)
        _safe_set(_metric("chain_blocks_behind"), behind)
        return behind
