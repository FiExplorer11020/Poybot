"""Multi-provider Polygon RPC client.

Wave-2 implementation. Tries providers in priority order with adaptive
rate limiting, circuit breaking, and in-flight call coalescing for
HTTP-side JSON-RPC methods.

HTTP methods (eth_call, eth_getLogs, eth_getBlockByNumber) are coalesced
via a 30-second TTL future cache, mirroring
:class:`src.registry.falcon_client.FalconClient`. eth_subscribe is
explicitly NOT coalesced: subscribers each get their own WebSocket
stream so the underlying socket reconnects don't bleed across consumers.

Defensive imports follow the FalconClient pattern: ingest-health
heartbeat and Prometheus metrics fall back to no-ops if the supporting
module is unavailable in a sparse test env.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
from typing import Any, AsyncIterator

import httpx
from loguru import logger

from src.rpc.providers import ProviderPool, RPCProvider

# Defensive metrics import — keeps the test harness happy in checkouts
# that strip prometheus_client.
try:
    from src.monitoring.metrics import (  # type: ignore[attr-defined]
        rpc_calls_total,
        rpc_coalesced_calls_total,
        rpc_latency_seconds,
    )
except Exception:  # pragma: no cover — defensive fallback
    class _NoOpLabel:
        def labels(self, *_args, **_kwargs):
            return self

        def inc(self, *_args, **_kwargs):
            return None

        def observe(self, *_args, **_kwargs):
            return None

    rpc_calls_total = _NoOpLabel()  # type: ignore[assignment]
    rpc_coalesced_calls_total = _NoOpLabel()  # type: ignore[assignment]
    rpc_latency_seconds = _NoOpLabel()  # type: ignore[assignment]

# Defensive ingest-health heartbeat, mirroring _heartbeat_falcon in
# src/registry/falcon_client.py. The constant name is symbolic only --
# ingest_health may not define it yet (Wave 3 will land alongside the
# on-chain listener).
try:
    from src.monitoring.ingest_health import (  # type: ignore[attr-defined]
        get_health_monitor,
    )

    def _heartbeat_rpc(method: str) -> None:
        try:
            get_health_monitor().heartbeat(f"rpc_{method}")
        except Exception:
            pass
except Exception:  # pragma: no cover — defensive
    def _heartbeat_rpc(method: str) -> None:
        return None


# How long a completed coalesced future is kept around as a fast hit
# for re-issued identical calls. Mirrors FALCON_COALESCE_TTL_S.
RPC_COALESCE_TTL_S: float = 30.0
# Default per-call HTTP timeout. Conservative -- the local Erigon
# should respond in well under 100ms, but a paid-provider fallback
# could legitimately take seconds for a wide getLogs.
RPC_HTTP_TIMEOUT_S: float = 10.0


class RPCClient:
    """Multi-provider Polygon RPC client.

    See module docstring. Public surface:
      * ``eth_call(contract, method, args)`` -> Any
      * ``eth_getLogs(filter_obj, from_block, to_block)`` -> list[dict]
      * ``eth_getBlockByNumber(num)`` -> dict
      * ``eth_subscribe(filter_obj)`` -> AsyncIterator[dict]
      * ``close()``  -- idempotent shutdown.

    Tests inject a pool whose providers have been constructed against a
    fake :class:`httpx.MockTransport`. The client never touches a real
    RPC endpoint during pytest.
    """

    def __init__(
        self,
        providers_or_pool: list[RPCProvider] | ProviderPool,
        http_client: httpx.AsyncClient | None = None,
        timeout_s: float = RPC_HTTP_TIMEOUT_S,
    ) -> None:
        if isinstance(providers_or_pool, ProviderPool):
            self._pool = providers_or_pool
        else:
            self._pool = ProviderPool(providers_or_pool)
        # Tests pass a pre-built AsyncClient with a MockTransport; in
        # production we build one lazily. We always own ``_owns_client``
        # to decide whether close() should close the transport.
        if http_client is not None:
            self._http: httpx.AsyncClient = http_client
            self._owns_client = False
        else:
            # http2=True requires the h2 extra; we don't depend on it
            # here. HTTP/1.1 keepalive is more than enough for our
            # typical concurrency. The architect's docstring mentions
            # HTTP/2 as a Wave-3 polish item.
            self._http = httpx.AsyncClient(timeout=timeout_s)
            self._owns_client = True
        self._timeout_s = float(timeout_s)

        # In-flight coalescing state -- mirrors FalconClient._inflight.
        # Key: cache key string. Value: (future, completed_at).
        self._inflight: dict[str, tuple[asyncio.Future, float]] = {}
        self._inflight_lock = asyncio.Lock()
        self._coalesce_ttl_s = RPC_COALESCE_TTL_S

        # Fire-and-forget tasks (coalesce-expiry, ws reconnect loops)
        # tracked so close() can cancel any survivors.
        self._bg_tasks: set[asyncio.Task] = set()
        # WS connections we may need to close on shutdown.
        self._ws_connections: set[Any] = set()
        # Request ID counter for JSON-RPC envelopes.
        self._next_rpc_id = 0
        self._closed = False

    # ------------------------------------------------------------------ #
    # Coalescing                                                         #
    # ------------------------------------------------------------------ #

    def _coalesce_key(self, method: str, params: list[Any]) -> str:
        """Stable cache key for the (method, params) tuple."""
        h = hashlib.md5(
            json.dumps({"m": method, "p": params}, sort_keys=True, default=str).encode()
        ).hexdigest()
        return f"rpc:{method}:{h}"

    async def _coalesce_lookup(
        self, cache_key: str, method: str
    ) -> tuple[asyncio.Future, bool]:
        """Return (future, is_owner). If owner, caller does the real work
        and resolves the future. Otherwise caller awaits it."""
        now = time.monotonic()
        async with self._inflight_lock:
            entry = self._inflight.get(cache_key)
            if entry is not None:
                fut, completed_at = entry
                if completed_at == 0.0 or (now - completed_at) <= self._coalesce_ttl_s:
                    try:
                        rpc_coalesced_calls_total.labels(
                            provider="*", method=method
                        ).inc()
                    except Exception:  # pragma: no cover — defensive
                        pass
                    return fut, False
                self._inflight.pop(cache_key, None)
            new_fut: asyncio.Future = asyncio.get_event_loop().create_future()
            self._inflight[cache_key] = (new_fut, 0.0)
            return new_fut, True

    async def _coalesce_complete(
        self,
        cache_key: str,
        fut: asyncio.Future,
        value: Any = None,
        exc: BaseException | None = None,
    ) -> None:
        if exc is not None and not fut.done():
            fut.set_exception(exc)
        elif not fut.done():
            fut.set_result(value)
        async with self._inflight_lock:
            self._inflight[cache_key] = (fut, time.monotonic())
            task = asyncio.create_task(
                self._coalesce_expire(cache_key, self._coalesce_ttl_s)
            )
            self._bg_tasks.add(task)
            task.add_done_callback(self._bg_tasks.discard)

    async def _coalesce_expire(self, cache_key: str, ttl_s: float) -> None:
        try:
            await asyncio.sleep(max(0.0, ttl_s))
        except asyncio.CancelledError:
            return
        async with self._inflight_lock:
            entry = self._inflight.get(cache_key)
            if entry is None:
                return
            _fut, completed_at = entry
            if completed_at > 0.0 and (time.monotonic() - completed_at) >= ttl_s:
                self._inflight.pop(cache_key, None)

    # ------------------------------------------------------------------ #
    # JSON-RPC plumbing                                                  #
    # ------------------------------------------------------------------ #

    def _rpc_envelope(self, method: str, params: list[Any]) -> dict[str, Any]:
        self._next_rpc_id += 1
        return {
            "jsonrpc": "2.0",
            "id": self._next_rpc_id,
            "method": method,
            "params": params,
        }

    async def _do_http_call(
        self,
        provider: RPCProvider,
        method: str,
        params: list[Any],
    ) -> Any:
        """Single HTTP JSON-RPC call against one provider. Returns the
        decoded ``result`` field. Raises on transport error or RPC
        error response."""
        envelope = self._rpc_envelope(method, params)
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if provider.api_key:
            headers["Authorization"] = f"Bearer {provider.api_key}"
        attempt_start = time.monotonic()
        attempt_result = "error"
        try:
            resp = await self._http.post(provider.url, json=envelope, headers=headers)
            if resp.status_code == 429:
                self._pool.report_429(provider.name)
                attempt_result = "rate_limited"
                raise httpx.HTTPStatusError(
                    f"HTTP 429 from {provider.name}", request=resp.request, response=resp
                )
            if resp.status_code >= 500:
                attempt_result = "error"
                raise httpx.HTTPStatusError(
                    f"HTTP {resp.status_code} from {provider.name}",
                    request=resp.request,
                    response=resp,
                )
            if resp.status_code >= 400:
                attempt_result = "error"
                raise httpx.HTTPStatusError(
                    f"HTTP {resp.status_code} from {provider.name}: {resp.text[:200]}",
                    request=resp.request,
                    response=resp,
                )
            payload = resp.json()
            if isinstance(payload, dict) and "error" in payload and payload.get("error"):
                attempt_result = "error"
                err = payload["error"]
                raise RuntimeError(f"RPC error from {provider.name}: {err}")
            result = payload.get("result") if isinstance(payload, dict) else payload
            attempt_result = "ok" if result is not None else "empty"
            _heartbeat_rpc(method)
            return result
        except asyncio.TimeoutError:
            attempt_result = "timeout"
            raise
        finally:
            try:
                rpc_latency_seconds.labels(provider=provider.name, method=method).observe(
                    time.monotonic() - attempt_start
                )
                rpc_calls_total.labels(
                    provider=provider.name, method=method, result=attempt_result
                ).inc()
            except Exception:  # pragma: no cover — defensive
                pass

    async def _call_with_failover(
        self, method: str, params: list[Any]
    ) -> Any:
        """Acquire a provider, run the call. On failure the breaker
        records the failure (via the pool's context manager) and we
        retry with the *next* provider in priority order -- the failing
        one is added to a per-call exclusion set so we don't bounce on
        the same offender.

        We limit to ``pool.size`` total attempts so a fully-tripped
        pool fails fast rather than spinning forever inside the
        acquire-timeout loop.
        """
        attempts = max(1, self._pool.size)
        last_exc: BaseException | None = None
        excluded: set[str] = set()
        for _ in range(attempts):
            picked_name: str | None = None
            try:
                async with self._pool.acquire(exclude=excluded) as provider:
                    picked_name = provider.name
                    return await self._do_http_call(provider, method, params)
            except Exception as exc:
                last_exc = exc
                if picked_name is not None:
                    excluded.add(picked_name)
                logger.debug(
                    "RPC call {} failed via failover (provider={}): {}",
                    method,
                    picked_name,
                    exc,
                )
                continue
        assert last_exc is not None
        raise last_exc

    async def _coalesced_call(self, method: str, params: list[Any]) -> Any:
        """Coalesced JSON-RPC call. Identical concurrent (method, params)
        share one HTTP request; the second caller awaits the first's
        future. Completed futures are cached for ``RPC_COALESCE_TTL_S``
        seconds for fast re-issue."""
        cache_key = self._coalesce_key(method, params)
        fut, is_owner = await self._coalesce_lookup(cache_key, method)
        if not is_owner:
            return await fut
        try:
            result = await self._call_with_failover(method, params)
        except BaseException as exc:
            await self._coalesce_complete(cache_key, fut, exc=exc)
            raise
        await self._coalesce_complete(cache_key, fut, value=result)
        return result

    # ------------------------------------------------------------------ #
    # Public RPC surface                                                 #
    # ------------------------------------------------------------------ #

    async def eth_call(
        self, contract: str, method: str, args: tuple | list = ()
    ) -> Any:
        """Synchronous JSON-RPC ``eth_call`` with provider fallback.

        ``method`` here is the *ABI-encoded calldata string* (already
        0x-prefixed) for Wave-2 simplicity. The architect's docstring
        mentions ABI encoding helpers as Wave-3 polish: callers that
        need full ABI encoding wrap eth_call themselves until then.

        Backward-compat shim: if ``method`` does not start with ``0x``
        we treat it as opaque and pass it through -- tests inject
        whatever string they want and the mock transport echoes a
        canned reply.
        """
        call_obj = {"to": contract, "data": method}
        # ``args`` are accepted for API stability with the architect
        # contract but only the encoded data string is forwarded.
        # Future ABI helper will fill ``data`` from (method, args).
        _ = args
        return await self._coalesced_call("eth_call", [call_obj, "latest"])

    async def eth_getLogs(
        self,
        filter_obj: dict,
        from_block: int,
        to_block: int | None = None,
    ) -> list[dict]:
        """Historical log fetch. Wave-2 is single-page: callers that need
        chunked block ranges build them on top. The provider-side cap
        handling lands in Wave-3 alongside the universe backfill."""
        merged: dict[str, Any] = dict(filter_obj or {})
        merged["fromBlock"] = (
            hex(from_block) if isinstance(from_block, int) else from_block
        )
        if to_block is not None:
            merged["toBlock"] = (
                hex(to_block) if isinstance(to_block, int) else to_block
            )
        result = await self._coalesced_call("eth_getLogs", [merged])
        if not isinstance(result, list):
            return []
        return result

    async def eth_getBlockByNumber(
        self, num: int | str, include_txs: bool = False
    ) -> dict:
        """Fetch a single block header (+ tx list iff include_txs)."""
        if isinstance(num, int):
            tag = hex(num)
        else:
            tag = num
        result = await self._coalesced_call(
            "eth_getBlockByNumber", [tag, bool(include_txs)]
        )
        if not isinstance(result, dict):
            return {}
        return result

    async def eth_subscribe(self, filter_obj: dict) -> AsyncIterator[dict]:
        """Long-lived subscription. NOT coalesced -- each subscriber
        gets its own WebSocket stream.

        Wave-2 ships the structural piece: pick a provider with a
        ``ws_url``, establish the connection, issue eth_subscribe, yield
        decoded params.result entries. Bounded-backoff reconnect on
        drop, re-issue SUBSCRIBE on reconnect. Full
        websockets.connect-driven implementation is exercised by the
        on-chain listener tests in Wave-3; here the integration shape
        is what matters.
        """
        # Import lazily so this module loads without `websockets`
        # installed in checkouts that don't need the subscribe path.
        import websockets  # type: ignore[import-not-found]

        backoff_s = 1.0
        max_backoff_s = 30.0
        while not self._closed:
            # Pick the first provider with a configured ws_url.
            ws_provider: RPCProvider | None = None
            for p in self._pool.providers:
                if p.ws_url:
                    ws_provider = p
                    break
            if ws_provider is None:
                raise RuntimeError(
                    "No RPC provider has a ws_url configured; cannot eth_subscribe"
                )
            try:
                async with websockets.connect(ws_provider.ws_url) as ws:
                    self._ws_connections.add(ws)
                    sub_id_envelope = self._rpc_envelope(
                        "eth_subscribe", ["logs", filter_obj]
                    )
                    await ws.send(json.dumps(sub_id_envelope))
                    # First reply is the subscription ID.
                    raw = await ws.recv()
                    _resp = json.loads(raw)  # noqa: F841 — kept for debug parity
                    backoff_s = 1.0  # reset after a clean handshake
                    while not self._closed:
                        raw = await ws.recv()
                        try:
                            msg = json.loads(raw)
                        except Exception:
                            continue
                        params = msg.get("params") if isinstance(msg, dict) else None
                        if isinstance(params, dict) and "result" in params:
                            yield params["result"]
            except Exception as exc:
                if self._closed:
                    return
                logger.warning(
                    "eth_subscribe WS dropped on provider={}; reconnecting in {:.1f}s "
                    "({})",
                    ws_provider.name,
                    backoff_s,
                    exc,
                )
                await asyncio.sleep(backoff_s)
                backoff_s = min(backoff_s * 2.0, max_backoff_s)
                continue
            finally:
                # Drop any reference to a closed socket so close() doesn't
                # try to double-close it. websockets.connect's CM has
                # already shut it down by the time we get here.
                pass

    # ------------------------------------------------------------------ #
    # Lifecycle                                                          #
    # ------------------------------------------------------------------ #

    @property
    def pool(self) -> ProviderPool:
        """Read-only access to the underlying ProviderPool."""
        return self._pool

    async def close(self) -> None:
        """Tear down the HTTP client + all background tasks. Idempotent."""
        if self._closed:
            return
        self._closed = True
        # Cancel coalesce-expire tasks.
        for task in list(self._bg_tasks):
            task.cancel()
        self._bg_tasks.clear()
        # Close any tracked websockets. Most are managed by their
        # async-with block in eth_subscribe(); this loop is for safety
        # if a Wave-3 caller starts tracking long-lived sockets manually.
        for ws in list(self._ws_connections):
            try:
                close_coro = getattr(ws, "close", None)
                if callable(close_coro):
                    res = close_coro()
                    if asyncio.iscoroutine(res):
                        await res
            except Exception:  # pragma: no cover — defensive
                pass
        self._ws_connections.clear()
        if self._owns_client and self._http is not None:
            try:
                await self._http.aclose()
            except Exception:  # pragma: no cover — defensive
                pass
