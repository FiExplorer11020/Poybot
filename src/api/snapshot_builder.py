"""
Builds the terminal snapshot used by /api/v1/live-summary.

Runs in the maintenance container (NOT the API process) to avoid pool DB
contention. The 17 SQL queries that compose the snapshot used to fan out
in parallel inside the API request handler, saturating the DB pool and
turning sub-1s queries into 30s waits.

This module:

1. Acquires a singleton in-process lock so concurrent maintenance ticks
   serialise (one builder at a time per process).
2. Runs the 17 `queries.*` functions with bounded concurrency (max 3 in
   flight at once via a semaphore) so the pool sees at most a handful
   of connections, not 17.
3. Wraps every section in try/except — a per-section failure falls back
   to a safe default and the rest of the snapshot still gets built.
4. Composes the final dict shape via `terminal_snapshot.build_terminal_snapshot`
   so the JSON the API serves is byte-compatible with the in-process
   version.
5. Writes the JSON to Redis (`SET ... EX 120`), records the build epoch,
   and publishes a pubsub event so the WS bridge can fan out an
   `snapshot_updated` event to dashboard clients.

The function returns the snapshot dict so callers (and tests) can
inspect it directly.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from typing import Any

from loguru import logger

from src.api import queries
from src.api.terminal_snapshot import (
    build_terminal_snapshot as compose_terminal_snapshot,
)
from src.api.terminal_snapshot import load_recent_log_entries
from src.engine.neural_readiness import ReadinessInputs, build_neural_readiness_snapshot

# Redis key + channel names. The API endpoint reads from these; the WS
# bridge subscribes to the pubsub channel for push-fanout.
SNAPSHOT_REDIS_KEY = "snapshot:live_summary"
SNAPSHOT_BUILT_AT_KEY = "snapshot:live_summary:built_at"
SNAPSHOT_TTL_S = 120
SNAPSHOT_PUBSUB_CHANNEL = "snapshot:live_summary:updated"

# Singleton in-process lock — guarantees that overlapping maintenance
# ticks do not run two builds at once (build can take 20-40s under load
# even with bounded concurrency).
_BUILD_LOCK = asyncio.Lock()

# Bounded parallelism for the 17 queries. The API version fired all 17
# in parallel, saturating the 25-slot pool. Three keeps DB contention
# minimal while still cutting wall-clock time vs strictly sequential.
_MAX_PARALLEL = 3

# Default shapes — mirror the `defaults` tuple in `_get_terminal_snapshot`
# (src/api/main.py:~878). Used when a section query raises.
_DEFAULTS: dict[str, Any] = {
    "overview": {},
    "ml": {},
    "system": {},
    "positions_live": [],
    "positions": {"open": [], "closed": [], "stats": {}},
    "decisions": [],
    "decision_stats": {"totals": {}},
    "risk": {},
    "activation": [],
    "data_quality": {},
    "health": {},
    "market_rows": [],
    "observed_trades": [],
    "alpha_extras": {"timeline": [], "follow_ready": [], "totals": {}},
    "wallet_graph": {"nodes": [], "edges": [], "stats": {}},
    "rejections": {"total": 0, "breakdown": []},
    "equity_curve": {"series": [], "by_leader": [], "by_strategy": []},
}

# Section names in build order — also the keys used in the result dict
# the builder hands to `build_terminal_snapshot(...)`.
_SECTION_NAMES: tuple[str, ...] = tuple(_DEFAULTS.keys())


# --------------------------------------------------------------------------- #
# Section runners                                                             #
# --------------------------------------------------------------------------- #
async def _run_section(
    name: str,
    coro_factory,
    semaphore: asyncio.Semaphore,
    failures: list[str],
) -> Any:
    """Execute a single section coroutine under the concurrency semaphore.

    On any exception (including timeouts the caller layers on top), the
    section's default is returned and `name` is appended to `failures`
    so the caller can log a summary count.
    """
    async with semaphore:
        try:
            return await coro_factory()
        except Exception as exc:
            failures.append(name)
            logger.warning(
                f"snapshot section '{name}' failed: {type(exc).__name__}: {exc}"
            )
            return _DEFAULTS[name]


async def _health_snapshot(pool, redis_client) -> dict[str, Any]:
    """Standalone health snapshot — mirrors the API's `_health_checks()`
    output shape but only what we can compute from `pool` + `redis_client`.

    Notably skipped: the in-process Falcon probe (lives in the API
    process, not maintenance). `falcon` and `falcon_error` are left as
    `None`/`False` so the dashboard renders "unknown" rather than a
    stale value. The API may overlay its own probe before serving if it
    wants to surface it.
    """
    db_ok = False
    redis_ok = False
    last_trade_age_s: float | None = None
    websocket_connected = False
    last_message_age_s: float | None = None
    book_age_p95_s: float | None = None
    fee_snapshot_coverage_pct: float | None = None
    token_map_coverage_pct: float | None = None
    rejected_signals_1h: dict[str, int] = {}
    paper_rejections_1h: dict[str, int] = {}
    ws_msgs_last_minute = 0

    try:
        async with pool.acquire() as conn:
            last = await conn.fetchval(
                "SELECT EXTRACT(EPOCH FROM (NOW() - MAX(time))) FROM trades_observed"
            )
        db_ok = True
        last_trade_age_s = float(last) if last is not None else None
    except Exception as exc:
        logger.warning(f"health DB probe failed: {exc}")

    try:
        await redis_client.ping()
        redis_ok = True
        last_message_ts = await redis_client.get("ws:market:last_message_ts")
        if last_message_ts is not None:
            last_message_age_s = max(0.0, time.time() - float(last_message_ts))
            websocket_connected = last_message_age_s <= 30.0
        try:
            prev_minute = int(time.time() // 60) - 1
            ws_msgs = await redis_client.get(f"ws:msgs:minute:{prev_minute}")
            ws_msgs_last_minute = int(ws_msgs) if ws_msgs is not None else 0
        except Exception:
            ws_msgs_last_minute = 0
        book_age = await redis_client.get("metrics:book_age_p95_s")
        fee_coverage = await redis_client.get("metrics:fee_snapshot_coverage_pct")
        token_coverage = await redis_client.get("metrics:token_map_coverage_pct")
        rejected = await redis_client.hgetall("signals:rejected:1h")
        paper_rejected = await redis_client.hgetall("paper:rejections:1h")
        book_age_p95_s = float(book_age) if book_age is not None else None
        fee_snapshot_coverage_pct = (
            float(fee_coverage) if fee_coverage is not None else None
        )
        token_map_coverage_pct = (
            float(token_coverage) if token_coverage is not None else None
        )
        rejected_signals_1h = {
            str(reason): int(count) for reason, count in dict(rejected or {}).items()
        }
        paper_rejections_1h = {
            str(reason): int(count)
            for reason, count in dict(paper_rejected or {}).items()
        }
    except Exception as exc:
        logger.warning(f"health Redis probe failed: {exc}")

    return {
        "db": db_ok,
        "redis": redis_ok,
        # Falcon probe runs only in the API process; maintenance leaves
        # this null and the dashboard renders it as "unknown".
        "falcon": None,
        "falcon_error": None,
        "websocket": websocket_connected,
        "websocket_connected": websocket_connected,
        "last_message_age_s": last_message_age_s,
        "ws_messages_last_minute": ws_msgs_last_minute,
        "book_age_p95_s": book_age_p95_s,
        "fee_snapshot_coverage_pct": fee_snapshot_coverage_pct,
        "fee_snapshot_coverage_source": "redis" if fee_snapshot_coverage_pct else None,
        "token_map_coverage_pct": token_map_coverage_pct,
        "data_accumulation_counts": {},
        "rejected_signals_1h": rejected_signals_1h,
        "paper_rejections_1h": paper_rejections_1h,
        "pipeline_stage_health": {
            "signal_rejections_1h": rejected_signals_1h,
            "paper_rejections_1h": paper_rejections_1h,
        },
        "last_trade_age_s": last_trade_age_s,
    }


# --------------------------------------------------------------------------- #
# Main entry point                                                            #
# --------------------------------------------------------------------------- #
async def build_terminal_snapshot(pool, redis_client) -> dict[str, Any]:
    """Build the full terminal snapshot, write it to Redis, publish pubsub.

    Parameters
    ----------
    pool : asyncpg.Pool
        Live database pool (one connection acquired per section).
    redis_client : redis.asyncio.Redis
        Live Redis client (used for `data_quality`, `overview`,
        positions live prices, and the final SET + PUBLISH).

    Returns
    -------
    dict
        The composed snapshot dict — same shape as the legacy in-process
        builder produced.

    Notes
    -----
    Concurrent invocations are serialised via the module-level lock.
    Per-section errors do NOT crash the build — defaults are used and a
    summary is logged at the end. A complete Redis failure on the final
    write also does not crash; it logs and returns the dict so the
    caller can decide what to do.
    """
    async with _BUILD_LOCK:
        return await _build_locked(pool, redis_client)


async def _build_locked(pool, redis_client) -> dict[str, Any]:
    started = time.perf_counter()
    semaphore = asyncio.Semaphore(_MAX_PARALLEL)
    failures: list[str] = []

    # Per-section coroutine factories. Each factory acquires its own
    # pool connection (so the semaphore controls pool usage), then
    # calls the underlying `queries.*` function with the right args.
    # Note: we deliberately do NOT use the API's `_fetch_*_snapshot`
    # helpers — those wrap an in-process TTL cache that is meaningless
    # here (we ARE the cache producer).
    async def overview():
        async with pool.acquire() as conn:
            return await queries.overview(conn, redis_client=redis_client)

    async def ml():
        async with pool.acquire() as conn:
            return await queries.ml_summary(conn)

    async def system():
        async with pool.acquire() as conn:
            return await queries.system_status(conn)

    async def positions_live():
        async with pool.acquire() as conn:
            return await queries.open_positions_with_prices(conn, redis_client)

    async def positions():
        async with pool.acquire() as conn:
            return await queries.positions(conn)

    async def decisions():
        async with pool.acquire() as conn:
            return await queries.decisions(conn, limit=60, offset=0)

    async def decision_stats():
        async with pool.acquire() as conn:
            return await queries.decisions_stats(conn, window_hours=24)

    async def risk():
        async with pool.acquire() as conn:
            return await queries.risk(conn)

    async def activation():
        async with pool.acquire() as conn:
            return await queries.activation_queue(conn)

    async def data_quality():
        async with pool.acquire() as conn:
            return await queries.data_quality(conn, redis_client=redis_client)

    async def health():
        return await _health_snapshot(pool, redis_client)

    async def market_rows():
        async with pool.acquire() as conn:
            return await queries.market_scanner_rows(conn, limit=60)

    async def observed_trades():
        async with pool.acquire() as conn:
            return await queries.recent_observed_trades(conn, limit=60)

    async def alpha_extras():
        async with pool.acquire() as conn:
            return await queries.alpha_extras(conn)

    async def wallet_graph():
        async with pool.acquire() as conn:
            return await queries.wallet_graph(conn)

    async def rejections():
        async with pool.acquire() as conn:
            return await queries.decision_rejections_breakdown(conn, hours=1)

    async def equity_curve():
        async with pool.acquire() as conn:
            return await queries.equity_curve(conn)

    factories = {
        "overview": overview,
        "ml": ml,
        "system": system,
        "positions_live": positions_live,
        "positions": positions,
        "decisions": decisions,
        "decision_stats": decision_stats,
        "risk": risk,
        "activation": activation,
        "data_quality": data_quality,
        "health": health,
        "market_rows": market_rows,
        "observed_trades": observed_trades,
        "alpha_extras": alpha_extras,
        "wallet_graph": wallet_graph,
        "rejections": rejections,
        "equity_curve": equity_curve,
    }

    # Run all sections with bounded concurrency. `gather` returns in
    # the same order as the input — preserve that for the unpack below.
    results = await asyncio.gather(
        *(
            _run_section(name, factory, semaphore, failures)
            for name, factory in factories.items()
        )
    )
    section_data = dict(zip(_SECTION_NAMES, results))

    # Neural readiness is composed from the already-fetched sections,
    # so it does NOT consume a separate query slot.
    readiness_data = build_neural_readiness_snapshot(
        ReadinessInputs(
            health=section_data["health"],
            activation=section_data["activation"],
            risk=section_data["risk"],
            ml=section_data["ml"],
        )
    )

    # Runtime metadata — the maintenance container is the writer of
    # this snapshot, but the dashboard reads from the cached payload
    # so the timestamp here reflects when *this build* happened.
    runtime = {
        "started_at": datetime.now(timezone.utc).isoformat(),
        "uptime_seconds": 0,
        "cycle_latency_ms": 0.0,
        "last_command_at": None,
        "control_available": True,
        "config_mutable": True,
    }

    # Effective runtime overrides — best-effort. If the runtime_config
    # is not initialised in this process (it normally is in maintenance),
    # we just leave overrides as None and `build_terminal_snapshot` will
    # fall back to the defaults baked into the payload.
    runtime_overrides = None
    try:
        from src.control.runtime_config import get_runtime_config

        runtime_overrides = await get_runtime_config().effective()
    except Exception as exc:
        logger.debug(f"runtime_overrides unavailable in builder: {exc}")

    # Logs — best-effort. The maintenance container has the same log
    # paths mounted as the API (see docker-compose), so this works in
    # prod. In tests / dev it just returns an empty list.
    try:
        from pathlib import Path

        log_paths = [
            Path("/tmp/polymarket-bot-observer.log"),
            Path(__file__).parent.parent.parent / "orchestrate.log",
        ]
        logs = load_recent_log_entries(log_paths, limit=120)
    except Exception as exc:
        logger.debug(f"log loading skipped in builder: {exc}")
        logs = []

    snapshot = compose_terminal_snapshot(
        overview=section_data["overview"],
        ml=section_data["ml"],
        system=section_data["system"],
        positions_live=section_data["positions_live"],
        positions=section_data["positions"],
        decisions=section_data["decisions"],
        decision_stats=section_data["decision_stats"],
        risk=section_data["risk"],
        readiness=readiness_data,
        data_quality=section_data["data_quality"],
        health=section_data["health"],
        market_rows=section_data["market_rows"],
        observed_trades=section_data["observed_trades"],
        alpha_extras=section_data["alpha_extras"],
        wallet_graph=section_data["wallet_graph"],
        rejections=section_data["rejections"],
        equity_curve=section_data["equity_curve"],
        runtime=runtime,
        logs=logs,
        runtime_overrides=runtime_overrides,
    )
    build_ms = round((time.perf_counter() - started) * 1000, 2)
    snapshot.setdefault("bot", {})["cycle_latency_ms"] = build_ms

    # Serialise + persist. Compact separators shave ~10-15% bytes vs
    # default json output. `default=str` catches stray datetimes that
    # slipped through without explicit isoformat() calls.
    try:
        raw = json.dumps(snapshot, default=str, separators=(",", ":"))
    except Exception as exc:
        logger.exception(f"snapshot serialisation failed: {exc}")
        # Don't try to write a broken payload to Redis.
        return snapshot

    try:
        await redis_client.set(SNAPSHOT_REDIS_KEY, raw, ex=SNAPSHOT_TTL_S)
        await redis_client.set(
            SNAPSHOT_BUILT_AT_KEY, str(time.time()), ex=SNAPSHOT_TTL_S
        )
        await redis_client.publish(SNAPSHOT_PUBSUB_CHANNEL, "updated")
    except Exception as exc:
        # Redis write failure is non-fatal — the snapshot is still
        # composed correctly and returned; the next cycle will retry.
        logger.warning(f"snapshot Redis write failed: {exc}")

    logger.info(
        f"snapshot built in {build_ms}ms "
        f"(sections_failed={len(failures)}/{len(_SECTION_NAMES)}"
        f"{', failed=' + ','.join(failures) if failures else ''}, "
        f"bytes={len(raw)})"
    )
    return snapshot


__all__ = [
    "build_terminal_snapshot",
    "SNAPSHOT_REDIS_KEY",
    "SNAPSHOT_BUILT_AT_KEY",
    "SNAPSHOT_TTL_S",
    "SNAPSHOT_PUBSUB_CHANNEL",
]
