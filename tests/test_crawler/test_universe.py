"""Unit tests for src/crawler/universe.py — WalletUniverse.

We mock the asyncpg connection via the project's standard
``patch("src.crawler.universe.get_db", ...)`` pattern (mirrors
test_observer/test_position_tracker.py). Tests cover:

  * add_wallet_if_new: new → True, dup → False
  * update_activity: counters increment; last_active_block GREATEST()
    intent is encoded in the SQL we issue.
  * backfill_from_chain: RPC mock returns N synthetic events → N new
    wallets; repeated run yields 0 (idempotency).
  * by_tier / total_size / tier_counts / get_stats: shape correctness.
  * set_tier: emits ``wallet_universe_promotions_total`` on transition
    only.
  * Internal _extract_wallets_from_log: topic-encoded + pre-decoded
    paths.
"""

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import src.crawler.universe as universe_mod
from src.crawler.universe import (
    WalletUniverse,
    _extract_block_number,
    _extract_wallets_from_log,
    _normalize_address,
)

# ---------------------------------------------------------------------- #
# Helpers                                                                 #
# ---------------------------------------------------------------------- #


def _make_conn():
    """Return a mock asyncpg connection.

    ``conn.transaction()`` returns a no-op async CM so production code
    using ``async with conn.transaction():`` works unchanged.
    """
    conn = AsyncMock()
    conn.execute = AsyncMock()
    conn.fetch = AsyncMock(return_value=[])
    conn.fetchrow = AsyncMock(return_value=None)
    conn.fetchval = AsyncMock(return_value=0)

    @asynccontextmanager
    async def _tx():
        yield None

    conn.transaction = MagicMock(side_effect=lambda *a, **kw: _tx())
    return conn


def _patch_get_db(conn):
    @asynccontextmanager
    async def _fake_get_db():
        yield conn

    return patch("src.crawler.universe.get_db", _fake_get_db)


# ---------------------------------------------------------------------- #
# add_wallet_if_new                                                       #
# ---------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_add_wallet_if_new_inserts_new_wallet():
    conn = _make_conn()
    # RETURNING wallet_address fires when the INSERT lands.
    conn.fetchrow = AsyncMock(return_value={"wallet_address": "0xabc"})

    u = WalletUniverse()
    with _patch_get_db(conn):
        result = await u.add_wallet_if_new("0xabc", 1234)

    assert result is True
    assert conn.fetchrow.await_count == 1
    sql = conn.fetchrow.await_args.args[0]
    assert "INSERT INTO wallet_universe" in sql
    assert "ON CONFLICT (wallet_address) DO NOTHING" in sql
    assert "RETURNING" in sql
    # Bind args: wallet, default tier (2), first_seen_block.
    assert conn.fetchrow.await_args.args[1:] == ("0xabc", 2, 1234)


@pytest.mark.asyncio
async def test_add_wallet_if_new_returns_false_on_conflict():
    conn = _make_conn()
    # ON CONFLICT DO NOTHING with RETURNING → no row returned.
    conn.fetchrow = AsyncMock(return_value=None)

    u = WalletUniverse()
    with _patch_get_db(conn):
        result = await u.add_wallet_if_new("0xabc", 1234)

    assert result is False


@pytest.mark.asyncio
async def test_add_wallet_if_new_increments_size_gauge_on_insert():
    conn = _make_conn()
    conn.fetchrow = AsyncMock(return_value={"wallet_address": "0xabc"})

    gauge = MagicMock()
    u = WalletUniverse()
    with _patch_get_db(conn), patch.object(
        universe_mod, "wallet_universe_size", gauge
    ):
        await u.add_wallet_if_new("0xabc", 1234)

    gauge.inc.assert_called_once()


@pytest.mark.asyncio
async def test_add_wallet_if_new_does_not_increment_gauge_on_conflict():
    conn = _make_conn()
    conn.fetchrow = AsyncMock(return_value=None)

    gauge = MagicMock()
    u = WalletUniverse()
    with _patch_get_db(conn), patch.object(
        universe_mod, "wallet_universe_size", gauge
    ):
        await u.add_wallet_if_new("0xabc", 1234)

    gauge.inc.assert_not_called()


# ---------------------------------------------------------------------- #
# update_activity                                                         #
# ---------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_update_activity_issues_incrementing_update_with_greatest():
    conn = _make_conn()
    u = WalletUniverse()
    with _patch_get_db(conn):
        await u.update_activity(
            "0xabc", n_trades=5, volume_usdc=123.45, last_active_block=200
        )

    assert conn.execute.await_count == 1
    sql = conn.execute.await_args.args[0]
    assert "UPDATE wallet_universe" in sql
    # Critical pieces: incrementing, last_active = NOW(), GREATEST() on block.
    assert "total_trades_ever      = total_trades_ever + $2" in sql
    assert "total_volume_usdc_ever = total_volume_usdc_ever + $3" in sql
    assert "last_active            = NOW()" in sql
    assert "GREATEST(" in sql
    assert conn.execute.await_args.args[1:] == ("0xabc", 5, 123.45, 200)


@pytest.mark.asyncio
async def test_update_activity_runs_inside_a_transaction():
    conn = _make_conn()
    u = WalletUniverse()
    with _patch_get_db(conn):
        await u.update_activity("0xabc", 1, 1.0, 1)

    # The MagicMock-wrapped transaction() factory captured one call.
    assert conn.transaction.call_count == 1


# ---------------------------------------------------------------------- #
# Read helpers: total_size / tier_counts / by_tier / get_stats            #
# ---------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_total_size_returns_count():
    conn = _make_conn()
    conn.fetchval = AsyncMock(return_value=42)
    u = WalletUniverse()
    with _patch_get_db(conn):
        size = await u.total_size()
    assert size == 42


@pytest.mark.asyncio
async def test_tier_counts_aggregates_group_by():
    conn = _make_conn()
    conn.fetch = AsyncMock(
        return_value=[
            {"depth_tier": 0, "n": 200},
            {"depth_tier": 1, "n": 2_000},
            {"depth_tier": 2, "n": 1_500_000},
        ]
    )
    u = WalletUniverse()
    with _patch_get_db(conn):
        counts = await u.tier_counts()
    assert counts == {0: 200, 1: 2_000, 2: 1_500_000}


@pytest.mark.asyncio
async def test_by_tier_returns_wallet_list():
    conn = _make_conn()
    conn.fetch = AsyncMock(
        return_value=[{"wallet_address": "0xa"}, {"wallet_address": "0xb"}]
    )
    u = WalletUniverse()
    with _patch_get_db(conn):
        wallets = await u.by_tier(0)
    assert wallets == ["0xa", "0xb"]
    assert conn.fetch.await_args.args[1] == 0


@pytest.mark.asyncio
async def test_get_stats_returns_dict_or_none():
    conn = _make_conn()
    # Missing wallet → None
    conn.fetchrow = AsyncMock(return_value=None)
    u = WalletUniverse()
    with _patch_get_db(conn):
        assert await u.get_stats("0xnope") is None

    # Existing wallet → dict
    conn.fetchrow = AsyncMock(
        return_value={
            "wallet_address": "0xabc",
            "first_seen": "ts1",
            "last_active": "ts2",
            "total_trades_ever": 10,
            "total_volume_usdc_ever": 1234.5,
            "depth_tier": 1,
            "last_tier_review": None,
            "first_seen_block": 100,
            "last_active_block": 200,
        }
    )
    with _patch_get_db(conn):
        stats = await u.get_stats("0xabc")
    assert stats is not None
    assert stats["wallet_address"] == "0xabc"
    assert stats["total_trades_ever"] == 10
    assert stats["total_volume_usdc_ever"] == 1234.5
    assert stats["depth_tier"] == 1


# ---------------------------------------------------------------------- #
# set_tier                                                                #
# ---------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_set_tier_emits_promotion_counter_on_transition():
    conn = _make_conn()
    # Current row sits at tier 2 — we'll promote to tier 1.
    conn.fetchrow = AsyncMock(return_value={"depth_tier": 2})

    counter = MagicMock()
    counter.labels = MagicMock(return_value=MagicMock(inc=MagicMock()))
    u = WalletUniverse()
    with _patch_get_db(conn), patch.object(
        universe_mod, "wallet_universe_promotions_total", counter
    ):
        await u.set_tier("0xabc", 1)

    counter.labels.assert_called_once_with(from_tier="2", to_tier="1")
    counter.labels.return_value.inc.assert_called_once()
    # And the UPDATE itself ran.
    assert conn.execute.await_count == 1
    update_sql = conn.execute.await_args.args[0]
    assert "UPDATE wallet_universe" in update_sql
    assert "depth_tier = $2" in update_sql


@pytest.mark.asyncio
async def test_set_tier_no_emit_when_tier_unchanged():
    conn = _make_conn()
    conn.fetchrow = AsyncMock(return_value={"depth_tier": 1})

    counter = MagicMock()
    counter.labels = MagicMock(return_value=MagicMock(inc=MagicMock()))
    u = WalletUniverse()
    with _patch_get_db(conn), patch.object(
        universe_mod, "wallet_universe_promotions_total", counter
    ):
        await u.set_tier("0xabc", 1)  # same tier

    counter.labels.assert_not_called()


@pytest.mark.asyncio
async def test_set_tier_no_op_when_wallet_missing():
    conn = _make_conn()
    # No existing row.
    conn.fetchrow = AsyncMock(return_value=None)
    counter = MagicMock()
    u = WalletUniverse()
    with _patch_get_db(conn), patch.object(
        universe_mod, "wallet_universe_promotions_total", counter
    ):
        await u.set_tier("0xghost", 1)

    # We issue only the SELECT, no UPDATE, no counter increment.
    assert conn.execute.await_count == 0
    counter.labels.assert_not_called()


# ---------------------------------------------------------------------- #
# backfill_from_chain                                                     #
# ---------------------------------------------------------------------- #


def _synth_log(
    *,
    maker: str = "0x" + "11" * 20,
    taker: str = "0x" + "22" * 20,
    block: int = 1000,
) -> dict:
    """Return a fake eth_getLogs entry in the pre-decoded shape — easier
    to assert on than 32-byte topic encoding."""
    return {
        "maker": maker,
        "taker": taker,
        "blockNumber": hex(block),
    }


@pytest.mark.asyncio
async def test_backfill_from_chain_requires_rpc_client():
    u = WalletUniverse(rpc_client=None)
    with pytest.raises(RuntimeError):
        await u.backfill_from_chain(0, 10)


@pytest.mark.asyncio
async def test_backfill_from_chain_adds_n_unique_wallets():
    # Two synthetic events → 4 wallets total but only 4 distinct.
    logs_chunk = [
        _synth_log(maker="0x" + "aa" * 20, taker="0x" + "bb" * 20, block=10),
        _synth_log(maker="0x" + "cc" * 20, taker="0x" + "dd" * 20, block=11),
    ]

    rpc = AsyncMock()
    rpc.eth_getLogs = AsyncMock(return_value=logs_chunk)

    u = WalletUniverse(rpc_client=rpc)
    # Track every add_wallet_if_new call; simulate "all wallets new".
    u.add_wallet_if_new = AsyncMock(return_value=True)  # type: ignore[method-assign]

    inserted = await u.backfill_from_chain(0, 50_000, batch_size=10_000)

    # 4 distinct wallets across the 2 events.
    assert inserted == 4 * 6  # 6 chunks of size 10k cover [0, 50000]
    # Sanity: eth_getLogs was paged.
    assert rpc.eth_getLogs.await_count == 6


@pytest.mark.asyncio
async def test_backfill_from_chain_idempotent_on_rerun():
    """Second run over the same range: ON CONFLICT DO NOTHING → 0 new."""
    logs_chunk = [
        _synth_log(maker="0x" + "aa" * 20, taker="0x" + "bb" * 20, block=10),
    ]
    rpc = AsyncMock()
    rpc.eth_getLogs = AsyncMock(return_value=logs_chunk)

    u = WalletUniverse(rpc_client=rpc)
    # Second run: every wallet already present → add_wallet_if_new returns False.
    u.add_wallet_if_new = AsyncMock(return_value=False)  # type: ignore[method-assign]

    inserted = await u.backfill_from_chain(0, 9_999, batch_size=10_000)
    assert inserted == 0


@pytest.mark.asyncio
async def test_backfill_from_chain_skips_chunk_on_rpc_error():
    # First chunk: RPC raises. Second chunk: returns one log.
    logs_chunk = [_synth_log(maker="0x" + "aa" * 20, taker="0x" + "bb" * 20)]

    rpc = AsyncMock()
    rpc.eth_getLogs = AsyncMock(side_effect=[RuntimeError("boom"), logs_chunk])

    u = WalletUniverse(rpc_client=rpc)
    u.add_wallet_if_new = AsyncMock(return_value=True)  # type: ignore[method-assign]

    inserted = await u.backfill_from_chain(0, 19_999, batch_size=10_000)
    # Only the second chunk's 2 wallets got added.
    assert inserted == 2


@pytest.mark.asyncio
async def test_backfill_from_chain_validates_batch_size():
    rpc = AsyncMock()
    u = WalletUniverse(rpc_client=rpc)
    with pytest.raises(ValueError):
        await u.backfill_from_chain(0, 10, batch_size=0)


# ---------------------------------------------------------------------- #
# Internal helpers                                                        #
# ---------------------------------------------------------------------- #


def test_normalize_address_accepts_topic_form():
    # 32-byte left-padded address (Solidity indexed-topic encoding).
    topic = "0x" + "00" * 12 + "ab" * 20
    norm = _normalize_address(topic)
    assert norm == "0x" + "ab" * 20


def test_normalize_address_rejects_garbage():
    assert _normalize_address(None) is None
    assert _normalize_address("") is None
    assert _normalize_address("no-prefix") is None
    assert _normalize_address("0xZZ") is None


def test_extract_wallets_from_log_pre_decoded():
    log = {"maker": "0x" + "aa" * 20, "taker": "0x" + "bb" * 20}
    wallets = _extract_wallets_from_log(log)
    assert wallets == {"0x" + "aa" * 20, "0x" + "bb" * 20}


def test_extract_wallets_from_log_topic_encoded():
    sig = "0x" + "ff" * 32  # event signature, ignored
    maker = "0x" + "00" * 12 + "11" * 20
    taker = "0x" + "00" * 12 + "22" * 20
    log = {"topics": [sig, maker, taker]}
    wallets = _extract_wallets_from_log(log)
    assert wallets == {"0x" + "11" * 20, "0x" + "22" * 20}


def test_extract_block_number_hex_and_int():
    assert _extract_block_number({"blockNumber": "0x10"}) == 16
    assert _extract_block_number({"blockNumber": 42}) == 42
    assert _extract_block_number({"block_number": 7}) == 7
    assert _extract_block_number({}) is None
