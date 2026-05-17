"""Regression tests for the robust ``backfill_resolved_outcomes`` rewrite.

The 2026-05-17 production audit found the job stalled at populated=0
despite running, with HTTP 429 floods from Gamma's closed-market
endpoint. This module pins the contract of the rewrite:

  - Happy path: pagination walks Gamma until the response is empty.
  - Idempotency: the UPDATE only fires WHERE resolved_outcome IS NULL.
  - Retry on 429: exponential backoff with jitter, ``Retry-After`` honoured.
  - Bail-out after 5 consecutive 429s — no raise, just skip + ERROR log.
  - Malformed payloads (missing outcomePrices) increment ``skipped_malformed``.
  - Lag alert publishes on ``engine:backfill:lag_alert`` over the threshold.

All tests use lightweight asyncpg / aiohttp / redis stubs so they stay
pure-Python and never touch a real backend.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from scripts import maintenance_loop as ml


# --------------------------------------------------------------------------- #
# Helpers — asyncpg pool stub                                                  #
# --------------------------------------------------------------------------- #


class _FakeConn:
    def __init__(self, parent: "_FakePool") -> None:
        self._parent = parent

    async def execute(self, sql: str, *args: Any) -> str:
        return self._parent._on_execute(sql, args)

    async def fetchval(self, sql: str, *args: Any) -> Any:
        return self._parent._on_fetchval(sql, args)


class _FakePool:
    """Records executes + fetchvals; replays canned responses by SQL substring."""

    def __init__(self) -> None:
        self.executed: list[tuple[str, tuple]] = []
        self.fetchvals: list[tuple[str, tuple]] = []
        self.execute_handlers: list[tuple[str, Any]] = []
        self.fetchval_handlers: list[tuple[str, Any]] = []

    def acquire(self) -> Any:
        @asynccontextmanager
        async def _ctx():
            yield _FakeConn(self)
        return _ctx()

    def on_execute(self, substr: str, response: Any) -> None:
        self.execute_handlers.append((substr, response))

    def on_fetchval(self, substr: str, response: Any) -> None:
        self.fetchval_handlers.append((substr, response))

    def _on_execute(self, sql: str, args: tuple) -> str:
        self.executed.append((sql, args))
        return _resolve(self.execute_handlers, sql, args, default="UPDATE 0")

    def _on_fetchval(self, sql: str, args: tuple) -> Any:
        self.fetchvals.append((sql, args))
        return _resolve(self.fetchval_handlers, sql, args, default=0)


def _resolve(handlers, sql, args, default):
    chosen = None
    for substr, response in handlers:
        if substr in sql:
            chosen = response
    if chosen is None:
        return default
    if callable(chosen):
        return chosen(args)
    return chosen


# --------------------------------------------------------------------------- #
# Helpers — aiohttp session stub                                               #
# --------------------------------------------------------------------------- #


class _FakeResp:
    """Async-context-manager response with optional ``Retry-After`` header."""

    def __init__(
        self,
        status: int,
        payload: Any,
        headers: dict | None = None,
    ) -> None:
        self.status = status
        self._payload = payload
        self.headers = headers or {}

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self):
        return self._payload


class _ScriptedSession:
    """Returns a scripted sequence of responses per ``get`` call.

    Each entry is either a `_FakeResp` (returned as-is) or a callable that
    inspects the params (`{"offset": "0", "limit": "100", ...}`) and
    returns a `_FakeResp`. Useful for paginated tests.
    """

    def __init__(self, responses: list[Any]) -> None:
        self._responses = responses
        self._call = 0
        self.calls: list[dict] = []

    def get(self, url: str, *, params=None, timeout=None):
        self.calls.append({"url": url, "params": params or {}})
        idx = self._call
        self._call += 1
        if idx >= len(self._responses):
            return _FakeResp(200, [])
        entry = self._responses[idx]
        if callable(entry):
            return entry(params or {})
        return entry


def _market(cid: str, yes_price: str = "1", *, closed: bool = True) -> dict:
    """Build a minimal Gamma-shaped market dict."""
    return {
        "conditionId": cid,
        "closed": closed,
        "outcomePrices": f'["{yes_price}", "0"]' if yes_price == "1" else
                          f'["{yes_price}", "1"]',
        "endDate": "2024-01-01T00:00:00Z",
    }


# --------------------------------------------------------------------------- #
# Tests                                                                        #
# --------------------------------------------------------------------------- #


class TestBackfillResolvedOutcomesRobust:
    async def test_happy_path_single_page(self):
        """100 markets → 100 populated; metrics line carries the right
        counters and ``missing_after`` is zero so no lag alert fires."""
        pool = _FakePool()
        # Every UPDATE writes one row (idempotent path: resolved_outcome IS NULL).
        pool.on_execute("UPDATE markets", "UPDATE 1")
        pool.on_fetchval("SELECT COUNT(*)", 0)

        page = [_market(f"0x{i:064x}", yes_price="1") for i in range(100)]
        session = _ScriptedSession([_FakeResp(200, page), _FakeResp(200, [])])

        result = await ml.backfill_resolved_outcomes(
            pool, session,
            batch_size=500,
            lag_alert_threshold=1000,
            initial_backoff_s=0.0,
            max_backoff_s=0.0,
        )

        assert result["fetched"] == 100
        assert result["populated"] == 100
        assert result["skipped_malformed"] == 0
        assert result["retried_429"] == 0
        assert result["missing_after"] == 0
        assert result["lag_alert_fired"] is False
        # All UPDATEs target the right markets table + filter.
        for sql, _ in pool.executed:
            assert "UPDATE markets" in sql
            assert "resolved_outcome IS NULL" in sql

    async def test_pagination_walks_until_empty(self):
        """250 markets distributed over 3 pages (100 + 100 + 50) all
        populated. The 4th call would be unnecessary since len(page) <
        page_limit on the third call. Verify the paginator stops cleanly."""
        pool = _FakePool()
        pool.on_execute("UPDATE markets", "UPDATE 1")
        pool.on_fetchval("SELECT COUNT(*)", 0)

        p1 = [_market(f"0xA{i:063x}", "1") for i in range(100)]
        p2 = [_market(f"0xB{i:063x}", "1") for i in range(100)]
        p3 = [_market(f"0xC{i:063x}", "0") for i in range(50)]
        session = _ScriptedSession([
            _FakeResp(200, p1),
            _FakeResp(200, p2),
            _FakeResp(200, p3),
        ])

        result = await ml.backfill_resolved_outcomes(
            pool, session, batch_size=500, initial_backoff_s=0.0,
            max_backoff_s=0.0,
        )

        assert result["fetched"] == 250
        assert result["populated"] == 250
        # Three GET requests, in order, with monotonically increasing offset.
        offsets = [int(c["params"]["offset"]) for c in session.calls]
        assert offsets == [0, 100, 200]

    async def test_http_429_retries_then_succeeds(self):
        """First call returns 429, second returns the page. The retry
        counter MUST increment and the page MUST eventually populate."""
        pool = _FakePool()
        pool.on_execute("UPDATE markets", "UPDATE 1")
        pool.on_fetchval("SELECT COUNT(*)", 0)

        page = [_market("0xRETRY", "1")]
        session = _ScriptedSession([
            _FakeResp(429, None, headers={"Retry-After": "0"}),
            _FakeResp(200, page),
            _FakeResp(200, []),
        ])

        result = await ml.backfill_resolved_outcomes(
            pool, session,
            batch_size=500,
            initial_backoff_s=0.0,
            max_backoff_s=0.0,
        )

        assert result["retried_429"] == 1
        assert result["fetched"] == 1
        assert result["populated"] == 1

    async def test_five_consecutive_429s_skip_batch_no_raise(self):
        """5 consecutive 429s on the same endpoint → the function logs
        ERROR (caller path) and returns gracefully with populated=0.
        No exception bubbles up so the maintenance loop survives."""
        pool = _FakePool()
        pool.on_execute("UPDATE markets", "UPDATE 1")
        pool.on_fetchval("SELECT COUNT(*)", 0)

        # 5 × 429 ⇒ paginator bails after BACKFILL_MAX_CONSECUTIVE_429.
        session = _ScriptedSession([
            _FakeResp(429, None, headers={"Retry-After": "0"})
            for _ in range(ml.BACKFILL_MAX_CONSECUTIVE_429)
        ])

        result = await ml.backfill_resolved_outcomes(
            pool, session,
            batch_size=500,
            initial_backoff_s=0.0,
            max_backoff_s=0.0,
        )

        # Five attempts consumed, no successful fetch.
        assert result["retried_429"] == ml.BACKFILL_MAX_CONSECUTIVE_429
        assert result["fetched"] == 0
        assert result["populated"] == 0
        # No UPDATE was issued.
        update_calls = [s for s, _ in pool.executed if "UPDATE markets" in s]
        assert update_calls == []

    async def test_malformed_payload_increments_skipped(self):
        """A market missing ``outcomePrices`` is skipped (no UPDATE) and
        the ``skipped_malformed`` counter ticks up — the rest of the
        batch still processes."""
        pool = _FakePool()
        pool.on_execute("UPDATE markets", "UPDATE 1")
        pool.on_fetchval("SELECT COUNT(*)", 0)

        good = _market("0xGOOD", "1")
        bad_no_prices = {
            "conditionId": "0xBADNOPRICES",
            "closed": True,
        }
        bad_bad_json = {
            "conditionId": "0xBADJSON",
            "closed": True,
            "outcomePrices": "this is not json",
        }
        bad_no_cid = {
            "closed": True,
            "outcomePrices": '["1","0"]',
        }
        session = _ScriptedSession([
            _FakeResp(200, [good, bad_no_prices, bad_bad_json, bad_no_cid]),
            _FakeResp(200, []),
        ])

        result = await ml.backfill_resolved_outcomes(
            pool, session, batch_size=500, initial_backoff_s=0.0,
            max_backoff_s=0.0,
        )

        assert result["fetched"] == 4
        assert result["populated"] == 1
        assert result["skipped_malformed"] == 3
        # Exactly one UPDATE was issued (the good market).
        update_calls = [s for s, args in pool.executed if "UPDATE markets" in s]
        assert len(update_calls) == 1

    async def test_lag_alert_publishes_when_threshold_exceeded(self):
        """If after a run COUNT(missing) > threshold, the function MUST
        publish an envelope on ``engine:backfill:lag_alert`` and flip
        ``lag_alert_fired`` to True. CRITICAL for operator escalation."""
        pool = _FakePool()
        pool.on_execute("UPDATE markets", "UPDATE 1")
        # Simulate a remaining backlog of 50000 missing rows.
        pool.on_fetchval("SELECT COUNT(*)", 50000)

        page = [_market("0xLAG", "1")]
        session = _ScriptedSession([_FakeResp(200, page), _FakeResp(200, [])])
        redis_client = MagicMock()
        redis_client.publish = AsyncMock()

        result = await ml.backfill_resolved_outcomes(
            pool, session,
            redis_client=redis_client,
            batch_size=500,
            lag_alert_threshold=1000,
            initial_backoff_s=0.0,
            max_backoff_s=0.0,
        )

        assert result["missing_after"] == 50000
        assert result["lag_alert_fired"] is True
        # Exactly one publish call on the expected channel.
        assert redis_client.publish.await_count == 1
        channel, payload = redis_client.publish.await_args.args
        assert channel == ml.REDIS_BACKFILL_LAG_ALERT_CHANNEL
        envelope = json.loads(payload)
        assert envelope["type"] == "backfill_resolved_outcomes_lag"
        assert envelope["missing_count"] == 50000
        assert envelope["threshold"] == 1000
        assert "ts" in envelope

    async def test_lag_alert_silent_below_threshold(self):
        """When missing_after ≤ threshold, NO publish fires and the
        flag stays False. Operator only hears about real lag."""
        pool = _FakePool()
        pool.on_execute("UPDATE markets", "UPDATE 1")
        pool.on_fetchval("SELECT COUNT(*)", 250)  # well under 1000

        page = [_market("0xOK", "1")]
        session = _ScriptedSession([_FakeResp(200, page), _FakeResp(200, [])])
        redis_client = MagicMock()
        redis_client.publish = AsyncMock()

        result = await ml.backfill_resolved_outcomes(
            pool, session,
            redis_client=redis_client,
            batch_size=500,
            lag_alert_threshold=1000,
            initial_backoff_s=0.0,
            max_backoff_s=0.0,
        )

        assert result["missing_after"] == 250
        assert result["lag_alert_fired"] is False
        redis_client.publish.assert_not_called()

    async def test_idempotent_second_run_no_rewrite(self):
        """Idempotency contract: running the job twice in a row when
        every row already has resolved_outcome ⇒ asyncpg returns
        ``UPDATE 0`` for every UPDATE and ``populated`` stays at 0 on
        the second run."""
        pool = _FakePool()
        # Simulate "every row already has resolved_outcome set":
        # the partial-index filter `WHERE resolved_outcome IS NULL`
        # makes every UPDATE a no-op.
        pool.on_execute("UPDATE markets", "UPDATE 0")
        pool.on_fetchval("SELECT COUNT(*)", 0)

        page = [_market(f"0x{i:064x}", "1") for i in range(10)]

        # Each run gets a FRESH scripted session — in prod every tick
        # also opens a fresh paginator state. What matters for the
        # idempotency contract is that BOTH runs report populated=0
        # when the rows are already resolved (UPDATE 0 from asyncpg).
        session1 = _ScriptedSession([_FakeResp(200, page), _FakeResp(200, [])])
        session2 = _ScriptedSession([_FakeResp(200, page), _FakeResp(200, [])])

        result1 = await ml.backfill_resolved_outcomes(
            pool, session1, batch_size=500, initial_backoff_s=0.0,
            max_backoff_s=0.0,
        )
        result2 = await ml.backfill_resolved_outcomes(
            pool, session2, batch_size=500, initial_backoff_s=0.0,
            max_backoff_s=0.0,
        )

        # Each run sees 10 markets but UPDATE-affected rows is 0 on both.
        assert result1["fetched"] == 10
        assert result1["populated"] == 0
        assert result2["fetched"] == 10
        assert result2["populated"] == 0
        # 20 UPDATEs were attempted across both runs.
        update_calls = [s for s, _ in pool.executed if "UPDATE markets" in s]
        assert len(update_calls) == 20

    async def test_outcome_parsing_yes_no_boundary(self):
        """yes_terminal > 0.5 ⇒ "yes"; ≤ 0.5 ⇒ "no". Verify the second
        positional UPDATE argument carries the right value."""
        pool = _FakePool()
        pool.on_execute("UPDATE markets", "UPDATE 1")
        pool.on_fetchval("SELECT COUNT(*)", 0)

        page = [
            {"conditionId": "0xYES", "closed": True,
             "outcomePrices": '["0.95","0.05"]'},
            {"conditionId": "0xNO", "closed": True,
             "outcomePrices": '["0.10","0.90"]'},
            # Boundary: exactly 0.5 → "no".
            {"conditionId": "0xBOUND", "closed": True,
             "outcomePrices": '["0.50","0.50"]'},
        ]
        session = _ScriptedSession([_FakeResp(200, page), _FakeResp(200, [])])

        result = await ml.backfill_resolved_outcomes(
            pool, session, batch_size=500, initial_backoff_s=0.0,
            max_backoff_s=0.0,
        )
        assert result["populated"] == 3
        # Inspect the args passed to each UPDATE.
        update_args = [args for sql, args in pool.executed if "UPDATE markets" in sql]
        outcomes_by_cid = {a[0]: a[1] for a in update_args}
        assert outcomes_by_cid["0xYES"] == "yes"
        assert outcomes_by_cid["0xNO"] == "no"
        assert outcomes_by_cid["0xBOUND"] == "no"

    async def test_retry_after_header_honoured(self):
        """When Gamma sends ``Retry-After: <seconds>``, the paginator
        uses that value as the sleep delay (capped to max_backoff_s)
        instead of the computed exponential backoff."""
        # We use a Retry-After of 0 to keep the test fast; what we're
        # validating is the code path that READS the header.
        pool = _FakePool()
        pool.on_execute("UPDATE markets", "UPDATE 1")
        pool.on_fetchval("SELECT COUNT(*)", 0)

        page = [_market("0xRH", "1")]
        session = _ScriptedSession([
            _FakeResp(429, None, headers={"Retry-After": "0"}),
            _FakeResp(429, None, headers={"Retry-After": "0"}),
            _FakeResp(200, page),
            _FakeResp(200, []),
        ])

        result = await ml.backfill_resolved_outcomes(
            pool, session,
            batch_size=500,
            initial_backoff_s=10.0,  # Would normally sleep 10s,
            max_backoff_s=20.0,      # capped at 20. Retry-After=0 overrides.
        )

        assert result["retried_429"] == 2
        assert result["populated"] == 1
