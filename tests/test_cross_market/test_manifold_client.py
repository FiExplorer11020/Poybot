"""ManifoldClient tests with mocked aiohttp."""
from __future__ import annotations

import json
from typing import Any

import pytest

from src.cross_market.manifold_client import ManifoldClient


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
                return _fake_response(200, [])()
            entry = queue.pop(0)
            if isinstance(entry, tuple):
                status, body = entry
            else:
                status, body = 200, entry
            return _fake_response(status, body)()

    return _Sess(), captured


class TestFetchMarket:
    @pytest.mark.asyncio
    async def test_returns_market_payload(self):
        sess, _ = _build_session([{"id": "abc", "question": "Will X?"}])
        client = ManifoldClient(sess)
        m = await client.fetch_market("abc")
        assert m is not None
        assert m["id"] == "abc"

    @pytest.mark.asyncio
    async def test_404_returns_none(self):
        sess, _ = _build_session([(404, None)])
        client = ManifoldClient(sess)
        assert await client.fetch_market("abc") is None


class TestFetchWalletPositions:
    @pytest.mark.asyncio
    async def test_returns_bets_list(self):
        bets = [
            {"contractId": "m1", "outcome": "YES", "amount": 100},
            {"contractId": "m2", "outcome": "NO", "amount": 50},
        ]
        sess, captured = _build_session([bets])
        client = ManifoldClient(sess)
        out = await client.fetch_wallet_positions("alice")
        assert len(out) == 2
        assert captured[0]["params"]["username"] == "alice"

    @pytest.mark.asyncio
    async def test_non_list_response_returns_empty(self):
        sess, _ = _build_session([{"error": "bad request"}])
        client = ManifoldClient(sess)
        out = await client.fetch_wallet_positions("alice")
        assert out == []


class TestStreamTrades:
    @pytest.mark.asyncio
    async def test_aggregates_per_market(self):
        bets1 = [{"id": "b1", "contractId": "m1"}]
        bets2 = [{"id": "b2", "contractId": "m2"}]
        sess, _ = _build_session([bets1, bets2])
        client = ManifoldClient(sess)
        out = await client.stream_trades(["m1", "m2"])
        assert len(out) == 2

    @pytest.mark.asyncio
    async def test_empty_input(self):
        sess, captured = _build_session([])
        client = ManifoldClient(sess)
        out = await client.stream_trades([])
        assert out == []
        assert captured == []
