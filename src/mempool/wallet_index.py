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

Implementation
--------------
We don't take a dependency on ``pybloom_live`` or ``mmh3`` — the
needed surface is tiny (set bit / test bit) and the cryptographic
``hashlib.blake2b`` with rotating seeds gives us enough hash
diversity for the K independent slots a bloom filter wants. A
parallel ``set[str]`` keeps the actual address list so we can hand
it to the Erigon subscription filter (the bloom itself can't
enumerate).

See ``docs/ROUND_7_MEMPOOL_AND_PREFILL.md`` § 3.3 for the spec.
"""

from __future__ import annotations

import asyncio
import hashlib
import math
from typing import TYPE_CHECKING, Iterable

from loguru import logger

if TYPE_CHECKING:  # pragma: no cover — type-only imports
    pass


# Bloom-filter sizing defaults.
#
# Capacity 4096 at 1% FP rate fits comfortably under 32 KB (the
# theoretical lower bound is ~9.6 bits per entry; we round up). At a
# 2000-wallet steady state we have 2× headroom before FP rate
# degrades.
DEFAULT_BLOOM_CAPACITY: int = 4096
DEFAULT_BLOOM_ERROR_RATE: float = 0.01


def _optimal_m_k(capacity: int, error_rate: float) -> tuple[int, int]:
    """Return (m_bits, k_hashes) for a bloom of given capacity / FP rate.

    Classic formulas:
      m = -(n * ln(p)) / (ln 2)^2
      k = (m / n) * ln 2

    Both are rounded up to the nearest positive integer.
    """
    n = max(1, int(capacity))
    p = float(error_rate)
    if not 0.0 < p < 1.0:
        p = 0.01
    m = -(n * math.log(p)) / (math.log(2) ** 2)
    k = (m / n) * math.log(2)
    return max(8, int(math.ceil(m))), max(1, int(math.ceil(k)))


class _Bloom:
    """Tiny inline bloom-filter. Bit array backed by ``bytearray``.

    Each hash is :func:`hashlib.blake2b` keyed with the hash slot
    index — this gives ``k`` independent hash functions without
    needing a third-party multi-hash library. ``blake2b`` is a few
    microseconds per call which is plenty fast for a 7-hash check
    on the mempool hot path.
    """

    __slots__ = ("_m_bits", "_k", "_bits", "_n_added")

    def __init__(self, capacity: int, error_rate: float) -> None:
        self._m_bits, self._k = _optimal_m_k(capacity, error_rate)
        # bytearray rounded up to whole bytes.
        self._bits = bytearray((self._m_bits + 7) // 8)
        self._n_added = 0

    def add(self, item: str) -> None:
        for idx in self._hash_indexes(item):
            byte = idx >> 3
            bit = idx & 7
            self._bits[byte] |= 1 << bit
        self._n_added += 1

    def __contains__(self, item: str) -> bool:
        for idx in self._hash_indexes(item):
            byte = idx >> 3
            bit = idx & 7
            if not (self._bits[byte] & (1 << bit)):
                return False
        return True

    def _hash_indexes(self, item: str) -> Iterable[int]:
        data = item.encode("utf-8")
        for k in range(self._k):
            # blake2b accepts a 0–16 byte key — use the slot index
            # as the key so each hash function is independent. The
            # 8-byte digest is plenty for our m_bits range.
            digest = hashlib.blake2b(
                data, digest_size=8, key=k.to_bytes(2, "big")
            ).digest()
            yield int.from_bytes(digest, "big") % self._m_bits

    @property
    def size_bytes(self) -> int:
        return len(self._bits)

    @property
    def n_added(self) -> int:
        return self._n_added


class WatchedWalletIndex:
    """In-memory bloom-filter index over the active leader wallet set.

    Concurrency
    -----------
    The hot-path :meth:`__contains__` is safe to call from the
    mempool subscription's async loop while the refresh task is
    rebuilding. The refresh swaps the bloom + parallel set in a
    single attribute assignment (Python's GIL guarantees the
    assignment is observed atomically; a brief race during rebuild
    sees the OLD bloom, which is harmless).
    """

    def __init__(
        self,
        bloom_capacity: int = DEFAULT_BLOOM_CAPACITY,
        error_rate: float = DEFAULT_BLOOM_ERROR_RATE,
    ) -> None:
        self._capacity = int(bloom_capacity)
        self._error_rate = float(error_rate)
        self._bloom = _Bloom(self._capacity, self._error_rate)
        # Parallel address list — bloom itself can't enumerate, but
        # the eth_subscribe filter needs the explicit list. Kept as
        # a set to deduplicate on incremental `add` calls and
        # converted to a list at snapshot time.
        self._addresses: set[str] = set()

    @staticmethod
    def _normalize(wallet: str) -> str:
        """Lowercase + 0x-prefix-normalise. Mirrors the wallet_universe
        table convention."""
        if not isinstance(wallet, str):
            return ""
        s = wallet.strip().lower()
        if not s:
            return ""
        if not s.startswith("0x"):
            s = "0x" + s
        return s

    def __contains__(self, wallet: str) -> bool:
        norm = self._normalize(wallet)
        if not norm:
            return False
        return norm in self._bloom

    def add(self, wallet: str) -> None:
        """Add a wallet to the bloom + the parallel address set."""
        norm = self._normalize(wallet)
        if not norm:
            return
        if norm in self._addresses:
            return
        self._bloom.add(norm)
        self._addresses.add(norm)

    async def refresh_from_universe(self) -> int:
        """Rebuild the bloom from a fresh ``wallet_universe`` SELECT.

        SQL contract::

            SELECT wallet_address
            FROM wallet_universe
            WHERE depth_tier IN (0, 1)

        Returns the count of entries added.
        """
        try:
            from src.database.connection import get_db
        except Exception as exc:
            logger.warning(
                "WatchedWalletIndex.refresh_from_universe: db unavailable: {!r}",
                exc,
            )
            return 0
        try:
            async with get_db() as conn:
                rows = await conn.fetch(
                    "SELECT wallet_address FROM wallet_universe "
                    "WHERE depth_tier IN (0, 1)"
                )
        except Exception as exc:
            logger.warning(
                "WatchedWalletIndex.refresh_from_universe: query failed: {!r}",
                exc,
            )
            return 0
        # Build the NEW bloom in a local. Atomic swap at the end so
        # the hot path never sees a partially-built bloom.
        new_bloom = _Bloom(self._capacity, self._error_rate)
        new_addresses: set[str] = set()
        for row in rows:
            norm = self._normalize(row["wallet_address"])
            if not norm or norm in new_addresses:
                continue
            new_bloom.add(norm)
            new_addresses.add(norm)
        self._bloom = new_bloom
        self._addresses = new_addresses
        return len(new_addresses)

    async def run_refresh_loop(self, interval_s: int = 300) -> None:
        """Long-running task: refresh every ``interval_s`` seconds."""
        while True:
            try:
                n = await self.refresh_from_universe()
                logger.info(
                    "WatchedWalletIndex: refreshed n={} wallets", n
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception(
                    "WatchedWalletIndex refresh failed: {!r}", exc
                )
            try:
                await asyncio.sleep(interval_s)
            except asyncio.CancelledError:
                raise

    def snapshot_addresses(self) -> list[str]:
        """Return a fresh list of the current addresses."""
        # New list so the subscription is free to keep its own reference.
        return list(self._addresses)

    def __len__(self) -> int:
        return len(self._addresses)
