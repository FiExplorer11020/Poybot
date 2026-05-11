"""Multi-provider Polygon RPC client.

WAVE-1 ARCHITECT SKELETON. Bodies intentionally not implemented; Wave 2
fills them in. See docs/ROUND_6_THE_SPINE.md § 3.2 for the full
contract, and src/registry/falcon_client.py for the canonical reference
of the patterns this module generalises.
"""

from __future__ import annotations

from typing import Any, AsyncIterator

from src.rpc.providers import RPCProvider


class RPCClient:
    """Multi-provider Polygon RPC client.

    Tries providers in priority order:
      1. Local Erigon (priority 0, infinite rate, ~5ms latency)
      2. Alchemy (priority 1, paid tier, fallback)
      3. QuickNode (priority 2, free tier, last resort)

    Per-provider:
      - Adaptive token bucket (extends the Phase 1 FalconClient pattern,
        provider-aware tuning — see src/rpc/rate_limiter.py)
      - Circuit breaker: 5 consecutive failures → 60s cooldown
      - HTTP/2 multiplexing via httpx (or aiohttp; Wave 2 picks the lib
        based on whether httpx ships with cleanly-shaped websocket
        support at implementation time)
      - In-flight call coalescing (identical concurrent requests share
        one HTTP call, 30s TTL — same pattern as FalconClient)

    Methods mirror eth-rpc semantics but with our defensive layer:
      - eth_subscribe(filter) → AsyncIterator[log]
      - eth_call(contract, method, args) → result
      - eth_getLogs(filter, from_block, to_block) → list[log]
      - eth_getBlockByNumber(num) → block

    Reconnect / replay semantics:
      eth_subscribe is the long-lived path used by CLOBChainListener.
      On a transient drop, the iterator re-establishes the subscription
      transparently. Callers receive a continuous stream; coverage
      gaps are surfaced via the chain_sync_state cursor (migration 022).
    """

    def __init__(self, providers: list[RPCProvider]) -> None:
        """
        Args:
            providers: Pre-constructed RPCProvider list. Typically built
                by ``ProviderPool.from_settings()`` in Wave 2; tests
                inject mocks directly.
        """
        # TODO: implement in Wave 2 — see ROUND_6_THE_SPINE.md § 3.2
        raise NotImplementedError("Wave 2 — see ROUND_6_THE_SPINE.md § 3.2")

    async def eth_subscribe(self, filter_obj: dict) -> AsyncIterator[dict]:
        """Long-lived subscription to a filter (typically a ``logs``
        filter scoped to the Polymarket CLOB contract).

        Yields decoded JSON-RPC ``params.result`` payloads. On a
        provider-level disconnect, the underlying WebSocket is re-
        established transparently and the SUBSCRIBE is re-issued
        against the same filter; this method does NOT raise on
        transient drops. Hard failures (every provider tripped) raise
        :class:`RuntimeError` after exhausting retries.

        Args:
            filter_obj: JSON-RPC filter dict, e.g.::

                {
                    "address": "0x...CLOB...",
                    "topics": [
                        ["0x...OrderFilled", "0x...OrdersMatched"],
                    ],
                    "fromBlock": "latest",
                }

        Yields:
            One decoded ``result`` dict per matching log event.
        """
        # TODO: implement in Wave 2 — see ROUND_6_THE_SPINE.md § 3.2
        raise NotImplementedError("Wave 2 — see ROUND_6_THE_SPINE.md § 3.2")
        # unreachable; documents the async-generator shape
        yield  # type: ignore[unreachable]

    async def eth_call(self, contract: str, method: str, args: tuple) -> Any:
        """Synchronous JSON-RPC ``eth_call`` with provider fallback.

        Args:
            contract: 0x-prefixed contract address.
            method: ABI method signature (e.g. ``"balanceOf(address)"``).
                The full ABI encoding is handled by Wave 2's helper
                (likely web3.py's ``encode_function_data`` or eth-abi).
            args: Positional ABI arguments, tuple form.

        Returns:
            The decoded return value(s) from the contract call. Single
            return → scalar; multiple → tuple.
        """
        # TODO: implement in Wave 2 — see ROUND_6_THE_SPINE.md § 3.2
        raise NotImplementedError("Wave 2 — see ROUND_6_THE_SPINE.md § 3.2")

    async def eth_getLogs(
        self,
        filter_obj: dict,
        from_block: int,
        to_block: int | None = None,
    ) -> list[dict]:
        """Historical log fetch.

        Used for:
          * The one-time wallet-universe backfill
            (src/crawler/universe.py::backfill_from_chain).
          * Reconciler gap-fill — when CoverageReconciler detects an
            on-chain trade we missed, it back-fills via this method.

        Implementation note: paid providers cap a single getLogs to a
        block range (typically 2k blocks). Wave 2 implements automatic
        chunking + concatenation when ``to_block - from_block`` exceeds
        the provider's documented max.

        Args:
            filter_obj: Same shape as eth_subscribe's filter (address +
                topics).
            from_block: Inclusive lower bound.
            to_block: Inclusive upper bound. None means chain head at
                call time.

        Returns:
            List of raw log dicts in ascending (block_number, log_index)
            order.
        """
        # TODO: implement in Wave 2 — see ROUND_6_THE_SPINE.md § 3.2
        raise NotImplementedError("Wave 2 — see ROUND_6_THE_SPINE.md § 3.2")

    async def eth_getBlockByNumber(self, num: int | str) -> dict:
        """Fetch a single block header + tx list.

        Args:
            num: Block number, or one of the JSON-RPC string tags
                ``"latest"``, ``"safe"``, ``"finalized"``,
                ``"pending"``, ``"earliest"``.

        Returns:
            Block dict (raw JSON-RPC shape). Wave 2 may layer a Pydantic
            model on top if multiple call sites need the same fields.
        """
        # TODO: implement in Wave 2 — see ROUND_6_THE_SPINE.md § 3.2
        raise NotImplementedError("Wave 2 — see ROUND_6_THE_SPINE.md § 3.2")

    async def close(self) -> None:
        """Tear down every aiohttp/websocket session held by every
        provider in the pool, plus any background tasks (health-check
        loop, coalescing-expiry tasks, subscription reconnect loops).

        Idempotent — safe to call from atexit handlers / signal handlers
        / unit-test fixtures.
        """
        # TODO: implement in Wave 2 — see ROUND_6_THE_SPINE.md § 3.2
        raise NotImplementedError("Wave 2 — see ROUND_6_THE_SPINE.md § 3.2")
