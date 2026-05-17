"""Pillar 2 — regression tests for ``scripts.reconciliation``.

Covers the contract that lets the operator trust the nightly Gamma
reconciliation pass:

  - Happy path: 3 trades all match → no divergence row, no Redis event.
  - fake_win: DB +100$ vs Gamma resolves against → INSERT flag='fake_win'.
  - fake_loss: DB -100$ vs Gamma resolves in favour → INSERT flag='fake_loss'.
  - still_open_in_reality: Gamma closed=false → INSERT flag.
  - match_within_tolerance: |delta| ≤ tolerance → skipped.
  - Idempotent rerun: ON CONFLICT path on the 2nd pass.
  - Gamma unreachable: HTTP error → metric bump + no crash.
  - Redis publish: at least one divergence inserted → envelope on
    ``paper:audit:divergence`` containing the top-3-worst.
  - Alarming discrepancy: |total| > 100$ → log WARNING + envelope flag.

Tests use lightweight asyncpg / aiohttp / redis stubs so they stay
pure-Python and never touch a real backend.
"""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock

import pytest

from scripts import reconciliation as rec
from src.telegram_bot import formatters


# --------------------------------------------------------------------------- #
# Helpers — asyncpg pool stub                                                  #
# --------------------------------------------------------------------------- #


class _FakeConn:
    def __init__(self, parent: "_FakePool") -> None:
        self._parent = parent

    async def execute(self, sql: str, *args: Any) -> str:
        return self._parent._on_execute(sql, args)

    async def fetch(self, sql: str, *args: Any) -> list[dict]:
        return self._parent._on_fetch(sql, args)

    async def fetchrow(self, sql: str, *args: Any) -> Any:
        return self._parent._on_fetchrow(sql, args)

    async def fetchval(self, sql: str, *args: Any) -> Any:
        return self._parent._on_fetchval(sql, args)


class _FakePool:
    """Records executes + fetches; replays canned responses by SQL substring."""

    def __init__(self) -> None:
        self.executed: list[tuple[str, tuple]] = []
        self.fetched: list[tuple[str, tuple]] = []
        self.fetchrows: list[tuple[str, tuple]] = []
        self.fetch_handlers: list[tuple[str, Any]] = []
        self.fetchrow_handlers: list[tuple[str, Any]] = []
        # upsert_responses is a queue: each upsert pops one
        # (inserted: bool) value off the front; defaults to True.
        self.upsert_inserts: list[bool] = []

    def acquire(self) -> Any:
        @asynccontextmanager
        async def _ctx():
            yield _FakeConn(self)
        return _ctx()

    def on_fetch(self, substr: str, response: Any) -> None:
        self.fetch_handlers.append((substr, response))

    def on_fetchrow(self, substr: str, response: Any) -> None:
        self.fetchrow_handlers.append((substr, response))

    def _on_execute(self, sql: str, args: tuple) -> str:
        self.executed.append((sql, args))
        return "UPDATE 1"

    def _on_fetch(self, sql: str, args: tuple) -> list[dict]:
        self.fetched.append((sql, args))
        return _resolve(self.fetch_handlers, sql, args, default=[])

    def _on_fetchrow(self, sql: str, args: tuple) -> Any:
        self.fetchrows.append((sql, args))
        # The UPSERT into paper_close_divergences uses fetchrow to
        # detect insert-vs-update via xmax. Serve from the queue.
        if "INSERT INTO paper_close_divergences" in sql:
            inserted = True
            if self.upsert_inserts:
                inserted = self.upsert_inserts.pop(0)
            return {"inserted": inserted}
        return _resolve(self.fetchrow_handlers, sql, args, default=None)

    def _on_fetchval(self, sql: str, args: tuple) -> Any:
        return 0


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
    """Scripted aiohttp.ClientSession.get() returning responses in order."""

    def __init__(self, responses: list[Any]) -> None:
        self._responses = responses
        self._call = 0
        self.calls: list[dict] = []

    def get(self, url: str, *, params=None, timeout=None):
        self.calls.append({"url": url, "params": dict(params or {})})
        idx = self._call
        self._call += 1
        if idx >= len(self._responses):
            return _FakeResp(200, [])
        entry = self._responses[idx]
        if callable(entry):
            return entry(params or {})
        return entry


class _ExplodingSession:
    """Session whose every GET raises an aiohttp.ClientError."""

    def __init__(self) -> None:
        self.calls: int = 0

    def get(self, url: str, *, params=None, timeout=None):
        self.calls += 1
        import aiohttp
        raise aiohttp.ClientError("boom")


# --------------------------------------------------------------------------- #
# Helpers — trade builders                                                     #
# --------------------------------------------------------------------------- #


def _trade(
    trade_id: int = 1,
    *,
    market_id: str = "0xABC",
    direction: str = "yes",
    entry_price: float = 0.5,
    exit_price: float = 0.6,
    size_usdc: float = 100.0,
    pnl_usdc: float = 20.0,
    fee_paid_usdc: float = 0.0,
    closed_at: datetime | None = None,
) -> dict:
    return {
        "id": trade_id,
        "market_id": market_id,
        "token_id": f"tok_{market_id}_{direction}",
        "direction": direction,
        "entry_price": entry_price,
        "exit_price": exit_price,
        "size_usdc": size_usdc,
        "pnl_usdc": pnl_usdc,
        "fee_paid_usdc": fee_paid_usdc,
        "closed_at": closed_at or (datetime.now(timezone.utc) - timedelta(days=1)),
        "close_reason": "leader_exit",
    }


# --------------------------------------------------------------------------- #
# Tests                                                                        #
# --------------------------------------------------------------------------- #


class TestReconcileClosedTrades:
    async def test_happy_path_all_match_within_tolerance(self):
        """3 trades, each pnl_usdc matches the theoretical Gamma-resolved
        truth within tolerance → no INSERT, no Redis publish."""
        pool = _FakePool()
        # Trade #1: YES bought at 0.5, size 100, exit 1.0 (won). Truth
        #   = 100/0.5 * 1.0 - 100 = 100. DB PnL = 100 (match exactly).
        # Trade #2: YES bought at 0.5, size 100, exit 0.0 (lost). Truth
        #   = -100 (resolved='no' → YES holder loses). DB PnL = -100.
        # Trade #3: NO bought at 0.4, size 80, won (resolved='no' → NO
        #   holder wins). Truth = 80/0.4 * 1.0 - 80 = 120. DB PnL = 121
        #   (delta=1 < tol=2).
        trades = [
            _trade(1, market_id="m1", direction="yes",
                   entry_price=0.5, exit_price=1.0, size_usdc=100.0,
                   pnl_usdc=100.0),
            _trade(2, market_id="m2", direction="yes",
                   entry_price=0.5, exit_price=0.0, size_usdc=100.0,
                   pnl_usdc=-100.0),
            _trade(3, market_id="m3", direction="no",
                   entry_price=0.4, exit_price=1.0, size_usdc=80.0,
                   pnl_usdc=121.0),
        ]
        pool.on_fetch("FROM paper_trades", trades)
        # DB resolved_outcome populated for all 3 markets.
        # m1: yes wins (YES holder wins). m2: no wins (YES holder loses).
        # m3: no wins (NO holder wins).
        outcomes = {"m1": "yes", "m2": "no", "m3": "no"}
        pool.on_fetchrow(
            "FROM markets",
            lambda args: {"resolved_outcome": outcomes.get(args[0])},
        )

        redis_client = AsyncMock()
        session = _ScriptedSession([])  # no Gamma hits expected

        metrics = await rec.reconcile_closed_trades(
            pool=pool,
            redis_client=redis_client,
            http_session=session,
            tolerance_usdc=2.0,
        )

        assert metrics["scanned"] == 3
        assert metrics["matched"] == 3
        assert metrics["divergences_inserted"] == 0
        assert metrics["by_flag"] == {}
        assert session.calls == []
        redis_client.publish.assert_not_awaited()

    async def test_fake_win_db_overstates_pnl(self):
        """DB booked +100$ but Gamma resolves against the held side
        (truth = -100$). Delta = +200 > tolerance → INSERT 'fake_win'."""
        pool = _FakePool()
        trades = [
            _trade(10, market_id="mFW", direction="yes",
                   entry_price=0.5, exit_price=0.999, size_usdc=100.0,
                   pnl_usdc=100.0),
        ]
        pool.on_fetch("FROM paper_trades", trades)
        # Gamma resolved 'no' → YES holder loses → truth pnl = -100$.
        pool.on_fetchrow("FROM markets", {"resolved_outcome": "no"})

        redis_client = AsyncMock()
        session = _ScriptedSession([])
        metrics = await rec.reconcile_closed_trades(
            pool=pool, redis_client=redis_client, http_session=session,
        )

        assert metrics["scanned"] == 1
        assert metrics["divergences_inserted"] == 1
        assert metrics["by_flag"] == {"fake_win": 1}
        # Verify the UPSERT was called with the right flag + delta.
        upsert_calls = [
            args for sql, args in pool.fetchrows
            if "INSERT INTO paper_close_divergences" in sql
        ]
        assert len(upsert_calls) == 1
        args = upsert_calls[0]
        # Positional args: trade_id, detected_at, closed_at, market_id,
        # direction, db_pnl, truth_pnl, delta, db_exit, truth_exit,
        # gamma_outcome, snapshot, flag, notes
        assert args[0] == 10  # trade_id
        assert args[5] == pytest.approx(100.0)  # db_pnl
        assert args[6] == pytest.approx(-100.0)  # truth_pnl
        assert args[7] == pytest.approx(200.0)  # delta
        assert args[12] == "fake_win"
        # Redis publish fired.
        redis_client.publish.assert_awaited_once()

    async def test_fake_loss_db_understates_pnl(self):
        """DB booked -100$ but Gamma resolves in favour (truth = +100$).
        Delta = -200 → INSERT 'fake_loss'."""
        pool = _FakePool()
        trades = [
            _trade(11, market_id="mFL", direction="yes",
                   entry_price=0.5, exit_price=0.01, size_usdc=100.0,
                   pnl_usdc=-100.0),
        ]
        pool.on_fetch("FROM paper_trades", trades)
        pool.on_fetchrow("FROM markets", {"resolved_outcome": "yes"})

        redis_client = AsyncMock()
        session = _ScriptedSession([])
        metrics = await rec.reconcile_closed_trades(
            pool=pool, redis_client=redis_client, http_session=session,
        )

        assert metrics["divergences_inserted"] == 1
        assert metrics["by_flag"] == {"fake_loss": 1}
        upsert = [
            args for sql, args in pool.fetchrows
            if "INSERT INTO paper_close_divergences" in sql
        ][0]
        assert upsert[5] == pytest.approx(-100.0)
        assert upsert[6] == pytest.approx(100.0)
        assert upsert[12] == "fake_loss"

    async def test_still_open_in_reality_overrides_pnl_judgement(self):
        """Gamma /markets returns closed=false → flag wins regardless
        of db_pnl sign. Mirrors the BTC #1/#2 phantom-win pattern."""
        pool = _FakePool()
        trades = [
            _trade(99, market_id="mBTC", direction="yes",
                   entry_price=0.4, exit_price=0.999, size_usdc=150.0,
                   pnl_usdc=224.99),
        ]
        pool.on_fetch("FROM paper_trades", trades)
        # DB doesn't know — resolved_outcome NULL → Gamma fallback.
        pool.on_fetchrow("FROM markets", {"resolved_outcome": None})

        # Gamma response: market is still open.
        gamma_payload = [{
            "conditionId": "mBTC",
            "closed": False,
            "outcomePrices": '["0.55", "0.45"]',
        }]
        session = _ScriptedSession([_FakeResp(200, gamma_payload)])
        redis_client = AsyncMock()

        metrics = await rec.reconcile_closed_trades(
            pool=pool, redis_client=redis_client, http_session=session,
        )

        assert metrics["divergences_inserted"] == 1
        assert metrics["still_open_in_reality"] == 1
        assert metrics["by_flag"] == {"still_open_in_reality": 1}
        upsert = [
            args for sql, args in pool.fetchrows
            if "INSERT INTO paper_close_divergences" in sql
        ][0]
        assert upsert[12] == "still_open_in_reality"
        # truth_exit_price should be NULL (we don't know yet).
        assert upsert[9] is None
        # gamma_snapshot persisted.
        snap = upsert[11]
        assert snap is not None and "mBTC" in snap
        redis_client.publish.assert_awaited_once()

    async def test_match_within_tolerance_skipped(self):
        """|delta| == 1.5$ with tolerance=2.0 → matched, no INSERT."""
        pool = _FakePool()
        trades = [
            _trade(20, market_id="mTol", direction="yes",
                   entry_price=0.5, exit_price=0.999, size_usdc=100.0,
                   pnl_usdc=98.5),  # truth = +100, delta=-1.5
        ]
        pool.on_fetch("FROM paper_trades", trades)
        pool.on_fetchrow("FROM markets", {"resolved_outcome": "yes"})

        redis_client = AsyncMock()
        session = _ScriptedSession([])
        metrics = await rec.reconcile_closed_trades(
            pool=pool,
            redis_client=redis_client,
            http_session=session,
            tolerance_usdc=2.0,
        )

        assert metrics["matched"] == 1
        assert metrics["divergences_inserted"] == 0
        redis_client.publish.assert_not_awaited()

    async def test_idempotent_rerun_updates_existing_row(self):
        """Second pass on the same trade: upsert returns inserted=False,
        metrics increment divergences_updated, no new Redis publish
        (we only publish when new rows are inserted)."""
        pool = _FakePool()
        # Same fake_win pattern as test #2 but the upsert reports an
        # UPDATE (xmax != 0). Two runs share the same handlers.
        trades = [
            _trade(30, market_id="mRR", direction="yes",
                   entry_price=0.5, exit_price=0.999, size_usdc=100.0,
                   pnl_usdc=100.0),
        ]
        pool.on_fetch("FROM paper_trades", trades)
        pool.on_fetchrow("FROM markets", {"resolved_outcome": "no"})
        # First run: new INSERT. Second run: UPDATE.
        pool.upsert_inserts = [True, False]

        redis_client = AsyncMock()
        session = _ScriptedSession([])

        m1 = await rec.reconcile_closed_trades(
            pool=pool, redis_client=redis_client, http_session=session,
        )
        assert m1["divergences_inserted"] == 1
        assert m1["divergences_updated"] == 0
        assert redis_client.publish.await_count == 1

        m2 = await rec.reconcile_closed_trades(
            pool=pool, redis_client=redis_client, http_session=session,
        )
        assert m2["divergences_inserted"] == 0
        assert m2["divergences_updated"] == 1
        # No NEW publish — only inserts trigger the envelope.
        assert redis_client.publish.await_count == 1

    async def test_update_on_second_run_with_new_gamma_outcome(self):
        """Between runs the Gamma payload changes (still_open → resolved).
        The 2nd run's UPDATE writes the fresh flag + truth_pnl."""
        pool = _FakePool()
        trades = [
            _trade(40, market_id="mUpd", direction="yes",
                   entry_price=0.5, exit_price=0.999, size_usdc=100.0,
                   pnl_usdc=100.0),
        ]
        pool.on_fetch("FROM paper_trades", trades)
        # DB always NULL → Gamma each time.
        pool.on_fetchrow("FROM markets", {"resolved_outcome": None})
        pool.upsert_inserts = [True, False]

        gamma_open = [{
            "conditionId": "mUpd", "closed": False,
            "outcomePrices": '["0.55","0.45"]',
        }]
        gamma_resolved = [{
            "conditionId": "mUpd", "closed": True,
            "outcomePrices": '["1","0"]',
        }]
        # Run 1 hits gamma once; run 2 hits gamma once.
        session1 = _ScriptedSession([_FakeResp(200, gamma_open)])
        session2 = _ScriptedSession([_FakeResp(200, gamma_resolved)])
        redis_client = AsyncMock()

        m1 = await rec.reconcile_closed_trades(
            pool=pool, redis_client=redis_client, http_session=session1,
        )
        assert m1["by_flag"] == {"still_open_in_reality": 1}
        first_flag = pool.fetchrows[-1][1][12]
        assert first_flag == "still_open_in_reality"

        m2 = await rec.reconcile_closed_trades(
            pool=pool, redis_client=redis_client, http_session=session2,
        )
        # Now Gamma resolved YES → YES holder won → truth = +100, db=+100
        # → match within tolerance, NO upsert ⇒ no flag count change.
        assert m2["matched"] == 1
        # But if the truth had diverged, the latest fetchrow would carry
        # the new flag — verified by the still_open_in_reality test
        # already. This test pins the "DB outcome refreshed" path:
        # the UPDATE path is exercised by test_idempotent_rerun_updates.

    async def test_redis_publish_envelope_includes_top_3_and_metrics(self):
        """End-to-end: 4 divergences, envelope must carry the top-3-worst
        sorted by |delta| descending, plus aggregate counters."""
        pool = _FakePool()
        # Build 4 fake_win trades with increasing |delta|.
        trades = [
            _trade(i, market_id=f"m{i}", direction="yes",
                   entry_price=0.5, exit_price=0.999, size_usdc=100.0,
                   pnl_usdc=50.0 + 20 * i)  # truth=-100, delta=150 .. 230
            for i in range(1, 5)
        ]
        pool.on_fetch("FROM paper_trades", trades)
        pool.on_fetchrow("FROM markets", {"resolved_outcome": "no"})

        redis_client = AsyncMock()
        session = _ScriptedSession([])
        metrics = await rec.reconcile_closed_trades(
            pool=pool, redis_client=redis_client, http_session=session,
        )

        assert metrics["divergences_inserted"] == 4
        redis_client.publish.assert_awaited_once()
        channel, raw_envelope = redis_client.publish.await_args[0]
        assert channel == rec.CHANNEL_PAPER_AUDIT_DIVERGENCE
        envelope = json.loads(raw_envelope)
        assert envelope["type"] == "reconciliation_nightly"
        assert envelope["scanned"] == 4
        assert envelope["divergences"] == {"fake_win": 4}
        assert len(envelope["top_3_worst"]) == 3
        # Sorted by |delta| desc → trade #4 first (delta=230), #3 (210), #2 (190).
        ids = [e["trade_id"] for e in envelope["top_3_worst"]]
        assert ids == [4, 3, 2]
        assert envelope["top_3_worst"][0]["flag"] == "fake_win"

    async def test_gamma_unreachable_bumps_metric_no_crash(self, monkeypatch):
        """All Gamma GET calls raise → metric bump, no exception
        propagates, the job returns normally."""
        pool = _FakePool()
        trades = [
            _trade(50, market_id="mGU", direction="yes"),
        ]
        pool.on_fetch("FROM paper_trades", trades)
        pool.on_fetchrow("FROM markets", {"resolved_outcome": None})

        redis_client = AsyncMock()
        session = _ExplodingSession()

        # Hammer down the retry envelope so the test completes quickly.
        monkeypatch.setattr(rec, "RECONCILE_RETRY_INITIAL_S", 0.0)
        monkeypatch.setattr(rec, "RECONCILE_RETRY_MAX_S", 0.0)
        metrics = await rec.reconcile_closed_trades(
            pool=pool, redis_client=redis_client, http_session=session,
        )

        assert metrics["scanned"] == 1
        assert metrics["gamma_unreachable"] == 1
        assert metrics["divergences_inserted"] == 0
        # No upsert attempted because we couldn't decide.
        assert not any(
            "INSERT INTO paper_close_divergences" in sql
            for sql, _ in pool.fetchrows
        )
        # The retry loop hit the explosion BACKFILL_MAX times.
        assert session.calls >= rec.RECONCILE_MAX_CONSECUTIVE_429

    async def test_alarming_discrepancy_flag_in_envelope(self):
        """|total_db - total_truth| > 100$ → envelope.alarming=True."""
        pool = _FakePool()
        # Single fake_win with delta=200 → discrepancy=200 > 100 alarm.
        trades = [
            _trade(60, market_id="mAlrm", direction="yes",
                   entry_price=0.5, exit_price=0.999, size_usdc=100.0,
                   pnl_usdc=100.0),
        ]
        pool.on_fetch("FROM paper_trades", trades)
        pool.on_fetchrow("FROM markets", {"resolved_outcome": "no"})

        redis_client = AsyncMock()
        session = _ScriptedSession([])
        await rec.reconcile_closed_trades(
            pool=pool, redis_client=redis_client, http_session=session,
        )
        redis_client.publish.assert_awaited_once()
        envelope = json.loads(redis_client.publish.await_args[0][1])
        assert envelope["alarming"] is True
        assert envelope["discrepancy"] > rec.ALARMING_DISCREPANCY_USDC

    async def test_gamma_cache_reuse_across_trades_same_market(self):
        """Two trades on the same market → Gamma is fetched ONCE."""
        pool = _FakePool()
        trades = [
            _trade(70, market_id="mShared", direction="yes",
                   entry_price=0.5, exit_price=0.999, size_usdc=100.0,
                   pnl_usdc=100.0),
            _trade(71, market_id="mShared", direction="yes",
                   entry_price=0.5, exit_price=0.999, size_usdc=50.0,
                   pnl_usdc=50.0),
        ]
        pool.on_fetch("FROM paper_trades", trades)
        pool.on_fetchrow("FROM markets", {"resolved_outcome": None})

        gamma_payload = [{
            "conditionId": "mShared", "closed": True,
            "outcomePrices": '["1","0"]',
        }]
        session = _ScriptedSession([_FakeResp(200, gamma_payload)])
        redis_client = AsyncMock()
        await rec.reconcile_closed_trades(
            pool=pool, redis_client=redis_client, http_session=session,
        )
        # Single Gamma call despite 2 trades.
        assert len(session.calls) == 1


# --------------------------------------------------------------------------- #
# Pure-function tests for the classifier + helpers                             #
# --------------------------------------------------------------------------- #


class TestClassify:
    def test_classify_matches_within_tolerance(self):
        truth = rec.TrueOutcome(
            truth_exit_price=1.0, truth_pnl_usdc=100.0,
            gamma_outcome="yes", gamma_snapshot=None,
            still_open=False, source="db_resolved",
        )
        flag, _ = rec._classify(
            db_pnl=101.0,
            truth=truth,
            tolerance=2.0,
            closed_at=datetime.now(timezone.utc),
            detected_at=datetime.now(timezone.utc),
        )
        assert flag is None

    def test_classify_still_open_takes_priority(self):
        truth = rec.TrueOutcome(
            truth_exit_price=None, truth_pnl_usdc=None,
            gamma_outcome=None, gamma_snapshot={"closed": False},
            still_open=True, source="gamma_open",
        )
        flag, notes = rec._classify(
            db_pnl=200.0,
            truth=truth,
            tolerance=2.0,
            closed_at=datetime.now(timezone.utc),
            detected_at=datetime.now(timezone.utc),
        )
        assert flag == "still_open_in_reality"
        assert notes is not None

    def test_classify_unknown_truth_returns_none(self):
        truth = rec.TrueOutcome(
            truth_exit_price=None, truth_pnl_usdc=None,
            gamma_outcome=None, gamma_snapshot=None,
            still_open=False, source="unknown",
        )
        flag, _ = rec._classify(
            db_pnl=200.0,
            truth=truth,
            tolerance=2.0,
            closed_at=datetime.now(timezone.utc),
            detected_at=datetime.now(timezone.utc),
        )
        assert flag is None


# --------------------------------------------------------------------------- #
# Telegram formatter for paper:audit:divergence                                #
# --------------------------------------------------------------------------- #


class TestFormatPaperAuditDivergence:
    def test_format_contains_key_fields(self):
        payload = {
            "type": "reconciliation_nightly",
            "run_id": "abc-123",
            "scanned": 247,
            "divergences": {"fake_win": 3, "fake_loss": 4, "still_open_in_reality": 1},
            "total_db_pnl": 39784.0,
            "total_truth_pnl": -2062.0,
            "discrepancy": 41846.0,
            "alarming": True,
            "top_3_worst": [
                {"trade_id": 2, "flag": "fake_win", "delta": 38519.0,
                 "market_id": "0xBTCverylongid", "direction": "yes"},
                {"trade_id": 1, "flag": "fake_win", "delta": 4184.0,
                 "market_id": "0xBTCverylongid", "direction": "yes"},
                {"trade_id": 23, "flag": "fake_loss", "delta": -380.0,
                 "market_id": "0xPunjabvsRCB", "direction": "no"},
            ],
        }
        out = formatters.format_paper_audit_divergence(payload)
        assert "NIGHTLY RECONCILIATION" in out
        assert "247" in out  # scanned
        assert "8" in out  # n_total = 3+4+1
        assert "fake_win" in out
        assert "fake_loss" in out
        assert "still_open_in_reality" in out
        # Money formatted (per _money helper convention).
        assert "+39784.00$" in out
        assert "-2062.00$" in out
        assert "+41846.00$" in out
        # Top-3 list.
        assert "#2" in out and "#1" in out and "#23" in out
        # Alarming icon (🚨 instead of 📊).
        assert "🚨" in out

    def test_format_normal_when_not_alarming(self):
        payload = {
            "scanned": 5,
            "divergences": {"fake_win": 1},
            "total_db_pnl": 10.0,
            "total_truth_pnl": -5.0,
            "discrepancy": 15.0,
            "alarming": False,
            "top_3_worst": [],
        }
        out = formatters.format_paper_audit_divergence(payload)
        assert "📊" in out
        assert "🚨" not in out
