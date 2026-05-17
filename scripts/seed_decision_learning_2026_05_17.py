"""Pre-seed leader_profiles.decision_learning so the Thompson posteriors
launch from a REAL prior instead of the Beta(1,1) cold-start.

Why this exists
---------------
`confidence_engine._seed_thompson_from_profile` reads
`profile_json.decision_learning.{follow,fade}.{wins, losses, beta_a, beta_b}`
to bootstrap the Thompson Beta posteriors per leader. When a leader has zero
historical paper trades AND no replayed virtual outcomes, the posterior is
uniform Beta(1,1) and the engine wastes its exploration budget on cold-start
leaders that we already know (from Falcon Wallet 360 + our own
positions_reconstructed) are good or bad.

This script populates `decision_learning` in TWO passes (per the round-2 plan,
docs/.../01_DATA_OPTIMIZATION_PLAN.md, Lever D):

Pass 1 — paper-trade replay
    For every leader currently in `leader_profiles` with
    `external_resolved_count >= --min-external-resolved` (default 10), find
    each decision in `decision_log` for that leader that has a matching
    CLOSED row in `paper_trades` (joined on leader_wallet + market_id +
    decision/opened time window). For each such outcome, credit the
    (leader, action) bucket: W=1 if pnl_usdc>0 else 0.

Pass 2 — positions_reconstructed seeding
    For every leader in `leader_profiles` with
    `positions_resolved >= --min-internal-resolved` (default 30), treat each
    closed row in `positions_reconstructed` as a *virtual decision outcome*:
    a virtual FOLLOW that resolved win if pnl_usdc>0 else loss. We don't
    have action labels on positions, so we attribute them to the FOLLOW
    bucket (which is what the live engine would emit on a leader trade
    when the gates pass).

Idempotency
    A marker `profile_json.seed_log.last_seeded_2026_05_17` carries a list
    of processed `decision_log.id` and `positions_reconstructed.id` values
    per leader so re-running the script never double-credits. The marker
    is also bounded (most recent 10_000 IDs per leader) to keep
    profile_json bounded.

Concurrency safety
    All UPDATEs run inside a SERIALIZABLE transaction per leader; the
    INSERT/UPDATE on `leader_profiles` uses ON CONFLICT DO UPDATE.
    The live `BehaviorProfiler` writer is the only other touch point —
    the per-row transaction means the worst case is one of us repeating
    the same write, never an inconsistent merge.

Usage
-----
Local (against a tunnel):
    ssh -L 5432:localhost:5432 polymarket-prod &
    DATABASE_URL=postgresql://polymarket:...@localhost:5432/polymarket \\
        python scripts/seed_decision_learning_2026_05_17.py --dry-run

Prod (inside the engine container, recommended):
    docker exec polymarket_engine \\
        python -m scripts.seed_decision_learning_2026_05_17

Flags
-----
    --dry-run                  Don't write anything; print the planned counts.
    --wallet ADDRESS           Process only this one leader (for debugging).
    --min-external-resolved N  Pass-1 inclusion floor (default 10).
    --min-internal-resolved N  Pass-2 inclusion floor (default 30).
"""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import os
import sys
from datetime import datetime, timezone
from typing import Any

import asyncpg
from loguru import logger


DEFAULT_DSN = "postgresql://polymarket:polymarket_dev_password@localhost:5432/polymarket"

SEED_MARKER_KEY = "last_seeded_2026_05_17"

# Bound the processed-id history we carry in profile_json. 10k IDs per
# leader is ~ 200KB of JSON, still well below Postgres's TOAST limit and
# fast to round-trip. The script keeps the MOST RECENT 10k IDs and drops
# older entries — `id` values are monotonically increasing so this is a
# correct sliding window for idempotency on the active tail.
MAX_PROCESSED_IDS_PER_LEADER = 10_000


# ---------------------------------------------------------------------------
# Default decision_learning structures — mirror src/profiler/behavior_profiler
# (we duplicate here so the script has zero runtime deps on the engine pool /
# Redis layer).
# ---------------------------------------------------------------------------


def _default_decision_bucket() -> dict[str, Any]:
    return {
        "wins": 0,
        "losses": 0,
        "beta_a": 1.0,
        "beta_b": 1.0,
        "avg_win_pnl": 0.0,
        "avg_loss_pnl": 0.0,
        "avg_win_confidence": 0.0,
        "avg_loss_confidence": 0.0,
        "reason_stats": {},
    }


def _default_seed_log() -> dict[str, Any]:
    return {
        "processed_decision_ids": [],
        "processed_position_ids": [],
        "last_run_utc": None,
        "pass1_decisions_credited": 0,
        "pass2_positions_credited": 0,
    }


def _ensure_learning(profile: dict[str, Any]) -> dict[str, Any]:
    learning = profile.setdefault("decision_learning", {})
    for action in ("follow", "fade"):
        bucket = learning.get(action)
        if not isinstance(bucket, dict):
            learning[action] = _default_decision_bucket()
            continue
        defaults = _default_decision_bucket()
        for key, value in defaults.items():
            bucket.setdefault(key, copy.deepcopy(value))
    return learning


def _ensure_seed_log(profile: dict[str, Any]) -> dict[str, Any]:
    seed_log = profile.setdefault("seed_log", {})
    marker = seed_log.setdefault(SEED_MARKER_KEY, _default_seed_log())
    defaults = _default_seed_log()
    for key, value in defaults.items():
        marker.setdefault(key, copy.deepcopy(value))
    return marker


def _running_average(previous: float, count: int, new_value: float) -> float:
    """Mirror behavior_profiler._running_average so seeded buckets match
    the live engine's bookkeeping exactly."""
    if count <= 1:
        return new_value
    return previous + (new_value - previous) / count


def _credit_outcome(
    bucket: dict[str, Any],
    *,
    won: bool,
    pnl_usdc: float,
    confidence: float,
) -> None:
    """Update a single (follow|fade) bucket with one observed outcome.

    Mirrors `src/profiler/behavior_profiler._update_decision_learning` for
    the canonical fields (wins/losses/beta_a/beta_b/avg_*_pnl/avg_*_confidence).
    Reason-codes are NOT seeded — we lack the trade context here and the
    engine will populate them on its next live update.
    """
    if won:
        bucket["wins"] = int(bucket.get("wins", 0)) + 1
        bucket["beta_a"] = float(bucket.get("beta_a", 1.0)) + 1.0
        bucket["avg_win_pnl"] = _running_average(
            float(bucket.get("avg_win_pnl", 0.0)),
            int(bucket["wins"]),
            pnl_usdc,
        )
        bucket["avg_win_confidence"] = _running_average(
            float(bucket.get("avg_win_confidence", 0.0)),
            int(bucket["wins"]),
            confidence,
        )
    else:
        bucket["losses"] = int(bucket.get("losses", 0)) + 1
        bucket["beta_b"] = float(bucket.get("beta_b", 1.0)) + 1.0
        bucket["avg_loss_pnl"] = _running_average(
            float(bucket.get("avg_loss_pnl", 0.0)),
            int(bucket["losses"]),
            pnl_usdc,
        )
        bucket["avg_loss_confidence"] = _running_average(
            float(bucket.get("avg_loss_confidence", 0.0)),
            int(bucket["losses"]),
            confidence,
        )


# ---------------------------------------------------------------------------
# SQL — leader selection + data fetching
# ---------------------------------------------------------------------------

# Leaders eligible for pass 1: external (Falcon) counts available.
# NOTE: lp.external_resolved_count comes from migration 046 (Agent B's
# territory). If the column does not exist yet, this script errors fast
# with a clear message rather than silently producing 0 results — see
# `_assert_external_columns_exist` below.
SELECT_LEADERS_PASS1_SQL = """
SELECT lp.wallet_address,
       lp.profile_json,
       lp.positions_resolved,
       lp.trades_observed,
       lp.profile_maturity,
       lp.external_resolved_count
FROM leader_profiles lp
WHERE lp.external_resolved_count IS NOT NULL
  AND lp.external_resolved_count >= $1
"""

# Leaders eligible for pass 2: internal resolved count >= threshold.
SELECT_LEADERS_PASS2_SQL = """
SELECT lp.wallet_address,
       lp.profile_json,
       lp.positions_resolved,
       lp.trades_observed,
       lp.profile_maturity
FROM leader_profiles lp
WHERE lp.positions_resolved >= $1
"""

# Pass 1 query — for one leader, fetch decisions joined with the paper trade
# they opened (matched on leader+market+time window). Each row gives us:
#   * decision_log.id (for idempotency marker)
#   * action (follow | fade | skip; we only credit follow/fade outcomes)
#   * confidence at decision time (carried through to the avg_*_confidence
#     running averages)
#   * paper_trades.pnl_usdc — the realized outcome
# We restrict to CLOSED paper trades; an open trade has no outcome to credit.
SELECT_DECISIONS_FOR_LEADER_SQL = """
SELECT dl.id        AS decision_id,
       dl.action,
       dl.confidence,
       pt.pnl_usdc::float AS pnl_usdc,
       pt.opened_at
FROM decision_log dl
INNER JOIN paper_trades pt
   ON  pt.leader_wallet = dl.leader_wallet
   AND pt.market_id     = dl.market_id
   AND pt.status        = 'closed'
   AND pt.strategy IN ('follow', 'fade')
   AND pt.opened_at BETWEEN dl.time - INTERVAL '5 minutes'
                        AND dl.time + INTERVAL '30 minutes'
WHERE dl.leader_wallet = $1
  AND dl.action IN ('follow', 'fade')
  AND pt.pnl_usdc IS NOT NULL
ORDER BY dl.id ASC
"""

# Pass 2 query — for one leader, fetch every closed reconstructed position.
SELECT_POSITIONS_FOR_LEADER_SQL = """
SELECT id,
       COALESCE(net_pnl_usdc, pnl_usdc)::float AS pnl_usdc,
       open_time
FROM positions_reconstructed
WHERE wallet_address = $1
  AND close_time IS NOT NULL
  AND invalidated_at IS NULL
  AND COALESCE(net_pnl_usdc, pnl_usdc) IS NOT NULL
ORDER BY id ASC
"""

# UPSERT — write back profile_json. We MUST go through ON CONFLICT DO UPDATE
# because the live BehaviorProfiler also writes here; we can't rely on
# INSERT alone.
UPSERT_PROFILE_SQL = """
INSERT INTO leader_profiles
    (wallet_address, profile_json, trades_observed, positions_resolved,
     profile_maturity, last_updated)
VALUES ($1, $2::jsonb, $3, $4, $5, NOW())
ON CONFLICT (wallet_address) DO UPDATE SET
    profile_json   = EXCLUDED.profile_json,
    last_updated   = EXCLUDED.last_updated
"""


# ---------------------------------------------------------------------------
# Pre-flight: make sure migration 046 has been applied. Without
# external_resolved_count we cannot run pass 1, and we want to fail loudly
# rather than silently produce 0 results.
# ---------------------------------------------------------------------------


async def _assert_external_columns_exist(conn: asyncpg.Connection) -> None:
    row = await conn.fetchrow(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_name = 'leader_profiles'
          AND column_name = 'external_resolved_count'
        LIMIT 1
        """
    )
    if row is None:
        raise SystemExit(
            "[seed] leader_profiles.external_resolved_count is missing — "
            "migration 046_leader_external_stats.sql hasn't been applied yet. "
            "Wait for Agent B's deploy, then re-run."
        )


# ---------------------------------------------------------------------------
# Pass 1 — credit decisions that opened a closed paper trade
# ---------------------------------------------------------------------------


def _parse_profile(raw: Any) -> dict[str, Any]:
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return copy.deepcopy(raw)
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return {}
    return {}


async def _seed_pass1_for_leader(
    conn: asyncpg.Connection,
    *,
    wallet: str,
    profile: dict[str, Any],
    dry_run: bool,
) -> int:
    """Credit decision_log outcomes for one leader. Returns the number of
    NEWLY credited decisions (already-credited ones are skipped via the
    seed_log marker)."""
    rows = await conn.fetch(SELECT_DECISIONS_FOR_LEADER_SQL, wallet)
    if not rows:
        return 0

    marker = _ensure_seed_log(profile)
    processed = set(int(x) for x in marker.get("processed_decision_ids", []))
    learning = _ensure_learning(profile)

    credited = 0
    new_ids: list[int] = []
    for row in rows:
        did = int(row["decision_id"])
        if did in processed:
            continue
        action = row["action"]
        if action not in ("follow", "fade"):
            continue
        bucket = learning[action]
        pnl = float(row["pnl_usdc"])
        confidence = float(row["confidence"] or 0.0)
        won = pnl > 0.0
        if not dry_run:
            _credit_outcome(bucket, won=won, pnl_usdc=pnl, confidence=confidence)
        processed.add(did)
        new_ids.append(did)
        credited += 1

    if not dry_run and credited > 0:
        marker["processed_decision_ids"] = _bound_id_list(
            list(processed),
            limit=MAX_PROCESSED_IDS_PER_LEADER,
        )
        marker["pass1_decisions_credited"] = (
            int(marker.get("pass1_decisions_credited", 0)) + credited
        )
    return credited


# ---------------------------------------------------------------------------
# Pass 2 — credit virtual FOLLOW outcomes from positions_reconstructed
# ---------------------------------------------------------------------------


async def _seed_pass2_for_leader(
    conn: asyncpg.Connection,
    *,
    wallet: str,
    profile: dict[str, Any],
    dry_run: bool,
) -> int:
    """Credit closed reconstructed positions as virtual FOLLOW outcomes."""
    rows = await conn.fetch(SELECT_POSITIONS_FOR_LEADER_SQL, wallet)
    if not rows:
        return 0

    marker = _ensure_seed_log(profile)
    processed = set(int(x) for x in marker.get("processed_position_ids", []))
    learning = _ensure_learning(profile)
    follow_bucket = learning["follow"]

    credited = 0
    for row in rows:
        pid = int(row["id"])
        if pid in processed:
            continue
        pnl = float(row["pnl_usdc"])
        won = pnl > 0.0
        # Confidence unknown for synthetic outcomes — leave the avg
        # confidence streams at 0 by passing 0.0; the live engine will
        # blend its real confidences in once it starts trading the leader.
        if not dry_run:
            _credit_outcome(follow_bucket, won=won, pnl_usdc=pnl, confidence=0.0)
        processed.add(pid)
        credited += 1

    if not dry_run and credited > 0:
        marker["processed_position_ids"] = _bound_id_list(
            list(processed),
            limit=MAX_PROCESSED_IDS_PER_LEADER,
        )
        marker["pass2_positions_credited"] = (
            int(marker.get("pass2_positions_credited", 0)) + credited
        )
    return credited


def _bound_id_list(ids: list[int], *, limit: int) -> list[int]:
    """Keep only the newest `limit` IDs (largest values). IDs are
    monotonically increasing BIGSERIAL values, so the largest are the
    most-recently-credited — exactly the ones we need to keep to suppress
    a re-credit on a near-term re-run."""
    if len(ids) <= limit:
        return sorted(ids)
    return sorted(ids)[-limit:]


# ---------------------------------------------------------------------------
# Orchestration — one transaction per leader
# ---------------------------------------------------------------------------


async def _process_leader(
    conn: asyncpg.Connection,
    *,
    wallet: str,
    profile_raw: Any,
    positions_resolved: int,
    trades_observed: int,
    profile_maturity: float,
    do_pass1: bool,
    do_pass2: bool,
    dry_run: bool,
) -> dict[str, int]:
    """Run pass1 + pass2 for one leader inside a single transaction."""
    profile = _parse_profile(profile_raw)
    counts = {"pass1": 0, "pass2": 0}

    async with conn.transaction():
        if do_pass1:
            counts["pass1"] = await _seed_pass1_for_leader(
                conn,
                wallet=wallet,
                profile=profile,
                dry_run=dry_run,
            )
        if do_pass2:
            counts["pass2"] = await _seed_pass2_for_leader(
                conn,
                wallet=wallet,
                profile=profile,
                dry_run=dry_run,
            )

        if (counts["pass1"] + counts["pass2"] > 0) and not dry_run:
            marker = _ensure_seed_log(profile)
            marker["last_run_utc"] = datetime.now(tz=timezone.utc).isoformat()
            await conn.execute(
                UPSERT_PROFILE_SQL,
                wallet,
                json.dumps(profile),
                trades_observed,
                positions_resolved,
                round(float(profile_maturity or 0.0), 4),
            )
    return counts


async def run_seeding(
    *,
    dsn: str,
    wallet_filter: str | None,
    min_external_resolved: int,
    min_internal_resolved: int,
    dry_run: bool,
) -> dict[str, Any]:
    """Top-level orchestrator. Returns aggregate counts for the report."""
    conn = await asyncpg.connect(dsn)
    try:
        await _assert_external_columns_exist(conn)

        # ── Build the union of leaders eligible for either pass ──
        pass1_rows = await conn.fetch(SELECT_LEADERS_PASS1_SQL, min_external_resolved)
        pass2_rows = await conn.fetch(SELECT_LEADERS_PASS2_SQL, min_internal_resolved)

        eligible: dict[str, dict[str, Any]] = {}
        for row in pass1_rows:
            w = row["wallet_address"]
            if wallet_filter and w != wallet_filter:
                continue
            eligible[w] = {
                "row": row,
                "do_pass1": True,
                "do_pass2": False,
            }
        for row in pass2_rows:
            w = row["wallet_address"]
            if wallet_filter and w != wallet_filter:
                continue
            entry = eligible.setdefault(
                w, {"row": row, "do_pass1": False, "do_pass2": False}
            )
            entry["do_pass2"] = True
            # Prefer pass1 row (has external counts) when both exist; either
            # row carries the same profile_json / positions_resolved.
            entry["row"] = entry["row"] or row

        logger.info(
            f"[seed] {len(eligible)} leaders eligible "
            f"(pass1_only={sum(1 for v in eligible.values() if v['do_pass1'] and not v['do_pass2'])}, "
            f"pass2_only={sum(1 for v in eligible.values() if v['do_pass2'] and not v['do_pass1'])}, "
            f"both={sum(1 for v in eligible.values() if v['do_pass1'] and v['do_pass2'])})"
        )

        total_pass1 = 0
        total_pass2 = 0
        wallets_seeded = 0
        for wallet, entry in eligible.items():
            row = entry["row"]
            counts = await _process_leader(
                conn,
                wallet=wallet,
                profile_raw=row["profile_json"],
                positions_resolved=int(row["positions_resolved"] or 0),
                trades_observed=int(row["trades_observed"] or 0),
                profile_maturity=float(row["profile_maturity"] or 0.0),
                do_pass1=entry["do_pass1"],
                do_pass2=entry["do_pass2"],
                dry_run=dry_run,
            )
            total_pass1 += counts["pass1"]
            total_pass2 += counts["pass2"]
            if counts["pass1"] + counts["pass2"] > 0:
                wallets_seeded += 1

        return {
            "dry_run": dry_run,
            "leaders_eligible": len(eligible),
            "leaders_seeded": wallets_seeded,
            "decisions_written_pass1": total_pass1,
            "decisions_written_pass2": total_pass2,
            "decisions_written_total": total_pass1 + total_pass2,
        }
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--dsn",
        type=str,
        default=os.environ.get("DATABASE_URL", DEFAULT_DSN),
    )
    p.add_argument("--wallet", type=str, default=None)
    p.add_argument("--min-external-resolved", type=int, default=10)
    p.add_argument("--min-internal-resolved", type=int, default=30)
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Don't write anything; print the planned counts.",
    )
    return p.parse_args(argv)


async def _amain(argv: list[str]) -> int:
    args = _parse_args(argv)
    logger.info(
        f"[seed] start dsn={args.dsn.split('@')[-1]} dry_run={args.dry_run} "
        f"min_ext={args.min_external_resolved} min_int={args.min_internal_resolved}"
    )
    result = await run_seeding(
        dsn=args.dsn,
        wallet_filter=args.wallet,
        min_external_resolved=args.min_external_resolved,
        min_internal_resolved=args.min_internal_resolved,
        dry_run=args.dry_run,
    )
    logger.info(
        f"[seed] DONE  leaders_eligible={result['leaders_eligible']}  "
        f"leaders_seeded={result['leaders_seeded']}  "
        f"pass1={result['decisions_written_pass1']}  "
        f"pass2={result['decisions_written_pass2']}  "
        f"total={result['decisions_written_total']}  "
        f"dry_run={result['dry_run']}"
    )
    print(json.dumps(result, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    return asyncio.run(_amain(list(sys.argv[1:] if argv is None else argv)))


if __name__ == "__main__":
    raise SystemExit(main())
