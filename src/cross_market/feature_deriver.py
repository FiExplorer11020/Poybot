"""Round 12 — Per-wallet cross-market feature derivation (spec § 4.4).

Output dict matches the J. CROSS_MARKET slot names appended to R8's
:data:`FEATURE_NAMES` by R12:

  * ``active_venue_count``         — number of venues the operator
                                     trades on within the lookback
                                     window.
  * ``cross_venue_correlation``    — same-direction-on-same-event rate
                                     across paired (polymarket, kalshi)
                                     positions.
  * ``cross_venue_lag_s``          — Kalshi → Polymarket median lag in
                                     seconds (negative = Kalshi LEADS).

Pure-Python compute over rows; the feature_store reader handles SQL.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any


@dataclass(frozen=True)
class CrossMarketFeatures:
    """Output shape matching the J. CROSS_MARKET slot names exactly."""

    active_venue_count: int
    cross_venue_correlation: float | None
    cross_venue_lag_s: float | None

    def as_dict(self) -> dict[str, float | int | None]:
        return {
            "active_venue_count": int(self.active_venue_count),
            "cross_venue_correlation": self.cross_venue_correlation,
            "cross_venue_lag_s": self.cross_venue_lag_s,
        }


def _ensure_utc(ts: Any) -> datetime | None:
    if ts is None:
        return None
    if isinstance(ts, datetime):
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    return None


def derive_features(
    cross_market_rows: list[dict[str, Any]],
    polymarket_trades: list[dict[str, Any]],
    *,
    asof_ts: datetime,
    lookback_days: int,
) -> CrossMarketFeatures:
    """Compute the three J. CROSS_MARKET feature values from rows.

    Args:
      cross_market_rows:  list of cross_market_positions rows (dict-shaped).
                          Must carry ``venue``, ``market_id``, ``side``,
                          ``opened_at`` (or ``snapshot_at``).
      polymarket_trades:  list of trades_observed rows for the same
                          operator's Polymarket wallet. Must carry
                          ``time``, ``market_id``, ``side``.
      asof_ts:            cutoff for both sources.
      lookback_days:      window length (matches R12 default 30).

    Returns:
      :class:`CrossMarketFeatures`. ``cross_venue_correlation`` /
      ``cross_venue_lag_s`` may be None when the operator has no
      paired (polymarket, kalshi) positions in the window.
    """
    if asof_ts.tzinfo is None:
        asof_ts = asof_ts.replace(tzinfo=timezone.utc)
    floor = asof_ts - timedelta(days=max(1, int(lookback_days)))

    # active_venue_count — distinct venues with at least one position
    # snapshot in [floor, asof].
    venues: set[str] = set()
    by_venue: dict[str, list[dict[str, Any]]] = {}
    for row in cross_market_rows:
        ts = _ensure_utc(row.get("opened_at")) or _ensure_utc(
            row.get("snapshot_at")
        )
        if ts is None or ts < floor or ts > asof_ts:
            continue
        venue = str(row.get("venue") or "")
        if not venue:
            continue
        venues.add(venue)
        by_venue.setdefault(venue, []).append({**row, "_ts": ts})
    # Polymarket trades count as venue presence too.
    pm_in_window: list[dict[str, Any]] = []
    for t in polymarket_trades:
        ts = _ensure_utc(t.get("time"))
        if ts is None or ts < floor or ts > asof_ts:
            continue
        pm_in_window.append({**t, "_ts": ts})
    if pm_in_window:
        venues.add("polymarket")
    active_venue_count = len(venues)

    # Cross-venue correlation: for each (market_id) where we see BOTH a
    # Polymarket trade AND a Kalshi position, count "same direction".
    # We use a fuzzy market match (string equality on market_id) — in
    # production the wallet resolver should provide a venue→market
    # canonical mapping, but for the deriver we treat string matches
    # conservatively.
    kalshi_rows = by_venue.get("kalshi", [])
    pm_by_market: dict[str, list[dict[str, Any]]] = {}
    for t in pm_in_window:
        pm_by_market.setdefault(str(t.get("market_id") or ""), []).append(t)

    paired_total = 0
    paired_same_dir = 0
    lags_s: list[float] = []
    for k_row in kalshi_rows:
        market_id = str(k_row.get("market_id") or "")
        if not market_id:
            continue
        pm_trades_for_market = pm_by_market.get(market_id, [])
        if not pm_trades_for_market:
            continue
        k_side = str(k_row.get("side") or "").lower()
        k_ts = k_row["_ts"]
        for pm in pm_trades_for_market:
            paired_total += 1
            pm_side = str(pm.get("side") or "").lower()
            # Polymarket trades carry 'buy' / 'sell'. We map them to
            # yes/no via the trade-side conservative heuristic: 'buy'
            # → directional towards 'yes', 'sell' → directional towards
            # 'no'. The deriver is a feature; the operator's matcher
            # is the source of truth.
            pm_dir = "yes" if pm_side == "buy" else "no"
            if pm_dir == k_side:
                paired_same_dir += 1
            # Lag: Kalshi → Polymarket (positive = Kalshi LEADS by
            # `lag` seconds). Per spec § 4.4 commentary; sign convention
            # mirrors the social lag (negative = Kalshi leads).
            pm_ts = pm["_ts"]
            lags_s.append((pm_ts - k_ts).total_seconds())

    correlation: float | None = None
    if paired_total > 0:
        correlation = paired_same_dir / paired_total

    lag_median: float | None = None
    if lags_s:
        lag_median = float(statistics.median(lags_s))

    return CrossMarketFeatures(
        active_venue_count=active_venue_count,
        cross_venue_correlation=correlation,
        cross_venue_lag_s=lag_median,
    )


def features_to_feature_store_dict(
    features: CrossMarketFeatures,
) -> dict[str, float | int | None]:
    """Translate the dataclass to the dict shape that the R8 features
    extractor consumes."""
    return features.as_dict()


__all__ = [
    "CrossMarketFeatures",
    "derive_features",
    "features_to_feature_store_dict",
]
