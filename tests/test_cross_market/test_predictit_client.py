"""PredictItClient tests with mocked aiohttp."""
from __future__ import annotations

import json
from typing import Any

import pytest

from src.cross_market.predictit_client import PredictItClient


def _fake_response(status: int, body: Any):
    class _Resp:
        def __init__(self):
            self.status = status
            self._body = body

        async def text(self):
            return json.dumps(self._body) if self._body else ""

        async def json(self):
            return self._body

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

    return _Resp


def _build_session(responses: list[Any]):
    queue = list(responses)
    captured: list[dict[str, Any]] = []

    class _Sess:
        def get(self, url, params=None, headers=None, timeout=None):
            captured.append({"url": url, "params": dict(params or {})})
            if not queue:
                return _fake_response(404, None)()
            entry = queue.pop(0)
            if isinstance(entry, tuple):
                status, body = entry
            else:
                status, body = 200, entry
            return _fake_response(status, body)()

    return _Sess(), captured


class TestFetchMarket:
    @pytest.mark.asyncio
    async def test_returns_market(self):
        body = {
            "ID": 1234,
            "Name": "Will Fed raise?",
            "Contracts": [
                {"ID": 1, "Name": "Yes", "LastTradePrice": 0.40},
                {"ID": 2, "Name": "No", "LastTradePrice": 0.60},
            ],
        }
        sess, _ = _build_session([body])
        client = PredictItClient(sess)
        m = await client.fetch_market("1234")
        assert m is not None
        assert len(m["Contracts"]) == 2


class TestPositionsAreEmpty:
    @pytest.mark.asyncio
    async def test_individual_positions_not_exposed(self):
        # PredictIt's API does NOT expose individual positions; the
        # client returns [] regardless.
        sess, _ = _build_session([])
        client = PredictItClient(sess)
        out = await client.fetch_wallet_positions("any-account")
        assert out == []


class TestStreamTrades:
    @pytest.mark.asyncio
    async def test_returns_market_state_snapshot(self):
        body = {
            "ID": 1234,
            "Name": "Will Fed raise?",
            "Contracts": [
                {"ID": 1, "Name": "Yes", "LastTradePrice": 0.40,
                 "BestBuyYesCost": 0.42, "BestBuyNoCost": 0.60,
                 "BestSellYesCost": 0.38, "BestSellNoCost": 0.58},
            ],
        }
        sess, _ = _build_session([body])
        client = PredictItClient(sess)
        out = await client.stream_trades(["1234"])
        assert len(out) == 1
        assert out[0]["last_trade_price"] == 0.40
