"""
Unit tests for the S3.11 Telegram-bot expansion.

Covers:
  * notifier: hash-based dedup window, tier filtering, backoff
  * alerts: rule lifecycle (add/list/remove), evaluation cooldown
  * new formatters: render-without-crash + key field presence
  * digest: build_hourly returns None on empty window
"""

from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from fakeredis.aioredis import FakeRedis

from src.telegram_bot import auth, formatters, notifier
from src.telegram_bot.alerts import ALERT_COOLDOWN_S, AlertRule, AlertsManager


@pytest_asyncio.fixture
async def redis_client():
    r = FakeRedis()
    try:
        yield r
    finally:
        await r.aclose()


@pytest.fixture(autouse=True)
def _allow_chat(monkeypatch):
    """Notifier broadcast requires at least one chat_id in the allowlist."""
    monkeypatch.setattr(auth.settings, "TELEGRAM_CHAT_IDS", "111")
    auth.reload_allowlist()
    yield
    auth.reload_allowlist()


async def _wait_for(predicate, timeout: float = 1.0) -> bool:
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return False


# --------------------------------------------------------------------------- #
# Notifier: dedup, tiers, backoff                                              #
# --------------------------------------------------------------------------- #


async def test_dedup_drops_identical_message_within_window(redis_client):
    """Same channel + same payload twice in <60s → only one send."""
    send = AsyncMock()
    n = notifier.TelegramNotifier(
        redis_client=redis_client,
        send_fn=send,
        ingest_alert_cooldown_s=0,  # disable per-source cooldown
        dedup_window_s=60,
    )
    await n.start()
    try:
        await asyncio.sleep(0.05)
        payload = json.dumps(
            {"source": "falcon_x", "duration_s": 700.0, "severity": "warning"}
        )
        await redis_client.publish(notifier.CHANNEL_INGEST_GAP, payload)
        await asyncio.sleep(0.1)
        await redis_client.publish(notifier.CHANNEL_INGEST_GAP, payload)
        # Only one send because the formatted message hash matches.
        ok = await _wait_for(lambda: send.await_count >= 1, timeout=1.0)
        assert ok
        # Wait a bit more to confirm the second is dropped.
        await asyncio.sleep(0.3)
        assert send.await_count == 1
    finally:
        await n.stop()


async def test_dedup_zero_window_disables_dedup(redis_client):
    """Two identical payloads with dedup_window_s=0 both go out."""
    send = AsyncMock()
    n = notifier.TelegramNotifier(
        redis_client=redis_client,
        send_fn=send,
        ingest_alert_cooldown_s=0,
        dedup_window_s=0,
    )
    await n.start()
    try:
        await asyncio.sleep(0.05)
        payload = json.dumps(
            {"source": "falcon_y", "duration_s": 800.0, "severity": "warning"}
        )
        await redis_client.publish(notifier.CHANNEL_INGEST_GAP, payload)
        await redis_client.publish(notifier.CHANNEL_INGEST_GAP, payload)
        ok = await _wait_for(lambda: send.await_count >= 2, timeout=1.5)
        assert ok, f"expected 2 sends, got {send.await_count}"
    finally:
        await n.stop()


async def test_verbosity_quiet_filters_info_tier(redis_client):
    """In 'quiet', only CRITICAL channels send."""
    send = AsyncMock()
    n = notifier.TelegramNotifier(
        redis_client=redis_client,
        send_fn=send,
        verbosity="quiet",
        dedup_window_s=0,
    )
    await n.start()
    try:
        await asyncio.sleep(0.05)
        # paper_opened is TIER_INFO — should be filtered out in quiet mode.
        await redis_client.publish(
            notifier.CHANNEL_PAPER_OPENED,
            json.dumps(
                {
                    "trade_id": 1,
                    "market_id": "m1",
                    "direction": "yes",
                    "strategy": "follow",
                    "entry_price": 0.5,
                    "size_usdc": 100,
                }
            ),
        )
        # engine_crash is TIER_CRITICAL — should always send.
        await redis_client.publish(
            notifier.CHANNEL_ENGINE_CRASH,
            json.dumps({"component": "engine", "error": "boom"}),
        )
        await _wait_for(lambda: send.await_count >= 1, timeout=1.0)
        await asyncio.sleep(0.2)
        # Exactly 1 send (the crash); the paper_opened was filtered.
        assert send.await_count == 1
        text = send.await_args_list[0][0][1]
        assert "CRASH" in text
    finally:
        await n.stop()


async def test_set_verbosity_swaps_tier_filter(redis_client):
    """notifier.set_verbosity rejects unknown values, accepts valid ones."""
    n = notifier.TelegramNotifier(
        redis_client=redis_client,
        send_fn=AsyncMock(),
        verbosity="verbose",
    )
    assert n.current_verbosity() == "verbose"
    assert n.set_verbosity("quiet") == "quiet"
    assert n._max_tier == notifier.TIER_CRITICAL
    with pytest.raises(ValueError):
        n.set_verbosity("unknown")


async def test_follower_confirmed_is_silent_but_counted(redis_client):
    """S3.12: follower_confirmed bumps the 24h counter but sends no Telegram."""
    send = AsyncMock()
    n = notifier.TelegramNotifier(
        redis_client=redis_client,
        send_fn=send,
        verbosity="debug",  # would normally let everything through
        dedup_window_s=0,
    )
    await n.start()
    try:
        await asyncio.sleep(0.05)
        payload = json.dumps(
            {
                "leader_wallet": "0xleader",
                "follower_wallet": "0xfoll",
                "follow_probability": 0.82,
                "same_direction_rate": 0.85,
                "co_occurrences": 7,
            }
        )
        await redis_client.publish(notifier.CHANNEL_FOLLOWER_CONFIRMED, payload)
        await redis_client.publish(notifier.CHANNEL_FOLLOWER_CONFIRMED, payload)
        await redis_client.publish(notifier.CHANNEL_FOLLOWER_CONFIRMED, payload)
        await asyncio.sleep(0.3)
        # Crucially: zero sends.
        assert send.await_count == 0, "follower_confirmed must not produce an instant alert"
        # But the counter incremented to 3 in both windows.
        raw_24h = await redis_client.get("telegram:counter:follower_confirmed:24h")
        raw_1h = await redis_client.get("telegram:counter:follower_confirmed:1h")
        if isinstance(raw_24h, bytes):
            raw_24h = raw_24h.decode()
        if isinstance(raw_1h, bytes):
            raw_1h = raw_1h.decode()
        assert int(raw_24h) == 3
        assert int(raw_1h) == 3
    finally:
        await n.stop()


async def test_counted_non_silent_channel_still_sends(redis_client):
    """Channels in COUNTED_CHANNELS but NOT in SILENT_COUNT both send and count."""
    send = AsyncMock()
    n = notifier.TelegramNotifier(
        redis_client=redis_client,
        send_fn=send,
        verbosity="verbose",
        dedup_window_s=0,
    )
    await n.start()
    try:
        await asyncio.sleep(0.05)
        # drift_detected is counted AND sent (verbosity verbose covers ALERT tier).
        payload = json.dumps(
            {"wallet": "0xabc", "phase_before": 3, "phase_after": 2, "cusum_value": 3.5}
        )
        await redis_client.publish(notifier.CHANNEL_DRIFT_DETECTED, payload)
        ok = await _wait_for(lambda: send.await_count >= 1, timeout=1.0)
        assert ok
        raw = await redis_client.get("telegram:counter:drift_events:24h")
        if isinstance(raw, bytes):
            raw = raw.decode()
        assert int(raw) == 1
    finally:
        await n.stop()


async def test_critical_bypasses_throttle_and_dedup(redis_client):
    """An over-throttle CRITICAL still sends (engine_crash twice in a row)."""
    send = AsyncMock()
    n = notifier.TelegramNotifier(
        redis_client=redis_client,
        send_fn=send,
        max_per_minute=1,  # very tight
        dedup_window_s=60,  # would normally dedup
    )
    await n.start()
    try:
        await asyncio.sleep(0.05)
        crash = json.dumps({"component": "engine", "error": "x"})
        await redis_client.publish(notifier.CHANNEL_ENGINE_CRASH, crash)
        await redis_client.publish(notifier.CHANNEL_ENGINE_CRASH, crash)
        ok = await _wait_for(lambda: send.await_count >= 2, timeout=1.5)
        assert ok, f"CRITICAL must bypass throttle+dedup; got {send.await_count}"
    finally:
        await n.stop()


# --------------------------------------------------------------------------- #
# AlertsManager: lifecycle + cooldown                                          #
# --------------------------------------------------------------------------- #


async def test_alerts_add_list_remove(redis_client):
    mgr = AlertsManager(redis_client=redis_client)
    rules = await mgr.list_rules()
    assert rules == []

    rule = await mgr.add_rule(rule_type="drawdown", threshold=0.05)
    rules = await mgr.list_rules()
    assert len(rules) == 1
    assert rules[0]["rule_type"] == "drawdown"
    assert rules[0]["threshold"] == 0.05

    ok = await mgr.remove_rule(rule.id)
    assert ok is True
    rules = await mgr.list_rules()
    assert rules == []


async def test_alerts_unknown_type_rejected(redis_client):
    mgr = AlertsManager(redis_client=redis_client)
    with pytest.raises(ValueError):
        await mgr.add_rule(rule_type="nonsense", threshold=0.1)


async def test_alerts_cooldown_blocks_repeat_fire(redis_client):
    """Once a rule fires, it shouldn't refire within ALERT_COOLDOWN_S."""
    mgr = AlertsManager(redis_client=redis_client)
    rule = await mgr.add_rule(rule_type="drawdown", threshold=0.01)
    # Simulate "already fired 10s ago".
    rule.last_fired_at = time.time() - 10
    state = {"drawdown_pct": 0.5}
    msg = AlertsManager._evaluate_one(rule, state)
    assert msg is not None  # would fire if checked
    # But the manager's evaluate skips it because of cooldown.
    fake_state = state
    mgr._snapshot_state = AsyncMock(return_value=fake_state)
    fired = await mgr.evaluate()
    assert fired == [], "rule within cooldown must not fire"

    # Outside cooldown — fires.
    rule.last_fired_at = time.time() - (ALERT_COOLDOWN_S + 10)
    fired = await mgr.evaluate()
    assert len(fired) == 1


# --------------------------------------------------------------------------- #
# New formatters: render-without-crash                                         #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "fn,payload",
    [
        (formatters.format_suspicious_close, {"trade_id": 1, "pnl_pct": -2.5}),
        (formatters.format_risk_breaker, {"breaker": "drawdown", "value": 0.21, "threshold": 0.20}),
        (formatters.format_drawdown_threshold, {"drawdown_pct": 0.05, "threshold": 0.05, "peak_capital": 10000, "current_capital": 9500}),
        (formatters.format_drift_detected, {"wallet": "0xabcd1234", "phase_before": 3, "phase_after": 2, "cusum_value": 3.4}),
        (formatters.format_phase_upgraded, {"wallet": "0xabcd1234", "old_phase": 1, "new_phase": 2, "positions_resolved": 105}),
        (formatters.format_watchdog_restart, {"component": "profiler", "reason": "heartbeat freeze", "restart_count": 1, "max_restarts": 3}),
        (formatters.format_follower_confirmed, {"leader_wallet": "0xleader", "follower_wallet": "0xfoll", "follow_probability": 0.78, "same_direction_rate": 0.82, "co_occurrences": 7}),
        (formatters.format_leader_added, {"wallet_address": "0xnewleader", "falcon_score": 12.3}),
        (formatters.format_leader_excluded, {"wallet_address": "0xbot", "exclude_reason": "structural_bot"}),
        (formatters.format_runtime_config_changed, {"actor": "telegram_op", "edits": {"risk_per_trade_pct": 0.015}, "ts": time.time()}),
        (formatters.format_market_resolved_position, {"market_id": "abc123def456", "outcome": "yes", "our_direction": "yes", "size_usdc": 100, "pnl_usdc": 35.5, "venue": "paper"}),
    ],
)
def test_new_alert_formatters_render(fn, payload):
    out = fn(payload)
    assert isinstance(out, str)
    assert len(out) > 0
    # Sanity: each formatter emits at least one unicode glyph/emoji.
    assert any(ord(c) > 127 for c in out), f"{fn.__name__} produced no glyphs"


def test_format_health_handles_missing_fields():
    out = formatters.format_health({})
    assert "PIPELINE HEALTH" in out
    assert "redis: ❌" in out  # missing → not ok


def test_format_digest_hourly_with_drift_events():
    out = formatters.format_digest_hourly(
        {"trades_closed": 5, "trades_opened": 3, "wins": 3, "losses": 2, "net_pnl": 42.5, "drift_events": 2}
    )
    assert "drift events" in out
    assert "+42.50$" in out


def test_format_risk_marks_overrides():
    cfg = {"risk_per_trade_pct": 0.015, "kelly_fraction": 0.8}
    defaults = {"risk_per_trade_pct": 0.02, "kelly_fraction": 1.0}
    out = formatters.format_risk(cfg, defaults)
    assert "★" in out  # override marker present for the diffs


# --------------------------------------------------------------------------- #
# Digest                                                                       #
# --------------------------------------------------------------------------- #


async def test_hourly_digest_returns_none_on_empty_window(monkeypatch, redis_client):
    """No activity + no events → digest skipped."""
    from src.telegram_bot import digest as digest_mod

    # Patch get_db to return zero rows. The real get_db is an async
    # context manager factory (not a coroutine), so the fake is a plain
    # function returning a context manager.
    class _FakeConn:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            pass
        async def fetchval(self, *a, **kw):
            return 0
        async def fetchrow(self, *a, **kw):
            return {"n": 0, "wins": 0, "losses": 0, "net": 0}

    def fake_get_db():
        return _FakeConn()

    import src.database.connection as conn_mod
    monkeypatch.setattr(conn_mod, "get_db", fake_get_db)

    out = await digest_mod.build_hourly_digest(redis_client=redis_client)
    assert out is None
