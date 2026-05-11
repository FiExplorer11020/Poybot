"""Bloom-filter membership index for watched leader wallets.

Round 7 / The Front Door — § 3.3.

At ~2000 watched wallets the choice is between:

* a Python ``set[str]`` — O(1) lookup but ~80 bytes per entry
  (160 KB) AND every check walks the hash table.
* a 32 KB bloom filter at 1% false-positive rate — ~50ns per
  check, fits a CPU cache line, and 1% FP just costs one
  unnecessary decode attempt downstream.

We pick the bloom. Even though Erigon's filtered subscription
already removes most of the firehose, we keep the bloom as
defence-in-depth: Erigon could miss a filter update during a
restart, and the bloom check is essentially free.

Refresh contract
----------------
The index sources from the ``wallet_universe`` table (R6 migration
020). We pull every row with ``depth_tier IN (0, 1)`` — those are
the FULL-refresh and PERIODIC-refresh tiers, i.e. the active
leaders. Tier 2 wallets are the long-tail crawl set and don't
warrant mempool watching.

A background task in the mempool daemon calls
:meth:`run_refresh_loop` with ``interval_s=300`` (5 minutes) — the
same cadence the R6 AdaptiveDepth ``review_tiers`` job uses, so we
pick up tier transitions promptly. The full rebuild is cheap: at
2000 rows it's a single SQL ``SELECT`` + 2000 ``bloom.add`` calls.

False-positive impact
---------------------
At 1% FP a non-watched wallet's tx survives the bloom 1% of the time
and reaches the decoder. The decoder will then see calldata that may
or may not be a CLOB call; in the worst case it decodes ~1% extra
load. Acceptable.

Wave-2 may use either:
  * ``pybloom_live`` (small dep, well-tested) — preferred.
  * A 32 KB inline bytearray bloom (3 hash functions of md5/sha1
    bits) — keeps the dep tree small. The interface here doesn't
    care which.

See ``docs/ROUND_7_MEMPOOL_AND_PREFILL.md`` § 3.3 for the spec.
"""

from __future__ import annotations

_WAVE_2_REF = "Wave 2 — see docs/ROUND_7_MEMPOOL_AND_PREFILL.md § 3.3"


# Bloom-filter sizing defaults.
#
# Capacity 4096 at 1% FP rate fits comfortably under 32 KB (the
# theoretical lower bound is ~9.6 bits per entry; we round up). At a
# 2000-wallet steady state we have 2× headroom before FP rate
# degrades.
DEFAULT_BLOOM_CAPACITY: int = 4096
DEFAULT_BLOOM_ERROR_RATE: float = 0.01


class WatchedWalletIndex:
    """In-memory bloom-filter index over the active leader wallet set.

    Thread-safety
    -------------
    The hot-path :meth:`__contains__` MUST be safe to call from the
    mempool subscription's async loop while the refresh task is
    rebuilding. Wave-2 should implement the swap as: build the new
    bloom in a local variable, then atomically replace the instance
    attribute under a single assignment (Python's GIL guarantees the
    assignment is observed atomically; a brief race during rebuild
    sees the OLD bloom, which is harmless).

    The :meth:`add` helper exists for tests and for incremental
    updates between full rebuilds (e.g. an event-driven path where
    the registry's wallet-promotion bridge tells us "this wallet just
    became tier 1, please watch it now" without waiting for the
    5-min rebuild).
    """

    def __init__(
        self,
        bloom_capacity: int = DEFAULT_BLOOM_CAPACITY,
        error_rate: float = DEFAULT_BLOOM_ERROR_RATE,
    ) -> None:
        """Construct an empty bloom.

        Parameters
        ----------
        bloom_capacity
            Expected number of entries. The bloom is sized to hit the
            ``error_rate`` false-positive rate at this load. Going
            over capacity is permitted but degrades FP rate.
        error_rate
            Target false-positive rate at full capacity.
        """
        raise NotImplementedError(_WAVE_2_REF)

    def __contains__(self, wallet: str) -> bool:
        """Probabilistic membership test.

        Returns ``True`` if ``wallet`` is *probably* in the watch set
        (false-positive rate ≤ ``error_rate`` at capacity), or
        ``False`` if definitely not. The mempool subscription uses
        this as its hot-path filter:

            if tx.from_wallet not in self._wallet_index:
                continue

        Implementation note: lowercase + 0x-prefix-normalise the
        input before bloom-hashing. The wallet_universe table stores
        addresses in lowercase 0x form, and Erigon emits lowercase
        too — this is purely a defence against a caller passing a
        mixed-case checksummed address.
        """
        raise NotImplementedError(_WAVE_2_REF)

    def add(self, wallet: str) -> None:
        """Add a wallet to the bloom. Same normalisation as
        :meth:`__contains__`.

        Idempotent. The bloom never shrinks — to remove a wallet we
        rebuild from scratch via :meth:`refresh_from_universe`.
        """
        raise NotImplementedError(_WAVE_2_REF)

    async def refresh_from_universe(self) -> int:
        """Rebuild the bloom from a fresh ``wallet_universe`` SELECT.

        SQL contract::

            SELECT wallet_address
            FROM wallet_universe
            WHERE depth_tier IN (0, 1)

        Returns the count of entries added.

        Sets the bloom atomically — see the class docstring on
        thread-safety. After this returns, every future
        :meth:`__contains__` reflects the new set.

        Wave-2 should set the ``polybot_mempool_subscriptions_active``
        gauge as part of the refresh path's debug telemetry; pure
        bloom-size is not metric-worthy.
        """
        raise NotImplementedError(_WAVE_2_REF)

    async def run_refresh_loop(self, interval_s: int = 300) -> None:
        """Long-running task: refresh every ``interval_s`` seconds.

        Wave-2 implementation outline::

            while True:
                try:
                    n = await self.refresh_from_universe()
                    logger.info(
                        "WatchedWalletIndex: refreshed n={} wallets", n
                    )
                except Exception as exc:
                    logger.exception(
                        "WatchedWalletIndex refresh failed: {}", exc
                    )
                await asyncio.sleep(interval_s)

        The daemon entrypoint spawns one task per
        :class:`WatchedWalletIndex` instance and cancels it on
        shutdown.
        """
        raise NotImplementedError(_WAVE_2_REF)

    def snapshot_addresses(self) -> list[str]:
        """Return a SNAPSHOT of the current addresses for filter
        construction in :class:`src.mempool.node_client.MempoolSubscription`.

        The bloom itself can't enumerate its contents — Wave-2 keeps
        a parallel sorted set / list of the actual addresses for this
        purpose. The list grows with :meth:`add`/:meth:`refresh_from_universe`
        and is rebuilt under the same atomic-replace contract as the
        bloom. ~2000 strings is ~80 KB, negligible inside the daemon's
        300 MB budget.

        Returns a NEW list — the subscription is free to keep its own
        reference without worrying about concurrent mutation.
        """
        raise NotImplementedError(_WAVE_2_REF)
