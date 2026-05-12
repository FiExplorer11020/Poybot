"""Microstructure feature derivers — Round 11 § 3.2.

Five detectors with a common :meth:`observe` interface that takes a
:class:`BookEvent` and updates in-memory state. The composer
:class:`MicrostructureFeatureDeriver` exposes the aggregated state to
the :class:`~src.microstructure.rollup.MicrostructureRollup` (60 s by
default). Taxonomy (spec § 3.2):

  * A. ICEBERG     — same wallet refilling at the same price level.
  * B. SPOOF       — large + cancelled-within-5s + zero-fill placement.
  * C. OFI         — signed top-of-book size delta, 5 s rolling.
  * D. PLACE→FILL  — per-fill latency distribution (in :mod:`trackers`).
  * E. CANCEL/FILL — per-wallet rolling cancel/fill ratio (in :mod:`trackers`).

Memory: each detector caps its per-key working set at
``_MAX_TRACKED_KEYS_PER_DETECTOR`` (50,000) so a long-running daemon
never accumulates unbounded state. The per-wallet trackers live in
:mod:`src.microstructure.trackers` to keep this module under the
500-line ceiling.
"""

from __future__ import annotations

import math
import time
from collections import defaultdict, deque
from dataclasses import dataclass, field
from typing import Iterable

from loguru import logger

from src.config import settings
from src.observer.clob_book_decoder import (
    EVENT_CANCELLED,
    EVENT_FILLED,
    EVENT_MODIFIED,
    EVENT_PARTIAL_FILL,
    EVENT_PLACED,
    BookEvent,
)
from src.microstructure._helpers import (
    _MAX_TRACKED_KEYS_PER_DETECTOR,
    _event_ts,
    _to_float,
    next_bucket_boundary,
    truncate_to_bucket,
)
from src.microstructure.trackers import (
    CancelToFillRatioTracker,
    PlaceToFillTimingTracker,
)

try:
    from src.monitoring.metrics import (  # type: ignore[attr-defined]
        iceberg_detections_total,
        ofi_calculations_per_minute,
        spoof_detections_total,
    )
except Exception:  # pragma: no cover

    class _NoOpLabel:
        def labels(self, *_args, **_kwargs):
            return self

        def inc(self, *_args, **_kwargs):
            return None

        def set(self, *_args, **_kwargs):
            return None

    iceberg_detections_total = _NoOpLabel()  # type: ignore[assignment]
    spoof_detections_total = _NoOpLabel()  # type: ignore[assignment]
    ofi_calculations_per_minute = _NoOpLabel()  # type: ignore[assignment]


@dataclass(slots=True)
class IcebergBucket:
    """Per-(wallet, market, token, price) iceberg accumulator within the
    current rollup bucket. Resets each time
    :meth:`IcebergDetector.flush_bucket` is called."""

    count: int = 0
    total_size: float = 0.0


@dataclass(slots=True)
class _SpoofRecord:
    placed_at: float
    side: str
    size: float
    market_id: str
    token_id: str
    wallet_address: str | None
    filled: bool = False


class IcebergDetector:
    """Iceberg detection — spec § 3.2.A.

    Rolling window of ``MICROSTRUCTURE_ICEBERG_WINDOW_S`` seconds per
    (wallet, market, token, price). When the same wallet places
    ``MICROSTRUCTURE_ICEBERG_MIN_REFILLS`` orders at the same price within
    the window AND each order is ≤ 50% of the typical (median) size on
    that token, we flag the pattern as iceberg activity and increment
    the per-bucket counters.
    """

    EWMA_LAMBDA = 0.94  # ~15 day half-life at per-day cadence
    SIZE_RATIO_GATE = 0.5  # each iceberg slice ≤ 50% of typical
    _MISSING_PRICE = -1.0

    def __init__(
        self,
        *,
        window_s: int | None = None,
        min_refills: int | None = None,
    ) -> None:
        self.window_s = int(
            window_s if window_s is not None else settings.MICROSTRUCTURE_ICEBERG_WINDOW_S
        )
        self.min_refills = int(
            min_refills
            if min_refills is not None
            else settings.MICROSTRUCTURE_ICEBERG_MIN_REFILLS
        )
        self._windows: dict[tuple[str, str, str, float], deque[float]] = {}
        self._size_ewma: dict[str, float] = {}
        self._bucket: dict[tuple[str, str], IcebergBucket] = defaultdict(IcebergBucket)

    def observe(self, event: BookEvent) -> bool:
        if event.event_type != EVENT_PLACED:
            return False
        if event.wallet_address is None:
            return False
        size = _to_float(event.size_delta)
        price = _to_float(event.price)
        if size is None or size <= 0:
            return False
        # Update size EWMA.
        prior = self._size_ewma.get(event.token_id)
        self._size_ewma[event.token_id] = (
            self.EWMA_LAMBDA * prior + (1.0 - self.EWMA_LAMBDA) * size
            if prior is not None
            else size
        )
        typical = self._size_ewma[event.token_id]

        key = (
            event.wallet_address,
            event.market_id,
            event.token_id,
            price if price is not None else self._MISSING_PRICE,
        )
        ts = _event_ts(event)
        window = self._windows.get(key)
        if window is None:
            if len(self._windows) >= _MAX_TRACKED_KEYS_PER_DETECTOR:
                try:
                    oldest = next(iter(self._windows))
                    del self._windows[oldest]
                except StopIteration:
                    pass
            window = deque()
            self._windows[key] = window
        window.append(ts)
        cutoff = ts - self.window_s
        while window and window[0] < cutoff:
            window.popleft()

        if (
            typical > 0
            and (size / typical) <= self.SIZE_RATIO_GATE
            and len(window) >= self.min_refills
        ):
            bucket = self._bucket[(event.market_id, event.token_id)]
            bucket.count += 1
            bucket.total_size += size
            try:
                iceberg_detections_total.inc()
            except Exception:  # pragma: no cover
                pass
            return True
        return False

    def flush_bucket(self) -> dict[tuple[str, str], IcebergBucket]:
        out = dict(self._bucket)
        self._bucket = defaultdict(IcebergBucket)
        return out


@dataclass(slots=True)
class SpoofBucket:
    count: int = 0
    total_size: float = 0.0


class SpoofDetector:
    """Spoof detection — spec § 3.2.B.

    Tracks every placement; when it's followed by a cancellation within
    ``MICROSTRUCTURE_SPOOF_CANCEL_LIMIT_S`` seconds AND the order saw
    zero fills AND the placement size was at-or-above
    ``MICROSTRUCTURE_SPOOF_SIZE_PERCENTILE`` of the rolling distribution,
    we flag the pattern.

    Repeated spoofs on the opposite side increase the score (the
    classic spoof pattern is alternating bid/ask manipulation).
    """

    _RESERVOIR_SIZE = 256

    def __init__(
        self,
        *,
        cancel_limit_s: int | None = None,
        size_percentile: float | None = None,
    ) -> None:
        self.cancel_limit_s = int(
            cancel_limit_s
            if cancel_limit_s is not None
            else settings.MICROSTRUCTURE_SPOOF_CANCEL_LIMIT_S
        )
        self.size_percentile = float(
            size_percentile
            if size_percentile is not None
            else settings.MICROSTRUCTURE_SPOOF_SIZE_PERCENTILE
        )
        self._inflight: dict[str, _SpoofRecord] = {}
        self._size_reservoir: dict[tuple[str, str], deque[float]] = {}
        self._last_spoof_side: dict[str, str] = {}
        self._bucket: dict[tuple[str, str], SpoofBucket] = defaultdict(SpoofBucket)

    def _track_size(self, market_id: str, token_id: str, size: float) -> None:
        key = (market_id, token_id)
        reservoir = self._size_reservoir.get(key)
        if reservoir is None:
            if len(self._size_reservoir) >= _MAX_TRACKED_KEYS_PER_DETECTOR:
                try:
                    oldest = next(iter(self._size_reservoir))
                    del self._size_reservoir[oldest]
                except StopIteration:
                    pass
            reservoir = deque(maxlen=self._RESERVOIR_SIZE)
            self._size_reservoir[key] = reservoir
        reservoir.append(size)

    def _size_at_percentile(self, market_id: str, token_id: str) -> float | None:
        reservoir = self._size_reservoir.get((market_id, token_id))
        if not reservoir or len(reservoir) < 8:
            return None
        sorted_sizes = sorted(reservoir)
        idx = max(
            0,
            min(len(sorted_sizes) - 1, int(self.size_percentile * len(sorted_sizes))),
        )
        return sorted_sizes[idx]

    def observe(self, event: BookEvent) -> bool:
        size = _to_float(event.size_delta)
        if event.event_type == EVENT_PLACED:
            if size is None or size <= 0:
                return False
            self._track_size(event.market_id, event.token_id, size)
            key = event.order_hash or f"synthetic::{event.market_id}::{event.token_id}::{_event_ts(event):.6f}"
            if len(self._inflight) >= _MAX_TRACKED_KEYS_PER_DETECTOR:
                try:
                    oldest = next(iter(self._inflight))
                    del self._inflight[oldest]
                except StopIteration:
                    pass
            self._inflight[key] = _SpoofRecord(
                placed_at=_event_ts(event),
                side=event.side,
                size=size,
                market_id=event.market_id,
                token_id=event.token_id,
                wallet_address=event.wallet_address,
            )
            return False

        if event.event_type in (EVENT_PARTIAL_FILL, EVENT_FILLED):
            key = event.order_hash
            if key is None:
                return False
            record = self._inflight.get(key)
            if record is not None:
                record.filled = True
            if event.event_type == EVENT_FILLED:
                self._inflight.pop(key, None)
            return False

        if event.event_type == EVENT_CANCELLED:
            key = event.order_hash
            if key is None:
                return False
            record = self._inflight.pop(key, None)
            if record is None:
                return False
            cancelled_at = _event_ts(event)
            elapsed = cancelled_at - record.placed_at
            if elapsed < 0 or elapsed > self.cancel_limit_s:
                return False
            if record.filled:
                return False
            gate = self._size_at_percentile(record.market_id, record.token_id)
            if gate is None or record.size < gate:
                return False
            bucket = self._bucket[(record.market_id, record.token_id)]
            bucket.count += 1
            bucket.total_size += record.size
            # Alternation bump — repeated spoof on opposite side from
            # the wallet's last spoof = classic spoof pattern.
            if record.wallet_address:
                last_side = self._last_spoof_side.get(record.wallet_address)
                if last_side and last_side != record.side:
                    bucket.count += 1
                    bucket.total_size += record.size
                self._last_spoof_side[record.wallet_address] = record.side
            try:
                spoof_detections_total.inc()
            except Exception:  # pragma: no cover
                pass
            return True

        return False

    def flush_bucket(self) -> dict[tuple[str, str], SpoofBucket]:
        out = dict(self._bucket)
        self._bucket = defaultdict(SpoofBucket)
        return out


@dataclass(slots=True)
class OFIBucket:
    """OFI accumulators per (market, token) for one rollup bucket."""

    samples: list[float] = field(default_factory=list)

    def add(self, value: float) -> None:
        self.samples.append(value)

    def summary(self) -> tuple[float, float, float, float] | None:
        if not self.samples:
            return None
        n = len(self.samples)
        mean = sum(self.samples) / n
        mx = max(self.samples)
        mn = min(self.samples)
        if n == 1:
            std = 0.0
        else:
            mean_sq = sum(x * x for x in self.samples) / n
            std = math.sqrt(max(0.0, mean_sq - mean * mean))
        return mean, mx, mn, std


class OrderFlowImbalanceCalculator:
    """Order flow imbalance — spec § 3.2.C.

    OFI for a (market, token) over a 5 s rolling window is
    ``sum(signed_size_delta)`` where ``signed_size_delta`` is the
    placement size on the bid (positive) or ask (negative) side. We
    compute a rolling OFI value per detected event and aggregate it
    over the rollup bucket.
    """

    def __init__(self, *, window_s: int | None = None) -> None:
        self.window_s = int(
            window_s if window_s is not None else settings.MICROSTRUCTURE_OFI_WINDOW_S
        )
        self._signed_buffers: dict[tuple[str, str], deque[tuple[float, float]]] = {}
        self._bucket: dict[tuple[str, str], OFIBucket] = defaultdict(OFIBucket)
        self._calc_timestamps: deque[float] = deque(maxlen=10_000)

    def observe(self, event: BookEvent) -> bool:
        if event.event_type not in (EVENT_PLACED, EVENT_MODIFIED, EVENT_CANCELLED):
            return False
        size = _to_float(event.size_delta)
        if size is None or size == 0:
            return False
        ts = _event_ts(event)
        # Sign convention: bid increase positive, ask increase negative.
        # size_delta is already negative for cancels (set by the decoder),
        # so the side-sign multiplier captures the four quadrants.
        side_sign = 1.0 if event.side == "buy" else -1.0
        signed = side_sign * size

        key = (event.market_id, event.token_id)
        buf = self._signed_buffers.get(key)
        if buf is None:
            if len(self._signed_buffers) >= _MAX_TRACKED_KEYS_PER_DETECTOR:
                try:
                    oldest = next(iter(self._signed_buffers))
                    del self._signed_buffers[oldest]
                except StopIteration:
                    pass
            buf = deque()
            self._signed_buffers[key] = buf
        buf.append((ts, signed))
        cutoff = ts - self.window_s
        while buf and buf[0][0] < cutoff:
            buf.popleft()
        rolling = sum(v for _t, v in buf)
        self._bucket[key].add(rolling)
        self._calc_timestamps.append(ts)
        return True

    def rate_per_minute(self, *, now_s: float | None = None) -> float:
        now_s = now_s if now_s is not None else time.time()
        cutoff = now_s - 60.0
        return float(sum(1 for ts in self._calc_timestamps if ts >= cutoff))

    def flush_bucket(self) -> dict[tuple[str, str], OFIBucket]:
        out = dict(self._bucket)
        self._bucket = defaultdict(OFIBucket)
        try:
            ofi_calculations_per_minute.set(self.rate_per_minute())
        except Exception:  # pragma: no cover
            pass
        return out


class MicrostructureFeatureDeriver:
    """Composes the four bucket-emitting detectors plus the two state-only
    trackers (place-to-fill, cancel/fill) into a single observable. Each
    incoming :class:`BookEvent` is offered to every detector; the
    aggregated state can be flushed into a per-bucket snapshot for the
    rollup writer.

    The class is intentionally framework-free — no Redis, no DB, no
    asyncio. The daemon module wires the I/O around it.
    """

    def __init__(
        self,
        *,
        iceberg: IcebergDetector | None = None,
        spoof: SpoofDetector | None = None,
        ofi: OrderFlowImbalanceCalculator | None = None,
        place_to_fill: PlaceToFillTimingTracker | None = None,
        cancel_to_fill: CancelToFillRatioTracker | None = None,
    ) -> None:
        self.iceberg = iceberg or IcebergDetector()
        self.spoof = spoof or SpoofDetector()
        self.ofi = ofi or OrderFlowImbalanceCalculator()
        self.place_to_fill = place_to_fill or PlaceToFillTimingTracker()
        self.cancel_to_fill = cancel_to_fill or CancelToFillRatioTracker()
        self._events_seen: int = 0

    def observe(self, event: BookEvent) -> None:
        self._events_seen += 1
        try:
            self.iceberg.observe(event)
        except Exception as exc:
            logger.debug(f"iceberg.observe failed: {exc}")
        try:
            self.spoof.observe(event)
        except Exception as exc:
            logger.debug(f"spoof.observe failed: {exc}")
        try:
            self.ofi.observe(event)
        except Exception as exc:
            logger.debug(f"ofi.observe failed: {exc}")
        try:
            self.place_to_fill.observe(event)
        except Exception as exc:
            logger.debug(f"place_to_fill.observe failed: {exc}")
        try:
            self.cancel_to_fill.observe(event)
        except Exception as exc:
            logger.debug(f"cancel_to_fill.observe failed: {exc}")

    def observe_batch(self, events: Iterable[BookEvent]) -> None:
        for event in events:
            self.observe(event)

    def flush_bucket(self) -> dict:
        """Hand the rollup writer everything it needs for the current
        bucket. The dict has three keys:

          * ``iceberg`` — dict[(market_id, token_id), IcebergBucket]
          * ``spoof``   — dict[(market_id, token_id), SpoofBucket]
          * ``ofi``     — dict[(market_id, token_id), OFIBucket]
        """
        return {
            "iceberg": self.iceberg.flush_bucket(),
            "spoof": self.spoof.flush_bucket(),
            "ofi": self.ofi.flush_bucket(),
        }

    def events_seen(self) -> int:
        return self._events_seen


