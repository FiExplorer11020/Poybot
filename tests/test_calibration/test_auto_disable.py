"""Tests for :mod:`src.calibration.auto_disable`.

Covers the singleton plumbing, the cache TTL, and — most importantly
— the ``PROTECTED_FROM_AUTO_DISABLE`` guard that refuses auto-disable
of ``follow_confidence``.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.calibration.auto_disable import (
    ModelAutoDisabler,
    PROTECTED_FROM_AUTO_DISABLE,
    get_auto_disabler,
    init_auto_disabler,
)


@pytest.fixture
def fake_db(monkeypatch):
    """Patch :func:`src.calibration.auto_disable.get_db` with an
    in-memory dict masquerading as a postgres table.

    The fake exposes a minimal asyncpg-shaped surface: ``fetch``,
    ``fetchrow``, ``execute``. Tests inspect ``state.rows`` to verify
    what would have been written.
    """

    class _FakeConn:
        def __init__(self) -> None:
            self.rows: dict[str, dict] = {}

        async def fetch(self, sql: str, *args):
            return [dict(r) for r in self.rows.values()]

        async def fetchrow(self, sql: str, *args):
            # Only used by the streak update; not exercised in this
            # test module — return None.
            return None

        async def execute(self, sql: str, *args):
            sql_lower = sql.strip().lower()
            if sql_lower.startswith("insert into model_disable_state"):
                model, reason, auto_or_manual = args
                self.rows[model] = {
                    "model": model,
                    "is_disabled": True,
                    "disabled_at": datetime.now(tz=timezone.utc),
                    "disabled_reason": reason,
                    "auto_or_manual": auto_or_manual,
                }
                return "INSERT 0 1"
            if sql_lower.startswith("update model_disable_state"):
                (model,) = args
                if model in self.rows:
                    self.rows[model]["is_disabled"] = False
                    self.rows[model]["disabled_at"] = None
                    self.rows[model]["disabled_reason"] = None
                    self.rows[model]["auto_or_manual"] = "manual"
                    return "UPDATE 1"
                return "UPDATE 0"
            return "OK"

    conn = _FakeConn()

    class _GetDBCM:
        async def __aenter__(self):
            return conn

        async def __aexit__(self, *args):
            return None

    monkeypatch.setattr(
        "src.calibration.auto_disable.get_db",
        lambda: _GetDBCM(),
    )
    return conn


# --------------------------------------------------------------------------- #
# 1. Protected-model guard                                                    #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_auto_disable_refuses_protected_follow_confidence(fake_db):
    """``follow_confidence`` cannot be auto-disabled. The notify_fn
    fires with the emergency message instead."""
    emergency: list[str] = []

    async def _notify(msg: str) -> None:
        emergency.append(msg)

    disabler = ModelAutoDisabler(notify_fn=_notify)
    out = await disabler.disable_model(
        "follow_confidence",
        reason="drift detected for 5 consecutive days",
        auto_or_manual="auto",
    )
    assert out is False
    assert fake_db.rows == {}  # nothing written
    # The emergency alert went out.
    assert emergency
    assert "CRITICAL" in emergency[0]
    assert "follow_confidence" in emergency[0]


@pytest.mark.asyncio
async def test_manual_disable_of_protected_model_succeeds(fake_db):
    """The operator CAN manually disable ``follow_confidence`` via
    Telegram — the guard only refuses ``auto`` disables."""
    disabler = ModelAutoDisabler()
    out = await disabler.disable_model(
        "follow_confidence",
        reason="operator override",
        auto_or_manual="manual",
    )
    assert out is True
    assert "follow_confidence" in fake_db.rows
    assert fake_db.rows["follow_confidence"]["auto_or_manual"] == "manual"


@pytest.mark.asyncio
async def test_auto_disable_succeeds_for_unprotected_models(fake_db):
    disabler = ModelAutoDisabler()
    out = await disabler.disable_model(
        "volume_forecast",
        reason="drift detected for 3 consecutive days",
        auto_or_manual="auto",
    )
    assert out is True
    assert fake_db.rows["volume_forecast"]["auto_or_manual"] == "auto"


# --------------------------------------------------------------------------- #
# 2. Disable / enable round-trip                                              #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_disable_then_enable_round_trip(fake_db):
    disabler = ModelAutoDisabler()
    # Initial: nothing disabled.
    assert (await disabler.is_disabled("volume_forecast")) is False
    # Disable.
    await disabler.disable_model(
        "volume_forecast", reason="x", auto_or_manual="manual"
    )
    # Cache was busted — fresh read sees it.
    assert (await disabler.is_disabled("volume_forecast")) is True
    # Enable.
    out = await disabler.enable_model("volume_forecast")
    assert out is True
    assert (await disabler.is_disabled("volume_forecast")) is False


@pytest.mark.asyncio
async def test_enable_returns_false_when_no_row(fake_db):
    disabler = ModelAutoDisabler()
    out = await disabler.enable_model("never_seen_model")
    assert out is False


# --------------------------------------------------------------------------- #
# 3. list_disabled                                                            #
# --------------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_list_disabled_returns_currently_disabled(fake_db):
    disabler = ModelAutoDisabler()
    await disabler.disable_model("volume_forecast", reason="a")
    await disabler.disable_model("causal_ate", reason="b")
    await disabler.enable_model("volume_forecast")
    out = await disabler.list_disabled()
    models = {row["model"] for row in out}
    assert models == {"causal_ate"}


# --------------------------------------------------------------------------- #
# 4. Singleton plumbing                                                       #
# --------------------------------------------------------------------------- #


def test_singleton_stable_across_calls():
    a = get_auto_disabler()
    b = get_auto_disabler()
    assert a is b


def test_init_overrides_singleton():
    a = get_auto_disabler()
    b = init_auto_disabler(notify_fn=lambda m: None)  # type: ignore[arg-type]
    assert a is not b
    assert get_auto_disabler() is b


# --------------------------------------------------------------------------- #
# 5. Protected set declaration is exactly the spec § 3.4 set                  #
# --------------------------------------------------------------------------- #


def test_protected_set_contains_follow_confidence_only():
    """The auto-disable spec § 3.4 protection is currently scoped to
    a single model (follow_confidence). The frozen set lets us audit
    that explicitly."""
    assert PROTECTED_FROM_AUTO_DISABLE == frozenset({"follow_confidence"})
