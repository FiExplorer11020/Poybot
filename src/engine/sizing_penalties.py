"""Plan 2026-05-19 P1 — sizing penalties module.

Philosophy
----------
The confidence_engine has 13 hard gates that reject candidate trades
outright. Plan P1 collapses the SOFT ones (gates where the trade is
"less interesting" but not strictly invalid) into a continuous penalty
that scales the Kelly sizing rather than dropping the trade entirely.

This file is the registry of those penalty contributors. Each penalty
returns a value in ``[0.0, 1.0]`` where ``0.0`` means "no penalty"
(this dimension is fine) and ``1.0`` means "maximum penalty"
(this dimension wants the trade at min-size). The confidence_engine
sums the contributors (clamped at 0.8 so the trade is never dropped to
zero — the existing 0.20 floor in ``_kelly_size`` does the final clamp).

Wiring
------
``compute_market_context_penalty(trade_context)`` is called from
``confidence_engine._build_trade_context`` AFTER the leader behavioral
penalty is computed and BEFORE the action is selected. Its output is
added to the leader penalty (capped at 0.8) and the contributor codes
are appended to ``selected_codes`` so the decision_log reason carries
attribution (e.g. ``follow|risk=0.45|liquidity_zone,near_res_zone``).

Why penalties, not hard gates
-----------------------------
The user's stated objective is *"a bot that takes all trades it has
identified as potentially interesting"*. A trade in a $1500 market 5h
before resolution is LESS interesting than one in a $50k market 48h
out — but it's not zero-interest. Penalties let the bot still take it
at a fraction of the Kelly size, accumulate the outcome, and feed the
learning loop.
"""
from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------- #
# Individual penalty contributors                                              #
# ---------------------------------------------------------------------------- #


def _liquidity_zone_penalty(volume_24h: float | None) -> float:
    """Scaled penalty on markets with thin volume.

    The hard gate ``low_market_liquidity`` (default 1000 USDC floor)
    still rejects truly empty markets. Between the floor and a healthy
    $10k market we apply a linear penalty so $1500 markets are sized
    around 40% of Kelly while $9k markets are nearly full-Kelly.

    Returns 0.0 if volume is unknown or >= $10k, 0.5 at the floor.
    """
    if volume_24h is None:
        return 0.0
    try:
        v = float(volume_24h)
    except (TypeError, ValueError):
        return 0.0
    if v >= 10_000.0:
        return 0.0
    if v <= 1_000.0:
        return 0.5
    # Linear interpolation: 0.5 → 0.0 over [1k, 10k]
    return 0.5 * (1.0 - (v - 1_000.0) / 9_000.0)


def _near_resolution_penalty(hours_to_resolution: float | None) -> float:
    """Scaled penalty on markets near resolution.

    The hard gate ``near_resolution`` (default 6h FOLLOW / 6h FADE)
    rejects markets closer than the threshold. Between 6h and 24h we
    apply a linearly decaying penalty so a 7h market is sized lower
    than a 23h market.

    Returns 0.0 if hours unknown or >= 24h, 0.4 at 6h, 0.0 at 24h.
    """
    if hours_to_resolution is None:
        return 0.0
    try:
        h = float(hours_to_resolution)
    except (TypeError, ValueError):
        return 0.0
    if h >= 24.0:
        return 0.0
    if h <= 6.0:
        return 0.4
    return 0.4 * (1.0 - (h - 6.0) / 18.0)


def _high_price_zone_penalty(entry_price: float | None) -> float:
    """Scaled penalty on trades entering near the price ceiling.

    The hard gates ``high_price_follow_blocked`` (>= 0.85 engine-side)
    and ``high_entry_ask_blocked`` (>= 0.92 paper-side) cap the upper
    range. Between 0.75 and 0.85 we apply a penalty so a high-price
    trade is sized down even when it's not blocked outright.

    Returns 0.0 below 0.75, 0.3 at 0.85, scaled linearly.
    """
    if entry_price is None:
        return 0.0
    try:
        p = float(entry_price)
    except (TypeError, ValueError):
        return 0.0
    if p <= 0.75:
        return 0.0
    if p >= 0.85:
        return 0.3
    return 0.3 * (p - 0.75) / 0.10


def _partial_live_match_penalty(live_match_signals: int | None) -> float:
    """Scaled penalty for markets with exactly 1 live-match signal.

    The hard gate ``live_match_blocked`` (default require >=2 signals
    after Plan P0-5) lets through markets with 1 partial signal. Those
    are still riskier than a no-signal market — apply a 0.3 penalty.

    Returns 0.3 if signals==1, 0.0 otherwise.
    """
    if live_match_signals is None:
        return 0.0
    try:
        n = int(live_match_signals)
    except (TypeError, ValueError):
        return 0.0
    return 0.3 if n == 1 else 0.0


def _ofi_alignment_penalty(
    ofi_mean: float | None, leader_side: str | None
) -> float:
    """Plan P2 — penalty when order-flow imbalance OPPOSES leader's side.

    OFI mean is signed: positive = buy pressure, negative = sell
    pressure. When the leader is BUY-ing but OFI is negative (selling
    pressure dominant), the entry is against the prevailing flow → apply
    a penalty. Mirrored for SELL leaders (but P1-3 already rejects
    SELL pre-engine; this branch is defensive).

    Returns 0.0 to 0.2 scaled by |ofi_mean|.
    """
    if ofi_mean is None or leader_side is None:
        return 0.0
    try:
        ofi = float(ofi_mean)
    except (TypeError, ValueError):
        return 0.0
    side = str(leader_side).lower()
    # Magnitudes scale by 5x — typical ofi_mean is in [-0.5, 0.5].
    if side == "buy" and ofi < 0:
        return min(0.2, abs(ofi) * 0.4)
    if side == "sell" and ofi > 0:
        return min(0.2, ofi * 0.4)
    return 0.0


def _social_exit_penalty(last_intent: str | None, age_s: float | None) -> float:
    """Plan P2 — penalty when the leader recently posted an exit signal
    on social media. A fresh "I'm exiting" post is the strongest signal
    that following them is about to land into a closing position.

    Returns 0.4 if last_intent='exit_signal' and age < 1h, 0.2 if < 6h,
    0.0 otherwise.
    """
    if last_intent != "exit_signal" or age_s is None:
        return 0.0
    try:
        a = float(age_s)
    except (TypeError, ValueError):
        return 0.0
    if a < 3_600:
        return 0.4
    if a < 21_600:
        return 0.2
    return 0.0


# ---------------------------------------------------------------------------- #
# Aggregator                                                                   #
# ---------------------------------------------------------------------------- #


def compute_market_context_penalty(
    trade_context: dict[str, Any],
) -> tuple[float, list[str]]:
    """Aggregate the market-context penalty contributors.

    Reads from ``trade_context`` (the dict passed around inside
    confidence_engine.evaluate). Tolerant of missing keys — a missing
    feature contributes 0.

    Contributors (Plan 2026-05-19):
      P1:
        - liquidity_zone     (markets.volume_24h linear over [1k, 10k])
        - near_res_zone      (hours_to_resolution linear over [6h, 24h])
        - high_price_zone    (entry_price linear over [0.75, 0.85])
        - partial_live_match (1 live-match signal among 5 possibles)
      P2 (microstructure / social / cross-market):
        - ofi_opposite       (OFI mean opposite to leader direction)
        - social_exit_recent (leader posted exit_signal within hours)

    Returns
    -------
    tuple[float, list[str]]
        ``(penalty_sum, reason_codes)``. The sum is clamped at 0.8 so
        the existing 0.20 floor in `_kelly_size` keeps the trade
        viable. Codes describe which dimensions contributed.
    """
    contributors: list[tuple[str, float]] = []

    liq_pen = _liquidity_zone_penalty(trade_context.get("market_volume_24h"))
    if liq_pen > 0.001:
        contributors.append(("liquidity_zone", liq_pen))

    near_pen = _near_resolution_penalty(trade_context.get("hours_to_resolution"))
    if near_pen > 0.001:
        contributors.append(("near_res_zone", near_pen))

    price_pen = _high_price_zone_penalty(trade_context.get("entry_price"))
    if price_pen > 0.001:
        contributors.append(("high_price_zone", price_pen))

    live_pen = _partial_live_match_penalty(
        trade_context.get("live_match_signal_count")
    )
    if live_pen > 0.001:
        contributors.append(("partial_live_match", live_pen))

    # Plan P2 — microstructure / social
    ofi_pen = _ofi_alignment_penalty(
        trade_context.get("ofi_mean"),
        trade_context.get("leader_side"),
    )
    if ofi_pen > 0.001:
        contributors.append(("ofi_opposite", ofi_pen))

    social_pen = _social_exit_penalty(
        trade_context.get("social_last_intent"),
        trade_context.get("social_last_signal_age_s"),
    )
    if social_pen > 0.001:
        contributors.append(("social_exit_recent", social_pen))

    if not contributors:
        return 0.0, []

    total = min(0.8, sum(pen for _, pen in contributors))
    codes = [code for code, _ in contributors]
    return total, codes
