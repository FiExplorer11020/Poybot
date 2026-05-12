"""Per-wallet state trackers — Round 11 § 3.2 (D + E).

Two trackers that don't emit per-bucket counters (unlike the
bucket-emitting iceberg / spoof / OFI detectors). They maintain
per-wallet state that the nightly :class:`WalletSignatureBatch`
reads to populate the
``wallet_microstructure_signature`` table.

Split out of :mod:`src.microstructure.derivers` so the main deriver
module stays under the 500-line file ceiling.

  * :class:`PlaceToFillTimingTracker` — per-wallet histogram of
    placement→fill seconds.
  * :class:`CancelToFillRatioTracker` — per-wallet rolling-30-min
    cancel/fill ratio. Tier-0/1 wallets only (the caller filters
    upstream; the tracker itself is wallet-agnostic).
"""

from __future__ import annotations

from collections import deque

from src.config import settings
from src.observer.clob_book_decoder import (
    EVENT_CANCELLED,
    EVENT_FILLED,
    EVENT_PARTIAL_FILL,
    EVENT_PLACED,
    BookEvent,
)
from src.microstructure._helpers import (
    _MAX_TRACKED_KEYS_PER_DETECTOR,
    _event_ts,
)


class _PlaceToFillRecord:
    """Lightweight named-tuple replacement (smaller than dataclass)."""

    __slots__ = ("placed_at", "wallet_address")

    def __init__(self, placed_at: float, wallet_address: str | None) -> None:
        self.placed_at = placed_at
        self.wallet_address = wallet_address


class PlaceToFillTimingTracker:
    """Place-to-fill timing — spec § 3.2.D.

    For each fill, record the elapsed seconds between the matching
    placement event and the fill event. Per-wallet distribution is
    materialised by the wallet signature batch; the detector itself
    only keeps the per-wallet deque of recent samples in memory so the
    nightly batch can compute p50/p99 cheaply.
    """

    _MAX_SAMPLES_PER_WALLET = 1_000

    def __init__(self) -> None:
        self._inflight: dict[str, _PlaceToFillRecord] = {}
        self._per_wallet_samples: dict[str, deque[float]] = {}

    def observe(self, event: BookEvent) -> bool:
        if event.event_type == EVENT_PLACED and event.order_hash:
            if len(self._inflight) >= _MAX_TRACKED_KEYS_PER_DETECTOR:
                try:
                    oldest = next(iter(self._inflight))
                    del self._inflight[oldest]
                except StopIteration:
                    pass
            self._inflight[event.order_hash] = _PlaceToFillRecord(
                placed_at=_event_ts(event),
                wallet_address=event.wallet_address,
            )
            return False
        if event.event_type in (EVENT_FILLED, EVENT_PARTIAL_FILL) and event.order_hash:
            record = self._inflight.get(event.order_hash)
            if record is None:
                return False
            elapsed = max(0.0, _event_ts(event) - record.placed_at)
            wallet = event.wallet_address or record.wallet_address
            if wallet is None:
                # Fill without wallet attribution → we cannot bucket it
                # per-wallet; skip rather than store under a sentinel
                # (would corrupt the per-wallet distribution).
                if event.event_type == EVENT_FILLED:
                    self._inflight.pop(event.order_hash, None)
                return False
            samples = self._per_wallet_samples.get(wallet)
            if samples is None:
                if len(self._per_wallet_samples) >= _MAX_TRACKED_KEYS_PER_DETECTOR:
                    try:
                        oldest = next(iter(self._per_wallet_samples))
                        del self._per_wallet_samples[oldest]
                    except StopIteration:
                        pass
                samples = deque(maxlen=self._MAX_SAMPLES_PER_WALLET)
                self._per_wallet_samples[wallet] = samples
            samples.append(elapsed)
            if event.event_type == EVENT_FILLED:
                self._inflight.pop(event.order_hash, None)
            return True
        if event.event_type == EVENT_CANCELLED and event.order_hash:
            self._inflight.pop(event.order_hash, None)
        return False

    def percentile_for_wallet(
        self, wallet: str, *, quantile: float = 0.5
    ) -> float | None:
        samples = self._per_wallet_samples.get(wallet)
        if not samples:
            return None
        sorted_samples = sorted(samples)
        idx = max(
            0, min(len(sorted_samples) - 1, int(quantile * len(sorted_samples)))
        )
        return sorted_samples[idx]

    def per_wallet_snapshot(self) -> dict[str, list[float]]:
        """Snapshot for the wallet-signature batch. Returns a dict of
        wallet → recent place-to-fill samples (in seconds)."""
        return {w: list(d) for w, d in self._per_wallet_samples.items()}


class CancelToFillRatioTracker:
    """Cancel-to-fill ratio — spec § 3.2.E.

    Rolling 30 min counters per wallet. Only tier-0/1 wallets should be
    fed into this tracker (the caller is responsible for filtering — the
    detector itself doesn't know about the tier; it just tracks every
    wallet it sees). The cardinality gate is enforced by the daemon
    composing the pipeline.
    """

    def __init__(self, *, window_s: int | None = None) -> None:
        self.window_s = int(
            window_s
            if window_s is not None
            else settings.MICROSTRUCTURE_CANCEL_TO_FILL_WINDOW_S
        )
        # Per-wallet rolling deques of (ts, kind) where kind ∈ {'c', 'f'}.
        self._events: dict[str, deque[tuple[float, str]]] = {}

    def observe(self, event: BookEvent) -> bool:
        wallet = event.wallet_address
        if not wallet:
            return False
        if event.event_type == EVENT_CANCELLED:
            kind = "c"
        elif event.event_type == EVENT_FILLED:
            kind = "f"
        else:
            return False
        ts = _event_ts(event)
        buf = self._events.get(wallet)
        if buf is None:
            if len(self._events) >= _MAX_TRACKED_KEYS_PER_DETECTOR:
                try:
                    oldest = next(iter(self._events))
                    del self._events[oldest]
                except StopIteration:
                    pass
            buf = deque()
            self._events[wallet] = buf
        buf.append((ts, kind))
        cutoff = ts - self.window_s
        while buf and buf[0][0] < cutoff:
            buf.popleft()
        return True

    def ratio_for_wallet(self, wallet: str) -> float | None:
        buf = self._events.get(wallet)
        if not buf:
            return None
        n_c = sum(1 for _t, k in buf if k == "c")
        n_f = sum(1 for _t, k in buf if k == "f")
        if n_f == 0 and n_c == 0:
            return None
        if n_f == 0:
            # Sentinel: pure-cancel wallets get a large finite value
            # (rather than +inf) so the DB column stays numeric.
            return float(n_c)
        return n_c / n_f

    def counts_for_wallet(self, wallet: str) -> tuple[int, int]:
        buf = self._events.get(wallet)
        if not buf:
            return (0, 0)
        n_c = sum(1 for _t, k in buf if k == "c")
        n_f = sum(1 for _t, k in buf if k == "f")
        return (n_c, n_f)
