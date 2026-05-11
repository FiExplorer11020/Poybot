"""Multi-provider Polygon RPC abstraction (Round 6 / The Spine § 3.2).

The Bot never calls a Polygon RPC provider directly. Every chain read /
subscription flows through :class:`src.rpc.client.RPCClient` which sits
on top of :class:`src.rpc.providers.ProviderPool` and applies a per-
provider :class:`src.rpc.rate_limiter.AdaptiveTokenBucket` plus a
:class:`src.rpc.circuit_breaker.CircuitBreaker`.

The patterns mirror :mod:`src.registry.falcon_client` (token bucket,
in-flight coalescing, multi-key pool). See
docs/ROUND_6_THE_SPINE.md § 3.2 for the full contract.
"""
