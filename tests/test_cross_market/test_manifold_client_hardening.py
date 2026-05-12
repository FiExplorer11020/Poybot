"""Wave-3 hardening for the Manifold client.

Coverage:
  * Malformed JSON body — fetch_market returns None gracefully.
  * Connection error (network down) — fetch_market returns None, no crash.
  * Empty contractId list short-circuits without a single GET.
"""
from __future__ import annotations

from typing import Any

import pytest

from src.cross_market.manifold_client import ManifoldClient


class _BadJsonResp:
    def __init__(self) -> None:
        self.status = 200
        self.headers: dict[str, str] = {}

    async def text(self) -> str:
        return "garbage"

    async def json(self) -> Any:
        raise ValueError("body is not json")

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False


class _ErrSession:
    """Session whose .get() raises a connection error."""

    def get(self, *_, **__):
        class _Ctx:
            async def __aenter__(self):
                raise ConnectionError("DNS failure")

            async def __aexit__(self, *_):
                return False

        return _Ctx()


class TestMalformedJson:
    @pytest.mark.asyncio
    async def test_bad_json_returns_none(self):
        class _Sess:
            def get(self, *a, **kw):
                return _BadJsonResp()

        client = ManifoldClient(_Sess())
        out = await client.fetch_market("anything")
        assert out is None


class TestConnectionError:
    @pytest.mark.asyncio
    async def test_connection_error_returns_none(self):
        client = ManifoldClient(_ErrSession())
        out = await client.fetch_market("anything")
        assert out is None

    @pytest.mark.asyncio
    async def test_connection_error_yields_empty_positions(self):
        client = ManifoldClient(_ErrSession())
        out = await client.fetch_wallet_positions("alice")
        assert out == []


class TestEmptyStreamTrades:
    @pytest.mark.asyncio
    async def test_empty_market_list_short_circuits(self):
        calls: list[Any] = []

        class _Sess:
            def get(self, *a, **kw):
                calls.append(a)
                raise AssertionError("should not be called")

        client = ManifoldClient(_Sess())
        out = await client.stream_trades([])
        assert out == []
        assert calls == []
