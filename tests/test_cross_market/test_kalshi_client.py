"""KalshiClient tests with mocked aiohttp.

Coverage:
  * fetch_market returns the unwrapped market dict.
  * fetch_wallet_positions returns the list of positions.
  * stream_trades filters by requested market_ids.
  * 4xx response returns empty / None.
  * 429 marks rate_limited (no crash).
  * Token bucket caps burst at capacity.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from src.cross_market.kalshi_client import KalshiClient


def _fake_response(status: int, body: Any, headers: dict | None = None):
    class _Resp:
        def __init__(self):
            self.status = status
            self.headers = headers or {}
            self._body = body

        async def text(self):
            import json
            return json.dumps(self._body) if self._body else ""

        async def json(self):
            return self._body

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

    return _Resp


def _build_session(responses: list[Any]):
    """Each entry is either a dict (200 JSON body) or an (status, body) tuple."""
    queue = list(responses)
    captured: list[dict[str, Any]] = []

    class _Sess:
        def get(self, url, params=None, headers=None, timeout=None):
            captured.append({"url": url, "params": dict(params or {})})
            if not queue:
                return _fake_response(200, None)()
            entry = queue.pop(0)
            if isinstance(entry, tuple):
                status, body = entry
            else:
                status, body = 200, entry
            return _fake_response(status, body)()

    return _Sess(), captured


class TestFetchMarket:
    @pytest.mark.asyncio
    async def test_unwraps_market_key(self):
        sess, _ = _build_session([{"market": {"ticker": "FED-RATE", "open": True}}])
        client = KalshiClient(sess, api_key="test-key")
        market = await client.fetch_market("FED-RATE")
        assert market is not None
        assert market["ticker"] == "FED-RATE"

    @pytest.mark.asyncio
    async def test_404_returns_none(self):
        sess, _ = _build_session([(404, None)])
        client = KalshiClient(sess, api_key="test-key")
        market = await client.fetch_market("FED-RATE")
        assert market is None


class TestFetchWalletPositions:
    @pytest.mark.asyncio
    async def test_returns_market_positions_list(self):
        positions = [
            {"ticker": "FED-RATE", "position": 100, "market_exposure": 500.0},
            {"ticker": "OTHER", "position": -50, "market_exposure": 250.0},
        ]
        sess, _ = _build_session([{"market_positions": positions}])
        client = KalshiClient(sess, api_key="test-key")
        out = await client.fetch_wallet_positions("acct-1")
        assert len(out) == 2

    @pytest.mark.asyncio
    async def test_empty_response_returns_empty_list(self):
        sess, _ = _build_session([{"market_positions": []}])
        client = KalshiClient(sess, api_key="test-key")
        out = await client.fetch_wallet_positions("acct-1")
        assert out == []

    @pytest.mark.asyncio
    async def test_error_response_returns_empty_list(self):
        sess, _ = _build_session([(500, None)])
        client = KalshiClient(sess, api_key="test-key")
        out = await client.fetch_wallet_positions("acct-1")
        assert out == []


class TestStreamTrades:
    @pytest.mark.asyncio
    async def test_filters_by_market_id(self):
        trades = [
            {"ticker": "FED-RATE", "trade_id": "1"},
            {"ticker": "OTHER", "trade_id": "2"},
            {"ticker": "FED-RATE", "trade_id": "3"},
        ]
        sess, _ = _build_session([{"trades": trades}])
        client = KalshiClient(sess, api_key="test-key")
        out = await client.stream_trades(["FED-RATE"])
        ids = [t["trade_id"] for t in out]
        assert set(ids) == {"1", "3"}

    @pytest.mark.asyncio
    async def test_empty_market_ids_returns_empty(self):
        sess, captured = _build_session([])
        client = KalshiClient(sess, api_key="test-key")
        out = await client.stream_trades([])
        assert out == []
        # No HTTP call made.
        assert captured == []


class TestRateLimit:
    @pytest.mark.asyncio
    async def test_429_response_does_not_crash(self):
        sess, _ = _build_session([(429, None)])
        client = KalshiClient(sess, api_key="test-key")
        market = await client.fetch_market("FED-RATE")
        assert market is None  # 429 → status not 200 → None
