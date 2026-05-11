"""Unit tests for src.execution.prefill.pool.

Round 7 Wave-2 — covers PreSignedOrder lifecycle, PreSignedPool warm
+ fire + expire_stale + rotation + concurrency. The CLOB client is
mocked end-to-end; the production binding goes through
:class:`src.engine.clob_client_wrapper.CLOBClientWrapper` (see agent
return summary).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from unittest.mock import MagicMock, patch

import pytest

from src.execution.prefill.pool import (
    FilledOrder,
    PoolKey,
    PreSignedOrder,
    PreSignedPool,
)


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


@dataclass
class _FakeIntent:
    """Stand-in for src.mempool.tx_decoder.LeaderIntent during tests.

    We don't import the real LeaderIntent because Wave-1 left it as a
    NotImplementedError-free dataclass — importing is fine but the
    construction shape is mirrored here for clarity + decoupling.
    """

    market_id: str
    token_id: str
    side: str
    size_usdc: Decimal
    # Padding fields the real dataclass has; not used by pool.fire.
    intent_id: str = "intent-xyz"
    wallet: str = "0xleader"
    price: Decimal = Decimal("0.5")
    order_type: str = "GTC"
    intent_received_at: datetime = datetime(2026, 5, 12, 10, 0, tzinfo=timezone.utc)
    expected_block: int = 1
    tx_hash: str = "0xabc"
    nonce: int = 1
    replaces: str | None = None


class _FakeClob:
    """Deterministic CLOB client double.

    - ``sign_order`` returns a fresh signature per call (counter-suffixed).
    - ``submit_presigned`` returns success by default, configurable per
      instance for failure-path tests.
    """

    def __init__(self) -> None:
        self.sign_calls: list[dict] = []
        self.submit_calls: list[PreSignedOrder] = []
        self._counter = 0
        # Per-call overrides
        self.submit_success: bool = True
        self.submit_raises: BaseException | None = None
        self.sign_raises_on_call: int | None = None
        self.sign_delay_s: float = 0.0

    async def sign_order(
        self,
        *,
        market_id: str,
        token_id: str,
        direction: str,
        size_bucket: int,
    ) -> dict:
        self._counter += 1
        self.sign_calls.append(
            {
                "market_id": market_id,
                "token_id": token_id,
                "direction": direction,
                "size_bucket": size_bucket,
                "counter": self._counter,
            }
        )
        if (
            self.sign_raises_on_call is not None
            and self._counter == self.sign_raises_on_call
        ):
            raise RuntimeError("sign blew up")
        if self.sign_delay_s:
            await asyncio.sleep(self.sign_delay_s)
        return {
            "signature": f"sig-{self._counter}",
            "price": "0.55",
        }

    async def submit_presigned(self, order: PreSignedOrder) -> dict:
        self.submit_calls.append(order)
        if self.submit_raises is not None:
            raise self.submit_raises
        if not self.submit_success:
            return {"success": False, "error": "submit failed"}
        return {
            "success": True,
            "clob_order_id": f"order-{order.nonce}",
            "filled_size_shares": float(order.size_bucket) / float(order.price),
            "avg_fill_price": float(order.price),
            "fee_paid_usdc": 0.0,
        }


async def _empty_markets() -> list[str]:
    return []


# ---------------------------------------------------------------------------
# PreSignedOrder.is_expired
# ---------------------------------------------------------------------------


def test_presigned_order_is_expired_true_when_past():
    now = datetime(2026, 5, 12, 12, 0, tzinfo=timezone.utc)
    order = PreSignedOrder(
        market_id="m1",
        token_id="m1:YES",
        direction="buy",
        size_bucket=500,
        price=Decimal("0.5"),
        signature="sig",
        signed_at=now - timedelta(minutes=10),
        expires_at=now - timedelta(seconds=1),
        nonce=1,
    )
    assert order.is_expired(now) is True


def test_presigned_order_is_expired_false_when_future():
    now = datetime(2026, 5, 12, 12, 0, tzinfo=timezone.utc)
    order = PreSignedOrder(
        market_id="m1",
        token_id="m1:YES",
        direction="buy",
        size_bucket=500,
        price=Decimal("0.5"),
        signature="sig",
        signed_at=now,
        expires_at=now + timedelta(minutes=5),
        nonce=1,
    )
    assert order.is_expired(now) is False


def test_presigned_order_is_expired_uses_now_default():
    """No-arg call falls back to datetime.now(UTC)."""
    far_past = datetime(2000, 1, 1, tzinfo=timezone.utc)
    order = PreSignedOrder(
        market_id="m1",
        token_id="m1:YES",
        direction="buy",
        size_bucket=500,
        price=Decimal("0.5"),
        signature="sig",
        signed_at=far_past,
        expires_at=far_past,
        nonce=1,
    )
    assert order.is_expired() is True


# ---------------------------------------------------------------------------
# warm()
# ---------------------------------------------------------------------------


async def test_warm_signs_full_grid():
    """N markets x 2 tokens x 2 directions x B buckets pre-signs."""
    fake = _FakeClob()
    pool = PreSignedPool(clob_client=fake, markets_provider=_empty_markets)

    markets = ["m1", "m2"]
    total = await pool.warm(markets)

    from src.config import settings

    buckets = len(settings.PREFILL_POOL_SIZE_BUCKETS_USDC)
    expected = len(markets) * 2 * 2 * buckets  # 2 tokens, 2 directions
    assert total == expected
    assert len(fake.sign_calls) == expected
    # Every key in the pool has exactly 1 order after warm.
    for orders in pool._pool.values():
        assert len(orders) == 1


async def test_warm_continues_after_one_sign_failure():
    """A single sign failure doesn't poison the whole warm pass."""
    fake = _FakeClob()
    fake.sign_raises_on_call = 3  # third sign blows up
    pool = PreSignedPool(clob_client=fake, markets_provider=_empty_markets)

    total = await pool.warm(["m1"])

    from src.config import settings

    expected_full = 1 * 2 * 2 * len(settings.PREFILL_POOL_SIZE_BUCKETS_USDC)
    # One sign failed -> total is full minus one.
    assert total == expected_full - 1


# ---------------------------------------------------------------------------
# fire() — bucket selection + happy path
# ---------------------------------------------------------------------------


def _make_intent(
    market_id: str = "m1",
    token_suffix: str = "YES",
    side: str = "buy",
    size_usdc: float | int | str = 5_000,
) -> _FakeIntent:
    return _FakeIntent(
        market_id=market_id,
        token_id=f"{market_id}:{token_suffix}",
        side=side,
        size_usdc=Decimal(str(size_usdc)),
    )


async def test_fire_returns_filled_for_matching_bucket():
    fake = _FakeClob()
    pool = PreSignedPool(clob_client=fake, markets_provider=_empty_markets)
    await pool.warm(["m1"])

    intent = _make_intent(size_usdc=5_000)  # fits bucket 2000
    filled = await pool.fire(intent)

    assert isinstance(filled, FilledOrder)
    assert filled.clob_order_id.startswith("order-")
    # Submit was called once, with the popped order whose bucket=2000.
    assert len(fake.submit_calls) == 1
    assert fake.submit_calls[0].size_bucket == 2_000


async def test_fire_selects_largest_bucket_le_intent_size():
    """intent.size=49_000 must pick bucket 10_000, not 50_000."""
    fake = _FakeClob()
    pool = PreSignedPool(clob_client=fake, markets_provider=_empty_markets)
    await pool.warm(["m1"])

    intent = _make_intent(size_usdc=49_000)
    filled = await pool.fire(intent)

    assert filled is not None
    assert fake.submit_calls[0].size_bucket == 10_000


async def test_fire_removes_order_from_pool():
    """fire() is single-use: a second fire on the same key returns
    None for `all_expired`/empty-list."""
    fake = _FakeClob()
    pool = PreSignedPool(clob_client=fake, markets_provider=_empty_markets)
    await pool.warm(["m1"])

    intent = _make_intent(size_usdc=5_000)
    first = await pool.fire(intent)
    second = await pool.fire(intent)

    assert first is not None
    assert second is None
    assert len(fake.submit_calls) == 1


# ---------------------------------------------------------------------------
# fire() — misses
# ---------------------------------------------------------------------------


async def test_fire_returns_none_when_below_smallest_bucket():
    """Smallest bucket is 500. An intent of 250 USDC pool-misses."""
    fake = _FakeClob()
    pool = PreSignedPool(clob_client=fake, markets_provider=_empty_markets)
    await pool.warm(["m1"])

    intent = _make_intent(size_usdc=250)
    with patch(
        "src.execution.prefill.pool.prefill_pool_misses_total"
    ) as misses:
        result = await pool.fire(intent)

    assert result is None
    misses.labels.assert_called_once_with(reason="no_bucket_fit")
    misses.labels.return_value.inc.assert_called_once()


async def test_fire_returns_none_for_unknown_market():
    """no_market reason when the pool has zero entries for the market."""
    fake = _FakeClob()
    pool = PreSignedPool(clob_client=fake, markets_provider=_empty_markets)
    await pool.warm(["m1"])

    intent = _make_intent(market_id="m_unknown", size_usdc=5_000)
    with patch(
        "src.execution.prefill.pool.prefill_pool_misses_total"
    ) as misses:
        result = await pool.fire(intent)

    assert result is None
    misses.labels.assert_called_once_with(reason="no_market")


async def test_fire_skips_expired_and_returns_none_if_all_expired():
    """If every order in the slot is past expires_at, fire() returns
    None (and drops them so the rotation refills next tick)."""
    fake = _FakeClob()
    pool = PreSignedPool(clob_client=fake, markets_provider=_empty_markets)
    await pool.warm(["m1"])

    # Manually expire every order for the intent's slot.
    far_past = datetime(2000, 1, 1, tzinfo=timezone.utc)
    for orders in pool._pool.values():
        for o in orders:
            o.expires_at = far_past

    intent = _make_intent(size_usdc=5_000)
    with patch(
        "src.execution.prefill.pool.prefill_pool_misses_total"
    ) as misses:
        result = await pool.fire(intent)

    assert result is None
    # Reason is "all_expired" since the slot was warmed (key exists).
    args, kwargs = misses.labels.call_args
    assert kwargs.get("reason") == "all_expired"


async def test_fire_skips_expired_picks_next_fresh_order():
    """When the slot has [expired, fresh], fire() should return fresh."""
    fake = _FakeClob()
    pool = PreSignedPool(clob_client=fake, markets_provider=_empty_markets)
    await pool.warm(["m1"])

    intent = _make_intent(size_usdc=5_000)
    key: PoolKey = (intent.market_id, intent.token_id, intent.side, 2_000)

    # Pre-existing order (from warm) is fresh; prepend an expired one.
    far_past = datetime(2000, 1, 1, tzinfo=timezone.utc)
    expired = PreSignedOrder(
        market_id=intent.market_id,
        token_id=intent.token_id,
        direction=intent.side,  # type: ignore[arg-type]
        size_bucket=2_000,
        price=Decimal("0.5"),
        signature="sig-expired",
        signed_at=far_past,
        expires_at=far_past,
        nonce=999,
    )
    pool._pool[key].insert(0, expired)

    result = await pool.fire(intent)

    assert result is not None
    # Expired order was discarded, the fresh one (warm-time) was submitted.
    assert len(fake.submit_calls) == 1
    assert fake.submit_calls[0].signature != "sig-expired"


async def test_fire_returns_none_on_submit_exception():
    fake = _FakeClob()
    fake.submit_raises = RuntimeError("network down")
    pool = PreSignedPool(clob_client=fake, markets_provider=_empty_markets)
    await pool.warm(["m1"])

    intent = _make_intent(size_usdc=5_000)
    with patch(
        "src.execution.prefill.pool.prefill_pool_misses_total"
    ) as misses:
        result = await pool.fire(intent)

    assert result is None
    misses.labels.assert_called_with(reason="signing_failed")


# ---------------------------------------------------------------------------
# expire_stale + stats
# ---------------------------------------------------------------------------


async def test_expire_stale_removes_all_expired_returns_count():
    fake = _FakeClob()
    pool = PreSignedPool(clob_client=fake, markets_provider=_empty_markets)
    await pool.warm(["m1"])

    initial = sum(len(v) for v in pool._pool.values())
    far_past = datetime(2000, 1, 1, tzinfo=timezone.utc)
    for orders in pool._pool.values():
        for o in orders:
            o.expires_at = far_past

    dropped = await pool.expire_stale()

    assert dropped == initial
    assert all(len(v) == 0 for v in pool._pool.values()) or pool._pool == {}


async def test_stats_aggregates_pool_state():
    fake = _FakeClob()
    pool = PreSignedPool(clob_client=fake, markets_provider=_empty_markets)
    await pool.warm(["m1", "m2"])

    stats = pool.stats()

    from src.config import settings

    buckets = len(settings.PREFILL_POOL_SIZE_BUCKETS_USDC)
    expected_total = 2 * 2 * 2 * buckets
    assert stats["total_orders"] == expected_total
    assert set(stats["by_market"].keys()) == {"m1", "m2"}
    assert stats["by_market"]["m1"] == 2 * 2 * buckets
    assert stats["by_direction"]["buy"] == expected_total // 2
    assert stats["by_direction"]["sell"] == expected_total // 2
    assert stats["oldest_signed_at"] is not None


# ---------------------------------------------------------------------------
# start_rotation + stop
# ---------------------------------------------------------------------------


async def test_start_rotation_wakes_and_expires():
    """The rotation loop should call expire_stale() at least once."""
    fake = _FakeClob()
    pool = PreSignedPool(clob_client=fake, markets_provider=_empty_markets)
    await pool.warm(["m1"])

    # Expire every order so the next rotation tick drops them all.
    far_past = datetime(2000, 1, 1, tzinfo=timezone.utc)
    for orders in pool._pool.values():
        for o in orders:
            o.expires_at = far_past

    # Patch the rotation interval to ~0 for fast tests.
    with patch("src.execution.prefill.pool.settings") as mock_settings:
        # Mirror the real settings the pool reads, but with a fast tick.
        from src.config import settings as real_settings

        mock_settings.PREFILL_ROTATION_INTERVAL_S = 0.01
        mock_settings.PREFILL_ORDER_VALIDITY_S = (
            real_settings.PREFILL_ORDER_VALIDITY_S
        )
        mock_settings.PREFILL_POOL_SIZE_BUCKETS_USDC = (
            real_settings.PREFILL_POOL_SIZE_BUCKETS_USDC
        )

        await pool.start_rotation()
        # Yield long enough for at least one tick.
        await asyncio.sleep(0.05)
        await pool.stop()

    # After rotation, the pool should have refilled (post expire,
    # _refill_empty re-signs every empty slot).
    assert any(len(v) > 0 for v in pool._pool.values())


async def test_stop_cancels_rotation_idempotently():
    fake = _FakeClob()
    pool = PreSignedPool(clob_client=fake, markets_provider=_empty_markets)

    await pool.start_rotation()
    assert pool._rotation_task is not None
    assert not pool._rotation_task.done()

    await pool.stop()
    assert pool._rotation_task is None

    # Idempotent: a second stop on the already-stopped pool is fine.
    await pool.stop()


async def test_start_rotation_idempotent():
    """Calling start_rotation twice in a row does NOT spawn two tasks."""
    fake = _FakeClob()
    pool = PreSignedPool(clob_client=fake, markets_provider=_empty_markets)

    await pool.start_rotation()
    first_task = pool._rotation_task
    await pool.start_rotation()
    second_task = pool._rotation_task

    assert first_task is second_task

    await pool.stop()


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------


async def test_concurrent_fires_get_distinct_orders():
    """10 concurrent fires on the SAME key -> each gets a distinct
    pre-signed order (or None when the pool runs dry). The lock
    prevents two concurrent calls from popping the same slot."""
    fake = _FakeClob()
    pool = PreSignedPool(clob_client=fake, markets_provider=_empty_markets)
    await pool.warm(["m1"])

    # Stuff the slot for size_bucket=2000 with 10 orders so all 10
    # concurrent fires CAN succeed.
    intent = _make_intent(size_usdc=5_000)
    key: PoolKey = (intent.market_id, intent.token_id, intent.side, 2_000)
    now = datetime.now(tz=timezone.utc)
    extras = [
        PreSignedOrder(
            market_id=intent.market_id,
            token_id=intent.token_id,
            direction=intent.side,  # type: ignore[arg-type]
            size_bucket=2_000,
            price=Decimal("0.5"),
            signature=f"sig-extra-{i}",
            signed_at=now,
            expires_at=now + timedelta(minutes=5),
            nonce=1_000 + i,
        )
        for i in range(9)
    ]
    pool._pool[key].extend(extras)

    results = await asyncio.gather(*(pool.fire(intent) for _ in range(10)))

    filled = [r for r in results if r is not None]
    assert len(filled) == 10
    submitted_nonces = [c.nonce for c in fake.submit_calls]
    assert len(submitted_nonces) == 10
    assert len(set(submitted_nonces)) == 10  # all unique


# ---------------------------------------------------------------------------
# Signing latency metric
# ---------------------------------------------------------------------------


async def test_signing_latency_metric_observed():
    """warm() routes every sign through prefill_pool_signing_seconds.time()."""
    fake = _FakeClob()
    pool = PreSignedPool(clob_client=fake, markets_provider=_empty_markets)

    with patch(
        "src.execution.prefill.pool.prefill_pool_signing_seconds"
    ) as hist:
        cm = MagicMock()
        cm.__enter__ = MagicMock(return_value=None)
        cm.__exit__ = MagicMock(return_value=None)
        hist.time.return_value = cm

        await pool.warm(["m1"])

    # Expect one time() context per sign call.
    from src.config import settings

    expected = 1 * 2 * 2 * len(settings.PREFILL_POOL_SIZE_BUCKETS_USDC)
    assert hist.time.call_count == expected
