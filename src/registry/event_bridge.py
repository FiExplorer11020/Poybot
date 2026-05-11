"""
Event bridge — wires Polymarket trade observations to event-driven Falcon
refresh of the leader registry.

Phase 3 Round 1 Agent A. The user-facing problem this addresses is the
"10-30 min pauses between continuous data gathering" symptom that maps
exactly to `FALCON_REFRESH_INTERVAL_S=1800`. Lowering the timer wastes
Falcon quota on idle markets; instead we keep the 1800 s timer as a
floor and trigger an incremental `LeaderRegistry.refresh_wallet(wallet)`
on three signal sources:

1. A new wallet's trade enters `trades:observed` with a notional above
   ``settings.EVENT_REFRESH_MIN_USDC``, OR
2. The trade observer sees ``settings.EVENT_REFRESH_UNKNOWN_TRADES``
   consecutive trades from a single unknown wallet, OR
3. An external caller (Telegram /refresh, Agent D's watchdog) invokes
   ``LeaderRegistry.refresh_wallet(wallet, reason="user_command")``
   directly. No Redis bridge needed for that path.

The bridge subscribes to ``trades:observed`` via the existing
``src.control.redis_pubsub.Subscriber`` plumbing (so it shares the
reconnect-safety contract documented in Phase 2 Task D) and emits one
``refresh_wallet`` call per qualifying trade, deduped by the cooldown
the registry itself maintains.

Design constraint: this bridge MUST NOT block the publishing path. The
Subscriber dispatches handlers in its own task; we await
``refresh_wallet`` from inside the handler, but we never hold the
publisher's coroutine. If Falcon is hard-down,
``refresh_wallet`` returns False quickly via the budget gate.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

from loguru import logger

from src.config import settings
from src.control.redis_pubsub import Subscriber

REDIS_TRADES_CHANNEL = "trades:observed"

# Bounded sweep cap on the in-memory "unknown wallet counter" so a flood
# of one-off addresses can't grow this dict unbounded. We periodically
# trim entries older than `_UNKNOWN_COUNTER_TTL_S` on a counter touch.
_UNKNOWN_COUNTER_TTL_S = 3600  # 1h sliding window
_UNKNOWN_COUNTER_MAX_ENTRIES = 50_000


class LeaderEventBridge:
    """Subscribes to ``trades:observed`` and emits incremental refreshes.

    ``registry`` must expose ``refresh_wallet(wallet: str,
    reason: str) -> Awaitable[bool]`` — the LeaderRegistry side does the
    coalescing + budget enforcement.
    """

    def __init__(
        self,
        registry: Any,
        *,
        redis_url: str | None = None,
        subscriber_name: str = "registry.event_bridge",
    ) -> None:
        self._registry = registry
        self._subscriber = Subscriber(
            redis_url or settings.REDIS_URL,
            name=subscriber_name,
        )
        self._subscriber.register(REDIS_TRADES_CHANNEL, self._on_trade)

        # Per-wallet streak counter for the "N consecutive trades from an
        # unknown wallet" trigger. Entry value is (count, last_seen_s).
        self._unknown_streak: dict[str, tuple[int, float]] = {}
        # A small in-memory set of wallets the registry currently treats
        # as active (passed in by the registry on construction). Falls
        # back to defaultdict(False) so missing wallets read as unknown
        # — i.e. fail-open toward triggering a refresh.
        self._known_wallets: set[str] = set()
        self._known_lock = asyncio.Lock()

    async def start(self) -> None:
        await self._subscriber.start()

    async def stop(self) -> None:
        await self._subscriber.stop()

    def update_known_wallets(self, wallets: set[str]) -> None:
        """Hot-swap the active leader set.

        Called by the registry on every cycle so the bridge knows which
        wallets to treat as "already enriched". A wallet in this set
        will only trigger an event-driven refresh if it crosses the
        notional threshold — not on every trade.
        """
        # Atomic swap of the reference; no lock needed since dict
        # references are CPython-atomic.
        self._known_wallets = set(wallets)
        # Prune the streak counter to keep memory bounded.
        self._prune_unknown_streak()

    def _prune_unknown_streak(self) -> None:
        now = time.time()
        if len(self._unknown_streak) > _UNKNOWN_COUNTER_MAX_ENTRIES:
            # Aggressive trim — drop everything older than the TTL.
            self._unknown_streak = {
                w: (c, t)
                for w, (c, t) in self._unknown_streak.items()
                if now - t < _UNKNOWN_COUNTER_TTL_S
            }

    async def _on_trade(self, payload: Any, channel: str) -> None:
        """Handler for `trades:observed` messages.

        Decides whether the trade qualifies for an event-driven refresh
        and dispatches to ``registry.refresh_wallet`` if so.
        """
        if not isinstance(payload, dict):
            return
        wallet = str(payload.get("wallet_address") or "").strip()
        if not wallet:
            return

        # If this wallet is already in the active leader set, the timer-
        # based path will refresh it at most every 30 min — that's the
        # contract. The only reason to trigger an event-driven refresh
        # for a known wallet is a high-notional trade signalling a
        # change in regime worth re-classifying immediately.
        is_known = wallet in self._known_wallets
        size_usdc = self._safe_float(payload.get("size_usdc"))
        reason: str | None = None

        if not is_known:
            count, _last = self._unknown_streak.get(wallet, (0, 0.0))
            count += 1
            self._unknown_streak[wallet] = (count, time.time())
            if (
                size_usdc is not None
                and size_usdc >= settings.EVENT_REFRESH_MIN_USDC
            ):
                reason = "ws_unknown_wallet"
            elif count >= settings.EVENT_REFRESH_UNKNOWN_TRADES:
                reason = "ws_unknown_wallet"
                # Reset the streak on dispatch so we don't re-fire every
                # subsequent trade — the cooldown inside refresh_wallet
                # provides the longer-term throttle.
                self._unknown_streak[wallet] = (0, time.time())
        else:
            # Known-wallet path: only big trades qualify. We don't keep
            # a streak counter for known wallets; size is the only
            # admissible event signal.
            if (
                size_usdc is not None
                and size_usdc >= settings.EVENT_REFRESH_MIN_USDC
            ):
                reason = "ws_unknown_wallet"  # high-notional regime change

        if reason is None:
            return

        try:
            await self._registry.refresh_wallet(wallet, reason=reason)
        except Exception as exc:
            # The registry is responsible for budget / coalescing
            # accounting; we only catch hard exceptions so the
            # subscriber's handler-error counter still fires but the
            # next message is processed normally.
            logger.debug(
                f"event_bridge: refresh_wallet({wallet}) raised: {exc}"
            )
            raise

    @staticmethod
    def _safe_float(value: Any) -> float | None:
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
