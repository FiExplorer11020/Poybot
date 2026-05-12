"""Round 12 — Cross-market position aggregator (spec § 4.3).

For each *confirmed* operator in ``cross_market_operators``, fetch their
positions across every reachable venue and write a unified snapshot row
to ``cross_market_positions``.

"Confirmed" = ``confidence >= CROSS_MARKET_MIN_RESOLUTION_CONFIDENCE``.
Pending-review fingerprint matches do NOT trigger production polls.

Schema fields per spec § 4.3 + migration 037:
  operator_id, venue, market_id, side, size_usdc, opened_at, closed_at,
  snapshot_at.

Each venue client returns venue-specific payload shapes; the aggregator
normalises them via per-venue translators kept short + close to the
adapter call.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from loguru import logger

from src.config import settings
from src.cross_market.kalshi_client import KalshiClient
from src.cross_market.manifold_client import ManifoldClient
from src.cross_market.predictit_client import PredictItClient
from src.database.connection import get_db


# Defensive metric imports.
try:
    from src.monitoring.metrics import (  # type: ignore[attr-defined]
        crossmarket_positions_observed_total,
        crossmarket_venues_reachable,
    )
except Exception:  # pragma: no cover
    class _NoOp:
        def labels(self, *_a, **_kw):
            return self

        def inc(self, *_a, **_kw):
            return None

        def set(self, *_a, **_kw):
            return None

    crossmarket_positions_observed_total = _NoOp()  # type: ignore[assignment]
    crossmarket_venues_reachable = _NoOp()  # type: ignore[assignment]


def _kalshi_position_to_row(
    operator_id: int, raw: dict[str, Any], snapshot_at: datetime
) -> dict[str, Any] | None:
    try:
        market_id = str(raw.get("ticker") or raw.get("market_id") or "")
        if not market_id:
            return None
        position = raw.get("position") or 0
        # Kalshi's position is signed (positive = YES, negative = NO).
        try:
            pos_i = int(position)
        except (TypeError, ValueError):
            return None
        side = "yes" if pos_i > 0 else "no" if pos_i < 0 else "flat"
        size = abs(float(raw.get("market_exposure") or raw.get("volume") or 0.0))
        opened = raw.get("created_time")
        closed = raw.get("closed_time")
        return {
            "operator_id": operator_id,
            "venue": "kalshi",
            "market_id": market_id,
            "side": side,
            "size_usdc": size,
            "opened_at": _parse_ts(opened),
            "closed_at": _parse_ts(closed),
            "snapshot_at": snapshot_at,
        }
    except Exception:
        return None


def _manifold_bet_to_row(
    operator_id: int, raw: dict[str, Any], snapshot_at: datetime
) -> dict[str, Any] | None:
    try:
        market_id = str(raw.get("contractId") or "")
        if not market_id:
            return None
        outcome = str(raw.get("outcome") or "").lower()
        side = "yes" if outcome in ("yes", "y") else "no" if outcome in ("no", "n") else outcome
        amount = float(raw.get("amount") or 0.0)
        opened = raw.get("createdTime")
        if isinstance(opened, (int, float)):
            opened_dt: datetime | None = datetime.fromtimestamp(
                float(opened) / 1000.0, tz=timezone.utc
            ) if opened > 1e10 else datetime.fromtimestamp(float(opened), tz=timezone.utc)
        else:
            opened_dt = _parse_ts(opened)
        return {
            "operator_id": operator_id,
            "venue": "manifold",
            "market_id": market_id,
            "side": side,
            "size_usdc": amount,  # mana ≈ USD for scale; operator can rescale
            "opened_at": opened_dt,
            "closed_at": None,
            "snapshot_at": snapshot_at,
        }
    except Exception:
        return None


def _parse_ts(value: Any) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except Exception:
            return None
    return None


class CrossMarketPositionAggregator:
    """Hourly aggregator over ``cross_market_operators``.

    Each :meth:`run_once` cycle:
      1. Loads operators with confidence ≥
         ``CROSS_MARKET_MIN_RESOLUTION_CONFIDENCE``.
      2. Calls each venue client with the operator's per-venue handle.
      3. Normalises payloads to the unified row shape.
      4. Inserts into ``cross_market_positions`` in one batch per
         operator.

    Venue client unavailability is tolerated — a missing client simply
    skips that venue this cycle.
    """

    def __init__(
        self,
        *,
        kalshi: KalshiClient | None = None,
        manifold: ManifoldClient | None = None,
        predictit: PredictItClient | None = None,
        min_confidence: float | None = None,
    ) -> None:
        self.kalshi = kalshi
        self.manifold = manifold
        self.predictit = predictit
        self._min_conf = float(
            min_confidence
            if min_confidence is not None
            else settings.CROSS_MARKET_MIN_RESOLUTION_CONFIDENCE
        )
        self.positions_written: int = 0

    async def _load_operators(self) -> list[dict[str, Any]]:
        try:
            async with get_db() as conn:
                rows = await conn.fetch(
                    """
                    SELECT operator_id,
                           polymarket_wallet, kalshi_account,
                           manifold_handle, predictit_account,
                           x_handle, resolution_source, confidence
                    FROM cross_market_operators
                    WHERE confidence >= $1
                      AND (resolution_source IN ('manual', 'profile_link')
                           OR confidence >= $1)
                    """,
                    self._min_conf,
                )
            return [dict(r) for r in rows]
        except Exception as exc:
            logger.warning(
                f"CrossMarketPositionAggregator: load operators failed: {exc}"
            )
            return []

    async def _aggregate_operator(
        self, operator: dict[str, Any], snapshot_at: datetime
    ) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        op_id = int(operator["operator_id"])
        if self.kalshi is not None and operator.get("kalshi_account"):
            for raw in await self.kalshi.fetch_wallet_positions(
                operator["kalshi_account"]
            ):
                row = _kalshi_position_to_row(op_id, raw, snapshot_at)
                if row is not None:
                    rows.append(row)
        if self.manifold is not None and operator.get("manifold_handle"):
            for raw in await self.manifold.fetch_wallet_positions(
                operator["manifold_handle"]
            ):
                row = _manifold_bet_to_row(op_id, raw, snapshot_at)
                if row is not None:
                    rows.append(row)
        # PredictIt deliberately produces no per-operator rows
        # (regulator-imposed; see PredictItClient docstring).
        return rows

    async def _persist_rows(self, rows: list[dict[str, Any]]) -> int:
        if not rows:
            return 0
        written = 0
        try:
            async with get_db() as conn:
                for row in rows:
                    try:
                        await conn.execute(
                            """
                            INSERT INTO cross_market_positions (
                                operator_id, venue, market_id, side,
                                size_usdc, opened_at, closed_at, snapshot_at
                            ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                            """,
                            row["operator_id"],
                            row["venue"],
                            row["market_id"],
                            row["side"],
                            row["size_usdc"],
                            row.get("opened_at"),
                            row.get("closed_at"),
                            row["snapshot_at"],
                        )
                        written += 1
                        try:
                            crossmarket_positions_observed_total.labels(
                                venue=row["venue"]
                            ).inc()
                        except Exception:  # pragma: no cover
                            pass
                    except Exception as exc:
                        logger.debug(
                            f"CrossMarketPositionAggregator: persist failed for "
                            f"op={row['operator_id']} venue={row['venue']}: {exc}"
                        )
        except Exception as exc:
            logger.warning(
                f"CrossMarketPositionAggregator: DB connect failed: {exc}"
            )
        self.positions_written += written
        return written

    async def run_once(self) -> dict[str, int]:
        """One aggregator pass. Returns a small summary:
        {n_operators, n_rows_written}."""
        snapshot_at = datetime.now(tz=timezone.utc)
        operators = await self._load_operators()
        n_rows = 0
        for op in operators:
            rows = await self._aggregate_operator(op, snapshot_at)
            n_rows += await self._persist_rows(rows)
        # Surface venues_reachable gauge as a side-effect.
        reachable = sum(
            1 for c in (self.kalshi, self.manifold, self.predictit) if c is not None
        )
        try:
            crossmarket_venues_reachable.set(reachable)
        except Exception:  # pragma: no cover
            pass
        return {
            "n_operators": len(operators),
            "n_rows_written": n_rows,
        }


__all__ = ["CrossMarketPositionAggregator"]
