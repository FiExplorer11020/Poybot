"""
Batch runner — nightly cold path jobs (Hawkes refit, LogReg refit, LightGBM refit).
Runs at BATCH_HOUR_UTC (default 3 AM UTC). Each step logs timing.
Failed steps are logged but do not abort the batch.

Usage:
    python scripts/batch_runner.py                     # normal run
    python scripts/batch_runner.py --dry-run           # report retention impact only

Retention sweep (Phase 0 Task D, audit R-6):
    The historic `step_cleanup_old_trades` (trades_observed 90d) is now joined
    by `step_apply_retention_policies`, which sweeps the rest of the unbounded
    tables flagged by the audit. The sweep is OFF BY DEFAULT — set
    RETENTION_ENABLED=true in the environment to opt in. The companion
    migration is `docs/migrations/011_retention_policies.sql`.
"""

import argparse
import asyncio
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from typing import NamedTuple

import redis.asyncio as redis_async
from loguru import logger

# Ensure project root is on the path when run as a script.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.config import settings
from src.database.connection import close_pool, get_db, initialize_pool
from src.engine.confidence_engine import ConfidenceEngine
from src.graph.hawkes_fitter import HawkesFitter
from src.profiler.behavior_profiler import BehaviorProfiler
from src.profiler.error_model import ErrorModel
from src.registry.falcon_client import FalconClient
from src.registry.leader_registry import LeaderRegistry


async def step_refresh_registry(falcon: FalconClient) -> None:
    """Step A: Refresh leader registry from Falcon."""
    async with get_db() as conn:
        registry = LeaderRegistry(falcon_client=falcon)
        await registry.refresh_leaderboard(conn)
        await registry.enrich_leaders(conn)


async def step_sync_markets(falcon: FalconClient) -> None:
    """Step A2: Upsert market metadata via Falcon agent 574."""
    registry = LeaderRegistry(falcon_client=falcon)
    async with get_db() as conn:
        count = await registry.sync_markets(conn)
    logger.info(f"sync_markets: {count} markets upserted")


async def step_backfill_trades(falcon: FalconClient) -> None:
    """Step B: Backfill missing trades via Falcon agent 556."""
    async with get_db() as conn:
        rows = await conn.fetch(
            "SELECT wallet_address FROM leaders "
            "WHERE on_watchlist = TRUE AND excluded = FALSE "
            "LIMIT 200"
        )
    for row in rows:
        try:
            result = await falcon.query(
                agent_id=556,
                params={"wallet_proxy": row["wallet_address"], "condition_id": "ALL"},
                limit=200,
            )
            trades = result if isinstance(result, list) else result.get("results", [])
            logger.debug(f"Backfill {row['wallet_address']}: {len(trades)} trades fetched")
        except Exception as e:
            logger.warning(f"Backfill failed for {row['wallet_address']}: {e}")


async def step_refit_hawkes() -> None:
    """Step C: Refit Hawkes process for confirmed edges."""
    fitter = HawkesFitter()
    updated = await fitter.run_batch()
    logger.info(f"Hawkes refit: {updated} edges updated")


async def step_refit_error_models() -> None:
    """Step D/E: Upgrade/refit error models for leaders with sufficient resolved positions."""
    async with get_db() as conn:
        rows = await conn.fetch(
            "SELECT wallet_address, positions_resolved FROM leader_profiles "
            "ORDER BY positions_resolved DESC LIMIT 200"
        )
    model = ErrorModel()
    for row in rows:
        wallet = row["wallet_address"]
        resolved = int(row["positions_resolved"] or 0)
        if resolved >= settings.MIN_RESOLVED_FOR_ERROR_P3:
            phase = 3
        elif resolved >= settings.MIN_RESOLVED_FOR_ERROR_P2:
            phase = 2
        else:
            phase = 1
        if phase > 1:
            try:
                _, profile, _ = await model._load_state(wallet)
                await model._upgrade_phase(wallet, phase, profile)
            except Exception as e:
                logger.warning(f"Error model refit failed for {wallet}: {e}")


async def step_precompute_confidence_cache(redis_client) -> None:
    """Precompute wallet-level confidence state for the hot path."""
    profiler = BehaviorProfiler(redis_client=None)
    error_model = ErrorModel()
    confidence = ConfidenceEngine(
        redis_client=redis_client,
        behavior_profiler=profiler,
        error_model=error_model,
    )
    cached = await confidence.precompute_redis_cache()
    logger.info(f"Confidence cache precomputed for {cached} leaders")


async def step_backfill_decision_learning() -> None:
    """Rebuild persisted follow/fade learning from historical paper trades."""
    profiler = BehaviorProfiler(redis_client=None)
    process_result = await profiler.rebuild_order_process()
    result = await profiler.rebuild_decision_learning()
    logger.info(
        f"Learning backfill: {process_result['wallets']} wallets / "
        f"{process_result['orders']} orders, "
        f"{result['wallets']} wallets / {result['trades']} trades replayed"
    )


async def step_cleanup_old_trades() -> None:
    """Step H: Drop trades_observed partitions older than RETENTION_TRADES_DAYS.

    Migration 013 (Phase 2 Task A) converted trades_observed to native PG
    declarative range partitioning by `time`. Retention is therefore now:

      1. For each child partition whose upper-bound is older than cutoff,
         DROP PARTITION (instant, no vacuum churn — the architect's #1 ROI
         move per docs/audit/03_schema_evolution.md M11).
      2. For the trades_observed_default partition (catches out-of-range
         rows that should normally be empty), fall back to a bounded DELETE.
         If the default ever accumulates rows, the maintenance script has
         fallen behind: log a warning and clean up by row.

    Backwards compatibility:
      * If trades_observed is NOT yet partitioned (migration 013 not
        applied), fall back to the legacy `DELETE FROM trades_observed
        WHERE time < cutoff` path. This keeps dev environments and CI
        with older schema snapshots working.

    The two paths are mutually exclusive: pg_class.relkind tells us which
    one to take. Both honour RETENTION_TRADES_DAYS exactly.
    """
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=settings.RETENTION_TRADES_DAYS)

    async with get_db() as conn:
        relkind = await conn.fetchval(
            "SELECT relkind FROM pg_class WHERE relname = 'trades_observed'"
        )

        if relkind != "p":
            # Legacy path — non-partitioned table. Keep the old DELETE.
            tag = await conn.execute(
                "DELETE FROM trades_observed WHERE time < $1",
                cutoff,
            )
            try:
                deleted = int(tag.split()[-1])
            except (AttributeError, ValueError, IndexError):
                deleted = 0
            logger.info(
                f"Cleanup (legacy/non-partitioned): deleted {deleted} trades "
                f"before {cutoff.date()}"
            )
            return

        # Partitioned path — DROP PARTITION for fully-aged-out children.
        # pg_partition_bounds gives us "FROM ('2026-04-01') TO ('2026-05-01')"
        # as a string; we parse the TO bound and drop only partitions whose
        # upper bound is <= cutoff (the whole partition is past retention).
        children = await conn.fetch(
            """
            SELECT
                c.relname AS partition_name,
                pg_get_expr(c.relpartbound, c.oid) AS bound_expr
            FROM pg_inherits i
            JOIN pg_class p   ON p.oid = i.inhparent
            JOIN pg_class c   ON c.oid = i.inhrelid
            WHERE p.relname = 'trades_observed'
              AND c.relkind = 'r'
            ORDER BY c.relname
            """
        )

        dropped: list[str] = []
        default_partition: str | None = None

        for row in children:
            name = row["partition_name"]
            bound = row["bound_expr"] or ""
            if "DEFAULT" in bound.upper():
                default_partition = name
                continue
            upper = _parse_partition_upper_bound(bound)
            if upper is None:
                logger.warning(
                    f"Cleanup: could not parse bound for partition {name!r}: "
                    f"{bound!r}. Skipping."
                )
                continue
            if upper <= cutoff:
                # Whole partition is older than retention — drop it.
                # Quoted identifier defensively (partitions are always
                # snake-case from our maintenance script, but be safe).
                await conn.execute(f'DROP TABLE IF EXISTS "{name}"')
                dropped.append(name)

        # Sweep the default partition by row, if any rows leaked in.
        default_deleted = 0
        if default_partition is not None:
            tag = await conn.execute(
                f'DELETE FROM "{default_partition}" WHERE time < $1',
                cutoff,
            )
            try:
                default_deleted = int(tag.split()[-1])
            except (AttributeError, ValueError, IndexError):
                default_deleted = 0
            if default_deleted > 0:
                logger.warning(
                    f"Cleanup: deleted {default_deleted} row(s) from "
                    f"{default_partition!r} — the rolling partition creator "
                    f"may be falling behind. Investigate "
                    f"scripts/maintenance/create_trades_partitions.py cron."
                )

    if dropped:
        logger.info(
            f"Cleanup (partitioned): dropped {len(dropped)} partition(s) "
            f"older than {cutoff.date()}: {', '.join(dropped)}"
        )
    else:
        logger.info(
            f"Cleanup (partitioned): nothing to drop before {cutoff.date()} "
            f"(default partition delta: {default_deleted})"
        )


def _parse_partition_upper_bound(bound_expr: str) -> datetime | None:
    """
    Extract the upper bound from a partition bound expression like:

        FOR VALUES FROM ('2026-04-01 00:00:00+00') TO ('2026-05-01 00:00:00+00')

    Returns the upper bound as an aware UTC datetime, or None if parsing
    fails (which we treat as "skip this partition" — never as "drop it").

    Intentionally regex-free for clarity; the format is fixed by PG.
    """
    if not bound_expr:
        return None
    marker = "TO ("
    idx = bound_expr.find(marker)
    if idx < 0:
        return None
    rest = bound_expr[idx + len(marker):]
    # Trim trailing ")" and surrounding quotes.
    end = rest.rfind(")")
    if end < 0:
        return None
    inner = rest[:end].strip().strip("'").strip('"').strip()
    try:
        dt = datetime.fromisoformat(inner)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


# --------------------------------------------------------------------------- #
# Retention policies (Phase 0 Task D — audit R-6)                              #
# --------------------------------------------------------------------------- #
#
# Each entry below maps an unbounded PG table to (time_column, default days).
# The defaults are derived from per-table volume estimates in
# docs/audit/01_data_inventory.md and are overridable via env variables of the
# form `RETENTION_<TABLE>_DAYS`. The whole sweep is gated by
# `RETENTION_ENABLED=true` — operator must opt in.
#
# Defaults rationale:
#   * decision_log (~1-5k rows/day, dashboard reads recent window)   -> 90d
#   * book_quality_snapshots (10-100k rows/day, highest growth)      -> 30d
#   * portfolio_equity (~1440 rows/day, equity curve)                -> 180d
#   * decision_state_transitions (small, used by neural readiness)   -> 90d
#   * live_orders (0 rows today, FK CASCADE from live_trades)        -> 180d
#   * signal_audits (0 rows — dormant)                               -> 90d
#   * fee_snapshots (0 rows — dormant)                               -> 90d
#   * system_control_audit (1 row per killswitch flip)               -> 365d
#   * risk_config_history (1 row per dashboard mutation)             -> 365d
#
# Audit/history tables keep a longer tail because their forensic value is
# disproportionate to their row count.

DEFAULT_RETENTION_DELETE_BATCH = 10_000


class RetentionPolicy(NamedTuple):
    table: str
    time_column: str
    default_days: int


# Order matters only for log readability — each policy runs independently.
RETENTION_POLICIES: tuple[RetentionPolicy, ...] = (
    RetentionPolicy("decision_log", "time", 90),
    RetentionPolicy("book_quality_snapshots", "observed_at", 30),
    RetentionPolicy("portfolio_equity", "time", 180),
    RetentionPolicy("decision_state_transitions", "created_at", 90),
    RetentionPolicy("live_orders", "placed_at", 180),
    RetentionPolicy("signal_audits", "created_at", 90),
    RetentionPolicy("fee_snapshots", "captured_at", 90),
    RetentionPolicy("system_control_audit", "changed_at", 365),
    RetentionPolicy("risk_config_history", "changed_at", 365),
    # Phase 3 Round 2 Agent Y — market_features_history is APPEND-ONLY
    # (~100 rows/day = ~36k rows/year). The error model reads at-or-
    # before `pr.open_time`, so the practical retention floor is the
    # phase-2 training lookback (90d) + some slack for late-resolved
    # positions. Default 540d = 18 months gives a comfortable buffer
    # for phase-3 retraining over multi-year windows and still keeps
    # the table at <60k rows even at 2x volume.
    RetentionPolicy("market_features_history", "captured_at", 540),
)


def _retention_env_var(table: str) -> str:
    """Env var name for per-table override, e.g. RETENTION_DECISION_LOG_DAYS."""
    return f"RETENTION_{table.upper()}_DAYS"


def _resolve_retention_days(policy: RetentionPolicy) -> int:
    """Resolve days for a policy from env (override) or default. Invalid env
    values fall back to the default with a warning."""
    raw = os.getenv(_retention_env_var(policy.table))
    if raw is None or raw == "":
        return policy.default_days
    try:
        days = int(raw)
    except ValueError:
        logger.warning(
            f"Retention: bad value for {_retention_env_var(policy.table)}={raw!r}, "
            f"falling back to default {policy.default_days}"
        )
        return policy.default_days
    if days <= 0:
        logger.warning(
            f"Retention: non-positive value for {_retention_env_var(policy.table)}={days}, "
            f"falling back to default {policy.default_days}"
        )
        return policy.default_days
    return days


def _retention_enabled() -> bool:
    """Default OFF. Operator must explicitly opt in via env."""
    return os.getenv("RETENTION_ENABLED", "false").strip().lower() in ("1", "true", "yes", "on")


async def _apply_one_retention_policy(
    policy: RetentionPolicy,
    *,
    dry_run: bool,
    batch_size: int = DEFAULT_RETENTION_DELETE_BATCH,
    max_batches: int = 10_000,
) -> int:
    """Apply a single retention policy. Returns rows deleted (or rows that
    WOULD be deleted in dry-run mode).

    Implementation note: PG does not support `DELETE ... LIMIT`. We use the
    CTID-pagination idiom (`WHERE ctid IN (SELECT ctid ... LIMIT N)`) so the
    delete is bounded per round and we can yield back to the event loop —
    avoiding a multi-minute lock on a big table.
    """
    days = _resolve_retention_days(policy)
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=days)
    label = f"retention[{policy.table}]"

    if dry_run:
        async with get_db() as conn:
            count = await conn.fetchval(
                f"SELECT COUNT(*) FROM {policy.table} WHERE {policy.time_column} < $1",
                cutoff,
            )
        count = int(count or 0)
        logger.info(
            f"{label}: dry-run — would delete {count} rows older than "
            f"{cutoff.isoformat()} (retention={days}d)"
        )
        return count

    total_deleted = 0
    rounds = 0
    while rounds < max_batches:
        async with get_db() as conn:
            # CTID-based pagination: pick up to `batch_size` qualifying rows
            # by their physical tuple id, then delete those. Guarantees the
            # loop terminates (each round shrinks the qualifying set).
            tag = await conn.execute(
                f"""
                DELETE FROM {policy.table}
                WHERE ctid IN (
                    SELECT ctid FROM {policy.table}
                    WHERE {policy.time_column} < $1
                    LIMIT {batch_size}
                )
                """,
                cutoff,
            )
        # asyncpg returns the command tag, e.g. "DELETE 1234"
        try:
            deleted_this_round = int(tag.split()[-1])
        except (AttributeError, ValueError, IndexError):
            deleted_this_round = 0

        total_deleted += deleted_this_round
        rounds += 1
        if deleted_this_round < batch_size:
            break
        # Yield to the event loop so the cleanup never monopolises the pool.
        await asyncio.sleep(0)

    logger.info(
        f"{label}: deleted {total_deleted} rows older than {cutoff.isoformat()} "
        f"(retention={days}d, {rounds} batch(es))"
    )
    return total_deleted


# --------------------------------------------------------------------------- #
# Cold storage export (Round 6 § 3.6 — Wave-2 Agent E)                         #
# --------------------------------------------------------------------------- #
#
# Nightly Parquet export of yesterday's data from the hot Postgres tier to
# /data/cold/<table>/year=YYYY/month=MM/day=DD/part-00000.parquet. Gated by
# `COLD_EXPORT_ENABLED=true` so operator opts in (paths must exist + be
# writable on the prod host). Per-table failures are logged but never abort
# the rest of the sweep.


def _cold_export_enabled() -> bool:
    """Default OFF. Operator must opt in via env."""
    return os.getenv("COLD_EXPORT_ENABLED", "false").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


async def step_cold_export() -> None:
    """Step J: Export yesterday's data from hot Postgres tables to Parquet.

    Sits AFTER `step_cleanup_old_trades` / `step_apply_retention_policies`
    so the cold tier captures the post-retention shape of each table (the
    operator is unlikely to want recently-deleted rows back, and exporting
    BEFORE retention would briefly duplicate them on disk).
    """
    if not _cold_export_enabled():
        logger.info(
            "Cold export: COLD_EXPORT_ENABLED is false (default), skipping. "
            "Set COLD_EXPORT_ENABLED=true to opt in."
        )
        return

    # Import locally so the batch can run on hosts that don't yet have
    # pyarrow/duckdb installed (legacy dev environments).
    from src.cold_storage.exporter import ColdExporter

    exporter = ColdExporter()
    results = await exporter.run_nightly()

    ok = sum(1 for r in results.values() if r.error is None)
    failed = [t for t, r in results.items() if r.error is not None]
    total_rows = sum(r.rows_exported for r in results.values())
    total_bytes = sum(r.bytes_written for r in results.values())
    logger.info(
        f"Cold export: {ok}/{len(results)} table(s) ok, "
        f"{total_rows} row(s), {total_bytes} byte(s) written"
        + (f" — failed: {failed}" if failed else "")
    )


async def step_apply_retention_policies(*, dry_run: bool = False) -> None:
    """Step I: Apply per-table retention policies (audit R-6).

    Gated by RETENTION_ENABLED — default false. In dry-run mode the gate is
    bypassed so operators can inspect impact before flipping the switch.

    Each policy runs independently: a failure on one table is logged but does
    NOT abort the rest of the sweep.
    """
    if not dry_run and not _retention_enabled():
        logger.info(
            "Retention: RETENTION_ENABLED is false (default), skipping policy sweep. "
            "Set RETENTION_ENABLED=true to opt in, or run with --dry-run to preview."
        )
        return

    if dry_run:
        logger.info("Retention: DRY-RUN — no rows will be deleted.")

    for policy in RETENTION_POLICIES:
        try:
            await _apply_one_retention_policy(policy, dry_run=dry_run)
        except Exception as e:  # noqa: BLE001 — intentional broad catch (per-policy isolation)
            logger.error(
                f"retention[{policy.table}]: failed — {e}. Continuing with next policy."
            )


async def _run_steps(
    redis_client,
    falcon: FalconClient,
    *,
    dry_run: bool = False,
) -> None:
    """Execute the batch pipeline against already-initialised infrastructure.

    Safe to call from inside a long-running process (the nightly scheduler)
    because it does NOT open or close the asyncpg pool.

    `dry_run` is forwarded to retention so operators can preview impact.
    """
    steps = [
        ("refresh_registry", step_refresh_registry, (falcon,)),
        ("sync_markets", step_sync_markets, (falcon,)),  # FIX 1
        ("backfill_trades", step_backfill_trades, (falcon,)),
        ("refit_hawkes", step_refit_hawkes, ()),
        ("backfill_decision_learning", step_backfill_decision_learning, ()),
        ("refit_error_models", step_refit_error_models, ()),
        ("precompute_confidence_cache", step_precompute_confidence_cache, (redis_client,)),
        ("cleanup_old_trades", step_cleanup_old_trades, ()),
        (
            "apply_retention_policies",
            step_apply_retention_policies,
            (),
            {"dry_run": dry_run},
        ),
        # Cold export runs LAST so it captures the post-retention shape of
        # each table — see step_cold_export docstring for rationale.
        ("cold_export", step_cold_export, ()),
    ]

    for entry in steps:
        if len(entry) == 4:
            name, fn, args, kwargs = entry
        else:
            name, fn, args = entry
            kwargs = {}
        t0 = time.time()
        try:
            await fn(*args, **kwargs)
            elapsed = time.time() - t0
            logger.info(f"Batch step '{name}' completed in {elapsed:.1f}s")
        except Exception as e:
            elapsed = time.time() - t0
            logger.error(f"Batch step '{name}' failed after {elapsed:.1f}s: {e}")


async def run_batch(
    *,
    manage_infrastructure: bool = True,
    dry_run: bool = False,
) -> None:
    """Standalone entry point.

    When invoked via `python scripts/batch_runner.py` (manage_infrastructure=True)
    we open/close the pool ourselves.  When the in-process scheduler calls us
    (manage_infrastructure=False) we reuse the already-open pool and only
    create short-lived redis + Falcon clients.

    `dry_run=True` only affects the retention sweep (it forces a count-only
    pass). All other steps still run normally.
    """
    if manage_infrastructure:
        await initialize_pool(
            dsn=settings.DATABASE_URL,
            min_size=settings.DB_POOL_MIN,
            max_size=settings.DB_POOL_MAX,
        )

    redis_client = redis_async.from_url(settings.REDIS_URL, decode_responses=True)
    falcon = FalconClient(redis_client=redis_client)
    try:
        await _run_steps(redis_client, falcon, dry_run=dry_run)
    finally:
        if manage_infrastructure:
            await close_pool()
        await redis_client.aclose()
        await falcon.close()
    logger.info("Batch run complete")


def _parse_cli_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Polymarket bot nightly batch runner.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Report what retention WOULD delete without performing any DELETE. "
             "Bypasses the RETENTION_ENABLED gate. Other batch steps still run.",
    )
    return parser.parse_args(argv)


if __name__ == "__main__":
    cli_args = _parse_cli_args()
    asyncio.run(run_batch(dry_run=cli_args.dry_run))
