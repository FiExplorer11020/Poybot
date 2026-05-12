"""Wave-3 hardening for the PredictIt client.

Coverage:
  * 5xx response → fetch_market returns None (gracefully; the aggregator
    cadence retries on the next cycle).
  * Multiple 5xx in sequence → still no crash; positions still empty.
  * fetch_wallet_positions is a documented no-op regardless of session
    behaviour (regulator-imposed; spec § 4.1).
"""
from __future__ import annotations

import json
from typing import Any

import pytest

from src.cross_market.predictit_client import PredictItClient


def _fake_response(status: int, body: Any):
    class _Resp:
        def __init__(self) -> None:
            self.status = status
            self._body = body
            self.headers: dict[str, str] = {}

        async def text(self) -> str:
            return json.dumps(self._body) if self._body else ""

        async def json(self) -> Any:
            return self._body

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

    return _Resp


def _build_session(statuses: list[int]):
    queue = list(statuses)
    captured: list[dict[str, Any]] = []

    class _Sess:
        def get(self, url, params=None, headers=None, timeout=None):
            captured.append({"url": url, "params": dict(params or {})})
            status = queue.pop(0) if queue else 200
            return _fake_response(status, None)()

    return _Sess(), captured


class TestServerErrorPaths:
    @pytest.mark.asyncio
    async def test_500_returns_none(self):
        sess, _ = _build_session([500])
        client = PredictItClient(sess)
        m = await client.fetch_market("1234")
        assert m is None

    @pytest.mark.asyncio
    async def test_503_returns_none(self):
        sess, _ = _build_session([503])
        client = PredictItClient(sess)
        m = await client.fetch_market("1234")
        assert m is None

    @pytest.mark.asyncio
    async def test_multiple_5xx_in_sequence_no_crash(self):
        # stream_trades over 3 markets, all return 500 — must produce
        # an empty list without raising.
        sess, captured = _build_session([500, 500, 500])
        client = PredictItClient(sess)
        out = await client.stream_trades(["1", "2", "3"])
        assert out == []
        # 3 GETs attempted.
        assert len(captured) == 3


class TestWalletPositionsAlwaysEmpty:
    @pytest.mark.asyncio
    async def test_returns_empty_under_session_failure(self):
        """Regardless of session state, PredictIt's fetch_wallet_positions
        contract is to return [] (regulator-imposed). The body of the
        method does NOT issue any HTTP call so even a failing session
        produces []."""
        class _Sess:
            def get(self, *a, **kw):
                raise AssertionError("must not be called")

        client = PredictItClient(_Sess())
        out = await client.fetch_wallet_positions("anyone")
        assert out == []
