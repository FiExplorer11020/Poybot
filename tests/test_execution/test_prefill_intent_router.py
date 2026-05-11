"""Tests for :class:`src.execution.prefill.intent_router.IntentRouter`.

Covers the R7 § 3.6 decision tree:
  * killswitch strict-path consult (both initial gate AND TOCTOU re-check)
  * confidence-engine gate
  * position-size cap
  * cooldown
  * SHADOW vs LIVE branching
  * pool_miss accounting
  * mempool_observations INSERT
  * metrics (decisions_total + latency_seconds)
  * resilience against handler-internal exceptions

Mocks every collaborator: pool, paper_trader, live_trader,
confidence_engine, risk_manager, killswitch, asyncpg connection.
"""
from __future__ import annotations

import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.execution.prefill import intent_router as ir_module
from src.execution.prefill.intent_router import (
    IntentRouter,
    RESULT_COOLDOWN,
    RESULT_CONFIDENCE_SKIP,
    RESULT_ERROR,
    RESULT_FILLED,
    RESULT_KILLSWITCH_OFF,
    RESULT_POOL_MISS,
    RESULT_SHADOW,
    RESULT_SIZE_CAP,
)
from src.execution.prefill.pool import FilledOrder
from src.mempool.tx_decoder import LeaderIntent


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


def _make_intent(
    *,
    size_usdc: Decimal | float = Decimal("100"),
    wallet: str = "0xleader",
    market_id: str = "market-1",
    intent_id: str | None = None,
) -> LeaderIntent:
    return LeaderIntent(
        intent_id=intent_id or str(uuid.uuid4()),
        wallet=wallet,
        market_id=market_id,
        token_id="token-yes",
        side="buy",
        size_usdc=Decimal(str(size_usdc)),
        price=Decimal("0.55"),
        order_type="GTC",
        intent_received_at=datetime.now(tz=timezone.utc) - timedelta(milliseconds=80),
        expected_block=42_000_000,
        tx_hash="0x" + "ab" * 32,
        nonce=7,
        replaces=None,
    )


def _make_killswitch(*, real_enabled: bool = True) -> MagicMock:
    ks = MagicMock()
    ks.is_real_execution_enabled = AsyncMock(return_value=real_enabled)
    return ks


def _make_confidence(action: str = "follow") -> MagicMock:
    engine = MagicMock()
    engine.recommend = AsyncMock(return_value={"action": action})
    return engine


def _make_risk_manager(
    *,
    in_cooldown: bool = False,
    raises: bool = False,
) -> MagicMock:
    rm = MagicMock()
    if raises:
        rm.in_cooldown = AsyncMock(side_effect=RuntimeError("boom"))
    else:
        rm.in_cooldown = AsyncMock(return_value=in_cooldown)
    return rm


def _make_pool(filled: FilledOrder | None = None) -> MagicMock:
    pool = MagicMock()
    pool.fire = AsyncMock(return_value=filled)
    pool.expire_stale = AsyncMock(return_value=0)
    return pool


def _make_paper_trader() -> MagicMock:
    paper = MagicMock()
    paper.capital = 10_000.0
    paper.open_trade = AsyncMock(return_value=1)
    return paper


def _make_live_trader() -> MagicMock:
    live = MagicMock()
    live.open_trade = AsyncMock(return_value=None)
    return live


def _make_runtime_config(
    *,
    prefill_live_enabled: bool | None = None,
    risk_per_trade_pct: float | None = None,
) -> MagicMock:
    rc = MagicMock()

    async def _get(key: str):
        if key == "prefill_live_enabled":
            return prefill_live_enabled
        if key == "risk_per_trade_pct":
            return risk_per_trade_pct
        return None

    rc.get = AsyncMock(side_effect=_get)
    return rc


def _make_router(
    *,
    pool=None,
    live_trader=None,
    paper_trader=None,
    confidence_engine=None,
    risk_manager=None,
    killswitch=None,
    runtime_config=None,
) -> IntentRouter:
    return IntentRouter(
        pool=pool or _make_pool(),
        live_trader=live_trader or _make_live_trader(),
        paper_trader=paper_trader or _make_paper_trader(),
        confidence_engine=confidence_engine or _make_confidence(),
        risk_manager=risk_manager or _make_risk_manager(),
        killswitch=killswitch or _make_killswitch(),
        runtime_config=runtime_config,
    )


# A captured-INSERT fake connection. Records each .execute() call's
# args so the test can assert on the row contents.
class _FakeConn:
    def __init__(self) -> None:
        self.execute_calls: list[tuple] = []

    async def execute(self, *args, **kwargs):
        self.execute_calls.append((args, kwargs))
        return "INSERT 0 1"


def _patch_get_db(conn: _FakeConn):
    @asynccontextmanager
    async def fake_get_db():
        yield conn

    return patch(
        "src.execution.prefill.intent_router.get_db", fake_get_db
    )


# Positional layout of the INSERT params (args[0] is the SQL string).
# Stays in sync with IntentRouter._insert_observation's ordered $-args.
_INSERT_FIELDS = (
    "_sql",
    "intent_id",
    "wallet_address",
    "market_id",
    "token_id",
    "side",
    "size_usdc",
    "intent_received_at",
    "tx_hash",
    "nonce",
    "replaces_tx_hash",
    "expected_block",
    "fired_at",
    "fire_result",
    "latency_ms_to_fire",
)


def _insert_field(args: tuple, name: str):
    return args[_INSERT_FIELDS.index(name)]


def _last_fire_result(conn: _FakeConn) -> str:
    return _insert_field(conn.execute_calls[-1][0], "fire_result")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_killswitch_off_skips_intent_no_fire():
    """Strict-path killswitch OFF → result='killswitch_off', no fire."""
    pool = _make_pool()
    paper = _make_paper_trader()
    router = _make_router(
        pool=pool,
        paper_trader=paper,
        killswitch=_make_killswitch(real_enabled=False),
    )

    conn = _FakeConn()
    with _patch_get_db(conn):
        await router._on_intent(_make_intent())

    pool.fire.assert_not_called()
    paper.open_trade.assert_not_called()
    # Observation row recorded with killswitch_off.
    assert len(conn.execute_calls) == 1
    assert _last_fire_result(conn) == RESULT_KILLSWITCH_OFF


@pytest.mark.asyncio
async def test_killswitch_strict_path_uses_bypass_cache():
    """The strict-path consult MUST call is_real_execution_enabled with
    bypass_cache=True (Phase 0 R2 B, audit F-05)."""
    ks = _make_killswitch(real_enabled=False)
    router = _make_router(killswitch=ks)

    conn = _FakeConn()
    with _patch_get_db(conn):
        await router._on_intent(_make_intent())

    assert ks.is_real_execution_enabled.await_count >= 1
    # Every call uses bypass_cache=True.
    for call in ks.is_real_execution_enabled.await_args_list:
        assert call.kwargs.get("bypass_cache") is True


@pytest.mark.asyncio
async def test_confidence_skip_does_not_fire():
    """Confidence engine returning SKIP → result='confidence_skip'."""
    pool = _make_pool()
    paper = _make_paper_trader()
    router = _make_router(
        pool=pool,
        paper_trader=paper,
        confidence_engine=_make_confidence(action="skip"),
    )

    conn = _FakeConn()
    with _patch_get_db(conn):
        await router._on_intent(_make_intent())

    pool.fire.assert_not_called()
    paper.open_trade.assert_not_called()
    assert _last_fire_result(conn) == RESULT_CONFIDENCE_SKIP


@pytest.mark.asyncio
async def test_confidence_engine_exception_treated_as_skip():
    """A bug in the confidence engine must not crash the consumer."""
    engine = MagicMock()
    engine.recommend = AsyncMock(side_effect=RuntimeError("posterior boom"))
    pool = _make_pool()
    router = _make_router(pool=pool, confidence_engine=engine)

    conn = _FakeConn()
    with _patch_get_db(conn):
        # Must NOT raise.
        await router._on_intent(_make_intent())

    pool.fire.assert_not_called()
    assert _last_fire_result(conn) == RESULT_CONFIDENCE_SKIP


@pytest.mark.asyncio
async def test_size_cap_blocks_oversized_intent():
    """size_usdc > current_capital * MAX_POSITION_PCT → result='size_cap'."""
    paper = _make_paper_trader()
    paper.capital = 10_000.0  # cap = 10_000 * 0.02 = 200 USDC

    router = _make_router(paper_trader=paper)

    intent = _make_intent(size_usdc=Decimal("500"))  # > $200

    conn = _FakeConn()
    with _patch_get_db(conn):
        await router._on_intent(intent)

    assert _last_fire_result(conn) == RESULT_SIZE_CAP


@pytest.mark.asyncio
async def test_size_cap_uses_runtime_config_risk_per_trade_pct():
    """Cap follows the runtime config knob, not the static default."""
    paper = _make_paper_trader()
    paper.capital = 10_000.0
    # Cockpit relaxed the cap to 10% — a $500 intent should pass now.
    rc = _make_runtime_config(risk_per_trade_pct=0.10)
    router = _make_router(paper_trader=paper, runtime_config=rc)

    intent = _make_intent(size_usdc=Decimal("500"))

    conn = _FakeConn()
    with _patch_get_db(conn):
        await router._on_intent(intent)

    # Should have advanced to shadow (default), NOT size_cap.
    assert conn.execute_calls[0][0][12] == RESULT_SHADOW


@pytest.mark.asyncio
async def test_cooldown_blocks_intent():
    """RiskManager reports cooldown active → result='cooldown'."""
    pool = _make_pool()
    paper = _make_paper_trader()
    router = _make_router(
        pool=pool,
        paper_trader=paper,
        risk_manager=_make_risk_manager(in_cooldown=True),
    )

    conn = _FakeConn()
    with _patch_get_db(conn):
        await router._on_intent(_make_intent())

    pool.fire.assert_not_called()
    paper.open_trade.assert_not_called()
    assert conn.execute_calls[0][0][12] == RESULT_COOLDOWN


@pytest.mark.asyncio
async def test_shadow_mode_routes_to_paper_trader():
    """All gates pass + live disabled (default) → paper_trader.open_trade."""
    pool = _make_pool()
    paper = _make_paper_trader()
    router = _make_router(pool=pool, paper_trader=paper)

    intent = _make_intent()

    conn = _FakeConn()
    with _patch_get_db(conn):
        await router._on_intent(intent)

    pool.fire.assert_not_called()
    paper.open_trade.assert_awaited_once()
    # The decision dict carries the intent_id for end-to-end trace.
    decision = paper.open_trade.await_args.args[0]
    assert decision["leader_wallet"] == intent.wallet
    assert decision["market_id"] == intent.market_id
    assert decision["trade_context"]["intent_id"] == intent.intent_id
    assert decision["trade_context"]["source"] == "mempool_prefill_shadow"
    assert decision["signal_audit"]["accepted"] is True
    assert conn.execute_calls[0][0][12] == RESULT_SHADOW


@pytest.mark.asyncio
async def test_live_mode_pool_miss():
    """Live mode + pool returns None → result='pool_miss', no paper."""
    pool = _make_pool(filled=None)
    paper = _make_paper_trader()
    rc = _make_runtime_config(prefill_live_enabled=True)
    router = _make_router(pool=pool, paper_trader=paper, runtime_config=rc)

    intent = _make_intent()

    conn = _FakeConn()
    with _patch_get_db(conn):
        await router._on_intent(intent)

    pool.fire.assert_awaited_once_with(intent)
    paper.open_trade.assert_not_called()
    assert conn.execute_calls[0][0][12] == RESULT_POOL_MISS


@pytest.mark.asyncio
async def test_live_mode_filled():
    """Live mode + pool returns FilledOrder → result='filled'."""
    filled = FilledOrder(
        clob_order_id="clob-1",
        filled_size_shares=100.0,
        avg_fill_price=0.55,
        fee_paid_usdc=0.5,
    )
    pool = _make_pool(filled=filled)
    paper = _make_paper_trader()
    rc = _make_runtime_config(prefill_live_enabled=True)
    router = _make_router(pool=pool, paper_trader=paper, runtime_config=rc)

    intent = _make_intent()

    conn = _FakeConn()
    with _patch_get_db(conn):
        await router._on_intent(intent)

    pool.fire.assert_awaited_once_with(intent)
    assert conn.execute_calls[0][0][12] == RESULT_FILLED


@pytest.mark.asyncio
async def test_live_mode_toctou_killswitch_recheck_blocks_fire():
    """Killswitch flips OFF between confidence check and pool.fire — the
    TOCTOU re-check must catch it and emit 'killswitch_off' WITHOUT
    firing.

    Defense in depth: the first consult passes, the second fails.
    """
    ks = MagicMock()
    # Two-call sequence: first True (entry gate), then False (TOCTOU).
    ks.is_real_execution_enabled = AsyncMock(side_effect=[True, False])

    pool = _make_pool(
        filled=FilledOrder(
            clob_order_id="never",
            filled_size_shares=0.0,
            avg_fill_price=0.0,
            fee_paid_usdc=0.0,
        )
    )
    rc = _make_runtime_config(prefill_live_enabled=True)
    router = _make_router(pool=pool, runtime_config=rc, killswitch=ks)

    conn = _FakeConn()
    with _patch_get_db(conn):
        await router._on_intent(_make_intent())

    pool.fire.assert_not_called()
    assert conn.execute_calls[0][0][12] == RESULT_KILLSWITCH_OFF
    # Both checks must have used bypass_cache=True.
    assert ks.is_real_execution_enabled.await_count == 2
    for call in ks.is_real_execution_enabled.await_args_list:
        assert call.kwargs.get("bypass_cache") is True


@pytest.mark.asyncio
async def test_mempool_observation_row_fields():
    """The INSERT into mempool_observations carries the canonical
    intent + result + latency fields."""
    paper = _make_paper_trader()
    router = _make_router(paper_trader=paper)

    intent = _make_intent(size_usdc=Decimal("100"))
    intent_id_str = intent.intent_id

    conn = _FakeConn()
    with _patch_get_db(conn):
        await router._on_intent(intent)

    assert len(conn.execute_calls) == 1
    args = conn.execute_calls[0][0]
    # Positional args: (sql, intent_id, wallet, market_id, token_id,
    # side, size_usdc, intent_received_at, tx_hash, nonce, replaces,
    # expected_block, fired_at, fire_result, latency_ms_to_fire)
    assert isinstance(args[1], uuid.UUID)
    assert str(args[1]) == intent_id_str
    assert args[2] == intent.wallet
    assert args[3] == intent.market_id
    assert args[4] == intent.token_id
    assert args[5] == intent.side
    assert args[6] == intent.size_usdc
    assert args[7] == intent.intent_received_at
    assert args[8] == intent.tx_hash
    assert args[9] == int(intent.nonce)
    assert args[10] is None  # replaces_tx_hash
    assert args[11] == int(intent.expected_block)
    assert args[12] == RESULT_SHADOW
    assert isinstance(args[13], int) and args[13] >= 0


@pytest.mark.asyncio
async def test_latency_metric_observed_on_every_branch():
    """The latency histogram fires for every decided intent."""
    fake_hist = MagicMock()
    fake_hist.observe = MagicMock()
    fake_counter = MagicMock()
    fake_counter.labels = MagicMock(return_value=MagicMock(inc=MagicMock()))

    router = _make_router()

    intent = _make_intent()
    conn = _FakeConn()

    with patch.object(
        ir_module,
        "_utcnow",
        return_value=intent.intent_received_at + timedelta(milliseconds=120),
    ):
        with patch(
            "src.monitoring.metrics.intent_router_latency_seconds", fake_hist
        ), patch(
            "src.monitoring.metrics.intent_router_decisions_total",
            fake_counter,
        ), _patch_get_db(conn):
            await router._on_intent(intent)

    fake_hist.observe.assert_called()
    # The observed value is the wall-clock elapsed from
    # intent_received_at; ~0.12 s with our patched clock.
    observed_values = [c.args[0] for c in fake_hist.observe.call_args_list]
    assert any(0.05 <= v <= 1.0 for v in observed_values), observed_values


@pytest.mark.asyncio
async def test_decisions_counter_labels_each_branch():
    """Every result label increments polybot_intent_router_decisions_total
    with the appropriate label."""
    fake_counter = MagicMock()
    label_calls: list[str] = []

    def _labels(result: str):
        label_calls.append(result)
        m = MagicMock()
        m.inc = MagicMock()
        return m

    fake_counter.labels = MagicMock(side_effect=_labels)

    conn = _FakeConn()

    with patch(
        "src.monitoring.metrics.intent_router_decisions_total", fake_counter
    ), _patch_get_db(conn):
        # 1. killswitch_off
        router_off = _make_router(killswitch=_make_killswitch(real_enabled=False))
        await router_off._on_intent(_make_intent())
        # 2. confidence_skip
        router_skip = _make_router(
            confidence_engine=_make_confidence(action="skip")
        )
        await router_skip._on_intent(_make_intent())
        # 3. shadow (default path)
        router_shadow = _make_router()
        await router_shadow._on_intent(_make_intent())
        # 4. cooldown
        router_cd = _make_router(
            risk_manager=_make_risk_manager(in_cooldown=True)
        )
        await router_cd._on_intent(_make_intent())

    assert RESULT_KILLSWITCH_OFF in label_calls
    assert RESULT_CONFIDENCE_SKIP in label_calls
    assert RESULT_SHADOW in label_calls
    assert RESULT_COOLDOWN in label_calls


@pytest.mark.asyncio
async def test_on_intent_swallows_unexpected_exceptions():
    """A bug in any collaborator must not propagate out of _on_intent
    (the consumer loop would otherwise blackhole the rest of the batch)."""
    paper = _make_paper_trader()
    paper.open_trade = AsyncMock(side_effect=RuntimeError("paper bug"))
    router = _make_router(paper_trader=paper)

    conn = _FakeConn()
    with _patch_get_db(conn):
        # Must not raise.
        await router._on_intent(_make_intent())

    # Paper raised — but the router still records the shadow row.
    assert conn.execute_calls
    assert conn.execute_calls[0][0][12] == RESULT_SHADOW


@pytest.mark.asyncio
async def test_runtime_config_flag_flips_live_mode():
    """Setting runtime_config['prefill_live_enabled']=True flips the
    router from shadow to live without touching env vars."""
    pool = _make_pool(
        filled=FilledOrder(
            clob_order_id="o1",
            filled_size_shares=1.0,
            avg_fill_price=0.55,
            fee_paid_usdc=0.0,
        )
    )
    paper = _make_paper_trader()
    rc_off = _make_runtime_config(prefill_live_enabled=False)
    rc_on = _make_runtime_config(prefill_live_enabled=True)

    conn = _FakeConn()
    with _patch_get_db(conn):
        # Flag OFF → shadow.
        router_off = _make_router(
            pool=pool, paper_trader=paper, runtime_config=rc_off
        )
        await router_off._on_intent(_make_intent())
        assert conn.execute_calls[-1][0][12] == RESULT_SHADOW

        # Flag ON → live (pool fires).
        router_on = _make_router(
            pool=pool, paper_trader=paper, runtime_config=rc_on
        )
        await router_on._on_intent(_make_intent())
        assert conn.execute_calls[-1][0][12] == RESULT_FILLED
        pool.fire.assert_awaited()


@pytest.mark.asyncio
async def test_runtime_config_get_failure_falls_back_to_settings():
    """If runtime_config.get raises, the router falls back to
    settings.PREFILL_LIVE_ENABLED (safe default = False = shadow)."""
    rc = MagicMock()
    rc.get = AsyncMock(side_effect=RuntimeError("redis down"))
    pool = _make_pool()
    paper = _make_paper_trader()
    router = _make_router(pool=pool, paper_trader=paper, runtime_config=rc)

    conn = _FakeConn()
    with _patch_get_db(conn):
        await router._on_intent(_make_intent())

    # Safe default = shadow, no pool fire.
    pool.fire.assert_not_called()
    paper.open_trade.assert_awaited_once()
    assert conn.execute_calls[0][0][12] == RESULT_SHADOW


@pytest.mark.asyncio
async def test_stream_entry_rehydrates_payload():
    """The stream-handler entrypoint rebuilds LeaderIntent from a JSON
    dict (what the StreamConsumer hands us) and runs the decision tree."""
    paper = _make_paper_trader()
    router = _make_router(paper_trader=paper)

    intent = _make_intent()
    payload = {
        "intent_id": intent.intent_id,
        "wallet": intent.wallet,
        "market_id": intent.market_id,
        "token_id": intent.token_id,
        "side": intent.side,
        # Stream payloads are JSON — numbers come back as strings or floats.
        "size_usdc": str(intent.size_usdc),
        "price": str(intent.price),
        "order_type": intent.order_type,
        "intent_received_at": intent.intent_received_at.isoformat(),
        "expected_block": intent.expected_block,
        "tx_hash": intent.tx_hash,
        "nonce": intent.nonce,
        "replaces": None,
    }

    conn = _FakeConn()
    with _patch_get_db(conn):
        await router._on_stream_entry(payload, "mempool:leader_intent", "0-1")

    paper.open_trade.assert_awaited_once()
    assert conn.execute_calls[0][0][12] == RESULT_SHADOW


@pytest.mark.asyncio
async def test_malformed_stream_payload_does_not_crash():
    """A malformed payload is logged + the error counter ticks, but the
    consumer keeps consuming."""
    router = _make_router()

    fake_counter = MagicMock()
    fake_counter.labels = MagicMock(return_value=MagicMock(inc=MagicMock()))

    with patch(
        "src.monitoring.metrics.intent_router_decisions_total", fake_counter
    ):
        # Missing required fields.
        await router._on_stream_entry({"intent_id": "not-a-uuid"}, "s", "0-1")

    # Counter incremented with RESULT_ERROR.
    labels_seen = [c.args[0] for c in fake_counter.labels.call_args_list]
    assert RESULT_ERROR in labels_seen


@pytest.mark.asyncio
async def test_in_cooldown_missing_on_risk_manager_is_tolerated():
    """The router gracefully skips the cooldown gate if RiskManager
    doesn't expose `in_cooldown` yet (the method is still on the roadmap).
    """
    rm = MagicMock()
    # Explicitly REMOVE the attribute so getattr() returns None.
    if hasattr(rm, "in_cooldown"):
        del rm.in_cooldown
    # MagicMock auto-creates attributes; force the lookup to None.
    rm.configure_mock(**{"in_cooldown": None})

    paper = _make_paper_trader()
    router = _make_router(paper_trader=paper, risk_manager=rm)

    conn = _FakeConn()
    with _patch_get_db(conn):
        await router._on_intent(_make_intent())

    # Decision proceeds past the cooldown gate -> shadow.
    paper.open_trade.assert_awaited_once()
    assert conn.execute_calls[0][0][12] == RESULT_SHADOW
