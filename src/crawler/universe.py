"""Universal Wallet Crawler — the ``wallet_universe`` table maintainer.

Round 6 (The Spine) / Phase 6.D. See docs/ROUND_6_THE_SPINE.md § 3.4.

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

from typing import TYPE_CHECKING, Any

from loguru import logger

from src.database.connection import get_db

try:  # pragma: no cover — metrics import is best-effort.
    from src.monitoring.metrics import (  # type: ignore[attr-defined]
        wallet_universe_promotions_total,
        wallet_universe_size,
        wallet_universe_tier_count,
    )
except Exception:  # pragma: no cover
    wallet_universe_size = None  # type: ignore[assignment]
    wallet_universe_tier_count = None  # type: ignore[assignment]
    wallet_universe_promotions_total = None  # type: ignore[assignment]

if TYPE_CHECKING:
    from src.rpc.client import RPCClient


# Default depth tier on insert (LIGHT). Promotion only happens once the
# nightly review observes enough volume to justify it. Kept as a module
# constant rather than imported from depth_tiers to avoid a circular
# import (depth_tiers imports the universe class).
_DEFAULT_DEPTH_TIER = 2

# Default chunk size for eth_getLogs paging in ``backfill_from_chain``.
# Paid providers cap a single request to ~2k blocks, but the underlying
# RPC client handles further chunking internally — 10k is a logical
# accounting unit for this crawler's progress logging.
_DEFAULT_BACKFILL_CHUNK = 10_000


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
        self._rpc_client = rpc_client

    # ------------------------------------------------------------------ #
    # Hot path                                                            #
    # ------------------------------------------------------------------ #

    async def add_wallet_if_new(
        self,
        wallet: str,
        first_seen_block: int,
    ) -> bool:
        """INSERT a wallet into ``wallet_universe`` if not already present.

        Hot path: called from CLOBChainListener for every decoded
        OrderFilled / OrdersMatched event (both maker and taker). MUST
        be cheap (single statement, indexed PK lookup).

        Args:
            wallet: 0x-prefixed lowercase wallet address.
            first_seen_block: Polygon block number when we first saw
                this wallet trade.

        Returns:
            True if a new row was inserted, False if the wallet was
            already known.
        """
        sql = """
            INSERT INTO wallet_universe (
                wallet_address, first_seen, last_active,
                total_trades_ever, total_volume_usdc_ever,
                depth_tier, first_seen_block, last_active_block
            )
            VALUES ($1, NOW(), NOW(), 0, 0, $2, $3, $3)
            ON CONFLICT (wallet_address) DO NOTHING
            RETURNING wallet_address
        """
        async with get_db() as conn:
            row = await conn.fetchrow(sql, wallet, _DEFAULT_DEPTH_TIER, first_seen_block)
        inserted = row is not None
        if inserted and wallet_universe_size is not None:
            try:
                wallet_universe_size.inc()
            except Exception:  # pragma: no cover — metrics best-effort
                logger.debug("wallet_universe_size.inc() failed", exc_info=True)
        return inserted

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

        Args:
            wallet: 0x-prefixed lowercase address.
            n_trades: Trades observed in this interval.
            volume_usdc: USDC volume in this interval.
            last_active_block: Highest block seen in the interval.
        """
        sql = """
            UPDATE wallet_universe
            SET total_trades_ever      = total_trades_ever + $2,
                total_volume_usdc_ever = total_volume_usdc_ever + $3,
                last_active            = NOW(),
                last_active_block      = GREATEST(
                    COALESCE(last_active_block, 0), $4
                )
            WHERE wallet_address = $1
        """
        async with get_db() as conn:
            async with conn.transaction():
                await conn.execute(
                    sql,
                    wallet,
                    int(n_trades),
                    float(volume_usdc),
                    int(last_active_block),
                )

    # ------------------------------------------------------------------ #
    # Backfill                                                            #
    # ------------------------------------------------------------------ #

    async def backfill_from_chain(
        self,
        from_block: int,
        to_block: int,
        batch_size: int = _DEFAULT_BACKFILL_CHUNK,
    ) -> int:
        """One-time historical scan to populate the universe.

        For each ``[chunk_start, chunk_end]`` slice, we issue a single
        ``eth_getLogs`` against the CLOB contract, extract maker + taker
        from every log, and call ``add_wallet_if_new`` for each. The
        running counter of new rows is returned at the end.

        Idempotent: re-running over the same block range adds 0 new
        wallets thanks to ``ON CONFLICT DO NOTHING``.

        Args:
            from_block: Inclusive lower bound (typically CLOB contract
                deployment block).
            to_block: Inclusive upper bound (typically chain head at
                start of backfill).
            batch_size: Block-range chunk size. Default 10k — the RPC
                client handles further sub-chunking against paid-provider
                getLogs caps.

        Returns:
            Total count of new rows inserted across all chunks.
        """
        if self._rpc_client is None:
            raise RuntimeError(
                "backfill_from_chain requires rpc_client at construction; "
                "got None"
            )
        if batch_size <= 0:
            raise ValueError(f"batch_size must be > 0, got {batch_size}")
        if to_block < from_block:
            return 0

        # Lazy import to avoid a hard dep on the settings module at
        # import time (keeps unit tests that instantiate WalletUniverse()
        # with no RPC client cheap).
        try:
            from src.config import settings  # type: ignore[import-not-found]
            clob_contract = getattr(
                settings, "POLYMARKET_CLOB_CONTRACT_ADDRESS", None
            )
        except Exception:  # pragma: no cover
            clob_contract = None

        filter_obj: dict[str, Any] = {}
        if clob_contract:
            filter_obj["address"] = clob_contract

        total_inserted = 0
        chunk_start = from_block
        while chunk_start <= to_block:
            chunk_end = min(chunk_start + batch_size - 1, to_block)
            try:
                logs = await self._rpc_client.eth_getLogs(
                    filter_obj,
                    from_block=chunk_start,
                    to_block=chunk_end,
                )
            except Exception as exc:
                logger.error(
                    f"backfill_from_chain: eth_getLogs failed for "
                    f"[{chunk_start}, {chunk_end}]: {exc}"
                )
                chunk_start = chunk_end + 1
                continue

            chunk_inserted = 0
            for log in logs or []:
                wallets = _extract_wallets_from_log(log)
                block_num = _extract_block_number(log) or chunk_start
                for wallet in wallets:
                    try:
                        if await self.add_wallet_if_new(wallet, block_num):
                            chunk_inserted += 1
                    except Exception as exc:  # pragma: no cover
                        logger.warning(
                            f"backfill: add_wallet_if_new failed for "
                            f"{wallet[:10]}…: {exc}"
                        )

            total_inserted += chunk_inserted
            logger.info(
                f"backfill chunk [{chunk_start}, {chunk_end}]: "
                f"{len(logs or [])} logs, {chunk_inserted} new wallets "
                f"(running total {total_inserted})"
            )
            chunk_start = chunk_end + 1

        return total_inserted

    # ------------------------------------------------------------------ #
    # Read helpers                                                        #
    # ------------------------------------------------------------------ #

    async def total_size(self) -> int:
        """Returns COUNT(*) FROM wallet_universe.

        Used by the ``polybot_wallet_universe_size`` gauge.
        """
        async with get_db() as conn:
            val = await conn.fetchval("SELECT COUNT(*) FROM wallet_universe")
        return int(val or 0)

    async def tier_counts(self) -> dict[int, int]:
        """Counts of wallets per depth_tier (0/1/2).

        Drives ``polybot_wallet_universe_tier_count{tier}``.
        """
        sql = """
            SELECT depth_tier, COUNT(*) AS n
            FROM wallet_universe
            GROUP BY depth_tier
        """
        async with get_db() as conn:
            rows = await conn.fetch(sql)
        return {int(r["depth_tier"]): int(r["n"]) for r in rows}

    async def by_tier(self, tier: int) -> list[str]:
        """Returns every wallet_address sitting in ``tier``.

        Used by the per-tier enrichment loops (Falcon refresh, strategy
        classifier) and by the nightly review-tiers loop's verification
        pass.
        """
        sql = "SELECT wallet_address FROM wallet_universe WHERE depth_tier = $1"
        async with get_db() as conn:
            rows = await conn.fetch(sql, int(tier))
        return [r["wallet_address"] for r in rows]

    async def set_tier(self, wallet: str, tier: int) -> None:
        """Update a single wallet's depth_tier.

        Emits ``polybot_wallet_universe_promotions_total{from_tier, to_tier}``
        when the tier actually changes (no-op writes don't increment).
        """
        sql = """
            UPDATE wallet_universe
               SET depth_tier = $2,
                   last_tier_review = NOW()
             WHERE wallet_address = $1
            RETURNING depth_tier
        """
        # We need the OLD tier first to know whether this is a transition.
        async with get_db() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "SELECT depth_tier FROM wallet_universe WHERE wallet_address = $1",
                    wallet,
                )
                if row is None:
                    return
                old_tier = int(row["depth_tier"])
                await conn.execute(sql, wallet, int(tier))
        if old_tier != int(tier) and wallet_universe_promotions_total is not None:
            try:
                wallet_universe_promotions_total.labels(
                    from_tier=str(old_tier), to_tier=str(int(tier))
                ).inc()
            except Exception:  # pragma: no cover
                logger.debug(
                    "wallet_universe_promotions_total.inc() failed",
                    exc_info=True,
                )

    async def get_stats(self, wallet: str) -> dict | None:
        """Return the activity stats dict for ``wallet``, or None.

        Shape matches what :func:`AdaptiveDepth.expected_tier` consumes —
        the caller of ``review_tiers`` is expected to enrich this with
        recent-window volume from ``trades_observed``.
        """
        sql = """
            SELECT wallet_address,
                   first_seen,
                   last_active,
                   total_trades_ever,
                   total_volume_usdc_ever,
                   depth_tier,
                   last_tier_review,
                   first_seen_block,
                   last_active_block
              FROM wallet_universe
             WHERE wallet_address = $1
        """
        async with get_db() as conn:
            row = await conn.fetchrow(sql, wallet)
        if row is None:
            return None
        return {
            "wallet_address": row["wallet_address"],
            "first_seen": row["first_seen"],
            "last_active": row["last_active"],
            "total_trades_ever": int(row["total_trades_ever"] or 0),
            "total_volume_usdc_ever": float(row["total_volume_usdc_ever"] or 0.0),
            "depth_tier": int(row["depth_tier"]),
            "last_tier_review": row["last_tier_review"],
            "first_seen_block": row["first_seen_block"],
            "last_active_block": row["last_active_block"],
        }

    async def refresh_tier_count_gauge(self) -> dict[int, int]:
        """Refresh ``polybot_wallet_universe_tier_count{tier}`` from DB.

        Helper for the daemon loop. Returns the counts so the caller can
        log them too.
        """
        counts = await self.tier_counts()
        if wallet_universe_tier_count is not None:
            for tier in (0, 1, 2):
                try:
                    wallet_universe_tier_count.labels(tier=str(tier)).set(
                        counts.get(tier, 0)
                    )
                except Exception:  # pragma: no cover
                    logger.debug(
                        "wallet_universe_tier_count.set() failed",
                        exc_info=True,
                    )
        return counts


# ---------------------------------------------------------------------- #
# Internal helpers                                                        #
# ---------------------------------------------------------------------- #


def _extract_wallets_from_log(log: dict[str, Any]) -> set[str]:
    """Pull maker + taker addresses out of a raw eth_getLogs entry.

    OrderFilled is the only event we currently care about; its first
    two indexed topics are ``maker`` and ``taker`` (see
    src/onchain/event_decoder.py). Both are 32-byte left-padded
    addresses in raw topic form.

    Some log shapes (already-decoded paths) may expose ``maker`` /
    ``taker`` as top-level keys; we accept either. Anything that doesn't
    look like a 0x-prefixed 40-hex address is dropped silently.
    """
    out: set[str] = set()

    # Pre-decoded paths.
    for key in ("maker", "taker", "wallet_address", "counterparty"):
        v = log.get(key)
        norm = _normalize_address(v)
        if norm:
            out.add(norm)

    # Raw topic path. Topics are hex strings; topic[0] is the event sig.
    topics = log.get("topics")
    if isinstance(topics, list):
        for raw in topics[1:3]:  # maker = topic[1], taker = topic[2]
            norm = _normalize_address(raw)
            if norm:
                out.add(norm)
    return out


def _normalize_address(raw: Any) -> str | None:
    """Convert a raw topic / address value to a 0x-prefixed lowercase
    40-hex address, or return None if it doesn't look like one."""
    if not isinstance(raw, str) or not raw:
        return None
    s = raw.lower()
    if not s.startswith("0x"):
        return None
    hex_part = s[2:]
    # Topic form: 32 bytes = 64 hex chars, with leading 0s for an address.
    if len(hex_part) == 64:
        hex_part = hex_part[-40:]
    if len(hex_part) != 40:
        return None
    try:
        int(hex_part, 16)
    except ValueError:
        return None
    return "0x" + hex_part


def _extract_block_number(log: dict[str, Any]) -> int | None:
    """Best-effort block-number extraction from a raw log dict.

    JSON-RPC returns ``blockNumber`` as a hex string; some decoders
    rewrite that to a plain int.
    """
    bn = log.get("blockNumber")
    if bn is None:
        bn = log.get("block_number")
    if isinstance(bn, int):
        return bn
    if isinstance(bn, str):
        try:
            if bn.startswith("0x"):
                return int(bn, 16)
            return int(bn)
        except ValueError:
            return None
    return None
