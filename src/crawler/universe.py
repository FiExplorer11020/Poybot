"""Universal Wallet Crawler — the ``wallet_universe`` table maintainer.

WAVE-1 ARCHITECT SKELETON. Bodies intentionally not implemented; Wave 2
fills them in. See docs/ROUND_6_THE_SPINE.md § 3.4.

The audit estimates 1.5M wallets have traded on Polymarket. Today we
track ~200. After Round 6: we track all of them, with adaptive depth.

Population strategy (one-time backfill):
  * Scan every block since CLOB contract deployment.
  * Extract ``maker`` and ``taker`` from every OrderFilled event.
  * INSERT INTO wallet_universe ON CONFLICT DO NOTHING.

Ongoing maintenance:
  * Each on-chain event from CLOBChainListener checks the wallet
    against wallet_universe and inserts if new.

Volume estimate:
  * 1.5M wallets × avg 20 trades = 30M edges.
  * Trivial for partitioned Postgres (already at-scale-ready post-R2).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.rpc.client import RPCClient


class WalletUniverse:
    """Maintains the ``wallet_universe`` table.

    Public API:
      * ``add_wallet_if_new(wallet, first_seen_block)`` — hot path
        used by CLOBChainListener on every decoded event.
      * ``update_activity(wallet, n_trades, volume_usdc, last_active_block)``
        — periodic activity stat rollup.
      * ``backfill_from_chain(from_block, to_block)`` — one-time
        historical scan using the paid-RPC pool.
      * ``total_size()`` — for the ``polybot_wallet_universe_size`` gauge.
    """

    def __init__(self, rpc_client: "RPCClient | None" = None) -> None:
        """
        Args:
            rpc_client: Required only for ``backfill_from_chain``;
                hot-path methods (``add_wallet_if_new`` and
                ``update_activity``) only touch Postgres.
        """
        # TODO: implement in Wave 2 — see ROUND_6_THE_SPINE.md § 3.4
        raise NotImplementedError("Wave 2 — see ROUND_6_THE_SPINE.md § 3.4")

    async def add_wallet_if_new(
        self,
        wallet: str,
        first_seen_block: int,
    ) -> bool:
        """INSERT a wallet into ``wallet_universe`` if not already present.

        Hot path: called from CLOBChainListener for every decoded
        OrderFilled / OrdersMatched event (both maker and taker). MUST
        be cheap (single statement, indexed PK lookup).

        Implementation contract (Wave 2):

            INSERT INTO wallet_universe (
                wallet_address, first_seen, last_active,
                total_trades_ever, total_volume_usdc_ever,
                depth_tier, first_seen_block, last_active_block
            )
            VALUES ($1, NOW(), NOW(), 0, 0,
                    {DEFAULT_DEPTH_TIER}, $2, $2)
            ON CONFLICT (wallet_address) DO NOTHING;

        Args:
            wallet: 0x-prefixed lowercase wallet address.
            first_seen_block: Polygon block number when we first saw
                this wallet trade.

        Returns:
            True if a new row was inserted, False if the wallet was
            already known. Useful for the ``polybot_wallet_universe_size``
            gauge increment.
        """
        raise NotImplementedError("Wave 2 — see ROUND_6_THE_SPINE.md § 3.4")

    async def update_activity(
        self,
        wallet: str,
        n_trades: int,
        volume_usdc: float,
        last_active_block: int,
    ) -> None:
        """Roll up activity stats onto an existing wallet_universe row.

        Called by a periodic batch (e.g. every 5 min from the crawler
        daemon) that aggregates the last interval's trades by wallet
        and pushes the totals. Doing this in batch (vs per-trade
        UPDATE) keeps the hot-path INSERT cheap.

        Implementation (Wave 2):

            UPDATE wallet_universe
            SET total_trades_ever     = total_trades_ever + $2,
                total_volume_usdc_ever = total_volume_usdc_ever + $3,
                last_active            = NOW(),
                last_active_block      = GREATEST(last_active_block, $4)
            WHERE wallet_address = $1;

        Args:
            wallet: 0x-prefixed lowercase address.
            n_trades: Trades observed in this interval.
            volume_usdc: USDC volume in this interval.
            last_active_block: Highest block seen in the interval.
        """
        raise NotImplementedError("Wave 2 — see ROUND_6_THE_SPINE.md § 3.4")

    async def backfill_from_chain(
        self,
        from_block: int,
        to_block: int,
    ) -> int:
        """One-time historical scan to populate the universe.

        Pseudocode::

            for chunk_start in range(from_block, to_block, CHUNK_SIZE):
                chunk_end = min(chunk_start + CHUNK_SIZE, to_block)
                logs = await rpc.eth_getLogs(filter, chunk_start, chunk_end)
                wallets = {extract_maker(l) for l in logs} | {extract_taker(l) for l in logs}
                inserted = await self._batch_insert_wallets(wallets, chunk_start)
                total_inserted += inserted
                metrics.wallet_universe_size.set(...)

        Uses the paid-RPC pool (Alchemy / QuickNode) — this is the only
        time we hit them heavily again. Expected wall time: 6–12h.

        Args:
            from_block: Inclusive lower bound (typically CLOB contract
                deployment block).
            to_block: Inclusive upper bound (typically chain head at
                start of backfill).

        Returns:
            Total count of new rows inserted across all chunks.
        """
        raise NotImplementedError("Wave 2 — see ROUND_6_THE_SPINE.md § 3.4")

    async def total_size(self) -> int:
        """Returns COUNT(*) FROM wallet_universe.

        Used by the ``polybot_wallet_universe_size`` gauge. Wave 2
        caches the result for ~30s to keep /metrics scrapes cheap when
        the table is large.
        """
        raise NotImplementedError("Wave 2 — see ROUND_6_THE_SPINE.md § 3.4")

    async def tier_counts(self) -> dict[int, int]:
        """Counts of wallets per depth_tier (0/1/2).

        Drives ``polybot_wallet_universe_tier_count{tier}``. Wave 2:
        single GROUP BY query, cached briefly.
        """
        raise NotImplementedError("Wave 2 — see ROUND_6_THE_SPINE.md § 3.4")
