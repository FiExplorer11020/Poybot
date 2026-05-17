"""
Regression tests for the 2026-05-17 runtime-config propagation bug.

The incident: paper_trade #25 opened on a sports market at 11:10 UTC
despite ``category_whitelist=crypto,macro`` having been HSET on
``runtime_config:risk`` at 11:08. Root cause: ``RuntimeConfig._load_overrides``
only read the JSON blob at ``runtime_config:overrides`` (the dashboard
path), and the operator's HSET landed in a completely different
Redis key the engine never consulted.

These tests pin the contract that, going forward:

  1. A direct ``HSET runtime_config:risk <field> <value>`` propagates
     to ``RuntimeConfig.get()`` within the cache TTL (5 s, per the
     2026-05-17 fix).
  2. A second write replaces the first within the cache TTL.
  3. The dashboard JSON blob (``runtime_config:overrides``) wins over
     the legacy hash on conflict — the documented authority order is
     preserved.
  4. ``set_overrides`` pub/sub publication triggers cache invalidation
     in a separately-instantiated RuntimeConfig (the engine-side
     ``start_pubsub`` wiring).
  5. Typo-ed hash fields (keys not in ALLOWED_KEYS) are dropped and
     never poison the effective config.
  6. Type coercion: string-only hash values land as the right Python
     type (int / bool / float / str) per ALLOWED_KEYS classification.
"""

from __future__ import annotations

import asyncio
import json
import time

import fakeredis.aioredis
import pytest

from src.control import runtime_config as rc_module
from src.control.runtime_config import (
    REDIS_KEY,
    REDIS_LEGACY_HASH_KEY,
    REDIS_PUBSUB_CHANNEL,
    RuntimeConfig,
    _CACHE_TTL_S,
)


# --------------------------------------------------------------------------- #
# Fixtures                                                                     #
# --------------------------------------------------------------------------- #


@pytest.fixture
async def redis_client():
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield client
    await client.aclose()


async def _wait_until(predicate, timeout: float = 2.0, interval: float = 0.01) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return predicate()


# --------------------------------------------------------------------------- #
# Tests                                                                        #
# --------------------------------------------------------------------------- #


async def test_cache_ttl_is_at_most_5s():
    """Operators were burned once by a 30 s stale window. Lock the new
    ceiling so a refactor can't silently lengthen it again."""
    assert _CACHE_TTL_S <= 5.0, (
        f"RuntimeConfig cache TTL is {_CACHE_TTL_S}s — must stay ≤5 s "
        "(2026-05-17 incident root cause was the prior 30 s window)."
    )


async def test_legacy_hash_propagates_within_cache_ttl(redis_client):
    """Reproduce the exact 2026-05-17 incident path: HSET on
    runtime_config:risk → effective() reflects it on the next read past
    the cache TTL. This is the test that would have caught the bug."""
    cfg = RuntimeConfig(redis_client=redis_client)

    # First call hydrates the cache with defaults (category_whitelist
    # = 'sports,crypto,macro' from settings).
    initial = await cfg.get("category_whitelist")
    assert "sports" in initial, "sanity: default whitelist includes sports"

    # Operator HSETs the legacy hash to lock down to crypto+macro,
    # replicating the production trace.
    await redis_client.hset(
        REDIS_LEGACY_HASH_KEY,
        mapping={"category_whitelist": "crypto,macro"},
    )

    # Cache TTL must elapse so the next read goes back to Redis.
    # Sleeping the full TTL + a 100 ms safety margin keeps the test
    # deterministic without making it slower than the contract demands.
    await asyncio.sleep(_CACHE_TTL_S + 0.1)

    effective = await cfg.get("category_whitelist")
    assert effective == "crypto,macro", (
        f"hand-edited hash override ignored; got {effective!r} "
        f"(expected 'crypto,macro')"
    )


async def test_legacy_hash_update_is_visible_within_ttl(redis_client):
    """Second HSET replaces the first within the cache TTL — proves the
    cache invalidates on schedule, not just on first read."""
    cfg = RuntimeConfig(redis_client=redis_client)

    await redis_client.hset(
        REDIS_LEGACY_HASH_KEY,
        mapping={"category_whitelist": "crypto"},
    )
    # Force the first read after the legacy write so the cache is loaded
    # with the "crypto" override.
    assert (await cfg.get("category_whitelist")) == "crypto"

    # Operator changes the whitelist to "macro" only.
    await redis_client.hset(
        REDIS_LEGACY_HASH_KEY,
        mapping={"category_whitelist": "macro"},
    )

    # Wait for the cache window to expire, then re-read.
    await asyncio.sleep(_CACHE_TTL_S + 0.1)
    final = await cfg.get("category_whitelist")
    assert final == "macro", (
        f"update-after-update propagation failed; got {final!r}"
    )


async def test_dashboard_json_wins_over_legacy_hash(redis_client):
    """If BOTH the JSON blob and the legacy hash carry an override for
    the same key, the dashboard's authoritative path must win. This
    documents the precedence — we don't want an old hand-edit silently
    overriding what the operator just clicked in the UI."""
    # Hand-edit lands first.
    await redis_client.hset(
        REDIS_LEGACY_HASH_KEY,
        mapping={"category_whitelist": "macro"},
    )
    # Dashboard pushes a more permissive whitelist via the JSON path.
    await redis_client.set(
        REDIS_KEY,
        json.dumps({"category_whitelist": "crypto,macro,sports"}),
    )

    cfg = RuntimeConfig(redis_client=redis_client)
    effective = await cfg.get("category_whitelist")
    assert effective == "crypto,macro,sports", (
        f"JSON precedence broken; got {effective!r} — the dashboard "
        "must out-rank the legacy hash."
    )


async def test_set_overrides_pubsub_invalidates_subscribed_cache(redis_client):
    """End-to-end engine-side wiring: writer's ``set_overrides`` publishes
    on ``runtime_config:changed``; reader's ``start_pubsub`` invalidates
    its cache; next read returns the fresh value WITHOUT waiting for the
    TTL. This is the audit Red Flag #6 contract."""
    # Reader side (engine).
    reader = RuntimeConfig(redis_client=redis_client)
    await reader.start_pubsub()
    try:
        # Prime the reader's cache.
        await reader.effective()

        # Writer side (API): different RuntimeConfig instance, same
        # Redis. set_overrides persists the JSON blob and publishes
        # the changed event.
        writer = RuntimeConfig(redis_client=redis_client)
        await writer.set_overrides({"category_whitelist": "crypto"})

        # Wait for the pub/sub round-trip to invalidate the reader's
        # cache. We do NOT sleep the full TTL — the whole point is
        # push-invalidation cuts the lag to <100 ms.
        ok = await _wait_until(
            lambda: reader._cache is None,
            timeout=2.0,
        )
        assert ok, "pub/sub invalidation did not clear the reader cache"

        # Next read pulls from Redis and returns the writer's value.
        value = await reader.get("category_whitelist")
        assert value == "crypto", (
            f"post-pubsub read returned stale {value!r} "
            "(expected 'crypto' from the writer)"
        )
    finally:
        await reader.stop_pubsub()


async def test_legacy_hash_typo_keys_are_dropped(redis_client):
    """A typo in the hand-edited hash (e.g. ``categry_whitelist``) must
    NOT poison the effective config — and must NOT cause the read path
    to raise. The dashboard JSON path has the same guarantee via
    ``set_overrides``; the legacy path needs it too."""
    await redis_client.hset(
        REDIS_LEGACY_HASH_KEY,
        mapping={
            "categry_whitelist": "macro",            # typo — must drop
            "category_whitelist": "crypto,macro",    # valid — must apply
            "totally_made_up": "yes",                # nonsense — must drop
        },
    )
    cfg = RuntimeConfig(redis_client=redis_client)
    eff = await cfg.effective()

    assert eff["category_whitelist"] == "crypto,macro"
    assert "categry_whitelist" not in eff
    assert "totally_made_up" not in eff


async def test_legacy_hash_type_coercion(redis_client):
    """Redis hash values are strings. The legacy reader must coerce them
    to the right Python type so callers downstream can ``int(...)`` /
    ``bool(...)`` / arithmetic them without surprises.

    The 2026-05-17 prod trace had ``max_consecutive_losses 3`` as a
    string in the hash; PaperTrader / RiskManager expect an int.
    """
    await redis_client.hset(
        REDIS_LEGACY_HASH_KEY,
        mapping={
            "category_whitelist": "crypto,macro",       # str
            "max_consecutive_losses": "3",              # int
            "risk_per_trade_pct": "0.015",              # float
            "strategy_conditional_confidence_enabled": "true",  # bool
        },
    )
    cfg = RuntimeConfig(redis_client=redis_client)
    eff = await cfg.effective()

    assert isinstance(eff["category_whitelist"], str)
    assert eff["category_whitelist"] == "crypto,macro"

    assert isinstance(eff["max_consecutive_losses"], int)
    assert eff["max_consecutive_losses"] == 3

    assert isinstance(eff["risk_per_trade_pct"], float)
    assert abs(eff["risk_per_trade_pct"] - 0.015) < 1e-9

    assert isinstance(eff["strategy_conditional_confidence_enabled"], bool)
    assert eff["strategy_conditional_confidence_enabled"] is True
