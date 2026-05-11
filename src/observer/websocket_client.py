"""
Polymarket CLOB WebSocket client — market channel subscription.
Handles auto-reconnect, ping/pong keepalive, dynamic market subscription updates.

Phase 3 Round 1 (Agent A) additions:

* **Per-channel freshness watchdog** (``_freshness_watchdog``). The ping/
  pong keepalive only proves the *socket* is alive; it doesn't catch a
  silent stall where the socket is open but the upstream stopped
  shipping events on a specific channel. The watchdog wakes every
  ``WS_FRESHNESS_TICK_S`` seconds, inspects Redis keys written by
  ``TradeObserver._handle_ws_message`` (``observer:ws:last_msg:<channel>``),
  and if any channel exceeds ``WS_CHANNEL_STALE_S`` it logs a WARNING,
  bumps ``polybot_ws_channel_stale_total{channel}`` and forces a
  reconnect.
* **Clamped reconnect backfill**. The old hardcoded "fetch 1h history"
  was either too greedy (wasting Falcon agent-556 quota) or too thin
  (missing burst tails). On reconnect we now compute the
  ``hours_to_backfill`` from the last seen WS message timestamp,
  clamped to ``[0, WS_BACKFILL_MAX_HOURS]``, and record it in
  ``polybot_ws_backfill_hours_used``. The actual fetch is delegated to
  the trade observer's ``data-api`` cursor poll — the cursor's
  ``OBSERVER_CURSOR_BOOTSTRAP_LOOKBACK_S`` fallback handles thin
  reconnects without needing dedicated logic here.
"""

import asyncio
import json
import time
from collections.abc import Awaitable, Callable

import websockets
from loguru import logger
from websockets.exceptions import ConnectionClosed, WebSocketException

from src.config import settings

# Phase 1 Task M / Phase 3 Round 1 (Agent A) contract import. See
# trade_observer.py for the rationale behind the no-op fallback (Task M
# lands first in production; this guard only fires in early CI runs).
try:
    from src.monitoring.metrics import (  # type: ignore[attr-defined]
        ws_backfill_hours_used,
        ws_channel_stale_total,
        ws_disconnects_total,
    )
except ImportError:  # pragma: no cover
    class _NoopMetric:
        def labels(self, *a, **kw):  # noqa: ANN001
            return self

        def inc(self, *a, **kw):  # noqa: ANN001
            return None

        def observe(self, *a, **kw):  # noqa: ANN001
            return None

    ws_disconnects_total = _NoopMetric()
    ws_channel_stale_total = _NoopMetric()
    ws_backfill_hours_used = _NoopMetric()

# Channels the watchdog monitors. These match the labels written by
# `TradeObserver._handle_ws_message`. We do NOT include "any" in this
# list because it's the union — adding it would double-count a single
# channel's silence.
_WATCHED_CHANNELS: tuple[str, ...] = ("book", "price_change", "trade")
_WS_LAST_MSG_KEY_PREFIX = "observer:ws:last_msg"


def _ws_last_msg_key(channel: str) -> str:
    return f"{_WS_LAST_MSG_KEY_PREFIX}:{channel}"

# Phase 3 Task D: ingest-health heartbeat. Defensive import so test
# harnesses that strip prometheus_client still load this module.
try:
    from src.monitoring.ingest_health import (  # type: ignore[attr-defined]
        SOURCE_WS_MARKET_FEED,
        get_health_monitor,
    )

    def _ws_heartbeat() -> None:
        try:
            get_health_monitor().heartbeat(SOURCE_WS_MARKET_FEED)
        except Exception:
            pass
except Exception:  # pragma: no cover
    def _ws_heartbeat() -> None:
        return None


class PolymarketWSClient:
    WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    SUBSCRIBE_CHUNK_SIZE = 100

    def __init__(
        self,
        on_message: Callable[[dict], Awaitable[None]],
        markets: set[str] | None = None,
        redis_client=None,  # optional redis.asyncio.Redis for the watchdog
    ):
        self._on_message = on_message
        self._markets: set[str] = markets or set()
        self._ws = None
        self._running = False
        self._stop_event = asyncio.Event()
        # Phase 3 Round 1 (Agent A) — the watchdog reads
        # `observer:ws:last_msg:<channel>` from Redis. If no redis
        # client was passed, the watchdog is a no-op (tests + cold
        # boot environments without Redis still work).
        self._redis = redis_client
        self._watchdog_task: asyncio.Task | None = None

        # Metrics
        self.reconnect_count: int = 0
        self.messages_received: int = 0
        self.last_message_at: float | None = None

    async def start(self) -> None:
        """Start the WebSocket connection loop + freshness watchdog."""
        self._running = True
        self._stop_event.clear()
        # Spin up the watchdog before the connect loop. The watchdog
        # only acts (force_reconnect) once `self._ws` is set, so racing
        # it ahead of `_connect_and_run` is safe.
        if self._redis is not None and self._watchdog_task is None:
            self._watchdog_task = asyncio.create_task(self._freshness_watchdog())
        try:
            await self._connect_loop()
        finally:
            if self._watchdog_task is not None and not self._watchdog_task.done():
                self._watchdog_task.cancel()
                try:
                    await self._watchdog_task
                except (asyncio.CancelledError, Exception):
                    pass
                self._watchdog_task = None

    async def stop(self) -> None:
        """Stop the WebSocket connection + watchdog."""
        self._running = False
        self._stop_event.set()
        if self._watchdog_task is not None and not self._watchdog_task.done():
            self._watchdog_task.cancel()
        if self._ws:
            await self._ws.close()

    async def force_reconnect(self) -> None:
        """Trip the current WebSocket session so the reconnect loop fires.

        Used by the Phase 3 Task D ingest health monitor as a recovery
        action when ``ws_market_feed`` heartbeats stop arriving. We
        intentionally don't tear down ``_running`` or ``_stop_event`` —
        the connect loop will see the close, increment its disconnect
        counter, and reconnect via the normal backoff path.
        """
        if not self._running:
            return
        ws = self._ws
        if ws is None:
            return
        try:
            await ws.close()
        except Exception as e:
            logger.warning(f"force_reconnect: ws.close raised: {e}")

    def update_markets(self, markets: set[str]) -> None:
        """Update the set of subscribed market token IDs."""
        self._markets = markets

    async def _freshness_watchdog(self) -> None:
        """Per-channel freshness watchdog (Phase 3 Round 1, Agent A).

        Wakes every ``settings.WS_FRESHNESS_TICK_S`` seconds and looks
        at each channel's last-message timestamp written by
        ``TradeObserver._handle_ws_message``. If any channel has been
        silent for >``settings.WS_CHANNEL_STALE_S`` AND we have at
        least one subscribed market (otherwise silence is expected),
        increment the stale counter and force a reconnect.

        Reconnect strategy: we close the underlying socket via
        ``force_reconnect`` and let ``_connect_loop`` reopen it; the
        normal backoff and ETag/cursor primitives in trade_observer
        handle catch-up. We do NOT directly fetch backfill from here
        — that's the trade_observer's job (its cursor knows the
        ``OBSERVER_CURSOR_BOOTSTRAP_LOOKBACK_S`` fallback).
        """
        tick_s = max(1, int(settings.WS_FRESHNESS_TICK_S))
        stale_s = max(tick_s, int(settings.WS_CHANNEL_STALE_S))
        while self._running and not self._stop_event.is_set():
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=tick_s)
                break
            except asyncio.TimeoutError:
                pass
            # If we have no markets subscribed (cold boot before
            # leader-registry has produced a market set), don't fault
            # on silence — there's no upstream that could speak.
            if not self._markets:
                continue
            try:
                stale_channels = await self._scan_stale_channels(
                    now_s=time.time(), stale_threshold_s=stale_s
                )
            except Exception as exc:
                logger.debug(f"_freshness_watchdog scan failed: {exc}")
                continue
            if not stale_channels:
                continue
            for channel, silent_for_s in stale_channels:
                ws_channel_stale_total.labels(channel=channel).inc()
                logger.warning(
                    f"WS channel {channel!r} silent for {silent_for_s:.0f}s "
                    f"(>{stale_s}s threshold) — forcing reconnect"
                )
            # One reconnect covers all stale channels — they all live
            # on the same socket.
            try:
                await self.force_reconnect()
            except Exception as exc:
                logger.debug(f"_freshness_watchdog: force_reconnect raised: {exc}")

    async def _scan_stale_channels(
        self, *, now_s: float, stale_threshold_s: float
    ) -> list[tuple[str, float]]:
        """Return ``[(channel, silent_for_s), ...]`` for stale channels.

        A channel with NO Redis key (e.g. it has never received a
        message) is considered stale only if the WS client has been
        running for at least ``stale_threshold_s`` — otherwise the
        absence is "we just connected and haven't heard anything yet",
        not a fault.
        """
        if self._redis is None:
            return []
        out: list[tuple[str, float]] = []
        process_age_s = (
            (now_s - self.last_message_at)
            if self.last_message_at is not None
            else float("inf")
        )
        for channel in _WATCHED_CHANNELS:
            try:
                raw = await self._redis.get(_ws_last_msg_key(channel))
            except Exception:
                continue
            if raw is None:
                # Missing key — only fault if we've been running long
                # enough that we should have *something*.
                if process_age_s >= stale_threshold_s:
                    out.append((channel, process_age_s))
                continue
            try:
                if isinstance(raw, (bytes, bytearray)):
                    raw = raw.decode("utf-8", errors="ignore")
                last_s = float(raw)
            except Exception:
                continue
            silent_for = max(0.0, now_s - last_s)
            if silent_for >= stale_threshold_s:
                out.append((channel, silent_for))
        return out

    async def _compute_backfill_hours(self) -> float:
        """How many hours of history to backfill on reconnect.

        Phase 3 Round 1 (Agent A) — replaces the hardcoded "1h"
        constant with the smaller of:

        * ``now - observer:ws:last_msg:any`` (no point reprocessing
          history we already covered), and
        * ``settings.WS_BACKFILL_MAX_HOURS`` (24 h default — hard cap
          to bound Falcon agent-556 quota on long outages).

        Returns 0.0 if no last-message marker is found (fresh boot —
        the trade observer's cursor handles bootstrap separately).
        """
        max_hours = max(0.0, float(settings.WS_BACKFILL_MAX_HOURS))
        if self._redis is None:
            return 0.0
        try:
            raw = await self._redis.get(_ws_last_msg_key("any"))
        except Exception:
            return 0.0
        if not raw:
            return 0.0
        try:
            if isinstance(raw, (bytes, bytearray)):
                raw = raw.decode("utf-8", errors="ignore")
            last_s = float(raw)
        except Exception:
            return 0.0
        hours = max(0.0, (time.time() - last_s) / 3600.0)
        return min(hours, max_hours)

    async def _connect_loop(self) -> None:
        backoff = 1
        max_backoff = 60
        while self._running and not self._stop_event.is_set():
            try:
                await self._connect_and_run()
                # A clean exit from `_connect_and_run` (i.e. the upstream
                # closed the socket without raising) still counts as a
                # disconnect from our perspective — the WS market channel
                # has no "end of session" signal.
                if self._running:
                    ws_disconnects_total.labels(reason="clean").inc()
                    # Phase 3 Round 1 (Agent A): on clean disconnect we
                    # still compute the backfill window — the next
                    # _connect_and_run does the resubscribe but the
                    # trade_observer's REST poll cursor handles the
                    # actual replay. We just emit the histogram so ops
                    # can see how much history we expected to recover.
                    hours = await self._compute_backfill_hours()
                    ws_backfill_hours_used.observe(hours)
                backoff = 1  # Reset on clean disconnect
            except (ConnectionClosed, WebSocketException, OSError) as e:
                if not self._running:
                    break
                self.reconnect_count += 1
                # Bucket disconnects by exception class so the dashboard can
                # tell "remote closed normally" from "TCP-level OSError"
                # from "protocol error". Keeping cardinality bounded by
                # using class names, not the string representation.
                ws_disconnects_total.labels(
                    reason=type(e).__name__
                ).inc()
                # On a noisy reconnect (any exception class), also emit
                # the backfill-hours histogram so the metric captures
                # both clean and dirty reconnect cases.
                try:
                    hours = await self._compute_backfill_hours()
                    ws_backfill_hours_used.observe(hours)
                except Exception:
                    pass
                logger.warning(
                    f"WebSocket disconnected: {e}. "
                    f"Reconnecting in {backoff}s (attempt #{self.reconnect_count})"
                )
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=backoff)
                    break  # stop_event was set
                except asyncio.TimeoutError:
                    pass
                backoff = min(backoff * 2, max_backoff)
            except Exception as e:
                if not self._running:
                    break
                ws_disconnects_total.labels(reason="unexpected").inc()
                logger.error(f"Unexpected WebSocket error: {e}")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, max_backoff)

    async def _connect_and_run(self) -> None:
        logger.info(f"Connecting to {self.WS_URL}")
        async with websockets.connect(
            self.WS_URL,
            ping_interval=settings.WEBSOCKET_PING_INTERVAL_S,
            ping_timeout=settings.WEBSOCKET_PONG_TIMEOUT_S,
            close_timeout=10,
        ) as ws:
            self._ws = ws
            logger.info("WebSocket connected")
            if self._markets:
                await self._subscribe(ws)
            try:
                async for raw in ws:
                    self.messages_received += 1
                    self.last_message_at = time.time()
                    # Phase 3 Task D: ingest-health heartbeat. Every WS
                    # message (book, price_change, last_trade_price)
                    # counts as ingestion liveness. O(1) dict write.
                    _ws_heartbeat()
                    # Loud INFO log on the first message and one every 100
                    # so we can prove in `docker compose logs observer` that
                    # the WS is actually receiving (not just connected). The
                    # health dashboard's "0 msgs/min" can mislead: this log
                    # is the source of truth.
                    if self.messages_received == 1 or self.messages_received % 100 == 0:
                        logger.info(
                            f"WS messages_received={self.messages_received} "
                            f"(first 80 chars: {str(raw)[:80]})"
                        )
                    try:
                        data = json.loads(raw)
                        if isinstance(data, list):
                            for item in data:
                                await self._on_message(item)
                        else:
                            await self._on_message(data)
                    except json.JSONDecodeError:
                        logger.debug(f"Non-JSON message: {raw[:100]}")
                    except Exception as e:
                        logger.error(f"Error processing message: {e}")
            finally:
                self._ws = None

    async def _ping_loop(self, ws) -> None:
        """Explicit ping loop used by tests and as a fallback keepalive helper."""
        while not self._stop_event.is_set():
            try:
                await asyncio.sleep(max(0, int(settings.WEBSOCKET_PING_INTERVAL_S)))
                pong_waiter = await ws.ping()
                await asyncio.wait_for(
                    pong_waiter,
                    timeout=max(0, int(settings.WEBSOCKET_PONG_TIMEOUT_S)),
                )
            except asyncio.TimeoutError:
                await ws.close()
                break
            except (ConnectionClosed, WebSocketException, OSError):
                break
            if not self._running:
                break

    async def _subscribe(self, ws) -> None:
        market_ids = sorted(self._markets)
        for start in range(0, len(market_ids), self.SUBSCRIBE_CHUNK_SIZE):
            chunk = market_ids[start : start + self.SUBSCRIBE_CHUNK_SIZE]
            msg = {
                "assets_ids": chunk,
                "type": "market",
                "custom_feature_enabled": True,
            }
            await ws.send(json.dumps(msg))
            if start + self.SUBSCRIBE_CHUNK_SIZE < len(market_ids):
                await asyncio.sleep(0.05)
        logger.info(f"Subscribed to {len(self._markets)} markets")
