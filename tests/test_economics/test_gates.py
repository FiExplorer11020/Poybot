from datetime import datetime, timedelta, timezone
from decimal import Decimal

from src.economics.gates import BookSnapshotRef, evaluate_signal_gate
from src.economics.models import FeeSnapshot, StrategyTrack


def _fee_snapshot(captured_at: datetime | None = None) -> FeeSnapshot:
    now = datetime.now(tz=timezone.utc)
    return FeeSnapshot(
        market_id="m1",
        token_id="t1",
        fee_enabled=True,
        fee_rate=Decimal("0.04"),
        source="unit-test",
        captured_at=captured_at or now,
    )


def _book(captured_at: datetime | None = None) -> BookSnapshotRef:
    now = datetime.now(tz=timezone.utc)
    return BookSnapshotRef(
        market_id="m1",
        token_id="t1",
        best_bid=Decimal("0.54"),
        best_ask=Decimal("0.56"),
        captured_at=captured_at or now,
        source="unit-test",
    )


def test_signal_gate_rejects_missing_fee_snapshot():
    audit = evaluate_signal_gate(
        strategy_track=StrategyTrack.LEADER_SWING,
        market_id="m1",
        token_id="t1",
        token_map_ok=True,
        fee_snapshot=None,
        book_snapshot=_book(),
    )

    assert audit.accepted is False
    assert audit.reject_reason == "missing_fee_snapshot"


def test_signal_gate_rejects_stale_book():
    now = datetime.now(tz=timezone.utc)

    audit = evaluate_signal_gate(
        strategy_track=StrategyTrack.MICRO_REACTIVE,
        market_id="m1",
        token_id="t1",
        token_map_ok=True,
        fee_snapshot=_fee_snapshot(now),
        book_snapshot=_book(now - timedelta(seconds=30)),
        now=now,
        max_book_age_s=5,
    )

    assert audit.accepted is False
    assert audit.reject_reason == "stale_book"


def test_signal_gate_accepts_when_fee_token_map_and_book_are_valid():
    now = datetime.now(tz=timezone.utc)

    audit = evaluate_signal_gate(
        strategy_track=StrategyTrack.LEADER_SWING,
        market_id="m1",
        token_id="t1",
        token_map_ok=True,
        fee_snapshot=_fee_snapshot(now),
        book_snapshot=_book(now),
        now=now,
    )

    assert audit.accepted is True
    assert audit.reject_reason is None
    assert audit.fee_snapshot is not None
    assert audit.book_snapshot is not None
