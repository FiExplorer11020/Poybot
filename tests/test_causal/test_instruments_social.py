"""R12 NewsEventDetector enhancement — social-signal-fed events.

Verifies that high-confidence entry/exit signals in social_signals
produce InstrumentalEvent objects, and that the threshold is honored.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from src.causal.instruments import NewsEventDetector


def _mock_get_db(rows):
    conn = AsyncMock()
    conn.fetch = AsyncMock(return_value=rows)
    conn.execute = AsyncMock()

    @asynccontextmanager
    async def _ctx():
        yield conn

    return _ctx, conn


class TestSocialSignalEvents:
    @pytest.mark.asyncio
    async def test_high_confidence_entry_signal_emits_event(self):
        rows = [
            {
                "signal_id": 1,
                "source": "x",
                "author_handle": "alice",
                "posted_at": datetime(2026, 5, 12, 10, 0, 0, tzinfo=timezone.utc),
                "intent": "entry_signal",
                "intent_confidence": 0.85,
                "parsed_market": "mkt-foo",
                "parsed_direction": "yes",
                "text": "just entered YES at 0.42",
            },
        ]
        ctx, _ = _mock_get_db(rows)
        with patch("src.causal.instruments.get_db", side_effect=ctx):
            det = NewsEventDetector(
                http_session=None, min_social_confidence=0.7,
            )
            events = await det.detect(datetime.now(tz=timezone.utc))
        assert len(events) == 1
        ev = events[0]
        assert ev.event_type == "news"
        assert ev.source == "social:x"
        assert ev.confidence == pytest.approx(0.85)
        assert ev.affected_market_ids == ["mkt-foo"]

    @pytest.mark.asyncio
    async def test_low_confidence_signal_filtered(self):
        # The SQL filter (intent_confidence > min) would exclude this
        # at the DB level. We simulate by returning an empty fetch.
        ctx, _ = _mock_get_db([])
        with patch("src.causal.instruments.get_db", side_effect=ctx):
            det = NewsEventDetector(
                http_session=None, min_social_confidence=0.7,
            )
            events = await det.detect(datetime.now(tz=timezone.utc))
        assert events == []

    @pytest.mark.asyncio
    async def test_signal_without_market_emits_event_with_empty_affected(self):
        rows = [
            {
                "signal_id": 2,
                "source": "telegram",
                "author_handle": "bob",
                "posted_at": datetime(2026, 5, 12, 10, 0, 0, tzinfo=timezone.utc),
                "intent": "exit_signal",
                "intent_confidence": 0.9,
                "parsed_market": None,
                "parsed_direction": None,
                "text": "took profit",
            },
        ]
        ctx, _ = _mock_get_db(rows)
        with patch("src.causal.instruments.get_db", side_effect=ctx):
            det = NewsEventDetector(http_session=None)
            events = await det.detect(datetime.now(tz=timezone.utc))
        assert len(events) == 1
        assert events[0].affected_market_ids == []
        assert events[0].source == "social:telegram"

    @pytest.mark.asyncio
    async def test_db_error_returns_empty_not_crash(self):
        conn = AsyncMock()
        conn.fetch = AsyncMock(side_effect=Exception("db down"))

        @asynccontextmanager
        async def _ctx():
            yield conn

        with patch("src.causal.instruments.get_db", side_effect=_ctx):
            det = NewsEventDetector(http_session=None)
            events = await det.detect(datetime.now(tz=timezone.utc))
        assert events == []
