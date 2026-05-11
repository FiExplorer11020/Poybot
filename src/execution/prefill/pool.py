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

  ~ top 100 markets × 2 tokens × 2 directions × 4 size buckets
  = ~3200 alive orders at any moment.

Each signature is valid for 5 minutes (configurable via
``settings.PREFILL_ORDER_VALIDITY_S``). A background task rotates
expired sigs every 30 s (``settings.PREFILL_ROTATION_INTERVAL_S``),
giving us a 10× safety margin so a hot-path fire never picks a
just-expired signature.

Size buckets
------------
Pre-signing for every possible size is combinatorially infeasible
— a leader might submit ANY of $50, $51, ..., $50000. We sign for
:data:`src.config.settings.PREFILL_POOL_SIZE_BUCKETS_USDC`
(default {500, 2000, 10000, 50000}) and pick the largest bucket
≤ the leader's intended size. A slight under-fill is acceptable —
we're already capturing 90%+ of the alpha by being EARLY; an exact
size match is the icing.

Live mode gate
--------------
The pool's :meth:`fire` path issues a REAL order via py-clob-client.
The IntentRouter's killswitch consult MUST gate this call site (the
pool itself is killswitch-agnostic — separation of concerns). The
``PREFILL_LIVE_ENABLED`` runtime config knob (default ``False``)
governs whether the router calls :meth:`fire` at all vs routing to
the paper trader.

State
-----
In-memory dict::

    self._orders: dict[PoolKey, list[PreSignedOrder]]

No DB persistence — signatures are time-limited anyway. On daemon
restart the pool is empty; :meth:`warm` rebuilds it within seconds.

See ``docs/ROUND_7_MEMPOOL_AND_PREFILL.md`` § 3.5 for the spec.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Literal, Optional

if TYPE_CHECKING:  # pragma: no cover — type-only imports
    from src.mempool.tx_decoder import LeaderIntent


_WAVE_2_REF = "Wave 2 — see docs/ROUND_7_MEMPOOL_AND_PREFILL.md § 3.5"


@dataclass(slots=True)
class PreSignedOrder:
    """One pre-signed CLOB order sitting in the pool.

    Attributes
    ----------
    market_id
        Polymarket condition / market id.
    token_id
        The outcome token (YES or NO).
    direction
        ``"buy"`` or ``"sell"`` — matches the way the CLOB encodes side.
    size_bucket
        One of :data:`src.config.settings.PREFILL_POOL_SIZE_BUCKETS_USDC`
        in USDC. Used as the lookup key + as the actual order size.
    price
        Limit price per share, in [0, 1]. Set at signing time using
        the current best bid/offer +/- a slippage budget (Wave-2
        decides the budget; the architect's note is "match
        ``settings.LIVE_SLIPPAGE_BPS`` for parity with the existing
        live trader's REST orders").
    signature
        The EIP-712 signature blob produced by py-clob-client. Opaque
        bytes; py-clob-client knows how to re-attach it on submit.
    signed_at
        Wall-clock time of signing. Used for staleness audits.
    expires_at
        ``signed_at + PREFILL_ORDER_VALIDITY_S``. Cached here so
        :meth:`expire_stale` doesn't have to recompute on every pass.
    nonce
        The order nonce (NOT the wallet nonce — the CLOB has its own
        order-nonce scheme that lets the maker invalidate by nonce
        without spending gas).
    """

    market_id: str
    token_id: str
    direction: Literal["buy", "sell"]
    size_bucket: int
    price: Decimal
    signature: str
    signed_at: datetime
    expires_at: datetime
    nonce: int


@dataclass(slots=True)
class FilledOrder:
    """Outcome of a successful :meth:`PreSignedPool.fire` call.

    Mirrors the shape of the existing :class:`OrderManager` outcome
    so the IntentRouter can hand the result off to the same
    persistence code path that :class:`LiveTrader` already uses.

    Attributes
    ----------
    clob_order_id
        Server-side order id assigned by Polymarket CLOB.
    filled_size_shares
        Actual shares filled (may be less than the bucket if liquidity
        was thin).
    avg_fill_price
        Volume-weighted average price across the fill.
    fee_paid_usdc
        Fee charged by the CLOB (for the per-market fee rate).
    """

    clob_order_id: str
    filled_size_shares: float
    avg_fill_price: float
    fee_paid_usdc: float


class PreSignedPool:
    """Warehouse of pre-signed CLOB orders, keyed by lookup tuple.

    Owns:
      * the :class:`py_clob_client.ClobClient` (signs + submits orders).
      * the EOA signing key (``settings.POLYMARKET_PRIVATE_KEY``).
      * the in-memory ``_orders`` dict.
      * a background task for ``expire_stale`` (started by the
        IntentRouter on its own ``start()`` since the pool itself
        has no asyncio lifecycle).

    Thread-safety: the hot-path :meth:`fire` is async and protected
    by a per-key asyncio.Lock so the same pre-signed slot isn't
    handed to two concurrent intents. The :meth:`warm` and
    :meth:`expire_stale` mutators take the same lock.
    """

    def __init__(self, clob_client, signing_key: str) -> None:
        """Bind to a CLOB client and the EOA signing key.

        Parameters
        ----------
        clob_client
            An instance of :class:`src.engine.clob_client_wrapper.CLOBClientWrapper`
            — Wave-2 may directly use the raw py-clob-client if the
            wrapper proves too coarse-grained for the signing-only
            path, but the wrapper is the canonical entry point.
        signing_key
            The EOA private key string (from ``settings.POLYMARKET_PRIVATE_KEY``).
            Stored ONLY in process memory; never logged.
        """
        raise NotImplementedError(_WAVE_2_REF)

    async def warm(self, markets: list[str]) -> int:
        """Pre-sign orders for every (market, direction, size) combo.

        Parameters
        ----------
        markets
            List of market ids to warm. In production this is the top
            ``settings.PREFILL_TOP_MARKETS`` markets by 24h volume,
            sourced from the ``markets`` table.

        Returns
        -------
        int
            Total number of pre-signed orders now in the pool.

        Wave-2 implementation outline::

            for market_id in markets:
                for token_id in (yes_token, no_token):
                    for direction in ("buy", "sell"):
                        for size in settings.PREFILL_POOL_SIZE_BUCKETS_USDC:
                            order = await self._sign_one(
                                market_id, token_id, direction, size
                            )
                            self._orders.setdefault(key, []).append(order)

        Time budget: at ~50 ms per sign × 3200 orders ≈ 160 seconds
        warm-up. That's OK — warm runs asynchronously at engine boot
        and the IntentRouter simply emits ``pool_miss`` until the
        pool fills.

        Metrics: per successful sign, observe
        ``polybot_prefill_pool_signing_seconds`` and increment the
        gauge ``polybot_prefill_pool_size{market, direction}``.
        """
        raise NotImplementedError(_WAVE_2_REF)

    async def fire(self, intent: "LeaderIntent") -> Optional[FilledOrder]:
        """Pick a matching pre-signed order and submit it. Hot path.

        Lookup tuple::

            key = (intent.market_id, intent.token_id,
                   intent.side,
                   _largest_bucket_le(intent.size_usdc))

        Where ``_largest_bucket_le`` returns the biggest bucket
        ``<= intent.size_usdc``. If no bucket fits (the leader's
        intent is smaller than the smallest bucket, e.g. <$500), we
        return ``None`` with ``polybot_prefill_pool_misses_total
        {reason="below_min_bucket"}``.

        On lookup hit:
          1. Pop the order from the list (atomic under the per-key
             lock).
          2. ``await clob_client.submit_presigned(order)`` — Wave-2
             defines the wrapper method.
          3. Wait for fill confirmation (bounded by
             ``settings.LIVE_ORDER_TIMEOUT_S``).
          4. Background-task: schedule a replacement sign for this
             key so the pool stays warm without a synchronous wait.
          5. Return :class:`FilledOrder`.

        On lookup miss:
          * ``polybot_prefill_pool_misses_total{reason="no_signature"}.inc()``
          * Return ``None``.

        On submit failure (rate-limit, CLOB-side rejection):
          * Re-queue the order (it's still valid, we just need to
            try again) UNLESS the order is now expired, in which case
            increment ``...{reason="signature_expired"}`` and drop it.
          * Return ``None``.

        The intent_router caller logs the appropriate
        ``decision_log`` row on success and emits the ``trades:stream``
        entry with ``source='prefill'``.
        """
        raise NotImplementedError(_WAVE_2_REF)

    async def expire_stale(self) -> int:
        """Drop signatures past their :attr:`PreSignedOrder.expires_at`.

        Returns the count dropped. Called every
        ``settings.PREFILL_ROTATION_INTERVAL_S`` seconds by a
        background task owned by the IntentRouter.

        Wave-2 should ALSO trigger a re-sign for every dropped slot
        (otherwise the pool drains over time). The re-sign goes
        through the same code path as :meth:`warm` but for one key
        rather than the full warm sweep.
        """
        raise NotImplementedError(_WAVE_2_REF)

    def stats(self) -> dict:
        """Snapshot of pool state for the dashboard + the
        ``polybot_prefill_pool_size{market, direction}`` gauge.

        Returns a dict with at minimum::

            {
                "total_orders": int,
                "by_market": {market_id: count},
                "by_direction": {"buy": count, "sell": count},
                "by_bucket": {size_bucket: count},
                "oldest_signed_at": datetime | None,
            }
        """
        raise NotImplementedError(_WAVE_2_REF)
