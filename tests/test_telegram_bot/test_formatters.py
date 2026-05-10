"""
Tests for the Telegram message formatters (S3.9).

These are pure functions — no mocks, no async, no Redis. Each test
asserts the resulting string contains the key fields a human operator
would scan for at 2am during an incident.
"""

from __future__ import annotations

from src.telegram_bot import formatters


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #


def test_short_truncates_long_market_id():
    long_id = "0x" + "a" * 60
    out = formatters._short(long_id)
    assert out.endswith("…")
    # 14 chars + ellipsis
    assert len(out) == 15


def test_short_keeps_short_market_id():
    assert formatters._short("0xabc") == "0xabc"


def test_short_handles_none():
    assert formatters._short(None) == "?"


def test_money_handles_none():
    assert formatters._money(None) == "?"


def test_money_signs_positive():
    assert formatters._money(12.5) == "+12.50$"


def test_money_signs_negative():
    assert formatters._money(-12.5) == "-12.50$"


def test_money_zero():
    assert formatters._money(0) == "0.00$"


# --------------------------------------------------------------------------- #
# Notifier formatters                                                          #
# --------------------------------------------------------------------------- #


def test_format_position_opened_paper():
    out = formatters.format_position_opened(
        venue="paper",
        payload={
            "trade_id": 42,
            "market_id": "0xdeadbeef" * 4,
            "strategy": "follow",
            "direction": "yes",
            "size_usdc": 50.0,
            "entry_price": 0.4321,
            "leader_wallet": "0xleader1234",
            "confidence": 0.78,
        },
    )
    assert "PAPER OPEN" in out
    assert "FOLLOW" in out
    assert "#42" in out
    assert "YES" in out
    assert "50.00$" in out
    assert "0.4321" in out
    assert "0.78" in out
    # market_id truncated
    assert "0xdeadbeef" in out


def test_format_position_opened_live_uses_live_icon():
    out = formatters.format_position_opened(
        venue="live",
        payload={
            "trade_id": 1,
            "market_id": "0x1",
            "strategy": "fade",
            "direction": "no",
            "size_usdc": 10.0,
            "entry_price": 0.5,
        },
    )
    assert "LIVE OPEN" in out
    assert "FADE" in out


def test_format_position_opened_handles_missing_fields():
    """A buggy producer must not crash the notifier."""
    out = formatters.format_position_opened(venue="paper", payload={})
    assert "PAPER OPEN" in out
    assert "?" in out  # placeholders


def test_format_position_closed_profit():
    out = formatters.format_position_closed(
        venue="paper",
        payload={
            "trade_id": 7,
            "market_id": "0xabc",
            "exit_price": 0.6,
            "pnl_usdc": 12.5,
            "close_reason": "tp",
        },
    )
    assert "PAPER CLOSE" in out
    assert "#7" in out
    assert "+12.50$" in out
    assert "tp" in out
    assert "📈" in out


def test_format_position_closed_loss():
    out = formatters.format_position_closed(
        venue="live",
        payload={"pnl_usdc": -5.5, "close_reason": "stop"},
    )
    assert "-5.50$" in out
    assert "📉" in out


def test_format_killswitch_changed_on():
    out = formatters.format_killswitch_changed(
        {
            "execution_enabled": True,
            "real_execution_enabled": False,
            "updated_by": "telegram_operator",
            "paused_reason": None,
        }
    )
    assert "KILLSWITCH FLIP" in out
    assert "execution: ON" in out
    assert "real:      OFF" in out
    assert "telegram_operator" in out


def test_format_killswitch_changed_off_includes_reason():
    out = formatters.format_killswitch_changed(
        {
            "execution_enabled": False,
            "real_execution_enabled": False,
            "updated_by": "manual",
            "paused_reason": "incident_storm",
        }
    )
    assert "🛑" in out
    assert "incident_storm" in out


def test_format_engine_crash():
    out = formatters.format_engine_crash(
        {
            "component": "engine",
            "error_type": "RuntimeError",
            "error": "boom",
        }
    )
    assert "CRITICAL" in out
    assert "engine" in out
    assert "RuntimeError" in out
    assert "boom" in out


# --------------------------------------------------------------------------- #
# Command formatters                                                           #
# --------------------------------------------------------------------------- #


def test_format_status():
    out = formatters.format_status(
        mode="dual",
        paper_capital=10500.0,
        paper_open=3,
        live_open=1,
        killswitch_exec=True,
        killswitch_real=False,
    )
    assert "STATUS" in out
    assert "mode: dual" in out
    assert "10500.00$" in out
    assert "open=3" in out
    assert "open=1" in out
    assert "exec=ON" in out
    assert "real=OFF" in out


def test_format_pnl():
    out = formatters.format_pnl(
        paper_realized=120.0,
        paper_unrealized=-5.5,
        live_realized=None,
        live_shadow_count=4,
        live_real_count=2,
    )
    assert "+120.00$" in out
    assert "-5.50$" in out
    assert "shadow=4" in out
    assert "real=2" in out
    # None becomes ?
    assert "live realized:    ?" in out


def test_format_positions_empty():
    out = formatters.format_positions(paper_positions=[], live_positions=[])
    assert "(none)" in out


def test_format_positions_with_paper_and_live():
    paper = [
        {
            "market_id": "0xpaper",
            "strategy": "follow",
            "direction": "yes",
            "size_usdc": 50.0,
            "entry_price": 0.4,
        }
    ]
    live = [
        {
            "market_id": "0xlive",
            "strategy": "fade",
            "direction": "no",
            "size_usdc": 25.0,
            "entry_price": 0.6,
            "status": "open",
        }
    ]
    out = formatters.format_positions(paper_positions=paper, live_positions=live)
    assert "PAPER (1)" in out
    assert "LIVE (1)" in out
    assert "0xpaper" in out
    assert "0xlive" in out
    assert "[open]" in out


def test_format_positions_truncates_at_10():
    paper = [
        {"market_id": f"0xpaper{i}", "strategy": "follow", "direction": "yes"}
        for i in range(15)
    ]
    out = formatters.format_positions(paper_positions=paper, live_positions=[])
    assert "PAPER (15)" in out
    assert "+5 more" in out


def test_format_mode_change():
    out = formatters.format_mode_change(old_mode="paper", new_mode="dual")
    assert "MODE CHANGED" in out
    assert "paper → dual" in out


def test_format_help_lists_all_commands():
    out = formatters.format_help()
    for cmd in (
        "/status",
        "/pnl",
        "/positions",
        "/mode",
        "/killswitch",
        "/pause",
        "/resume",
        "/help",
    ):
        assert cmd in out
