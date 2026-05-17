"""
Tests for the Telegram command handlers (S3.9).

Each handler takes a CommandContext and returns a string. We assemble
a minimal context with stub objects (paper_trader, killswitch) and a
fakeredis instance, then assert the reply contains the expected facts.

DB-backed commands (positions, pnl) are tested via monkeypatching
get_db / load_state to avoid touching Postgres.
"""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import AsyncMock

import fakeredis.aioredis
import pytest

from src.config import settings
from src.telegram_bot import commands


# --------------------------------------------------------------------------- #
# Fixtures                                                                     #
# --------------------------------------------------------------------------- #


@pytest.fixture
async def redis_client():
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield client
    await client.aclose()


@dataclass
class _StubKillswitchState:
    execution_enabled: bool = True
    real_execution_enabled: bool = False


class _StubKillswitch:
    def __init__(self) -> None:
        self.state = _StubKillswitchState()
        self.calls: list[tuple] = []

    async def get_state(self) -> _StubKillswitchState:
        return self.state

    async def set_execution_enabled(self, enabled: bool, *, reason: str, actor: str):
        self.calls.append((enabled, reason, actor))
        self.state = _StubKillswitchState(
            execution_enabled=enabled, real_execution_enabled=self.state.real_execution_enabled
        )
        return self.state


class _StubTrade:
    """Minimal duck-typed trade. PaperTrader.open_trades is a list of these."""
    def __init__(self, size: float = 50.0) -> None:
        self.size_usdc = size


class _StubPaperTrader:
    def __init__(self, unrealized: float = -5.5) -> None:
        self.capital = 10500.0
        self.open_trades = [_StubTrade(50.0), _StubTrade(25.0)]
        self._unrealized = unrealized

    async def compute_unrealized_pnl(self) -> float:
        return self._unrealized


@pytest.fixture
def ctx(redis_client):
    return commands.CommandContext(
        redis_client=redis_client,
        killswitch=_StubKillswitch(),
        paper_trader=_StubPaperTrader(),
        live_trader=None,
    )


# --------------------------------------------------------------------------- #
# /status                                                                      #
# --------------------------------------------------------------------------- #


async def test_status_default_paper_mode(ctx):
    out = await commands.cmd_status(ctx)
    assert "STATUS" in out
    assert "mode:" in out
    assert "10500.00$" in out
    assert "open=2" in out
    assert "exec=ON" in out


async def test_status_reflects_runtime_override(ctx):
    await ctx.redis_client.set(settings.TRADING_MODE_OVERRIDE_KEY, "dual")
    out = await commands.cmd_status(ctx)
    assert "mode: dual" in out


# --------------------------------------------------------------------------- #
# /pnl                                                                         #
# --------------------------------------------------------------------------- #


async def test_pnl_uses_portfolio_state(monkeypatch, ctx):
    fake_state = type("S", (), {"realized_pnl_cum": 120.0})()

    async def fake_load():
        return fake_state

    monkeypatch.setattr("src.engine.portfolio_state.load_state", fake_load)

    # Stub get_db so the live count + pnl queries fail soft (returning 0/None)
    class _BadDB:
        async def __aenter__(self):
            raise RuntimeError("no db in unit tests")

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr("src.database.connection.get_db", lambda: _BadDB())

    out = await commands.cmd_pnl(ctx)
    assert "PnL" in out
    assert "+120.00$" in out


async def test_pnl_unrealized_uses_mark_to_market(monkeypatch, ctx):
    """The audit's structural bug: cost-basis unrealized always returns ~0.
    Verify the new path calls compute_unrealized_pnl on the paper trader
    and surfaces its signed value instead of the broken formula."""
    fake_state = type("S", (), {"realized_pnl_cum": 0.0})()

    async def fake_load():
        return fake_state

    monkeypatch.setattr("src.engine.portfolio_state.load_state", fake_load)

    class _BadDB:
        async def __aenter__(self):
            raise RuntimeError("no db in unit tests")

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr("src.database.connection.get_db", lambda: _BadDB())

    # Stub paper trader returns +37.42 unrealized — must show up verbatim.
    ctx.paper_trader = _StubPaperTrader(unrealized=37.42)
    out = await commands.cmd_pnl(ctx)
    assert "paper unrealized: +37.42$" in out

    # Negative mark-to-market is the other half of the fix — the broken
    # formula could never return a meaningful loss when capital had been
    # debited by the size of open positions.
    ctx.paper_trader = _StubPaperTrader(unrealized=-128.10)
    out = await commands.cmd_pnl(ctx)
    assert "paper unrealized: -128.10$" in out


# --------------------------------------------------------------------------- #
# /positions                                                                   #
# --------------------------------------------------------------------------- #


async def test_positions_handles_db_failure_gracefully(monkeypatch, ctx):
    """If the DB is down, /positions returns "(none)" rather than crashing."""
    class _BadDB:
        async def __aenter__(self):
            raise RuntimeError("db down")

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr("src.database.connection.get_db", lambda: _BadDB())
    out = await commands.cmd_positions(ctx)
    assert "(none)" in out


# --------------------------------------------------------------------------- #
# /summary                                                                     #
# --------------------------------------------------------------------------- #


async def test_summary_handles_db_failure_gracefully(monkeypatch, ctx):
    """If the DB is down, /summary still renders the header + open count
    from the paper trader, rather than crashing."""
    fake_state = type("S", (), {"realized_pnl_cum": 0.0})()

    async def fake_load():
        return fake_state

    monkeypatch.setattr("src.engine.portfolio_state.load_state", fake_load)

    class _BadDB:
        async def __aenter__(self):
            raise RuntimeError("db down")

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr("src.database.connection.get_db", lambda: _BadDB())
    out = await commands.cmd_summary(ctx)
    assert "TODAY'S SUMMARY" in out
    # Open count + unrealized come from the paper trader, not the DB.
    assert "0 closed, 2 open" in out


# --------------------------------------------------------------------------- #
# /mode                                                                        #
# --------------------------------------------------------------------------- #


async def test_mode_no_args_shows_usage(ctx):
    out = await commands.cmd_mode(ctx, [])
    assert "Usage" in out


async def test_mode_invalid_arg(ctx):
    out = await commands.cmd_mode(ctx, ["nonsense"])
    assert "Invalid mode" in out


async def test_mode_sets_redis_override(ctx):
    out = await commands.cmd_mode(ctx, ["dual"])
    assert "MODE CHANGED" in out
    assert "dual" in out
    stored = await ctx.redis_client.get(settings.TRADING_MODE_OVERRIDE_KEY)
    assert stored == "dual"


async def test_mode_accepts_paper_live_dual(ctx):
    for v in ("paper", "live", "dual"):
        out = await commands.cmd_mode(ctx, [v])
        assert v in out
        stored = await ctx.redis_client.get(settings.TRADING_MODE_OVERRIDE_KEY)
        assert stored == v


# --------------------------------------------------------------------------- #
# /killswitch                                                                  #
# --------------------------------------------------------------------------- #


async def test_killswitch_no_args(ctx):
    out = await commands.cmd_killswitch(ctx, [])
    assert "Usage" in out


async def test_killswitch_off(ctx):
    out = await commands.cmd_killswitch(ctx, ["off"])
    assert "OFF" in out
    assert ctx.killswitch.calls == [(False, "telegram_command:off", "telegram_operator")]


async def test_killswitch_on(ctx):
    out = await commands.cmd_killswitch(ctx, ["on"])
    assert "ON" in out
    assert ctx.killswitch.calls == [(True, "telegram_command:on", "telegram_operator")]


async def test_killswitch_invalid_arg(ctx):
    out = await commands.cmd_killswitch(ctx, ["maybe"])
    assert "Invalid" in out
    assert ctx.killswitch.calls == []


# --------------------------------------------------------------------------- #
# /pause + /resume                                                             #
# --------------------------------------------------------------------------- #


async def test_pause_disables_execution(ctx):
    out = await commands.cmd_pause(ctx)
    assert "paused" in out.lower()
    assert ctx.killswitch.calls == [(False, "telegram_command:pause", "telegram_operator")]


async def test_resume_enables_execution(ctx):
    out = await commands.cmd_resume(ctx)
    assert "resumed" in out.lower()
    assert ctx.killswitch.calls == [(True, "telegram_command:resume", "telegram_operator")]


# --------------------------------------------------------------------------- #
# /help                                                                        #
# --------------------------------------------------------------------------- #


async def test_help(ctx):
    out = await commands.cmd_help(ctx)
    assert "/status" in out
    assert "/killswitch" in out
