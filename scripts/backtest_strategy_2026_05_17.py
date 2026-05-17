"""Backtest the proposed Phase 3 strategy filters on positions_reconstructed.

This script empirically validates the filter stack from
docs/autonomous_session_2026_05_17_strategy/00_DIAGNOSIS_AND_PLAN.md
BEFORE we deploy any production code change.

It re-uses three tables: positions_reconstructed (closed positions with PnL),
leader_profiles (posterior winrate per leader), and markets (category fallback).

CLI flags allow grid-searching the four levers we plan to wire into runtime
config: min_leader_winrate, entry band [min,max], max_hold_s, category whitelist.

Strategy upgrade 2026-05-17 (round 2 — Levers B + C, ADDED IN PLACE):
    - `--use-falcon-prior` : fuse the leader's external (Falcon Wallet 360)
      win/loss counts into `effective_resolved` / `effective_winrate` using a
      Bayesian fusion with a configurable external discount (default 0.5).
      Needs migration 046 (leader_profiles.external_*).
    - `--use-tier-based` : classify each leader as tier A / B / C from
      falcon_score and confirmed-follower count, then apply tier-specific
      `min_resolved` and `min_winrate` thresholds (replaces the single
      global pair).
    - Per-tier breakdown is rendered in addition to the existing per-category
      / per-entry / per-hold cohorts.

Outputs:
    - Pretty table to stdout with headline metrics + per-cohort breakdown
    - JSON report at reports/backtest_strategy_<timestamp>.json

Usage (inside engine container on prod, recommended):
    docker exec polymarket_engine \\
        python -m scripts.backtest_strategy_2026_05_17 \\
        --min-leader-winrate 0.6 --entry-min 0.5 --entry-max 0.9 \\
        --max-hold-s 86400 --categories sports,crypto,macro --days 60

Usage with the new (round-2) gates:
    docker exec polymarket_engine \\
        python -m scripts.backtest_strategy_2026_05_17 \\
        --use-falcon-prior --use-tier-based --days 60

Local usage (requires SSH tunnel to prod DB):
    ssh -L 5432:localhost:5432 polymarket-prod &
    DATABASE_URL=postgresql://polymarket:...@localhost:5432/polymarket \\
        python scripts/backtest_strategy_2026_05_17.py ...
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import asyncpg


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_DSN = "postgresql://polymarket:polymarket_dev_password@localhost:5432/polymarket"

ENTRY_BUCKETS: list[tuple[str, float, float]] = [
    ("[0.30,0.40)", 0.30, 0.40),
    ("[0.40,0.50)", 0.40, 0.50),
    ("[0.50,0.70)", 0.50, 0.70),
    ("[0.70,0.85)", 0.70, 0.85),
    ("[0.85,0.92]", 0.85, 0.9201),
]

HOLD_BUCKETS: list[tuple[str, int, int]] = [
    ("<=1h", 0, 3_600),
    ("1-4h", 3_600, 14_400),
    ("4-12h", 14_400, 43_200),
    ("12-24h", 43_200, 86_400),
    (">24h", 86_400, 10**9),
]


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------


@dataclass
class CohortStat:
    label: str
    n: int
    wins: int
    losses: int
    win_pct: float
    avg_pnl_pct: float
    total_pnl_usdc: float


@dataclass
class BacktestResult:
    params: dict[str, Any]
    n_total: int
    wins: int
    losses: int
    win_pct: float
    avg_pnl_pct: float
    total_pnl_usdc: float
    by_category: list[CohortStat] = field(default_factory=list)
    by_entry_bucket: list[CohortStat] = field(default_factory=list)
    by_hold_bucket: list[CohortStat] = field(default_factory=list)
    by_leader_top20: list[CohortStat] = field(default_factory=list)
    by_tier: list[CohortStat] = field(default_factory=list)  # tier A/B/C

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d


# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------

# Pulls every closed position joined with:
#   - leader posterior winrate + resolved-count (internal observations)
#   - leader_profiles.external_* (Falcon Wallet 360 stats, from migration 046)
#   - leaders.falcon_score (for tier classification)
#   - follower_edges confirmed-count (alternative tier-A criterion)
# All filters that depend on user-supplied thresholds are applied in Python so
# we can grid quickly without re-querying.
#
# NOTE: this query references `lp.external_resolved_count`, `lp.external_wins`,
# `lp.external_losses` (added by migration 046). If the migration hasn't been
# applied yet the columns will be `NULL` via `COALESCE`, so Falcon fusion is a
# no-op rather than a hard failure. The script keeps working pre-046 with
# `--use-falcon-prior` simply yielding 0 external samples for every leader.
CANDIDATE_SQL = """
WITH confirmed_followers AS (
    -- Count of confirmed (co_occurrences>=5, same_direction_rate>=0.7) follower
    -- edges per leader. Used by tier-A "≥5 confirmed edges" alternative gate.
    SELECT leader_wallet, COUNT(*) AS cnt
    FROM follower_edges
    WHERE co_occurrences >= 5 AND same_direction_rate >= 0.7
    GROUP BY leader_wallet
)
SELECT
    p.id,
    p.wallet_address,
    p.market_id,
    COALESCE(p.category, m.category, 'unknown')        AS category,
    p.entry_price::float                               AS entry_price,
    p.exit_price::float                                AS exit_price,
    COALESCE(p.net_pnl_usdc, p.pnl_usdc)::float        AS pnl_usdc,
    p.pnl_pct::float                                   AS pnl_pct,
    p.holding_period_s                                 AS holding_period_s,
    p.close_method                                     AS close_method,
    p.size_usdc::float                                 AS size_usdc,
    lp.positions_resolved                              AS leader_resolved,
    ((lp.profile_json -> 'accuracy') ->> 'overall')::float AS leader_winrate,
    -- Falcon Wallet 360 external stats (migration 046; NULL if not yet applied)
    COALESCE(lp.external_resolved_count, 0)::int       AS external_resolved,
    COALESCE(lp.external_wins, 0)::int                 AS external_wins,
    COALESCE(lp.external_losses, 0)::int               AS external_losses,
    l.falcon_score::float                              AS falcon_score,
    COALESCE(cf.cnt, 0)::int                           AS confirmed_followers
FROM positions_reconstructed p
LEFT JOIN markets         m  ON m.market_id     = p.market_id
LEFT JOIN leader_profiles lp ON lp.wallet_address = p.wallet_address
LEFT JOIN leaders         l  ON l.wallet_address  = p.wallet_address
LEFT JOIN confirmed_followers cf ON cf.leader_wallet = p.wallet_address
WHERE p.close_time IS NOT NULL
  AND p.pnl_pct    IS NOT NULL
  AND p.entry_price IS NOT NULL
  AND p.invalidated_at IS NULL
  AND p.open_time >= NOW() - ($1::int || ' days')::interval
"""


# ---------------------------------------------------------------------------
# Tier classification + Falcon prior fusion
# ---------------------------------------------------------------------------
#
# These helpers mirror the Lever B + Lever C logic that Agent B is wiring
# into `src/engine/confidence_engine.py`. Keeping the implementation here is
# intentional — the backtest must NOT import from the engine (which has
# Redis / DB pool requirements that don't suit a one-shot SQL script).


@dataclass
class TierThresholds:
    name: str           # 'A', 'B', or 'C'
    min_resolved: int
    min_winrate: float


def classify_tier(
    row: asyncpg.Record | dict,
    *,
    tier_a_falcon: float,
    tier_a_confirmed: int,
    tier_b_falcon: float,
    tier_b_confirmed: int,
) -> str:
    """Return 'A', 'B', or 'C' for a leader row.

    Tier A: falcon_score >= tier_a_falcon OR confirmed_followers >= tier_a_confirmed
    Tier B: falcon_score >= tier_b_falcon OR confirmed_followers >= tier_b_confirmed
    Tier C: everything else (including missing falcon_score)

    Defaults (per docs/.../01_DATA_OPTIMIZATION_PLAN.md §2.C):
      A: falcon>=50 OR confirmed>=5
      B: falcon>=20 OR confirmed>=3
      C: else
    """
    score = row["falcon_score"]
    confirmed = row["confirmed_followers"] or 0
    score_f = float(score) if score is not None else 0.0
    if score_f >= tier_a_falcon or confirmed >= tier_a_confirmed:
        return "A"
    if score_f >= tier_b_falcon or confirmed >= tier_b_confirmed:
        return "B"
    return "C"


def fuse_falcon_prior(
    row: asyncpg.Record | dict,
    *,
    external_discount: float,
) -> tuple[int, float]:
    """Compute (effective_resolved, effective_winrate) using a Bayesian fusion.

    `effective_resolved`  = max(internal_resolved, floor(external_resolved * d))
    `effective_winrate`   = (alpha_internal + d*alpha_external) /
                            (alpha_internal + d*alpha_external +
                             beta_internal  + d*beta_external)

    Beta posteriors:
      alpha_internal = round(internal_winrate * internal_resolved)
      beta_internal  = internal_resolved - alpha_internal
      alpha_external = external_wins
      beta_external  = external_losses

    The internal channel keeps its full weight; the external channel is
    weighted by `external_discount` (default 0.5) because external counts
    are reported, not directly observed.

    When the leader has zero internal AND zero external, returns
    (internal_resolved or 0, internal_winrate or 0.0) — i.e. the function
    is a no-op for empty profiles, which lets the tier gate reject them
    cleanly.
    """
    internal_resolved = int(row["leader_resolved"] or 0)
    internal_winrate = row["leader_winrate"]
    if internal_winrate is None:
        internal_winrate = 0.0
    internal_winrate_f = float(internal_winrate)

    external_resolved = int(row["external_resolved"] or 0)
    external_wins = int(row["external_wins"] or 0)
    external_losses = int(row["external_losses"] or 0)

    alpha_internal = max(0.0, round(internal_winrate_f * internal_resolved))
    beta_internal = max(0.0, internal_resolved - alpha_internal)

    alpha_external = float(external_wins)
    beta_external = float(external_losses)

    total_alpha = alpha_internal + external_discount * alpha_external
    total_beta = beta_internal + external_discount * beta_external
    total = total_alpha + total_beta

    effective_resolved = max(
        internal_resolved,
        int(external_resolved * external_discount),
    )

    if total <= 0.0:
        return effective_resolved, internal_winrate_f
    effective_winrate = total_alpha / total
    return effective_resolved, effective_winrate


def build_tier_table(
    *,
    tier_a_min_resolved: int,
    tier_a_min_winrate: float,
    tier_b_min_resolved: int,
    tier_b_min_winrate: float,
    tier_c_min_resolved: int,
    tier_c_min_winrate: float,
) -> dict[str, TierThresholds]:
    """Bundle the per-tier gate thresholds into a dict keyed by tier name."""
    return {
        "A": TierThresholds("A", tier_a_min_resolved, tier_a_min_winrate),
        "B": TierThresholds("B", tier_b_min_resolved, tier_b_min_winrate),
        "C": TierThresholds("C", tier_c_min_resolved, tier_c_min_winrate),
    }


# ---------------------------------------------------------------------------
# Filter + aggregate
# ---------------------------------------------------------------------------


def _passes_filters(
    row: asyncpg.Record,
    *,
    min_leader_resolved: int,
    min_leader_winrate: float,
    entry_min: float,
    entry_max: float,
    max_hold_s: int,
    categories: set[str],
    use_falcon_prior: bool = False,
    external_discount: float = 0.5,
    use_tier_based: bool = False,
    tier_thresholds: dict[str, TierThresholds] | None = None,
    tier_a_falcon: float = 50.0,
    tier_a_confirmed: int = 5,
    tier_b_falcon: float = 20.0,
    tier_b_confirmed: int = 3,
) -> bool:
    # Compute effective_resolved / effective_winrate. When --use-falcon-prior is
    # off, these collapse to the legacy internal-only values.
    if use_falcon_prior:
        eff_resolved, eff_winrate = fuse_falcon_prior(
            row, external_discount=external_discount
        )
    else:
        lr_raw = row["leader_resolved"]
        lw_raw = row["leader_winrate"]
        if lr_raw is None or lw_raw is None:
            return False
        eff_resolved = int(lr_raw)
        eff_winrate = float(lw_raw)

    # Tier-based thresholds replace the single global pair if enabled.
    if use_tier_based and tier_thresholds is not None:
        tier = classify_tier(
            row,
            tier_a_falcon=tier_a_falcon,
            tier_a_confirmed=tier_a_confirmed,
            tier_b_falcon=tier_b_falcon,
            tier_b_confirmed=tier_b_confirmed,
        )
        thr = tier_thresholds[tier]
        min_res, min_wr = thr.min_resolved, thr.min_winrate
    else:
        min_res, min_wr = min_leader_resolved, min_leader_winrate

    if eff_resolved < min_res:
        return False
    if eff_winrate < min_wr:
        return False

    ep = row["entry_price"]
    if ep is None or ep < entry_min or ep > entry_max:
        return False

    hp = row["holding_period_s"]
    if hp is None or hp < 0 or hp > max_hold_s:
        return False

    if categories and (row["category"] or "unknown") not in categories:
        return False

    return True


def _is_win(pnl_pct: float | None) -> bool:
    return pnl_pct is not None and pnl_pct > 0.0


def _cohort_stat(label: str, rows: list[dict[str, Any]]) -> CohortStat:
    n = len(rows)
    if n == 0:
        return CohortStat(label, 0, 0, 0, 0.0, 0.0, 0.0)
    wins = sum(1 for r in rows if _is_win(r["pnl_pct"]))
    losses = n - wins
    avg_pnl_pct = sum(r["pnl_pct"] for r in rows) / n
    total_pnl = sum((r.get("pnl_usdc") or 0.0) for r in rows)
    return CohortStat(
        label=label,
        n=n,
        wins=wins,
        losses=losses,
        win_pct=wins / n,
        avg_pnl_pct=avg_pnl_pct,
        total_pnl_usdc=total_pnl,
    )


def _bucketize_entry(price: float) -> str:
    for label, lo, hi in ENTRY_BUCKETS:
        if lo <= price < hi:
            return label
    return "other"


def _bucketize_hold(seconds: int) -> str:
    for label, lo, hi in HOLD_BUCKETS:
        if lo <= seconds < hi:
            return label
    return "other"


def evaluate(
    raw_rows: list[asyncpg.Record],
    *,
    min_leader_resolved: int,
    min_leader_winrate: float,
    entry_min: float,
    entry_max: float,
    max_hold_s: int,
    categories: set[str],
    use_falcon_prior: bool = False,
    external_discount: float = 0.5,
    use_tier_based: bool = False,
    tier_thresholds: dict[str, TierThresholds] | None = None,
    tier_a_falcon: float = 50.0,
    tier_a_confirmed: int = 5,
    tier_b_falcon: float = 20.0,
    tier_b_confirmed: int = 3,
) -> BacktestResult:
    filtered: list[dict[str, Any]] = []
    for r in raw_rows:
        if _passes_filters(
            r,
            min_leader_resolved=min_leader_resolved,
            min_leader_winrate=min_leader_winrate,
            entry_min=entry_min,
            entry_max=entry_max,
            max_hold_s=max_hold_s,
            categories=categories,
            use_falcon_prior=use_falcon_prior,
            external_discount=external_discount,
            use_tier_based=use_tier_based,
            tier_thresholds=tier_thresholds,
            tier_a_falcon=tier_a_falcon,
            tier_a_confirmed=tier_a_confirmed,
            tier_b_falcon=tier_b_falcon,
            tier_b_confirmed=tier_b_confirmed,
        ):
            row_dict = dict(r)
            # Stamp the tier so the per-tier cohort split below is exact —
            # even when tier-based gating is disabled the classification is
            # still informative for the report.
            row_dict["tier"] = classify_tier(
                r,
                tier_a_falcon=tier_a_falcon,
                tier_a_confirmed=tier_a_confirmed,
                tier_b_falcon=tier_b_falcon,
                tier_b_confirmed=tier_b_confirmed,
            )
            filtered.append(row_dict)

    overall = _cohort_stat("ALL", filtered)

    by_cat: dict[str, list[dict[str, Any]]] = {}
    by_entry: dict[str, list[dict[str, Any]]] = {}
    by_hold: dict[str, list[dict[str, Any]]] = {}
    by_leader: dict[str, list[dict[str, Any]]] = {}
    by_tier: dict[str, list[dict[str, Any]]] = {}

    for r in filtered:
        by_cat.setdefault(r["category"] or "unknown", []).append(r)
        by_entry.setdefault(_bucketize_entry(r["entry_price"]), []).append(r)
        by_hold.setdefault(_bucketize_hold(r["holding_period_s"]), []).append(r)
        by_leader.setdefault(r["wallet_address"], []).append(r)
        by_tier.setdefault(r["tier"], []).append(r)

    leader_stats = sorted(
        (_cohort_stat(w, rs) for w, rs in by_leader.items()),
        key=lambda c: c.n,
        reverse=True,
    )[:20]

    tier_stats = [
        _cohort_stat(f"tier_{name}", by_tier.get(name, []))
        for name in ("A", "B", "C")
    ]

    params: dict[str, Any] = {
        "min_leader_resolved": min_leader_resolved,
        "min_leader_winrate": min_leader_winrate,
        "entry_min": entry_min,
        "entry_max": entry_max,
        "max_hold_s": max_hold_s,
        "categories": sorted(categories),
        "use_falcon_prior": use_falcon_prior,
        "external_discount": external_discount,
        "use_tier_based": use_tier_based,
    }
    if use_tier_based and tier_thresholds is not None:
        params["tier_thresholds"] = {
            name: asdict(thr) for name, thr in tier_thresholds.items()
        }
        params["tier_a_falcon"] = tier_a_falcon
        params["tier_a_confirmed"] = tier_a_confirmed
        params["tier_b_falcon"] = tier_b_falcon
        params["tier_b_confirmed"] = tier_b_confirmed

    return BacktestResult(
        params=params,
        n_total=overall.n,
        wins=overall.wins,
        losses=overall.losses,
        win_pct=overall.win_pct,
        avg_pnl_pct=overall.avg_pnl_pct,
        total_pnl_usdc=overall.total_pnl_usdc,
        by_category=sorted(
            (_cohort_stat(k, v) for k, v in by_cat.items()),
            key=lambda c: c.n,
            reverse=True,
        ),
        by_entry_bucket=[
            _cohort_stat(label, by_entry.get(label, []))
            for label, _, _ in ENTRY_BUCKETS
        ],
        by_hold_bucket=[
            _cohort_stat(label, by_hold.get(label, []))
            for label, _, _ in HOLD_BUCKETS
        ],
        by_leader_top20=leader_stats,
        by_tier=tier_stats,
    )


# ---------------------------------------------------------------------------
# Pretty printing
# ---------------------------------------------------------------------------


def _fmt_pct(x: float) -> str:
    return f"{x * 100:6.2f}%"


def _fmt_usd(x: float) -> str:
    return f"${x:>13,.2f}"


def _print_cohorts(title: str, cohorts: Iterable[CohortStat]) -> None:
    print(f"\n  {title}")
    print(f"  {'label':<46} {'n':>7} {'wins':>6} {'win%':>8} {'avg_pnl%':>10} {'tot_pnl':>16}")
    for c in cohorts:
        if c.n == 0:
            continue
        print(
            f"  {c.label:<46} {c.n:>7d} {c.wins:>6d} {_fmt_pct(c.win_pct):>8} "
            f"{_fmt_pct(c.avg_pnl_pct):>10} {_fmt_usd(c.total_pnl_usdc):>16}"
        )


def pretty_print(result: BacktestResult) -> None:
    p = result.params
    print("=" * 100)
    print("BACKTEST: Polymarket strategy filter stack (2026-05-17)")
    print("=" * 100)
    print(
        f"  params: leader_resolved>={p['min_leader_resolved']}  "
        f"leader_wr>={p['min_leader_winrate']:.2f}  "
        f"entry=[{p['entry_min']:.2f},{p['entry_max']:.2f}]  "
        f"hold<={p['max_hold_s']}s  "
        f"cat={p['categories']}"
    )
    if p.get("use_falcon_prior"):
        print(
            f"  falcon prior: ON  external_discount={p.get('external_discount', 0.5):.2f}"
        )
    if p.get("use_tier_based") and p.get("tier_thresholds"):
        thr = p["tier_thresholds"]
        print(
            f"  tier-based: ON  "
            f"A(res>={thr['A']['min_resolved']},wr>={thr['A']['min_winrate']:.2f})  "
            f"B(res>={thr['B']['min_resolved']},wr>={thr['B']['min_winrate']:.2f})  "
            f"C(res>={thr['C']['min_resolved']},wr>={thr['C']['min_winrate']:.2f})"
        )
    print(
        f"  n={result.n_total:,}  wins={result.wins:,}  losses={result.losses:,}  "
        f"win%={_fmt_pct(result.win_pct)}  avg_pnl%={_fmt_pct(result.avg_pnl_pct)}  "
        f"total_pnl={_fmt_usd(result.total_pnl_usdc)}"
    )

    _print_cohorts("Per category", result.by_category)
    _print_cohorts("Per entry-price bucket", result.by_entry_bucket)
    _print_cohorts("Per holding-period bucket", result.by_hold_bucket)
    _print_cohorts("Per tier (A/B/C)", result.by_tier)
    _print_cohorts("Top 20 leaders by n", result.by_leader_top20)
    print("=" * 100)


# ---------------------------------------------------------------------------
# Grid search
# ---------------------------------------------------------------------------


@dataclass
class GridCell:
    min_leader_winrate: float
    entry_min: float
    entry_max: float
    max_hold_s: int
    n: int
    wins: int
    win_pct: float
    avg_pnl_pct: float
    total_pnl_usdc: float
    use_falcon_prior: bool = False
    use_tier_based: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def grid_search(
    raw_rows: list[asyncpg.Record],
    *,
    min_leader_resolved: int,
    categories: set[str],
    grid_leader_wr: list[float],
    grid_entry_min: list[float],
    grid_entry_max: list[float],
    grid_max_hold: list[int],
    min_sample: int,
    grid_falcon_tier_combos: list[tuple[bool, bool]] | None = None,
    tier_thresholds: dict[str, TierThresholds] | None = None,
    external_discount: float = 0.5,
) -> list[GridCell]:
    """Grid-search filter combinations.

    `grid_falcon_tier_combos`: list of `(use_falcon_prior, use_tier_based)` tuples
    to grid over. Default is just `[(False, False)]` (legacy backtest).
    The 4-combo grid `[(False, False), (True, False), (False, True), (True, True)]`
    answers "which combination maximizes trade volume at >=70% win-rate?".
    """
    combos = grid_falcon_tier_combos or [(False, False)]
    cells: list[GridCell] = []
    for use_fp, use_tb in combos:
        for lwr in grid_leader_wr:
            for emin in grid_entry_min:
                for emax in grid_entry_max:
                    if emax <= emin:
                        continue
                    for hold in grid_max_hold:
                        res = evaluate(
                            raw_rows,
                            min_leader_resolved=min_leader_resolved,
                            min_leader_winrate=lwr,
                            entry_min=emin,
                            entry_max=emax,
                            max_hold_s=hold,
                            categories=categories,
                            use_falcon_prior=use_fp,
                            external_discount=external_discount,
                            use_tier_based=use_tb,
                            tier_thresholds=tier_thresholds,
                        )
                        cells.append(
                            GridCell(
                                min_leader_winrate=lwr,
                                entry_min=emin,
                                entry_max=emax,
                                max_hold_s=hold,
                                n=res.n_total,
                                wins=res.wins,
                                win_pct=res.win_pct,
                                avg_pnl_pct=res.avg_pnl_pct,
                                total_pnl_usdc=res.total_pnl_usdc,
                                use_falcon_prior=use_fp,
                                use_tier_based=use_tb,
                            )
                        )
    return cells


def print_grid(cells: list[GridCell], min_sample: int) -> None:
    print("\n" + "=" * 100)
    print(f"GRID SEARCH (showing only n >= {min_sample}, sorted by win_pct desc)")
    print("=" * 100)
    print(
        f"  {'falc':>4} {'tier':>4} {'lwr':>5} {'emin':>5} {'emax':>5} {'hold':>8} {'n':>7} "
        f"{'win%':>8} {'avg_pnl%':>10} {'tot_pnl':>16}"
    )
    qualifying = [c for c in cells if c.n >= min_sample]
    qualifying.sort(key=lambda c: (c.win_pct, c.total_pnl_usdc), reverse=True)
    for c in qualifying:
        falc = "Y" if c.use_falcon_prior else "n"
        tier = "Y" if c.use_tier_based else "n"
        print(
            f"  {falc:>4} {tier:>4} {c.min_leader_winrate:>5.2f} {c.entry_min:>5.2f} {c.entry_max:>5.2f} "
            f"{c.max_hold_s:>8d} {c.n:>7d} {_fmt_pct(c.win_pct):>8} "
            f"{_fmt_pct(c.avg_pnl_pct):>10} {_fmt_usd(c.total_pnl_usdc):>16}"
        )

    # Best by win_pct gate
    print("\n  BEST CELLS:")
    above_70 = [c for c in qualifying if c.win_pct >= 0.70]
    if above_70:
        # Prefer the combo that unlocks the MOST trades (n), then PnL —
        # the round-2 mission is "more trades while keeping ≥70%".
        best = max(above_70, key=lambda c: (c.n, c.total_pnl_usdc))
        print(
            f"    >=70% win + max TRADES: falcon={best.use_falcon_prior} tier={best.use_tier_based} "
            f"lwr={best.min_leader_winrate:.2f} "
            f"entry=[{best.entry_min:.2f},{best.entry_max:.2f}] "
            f"hold<={best.max_hold_s}s -> n={best.n} "
            f"win%={_fmt_pct(best.win_pct)} pnl={_fmt_usd(best.total_pnl_usdc)}"
        )
        best_pnl_70 = max(above_70, key=lambda c: c.total_pnl_usdc)
        print(
            f"    >=70% win + max PnL:    falcon={best_pnl_70.use_falcon_prior} tier={best_pnl_70.use_tier_based} "
            f"lwr={best_pnl_70.min_leader_winrate:.2f} "
            f"entry=[{best_pnl_70.entry_min:.2f},{best_pnl_70.entry_max:.2f}] "
            f"hold<={best_pnl_70.max_hold_s}s -> n={best_pnl_70.n} "
            f"win%={_fmt_pct(best_pnl_70.win_pct)} pnl={_fmt_usd(best_pnl_70.total_pnl_usdc)}"
        )
    else:
        print("    No combo reaches 70% win at the n-floor.")
    if qualifying:
        best_n = max(qualifying, key=lambda c: c.win_pct)
        print(
            f"    Best win% (any PnL):    falcon={best_n.use_falcon_prior} tier={best_n.use_tier_based} "
            f"lwr={best_n.min_leader_winrate:.2f} "
            f"entry=[{best_n.entry_min:.2f},{best_n.entry_max:.2f}] "
            f"hold<={best_n.max_hold_s}s -> n={best_n.n} "
            f"win%={_fmt_pct(best_n.win_pct)} pnl={_fmt_usd(best_n.total_pnl_usdc)}"
        )
        best_pnl = max(qualifying, key=lambda c: c.total_pnl_usdc)
        print(
            f"    Max PnL (any win%):     falcon={best_pnl.use_falcon_prior} tier={best_pnl.use_tier_based} "
            f"lwr={best_pnl.min_leader_winrate:.2f} "
            f"entry=[{best_pnl.entry_min:.2f},{best_pnl.entry_max:.2f}] "
            f"hold<={best_pnl.max_hold_s}s -> n={best_pnl.n} "
            f"win%={_fmt_pct(best_pnl.win_pct)} pnl={_fmt_usd(best_pnl.total_pnl_usdc)}"
        )
    print("=" * 100)


# ---------------------------------------------------------------------------
# CLI plumbing
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--min-leader-resolved", type=int, default=30)
    p.add_argument("--min-leader-winrate", type=float, default=0.55)
    p.add_argument("--entry-min", type=float, default=0.40)
    p.add_argument("--entry-max", type=float, default=0.92)
    p.add_argument("--max-hold-s", type=int, default=86_400)
    p.add_argument("--categories", type=str, default="sports,crypto,macro")
    p.add_argument("--days", type=int, default=60)
    p.add_argument("--min-sample", type=int, default=500)
    p.add_argument(
        "--grid",
        action="store_true",
        help="Run the four-axis grid in addition to the headline params.",
    )
    # Strategy upgrade 2026-05-17 round 2 — Lever B (Falcon prior fusion).
    p.add_argument(
        "--use-falcon-prior",
        action="store_true",
        help=(
            "Fuse leader_profiles.external_* (Falcon Wallet 360) counts into "
            "effective_resolved / effective_winrate with Bayesian fusion."
        ),
    )
    p.add_argument(
        "--external-discount",
        type=float,
        default=0.5,
        help=(
            "Weight on the external (Falcon) channel in the Bayesian fusion. "
            "Default 0.5 = external counts are worth half an internal one."
        ),
    )
    # Strategy upgrade 2026-05-17 round 2 — Lever C (tier-based thresholds).
    p.add_argument(
        "--use-tier-based",
        action="store_true",
        help=(
            "Classify leaders into tiers A/B/C and apply tier-specific "
            "min_resolved/min_winrate thresholds. Overrides --min-leader-* when on."
        ),
    )
    p.add_argument("--tier-a-min-resolved", type=int, default=10)
    p.add_argument("--tier-a-min-winrate", type=float, default=0.50)
    p.add_argument("--tier-b-min-resolved", type=int, default=20)
    p.add_argument("--tier-b-min-winrate", type=float, default=0.55)
    p.add_argument("--tier-c-min-resolved", type=int, default=30)
    p.add_argument("--tier-c-min-winrate", type=float, default=0.55)
    p.add_argument("--tier-a-falcon", type=float, default=50.0)
    p.add_argument("--tier-a-confirmed", type=int, default=5)
    p.add_argument("--tier-b-falcon", type=float, default=20.0)
    p.add_argument("--tier-b-confirmed", type=int, default=3)
    p.add_argument(
        "--reports-dir",
        type=str,
        default=str(Path(__file__).resolve().parent.parent / "reports"),
    )
    p.add_argument(
        "--dsn",
        type=str,
        default=os.environ.get("DATABASE_URL", DEFAULT_DSN),
    )
    return p.parse_args(argv)


async def _migration_046_applied(conn: asyncpg.Connection) -> bool:
    """Return True iff `leader_profiles.external_resolved_count` exists.

    We probe `information_schema.columns` rather than catching a query
    error because the column reference in CANDIDATE_SQL would fail BEFORE
    asyncpg starts streaming rows, so try/except can't recover.
    """
    row = await conn.fetchrow(
        """
        SELECT 1
        FROM information_schema.columns
        WHERE table_name = 'leader_profiles'
          AND column_name = 'external_resolved_count'
        LIMIT 1
        """
    )
    return row is not None


def _candidate_sql_for_schema(*, has_external_cols: bool) -> str:
    """Pick the right SELECT depending on whether migration 046 is live.

    When the external_* columns don't exist yet, the JOIN works but the
    references to lp.external_* must be stripped out — Postgres rejects
    them at parse time even when wrapped in COALESCE.
    """
    if has_external_cols:
        return CANDIDATE_SQL
    # Pre-046 SQL: same shape, but the external_* columns are hard-coded to 0.
    return CANDIDATE_SQL.replace(
        "COALESCE(lp.external_resolved_count, 0)::int       AS external_resolved,",
        "0::int                                              AS external_resolved,",
    ).replace(
        "COALESCE(lp.external_wins, 0)::int                 AS external_wins,",
        "0::int                                              AS external_wins,",
    ).replace(
        "COALESCE(lp.external_losses, 0)::int               AS external_losses,",
        "0::int                                              AS external_losses,",
    )


async def _load_rows(dsn: str, days: int) -> list[asyncpg.Record]:
    conn = await asyncpg.connect(dsn)
    try:
        has_external = await _migration_046_applied(conn)
        sql = _candidate_sql_for_schema(has_external_cols=has_external)
        if not has_external:
            print(
                "[backtest] migration 046 not detected — external_* columns "
                "set to 0. --use-falcon-prior will be a no-op until Agent B "
                "deploys.",
                flush=True,
            )
        # NOTE: $1 is the only interpolation; SQL is otherwise static.
        return await conn.fetch(sql, days)
    finally:
        await conn.close()


async def _amain(argv: list[str]) -> int:
    args = _parse_args(argv)

    categories = {c.strip() for c in args.categories.split(",") if c.strip()}
    print(
        f"[backtest] connecting and loading positions for last {args.days} days...",
        flush=True,
    )
    rows = await _load_rows(args.dsn, args.days)
    print(f"[backtest] {len(rows):,} candidate closed positions loaded.", flush=True)

    tier_thresholds = build_tier_table(
        tier_a_min_resolved=args.tier_a_min_resolved,
        tier_a_min_winrate=args.tier_a_min_winrate,
        tier_b_min_resolved=args.tier_b_min_resolved,
        tier_b_min_winrate=args.tier_b_min_winrate,
        tier_c_min_resolved=args.tier_c_min_resolved,
        tier_c_min_winrate=args.tier_c_min_winrate,
    )

    result = evaluate(
        rows,
        min_leader_resolved=args.min_leader_resolved,
        min_leader_winrate=args.min_leader_winrate,
        entry_min=args.entry_min,
        entry_max=args.entry_max,
        max_hold_s=args.max_hold_s,
        categories=categories,
        use_falcon_prior=args.use_falcon_prior,
        external_discount=args.external_discount,
        use_tier_based=args.use_tier_based,
        tier_thresholds=tier_thresholds,
        tier_a_falcon=args.tier_a_falcon,
        tier_a_confirmed=args.tier_a_confirmed,
        tier_b_falcon=args.tier_b_falcon,
        tier_b_confirmed=args.tier_b_confirmed,
    )

    if result.n_total < args.min_sample:
        print(
            f"[backtest] WARNING: filtered n={result.n_total} < min_sample={args.min_sample};"
            " reporting anyway."
        )

    pretty_print(result)

    grid_cells: list[GridCell] = []
    if args.grid:
        # When --grid is on, sweep both legacy axes AND the (falcon × tier)
        # 4-combo grid so the operator can see which combo unlocks the most
        # trades at ≥70% win-rate.
        grid_cells = grid_search(
            rows,
            min_leader_resolved=args.min_leader_resolved,
            categories=categories,
            grid_leader_wr=[0.50, 0.55, 0.60, 0.65, 0.70],
            grid_entry_min=[0.30, 0.40, 0.50],
            grid_entry_max=[0.85, 0.90, 0.92],
            grid_max_hold=[3_600, 14_400, 43_200, 86_400],
            min_sample=args.min_sample,
            grid_falcon_tier_combos=[
                (False, False),
                (True, False),
                (False, True),
                (True, True),
            ],
            tier_thresholds=tier_thresholds,
            external_discount=args.external_discount,
        )
        print_grid(grid_cells, args.min_sample)

    # Persist JSON report
    Path(args.reports_dir).mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_path = Path(args.reports_dir) / f"backtest_strategy_{ts}.json"
    payload = {
        "ts_utc": ts,
        "params": result.params,
        "candidate_rows_loaded": len(rows),
        "headline": result.to_dict(),
        "grid": [c.to_dict() for c in grid_cells],
    }
    out_path.write_text(json.dumps(payload, indent=2, default=str))
    print(f"\n[backtest] report written to {out_path}", flush=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(_amain(list(sys.argv[1:] if argv is None else argv)))


if __name__ == "__main__":
    raise SystemExit(main())
