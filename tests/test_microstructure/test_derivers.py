"""Unit tests for :mod:`src.microstructure.derivers` — Round 11 § 3.2.

Each of the four bucket-emitting detectors gets driven by a synthetic
event stream that contains the pattern it's designed to flag. The
test then asserts:
  * The detector flagged the synthetic pattern.
  * The per-bucket counters are correct.
  * Negative-pattern events do NOT flag.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from src.microstructure.derivers import (
    CancelToFillRatioTracker,
    IcebergDetector,
    MicrostructureFeatureDeriver,
    OrderFlowImbalanceCalculator,
    PlaceToFillTimingTracker,
    SpoofDetector,
    truncate_to_bucket,
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
# 1. IcebergDetector                                                           #
# --------------------------------------------------------------------------- #


class TestIcebergDetector:
    def test_recovers_known_pattern(self):
        """Same wallet, same price, three small refills within 60 s and
        each at ~25% of the wallet's typical size → iceberg flagged.
        We bootstrap the EWMA with a big placement first so the small
        refills fall under the 50% gate.
        """
        det = IcebergDetector(window_s=60, min_refills=3)
        # First event sets a large typical size so subsequent small
        # placements fall under the 50% gate.
        det.observe(_evt("placed", size=400.0, offset_s=0))
        # Now three small placements at the same price within 60 s.
        for i, off in enumerate([1.0, 2.0, 3.0]):
            det.observe(
                _evt(
                    "placed",
                    size=80.0,
                    offset_s=off,
                    order_hash=f"r{i}",
                )
            )
        snap = det.flush_bucket()
        bucket = snap.get(("m1", "t1"))
        assert bucket is not None
        # The third small placement satisfied min_refills=3 (it sees a
        # window of 3 entries at the matched price level — the first
        # large entry doesn't share the price; we use same price=0.5
        # so all four share it, and the 3rd small placement is the
        # first to make len(window) == 4 ≥ 3).
        assert bucket.count >= 1
        assert bucket.total_size > 0

    def test_negative_pattern_no_flag(self):
        """One placement, no refills → no iceberg."""
        det = IcebergDetector(window_s=60, min_refills=3)
        det.observe(_evt("placed", size=100.0))
        snap = det.flush_bucket()
        assert snap.get(("m1", "t1")) is None

    def test_no_wallet_no_flag(self):
        """Placements without wallet attribution can't be bucketed by
        wallet → no iceberg detection on placements (spec § 3.1)."""
        det = IcebergDetector(window_s=60, min_refills=2)
        for _ in range(5):
            det.observe(_evt("placed", size=10.0, wallet=None))
        snap = det.flush_bucket()
        assert not snap


# --------------------------------------------------------------------------- #
# 2. SpoofDetector                                                             #
# --------------------------------------------------------------------------- #


class TestSpoofDetector:
    def test_flags_known_spoofer(self):
        """Build a reservoir of normal-size placements, then a single
        large+canceled-fast+zero-fill placement → spoof flagged."""
        det = SpoofDetector(cancel_limit_s=5, size_percentile=0.5)
        # Bootstrap the size reservoir with 10 small placements.
        for i in range(10):
            det.observe(
                _evt(
                    "placed",
                    size=50.0,
                    offset_s=float(i),
                    order_hash=f"normal{i}",
                )
            )
        # Big spoof: placed at 100s, cancelled at 102s, never filled.
        det.observe(
            _evt(
                "placed",
                size=10_000.0,
                offset_s=100.0,
                order_hash="spoof1",
            )
        )
        det.observe(
            _evt(
                "cancelled",
                size=10_000.0,
                offset_s=102.0,
                order_hash="spoof1",
            )
        )
        snap = det.flush_bucket()
        bucket = snap.get(("m1", "t1"))
        assert bucket is not None
        assert bucket.count >= 1
        assert bucket.total_size >= 10_000.0

    def test_no_flag_when_filled(self):
        """Large order placed, cancelled fast — but ALSO partially
        filled → not a spoof (the order saw real fills, the gate
        excludes it)."""
        det = SpoofDetector(cancel_limit_s=5, size_percentile=0.5)
        for i in range(10):
            det.observe(_evt("placed", size=50.0, offset_s=float(i), order_hash=f"n{i}"))
        det.observe(_evt("placed", size=10_000.0, offset_s=100.0, order_hash="s1"))
        det.observe(_evt("partial_fill", size=100.0, offset_s=101.0, order_hash="s1"))
        det.observe(_evt("cancelled", size=9_900.0, offset_s=102.0, order_hash="s1"))
        snap = det.flush_bucket()
        assert snap.get(("m1", "t1")) is None

    def test_no_flag_when_cancel_too_slow(self):
        """Large order placed, cancelled 10 s later (past 5 s gate) →
        not a spoof."""
        det = SpoofDetector(cancel_limit_s=5, size_percentile=0.5)
        for i in range(10):
            det.observe(_evt("placed", size=50.0, offset_s=float(i), order_hash=f"n{i}"))
        det.observe(_evt("placed", size=10_000.0, offset_s=100.0, order_hash="s2"))
        det.observe(_evt("cancelled", size=10_000.0, offset_s=110.0, order_hash="s2"))
        snap = det.flush_bucket()
        assert snap.get(("m1", "t1")) is None


# --------------------------------------------------------------------------- #
# 3. OrderFlowImbalanceCalculator                                              #
# --------------------------------------------------------------------------- #


class TestOFI:
    def test_pure_buy_pressure_positive(self):
        """Three big bid placements in a row → OFI mean should be
        positive (buy pressure)."""
        ofi = OrderFlowImbalanceCalculator(window_s=5)
        for i, off in enumerate([0, 1, 2]):
            ofi.observe(
                _evt(
                    "placed",
                    side="buy",
                    size=100.0,
                    offset_s=float(off),
                    order_hash=f"b{i}",
                )
            )
        snap = ofi.flush_bucket()
        bucket = snap.get(("m1", "t1"))
        assert bucket is not None
        summary = bucket.summary()
        assert summary is not None
        mean, mx, mn, std = summary
        # 3 cumulative samples: 100, 200, 300 → mean 200.
        assert mean == pytest.approx(200.0)
        assert mx == pytest.approx(300.0)
        assert mn == pytest.approx(100.0)

    def test_pure_sell_pressure_negative(self):
        ofi = OrderFlowImbalanceCalculator(window_s=5)
        for i, off in enumerate([0, 1, 2]):
            ofi.observe(
                _evt(
                    "placed",
                    side="sell",
                    size=100.0,
                    offset_s=float(off),
                    order_hash=f"s{i}",
                )
            )
        snap = ofi.flush_bucket()
        bucket = snap.get(("m1", "t1"))
        assert bucket is not None
        mean, mx, mn, std = bucket.summary()  # type: ignore[misc]
        # All negative — sells reduce the bid signal in our convention.
        assert mean < 0
        assert mn <= mean

    def test_window_pruning(self):
        """Events older than window_s drop out of the rolling sum."""
        ofi = OrderFlowImbalanceCalculator(window_s=5)
        # 3 buy placements far apart in time.
        ofi.observe(_evt("placed", side="buy", size=100, offset_s=0, order_hash="x1"))
        ofi.observe(_evt("placed", side="buy", size=100, offset_s=10, order_hash="x2"))
        ofi.observe(_evt("placed", side="buy", size=100, offset_s=20, order_hash="x3"))
        snap = ofi.flush_bucket()
        bucket = snap.get(("m1", "t1"))
        assert bucket is not None
        # Each event lives in its own 5 s window → 3 samples of value 100.
        mean, mx, mn, std = bucket.summary()  # type: ignore[misc]
        assert mean == pytest.approx(100.0)


# --------------------------------------------------------------------------- #
# 4. PlaceToFillTimingTracker                                                  #
# --------------------------------------------------------------------------- #


class TestPlaceToFill:
    def test_records_distribution_per_wallet(self):
        tracker = PlaceToFillTimingTracker()
        # Place at 0, fill at 30 — 30 s latency, wallet 0xa.
        tracker.observe(_evt("placed", offset_s=0, order_hash="o1", wallet="0xa"))
        tracker.observe(_evt("filled", offset_s=30, order_hash="o1", wallet="0xa"))
        # Place at 100, fill at 102 — 2 s latency, wallet 0xa.
        tracker.observe(_evt("placed", offset_s=100, order_hash="o2", wallet="0xa"))
        tracker.observe(_evt("filled", offset_s=102, order_hash="o2", wallet="0xa"))
        p50 = tracker.percentile_for_wallet("0xa", quantile=0.5)
        assert p50 is not None
        assert p50 in (2.0, 30.0)
        snapshot = tracker.per_wallet_snapshot()
        assert "0xa" in snapshot
        assert len(snapshot["0xa"]) == 2

    def test_cancel_clears_inflight(self):
        """A cancelled order should not show up in the place-to-fill
        distribution (it never filled)."""
        tracker = PlaceToFillTimingTracker()
        tracker.observe(_evt("placed", offset_s=0, order_hash="c1", wallet="0xa"))
        tracker.observe(_evt("cancelled", offset_s=2, order_hash="c1", wallet="0xa"))
        assert tracker.percentile_for_wallet("0xa") is None


# --------------------------------------------------------------------------- #
# 5. CancelToFillRatioTracker                                                  #
# --------------------------------------------------------------------------- #


class TestCancelToFillRatio:
    def test_basic_ratio(self):
        tracker = CancelToFillRatioTracker(window_s=1800)
        # 3 cancels, 1 fill — ratio = 3.0.
        for i, off in enumerate([0, 1, 2]):
            tracker.observe(
                _evt(
                    "cancelled",
                    offset_s=off,
                    order_hash=f"c{i}",
                    wallet="0xa",
                )
            )
        tracker.observe(_evt("filled", offset_s=3, order_hash="f1", wallet="0xa"))
        ratio = tracker.ratio_for_wallet("0xa")
        assert ratio == pytest.approx(3.0)

    def test_pure_cancel_returns_n_cancels(self):
        tracker = CancelToFillRatioTracker(window_s=1800)
        for i, off in enumerate([0, 1, 2]):
            tracker.observe(_evt("cancelled", offset_s=off, order_hash=f"c{i}", wallet="0xa"))
        ratio = tracker.ratio_for_wallet("0xa")
        # 3 cancels, 0 fills → sentinel = n_cancels = 3.0.
        assert ratio == pytest.approx(3.0)


# --------------------------------------------------------------------------- #
# 6. MicrostructureFeatureDeriver composition                                  #
# --------------------------------------------------------------------------- #


class TestComposition:
    def test_flush_bucket_returns_three_subdicts(self):
        deriver = MicrostructureFeatureDeriver()
        deriver.observe(_evt("placed", size=100.0))
        snap = deriver.flush_bucket()
        assert "iceberg" in snap
        assert "spoof" in snap
        assert "ofi" in snap

    def test_observe_batch(self):
        deriver = MicrostructureFeatureDeriver()
        events = [
            _evt("placed", offset_s=float(i), order_hash=f"o{i}") for i in range(5)
        ]
        deriver.observe_batch(events)
        assert deriver.events_seen() == 5


# --------------------------------------------------------------------------- #
# 7. Bucket boundary math                                                      #
# --------------------------------------------------------------------------- #


class TestBucketMath:
    def test_truncate_to_bucket_60s(self):
        ts = datetime(2026, 5, 12, 10, 30, 45, tzinfo=timezone.utc)
        assert truncate_to_bucket(ts, 60) == datetime(
            2026, 5, 12, 10, 30, 0, tzinfo=timezone.utc
        )

    def test_truncate_to_bucket_5s(self):
        ts = datetime(2026, 5, 12, 10, 30, 47, tzinfo=timezone.utc)
        assert truncate_to_bucket(ts, 5) == datetime(
            2026, 5, 12, 10, 30, 45, tzinfo=timezone.utc
        )
