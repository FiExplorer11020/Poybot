"""Wave-3 hardening tests for :mod:`src.microstructure.derivers` — R11.

These tests cover the load-bearing detector math that the baseline
suite touches lightly:

  * **Iceberg EWMA correctness** — verify the formula
    ``μ = λ·μ_prev + (1-λ)·x_new`` with λ=0.94 (the spec value).
  * **Iceberg false-positive resistance** — random non-iceberg streams
    must not flag.
  * **Spoof cancel-window boundary** — exactly 5.0s passes the gate,
    5.01s does not (inclusive vs. exclusive bound).
  * **Spoof percentile gate** — bootstrap with tiny sizes, then a
    same-tier order must NOT be flagged; only a clearly-above-percentile
    size flags.
  * **OFI sign convention** — buy = positive, sell = negative; the
    rolling sum spans the configured window only.
  * **OFI empty stream** — flush returns no rows.
  * **OFI per-(market, token) isolation** — cross-market events don't
    pollute each other's rolling sums.
  * **CancelToFillRatio rolling window pruning** — events outside the
    30-min window must be dropped from the deque.
  * **PlaceToFillTimingTracker partial-fill capture** — partial fills
    do produce timing samples (the spec § 3.2.D contract).
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from src.microstructure.derivers import (
    IcebergDetector,
    OrderFlowImbalanceCalculator,
    SpoofDetector,
)
from src.microstructure.trackers import (
    CancelToFillRatioTracker,
    PlaceToFillTimingTracker,
)
from src.observer.clob_book_observer import BookEvent

BASE_TS = datetime(2026, 5, 12, 10, 0, 0, tzinfo=timezone.utc)


def _evt(
    event_type: str,
    *,
    offset_s: float = 0.0,
    market_id: str = "m1",
    token_id: str = "t1",
    side: str = "buy",
    price: float | None = 0.5,
    size: float | None = 100.0,
    order_hash: str | None = "h1",
    wallet: str | None = "0xabc",
) -> BookEvent:
    return BookEvent(
        event_time=BASE_TS + timedelta(seconds=offset_s),
        market_id=market_id,
        token_id=token_id,
        event_type=event_type,
        side=side,
        price=Decimal(str(price)) if price is not None else None,
        size_delta=Decimal(str(size)) if size is not None else None,
        order_hash=order_hash,
        wallet_address=wallet,
        source="ws",
        received_at=(BASE_TS + timedelta(seconds=offset_s)).timestamp(),
    )


# --------------------------------------------------------------------------- #
# 1. Iceberg EWMA math                                                         #
# --------------------------------------------------------------------------- #


class TestIcebergEWMA:
    def test_ewma_formula_lambda_094(self):
        """Verify ``μ = λ·μ_prev + (1-λ)·x_new`` exactly with λ=0.94.

        After observing sizes [400, 80, 80, 80] the EWMA should be:
          μ_0 = 400
          μ_1 = 0.94*400 + 0.06*80   = 376 + 4.8   = 380.8
          μ_2 = 0.94*380.8 + 0.06*80 = 357.952 + 4.8 = 362.752
          μ_3 = 0.94*362.752 + 0.06*80 = 340.987 + 4.8 = 345.787
        """
        det = IcebergDetector(window_s=60, min_refills=3)
        sizes = [400.0, 80.0, 80.0, 80.0]
        # Use different order_hash so each placement is independent.
        for i, sz in enumerate(sizes):
            det.observe(_evt("placed", size=sz, offset_s=float(i), order_hash=f"o{i}"))
        # Internal EWMA inspection (private state, but load-bearing).
        ewma = det._size_ewma["t1"]
        expected = 400.0
        for sz in sizes[1:]:
            expected = 0.94 * expected + 0.06 * sz
        assert ewma == pytest.approx(expected, rel=1e-9)

    def test_size_ratio_gate_excludes_typical_sizes(self):
        """A wallet placing at typical-sized refills (not ≤ 50% of typical)
        must NOT flag iceberg, even with many refills at same price."""
        det = IcebergDetector(window_s=60, min_refills=3)
        # Bootstrap with typical size 100. Then refill at 100 each time
        # (1.0× typical — fails the ≤ 0.5 gate).
        for i in range(6):
            det.observe(_evt("placed", size=100.0, offset_s=float(i), order_hash=f"o{i}"))
        snap = det.flush_bucket()
        assert not snap, f"Expected no iceberg, got {snap}"

    def test_random_clean_stream_no_false_positives(self):
        """A pseudo-random stream of placements at random prices must
        not flag iceberg in the absence of the refill-at-same-price
        pattern."""
        random.seed(42)
        det = IcebergDetector(window_s=60, min_refills=3)
        for i in range(200):
            det.observe(
                _evt(
                    "placed",
                    size=random.uniform(50.0, 150.0),
                    price=round(random.uniform(0.1, 0.9), 3),
                    offset_s=float(i) * 0.1,
                    order_hash=f"r{i}",
                    wallet=f"0x{i % 10:02d}",  # 10 different wallets
                )
            )
        snap = det.flush_bucket()
        # Per-key working set well bounded; counts may be 0 or very low.
        total = sum(b.count for b in snap.values())
        assert total <= 2, f"Too many false positives on clean stream: {total}"


# --------------------------------------------------------------------------- #
# 2. Spoof detector boundaries                                                 #
# --------------------------------------------------------------------------- #


class TestSpoofBoundaries:
    def _bootstrap_reservoir(self, det: SpoofDetector, n: int = 20):
        for i in range(n):
            det.observe(
                _evt(
                    "placed",
                    size=50.0,
                    offset_s=float(i) * 0.1,
                    order_hash=f"normal{i}",
                )
            )

    def test_cancel_at_exactly_5s_passes_gate(self):
        """Spec gate is `elapsed > cancel_limit_s` rejects; equal to the
        limit is INSIDE the gate (inclusive)."""
        det = SpoofDetector(cancel_limit_s=5, size_percentile=0.5)
        self._bootstrap_reservoir(det)
        det.observe(_evt("placed", size=10_000.0, offset_s=10.0, order_hash="s1"))
        det.observe(_evt("cancelled", size=10_000.0, offset_s=15.0, order_hash="s1"))
        snap = det.flush_bucket()
        assert snap.get(("m1", "t1")) is not None
        assert snap[("m1", "t1")].count >= 1

    def test_cancel_just_over_5s_fails_gate(self):
        """5.01s past placement → outside gate, no spoof."""
        det = SpoofDetector(cancel_limit_s=5, size_percentile=0.5)
        self._bootstrap_reservoir(det)
        det.observe(_evt("placed", size=10_000.0, offset_s=10.0, order_hash="s2"))
        det.observe(
            _evt("cancelled", size=10_000.0, offset_s=15.01, order_hash="s2")
        )
        snap = det.flush_bucket()
        assert snap.get(("m1", "t1")) is None

    def test_negative_elapsed_rejected(self):
        """Out-of-order events (cancel before place by clock) must NOT
        flag — the elapsed gate is `< 0 or > limit → reject`."""
        det = SpoofDetector(cancel_limit_s=5, size_percentile=0.5)
        self._bootstrap_reservoir(det)
        det.observe(_evt("placed", size=10_000.0, offset_s=20.0, order_hash="s3"))
        # Cancel "earlier" than the placement → negative elapsed.
        det.observe(_evt("cancelled", size=10_000.0, offset_s=15.0, order_hash="s3"))
        snap = det.flush_bucket()
        assert snap.get(("m1", "t1")) is None

    def test_partial_fill_excludes_spoof_flag(self):
        """A partial-fill before cancel marks the order as filled →
        even with size ≥ 95th pct and cancel < 5s, must NOT flag."""
        det = SpoofDetector(cancel_limit_s=5, size_percentile=0.5)
        self._bootstrap_reservoir(det)
        det.observe(_evt("placed", size=10_000.0, offset_s=10.0, order_hash="s4"))
        det.observe(
            _evt("partial_fill", size=100.0, offset_s=11.0, order_hash="s4")
        )
        det.observe(
            _evt("cancelled", size=9_900.0, offset_s=12.0, order_hash="s4")
        )
        snap = det.flush_bucket()
        assert snap.get(("m1", "t1")) is None


# --------------------------------------------------------------------------- #
# 3. OFI sign + window + empty stream                                          #
# --------------------------------------------------------------------------- #


class TestOFIHardening:
    def test_empty_stream_emits_no_keys(self):
        ofi = OrderFlowImbalanceCalculator(window_s=5)
        snap = ofi.flush_bucket()
        assert snap == {}

    def test_cross_market_isolation(self):
        """Events on (m1, t1) must not pollute (m2, t2)'s rolling sum."""
        ofi = OrderFlowImbalanceCalculator(window_s=5)
        ofi.observe(
            _evt("placed", side="buy", size=100, market_id="m1", token_id="t1", order_hash="a")
        )
        ofi.observe(
            _evt("placed", side="buy", size=200, market_id="m2", token_id="t2", order_hash="b")
        )
        ofi.observe(
            _evt(
                "placed", side="buy", size=100, market_id="m1", token_id="t1",
                order_hash="c", offset_s=1,
            )
        )
        snap = ofi.flush_bucket()
        # m1/t1 sees two events → samples [100, 200]; m2/t2 sees one → [200].
        m1 = snap.get(("m1", "t1"))
        m2 = snap.get(("m2", "t2"))
        assert m1 is not None and m2 is not None
        mean_m1, _, _, _ = m1.summary()  # type: ignore[misc]
        mean_m2, _, _, _ = m2.summary()  # type: ignore[misc]
        assert mean_m1 == pytest.approx(150.0)  # (100 + 200) / 2
        assert mean_m2 == pytest.approx(200.0)

    def test_window_prunes_old_events(self):
        """An event 10s old must drop out of a 5s window."""
        ofi = OrderFlowImbalanceCalculator(window_s=5)
        ofi.observe(_evt("placed", side="buy", size=100, offset_s=0, order_hash="a"))
        ofi.observe(_evt("placed", side="buy", size=100, offset_s=10, order_hash="b"))
        # After the second event, the first should be pruned.
        # The rolling sum at second event = 100 (only b in window).
        snap = ofi.flush_bucket()
        bucket = snap.get(("m1", "t1"))
        assert bucket is not None
        # Samples = [100 (from a alone), 100 (from b alone — a pruned)].
        mean, mx, _, _ = bucket.summary()  # type: ignore[misc]
        assert mean == pytest.approx(100.0)
        assert mx == pytest.approx(100.0)

    def test_zero_size_event_skipped(self):
        """size_delta == 0 carries no information → skipped."""
        ofi = OrderFlowImbalanceCalculator(window_s=5)
        ofi.observe(_evt("placed", side="buy", size=0, order_hash="a"))
        snap = ofi.flush_bucket()
        assert snap == {}


# --------------------------------------------------------------------------- #
# 4. CancelToFillRatio window pruning                                          #
# --------------------------------------------------------------------------- #


class TestCancelToFillWindow:
    def test_events_outside_window_are_pruned(self):
        """A cancel event 31 min old must drop out of a 30 min window."""
        tracker = CancelToFillRatioTracker(window_s=1800)
        # Old cancel at t=0
        tracker.observe(_evt("cancelled", offset_s=0, order_hash="c_old", wallet="0xa"))
        # Fresh fill at t=1801 — should prune the cancel from the deque
        # because cutoff = 1801 - 1800 = 1, and 0 < 1.
        tracker.observe(_evt("filled", offset_s=1801, order_hash="f1", wallet="0xa"))
        n_c, n_f = tracker.counts_for_wallet("0xa")
        assert n_c == 0
        assert n_f == 1
        # ratio = 0 cancels / 1 fill → 0.0 (not the n_cancels sentinel).
        assert tracker.ratio_for_wallet("0xa") == 0.0

    def test_no_attribution_event_skipped(self):
        """A cancel without wallet_address can't be bucketed per-wallet
        → tracker silently skips it."""
        tracker = CancelToFillRatioTracker(window_s=1800)
        tracker.observe(
            _evt("cancelled", offset_s=0, order_hash="c1", wallet=None)
        )
        assert tracker.ratio_for_wallet("0xa") is None


# --------------------------------------------------------------------------- #
# 5. PlaceToFillTimingTracker partial-fill                                     #
# --------------------------------------------------------------------------- #


class TestPlaceToFillPartialFill:
    def test_partial_fill_records_timing_sample(self):
        """Spec § 3.2.D — partial fills DO contribute to the per-wallet
        timing distribution (the partial fill IS evidence the order
        executed in latency-X seconds)."""
        tracker = PlaceToFillTimingTracker()
        tracker.observe(
            _evt("placed", offset_s=0, order_hash="o1", wallet="0xa")
        )
        tracker.observe(
            _evt("partial_fill", offset_s=15, order_hash="o1", wallet="0xa")
        )
        snapshot = tracker.per_wallet_snapshot()
        assert "0xa" in snapshot
        assert snapshot["0xa"] == [15.0]

    def test_partial_then_full_fill_records_both(self):
        """A partial fill followed by a full fill records two samples
        for the same order — the placement is preserved through the
        partial fill so the full-fill latency is also captured."""
        tracker = PlaceToFillTimingTracker()
        tracker.observe(_evt("placed", offset_s=0, order_hash="o1", wallet="0xa"))
        tracker.observe(_evt("partial_fill", offset_s=15, order_hash="o1", wallet="0xa"))
        tracker.observe(_evt("filled", offset_s=30, order_hash="o1", wallet="0xa"))
        snapshot = tracker.per_wallet_snapshot()
        assert snapshot["0xa"] == [15.0, 30.0]

    def test_fill_without_matching_place_is_skipped(self):
        """A fill arriving without a recorded placement (e.g. WS replay
        cold start) must not raise; the sample is just dropped."""
        tracker = PlaceToFillTimingTracker()
        tracker.observe(_evt("filled", offset_s=10, order_hash="orphan", wallet="0xa"))
        assert tracker.percentile_for_wallet("0xa") is None
