"""Unit tests for ``src.control.price_oracle.PriceOracle``.

Pillar 1 (audit 2026-05-17). The PriceOracle is the canonical source for
close-time exit prices. These tests pin the cascade
``fresh_book → gamma → resolved → fail`` and the raw-snapshot contract
that feeds Pillar 5's close_audit_log.
"""

from __future__ import annotations

import json
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.control.price_oracle import (
    GAMMA_MAX_TRADE_AGE_S,
    PriceOracle,
    PriceQuote,
    _gamma_last_trade_age_s,
    _gamma_last_trade_price,
    _parse_epoch,
)


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #


def _book_payload(*, best_bid: float, best_ask: float, age_s: float) -> str:
    captured_at = datetime.now(tz=timezone.utc) - timedelta(seconds=age_s)
    return json.dumps(
        {
            "best_bid": best_bid,
            "best_ask": best_ask,
            "captured_at": captured_at.isoformat(),
            "source": "ws",
        }
    )


def _make_redis(book_payload: str | None = None) -> AsyncMock:
    redis = AsyncMock()
    redis.get = AsyncMock(return_value=book_payload)
    return redis


def _db_returning(row: dict | None):
    """Build an `asynccontextmanager` that yields a conn whose fetchrow
    returns the given mapping (or None)."""

    @asynccontextmanager
    async def _db():
        conn = AsyncMock()
        conn.fetchrow = AsyncMock(return_value=row)
        yield conn

    return _db


# --------------------------------------------------------------------------- #
# Step 1: fresh_book                                                          #
# --------------------------------------------------------------------------- #


class TestFreshBookCascade:
    @pytest.mark.asyncio
    async def test_fresh_book_returns_mid(self):
        """Fresh narrow-spread book → source='book', price=mid, raw_book populated."""
        redis = _make_redis(_book_payload(best_bid=0.45, best_ask=0.47, age_s=5))
        oracle = PriceOracle(redis_client=redis)

        quote = await oracle.get_close_price("m", "tok-A", "yes")

        assert quote.source == "book"
        assert quote.price == pytest.approx(0.46)
        assert quote.spread_pct is not None
        assert quote.spread_pct < 0.10
        assert quote.raw_book is not None
        assert quote.raw_book["best_bid"] == 0.45
        assert quote.raw_book["best_ask"] == 0.47
        assert quote.raw_gamma is None
        assert quote.raw_resolution is None

    @pytest.mark.asyncio
    async def test_stale_book_falls_through_to_resolved(self):
        """Book older than 30s → step 1 returns None. With Gamma absent
        (we patch the gamma helper to None) we land on the resolved step.
        """
        # 60s old, beyond the 30s ceiling.
        redis = _make_redis(_book_payload(best_bid=0.45, best_ask=0.47, age_s=60))
        oracle = PriceOracle(redis_client=redis)
        oracle._try_gamma_last_trade = AsyncMock(return_value=None)

        with patch(
            "src.control.price_oracle.get_db",
            _db_returning(
                {
                    "token_yes": "tok-A",
                    "token_no": "tok-B",
                    "resolved_outcome": "yes",
                    "end_date": None,
                }
            ),
        ):
            quote = await oracle.get_close_price("m", "tok-A", "yes")
        assert quote.source == "resolved"
        assert quote.price == 1.0

    @pytest.mark.asyncio
    async def test_wide_spread_book_rejected(self):
        """Spread > 30% → book step returns None and we fall through."""
        redis = _make_redis(_book_payload(best_bid=0.01, best_ask=0.99, age_s=5))
        oracle = PriceOracle(redis_client=redis)
        oracle._try_gamma_last_trade = AsyncMock(return_value=None)
        with patch(
            "src.control.price_oracle.get_db",
            _db_returning(None),  # market not in DB
        ):
            quote = await oracle.get_close_price("m", "tok-A", "yes")
        assert quote.source == "fail"
        assert quote.price is None


# --------------------------------------------------------------------------- #
# Step 2: Gamma last_trade_price                                              #
# --------------------------------------------------------------------------- #


class TestGammaCascade:
    @pytest.mark.asyncio
    async def test_gamma_fresh_trade_returns_quote(self):
        """Book stale → Gamma reports a recent trade → source='gamma'."""
        redis = _make_redis(None)  # book miss
        oracle = PriceOracle(redis_client=redis)
        now = time.time()
        gamma_payload = {
            "conditionId": "0xabc",
            "outcomePrices": "[0.62, 0.38]",
            "last_trade_time": now - 30.0,  # 30s old
            "token_yes": "tok-A",
        }
        oracle._fetch_gamma_market = AsyncMock(return_value=gamma_payload)

        quote = await oracle.get_close_price("m", "tok-A", "yes")
        assert quote.source == "gamma"
        assert quote.price == pytest.approx(0.62)
        assert quote.raw_gamma is not None
        assert quote.raw_gamma["last_trade_price"] == pytest.approx(0.62)
        assert quote.raw_gamma["condition_id"] == "0xabc"

    @pytest.mark.asyncio
    async def test_gamma_old_trade_falls_through(self):
        """last_trade_time older than 5 min → skip to resolved."""
        redis = _make_redis(None)
        oracle = PriceOracle(redis_client=redis)
        old_ts = time.time() - (GAMMA_MAX_TRADE_AGE_S + 100.0)
        oracle._fetch_gamma_market = AsyncMock(
            return_value={
                "conditionId": "0xabc",
                "outcomePrices": "[0.62, 0.38]",
                "last_trade_time": old_ts,
            }
        )
        with patch(
            "src.control.price_oracle.get_db",
            _db_returning(
                {
                    "token_yes": "tok-A",
                    "token_no": "tok-B",
                    "resolved_outcome": "no",
                    "end_date": None,
                }
            ),
        ):
            quote = await oracle.get_close_price("m", "tok-A", "yes")
        # Old Gamma trade → fall to resolved. resolved_outcome=NO,
        # held token = YES → loss → 0.0.
        assert quote.source == "resolved"
        assert quote.price == 0.0

    @pytest.mark.asyncio
    async def test_gamma_cache_hit_skips_http(self):
        """A second call inside the TTL window must NOT re-call _fetch_gamma."""
        redis = _make_redis(None)
        oracle = PriceOracle(redis_client=redis, gamma_cache_ttl_s=60.0)
        now = time.time()
        payload = {
            "conditionId": "0xabc",
            "outcomePrices": "[0.55, 0.45]",
            "last_trade_time": now - 10.0,
        }
        # First call populates the cache via the wrapped private fetch;
        # for this test we drive the cache directly so we can count
        # HTTP calls via the session mock.
        session = AsyncMock()
        # Default session.get returns 200 + payload. asyncio context
        # manager dance.
        resp = AsyncMock()
        resp.status = 200
        resp.json = AsyncMock(return_value=[payload])

        @asynccontextmanager
        async def _ctx(*_a, **_kw):
            yield resp

        session.get = MagicMock(side_effect=_ctx)
        session.closed = False
        oracle._http_session = session
        oracle._http_session_owned = False

        q1 = await oracle.get_close_price("m", "tok-A", "yes")
        q2 = await oracle.get_close_price("m", "tok-A", "yes")
        assert q1.source == "gamma"
        assert q2.source == "gamma"
        # Single HTTP call across the two oracle invocations.
        assert session.get.call_count == 1

    @pytest.mark.asyncio
    async def test_gamma_http_error_falls_through_to_resolved(self):
        """A 500/timeout/etc from Gamma must NOT abort the cascade."""
        redis = _make_redis(None)
        oracle = PriceOracle(redis_client=redis)
        # _fetch_gamma_market returns None on any error (the helper
        # catches and logs).
        oracle._fetch_gamma_market = AsyncMock(return_value=None)
        with patch(
            "src.control.price_oracle.get_db",
            _db_returning(
                {
                    "token_yes": "tok-A",
                    "token_no": "tok-B",
                    "resolved_outcome": "yes",
                    "end_date": None,
                }
            ),
        ):
            quote = await oracle.get_close_price("m", "tok-A", "yes")
        assert quote.source == "resolved"
        assert quote.price == 1.0


# --------------------------------------------------------------------------- #
# Step 3: markets.resolved_outcome                                            #
# --------------------------------------------------------------------------- #


class TestResolvedCascade:
    @pytest.mark.parametrize(
        "outcome,direction,held_token,token_yes,token_no,expected",
        [
            # YES outcome, held YES → 1.0
            ("yes", "yes", "tY", "tY", "tN", 1.0),
            # YES outcome, held NO → 0.0 (FADE bought the NO token)
            ("yes", "no", "tN", "tY", "tN", 0.0),
            # NO outcome, held YES → 0.0
            ("no", "yes", "tY", "tY", "tN", 0.0),
            # NO outcome, held NO → 1.0
            ("no", "no", "tN", "tY", "tN", 1.0),
        ],
    )
    @pytest.mark.asyncio
    async def test_resolved_outcome_maps_correctly(
        self, outcome, direction, held_token, token_yes, token_no, expected
    ):
        redis = _make_redis(None)
        oracle = PriceOracle(redis_client=redis)
        oracle._try_fresh_book = AsyncMock(return_value=None)
        oracle._try_gamma_last_trade = AsyncMock(return_value=None)

        with patch(
            "src.control.price_oracle.get_db",
            _db_returning(
                {
                    "token_yes": token_yes,
                    "token_no": token_no,
                    "resolved_outcome": outcome,
                    "end_date": None,
                }
            ),
        ):
            quote = await oracle.get_close_price("m", held_token, direction)

        assert quote.source == "resolved"
        assert quote.price == expected
        assert quote.raw_resolution is not None
        assert quote.raw_resolution["resolved_outcome"] == outcome
        assert quote.raw_resolution["held_token"] == held_token

    @pytest.mark.asyncio
    async def test_resolved_outcome_null_falls_to_fail(self):
        redis = _make_redis(None)
        oracle = PriceOracle(redis_client=redis)
        oracle._try_fresh_book = AsyncMock(return_value=None)
        oracle._try_gamma_last_trade = AsyncMock(return_value=None)
        with patch(
            "src.control.price_oracle.get_db",
            _db_returning(
                {
                    "token_yes": "tY",
                    "token_no": "tN",
                    "resolved_outcome": None,
                    "end_date": None,
                }
            ),
        ):
            quote = await oracle.get_close_price("m", "tY", "yes")
        assert quote.source == "fail"
        assert quote.price is None


# --------------------------------------------------------------------------- #
# Step 4: explicit failure                                                    #
# --------------------------------------------------------------------------- #


class TestFailureSemantics:
    @pytest.mark.asyncio
    async def test_no_source_returns_fail_quote(self):
        """Book miss + Gamma miss + DB miss → source='fail', price=None."""
        redis = _make_redis(None)
        oracle = PriceOracle(redis_client=redis)
        oracle._try_gamma_last_trade = AsyncMock(return_value=None)
        with patch(
            "src.control.price_oracle.get_db",
            _db_returning(None),
        ):
            quote = await oracle.get_close_price("m", "tok-A", "yes")
        assert quote.source == "fail"
        assert quote.price is None
        # raw_* are all None on fail — there is no evidence to snapshot.
        assert quote.raw_book is None
        assert quote.raw_gamma is None
        assert quote.raw_resolution is None


# --------------------------------------------------------------------------- #
# prefer_resolved flag                                                        #
# --------------------------------------------------------------------------- #


class TestPreferResolvedFlag:
    @pytest.mark.asyncio
    async def test_prefer_resolved_skips_book_and_gamma(self):
        """With prefer_resolved=True we MUST go straight to the DB."""
        # Build a fresh book that would normally win the cascade.
        redis = _make_redis(_book_payload(best_bid=0.40, best_ask=0.42, age_s=2))
        oracle = PriceOracle(redis_client=redis)
        gamma_spy = AsyncMock(return_value=None)
        book_spy = AsyncMock(return_value=PriceQuote(
            price=0.41, source="book", observed_ts=time.time()
        ))
        oracle._try_gamma_last_trade = gamma_spy
        oracle._try_fresh_book = book_spy

        with patch(
            "src.control.price_oracle.get_db",
            _db_returning(
                {
                    "token_yes": "tok-A",
                    "token_no": "tok-B",
                    "resolved_outcome": "yes",
                    "end_date": None,
                }
            ),
        ):
            quote = await oracle.get_close_price(
                "m", "tok-A", "yes", prefer_resolved=True
            )
        assert quote.source == "resolved"
        assert quote.price == 1.0
        # Neither book nor gamma helpers should have run.
        book_spy.assert_not_called()
        gamma_spy.assert_not_called()


# --------------------------------------------------------------------------- #
# Pure-helper unit tests (no I/O)                                             #
# --------------------------------------------------------------------------- #


class TestPureHelpers:
    def test_parse_epoch_float(self):
        assert _parse_epoch(1700000000.5) == pytest.approx(1700000000.5)

    def test_parse_epoch_iso(self):
        ts = _parse_epoch("2026-05-17T12:00:00Z")
        assert ts is not None and ts > 0

    def test_parse_epoch_garbage_returns_none(self):
        assert _parse_epoch("not-a-date") is None
        assert _parse_epoch(None) is None

    def test_gamma_last_trade_price_from_outcome_list_yes(self):
        payload = {
            "outcomePrices": "[0.7, 0.3]",
            "token_yes": "tY",
        }
        assert _gamma_last_trade_price(payload, "tY", "yes") == pytest.approx(0.7)

    def test_gamma_last_trade_price_from_outcome_list_no(self):
        payload = {
            "outcomePrices": "[0.7, 0.3]",
            "token_yes": "tY",
        }
        # Holding the NO token → expect the NO leg.
        assert _gamma_last_trade_price(payload, "tN", "no") == pytest.approx(0.3)

    def test_gamma_last_trade_age_s_missing(self):
        assert _gamma_last_trade_age_s({}) is None

    def test_gamma_last_trade_age_s_present(self):
        payload = {"last_trade_time": time.time() - 42.0}
        age = _gamma_last_trade_age_s(payload)
        assert age is not None and 40.0 < age < 50.0
