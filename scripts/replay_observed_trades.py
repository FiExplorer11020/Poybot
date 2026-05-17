"""Replay trades_observed → positions_reconstructed → follower_edges → leader_profiles.

THIS SCRIPT FIXES THE ARCHITECTURAL GAP: backfill_polymarket_trades.py INSERTS
into trades_observed but does NOT publish to Redis ``trades:observed``, so the
downstream pipeline (position_tracker, graph_engine, behavior_profiler,
error_model) never sees backfilled rows. Result: 1.2M observed trades, but
only ~2k positions, ~200 edges, all profiles stuck at insufficient_data /
phase 1.

REPLAY SCOPE (post-2026-05-17 sample-efficiency cleanup): the chunked
stream EXCLUDES rows where ``source='onchain'``. Those rows carry a
placeholder ``market_id = token_id`` and hardcoded ``price=0, side='buy'``
(CLAUDE.md §15, pending Wave-3 economic decoder) that would poison every
downstream signal — Beta posteriors, Hawkes MLE, follower edges,
accuracy aggregates. They will be reattributed once Wave-3 ships; until
then the replay is INTENTIONALLY incomplete on that source. Pre-flight
``COUNT(*)`` lines still show raw totals so operators can see the gap.

The pipeline is **subscription-driven**:

  trade_observer → trades:observed (pub/sub) → position_tracker
                                            → graph_engine
                                            → behavior_profiler.on_leader_trade

  position_tracker → positions:closed (pub/sub) → behavior_profiler.on_position_closed
                                                → error_model.update

This script replays the pipeline IN-PROCESS, bypassing Redis pub/sub:

  1. Reset downstream tables (positions_reconstructed, follower_edges,
     leader_profiles, position_tracker_state) — clean rebuild.
  2. Instantiate the three downstream components with an InProcessRedis
     adapter that dispatches publish() calls to local handlers instead of
     pushing to real Redis.
  3. Pre-load the markets cache so position_tracker doesn't hit the DB per
     trade for fee_rate / token_yes / token_no lookups.
  4. Stream trades_observed ORDER BY time ASC in chunks of CHUNK_SIZE.
  5. For each trade: fan out to position_tracker.on_trade(),
     graph_engine.on_trade(), behavior_profiler.on_leader_trade().
  6. When position_tracker closes a position, the InProcessRedis dispatches
     the event directly to behavior_profiler.on_position_closed() +
     error_model.update().

Usage
-----

.. code-block:: bash

    # Dry-run: count trades, show what would be reset, no writes.
    python -m scripts.replay_observed_trades --dry-run

    # Real run: STOP engine + observer containers first.
    docker compose stop engine observer
    python -m scripts.replay_observed_trades
    docker compose start engine observer

The script is **idempotent**: re-running on the same trades_observed
snapshot produces the same downstream state (modulo timestamp-based ties).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from loguru import logger

# Ensure project root is on the path when run as a script.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import redis.asyncio as redis_async

from src.config import settings
from src.database.connection import close_pool, get_db, initialize_pool
from src.graph.graph_engine import GraphEngine
from src.observer.position_tracker import PositionTracker
from src.profiler.behavior_profiler import BehaviorProfiler
from src.profiler.error_model import ErrorModel

CHUNK_SIZE = 10_000
PROGRESS_EVERY = 50_000


# --------------------------------------------------------------------------- #
# In-process Redis shim                                                        #
# --------------------------------------------------------------------------- #


class InProcessRedis:
    """Redis-compatible wrapper that dispatches publish() to local handlers.

    - publish(channel, payload) → calls registered handlers directly.
    - All other methods delegate to the wrapped real client (for cache reads
      from leader_profiles, etc.). Tests can pass a SimpleNamespace with no
      real client if no Redis access is required.
    """

    def __init__(self, real_client: Any | None = None):
        self._real = real_client
        self._handlers: dict[str, list] = {}
        self._publish_count: dict[str, int] = {}

    def add_handler(self, channel: str, handler) -> None:
        self._handlers.setdefault(channel, []).append(handler)

    async def publish(self, channel, payload):  # noqa: D401
        self._publish_count[channel] = self._publish_count.get(channel, 0) + 1
        handlers = self._handlers.get(channel, [])
        if not handlers:
            return 0
        if isinstance(payload, (str, bytes)):
            try:
                event = json.loads(payload)
            except json.JSONDecodeError:
                logger.warning(
                    f"InProcessRedis: bad JSON on channel={channel}, skipped"
                )
                return 0
        else:
            event = payload
        for handler in handlers:
            try:
                await handler(event)
            except Exception as e:
                logger.warning(f"InProcessRedis handler error on {channel}: {e}")
        return len(handlers)

    def __getattr__(self, name):
        if self._real is None:
            raise AttributeError(
                f"InProcessRedis: no real client available for attribute {name!r}"
            )
        return getattr(self._real, name)

    async def aclose(self) -> None:
        if self._real is not None:
            try:
                await self._real.aclose()
            except Exception:
                pass


# --------------------------------------------------------------------------- #
# Setup helpers                                                                #
# --------------------------------------------------------------------------- #


async def count_inputs() -> dict[str, int]:
    """Count rows in the tables we'll touch — before the run."""
    async with get_db() as conn:
        trades = await conn.fetchval("SELECT COUNT(*) FROM trades_observed")
        positions = await conn.fetchval(
            "SELECT COUNT(*) FROM positions_reconstructed"
        )
        edges = await conn.fetchval(
            "SELECT COUNT(*) FROM follower_edges"
        )
        profiles = await conn.fetchval(
            "SELECT COUNT(*) FROM leader_profiles"
        )
        open_state = await conn.fetchval(
            "SELECT COUNT(*) FROM position_tracker_state"
        )
        leaders_total = await conn.fetchval(
            "SELECT COUNT(*) FROM leaders WHERE on_watchlist=TRUE AND excluded=FALSE"
        )
        markets = await conn.fetchval("SELECT COUNT(*) FROM markets")
    return {
        "trades_observed": int(trades or 0),
        "positions_reconstructed": int(positions or 0),
        "follower_edges": int(edges or 0),
        "leader_profiles": int(profiles or 0),
        "position_tracker_state": int(open_state or 0),
        "leaders_active": int(leaders_total or 0),
        "markets": int(markets or 0),
    }


async def reset_downstream_tables() -> None:
    """TRUNCATE positions_reconstructed, follower_edges, position_tracker_state.
    Reset leader_profiles' counters and JSON payload to baseline."""
    async with get_db() as conn:
        async with conn.transaction():
            await conn.execute("TRUNCATE TABLE positions_reconstructed RESTART IDENTITY")
            logger.info("  ✓ TRUNCATE positions_reconstructed")
            await conn.execute("TRUNCATE TABLE follower_edges RESTART IDENTITY")
            logger.info("  ✓ TRUNCATE follower_edges")
            await conn.execute("TRUNCATE TABLE position_tracker_state")
            logger.info("  ✓ TRUNCATE position_tracker_state")
            await conn.execute(
                """
                UPDATE leader_profiles SET
                    profile_json = '{}'::jsonb,
                    error_model_phase = 1,
                    error_model_blob = NULL,
                    profile_maturity = 0,
                    trades_observed = 0,
                    positions_resolved = 0,
                    last_updated = NOW()
                """
            )
            logger.info("  ✓ RESET leader_profiles (profile_json + counters)")


async def promote_top_wallets_to_leaders(
    min_volume_usdc: float = 10_000.0,
    min_trades: int = 30,
    max_count: int = 1500,
) -> dict[str, int]:
    """Promote top wallets observed in trades_observed into the `leaders` table.

    The `leaders` table is normally populated ONLY from Falcon agent 584
    (Falcon Score Leaderboard), capped at INITIAL_LEADER_COUNT. The backfill
    script feeds trades_observed from a different set — `wallet_universe`
    top by volume_usdc_ever — so most heavy traders never enter `leaders`
    and never get a profile. This step closes that gap.

    Criteria (deliberately broad, error on the side of inclusion):
      - total observed volume >= `min_volume_usdc`
      - total observed trades >= `min_trades`
      - NOT already in `leaders`
      - cap at `max_count` (ordered by volume desc)

    Filtering out bots is deferred to a downstream `excluded=TRUE` sweep
    (strategy_classifier already does this on `behavior_class='bot'`); we
    want every credible volume holder in the table first so the pipeline
    can build profiles, and the operator (or a later automated pass) can
    flip `on_watchlist=FALSE` on the misclassifieds.

    Returns {"inserted": int, "already_in_leaders": int, "skipped": int}.
    """
    AGG_TIMEOUT_S = 600.0  # 10 min — GROUP BY over 1.2M rows can be slow.
    async with get_db() as conn:
        await conn.execute("SET statement_timeout = 0")
        # 1. compute candidate set from trades_observed. Exclude
        # source='onchain' rows: they carry placeholder market_id and
        # price=0 (CLAUDE.md § 15) and would falsely promote wallets
        # into the leaders watchlist based on noise rather than real
        # trading volume. Older rows without a source value still flow
        # through (IS DISTINCT FROM is NULL-safe).
        candidates = await conn.fetch(
            """
            WITH wallet_stats AS (
                SELECT
                    wallet_address,
                    SUM(size_usdc)::numeric AS total_volume,
                    COUNT(*) AS n_trades
                FROM trades_observed
                WHERE source IS DISTINCT FROM 'onchain'
                GROUP BY wallet_address
            )
            SELECT wallet_address, total_volume, n_trades
            FROM wallet_stats
            WHERE total_volume >= $1
              AND n_trades >= $2
              AND NOT EXISTS (
                  SELECT 1 FROM leaders l
                  WHERE l.wallet_address = wallet_stats.wallet_address
              )
            ORDER BY total_volume DESC
            LIMIT $3
            """,
            Decimal(str(min_volume_usdc)),
            min_trades,
            max_count,
            timeout=AGG_TIMEOUT_S,
        )
        # 2. counts for reporting — best effort, swallow timeouts.
        try:
            already_in_leaders = await conn.fetchval(
                """
                SELECT COUNT(*) FROM (
                    SELECT wallet_address, SUM(size_usdc) AS v, COUNT(*) AS n
                    FROM trades_observed
                    WHERE source IS DISTINCT FROM 'onchain'
                    GROUP BY wallet_address
                ) ws
                WHERE ws.v >= $1 AND ws.n >= $2
                  AND EXISTS (SELECT 1 FROM leaders l WHERE l.wallet_address=ws.wallet_address)
                """,
                Decimal(str(min_volume_usdc)),
                min_trades,
                timeout=AGG_TIMEOUT_S,
            )
        except Exception as exc:
            logger.warning(
                f"already_in_leaders count failed ({exc}); reporting 0."
            )
            already_in_leaders = 0
        if not candidates:
            return {
                "inserted": 0,
                "already_in_leaders": int(already_in_leaders or 0),
                "skipped": 0,
            }
        # 3. bulk INSERT (one row per candidate)
        rows = [
            (
                c["wallet_address"],
                None,           # falcon_score (unknown until enrichment)
                "{}",          # wallet360_json
                "{}",          # classification_json
                True,           # on_watchlist
                False,          # excluded
                None,           # exclude_reason
            )
            for c in candidates
        ]
        async with conn.transaction():
            await conn.executemany(
                """
                INSERT INTO leaders
                  (wallet_address, falcon_score, wallet360_json,
                   classification_json, on_watchlist, excluded, exclude_reason,
                   first_seen, last_refresh)
                VALUES ($1, $2, $3::jsonb, $4::jsonb, $5, $6, $7, NOW(), NOW())
                ON CONFLICT (wallet_address) DO NOTHING
                """,
                rows,
            )
    return {
        "inserted": len(candidates),
        "already_in_leaders": int(already_in_leaders or 0),
        "skipped": 0,
    }


async def flag_is_leader_in_trades() -> dict[str, int]:
    """Re-flag trades_observed.is_leader = TRUE for every row whose wallet
    is in leaders (on_watchlist=TRUE AND excluded=FALSE).

    The backfill script inserts is_leader=FALSE indiscriminately (see
    scripts/backfill_polymarket_trades.py line 117) because it cannot know
    the leader set at insert time. Without this fix, profiler.on_leader_trade
    skips every backfilled trade (it early-returns if is_leader=False), and
    graph_engine routes them through the follower-side rather than the
    leader-side. We must reconcile the denormalised flag before replay.

    Returns {"flagged": int, "unflagged": int}.
    """
    # 900s (15 min) timeout — these UPDATEs can touch hundreds of thousands
    # of rows on a fresh trades_observed (1.2M rows × ~500 newly-promoted
    # leaders pulls ≥100K row updates). The default 30s asyncpg timeout
    # is FAR too tight: a crash here aborts the replay and forces a
    # manual SQL fixup.
    LONG_TIMEOUT_S = 900.0
    async with get_db() as conn:
        # Disable per-statement timeout for this connection — belt and
        # braces against any DB-side statement_timeout default.
        await conn.execute("SET statement_timeout = 0")
        # Flag rows whose wallet is a current active leader.
        tag1 = await conn.execute(
            """
            UPDATE trades_observed t
            SET is_leader = TRUE
            FROM leaders l
            WHERE t.wallet_address = l.wallet_address
              AND l.on_watchlist = TRUE
              AND l.excluded = FALSE
              AND t.is_leader = FALSE
            """,
            timeout=LONG_TIMEOUT_S,
        )
        # Un-flag rows whose wallet was once flagged but is no longer
        # active (excluded=TRUE / on_watchlist=FALSE / removed). Keeps
        # the denormalised state consistent with the canonical leaders
        # table — otherwise stale flags re-introduce ghost leaders into
        # the replay.
        tag2 = await conn.execute(
            """
            UPDATE trades_observed t
            SET is_leader = FALSE
            WHERE t.is_leader = TRUE
              AND NOT EXISTS (
                  SELECT 1 FROM leaders l
                  WHERE l.wallet_address = t.wallet_address
                    AND l.on_watchlist = TRUE
                    AND l.excluded = FALSE
              )
            """,
            timeout=LONG_TIMEOUT_S,
        )
    flagged = _parse_update_count(tag1)
    unflagged = _parse_update_count(tag2)
    return {"flagged": flagged, "unflagged": unflagged}


def _parse_update_count(tag: str) -> int:
    """asyncpg returns 'UPDATE N' tags. Extract N defensively."""
    try:
        return int(tag.rsplit(" ", 1)[-1])
    except (AttributeError, ValueError, IndexError):
        return 0


async def preload_markets(position_tracker: PositionTracker) -> int:
    """Pre-populate position_tracker._market_tokens cache so every replayed
    trade doesn't trigger a DB roundtrip for (token_yes, token_no) lookup."""
    async with get_db() as conn:
        rows = await conn.fetch(
            "SELECT market_id, token_yes, token_no FROM markets"
        )
    loaded = 0
    for r in rows:
        mid = r["market_id"]
        ty, tn = r["token_yes"], r["token_no"]
        if mid and (ty or tn):
            position_tracker._market_tokens[mid] = (ty, tn)
            loaded += 1
    return loaded


# --------------------------------------------------------------------------- #
# Replay loop                                                                  #
# --------------------------------------------------------------------------- #


def _row_to_trade_dict(row: dict) -> dict:
    """Convert a trades_observed DB row to the dict shape the downstream
    handlers expect (matches trade_observer._publish_trade_event payload)."""
    t = row["time"]
    if isinstance(t, datetime):
        time_iso = (
            t.isoformat()
            if t.tzinfo is not None
            else t.replace(tzinfo=timezone.utc).isoformat()
        )
    else:
        time_iso = str(t)
    return {
        "wallet_address": row["wallet_address"],
        "market_id": row["market_id"],
        "token_id": row["token_id"],
        "side": (row["side"] or "").lower(),
        "price": str(row["price"]),
        "size_usdc": str(row["size_usdc"]),
        "time": time_iso,
        "is_leader": bool(row["is_leader"]),
        "category": row.get("category") or "unknown",
        "source": row["source"],
    }


async def replay_trades(
    position_tracker: PositionTracker,
    graph_engine: GraphEngine,
    profiler: BehaviorProfiler,
    *,
    skip_leader_trade_profiler: bool = False,
) -> dict:
    """Stream trades_observed ORDER BY time ASC, dispatch to all 3 components.

    Uses keyset-style pagination on (time, id) to avoid the OFFSET penalty
    on partitioned trades_observed (>1M rows).

    Set `skip_leader_trade_profiler=True` to bypass profiler.on_leader_trade
    during the loop (the per-trade `_load_profile / _save_profile` pair is
    the dominant cost — ~5-10 ms / trade × 440K leader trades = 30-70 min
    just for profile decision_process updates). The accuracy / Dirichlet /
    EWMA-sizing parts of profile_json are still updated via
    on_position_closed, which fires only on every CLOSE (~5-20 % of trades).
    The decision_process state (flip_rate, process_score_ewma) is the only
    thing we lose; the nightly batch can rebuild that from positions later
    via `profiler.rebuild_order_process()`.
    """
    stats = {
        "trades_seen": 0,
        "trades_dispatched": 0,
        "skipped_bad": 0,
        "errors": 0,
        "started_at": time.monotonic(),
    }

    last_time: datetime | None = None
    last_id: int = 0

    while True:
        async with get_db() as conn:
            if last_time is None:
                rows = await conn.fetch(
                    """
                    SELECT id, time, market_id, token_id, wallet_address,
                           side, price, size_usdc, is_leader, source, category
                    FROM trades_observed
                    WHERE source IS DISTINCT FROM 'onchain'
                    ORDER BY time ASC, id ASC
                    LIMIT $1
                    """,
                    CHUNK_SIZE,
                )
            else:
                rows = await conn.fetch(
                    """
                    SELECT id, time, market_id, token_id, wallet_address,
                           side, price, size_usdc, is_leader, source, category
                    FROM trades_observed
                    WHERE (time, id) > ($1, $2)
                      AND source IS DISTINCT FROM 'onchain'
                    ORDER BY time ASC, id ASC
                    LIMIT $3
                    """,
                    last_time,
                    last_id,
                    CHUNK_SIZE,
                )

        if not rows:
            break

        for row in rows:
            stats["trades_seen"] += 1
            row_d = dict(row)
            try:
                trade = _row_to_trade_dict(row_d)
            except Exception as e:
                logger.debug(f"skip bad row: {e}")
                stats["skipped_bad"] += 1
                continue

            # Dispatch in pipeline order.
            try:
                await position_tracker.on_trade(trade)
            except Exception as e:
                stats["errors"] += 1
                logger.debug(f"position_tracker.on_trade failed: {e}")

            try:
                await graph_engine.on_trade(trade)
            except Exception as e:
                stats["errors"] += 1
                logger.debug(f"graph_engine.on_trade failed: {e}")

            # behavior_profiler.on_leader_trade is gated on is_leader=True.
            # Optional: skip during the replay (per-trade DB cost dominates
            # the loop); rebuild decision_process state in the nightly batch.
            if not skip_leader_trade_profiler:
                try:
                    if trade["is_leader"]:
                        await profiler.on_leader_trade(trade)
                except Exception as e:
                    stats["errors"] += 1
                    logger.debug(f"profiler.on_leader_trade failed: {e}")

            stats["trades_dispatched"] += 1

            if stats["trades_seen"] % PROGRESS_EVERY == 0:
                elapsed = time.monotonic() - stats["started_at"]
                rate = stats["trades_seen"] / max(elapsed, 0.001)
                logger.info(
                    f"  [{stats['trades_seen']:>9,}] trades replayed "
                    f"in {elapsed/60:.1f} min ({rate:.0f}/s, "
                    f"errors={stats['errors']})"
                )

        last_time = rows[-1]["time"]
        last_id = rows[-1]["id"]

    stats["elapsed_s"] = time.monotonic() - stats["started_at"]
    return stats


# --------------------------------------------------------------------------- #
# Driver                                                                       #
# --------------------------------------------------------------------------- #


async def run(args: argparse.Namespace) -> int:
    logger.info("=" * 70)
    logger.info("REPLAY OBSERVED TRADES — rebuild downstream pipeline")
    logger.info("=" * 70)

    await initialize_pool(
        dsn=settings.DATABASE_URL,
        min_size=settings.DB_POOL_MIN,
        max_size=settings.DB_POOL_MAX,
    )

    try:
        # ---- 1. Pre-flight counts ------------------------------------- #
        logger.info("Pre-flight counts (BEFORE replay):")
        before = await count_inputs()
        for k, v in before.items():
            logger.info(f"  {k:<28} = {v:>12,}")

        if before["trades_observed"] == 0:
            logger.warning("No trades to replay. Exiting.")
            return 0

        # Preview the leaders promotion impact.
        async with get_db() as conn:
            await conn.execute("SET statement_timeout = 0")
            promote_preview = await conn.fetchrow(
                """
                WITH ws AS (
                    SELECT wallet_address, SUM(size_usdc) AS v, COUNT(*) AS n
                    FROM trades_observed
                    WHERE source IS DISTINCT FROM 'onchain'
                    GROUP BY wallet_address
                )
                SELECT
                    COUNT(*) FILTER (
                        WHERE v >= 10000 AND n >= 30
                          AND NOT EXISTS (SELECT 1 FROM leaders l WHERE l.wallet_address=ws.wallet_address)
                    ) AS would_promote,
                    COUNT(*) FILTER (
                        WHERE v >= 10000 AND n >= 30
                          AND EXISTS (SELECT 1 FROM leaders l WHERE l.wallet_address=ws.wallet_address)
                    ) AS already_in_leaders
                FROM ws
                """,
                timeout=600.0,
            )
        logger.info(
            f"  leaders promotion preview: "
            f"would_promote={int(promote_preview['would_promote'] or 0):,}, "
            f"already_in_leaders={int(promote_preview['already_in_leaders'] or 0):,}"
        )

        # Show how many trades would be re-flagged as leader (or un-flagged).
        async with get_db() as conn:
            await conn.execute("SET statement_timeout = 0")
            flag_preview = await conn.fetchrow(
                """
                SELECT
                    COUNT(*) FILTER (WHERE t.is_leader = FALSE AND l.wallet_address IS NOT NULL)
                        AS would_flag,
                    COUNT(*) FILTER (WHERE t.is_leader = TRUE AND l.wallet_address IS NULL)
                        AS would_unflag,
                    COUNT(*) FILTER (WHERE t.is_leader = TRUE) AS currently_flagged
                FROM trades_observed t
                LEFT JOIN leaders l
                  ON l.wallet_address = t.wallet_address
                 AND l.on_watchlist = TRUE
                 AND l.excluded = FALSE
                """,
                timeout=600.0,
            )
        logger.info(
            f"  is_leader preview: "
            f"currently_flagged={int(flag_preview['currently_flagged'] or 0):,}, "
            f"would_flag={int(flag_preview['would_flag'] or 0):,}, "
            f"would_unflag={int(flag_preview['would_unflag'] or 0):,}"
        )

        if args.dry_run:
            logger.info("")
            logger.info("DRY-RUN: would")
            logger.info(
                f"  1. Promote up to {int(promote_preview['would_promote'] or 0):,} "
                "top wallets into the leaders table (vol>=$10K, trades>=30)"
            )
            logger.info(
                f"  2. Re-flag is_leader on trades_observed "
                f"(+{int(flag_preview['would_flag'] or 0):,}, "
                f"-{int(flag_preview['would_unflag'] or 0):,})"
            )
            logger.info(
                "  3. TRUNCATE positions_reconstructed, follower_edges, "
                "position_tracker_state"
            )
            logger.info("  4. RESET leader_profiles (profile_json + counters)")
            logger.info(
                f"  5. Replay {before['trades_observed']:,} trades through "
                "position_tracker + graph_engine + behavior_profiler"
            )
            logger.info("Exiting without writes.")
            return 0

        # ---- 2a. Promote top wallets to leaders ------------------------ #
        logger.info("")
        logger.info("Promoting top wallets to leaders…")
        promo_stats = await promote_top_wallets_to_leaders(
            min_volume_usdc=args.min_volume,
            min_trades=args.min_trades,
            max_count=args.max_promote,
        )
        logger.info(
            f"  ✓ inserted={promo_stats['inserted']:,}, "
            f"already_in_leaders={promo_stats['already_in_leaders']:,}"
        )

        # ---- 2b. Reconcile is_leader flag on trades_observed ---------- #
        logger.info("")
        logger.info("Reconciling is_leader flag on trades_observed…")
        flag_stats = await flag_is_leader_in_trades()
        logger.info(
            f"  ✓ flagged={flag_stats['flagged']:,}, "
            f"unflagged={flag_stats['unflagged']:,}"
        )

        # ---- 2b. Reset downstream tables ------------------------------ #
        logger.info("")
        logger.info("Resetting downstream tables…")
        await reset_downstream_tables()

        # ---- 3. Wire up components with InProcessRedis ----------------- #
        logger.info("")
        logger.info("Instantiating in-process pipeline…")
        real_redis = redis_async.from_url(
            settings.REDIS_URL, decode_responses=True
        )
        in_redis = InProcessRedis(real_client=real_redis)

        # IMPORTANT: error_model=None during replay.
        #
        # BehaviorProfiler.on_position_closed → error_model.update() runs
        # CUSUM drift detection on every close, and a single drift event
        # triggers a synchronous phase-upgrade refit (BayesianRidge / LightGBM
        # over the wallet's whole resolved history). With 1.2M trades fanning
        # into tens of thousands of closes, that adds up to thousands of refits
        # → the replay slows to ~50 trades/s and would take >24h.
        #
        # The correct pattern is: replay first (positions / edges / profile
        # JSON), THEN run `scripts.batch_runner step_refit_error_models` once
        # which upgrades each leader's error model exactly once based on its
        # final positions_resolved count. The batch path is 10-100x faster
        # because it skips the per-close churn.
        profiler = BehaviorProfiler(
            redis_client=in_redis, error_model=None
        )
        position_tracker = PositionTracker(redis_client=in_redis)
        graph_engine = GraphEngine(redis_client=in_redis)

        # When position_tracker closes a position it publishes "positions:closed";
        # we route that straight into behavior_profiler.on_position_closed.
        async def _on_positions_closed(event: dict) -> None:
            try:
                await profiler.on_position_closed(event)
            except Exception as e:
                logger.debug(f"profiler.on_position_closed failed: {e}")

        in_redis.add_handler("positions:closed", _on_positions_closed)

        # ---- 4. Pre-load markets cache --------------------------------- #
        n_markets = await preload_markets(position_tracker)
        logger.info(f"  ✓ pre-loaded {n_markets:,} markets into "
                    f"position_tracker._market_tokens cache")

        # ---- 4b. Monkey-patch: disable position_tracker_state writes ---- #
        # The state table exists ONLY so a live observer crash can warm-start
        # without losing in-flight opens. During this batch replay there is
        # no concurrent observer (we stop it) and a crash would just mean
        # rerunning the script, so persistence is dead weight: it doubles
        # the number of DB writes (one UPSERT per BUY + one DELETE per
        # CLOSE). Skipping it 2-3x the throughput.
        async def _noop_state_write(*_args, **_kwargs):
            return None

        position_tracker._persist_open_state = _noop_state_write
        position_tracker._sync_state_after_close = _noop_state_write
        position_tracker._sync_state_after_eviction = _noop_state_write
        logger.info(
            "  ✓ monkey-patched _persist_open_state / _sync_state_after_* "
            "(skip persistence — saves ~50% of DB writes)"
        )

        # Replace _close_position with a lean version: INSERT positions_reconstructed
        # only, no trend/category lookup, no Redis publish. We still invoke the
        # in-process handler manually so behavior_profiler.on_position_closed
        # still updates leader_profiles.
        from src.economics.fees import calculate_polymarket_fee
        from src.economics.models import ECONOMIC_MODEL_VERSION, LiquidityRole
        from src.economics.pnl import calculate_long_pnl

        # pre-load markets.category for fast lookup
        async with get_db() as conn:
            cat_rows = await conn.fetch(
                "SELECT market_id, category FROM markets WHERE category IS NOT NULL"
            )
        markets_category = {r["market_id"]: r["category"] for r in cat_rows}
        logger.info(f"  ✓ pre-loaded {len(markets_category):,} market categories")

        # Buffer for bulk INSERT every BUFFER_FLUSH rows
        positions_buffer: list[tuple] = []
        BUFFER_FLUSH = 500

        async def _flush_positions():
            if not positions_buffer:
                return
            async with get_db() as flush_conn:
                await flush_conn.execute("SET statement_timeout = 0")
                await flush_conn.executemany(
                    """
                    INSERT INTO positions_reconstructed
                        (wallet_address, market_id, token_id, direction,
                         open_time, close_time, entry_price, exit_price,
                         size_usdc, pnl_usdc, pnl_pct, holding_period_s, close_method,
                         size_shares, entry_fee_usdc, exit_fee_usdc, gross_pnl_usdc,
                         net_pnl_usdc, economic_model_version, category)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14,$15,$16,$17,$18,$19,$20)
                    """,
                    list(positions_buffer),
                )
            positions_buffer.clear()

        async def _lean_close_position(
            pos, close_time, exit_price, close_shares, close_method
        ):
            """Lean replacement for PositionTracker._close_position.

            Differences from the real implementation:
              * No category lookup from markets (we use pre-loaded dict)
              * No trend lookup → is_contrarian is always False (acceptable
                for batch replay; full pipeline rebuilds it from on_leader_trade)
              * No DELETE FROM position_tracker_state (state writes disabled)
              * No Redis publish — we call profiler.on_position_closed directly
              * Buffered INSERT (flush every BUFFER_FLUSH rows)
            """
            entry_cost = pos.entry_price * close_shares
            entry_fee = calculate_polymarket_fee(
                shares=close_shares,
                price=pos.entry_price,
                fee_rate=pos.fee_rate_pct,
                liquidity_role=LiquidityRole.TAKER,
                fees_enabled=True,
            )
            exit_fee = calculate_polymarket_fee(
                shares=close_shares,
                price=exit_price,
                fee_rate=pos.fee_rate_pct,
                liquidity_role=LiquidityRole.TAKER,
                fees_enabled=True,
            )
            pnl = calculate_long_pnl(
                entry_price=pos.entry_price,
                exit_price=exit_price,
                size_shares=close_shares,
                entry_fee_usdc=entry_fee,
                exit_fee_usdc=exit_fee,
            )
            holding_s = int((close_time - pos.open_time).total_seconds())
            category = markets_category.get(pos.market_id, "unknown")

            positions_buffer.append((
                pos.wallet_address, pos.market_id, pos.token_id, pos.direction,
                pos.open_time, close_time, pos.entry_price, exit_price,
                entry_cost, round(pnl.net_pnl_usdc, 2), round(pnl.pnl_pct, 4),
                holding_s, close_method, close_shares, entry_fee, exit_fee,
                pnl.gross_pnl_usdc, pnl.net_pnl_usdc,
                ECONOMIC_MODEL_VERSION, category,
            ))
            if len(positions_buffer) >= BUFFER_FLUSH:
                await _flush_positions()

            # In-process dispatch to profiler.on_position_closed so the
            # leader_profile JSON still gets enriched.
            event = {
                "wallet_address": pos.wallet_address,
                "market_id": pos.market_id,
                "token_id": pos.token_id,
                "direction": pos.direction,
                "open_time": pos.open_time.isoformat(),
                "close_time": close_time.isoformat(),
                "pnl_usdc": str(round(pnl.net_pnl_usdc, 2)),
                "gross_pnl_usdc": str(round(pnl.gross_pnl_usdc, 2)),
                "category": category,
                "size_usdc": str(entry_cost),
                "size_shares": str(close_shares),
                "entry_price": str(pos.entry_price),
                "exit_price": str(exit_price),
                "economic_model_version": ECONOMIC_MODEL_VERSION,
                "holding_period_s": holding_s,
                "is_contrarian": False,
                "close_method": close_method,
            }
            try:
                await profiler.on_position_closed(event)
            except Exception:
                pass

        position_tracker._close_position = _lean_close_position
        logger.info(
            "  ✓ monkey-patched _close_position (lean: buffered INSERT, "
            "no trend/state writes; flush every 500 closes)"
        )
        # Stash flush callback for end-of-replay flush
        run._positions_flush = _flush_positions

        # ---- 5. Replay loop -------------------------------------------- #
        logger.info("")
        logger.info(
            f"Starting replay (fast_mode={args.fast_mode}, "
            f"this may take 30-120 min)…"
        )
        stats = await replay_trades(
            position_tracker,
            graph_engine,
            profiler,
            skip_leader_trade_profiler=args.fast_mode,
        )
        # Flush any positions buffered by the lean _close_position.
        await _flush_positions()
        logger.info("  ✓ flushed remaining positions buffer")
        logger.info("")
        logger.info(
            f"Replay done: {stats['trades_dispatched']:,} trades dispatched "
            f"in {stats['elapsed_s']/60:.1f} min "
            f"(rate ≈ {stats['trades_dispatched']/max(stats['elapsed_s'],1):.0f}/s, "
            f"errors={stats['errors']}, skipped={stats['skipped_bad']})"
        )
        logger.info(
            f"InProcessRedis dispatch counts: {in_redis._publish_count}"
        )

        # ---- 6. Post-flight counts ------------------------------------- #
        logger.info("")
        logger.info("Post-flight counts (AFTER replay):")
        after = await count_inputs()
        for k, v in after.items():
            delta = v - before.get(k, 0)
            sign = "+" if delta >= 0 else ""
            logger.info(
                f"  {k:<28} = {v:>12,}  ({sign}{delta:,} vs before)"
            )

        await in_redis.aclose()
        return 0
    finally:
        await close_pool()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show pre-flight counts and exit — no truncate, no replay.",
    )
    parser.add_argument(
        "--min-volume",
        type=float,
        default=10_000.0,
        help="Min observed volume (USDC) to promote a wallet into leaders. Default $10K.",
    )
    parser.add_argument(
        "--min-trades",
        type=int,
        default=30,
        help="Min observed trades to promote a wallet into leaders. Default 30.",
    )
    parser.add_argument(
        "--max-promote",
        type=int,
        default=1500,
        help="Cap on promoted wallets. Default 1500.",
    )
    parser.add_argument(
        "--fast-mode",
        action="store_true",
        default=True,
        help="Skip profiler.on_leader_trade during replay (decision_process "
             "state will be rebuilt by batch_runner step_backfill_decision_learning). "
             "DEFAULT: enabled (5-10x faster).",
    )
    parser.add_argument(
        "--full-mode",
        dest="fast_mode",
        action="store_false",
        help="Run profiler.on_leader_trade on every trade (slow, "
             "~16h for 1.2M trades).",
    )
    args = parser.parse_args()
    sys.exit(asyncio.run(run(args)))


if __name__ == "__main__":
    main()
