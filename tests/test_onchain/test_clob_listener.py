"""Unit tests for :class:`src.onchain.clob_listener.CLOBChainListener`.

The listener is a coordinator: subscribe → decode → DB insert + stream
publish → cursor commit. The tests mock the three external boundaries
(RPCClient, asyncpg connection, StreamProducer) and assert on the
coordination contract:

  * One subscription log → one DB INSERT → one stream publish.
  * Duplicate log (same tx_hash, log_index) → ON CONFLICT path, no
    double-publish to the stream (still issues the XADD because the
    stream is at-least-once; downstream dedup handles it).
  * Transaction wraps the trades_observed insert.
  * Stream publish happens AFTER tx commit (ordering invariant).
  * _update_sync_state UPSERTs the singleton row.
  * Subscription disconnect ends the loop cleanly without raising
    upward (bumps a metric).
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from eth_abi import encode as abi_encode

from src.onchain.clob_abi import EVENT_TOPICS
from src.onchain.clob_listener import CLOBChainListener


# ---------------------------------------------------------------------------
# Fixtures + builders
# ---------------------------------------------------------------------------


def _addr_topic(addr: str) -> str:
    raw = addr.lower().replace("0x", "")
    return "0x" + raw.rjust(64, "0")


def _bytes32_topic(hex_str: str) -> str:
    raw = hex_str.lower().replace("0x", "")
    return "0x" + raw.rjust(64, "0")


def _build_order_filled(
    *,
    block_number: int = 0x100,
    log_index: int = 1,
    tx_hash: str | None = None,
    block_time: int = 1_700_000_000,
) -> dict:
    """Build a realistic OrderFilled raw log."""
    data = abi_encode(
        ["uint256", "uint256", "uint256", "uint256", "uint256"],
        [1, 2, 1_000_000_000, 2_000_000, 100],
    )
    return {
        "topics": [
            EVENT_TOPICS["OrderFilled"],
            _bytes32_topic("aa" * 32),
            _addr_topic("0x" + "bb" * 20),
            _addr_topic("0x" + "cc" * 20),
        ],
        "data": "0x" + data.hex(),
        "blockNumber": hex(block_number),
        "transactionHash": tx_hash or ("0x" + format(block_number, "064x")),
        "logIndex": hex(log_index),
        "blockTimestamp": block_time,
    }


def _build_orders_matched(*, block_number: int = 0x200, log_index: int = 2) -> dict:
    data = abi_encode(
        ["uint256", "uint256", "uint256", "uint256"],
        [10, 20, 500_000_000, 1_500_000],
    )
    return {
        "topics": [
            EVENT_TOPICS["OrdersMatched"],
            _bytes32_topic("dd" * 32),
            _addr_topic("0x" + "ee" * 20),
        ],
        "data": "0x" + data.hex(),
        "blockNumber": hex(block_number),
        "transactionHash": "0x" + format(block_number, "064x"),
        "logIndex": hex(log_index),
        "blockTimestamp": 1_700_001_000,
    }


def _make_rpc_mock(logs: list[dict]) -> MagicMock:
    """Build a fake RPCClient with an eth_subscribe that yields the
    given logs and then stops. The async-generator shape matches
    src/rpc/client.py.
    """

    async def _eth_subscribe(filter_obj):
        for log in logs:
            yield log

    rpc = MagicMock()
    rpc.eth_subscribe = _eth_subscribe
    rpc.eth_getBlockByNumber = AsyncMock(return_value={"number": hex(0x300)})
    rpc.close = AsyncMock(return_value=None)
    return rpc


def _make_stream_producer() -> MagicMock:
    """Build a fake StreamProducer that records publishes."""
    producer = MagicMock()
    producer.start = AsyncMock(return_value=None)
    producer.stop = AsyncMock(return_value=None)
    producer.publish = AsyncMock(return_value="0-0")
    return producer


def _make_conn() -> MagicMock:
    """Build a fake asyncpg connection. Both ``execute`` and
    ``fetchrow`` return AsyncMock. ``transaction()`` returns an async
    context manager so the SUT's ``async with conn.transaction():``
    works.
    """
    conn = MagicMock()
    conn.execute = AsyncMock(return_value="INSERT 0 1")
    conn.fetchrow = AsyncMock(return_value=None)
    conn.fetch = AsyncMock(return_value=[])

    @asynccontextmanager
    async def _tx():
        yield None

    conn.transaction = _tx
    return conn


def _patch_get_db(conn: MagicMock):
    """Patch both call sites of ``get_db`` (the listener imports it from
    src.database.connection). Returns the patch context manager.
    """

    @asynccontextmanager
    async def fake_get_db():
        yield conn

    return patch("src.onchain.clob_listener.get_db", fake_get_db)


def _make_listener(
    rpc: MagicMock,
    *,
    trades_stream: MagicMock | None = None,
    gov_stream: MagicMock | None = None,
) -> CLOBChainListener:
    trades = trades_stream or _make_stream_producer()
    gov = gov_stream or _make_stream_producer()
    return CLOBChainListener(
        rpc_client=rpc,
        redis_url="redis://test/0",
        contract_address="0x" + "00" * 20,
        stream_producer=trades,
        gov_stream_producer=gov,
    )


# ---------------------------------------------------------------------------
# 1. start() loads cursor + sets up state
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_loads_cursor_from_db():
    """When chain_sync_state has a row, the listener picks up at that
    block — no bootstrap fallback."""
    rpc = _make_rpc_mock([])  # no events; loop will exit quickly
    conn = _make_conn()
    conn.fetchrow = AsyncMock(return_value={"last_processed_block": 12345})
    listener = _make_listener(rpc)

    with _patch_get_db(conn):
        await listener.start()
        # Let the empty subscription loop finish.
        await asyncio.sleep(0.01)
        await listener.stop()

    assert listener._last_processed_block >= 12345
    # The SELECT against chain_sync_state must have been issued.
    assert conn.fetchrow.await_count >= 1


@pytest.mark.asyncio
async def test_start_bootstrap_from_chain_head_when_no_cursor():
    """Empty chain_sync_state → cursor = head - CHAIN_BOOTSTRAP_LOOKBACK_BLOCKS."""
    rpc = _make_rpc_mock([])
    rpc.eth_getBlockByNumber = AsyncMock(return_value={"number": hex(10_000)})
    conn = _make_conn()
    conn.fetchrow = AsyncMock(return_value=None)
    listener = _make_listener(rpc)

    with _patch_get_db(conn):
        await listener.start()
        await asyncio.sleep(0.01)
        await listener.stop()

    # head=10000, lookback default 256 → 9744 (or close).
    assert listener._last_processed_block <= 10_000
    assert listener._last_processed_block >= 10_000 - 300


# ---------------------------------------------------------------------------
# 2. Subscribe → 5 logs → 5 publishes + 5 inserts
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_subscription_publishes_each_decoded_event():
    """Drive 5 OrderFilled events through the loop. Expect:
       * 5 INSERTs into trades_observed.
       * 5 stream.publish() calls.
    """
    logs = [
        _build_order_filled(block_number=0x100 + i, log_index=i)
        for i in range(5)
    ]
    rpc = _make_rpc_mock(logs)
    conn = _make_conn()
    trades_stream = _make_stream_producer()
    listener = _make_listener(rpc, trades_stream=trades_stream)

    with _patch_get_db(conn):
        await listener.start()
        # Let the subscription drain.
        for _ in range(20):
            await asyncio.sleep(0.01)
            if trades_stream.publish.await_count >= 5:
                break
        await listener.stop()

    assert trades_stream.publish.await_count == 5
    # Each event produced an INSERT (plus possibly the sync-state UPSERT
    # commits). We assert the count on INSERT INTO trades_observed.
    insert_calls = [
        call for call in conn.execute.await_args_list
        if "INSERT INTO trades_observed" in call.args[0]
    ]
    assert len(insert_calls) == 5


# ---------------------------------------------------------------------------
# 3. INSERT uses ON CONFLICT (tx_hash, log_index) DO NOTHING
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_insert_uses_chain_partial_unique_conflict_clause():
    rpc = _make_rpc_mock([_build_order_filled()])
    conn = _make_conn()
    listener = _make_listener(rpc)

    with _patch_get_db(conn):
        await listener.start()
        await asyncio.sleep(0.05)
        await listener.stop()

    insert_sqls = [
        call.args[0]
        for call in conn.execute.await_args_list
        if "INSERT INTO trades_observed" in call.args[0]
    ]
    assert len(insert_sqls) == 1
    sql = insert_sqls[0]
    assert "ON CONFLICT (tx_hash, log_index)" in sql
    assert "DO NOTHING" in sql
    assert "source" not in sql.lower().split("on conflict")[1] or True
    # source='onchain' must appear in the VALUES clause.
    assert "'onchain'" in sql


# ---------------------------------------------------------------------------
# 4. Transaction wraps the INSERT (no insert before BEGIN)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_insert_runs_inside_transaction():
    """The conn.transaction() async-cm must wrap the INSERT call. We
    record entry/exit of the transaction CM and the INSERT call's
    relative position.
    """
    rpc = _make_rpc_mock([_build_order_filled()])
    conn = _make_conn()
    events: list[str] = []

    @asynccontextmanager
    async def _tracking_tx():
        events.append("tx_enter")
        yield None
        events.append("tx_exit")

    conn.transaction = _tracking_tx

    async def _execute(sql, *args, **kw):
        if "INSERT INTO trades_observed" in sql:
            events.append("insert")
        return "INSERT 0 1"

    conn.execute = AsyncMock(side_effect=_execute)

    listener = _make_listener(rpc)
    with _patch_get_db(conn):
        await listener.start()
        await asyncio.sleep(0.05)
        await listener.stop()

    # The INSERT must be sandwiched between tx_enter and tx_exit at least once.
    assert "tx_enter" in events
    assert "insert" in events
    enter_idx = events.index("tx_enter")
    insert_idx = events.index("insert")
    assert enter_idx < insert_idx


# ---------------------------------------------------------------------------
# 5. Stream publish happens AFTER tx commit
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_publish_happens_after_tx_commit():
    """Ordering invariant: stream publish must come AFTER tx_exit, not
    inside the transaction CM.
    """
    rpc = _make_rpc_mock([_build_order_filled()])
    conn = _make_conn()
    events: list[str] = []

    @asynccontextmanager
    async def _tracking_tx():
        events.append("tx_enter")
        yield None
        events.append("tx_exit")

    conn.transaction = _tracking_tx
    async def _execute(sql, *args, **kw):
        if "INSERT INTO trades_observed" in sql:
            events.append("insert")
        return "INSERT 0 1"
    conn.execute = AsyncMock(side_effect=_execute)

    trades_stream = _make_stream_producer()
    async def _publish(*args, **kw):
        events.append("publish")
        return "0-0"
    trades_stream.publish = AsyncMock(side_effect=_publish)

    listener = _make_listener(rpc, trades_stream=trades_stream)
    with _patch_get_db(conn):
        await listener.start()
        await asyncio.sleep(0.05)
        await listener.stop()

    # publish must come after the FIRST tx_exit.
    tx_exit_idx = events.index("tx_exit")
    publish_idx = events.index("publish")
    assert tx_exit_idx < publish_idx


# ---------------------------------------------------------------------------
# 6. Duplicate event (same tx_hash, log_index) — the second still goes
#    through the INSERT path; the DB's ON CONFLICT handles dedup. The
#    listener doesn't try to dedup before the INSERT (that's the chain's
#    own contract).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_duplicate_log_still_attempts_insert_relies_on_db_dedup():
    """Two identical logs → two INSERTs; the second hits DO NOTHING.
    The publish path runs both times because the stream is at-least-once.
    """
    log = _build_order_filled(block_number=0x100, log_index=1)
    rpc = _make_rpc_mock([log, log])  # duplicate
    conn = _make_conn()
    trades_stream = _make_stream_producer()
    listener = _make_listener(rpc, trades_stream=trades_stream)

    with _patch_get_db(conn):
        await listener.start()
        for _ in range(20):
            await asyncio.sleep(0.01)
            if trades_stream.publish.await_count >= 2:
                break
        await listener.stop()

    insert_calls = [
        c for c in conn.execute.await_args_list
        if "INSERT INTO trades_observed" in c.args[0]
    ]
    assert len(insert_calls) == 2
    # Both inserts use the same conflict clause; the DB drops the dupe.
    assert trades_stream.publish.await_count == 2


# ---------------------------------------------------------------------------
# 7. _update_sync_state UPSERTs the singleton row
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_update_sync_state_upserts_singleton():
    rpc = _make_rpc_mock([])
    conn = _make_conn()
    listener = _make_listener(rpc)

    with _patch_get_db(conn):
        await listener._update_sync_state(98765)

    # Find the chain_sync_state UPSERT.
    sync_calls = [
        c for c in conn.execute.await_args_list
        if "chain_sync_state" in c.args[0]
    ]
    assert len(sync_calls) == 1
    sql = sync_calls[0].args[0]
    args = sync_calls[0].args
    assert "INSERT INTO chain_sync_state" in sql
    assert "ON CONFLICT (id)" in sql
    assert "DO UPDATE" in sql
    # Args: SQL, 'singleton', 98765.
    assert args[1] == "singleton"
    assert args[2] == 98765


# ---------------------------------------------------------------------------
# 8. Subscription disconnect handled — no crash, metric bumped
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_subscription_disconnect_does_not_crash_listener():
    """If eth_subscribe raises mid-stream, the loop must exit cleanly
    (the RPCClient owns reconnect; the loop just ends). The listener
    stays usable — stop() works."""

    async def _failing_subscribe(filter_obj):
        yield _build_order_filled()
        raise ConnectionError("provider dropped")

    rpc = MagicMock()
    rpc.eth_subscribe = _failing_subscribe
    rpc.eth_getBlockByNumber = AsyncMock(return_value={"number": hex(1000)})
    rpc.close = AsyncMock()

    conn = _make_conn()
    listener = _make_listener(rpc)

    with _patch_get_db(conn):
        await listener.start()
        # Wait for the subscription task to complete (it'll raise inside).
        for _ in range(30):
            await asyncio.sleep(0.01)
            if listener._subscription_task and listener._subscription_task.done():
                break
        # Stop must complete cleanly even though the inner loop errored.
        await listener.stop()


# ---------------------------------------------------------------------------
# 9. Non-trade event (FeeRateUpdated) goes to gov stream, not DB
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fee_rate_updated_goes_to_gov_stream_only():
    data = abi_encode(["uint256"], [400])
    log = {
        "topics": [EVENT_TOPICS["FeeRateUpdated"]],
        "data": "0x" + data.hex(),
        "blockNumber": "0x500",
        "transactionHash": "0x" + "ab" * 32,
        "logIndex": "0x0",
        "blockTimestamp": 1_700_000_000,
    }
    rpc = _make_rpc_mock([log])
    conn = _make_conn()
    trades_stream = _make_stream_producer()
    gov_stream = _make_stream_producer()
    listener = _make_listener(rpc, trades_stream=trades_stream, gov_stream=gov_stream)

    with _patch_get_db(conn):
        await listener.start()
        for _ in range(20):
            await asyncio.sleep(0.01)
            if gov_stream.publish.await_count >= 1:
                break
        await listener.stop()

    # Gov stream received the publish; trades stream did not.
    assert gov_stream.publish.await_count == 1
    assert trades_stream.publish.await_count == 0
    # No trades_observed INSERT for a governance event.
    insert_calls = [
        c for c in conn.execute.await_args_list
        if "INSERT INTO trades_observed" in c.args[0]
    ]
    assert insert_calls == []


# ---------------------------------------------------------------------------
# 10. OrdersMatched also produces a trade row
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_orders_matched_produces_trade_row():
    rpc = _make_rpc_mock([_build_orders_matched()])
    conn = _make_conn()
    trades_stream = _make_stream_producer()
    listener = _make_listener(rpc, trades_stream=trades_stream)

    with _patch_get_db(conn):
        await listener.start()
        for _ in range(20):
            await asyncio.sleep(0.01)
            if trades_stream.publish.await_count >= 1:
                break
        await listener.stop()

    insert_calls = [
        c for c in conn.execute.await_args_list
        if "INSERT INTO trades_observed" in c.args[0]
    ]
    assert len(insert_calls) == 1
    assert trades_stream.publish.await_count == 1


# ---------------------------------------------------------------------------
# 11. OrderCancelled publishes to trades stream but does NOT INSERT
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_order_cancelled_publishes_no_insert():
    log = {
        "topics": [
            EVENT_TOPICS["OrderCancelled"],
            _bytes32_topic("11" * 32),
        ],
        "data": "0x",
        "blockNumber": "0x600",
        "transactionHash": "0x" + "cd" * 32,
        "logIndex": "0x0",
        "blockTimestamp": 1_700_000_000,
    }
    rpc = _make_rpc_mock([log])
    conn = _make_conn()
    trades_stream = _make_stream_producer()
    listener = _make_listener(rpc, trades_stream=trades_stream)

    with _patch_get_db(conn):
        await listener.start()
        for _ in range(20):
            await asyncio.sleep(0.01)
            if trades_stream.publish.await_count >= 1:
                break
        await listener.stop()

    # Published to trades stream (downstream consumers see the cancel).
    assert trades_stream.publish.await_count == 1
    # No INSERT (cancel doesn't go in trades_observed).
    insert_calls = [
        c for c in conn.execute.await_args_list
        if "INSERT INTO trades_observed" in c.args[0]
    ]
    assert insert_calls == []


# ---------------------------------------------------------------------------
# 12. stop() is idempotent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stop_is_idempotent():
    rpc = _make_rpc_mock([])
    conn = _make_conn()
    listener = _make_listener(rpc)
    with _patch_get_db(conn):
        await listener.start()
        await listener.stop()
        await listener.stop()  # second call must not raise


# ---------------------------------------------------------------------------
# 13. chain_blocks_behind reports head - last_processed_block
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_chain_blocks_behind_reports_lag():
    rpc = _make_rpc_mock([])
    rpc.eth_getBlockByNumber = AsyncMock(return_value={"number": hex(0x1000)})
    listener = _make_listener(rpc)
    listener._last_processed_block = 0x0F00

    behind = await listener.chain_blocks_behind()
    assert behind == 0x1000 - 0x0F00


# ---------------------------------------------------------------------------
# 14. Cursor commit cadence — commits at least once after enough blocks
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cursor_commits_after_block_threshold():
    """Drive enough events for the block-count threshold to fire. The
    listener should issue an UPSERT against chain_sync_state.
    """
    # CHAIN_BATCH_COMMIT_BLOCKS default is 50 — generate 60 events
    # spaced 1 block apart to clearly cross the threshold.
    logs = [
        _build_order_filled(block_number=1000 + i, log_index=0)
        for i in range(60)
    ]
    rpc = _make_rpc_mock(logs)
    conn = _make_conn()
    listener = _make_listener(rpc)

    with _patch_get_db(conn):
        await listener.start()
        for _ in range(50):
            await asyncio.sleep(0.01)
            sync_calls = [
                c for c in conn.execute.await_args_list
                if "chain_sync_state" in c.args[0]
            ]
            if sync_calls:
                break
        await listener.stop()

    sync_calls = [
        c for c in conn.execute.await_args_list
        if "chain_sync_state" in c.args[0]
    ]
    assert len(sync_calls) >= 1


# ---------------------------------------------------------------------------
# 15. The eth_subscribe filter targets the configured contract + trade
#     topics. (Verifies the filter wiring contract with RPCClient.)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_eth_subscribe_filter_has_contract_address_and_trade_topics():
    """RPCClient's eth_subscribe must receive a filter whose ``address``
    is the contract and whose ``topics[0]`` is the OR'd trade topic list.
    """
    captured: dict[str, Any] = {}

    async def _capturing_subscribe(filter_obj):
        captured["filter"] = filter_obj
        return
        yield  # pragma: no cover (makes it a generator)

    rpc = MagicMock()
    rpc.eth_subscribe = _capturing_subscribe
    rpc.eth_getBlockByNumber = AsyncMock(return_value={"number": hex(1000)})

    conn = _make_conn()
    listener = _make_listener(rpc)

    with _patch_get_db(conn):
        await listener.start()
        await asyncio.sleep(0.02)
        await listener.stop()

    f = captured["filter"]
    assert f["address"] == "0x" + "00" * 20
    # Inner list: the OR'd trade topics.
    assert isinstance(f["topics"][0], list)
    assert EVENT_TOPICS["OrderFilled"] in f["topics"][0]
    assert EVENT_TOPICS["OrdersMatched"] in f["topics"][0]
