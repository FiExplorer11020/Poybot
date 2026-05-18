"""Plan 2026-05-19 P3 — leader-horizon-aware exit strategy.

The current paper_trader exits on:
  * holding cap (12h non-sport / 30 min sport)
  * stop_loss (-8% FOLLOW / -5% FADE / -3% sport)
  * take_profit (+10%)
  * leader_exit, market_resolved, near-resolution preclose

The investigation agent confirmed: the leader's classified `horizon`
(scalper / swing / holder) is computed and persisted in
`leaders.classification_json` but NEVER consulted at exit time. A
scalper leader holding 30-90 min has their copy held for 12h — past
the leader's own profit window.

This module exports two functions:

* ``resolve_holding_cap_for_horizon`` — adapts holding_cap_s based on
  the leader's horizon. Scalpers get a tighter cap (1h), holders stay
  on the legacy 12h. Sport caps still take priority on sport markets.

* ``check_trailing_stop`` — runs alongside the static stop/take. Once
  PnL crosses +5% it activates: tracks a peak, exits at a -X% trailing
  threshold scaled by horizon (scalper -2%, swing -4%, holder -6%).

Both functions are pure (no DB, no I/O). The caller is responsible
for tracking the peak_pnl in memory across ticks and supplying the
leader's horizon. The fall-back when horizon is unknown is "swing"
(the modal class on Polymarket).
"""
from __future__ import annotations

from dataclasses import dataclass


# Horizons recognised by the leader registry / strategy classifier.
HORIZON_SCALPER = "scalper"
HORIZON_SWING = "swing"
HORIZON_HOLDER = "holder"

# Default fallback when horizon is unset / unknown. Swing is the modal
# class on Polymarket per the 2026-05-15 leader_registry audit.
_DEFAULT_HORIZON = HORIZON_SWING

# Holding caps per horizon (seconds). These map to the leader's
# typical holding period — copying a scalper for 12h would hold the
# position long past the leader's own profit window.
_HOLDING_CAP_BY_HORIZON: dict[str, int] = {
    HORIZON_SCALPER: 3_600,        # 1h
    HORIZON_SWING:   21_600,       # 6h
    HORIZON_HOLDER:  86_400,       # 24h
}

# Trailing-stop activation threshold. Once PnL crosses this, the
# trailing stop becomes active (and the peak starts tracking).
_TRAILING_ACTIVATE_PNL_PCT = 0.05  # +5%

# Trailing-stop fallback distance per horizon (fraction of peak PnL).
# A scalper's profit window is short → tight trail. A holder is allowed
# to give back more before exiting.
_TRAILING_DISTANCE_BY_HORIZON: dict[str, float] = {
    HORIZON_SCALPER: 0.02,  # 2% off peak
    HORIZON_SWING:   0.04,  # 4% off peak
    HORIZON_HOLDER:  0.06,  # 6% off peak
}


@dataclass(frozen=True)
class TrailingDecision:
    """Result returned by ``check_trailing_stop``.

    * ``active``  — True iff PnL has crossed the activation threshold
      and the trailing stop is now armed.
    * ``new_peak`` — the new peak PnL (the caller persists it).
    * ``triggered`` — True iff the current PnL has retreated past the
      trailing distance from the peak. Caller closes the trade.
    * ``reason`` — short string for the close_reason argument.
    """
    active: bool
    new_peak: float
    triggered: bool
    reason: str = ""


def _normalize_horizon(horizon: str | None) -> str:
    if horizon is None:
        return _DEFAULT_HORIZON
    h = str(horizon).strip().lower()
    if h in _HOLDING_CAP_BY_HORIZON:
        return h
    return _DEFAULT_HORIZON


def resolve_holding_cap_for_horizon(
    *,
    default_cap_s: int,
    horizon: str | None,
    is_sport: bool,
    sport_cap_s: int,
) -> int:
    """Return the effective holding cap given the leader's horizon.

    Order of precedence:
      1. Sport markets always use ``sport_cap_s`` (the 30-min safety
         net is a hard guardrail).
      2. Otherwise pick from ``_HOLDING_CAP_BY_HORIZON[horizon]``.
      3. Fall back to ``default_cap_s`` if horizon is unknown.

    The returned cap is always positive — a non-positive default is
    treated as "disabled" and bypasses this function entirely; the
    legacy default applies.
    """
    if is_sport:
        return int(sport_cap_s) if sport_cap_s > 0 else int(default_cap_s)
    norm = _normalize_horizon(horizon)
    cap = _HOLDING_CAP_BY_HORIZON.get(norm)
    if cap is None or cap <= 0:
        return int(default_cap_s)
    return int(cap)


def check_trailing_stop(
    *,
    pnl_pct: float,
    peak_pnl_pct: float | None,
    horizon: str | None,
    is_sport: bool,
) -> TrailingDecision:
    """Evaluate the trailing-stop state for one open trade.

    Inputs are scalars only — the caller tracks ``peak_pnl_pct`` per
    trade.id in memory and supplies it on each tick.

    Activation:
      * Trailing stop is inactive until PnL crosses
        ``_TRAILING_ACTIVATE_PNL_PCT`` (+5% default).
      * Sport markets activate at the same +5% threshold — the
        sport-specific stop (-3%) is a separate, hard guardrail still
        evaluated upstream.

    Trigger:
      * Once active, the trail = ``peak - trail_distance_for_horizon``.
      * If current ``pnl_pct < trail``, ``triggered=True`` and the
        caller closes the trade with reason ``trailing_stop``.

    Returns a ``TrailingDecision`` with ``new_peak`` so the caller can
    persist the maximum-so-far.
    """
    norm = _normalize_horizon(horizon)
    trail_distance = _TRAILING_DISTANCE_BY_HORIZON.get(norm, 0.04)

    activated_prior = (
        peak_pnl_pct is not None
        and peak_pnl_pct >= _TRAILING_ACTIVATE_PNL_PCT
    )
    crosses_activation = pnl_pct >= _TRAILING_ACTIVATE_PNL_PCT
    active = activated_prior or crosses_activation

    if not active:
        return TrailingDecision(
            active=False,
            new_peak=peak_pnl_pct if peak_pnl_pct is not None else pnl_pct,
            triggered=False,
        )

    # Active path: update peak, check trigger.
    if peak_pnl_pct is None:
        new_peak = pnl_pct
    else:
        new_peak = max(peak_pnl_pct, pnl_pct)

    trail_threshold = new_peak - trail_distance
    triggered = pnl_pct < trail_threshold

    if triggered:
        reason = f"trailing_stop|h={norm}|peak={new_peak:.3f}|trail={trail_distance:.3f}"
    else:
        reason = ""

    return TrailingDecision(
        active=True,
        new_peak=new_peak,
        triggered=triggered,
        reason=reason,
    )
