"""Round 12 — per-wallet social feature derivation (spec § 3.4).

Reads from ``social_signals`` (migration 035) + ``trades_observed`` for
the lag/concordance math. Output dict matches the H. SOCIAL slot names
in R8's :data:`FEATURE_NAMES`.

Slots (4):
  * ``social_signal_density``           — non-noise tweets per day in
                                          the lookback window.
  * ``tweets_per_active_day``           — non-noise tweets / active days
                                          (so a wallet that tweets once a
                                          day on every day scores 1.0).
  * ``tweet_to_trade_lag_median_s``     — signed median: negative ⇒
                                          tweet PRECEDES trade.
  * ``social_signal_strategy_concordance`` — fraction of entry/exit
                                             tweets where the next-1h
                                             trade matched the parsed
                                             direction.

The deriver is a pure-Python compute layer over rows returned by the
caller. The feature_store reader handles the SQL.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any


@dataclass(frozen=True)
class SocialFeatures:
    """Output shape matching the H. SOCIAL slot names exactly."""

    social_signal_density: float
    tweets_per_active_day: float
    tweet_to_trade_lag_median_s: float | None
    social_signal_strategy_concordance: float | None

    def as_dict(self) -> dict[str, float | None]:
        return {
            "social_signal_density": self.social_signal_density,
            "tweets_per_active_day": self.tweets_per_active_day,
            "tweet_to_trade_lag_median_s": self.tweet_to_trade_lag_median_s,
            "social_signal_strategy_concordance": (
                self.social_signal_strategy_concordance
            ),
        }


def _ensure_utc(ts: Any) -> datetime | None:
    if ts is None:
        return None
    if isinstance(ts, datetime):
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    return None


def derive_features(
    signals: list[dict[str, Any]],
    trades: list[dict[str, Any]],
    *,
    asof_ts: datetime,
    lookback_days: int,
    concordance_window_s: int = 3600,
) -> SocialFeatures:
    """Compute the 4 H. SOCIAL feature values from per-wallet rows.

    Args:
      signals:  list of social_signals rows (dict-shaped). Must carry
                ``posted_at``, ``intent``, ``intent_confidence``,
                ``parsed_direction``.
      trades:   list of trades_observed rows. Must carry ``time``,
                ``side`` (yes/no proxy via 'buy' on token), and
                ``token_id`` / ``market_id``.
      asof_ts:  the as-of timestamp; only rows with posted_at <= asof
                are considered.
      lookback_days: window length for the density / per-day calcs.
      concordance_window_s: post-tweet horizon to look for a matching
                trade (spec § 3.4 mentions "next 1h").

    Returns:
      :class:`SocialFeatures` — values may be NaN-equivalents (None)
      when the input is too thin to compute.
    """
    if asof_ts.tzinfo is None:
        asof_ts = asof_ts.replace(tzinfo=timezone.utc)
    floor = asof_ts - timedelta(days=max(1, int(lookback_days)))

    # Keep only signals in [floor, asof]. Filter noise out of the count
    # but keep them for density-context — the spec calls out
    # signal_density as "tweets/day matching this wallet's handle",
    # which includes noise tweets. We track both: non-noise for the
    # rate calcs that intent-categorise, all for density.
    in_window: list[dict[str, Any]] = []
    for s in signals:
        ts = _ensure_utc(s.get("posted_at"))
        if ts is None or ts < floor or ts > asof_ts:
            continue
        in_window.append(s)

    n_days = max(1.0, float(lookback_days))
    n_total = float(len(in_window))
    n_non_noise = float(
        sum(1 for s in in_window if str(s.get("intent")) != "noise")
    )

    # social_signal_density: ALL tweets / window (matches spec § 3.4).
    density = n_total / n_days

    # tweets_per_active_day: non-noise count / # distinct active days.
    active_days: set[str] = set()
    for s in in_window:
        if str(s.get("intent")) == "noise":
            continue
        ts = _ensure_utc(s.get("posted_at"))
        if ts is None:
            continue
        active_days.add(ts.date().isoformat())
    per_active = (
        (n_non_noise / float(len(active_days))) if active_days else 0.0
    )

    # Lag + concordance computations need an aligned trade index.
    trades_in_window: list[dict[str, Any]] = []
    for t in trades:
        ts = _ensure_utc(t.get("time"))
        if ts is None or ts < floor or ts > asof_ts:
            continue
        trades_in_window.append({**t, "time": ts})
    trades_in_window.sort(key=lambda r: r["time"])  # type: ignore[index]

    def _next_trade_after(t0: datetime) -> dict[str, Any] | None:
        for t in trades_in_window:
            if t["time"] >= t0:
                return t
        return None

    # tweet_to_trade_lag_median_s — sign convention: negative if tweet
    # precedes trade (i.e., trade_ts - tweet_ts > 0; we return *signed*
    # tweet_ts - trade_ts, so a tweet BEFORE the trade is negative).
    lags: list[float] = []
    concordant_pairs = 0
    total_directed_pairs = 0

    for s in in_window:
        intent = str(s.get("intent"))
        if intent == "noise":
            continue
        ts = _ensure_utc(s.get("posted_at"))
        if ts is None:
            continue
        next_t = _next_trade_after(ts - timedelta(seconds=concordance_window_s))
        if next_t is None:
            continue
        trade_ts = next_t["time"]
        # Only consider pairs where the trade is within ±concordance_window.
        delta = (ts - trade_ts).total_seconds()
        if abs(delta) > concordance_window_s:
            continue
        lags.append(delta)
        # Concordance: parsed direction matches trade side.
        direction = s.get("parsed_direction")
        side = next_t.get("side")
        if direction and side:
            total_directed_pairs += 1
            d = str(direction).lower()
            si = str(side).lower()
            # 'buy' is direction-agnostic (token-level); we map
            # parsed_direction='yes' → expect a buy on a yes-side token,
            # parsed_direction='no' → expect a buy on a no-side token.
            # In trades_observed, the token_id is the level of detail —
            # but for the deriver we keep concordance simple: 'buy'
            # always matches an entry_signal; 'sell' always matches an
            # exit_signal.
            if intent == "entry_signal" and si == "buy":
                concordant_pairs += 1
            elif intent == "exit_signal" and si == "sell":
                concordant_pairs += 1
            elif d == "yes" and si == "buy":
                # Soft match — direction-only concordance.
                concordant_pairs += 1
            elif d == "no" and si == "sell":
                concordant_pairs += 1

    lag_median: float | None = None
    if lags:
        lag_median = float(statistics.median(lags))

    concordance: float | None = None
    if total_directed_pairs:
        concordance = float(concordant_pairs) / float(total_directed_pairs)

    return SocialFeatures(
        social_signal_density=float(density),
        tweets_per_active_day=float(per_active),
        tweet_to_trade_lag_median_s=lag_median,
        social_signal_strategy_concordance=concordance,
    )


def features_to_feature_store_dict(
    features: SocialFeatures,
) -> dict[str, float | None]:
    """Translate the dataclass to the dict shape that the R8 features
    extractor consumes. Provides one stable name → value map regardless
    of in-class field order changes."""
    return features.as_dict()


__all__ = [
    "SocialFeatures",
    "derive_features",
    "features_to_feature_store_dict",
]
