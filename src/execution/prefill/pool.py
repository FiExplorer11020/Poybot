"""Pre-signed CLOB order pool.

Round 7 / The Front Door — § 3.5.

CLOB order signing against an EOA key takes ~50 ms — far too slow on
the hot path. The :class:`PreSignedPool` warehouses signatures in
advance, keyed by ``(market_id, token_id, direction, size_bucket)``,
so the hot-path :meth:`fire` is purely a CLOB REST submit (the heavy
elliptic-curve work has already happened).

Sizing
------
Pool target at full warm:

  ~ top 100 markets x 2 tokens x 2 directions x 4 size buckets
  = ~3200 alive orders at any moment.

Each signature is valid for 5 minutes (configurable via
``settings.PREFILL_ORDER_VALIDITY_S``). A background task rotates
expired sigs every 30 s (``settings.PREFILL_ROTATION_INTERVAL_S``),
giving us a 10x safety margin so a hot-path fire never picks a
just-expired signature.

Size buckets
------------
Pre-signing for every possible size is combinatorially infeasible.
We sign for :data:`src.config.settings.PREFILL_POOL_SIZE_BUCKETS_USDC`
(default {500, 2000, 10000, 50000}) and pick the largest bucket
<= the leader's intended size.

Live mode gate
--------------
The pool's :meth:`fire` path issues a REAL order via py-clob-client.
The IntentRouter's killswitch consult MUST gate this call site (the
pool itself is killswitch-agnostic - separation of concerns).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import TYPE_CHECKING, Awaitable, Callable, Literal, Optional

from loguru import logger

from src.config import settings
from src.monitoring.metrics import (
    prefill_pool_misses_total,
    prefill_pool_signing_seconds,
    prefill_pool_size,
)

if TYPE_CHECKING:  # pragma: no cover - type-only imports
    from src.mempool.tx_decoder import LeaderIntent


# A markets provider is any async callable returning the list of
# market_ids to warm. Wave-2 the engine wires this to a SELECT against
# the `markets` table ranked by 24h volume.
MarketsProvider = Callable[[], Awaitable[list[str]]]


@dataclass(slots=True)
class PreSignedOrder:
    """One pre-signed CLOB order sitting in the pool.

    See module docstring + ``docs/ROUND_7_MEMPOOL_AND_PREFILL.md`` § 3.5.
    """

    market_id: str
    token_id: str
    direction: Literal["buy", "sell"]
    size_bucket: int  # USDC notional bucket
    price: Decimal
    signature: str
    signed_at: datetime
    expires_at: datetime
    nonce: int

    def is_expired(self, now: datetime | None = None) -> bool:
        """True iff the order's :attr:`expires_at` is in the past."""
        now = now or datetime.now(tz=timezone.utc)
        return now >= self.expires_at


@dataclass(slots=True)
class FilledOrder:
    """Outcome of a successful :meth:`PreSignedPool.fire` call.

    Mirrors the shape of the existing :class:`OrderManager` outcome
    so the IntentRouter can hand the result off to the same
    persistence code path that :class:`LiveTrader` already uses.
    """

    clob_order_id: str
    filled_size_shares: float
    avg_fill_price: float
    fee_paid_usdc: float
    raw_response: dict = field(default_factory=dict)


# Pool key alias: (market_id, token_id, direction, size_bucket).
PoolKey = tuple[str, str, str, int]


class PreSignedPool:
    """Warehouse of pre-signed CLOB orders, keyed by lookup tuple.

    Owns:
      * the CLOB client wrapper (signs + submits orders).
      * the in-memory ``_pool`` dict.
      * a background rotation task that expires stale orders +
        opportunistically refills empty slots.

    Concurrency model
    -----------------
    A single ``asyncio.Lock`` protects the ``_pool`` dict. Granularity
    is "whole pool" rather than per-key because:

      * The hot-path :meth:`fire` is a pop-from-list-then-submit. Pop
        is microseconds; we release the lock BEFORE the network submit
        so other ``fire`` calls on different keys aren't blocked.
      * Warm + expire_stale are bulk operations; per-key locking would
        require a lock per key (thousands of locks) for marginal gain.

    Tests verify that 10 concurrent ``fire`` calls on the same key
    each receive a DIFFERENT order (no double-handout).
    """

    def __init__(
        self,
        clob_client,
        markets_provider: MarketsProvider,
    ) -> None:
        """Bind to a CLOB client + a markets-provider callable.

        Parameters
        ----------
        clob_client
            Anything that exposes ``async sign_order(...)`` and
            ``async submit_presigned(order)``. In production this is
            a thin wrapper around :class:`src.engine.clob_client_wrapper.CLOBClientWrapper`
            that surfaces the py-clob-client ``create_order`` /
            ``post_order`` primitives separately (canonical wrapper
            today bundles them into ``place_limit_order``; Wave-2
            split is documented in the agent return summary).
        markets_provider
            Async callable returning ``list[str]`` of market_ids to
            warm. Production wires to a SELECT against the ``markets``
            table ranked by 24h volume, top
            :data:`src.config.settings.PREFILL_TOP_MARKETS` rows.
        """
        self._clob = clob_client
        self._markets_provider = markets_provider
        # Pool key: (market_id, token_id, direction, size_bucket).
        # Multiple orders per key are allowed so concurrent fires can
        # each take a distinct slot.
        self._pool: dict[PoolKey, list[PreSignedOrder]] = {}
        self._lock = asyncio.Lock()
        self._rotation_task: asyncio.Task | None = None
        self._stopped = False
        # Nonce monotonic per pool instance. Each pre-sign bumps it so
        # the EOA never has two orders out with the same nonce.
        self._next_nonce = 1

    # ------------------------------------------------------------------
    # Warm / sign-one
    # ------------------------------------------------------------------

    async def warm(self, markets: list[str]) -> int:
        """Pre-sign one order per (market, token, direction, bucket).

        Each market warms two synthetic token ids: ``"<market_id>:YES"``
        and ``"<market_id>:NO"``. The CLOB wrapper knows the actual
        token ids from its own lookup; we use stable synthetic strings
        here so tests can construct expectations without depending on
        the live token registry. Wave-2 production binding passes the
        REAL token ids via the markets_provider's payload shape.

        Returns the TOTAL number of orders now in the pool after warm
        (cumulative, not delta).
        """
        buckets = list(settings.PREFILL_POOL_SIZE_BUCKETS_USDC)
        signed_count = 0
        for market_id in markets:
            for token_suffix in ("YES", "NO"):
                token_id = f"{market_id}:{token_suffix}"
                for direction in ("buy", "sell"):
                    for bucket in buckets:
                        try:
                            order = await self._sign_one(
                                market_id=market_id,
                                token_id=token_id,
                                direction=direction,
                                size_bucket=bucket,
                            )
                        except Exception:
                            logger.exception(
                                "PreSignedPool warm sign failed",
                            )
                            continue
                        key: PoolKey = (
                            market_id,
                            token_id,
                            direction,
                            bucket,
                        )
                        async with self._lock:
                            self._pool.setdefault(key, []).append(order)
                        signed_count += 1
                        self._update_gauge(market_id, direction)
        total = sum(len(v) for v in self._pool.values())
        logger.info(
            "PreSignedPool warm complete: signed={signed} pool_total={total}",
            signed=signed_count,
            total=total,
        )
        return total

    async def _sign_one(
        self,
        *,
        market_id: str,
        token_id: str,
        direction: Literal["buy", "sell"],
        size_bucket: int,
    ) -> PreSignedOrder:
        """Issue one signing call to the CLOB client, time it, build
        the :class:`PreSignedOrder`.

        The CLOB client must expose an ``async sign_order(market_id,
        token_id, direction, size_bucket)`` returning a dict-like with
        ``signature`` and ``price`` keys (the price is set at sign time
        from the orderbook midpoint +/- the configured slippage). Wave-2
        production binding constructs this from the existing
        :meth:`src.engine.clob_client_wrapper.CLOBClientWrapper.place_limit_order`
        split into ``create_order`` (sign-only) + ``post_order``
        (submit) - the canonical wrapper today combines them.
        """
        # ``time()`` returns a context manager that observes wall time
        # into the prefill_pool_signing_seconds histogram.
        with prefill_pool_signing_seconds.time():
            payload = await self._clob.sign_order(
                market_id=market_id,
                token_id=token_id,
                direction=direction,
                size_bucket=size_bucket,
            )
        now = datetime.now(tz=timezone.utc)
        expires_at = now + timedelta(seconds=settings.PREFILL_ORDER_VALIDITY_S)
        nonce = self._next_nonce
        self._next_nonce += 1
        return PreSignedOrder(
            market_id=market_id,
            token_id=token_id,
            direction=direction,
            size_bucket=size_bucket,
            price=Decimal(str(payload.get("price", "0.5"))),
            signature=str(payload["signature"]),
            signed_at=now,
            expires_at=expires_at,
            nonce=nonce,
        )

    # ------------------------------------------------------------------
    # Hot path
    # ------------------------------------------------------------------

    async def fire(self, intent: "LeaderIntent") -> Optional[FilledOrder]:
        """Hot path: match an intent to a pre-signed order, submit it.

        Bucket selection: largest bucket ``<=`` ``intent.size_usdc``.
        No bucket fits => return ``None`` with ``reason='no_bucket_fit'``.

        Returns
        -------
        Optional[FilledOrder]
            The fill outcome on success; ``None`` on any pool miss /
            submit failure (caller responsible for routing /
            decision_log).
        """
        size = self._intent_size(intent)
        bucket = self._largest_bucket_le(size)
        if bucket is None:
            self._record_miss("no_bucket_fit")
            return None

        market_id = intent.market_id
        token_id = intent.token_id
        direction = intent.side

        key: PoolKey = (market_id, token_id, direction, bucket)

        # Quick-check: is there ANY slot for this market id at all?
        # Distinguishes no_market from no_direction / no_token_match for
        # the misses metric.
        async with self._lock:
            order = self._pop_non_expired(key)
            if order is None:
                miss_reason = self._classify_miss(
                    market_id=market_id,
                    token_id=token_id,
                    direction=direction,
                    bucket=bucket,
                )

        if order is None:
            self._record_miss(miss_reason)
            return None

        # Update gauge to reflect the pop.
        self._update_gauge(market_id, direction)

        # Submit OUTSIDE the lock. The submit is a network call; holding
        # the pool lock during it would serialize all fires.
        try:
            response = await self._clob.submit_presigned(order)
        except Exception:
            logger.exception("PreSignedPool submit_presigned failed")
            self._record_miss("signing_failed")
            return None

        if not response or not response.get("success"):
            self._record_miss("signing_failed")
            return None

        return FilledOrder(
            clob_order_id=str(response.get("clob_order_id", "")),
            filled_size_shares=float(response.get("filled_size_shares", 0.0)),
            avg_fill_price=float(response.get("avg_fill_price", 0.0)),
            fee_paid_usdc=float(response.get("fee_paid_usdc", 0.0)),
            raw_response=response if isinstance(response, dict) else {},
        )

    # ------------------------------------------------------------------
    # Expiry + rotation
    # ------------------------------------------------------------------

    async def expire_stale(self) -> int:
        """Drop every order with ``is_expired() == True``. Returns the
        count dropped + refreshes the size gauge for every touched
        (market, direction) pair.
        """
        now = datetime.now(tz=timezone.utc)
        dropped = 0
        touched: set[tuple[str, str]] = set()
        async with self._lock:
            for key, orders in self._pool.items():
                fresh = [o for o in orders if not o.is_expired(now)]
                stale_count = len(orders) - len(fresh)
                if stale_count == 0:
                    continue
                dropped += stale_count
                touched.add((key[0], key[2]))  # market, direction
                # Always update the list - even an empty list. We keep
                # the key alive so :meth:`_refill_empty` can re-sign
                # for that exact slot on the next rotation tick.
                self._pool[key] = fresh

        for market_id, direction in touched:
            self._update_gauge(market_id, direction)

        return dropped

    async def start_rotation(self) -> None:
        """Spawn the background expire_stale + opportunistic refill
        loop. Idempotent: a second call is a no-op while the task is
        already alive.
        """
        if self._rotation_task is not None and not self._rotation_task.done():
            return
        self._stopped = False
        self._rotation_task = asyncio.create_task(
            self._rotation_loop(),
            name="PreSignedPool.rotation",
        )

    async def stop(self) -> None:
        """Cancel the rotation task + await its cleanup. Idempotent."""
        self._stopped = True
        task = self._rotation_task
        if task is None:
            return
        if not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                # CancelledError is expected; other exceptions get
                # swallowed because shutdown shouldn't raise.
                pass
        self._rotation_task = None

    async def _rotation_loop(self) -> None:
        """Wake every PREFILL_ROTATION_INTERVAL_S, expire stale, refill
        empty slots opportunistically.
        """
        interval = settings.PREFILL_ROTATION_INTERVAL_S
        try:
            while not self._stopped:
                try:
                    await self.expire_stale()
                    await self._refill_empty()
                except Exception:
                    logger.exception("PreSignedPool rotation tick failed")
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            raise

    async def _refill_empty(self) -> int:
        """For every key that has ZERO non-expired orders, re-sign one.

        Re-signs only keys we have previously warmed (no spontaneous
        warming of new markets here — that's :meth:`warm`'s job).
        Returns the count of newly-signed orders.
        """
        async with self._lock:
            # Snapshot the keys whose lists are empty (or all expired).
            now = datetime.now(tz=timezone.utc)
            empty_keys = [
                key
                for key, orders in self._pool.items()
                if not any(not o.is_expired(now) for o in orders)
            ]

        refilled = 0
        for key in empty_keys:
            market_id, token_id, direction, bucket = key
            try:
                order = await self._sign_one(
                    market_id=market_id,
                    token_id=token_id,
                    direction=direction,  # type: ignore[arg-type]
                    size_bucket=bucket,
                )
            except Exception:
                logger.exception(
                    "PreSignedPool refill sign failed key={key}",
                    key=key,
                )
                continue
            async with self._lock:
                self._pool.setdefault(key, []).append(order)
            refilled += 1
            self._update_gauge(market_id, direction)
        return refilled

    # ------------------------------------------------------------------
    # Stats + introspection
    # ------------------------------------------------------------------

    def stats(self) -> dict:
        """Snapshot of pool state for the dashboard + alerts.

        Synchronous because reads of dict views are atomic under the
        GIL and we tolerate momentarily-stale counts.
        """
        by_market: dict[str, int] = {}
        by_direction: dict[str, int] = {"buy": 0, "sell": 0}
        by_bucket: dict[int, int] = {}
        oldest: datetime | None = None
        total = 0
        for (market_id, _token, direction, bucket), orders in self._pool.items():
            count = len(orders)
            total += count
            by_market[market_id] = by_market.get(market_id, 0) + count
            if direction in by_direction:
                by_direction[direction] += count
            by_bucket[bucket] = by_bucket.get(bucket, 0) + count
            for o in orders:
                if oldest is None or o.signed_at < oldest:
                    oldest = o.signed_at
        return {
            "total_orders": total,
            "by_market": by_market,
            "by_direction": by_direction,
            "by_bucket": by_bucket,
            "oldest_signed_at": oldest,
        }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _intent_size(self, intent: "LeaderIntent") -> Decimal:
        """Coerce intent.size_usdc into a Decimal for bucket compare."""
        size = intent.size_usdc
        if isinstance(size, Decimal):
            return size
        return Decimal(str(size))

    def _largest_bucket_le(self, size: Decimal) -> int | None:
        """Return the biggest bucket <= size, or None if size is below
        every bucket. Assumes settings.PREFILL_POOL_SIZE_BUCKETS_USDC
        is ascending (enforced by the config validator).
        """
        buckets = settings.PREFILL_POOL_SIZE_BUCKETS_USDC
        result: int | None = None
        for b in buckets:
            if Decimal(b) <= size:
                result = b
            else:
                break
        return result

    def _pop_non_expired(self, key: PoolKey) -> PreSignedOrder | None:
        """Caller MUST hold ``self._lock``. Pop the first non-expired
        order for ``key`` and drop any expired orders we step over.
        Returns ``None`` if the slot is empty / all-expired (the
        all-expired slot is left EMPTY so the rotation refill picks it
        up next tick).
        """
        orders = self._pool.get(key)
        if not orders:
            return None
        now = datetime.now(tz=timezone.utc)
        while orders:
            candidate = orders.pop(0)
            if not candidate.is_expired(now):
                return candidate
        # All expired — leave the (now empty) list in place for the
        # refill loop to find.
        return None

    def _classify_miss(
        self,
        *,
        market_id: str,
        token_id: str,
        direction: str,
        bucket: int,
    ) -> str:
        """Categorize a miss for the metric label.

        Caller MUST hold ``self._lock`` since we walk ``self._pool``.
        Distinguishes: no_market, no_token_match, no_direction,
        all_expired, no_bucket_fit (latter never raised here -
        caller handles bucket=None upstream).
        """
        keys = list(self._pool.keys())
        if not any(k[0] == market_id for k in keys):
            return "no_market"
        if not any(k[0] == market_id and k[1] == token_id for k in keys):
            return "no_token_match"
        if not any(
            k[0] == market_id and k[1] == token_id and k[2] == direction
            for k in keys
        ):
            return "no_direction"
        # Some entry exists for (market, token, direction) but the
        # specific bucket either had all-expired or empty list.
        return "all_expired"

    def _record_miss(self, reason: str) -> None:
        """Inc the misses counter with the given reason label.

        Wrapped so tests can patch the metric in one place.
        """
        try:
            prefill_pool_misses_total.labels(reason=reason).inc()
        except Exception:
            # Never let metrics emission take down the hot path.
            logger.exception("prefill_pool_misses_total inc failed")

    def _update_gauge(self, market_id: str, direction: str) -> None:
        """Recompute the per-(market, direction) gauge value."""
        count = 0
        for (m, _t, d, _b), orders in self._pool.items():
            if m == market_id and d == direction:
                count += len(orders)
        try:
            prefill_pool_size.labels(
                market=market_id, direction=direction
            ).set(count)
        except Exception:
            logger.exception("prefill_pool_size set failed")
