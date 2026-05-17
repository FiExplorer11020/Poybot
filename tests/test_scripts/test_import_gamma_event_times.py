"""Unit tests for ``scripts/import_gamma_event_times_2026_05_17.py``.

Pure-Python — every external dependency (asyncpg, aiohttp, Gamma) is
stubbed so the suite never reaches a real network or DB. Each test
pins one piece of the contract spelled out in Tier 1 fix #1
(``docs/autonomous_session_2026_05_17_strategy/02_STRUCTURAL_FIX_PLAN.md``):

  1. Gamma response parsing for every field-shape variant we've seen
     in the wild (top-level gameStartTime, space-vs-T separator,
     +00-vs-+00:00 offsets, events[0].startDate fallback, futures
     row with NULL game time).
  2. is_live_match computation (within ±2h, past, future).
  3. event_end_time projection logic.
  4. Idempotency: a second run on the same row UPDATEs but the result
     converges to the same final state.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import AsyncMock

import pytest

from scripts import import_gamma_event_times_2026_05_17 as ge


# --------------------------------------------------------------------------- #
# asyncpg / aiohttp / Gamma stubs                                              #
# --------------------------------------------------------------------------- #


class _FakeConn:
    def __init__(self, parent: "_FakePool") -> None:
        self._parent = parent

    async def execute(self, sql: str, *args: Any) -> str:
        return self._parent._on_execute(sql, args)

    async def fetch(self, sql: str, *args: Any) -> list[Any]:
        return self._parent._on_fetch(sql, args)


class _FakePool:
    def __init__(self) -> None:
        self.executed: list[tuple[str, tuple]] = []
        self.fetch_calls: list[tuple[str, tuple]] = []
        self.execute_handlers: list[tuple[str, Any]] = []
        self.fetch_handlers: list[tuple[str, Any]] = []

    def acquire(self) -> Any:
        @asynccontextmanager
        async def _ctx():
            yield _FakeConn(self)
        return _ctx()

    def on_execute(self, substr: str, response: Any) -> None:
        self.execute_handlers.append((substr, response))

    def on_fetch(self, substr: str, response: Any) -> None:
        self.fetch_handlers.append((substr, response))

    def _on_execute(self, sql: str, args: tuple) -> str:
        self.executed.append((sql, args))
        return _resolve(self.execute_handlers, sql, args, default="UPDATE 0")

    def _on_fetch(self, sql: str, args: tuple) -> list[Any]:
        self.fetch_calls.append((sql, args))
        return _resolve(self.fetch_handlers, sql, args, default=[])


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


def _patch_fetch(monkeypatch, payloads_by_market_id: dict[str, dict | None]):
    """Replace fetch_gamma_for_market with a static map.

    A value of None in the map simulates a Gamma miss (HTTP failure or
    market absent from Gamma) — the orchestrator should increment
    gamma_misses and skip the UPDATE.
    """
    calls: list[str] = []

    async def _fake(session, condition_id):
        calls.append(condition_id)
        payload = payloads_by_market_id.get(condition_id)
        if payload is None:
            return None
        return ge.GammaMarket.model_validate(payload)

    monkeypatch.setattr(ge, "fetch_gamma_for_market", _fake)
    return calls


# --------------------------------------------------------------------------- #
# 1. Gamma response parsing — field shape variants                             #
# --------------------------------------------------------------------------- #


class TestParseGammaPayload:
    def test_top_level_gameStartTime_space_separator(self):
        """The wild-shape we verified live for cricket / esports markets.
        Format: 'YYYY-MM-DD HH:MM:SS+00' (note: space, no colon in tz)."""
        market = ge.GammaMarket(gameStartTime="2026-05-17 10:00:00+00")
        ts, source = ge.extract_event_start(market)
        assert source == "gamma:gameStartTime"
        assert ts == datetime(2026, 5, 17, 10, 0, 0, tzinfo=timezone.utc)

    def test_top_level_gameStartTime_iso_T_separator(self):
        """Alternate shape Gamma occasionally returns (standard ISO)."""
        market = ge.GammaMarket(gameStartTime="2026-05-17T10:00:00Z")
        ts, source = ge.extract_event_start(market)
        assert source == "gamma:gameStartTime"
        assert ts == datetime(2026, 5, 17, 10, 0, 0, tzinfo=timezone.utc)

    def test_top_level_gameStartTime_full_offset(self):
        market = ge.GammaMarket(gameStartTime="2026-05-17 10:30:00+05:30")
        ts, source = ge.extract_event_start(market)
        assert source == "gamma:gameStartTime"
        assert ts == datetime(2026, 5, 17, 10, 30, 0,
                              tzinfo=timezone(timedelta(hours=5, minutes=30)))

    def test_events_startDate_fallback_when_gameStartTime_null(self):
        """A sport-row variant where Gamma populated the event but not
        the top-level field. We trust events[0].startDate ONLY if the
        event window is < 14 days (real match, not a futures series)."""
        market = ge.GammaMarket(
            gameStartTime=None,
            events=[
                ge.GammaEvent(
                    startDate="2026-05-20T15:00:00Z",
                    endDate="2026-05-21T15:00:00Z",
                )
            ],
        )
        ts, source = ge.extract_event_start(market)
        assert source == "gamma:event.startDate"
        assert ts == datetime(2026, 5, 20, 15, 0, 0, tzinfo=timezone.utc)

    def test_futures_event_returns_none_not_falsy_event_start(self):
        """The Stanley Cup pattern: event window spans an entire season
        (June 2025 → June 2026). gameStartTime is NULL because there's
        no single 'game'. We must NOT treat the season start as the
        event start — those markets aren't live matches."""
        market = ge.GammaMarket(
            gameStartTime=None,
            events=[
                ge.GammaEvent(
                    startDate="2025-06-23T16:00:00Z",
                    endDate="2026-06-30T00:00:00Z",  # 372d window — futures
                )
            ],
        )
        ts, source = ge.extract_event_start(market)
        assert source == "gamma:absent"
        assert ts is None

    def test_completely_empty_payload_returns_absent(self):
        market = ge.GammaMarket()
        ts, source = ge.extract_event_start(market)
        assert source == "gamma:absent"
        assert ts is None

    def test_malformed_gameStartTime_does_not_crash(self):
        market = ge.GammaMarket(gameStartTime="not-a-timestamp")
        ts, source = ge.extract_event_start(market)
        assert source == "gamma:absent"
        assert ts is None

    def test_iso_parser_handles_z_suffix_and_naive_strings(self):
        # 'Z' suffix → UTC
        assert ge._parse_iso_ts("2026-05-17T10:00:00Z") == \
            datetime(2026, 5, 17, 10, 0, 0, tzinfo=timezone.utc)
        # Already a datetime (naive) → stamped UTC
        naive = datetime(2026, 5, 17, 10, 0, 0)
        assert ge._parse_iso_ts(naive).tzinfo == timezone.utc
        # None / empty → None (not a crash)
        assert ge._parse_iso_ts(None) is None
        assert ge._parse_iso_ts("") is None
        assert ge._parse_iso_ts("   ") is None


# --------------------------------------------------------------------------- #
# 2. is_live_match computation                                                 #
# --------------------------------------------------------------------------- #


class TestIsLiveMatch:
    NOW = datetime(2026, 5, 17, 11, 0, 0, tzinfo=timezone.utc)

    def test_event_starting_exactly_now_is_live(self):
        assert ge.compute_is_live_match(self.NOW, self.NOW) is True

    def test_event_started_90min_ago_is_live(self):
        past = self.NOW - timedelta(minutes=90)
        assert ge.compute_is_live_match(past, self.NOW) is True

    def test_event_starting_90min_from_now_is_live(self):
        # Symmetric window — pre-match also flips True so the bot
        # doesn't try to FOLLOW a leader scalping into kickoff.
        future = self.NOW + timedelta(minutes=90)
        assert ge.compute_is_live_match(future, self.NOW) is True

    def test_event_at_exactly_2h_boundary_is_live(self):
        # ±2h is inclusive — `delta == window` flips True.
        boundary_past = self.NOW - timedelta(hours=2)
        boundary_future = self.NOW + timedelta(hours=2)
        assert ge.compute_is_live_match(boundary_past, self.NOW) is True
        assert ge.compute_is_live_match(boundary_future, self.NOW) is True

    def test_event_3h_in_past_is_not_live(self):
        past = self.NOW - timedelta(hours=3)
        assert ge.compute_is_live_match(past, self.NOW) is False

    def test_event_3h_in_future_is_not_live(self):
        future = self.NOW + timedelta(hours=3)
        assert ge.compute_is_live_match(future, self.NOW) is False

    def test_event_7_days_away_is_not_live(self):
        # The Stanley Cup case: the season is "live" in some sense but
        # the next match is days away — must not flip is_live_match.
        far_future = self.NOW + timedelta(days=7)
        assert ge.compute_is_live_match(far_future, self.NOW) is False

    def test_null_event_start_returns_false(self):
        # Long-dated futures with NULL event_start — safe default is
        # False (the confidence engine treats unknown as non-live).
        assert ge.compute_is_live_match(None, self.NOW) is False


# --------------------------------------------------------------------------- #
# 3. event_end_time projection                                                 #
# --------------------------------------------------------------------------- #


class TestComputeEventEnd:
    def test_returns_none_when_event_start_is_none(self):
        market = ge.GammaMarket()
        assert ge.compute_event_end(None, market) is None

    def test_falls_back_to_event_start_plus_4h_when_no_event(self):
        start = datetime(2026, 5, 17, 10, 0, 0, tzinfo=timezone.utc)
        market = ge.GammaMarket()  # no events list
        assert ge.compute_event_end(start, market) == \
            start + timedelta(hours=4)

    def test_uses_tighter_of_gamma_end_or_4h_projection(self):
        """Gamma sometimes gives a 24h window for a 3h match — we want
        the tighter projection (+4h) so the resolution wall is realistic.
        But when Gamma gives a 2h window for a 2h soccer match, we
        respect Gamma's tighter end."""
        start = datetime(2026, 5, 17, 10, 0, 0, tzinfo=timezone.utc)
        # Case 1: Gamma reports a generous 12h window → tighten to +4h.
        market = ge.GammaMarket(events=[
            ge.GammaEvent(
                startDate="2026-05-17T10:00:00Z",
                endDate="2026-05-17T22:00:00Z",
            )
        ])
        assert ge.compute_event_end(start, market) == \
            start + timedelta(hours=4)
        # Case 2: Gamma reports a tight 2h window → respect Gamma.
        market_tight = ge.GammaMarket(events=[
            ge.GammaEvent(
                startDate="2026-05-17T10:00:00Z",
                endDate="2026-05-17T12:00:00Z",
            )
        ])
        assert ge.compute_event_end(start, market_tight) == \
            datetime(2026, 5, 17, 12, 0, 0, tzinfo=timezone.utc)

    def test_futures_event_end_ignored_when_window_too_wide(self):
        """A futures market's events[0].endDate is months away — must
        not be used (we'd project a resolution wall 6 months out)."""
        start = datetime(2025, 6, 23, 16, 0, 0, tzinfo=timezone.utc)
        market = ge.GammaMarket(events=[
            ge.GammaEvent(
                startDate="2025-06-23T16:00:00Z",
                endDate="2026-06-30T00:00:00Z",  # 1y+ window
            )
        ])
        # 1y window > 14d sentinel → fall back to +4h projection.
        assert ge.compute_event_end(start, market) == \
            start + timedelta(hours=4)


# --------------------------------------------------------------------------- #
# 4. Orchestrator + idempotency                                                #
# --------------------------------------------------------------------------- #


class TestOrchestrator:
    NOW = datetime(2026, 5, 17, 11, 0, 0, tzinfo=timezone.utc)

    async def test_dry_run_does_not_write_but_counts(self, monkeypatch):
        pool = _FakePool()
        pool.on_fetch("FROM markets", [
            {"market_id": "0xIPL", "question": "IPL", "end_date": None},
        ])
        _patch_fetch(monkeypatch, {
            "0xIPL": {
                "conditionId": "0xIPL",
                "gameStartTime": "2026-05-17 10:00:00+00",  # within 2h of NOW
                "events": [],
            },
        })
        summary = await ge.run_import(
            pool=pool,
            session=AsyncMock(),
            dry_run=True,
            now=self.NOW,
        )
        assert summary.markets_scanned == 1
        assert summary.rows_populated == 1
        assert summary.live_match_count == 1
        # Critical: dry-run path NEVER executes any UPDATE.
        assert pool.executed == []

    async def test_writes_for_each_target_market(self, monkeypatch):
        pool = _FakePool()
        pool.on_fetch("FROM markets", [
            {"market_id": "0xIPL", "question": "IPL match", "end_date": None},
            {"market_id": "0xNHL", "question": "Stanley Cup", "end_date": None},
        ])
        pool.on_execute("UPDATE markets", "UPDATE 1")
        _patch_fetch(monkeypatch, {
            # Live match — should populate is_live_match=True
            "0xIPL": {
                "conditionId": "0xIPL",
                "gameStartTime": "2026-05-17 10:00:00+00",
                "events": [],
            },
            # Futures — should populate with NULL event_start and is_live=False
            "0xNHL": {
                "conditionId": "0xNHL",
                "gameStartTime": None,
                "events": [{
                    "startDate": "2025-06-23T16:00:00Z",
                    "endDate": "2026-06-30T00:00:00Z",  # 1y+ window → futures
                }],
            },
        })
        summary = await ge.run_import(
            pool=pool, session=AsyncMock(), dry_run=False, now=self.NOW,
        )
        assert summary.markets_scanned == 2
        assert summary.rows_populated == 2
        # Only the IPL match is live
        assert summary.live_match_count == 1
        # Two UPDATEs fired (one per market) plus the SELECT
        update_count = sum(
            1 for sql, _ in pool.executed if "UPDATE markets" in sql
        )
        assert update_count == 2
        # Source breakdown: 1 from gameStartTime, 1 absent (futures)
        assert summary.source_breakdown.get("gamma:gameStartTime") == 1
        assert summary.source_breakdown.get("gamma:absent") == 1

    async def test_gamma_miss_does_not_crash_or_block_other_markets(
        self, monkeypatch,
    ):
        pool = _FakePool()
        pool.on_fetch("FROM markets", [
            {"market_id": "0xMISS", "question": "?", "end_date": None},
            {"market_id": "0xOK", "question": "Cricket", "end_date": None},
        ])
        pool.on_execute("UPDATE markets", "UPDATE 1")
        _patch_fetch(monkeypatch, {
            "0xMISS": None,  # Simulates Gamma 500 / network timeout
            "0xOK": {
                "conditionId": "0xOK",
                "gameStartTime": "2026-05-17 10:00:00+00",
                "events": [],
            },
        })
        summary = await ge.run_import(
            pool=pool, session=AsyncMock(), dry_run=False, now=self.NOW,
        )
        assert summary.markets_scanned == 2
        assert summary.gamma_misses == 1
        # The OK market still got enriched despite the sibling miss.
        assert summary.rows_populated == 1
        assert summary.live_match_count == 1

    async def test_idempotent_second_run_produces_same_final_state(
        self, monkeypatch,
    ):
        """A second run on the same target set must produce identical
        counters. The UPDATE is unconditional (always overwrites
        is_live_match because it's a wall-clock derivative), so the
        idempotency contract is 'final state converges', not 'no
        write happens'.
        """
        pool_first = _FakePool()
        pool_second = _FakePool()
        for pool in (pool_first, pool_second):
            pool.on_fetch("FROM markets", [
                {"market_id": "0xIPL", "question": "IPL", "end_date": None},
            ])
            pool.on_execute("UPDATE markets", "UPDATE 1")
        gamma_response = {
            "0xIPL": {
                "conditionId": "0xIPL",
                "gameStartTime": "2026-05-17 10:00:00+00",
                "events": [],
            },
        }
        _patch_fetch(monkeypatch, gamma_response)

        s1 = await ge.run_import(
            pool=pool_first, session=AsyncMock(),
            dry_run=False, now=self.NOW,
        )
        s2 = await ge.run_import(
            pool=pool_second, session=AsyncMock(),
            dry_run=False, now=self.NOW,
        )
        # Same counters, same source breakdown, same live count.
        assert s1.as_dict()["rows_populated"] == s2.as_dict()["rows_populated"]
        assert s1.as_dict()["live_match_count"] == s2.as_dict()["live_match_count"]
        assert s1.as_dict()["source_breakdown"] == s2.as_dict()["source_breakdown"]

    async def test_is_live_recomputes_with_wall_clock(self, monkeypatch):
        """The same gamma payload should yield different is_live_match
        values depending on the `now` argument. This is THE point of
        the 30-min refresh job — a match that was live an hour ago
        isn't anymore."""
        pool = _FakePool()
        pool.on_fetch("FROM markets", [
            {"market_id": "0xM", "question": "match", "end_date": None},
        ])
        pool.on_execute("UPDATE markets", "UPDATE 1")
        _patch_fetch(monkeypatch, {
            "0xM": {
                "conditionId": "0xM",
                "gameStartTime": "2026-05-17 10:00:00+00",
                "events": [],
            },
        })

        # Wall-clock 11:00 → match started 1h ago → live
        s_live = await ge.run_import(
            pool=pool, session=AsyncMock(), dry_run=False,
            now=datetime(2026, 5, 17, 11, 0, 0, tzinfo=timezone.utc),
        )
        # Wall-clock 18:00 → match was 8h ago → not live anymore
        pool2 = _FakePool()
        pool2.on_fetch("FROM markets", [
            {"market_id": "0xM", "question": "match", "end_date": None},
        ])
        pool2.on_execute("UPDATE markets", "UPDATE 1")
        s_done = await ge.run_import(
            pool=pool2, session=AsyncMock(), dry_run=False,
            now=datetime(2026, 5, 17, 18, 0, 0, tzinfo=timezone.utc),
        )
        assert s_live.live_match_count == 1
        assert s_done.live_match_count == 0


# --------------------------------------------------------------------------- #
# 5. UPDATE SQL contract                                                       #
# --------------------------------------------------------------------------- #


class TestUpdateEventTimes:
    async def test_update_passes_all_5_params_in_order(self):
        """Defensive — the column order in the UPDATE is load-bearing
        for the asyncpg positional arguments. If someone re-orders
        the SET clause without re-ordering the bind args, every row
        gets corrupted (event_end_time becomes a boolean etc.)."""
        pool = _FakePool()
        pool.on_execute("UPDATE markets", "UPDATE 1")
        start = datetime(2026, 5, 17, 10, 0, 0, tzinfo=timezone.utc)
        end = start + timedelta(hours=4)
        async with pool.acquire() as conn:
            ok = await ge.update_event_times(
                conn,
                market_id="0xABC",
                event_start=start,
                event_end=end,
                is_live=True,
                source="gamma:gameStartTime",
            )
        assert ok is True
        # Inspect the bind args of the UPDATE call
        update_calls = [c for c in pool.executed if "UPDATE markets" in c[0]]
        assert len(update_calls) == 1
        _, args = update_calls[0]
        assert args == ("0xABC", start, end, True, "gamma:gameStartTime")


# --------------------------------------------------------------------------- #
# 6. Live-cohort acceptance: IPL Punjab vs Bengaluru test case                 #
# --------------------------------------------------------------------------- #


class TestIPLPunjabAcceptanceCase:
    """Pinned regression: the exact market that lost -97% on 2026-05-17.

    Gamma row (verified live 2026-05-17 via curl):
        question         "Indian Premier League: Punjab Kings vs RCB"
        conditionId      0x74ee3860...
        gameStartTime    "2026-05-17 10:00:00+00"   ← THE TRUTH
        endDate          "2026-05-24T12:00:00Z"     ← +169h, dispute window
        events[0].endDate "2026-05-24T12:00:00Z"

    Acceptance: at NOW = 2026-05-17 11:15 UTC the bot should flag
    this as is_live_match=True, NOT pass the 6h MIN_HOURS_TO_RESOLUTION
    gate based on end_date.
    """

    async def test_ipl_market_flagged_live_at_11_15_utc(self, monkeypatch):
        pool = _FakePool()
        pool.on_fetch("FROM markets", [{
            "market_id": "0x74ee3860",
            "question": "Indian Premier League: Punjab Kings vs RCB",
            "end_date": datetime(2026, 5, 24, 12, 0, 0, tzinfo=timezone.utc),
        }])
        pool.on_execute("UPDATE markets", "UPDATE 1")
        _patch_fetch(monkeypatch, {
            "0x74ee3860": {
                "conditionId": "0x74ee3860",
                "gameStartTime": "2026-05-17 10:00:00+00",
                "endDate": "2026-05-24T12:00:00Z",
                "events": [{
                    "startDate": "2026-05-14T15:26:49Z",
                    "endDate": "2026-05-24T12:00:00Z",
                }],
            },
        })
        wall_clock = datetime(2026, 5, 17, 11, 15, 0, tzinfo=timezone.utc)
        summary = await ge.run_import(
            pool=pool, session=AsyncMock(),
            dry_run=False, now=wall_clock,
        )
        # The market was correctly flagged as live (the bug fix).
        assert summary.live_match_count == 1
        # And we explicitly used gameStartTime (not the events.startDate
        # fallback, not absent).
        assert summary.source_breakdown.get("gamma:gameStartTime") == 1
        # UPDATE call wrote is_live_match=True (4th positional arg).
        update_calls = [c for c in pool.executed if "UPDATE markets" in c[0]]
        assert len(update_calls) == 1
        _, args = update_calls[0]
        assert args[3] is True  # is_live_match
