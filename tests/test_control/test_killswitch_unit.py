"""
Unit tests for KillswitchService — no DB, no Redis required.

We mock `get_db()` and the Redis client to verify:
  - cache hit avoids DB read
  - cache miss falls through to DB and repopulates
  - infra failure returns SAFE off state
  - is_real_execution_enabled requires BOTH flags
  - dataclass round-trip via to_dict / from_dict
  - strict path (bypass_cache=True) ignores stale cache (audit F-05)
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
# Strict path (bypass_cache=True) — audit finding F-05                         #
#                                                                              #
# The Redis cache has a TTL of 2s. Between a DB flip committing and the cache  #
# being refreshed, fast-path readers can see a stale value. Live-execution     #
# gates MUST bypass the cache so a flip propagates within one DB roundtrip     #
# rather than within the cache TTL.                                            #
# --------------------------------------------------------------------------- #


async def test_strict_path_ignores_stale_cache(monkeypatch):
    """The exact F-05 leak scenario:

      1. DB says killswitch DISABLED (real_execution_enabled=False).
      2. Redis still serves a stale ENABLED payload (cache not yet expired).
      3. A live-trade gate calls is_real_execution_enabled(bypass_cache=True).
      4. The result MUST be DISABLED — the stale cache must NOT be trusted.
    """
    # 1. DB: real_execution_enabled = False (the disabled truth).
    db_row = {
        "execution_enabled": True,
        "real_execution_enabled": False,
        "paused_reason": "operator_disabled_live",
        "updated_by": "oscar",
        "updated_at": datetime.now(tz=timezone.utc),
    }
    db_conn = _FakeConn(row=db_row)
    _patch_get_db(monkeypatch, db_conn)

    # 2. Redis cache: stale ENABLED payload (what a non-strict reader would see).
    stale_payload = json.dumps(
        {
            "execution_enabled": True,
            "real_execution_enabled": True,  # stale!
            "paused_reason": None,
            "updated_by": "stale_writer",
            "updated_at": datetime.now(tz=timezone.utc).isoformat(),
        }
    )
    redis = MagicMock()
    redis.get = AsyncMock(return_value=stale_payload)
    redis.set = AsyncMock(return_value=True)
    redis.delete = AsyncMock(return_value=1)

    svc = KillswitchService(redis_client=redis)

    # Sanity: the fast path returns the STALE value (this is the bug we are
    # closing — the fast path is still allowed for non-execution callers).
    fast_path = await svc.is_real_execution_enabled()
    assert fast_path is True, "fast path should still hit cache (paper-safe)"

    # 3 & 4. Strict path: must NOT hit the cache; must consult the DB and
    # return DISABLED.
    strict = await svc.is_real_execution_enabled(bypass_cache=True)
    assert strict is False, (
        "strict-path read must observe the DB-disabled state and refuse "
        "real execution even when the cache says ENABLED"
    )


async def test_strict_path_refreshes_cache_for_subsequent_readers(monkeypatch):
    """After a strict-path read pulls fresh state from the DB, the cache
    is repopulated so the next fast-path reader sees the new value too —
    this shortens the leak window for paper/dashboard callers as well."""
    db_row = {
        "execution_enabled": True,
        "real_execution_enabled": False,  # truth
        "paused_reason": None,
        "updated_by": "test",
        "updated_at": datetime.now(tz=timezone.utc),
    }
    db_conn = _FakeConn(row=db_row)
    _patch_get_db(monkeypatch, db_conn)

    redis = MagicMock()
    redis.get = AsyncMock(return_value=None)  # cache miss (irrelevant on strict)
    redis.set = AsyncMock(return_value=True)

    svc = KillswitchService(redis_client=redis)
    state = await svc.get_state(bypass_cache=True)

    assert state.real_execution_enabled is False
    # Strict path should still write-through to the cache.
    redis.set.assert_awaited_once()
    # AND it must NOT have read the cache at all (bypass_cache=True).
    redis.get.assert_not_called()


async def test_strict_path_fail_safe_on_db_failure(monkeypatch):
    """If the strict-path DB read itself fails, return SAFE-OFF — never
    fall back to a (potentially stale) cached value to "rescue" the read."""
    redis = MagicMock()
    # If the implementation accidentally fell back to the cache, this would
    # return an ENABLED state and the assertion below would fail.
    redis.get = AsyncMock(
        return_value=json.dumps(
            {
                "execution_enabled": True,
                "real_execution_enabled": True,
                "paused_reason": None,
                "updated_by": "stale",
                "updated_at": datetime.now(tz=timezone.utc).isoformat(),
            }
        )
    )
    redis.set = AsyncMock(return_value=True)

    @asynccontextmanager
    async def _broken_get_db():
        raise RuntimeError("DB pool not initialized")
        yield  # unreachable

    monkeypatch.setattr(ks_mod, "get_db", _broken_get_db)

    svc = KillswitchService(redis_client=redis)
    strict = await svc.is_real_execution_enabled(bypass_cache=True)

    # Must default to OFF on infra failure, even with a "happy" cache.
    assert strict is False


# --------------------------------------------------------------------------- #
# Singleton sanity                                                              #
# --------------------------------------------------------------------------- #


def test_get_killswitch_returns_singleton():
    ks_mod._singleton = None  # reset for test isolation
    a = ks_mod.get_killswitch()
    b = ks_mod.get_killswitch()
    assert a is b
    ks_mod._singleton = None
