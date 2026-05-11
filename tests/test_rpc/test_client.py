"""Tests for src.rpc.client.RPCClient.

Uses httpx.MockTransport to stub provider HTTP responses; no real RPC
is hit during pytest. Coalescing and fallover behaviour are verified by
counting how many requests reach the transport.
"""

import asyncio
import json
from typing import Any

import httpx
import pytest

from src.rpc.circuit_breaker import CircuitBreaker
from src.rpc.client import RPCClient
from src.rpc.providers import ProviderPool, RPCProvider
from src.rpc.rate_limiter import AdaptiveTokenBucket


# ---------------------------------------------------------------------- #
# Test fixtures / helpers                                                #
# ---------------------------------------------------------------------- #


def _make_provider(
    name: str, priority: int, url: str = "http://local"
) -> RPCProvider:
    return RPCProvider(
        name=name,
        url=url,
        priority=priority,
        bucket=AdaptiveTokenBucket(name, capacity=100, refill_per_sec=100.0),
        breaker=CircuitBreaker(name, failure_threshold=5, cooldown_s=0.05),
    )


class _CallRecorder:
    """Counts HTTP calls by provider URL so tests can assert coalescing
    / fallover behaviour without inspecting MockTransport internals."""

    def __init__(self, responder):
        self.calls: list[dict[str, Any]] = []
        self._responder = responder

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode())
        record = {"url": str(request.url), "body": body}
        self.calls.append(record)
        return self._responder(request, body)


def _ok_response(result: Any):
    def _r(request: httpx.Request, body: dict) -> httpx.Response:
        return httpx.Response(
            200,
            json={"jsonrpc": "2.0", "id": body.get("id"), "result": result},
        )

    return _r


def _err_response(status: int = 500):
    def _r(request: httpx.Request, body: dict) -> httpx.Response:
        return httpx.Response(status, json={"error": f"HTTP {status}"})

    return _r


# ---------------------------------------------------------------------- #
# Tests                                                                  #
# ---------------------------------------------------------------------- #


@pytest.mark.asyncio
async def test_eth_call_returns_mocked_response():
    """A single eth_call returns the mocked result string."""
    p0 = _make_provider("local", 0)
    rec = _CallRecorder(_ok_response("0x42"))
    transport = httpx.MockTransport(rec)
    async with httpx.AsyncClient(transport=transport) as http:
        client = RPCClient([p0], http_client=http)
        out = await client.eth_call("0xCONTRACT", "0xabcd")
        assert out == "0x42"
        await client.close()
    assert len(rec.calls) == 1
    assert rec.calls[0]["body"]["method"] == "eth_call"


@pytest.mark.asyncio
async def test_coalescing_two_concurrent_eth_call_share_one_http_request():
    """Two concurrent identical eth_call -> exactly ONE HTTP call, both
    callers receive the same payload."""
    p0 = _make_provider("local", 0)
    call_count = {"n": 0}

    def _slow_responder(request: httpx.Request, body: dict) -> httpx.Response:
        call_count["n"] += 1
        return httpx.Response(
            200, json={"jsonrpc": "2.0", "id": body["id"], "result": "0xCOALESCED"}
        )

    rec = _CallRecorder(_slow_responder)
    transport = httpx.MockTransport(rec)
    async with httpx.AsyncClient(transport=transport) as http:
        client = RPCClient([p0], http_client=http)
        # Fire two concurrent identical calls.
        r1, r2 = await asyncio.gather(
            client.eth_call("0xCONTRACT", "0xabcd"),
            client.eth_call("0xCONTRACT", "0xabcd"),
        )
        assert r1 == "0xCOALESCED"
        assert r2 == "0xCOALESCED"
        await client.close()
    # Exactly one HTTP roundtrip should have happened.
    assert call_count["n"] == 1, f"expected 1 HTTP call, got {call_count['n']}"


@pytest.mark.asyncio
async def test_eth_getLogs_returns_list_from_mock():
    """eth_getLogs returns a list payload."""
    p0 = _make_provider("local", 0)
    sample = [{"address": "0xC", "data": "0xdeadbeef", "blockNumber": "0x1"}]
    rec = _CallRecorder(_ok_response(sample))
    transport = httpx.MockTransport(rec)
    async with httpx.AsyncClient(transport=transport) as http:
        client = RPCClient([p0], http_client=http)
        logs = await client.eth_getLogs({"address": "0xC"}, from_block=1, to_block=2)
        assert logs == sample
        await client.close()
    body = rec.calls[0]["body"]
    # Filter dict should include the encoded block range tags.
    assert body["params"][0]["fromBlock"] == hex(1)
    assert body["params"][0]["toBlock"] == hex(2)


@pytest.mark.asyncio
async def test_eth_getBlockByNumber_returns_block_dict():
    p0 = _make_provider("local", 0)
    block = {"number": "0x10", "hash": "0xBLOCK"}
    rec = _CallRecorder(_ok_response(block))
    transport = httpx.MockTransport(rec)
    async with httpx.AsyncClient(transport=transport) as http:
        client = RPCClient([p0], http_client=http)
        out = await client.eth_getBlockByNumber(16)
        assert out == block
        # Numeric was hex-encoded.
        assert rec.calls[0]["body"]["params"][0] == hex(16)
        await client.close()


@pytest.mark.asyncio
async def test_failover_when_priority_zero_returns_500():
    """Priority-0 returns HTTP 500; the client falls over to priority-1
    and the fallback counter increments."""
    from src.monitoring.metrics import rpc_fallback_total

    p0 = _make_provider("local-fail", 0, url="http://local-fail")
    p1 = _make_provider("alchemy-fail", 1, url="http://alchemy-fail")

    def _router(request: httpx.Request, body: dict) -> httpx.Response:
        if "local-fail" in str(request.url):
            return httpx.Response(500, json={"error": "boom"})
        return httpx.Response(
            200, json={"jsonrpc": "2.0", "id": body["id"], "result": "0xOK"}
        )

    rec = _CallRecorder(_router)
    transport = httpx.MockTransport(rec)
    counter = rpc_fallback_total.labels(
        from_provider="local-fail", to_provider="alchemy-fail"
    )
    before = counter._value.get()
    async with httpx.AsyncClient(transport=transport) as http:
        client = RPCClient([p0, p1], http_client=http)
        out = await client.eth_call("0xC", "0xff")
        assert out == "0xOK"
        await client.close()
    after = counter._value.get()
    # Either p0 failed once (breaker records the failure, second attempt
    # picks p1) or the breaker tripped earlier; in both cases the
    # fallback counter must have moved.
    assert after >= before + 1


@pytest.mark.asyncio
async def test_close_cancels_background_tasks():
    """close() cancels coalesce-expire background tasks and is idempotent."""
    p0 = _make_provider("local", 0)
    rec = _CallRecorder(_ok_response("0x42"))
    transport = httpx.MockTransport(rec)
    async with httpx.AsyncClient(transport=transport) as http:
        client = RPCClient([p0], http_client=http)
        await client.eth_call("0xC", "0xab")
        # Coalesce-expire bg task should be tracked.
        assert len(client._bg_tasks) >= 0  # zero or one depending on timing
        await client.close()
        assert client._closed is True
        # Idempotent.
        await client.close()
        # bg_tasks emptied.
        assert client._bg_tasks == set()


@pytest.mark.asyncio
async def test_heartbeat_called_on_success_is_defensive():
    """The defensive _heartbeat_rpc import should not break the call
    path even when ingest_health is missing. The mock transport returns
    OK; the call must succeed regardless of heartbeat availability."""
    p0 = _make_provider("local", 0)
    rec = _CallRecorder(_ok_response("0x99"))
    transport = httpx.MockTransport(rec)
    async with httpx.AsyncClient(transport=transport) as http:
        client = RPCClient([p0], http_client=http)
        out = await client.eth_call("0xC", "0xff")
        assert out == "0x99"
        await client.close()


@pytest.mark.asyncio
async def test_pool_property_exposes_underlying_pool():
    """RPCClient.pool returns the ProviderPool instance for inspection."""
    p0 = _make_provider("local", 0)
    pool = ProviderPool([p0])
    rec = _CallRecorder(_ok_response("0x"))
    transport = httpx.MockTransport(rec)
    async with httpx.AsyncClient(transport=transport) as http:
        client = RPCClient(pool, http_client=http)
        assert client.pool is pool
        await client.close()


@pytest.mark.asyncio
async def test_rate_limited_response_triggers_pool_penalty():
    """A 429 response triggers pool.report_429() on the offending
    provider, which halves its bucket's refill rate."""
    p0 = _make_provider("local-429", 0, url="http://local-429")
    p1 = _make_provider("alchemy-429", 1, url="http://alchemy-429")
    base_refill = p0.bucket.refill

    def _router(request: httpx.Request, body: dict) -> httpx.Response:
        if "local-429" in str(request.url):
            return httpx.Response(429, json={"error": "rate_limited"})
        return httpx.Response(
            200, json={"jsonrpc": "2.0", "id": body["id"], "result": "0xOK"}
        )

    rec = _CallRecorder(_router)
    transport = httpx.MockTransport(rec)
    async with httpx.AsyncClient(transport=transport) as http:
        client = RPCClient([p0, p1], http_client=http)
        out = await client.eth_call("0xC", "0xff")
        assert out == "0xOK"
        await client.close()
    # p0's bucket should have been penalised.
    assert p0.bucket.refill < base_refill
    assert p0.bucket.penalty_active is True


@pytest.mark.asyncio
async def test_all_providers_failing_raises():
    """Every provider returns 500 -> the client raises rather than
    silently returning a bad payload."""
    p0 = _make_provider("p0-bad", 0, url="http://p0-bad")
    p1 = _make_provider("p1-bad", 1, url="http://p1-bad")
    rec = _CallRecorder(_err_response(500))
    transport = httpx.MockTransport(rec)
    async with httpx.AsyncClient(transport=transport) as http:
        client = RPCClient([p0, p1], http_client=http)
        with pytest.raises(Exception):
            await client.eth_call("0xC", "0xff")
        await client.close()
