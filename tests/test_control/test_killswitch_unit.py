"""
Unit tests for KillswitchService — no DB, no Redis required.

We mock `get_db()` and the Redis client to verify:
  - cache hit avoids DB read
  - cache miss falls through to DB and repopulates
  - infra failure returns SAFE off state
  - is_real_execution_enabled requires BOTH flags
  - dataclass round-trip via to_dict / from_dict
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.control import killswitch as ks_mod
from src.control.killswitch import KillswitchService, KillswitchState


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #


class _FakeConn:
    """Minimal asyncpg.Connection stand-in."""

    def __init__(self, row=None):
        self._row = row
        self.executed = []

    async def fetchrow(self, sql, *args):
        return self._row

    async def execute(self, sql, *args):
        self.executed.append((sql, args))
        return "OK"

    def transaction(self):
        return _FakeTxn()


class _FakeTxn:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False


def _patch_get_db(monkeypatch, conn: _FakeConn):
    @asynccontextmanager
    async def _fake_get_db():
        yield conn

    monkeypatch.setattr(ks_mod, "get_db", _fake_get_db)


# --------------------------------------------------------------------------- #
# State dataclass                                                              #
# --------------------------------------------------------------------------- #


def test_state_to_dict_and_from_dict_roundtrip():
    ts = datetime.now(tz=timezone.utc)
    s = KillswitchState(
        execution_enabled=True,
        real_execution_enabled=False,
        paused_reason="manual_test",
        updated_by="oscar",
        updated_at=ts,
    )
    d = s.to_dict()
    assert d["execution_enabled"] is True
    assert d["real_execution_enabled"] is False
    s2 = KillswitchState.from_dict(d)
    assert s2.execution_enabled == s.execution_enabled
    assert s2.real_execution_enabled == s.real_execution_enabled
    assert s2.paused_reason == s.paused_reason


# --------------------------------------------------------------------------- #
# Cache hit                                                                    #
# --------------------------------------------------------------------------- #


async def test_cache_hit_does_not_hit_db(monkeypatch):
    cached_payload = json.dumps(
        {
            "execution_enabled": True,
            "real_execution_enabled": True,
            "paused_reason": None,
            "updated_by": "test",
            "updated_at": datetime.now(tz=timezone.utc).isoformat(),
        }
    )
    redis = MagicMock()
    redis.get = AsyncMock(return_value=cached_payload)

    db_conn = _FakeConn(row=None)  # if hit, fetchrow returns None → would default safe-off
    _patch_get_db(monkeypatch, db_conn)

    svc = KillswitchService(redis_client=redis)
    state = await svc.get_state()

    assert state.execution_enabled is True
    assert state.real_execution_enabled is True
    redis.get.assert_awaited_once()
    # No DB call should have happened — proxy via no executed statements.
    assert db_conn.executed == []


# --------------------------------------------------------------------------- #
# Cache miss → DB read → cache write                                            #
# --------------------------------------------------------------------------- #


async def test_cache_miss_falls_back_to_db_and_repopulates(monkeypatch):
    redis = MagicMock()
    redis.get = AsyncMock(return_value=None)
    redis.set = AsyncMock(return_value=True)

    row = {
        "execution_enabled": True,
        "real_execution_enabled": False,
        "paused_reason": None,
        "updated_by": "migration_006",
        "updated_at": datetime.now(tz=timezone.utc),
    }
    db_conn = _FakeConn(row=row)
    _patch_get_db(monkeypatch, db_conn)

    svc = KillswitchService(redis_client=redis)
    state = await svc.get_state()

    assert state.execution_enabled is True
    assert state.real_execution_enabled is False
    redis.get.assert_awaited_once()
    redis.set.assert_awaited_once()  # cache repopulated


# --------------------------------------------------------------------------- #
# Infra failure → SAFE OFF                                                     #
# --------------------------------------------------------------------------- #


async def test_db_failure_returns_safe_off_state(monkeypatch):
    redis = MagicMock()
    redis.get = AsyncMock(return_value=None)
    redis.set = AsyncMock(return_value=True)

    @asynccontextmanager
    async def _broken_get_db():
        raise RuntimeError("DB pool not initialized")
        yield  # unreachable

    monkeypatch.setattr(ks_mod, "get_db", _broken_get_db)

    svc = KillswitchService(redis_client=redis)
    state = await svc.get_state()

    # Must default to OFF on infra failure — the "fail safe" requirement.
    assert state.execution_enabled is False
    assert state.real_execution_enabled is False
    assert state.paused_reason is not None
    assert "infra_failure" in state.paused_reason


async def test_redis_failure_does_not_break_db_read(monkeypatch):
    redis = MagicMock()
    redis.get = AsyncMock(side_effect=ConnectionError("redis down"))
    redis.set = AsyncMock(side_effect=ConnectionError("redis down"))

    row = {
        "execution_enabled": True,
        "real_execution_enabled": False,
        "paused_reason": None,
        "updated_by": "test",
        "updated_at": datetime.now(tz=timezone.utc),
    }
    db_conn = _FakeConn(row=row)
    _patch_get_db(monkeypatch, db_conn)

    svc = KillswitchService(redis_client=redis)
    state = await svc.get_state()

    # Redis failure must NOT cascade — DB is the source of truth.
    assert state.execution_enabled is True


# --------------------------------------------------------------------------- #
# is_real_execution_enabled — requires BOTH flags                              #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "exec_, real, expected",
    [
        (True, True, True),    # both on → real allowed
        (True, False, False),  # master on, real off → real refused
        (False, True, False),  # master off, real on → real refused (master wins)
        (False, False, False), # both off → real refused
    ],
)
async def test_is_real_execution_requires_both_flags(monkeypatch, exec_, real, expected):
    cached_payload = json.dumps(
        {
            "execution_enabled": exec_,
            "real_execution_enabled": real,
            "paused_reason": None,
            "updated_by": "test",
            "updated_at": datetime.now(tz=timezone.utc).isoformat(),
        }
    )
    redis = MagicMock()
    redis.get = AsyncMock(return_value=cached_payload)

    svc = KillswitchService(redis_client=redis)
    assert await svc.is_real_execution_enabled() is expected


# --------------------------------------------------------------------------- #
# Singleton sanity                                                              #
# --------------------------------------------------------------------------- #


def test_get_killswitch_returns_singleton():
    ks_mod._singleton = None  # reset for test isolation
    a = ks_mod.get_killswitch()
    b = ks_mod.get_killswitch()
    assert a is b
    ks_mod._singleton = None
