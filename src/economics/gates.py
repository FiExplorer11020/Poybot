from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from src.economics.models import FeeSnapshot, StrategyTrack


@dataclass(frozen=True)
class BookSnapshotRef:
    market_id: str
    token_id: str
    best_bid: Decimal
    best_ask: Decimal
    captured_at: datetime
    source: str
    reference: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SignalAudit:
    strategy_track: StrategyTrack
    market_id: str
    token_id: str
    accepted: bool
    reject_reason: str | None
    fee_snapshot: FeeSnapshot | None = None
    book_snapshot: BookSnapshotRef | None = None
    cost_assumptions: dict[str, Any] = field(default_factory=dict)
    inputs: dict[str, Any] = field(default_factory=dict)

    def to_metadata(self) -> dict[str, Any]:
        return {
            "strategy_track": self.strategy_track.value,
            "market_id": self.market_id,
            "token_id": self.token_id,
            "accepted": self.accepted,
            "reject_reason": self.reject_reason,
            "fee_source": self.fee_snapshot.source if self.fee_snapshot else None,
            "book_source": self.book_snapshot.source if self.book_snapshot else None,
            "cost_assumptions": dict(self.cost_assumptions),
            "inputs": dict(self.inputs),
        }


def _utc_now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _age_seconds(captured_at: datetime, now: datetime) -> float:
    if captured_at.tzinfo is None:
        captured_at = captured_at.replace(tzinfo=timezone.utc)
    return max(0.0, (now - captured_at).total_seconds())


def _reject(
    *,
    strategy_track: StrategyTrack,
    market_id: str,
    token_id: str,
    reason: str,
    fee_snapshot: FeeSnapshot | None = None,
    book_snapshot: BookSnapshotRef | None = None,
) -> SignalAudit:
    return SignalAudit(
        strategy_track=strategy_track,
        market_id=market_id,
        token_id=token_id,
        accepted=False,
        reject_reason=reason,
        fee_snapshot=fee_snapshot,
        book_snapshot=book_snapshot,
    )


def evaluate_signal_gate(
    *,
    strategy_track: StrategyTrack,
    market_id: str,
    token_id: str,
    token_map_ok: bool,
    fee_snapshot: FeeSnapshot | None,
    book_snapshot: BookSnapshotRef | None,
    now: datetime | None = None,
    max_book_age_s: float = 10.0,
    max_fee_age_s: float = 24 * 60 * 60,
) -> SignalAudit:
    """Validate the minimum economic inputs required before paper/dry-run execution."""
    now = now or _utc_now()
    track = StrategyTrack(strategy_track)

    if not market_id or not token_id or not token_map_ok:
        return _reject(
            strategy_track=track,
            market_id=market_id,
            token_id=token_id,
            reason="missing_token_map",
            fee_snapshot=fee_snapshot,
            book_snapshot=book_snapshot,
        )

    if fee_snapshot is None:
        return _reject(
            strategy_track=track,
            market_id=market_id,
            token_id=token_id,
            reason="missing_fee_snapshot",
            book_snapshot=book_snapshot,
        )

    if fee_snapshot.market_id != market_id or fee_snapshot.token_id != token_id:
        return _reject(
            strategy_track=track,
            market_id=market_id,
            token_id=token_id,
            reason="fee_snapshot_mismatch",
            fee_snapshot=fee_snapshot,
            book_snapshot=book_snapshot,
        )

    if _age_seconds(fee_snapshot.captured_at, now) > max_fee_age_s:
        return _reject(
            strategy_track=track,
            market_id=market_id,
            token_id=token_id,
            reason="stale_fee_snapshot",
            fee_snapshot=fee_snapshot,
            book_snapshot=book_snapshot,
        )

    if book_snapshot is None:
        return _reject(
            strategy_track=track,
            market_id=market_id,
            token_id=token_id,
            reason="missing_book_snapshot",
            fee_snapshot=fee_snapshot,
        )

    if book_snapshot.market_id != market_id or book_snapshot.token_id != token_id:
        return _reject(
            strategy_track=track,
            market_id=market_id,
            token_id=token_id,
            reason="book_snapshot_mismatch",
            fee_snapshot=fee_snapshot,
            book_snapshot=book_snapshot,
        )

    if _age_seconds(book_snapshot.captured_at, now) > max_book_age_s:
        return _reject(
            strategy_track=track,
            market_id=market_id,
            token_id=token_id,
            reason="stale_book",
            fee_snapshot=fee_snapshot,
            book_snapshot=book_snapshot,
        )

    return SignalAudit(
        strategy_track=track,
        market_id=market_id,
        token_id=token_id,
        accepted=True,
        reject_reason=None,
        fee_snapshot=fee_snapshot,
        book_snapshot=book_snapshot,
        cost_assumptions={
            "max_book_age_s": max_book_age_s,
            "max_fee_age_s": max_fee_age_s,
        },
    )
