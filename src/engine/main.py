"""Entry point for the Intelligence Engine (Confidence + PaperTrader + RiskManager + TelegramBot)."""

import asyncio
import json
import signal
import traceback

import redis.asyncio as redis_async
from loguru import logger

from src.config import settings
from src.control.killswitch import get_killswitch
from src.control.price_oracle import PriceOracle
from src.control.runtime_config import get_runtime_config, init_runtime_config
from src.database.connection import close_pool, initialize_pool
from src.engine.confidence_engine import ConfidenceEngine
from src.engine.decision_router import DecisionRouter
from src.engine.jobs import (
    make_killswitch_sync_job,
    make_nightly_batch_job,
    make_redis_cleanup_job,
    make_refresh_thresholds_job,
)
from src.engine.paper_trader import PaperTrader
from src.engine.risk_manager import RiskManager
from src.engine.scheduler import Scheduler
from src.engine.watchdog import Watchdog
from src.graph.graph_engine import GraphEngine
from src.logging_setup import configure_logging
from src.monitoring.ingest_health import (
    SOURCE_FALCON_LEADERBOARD,
    SOURCE_FALCON_MARKETS,
    SOURCE_FALCON_TRADES,
    SOURCE_FALCON_WALLET360,
    SOURCE_REDIS_PUBSUB,
    SOURCE_REST_DATA_API,
    SOURCE_WS_MARKET_FEED,
    get_health_monitor,
)
from src.observer.orderbook_observer import OrderBookObserver
from src.profiler.behavior_profiler import BehaviorProfiler
from src.profiler.error_model import ErrorModel
from src.telegram_bot import TelegramBot
from src.telegram_bot.notifier import CHANNEL_INGEST_GAP

# S3.9: any unhandled exception in main() publishes here so the
# Telegram notifier alerts the operator before the process dies.
ENGINE_CRASH_CHANNEL = "engine:crash"


def _make_falcon_alert_recovery(redis_client, source: str):
    """Build an alert-only recovery callback for a Falcon (or WS) source.

    Phase 3 Task D constraint: auto-recovery for Falcon-source gaps must
    NOT auto-retry HTTP calls. Hammering Falcon when it's already
    rate-limiting us is the opposite of what we want. The recovery here
    is "page the operator + log". The TelegramNotifier rate-limits at
    1 alert per INGEST_ALERT_COOLDOWN_S (default 300 s) per source.
    """

    async def _callback(_source: str, duration_s: float) -> None:
        from src.monitoring.ingest_health import DEFAULT_THRESHOLDS_S

        threshold = DEFAULT_THRESHOLDS_S.get(_source)
        severity = "critical" if duration_s > 2 * (threshold or 0) else "warning"
        payload = {
            "source": _source,
            "duration_s": float(duration_s),
            "severity": severity,
            "threshold_s": threshold,
        }
        try:
            await redis_client.publish(CHANNEL_INGEST_GAP, json.dumps(payload))
        except Exception as exc:
            logger.warning(
                f"ingest_health: publish to {CHANNEL_INGEST_GAP} failed for "
                f"{_source!r}: {exc}"
            )

    return _callback


def _make_rest_api_recovery(redis_client):
    """REST data-api recovery: alert + log only.

    The audit warns against eager retries — a gap usually means data-api
    is down or our outbound IP is rate-limited. Nothing we do in-process
    fixes that. We page the operator and let the regular 5 s poll loop
    keep trying with its existing retry/backoff logic.
    """
    return _make_falcon_alert_recovery(redis_client, SOURCE_REST_DATA_API)


def _make_redis_pubsub_recovery(redis_client):
    """Redis pub/sub recovery: alert the operator.

    Per-Subscriber rebuilds happen inside ``Subscriber.restart()``, but
    the engine container owns many subscribers — orchestrating their
    restart from here would require cross-component state we don't have.
    Instead we surface the gap to the operator; the watchdog's
    ``polybot_redis_subscriber_reconnects_total`` counter will tell us
    if Redis itself is flapping.
    """
    return _make_falcon_alert_recovery(redis_client, SOURCE_REDIS_PUBSUB)


async def _publish_crash(redis_client, component: str, exc: BaseException) -> None:
    """Best-effort crash broadcast. Never raises — we're already on
    the way down."""
    try:
        await redis_client.publish(
            ENGINE_CRASH_CHANNEL,
            json.dumps(
                {
                    "component": component,
                    "error_type": exc.__class__.__name__,
                    "error": str(exc)[:500],
                    "traceback": traceback.format_exc()[:2000],
                }
            ),
        )
    except Exception:
        pass


async def main() -> None:
    level = configure_logging()
    logger.info(f"Starting Intelligence Engine (log_level={level})")
    await initialize_pool(
        dsn=settings.DATABASE_URL,
        min_size=settings.DB_POOL_MIN,
        max_size=settings.DB_POOL_MAX,
    )
    redis_client = redis_async.from_url(settings.REDIS_URL, decode_responses=True)

    # B3 fix (2026-05-19): publish a persistent engine-boot timestamp so
    # the API (a separate, often-restarted container) can compute a real
    # uptime instead of resetting to 0 every time the API process is
    # recycled. SET NX = "set only if missing" — the value survives API
    # restarts but is intentionally rewritten on engine cold-starts so
    # operator-driven engine restarts surface as a fresh uptime.
    try:
        import time as _time_mod
        await redis_client.set(
            "bot:engine:started_at",
            str(_time_mod.time()),
        )
        logger.info("bot:engine:started_at written to Redis")
    except Exception as exc:
        logger.warning(f"Failed to write bot:engine:started_at: {exc}")

    # Killswitch service shared across components — the Telegram bot
    # mutates it via /killswitch and /pause /resume, RiskManager reads
    # it on every trade attempt.
    killswitch = get_killswitch(redis_client=redis_client)
    # Runtime config (Risk & Config Option 2). Reads/writes share the
    # same Redis key as the API service so dashboard edits propagate
    # to the engine within the 30s cache TTL — or now <100ms via the
    # runtime_config:changed pub/sub channel below.
    init_runtime_config(redis_client=redis_client)
    # Phase 2 Task D: push-invalidate the local override cache whenever
    # the API publishes a change on runtime_config:changed (audit Red
    # Flag #6). Without this every RiskManager check ate the 30s TTL.
    await get_runtime_config().start_pubsub()

    # S3.11: error_model takes a redis_client so drift + phase upgrade
    # events surface to Telegram. None = silent (test fixtures).
    error_model = ErrorModel(redis_client=redis_client)
    profiler = BehaviorProfiler(redis_client=redis_client, error_model=error_model)
    # DecisionRouter (S2.7): in-memory router that decides whether each
    # decision goes to paper, live, or both — based on TRADING_MODE env
    # plus a Redis runtime override.
    decision_router = DecisionRouter(redis_client=redis_client)
    confidence = ConfidenceEngine(
        redis_client=redis_client,
        behavior_profiler=profiler,
        error_model=error_model,
        decision_router=decision_router,
    )
    # S3.11: risk_manager takes a redis_client so circuit-breaker trips
    # and drawdown threshold crossings surface to Telegram.
    risk_manager = RiskManager(redis_client=redis_client)
    # Pillar 1 (audit 2026-05-17): the PriceOracle is the canonical
    # source for close-time exit prices. Owns its own aiohttp session
    # for Gamma /markets queries (cached 60s) and reuses the engine's
    # redis client for the fresh-book step. Shutdown is handled in the
    # main() finally block via aclose().
    price_oracle = PriceOracle(
        redis_client=redis_client,
        gamma_cache_ttl_s=60.0,
    )
    paper_trader = PaperTrader(
        redis_client=redis_client,
        confidence_engine=confidence,
        risk_manager=risk_manager,  # FIX 4
        price_oracle=price_oracle,  # Pillar 1
    )
    # GraphEngine: subscribes to trades:observed, builds the leader→follower
    # social graph in `follower_edges`. Without this, FOLLOW_MIN_FOLLOWERS=5
    # is never reached and FOLLOW signals stay locked behind cold-start
    # forever. The hot path is O(1) per trade; warm-start hydrates from
    # the last 30 minutes of trades_observed so a restart doesn't lose
    # in-flight follower windows.
    graph = GraphEngine(redis_client=redis_client)
    # OrderBookObserver (Phase 3 Round 2 Agent Z): per-minute rollup of
    # `book_quality_snapshots` → `orderbook_features_minute`. The raw
    # feed is written by the trade observer container (`trade_observer.
    # _record_book_metrics` → `_persist_book_quality_snapshot`); this
    # rollup is read-only on the source. Sleep cadence is 60 s with a
    # 70 s lookback for boundary safety. Best-effort: a missed minute
    # is missed (backfill is explicit, not live-path).
    orderbook_observer = OrderBookObserver(redis_client=redis_client)
    # TelegramBot (S3.9): receives push alerts for opens/closes/killswitch/
    # crash and exposes /status, /pnl, /positions, /mode, /killswitch,
    # /pause, /resume. Disabled by default — see TELEGRAM_ENABLED in
    # .env.example.
    telegram_bot = TelegramBot(
        redis_client=redis_client,
        killswitch=killswitch,
        paper_trader=paper_trader,
        # NOTE: live_trader runs in its own process; the engine container
        # only owns paper. /positions on live still works via DB snapshot.
        live_trader=None,
    )

    # ---- Phase 3 Task D: Ingest Health Monitor ------------------------- #
    # Tracks freshness of every ingestion source (WS, REST data-api,
    # Falcon agents, Redis pub/sub). Recovery callbacks page the operator
    # on Falcon gaps and force-reconnect the WS / pub/sub on transient
    # network gaps. Auto-recovery for Falcon endpoints is intentionally
    # alert-only — re-hammering the API when it's already rate-limiting
    # us is the opposite of what we want.
    health_monitor = get_health_monitor()
    health_monitor.register_recovery(
        SOURCE_REDIS_PUBSUB,
        _make_redis_pubsub_recovery(redis_client),
    )
    for falcon_source in (
        SOURCE_FALCON_LEADERBOARD,
        SOURCE_FALCON_WALLET360,
        SOURCE_FALCON_MARKETS,
        SOURCE_FALCON_TRADES,
    ):
        health_monitor.register_recovery(
            falcon_source, _make_falcon_alert_recovery(redis_client, falcon_source)
        )
    health_monitor.register_recovery(
        SOURCE_REST_DATA_API,
        _make_rest_api_recovery(redis_client),
    )
    # WS recovery is wired LATER (after the observer process is reachable
    # via Redis pub/sub). For the engine container, we keep WS recovery
    # as an operator alert — the observer container does the actual
    # force_reconnect from its own ingest-health bootstrap.
    health_monitor.register_recovery(
        SOURCE_WS_MARKET_FEED,
        _make_falcon_alert_recovery(redis_client, SOURCE_WS_MARKET_FEED),
    )
    await health_monitor.start()
    # -------------------------------------------------------------------- #

    stop_event = asyncio.Event()

    def handle_signal(*_):
        logger.info("Shutting down Intelligence Engine")
        stop_event.set()

    loop = asyncio.get_event_loop()
    loop.add_signal_handler(signal.SIGTERM, handle_signal)
    loop.add_signal_handler(signal.SIGINT, handle_signal)

    # ---- S3.10: Watchdog + APScheduler --------------------------------- #
    # Long-running coroutines are owned by the watchdog (auto-restart on
    # crash, alerts via engine:crash → Telegram). Periodic work runs
    # under the APScheduler.
    watchdog = Watchdog(redis_client=redis_client, stop_event=stop_event)
    await watchdog.register("profiler", profiler.start)
    await watchdog.register("confidence", confidence.start)
    await watchdog.register("paper_trader", paper_trader.start)
    await watchdog.register("graph", graph.start)
    await watchdog.register("telegram_bot", telegram_bot.start)
    # Wired AFTER graph so the source-of-truth raw feed (written by the
    # trade observer container) has had time to flow — the rollup
    # tolerates an empty window cleanly but starting the loop earlier
    # buys us nothing.
    await watchdog.register("orderbook_observer", orderbook_observer.start)

    # Round 7 supplement (2026-05-17 LAB diagnostic) — IntentRouter.
    # The mempool daemon publishes intents to the `mempool:leader_intent`
    # Redis stream (3520+ messages observed in prod), but the IntentRouter
    # consumer was never instantiated. Without this wiring R7 writes 0
    # rows to mempool_observations forever, blocking both shadow PnL
    # measurement AND R10 LeaderGasQuirkDetector (which reads from
    # mempool_observations). Routing in SHADOW mode by default —
    # `prefill_live_enabled` runtime flag stays False until the operator
    # explicitly enables LIVE firing via the LAB tab.
    try:
        from src.execution.prefill.intent_router import IntentRouter

        class _NoOpPreSignedPool:
            """Minimal pool stub for SHADOW mode. The IntentRouter's
            `_rotation_loop` calls `expire_stale()` every tick; in
            SHADOW we never `fire()`, so a true PreSignedPool with
            CLOB signing + market scanning is unnecessary. Replace with
            the real :class:`src.execution.prefill.pool.PreSignedPool`
            when `prefill_live_enabled` is about to be flipped ON.
            """
            async def expire_stale(self) -> int:
                return 0

        intent_router = IntentRouter(
            pool=_NoOpPreSignedPool(),
            live_trader=None,           # not touched in SHADOW path
            paper_trader=paper_trader,
            confidence_engine=confidence,
            risk_manager=risk_manager,
            killswitch=killswitch,
            runtime_config=get_runtime_config(),
        )
        await watchdog.register("intent_router", intent_router.start)
    except Exception as exc:  # pragma: no cover — graceful degrade
        logger.warning(
            f"Engine: skipping IntentRouter wiring: {exc}"
        )

    scheduler = Scheduler()
    scheduler.add_cron(
        "nightly_batch",
        make_nightly_batch_job(),
        hour=settings.BATCH_HOUR_UTC,
    )
    scheduler.add_cron(
        "redis_cleanup",
        make_redis_cleanup_job(redis_client),
        hour=settings.REDIS_CLEANUP_HOUR_UTC,
    )
    scheduler.add_interval(
        "killswitch_sync",
        make_killswitch_sync_job(killswitch),
        seconds=settings.KILLSWITCH_SYNC_INTERVAL_S,
    )
    scheduler.add_interval(
        "watchdog",
        watchdog.tick,
        seconds=settings.WATCHDOG_HEARTBEAT_INTERVAL_S,
    )
    # Adaptive thresholds: recompute every 5 min so the cold-start gates
    # progressively relax as profiles_with_data / resolved_total /
    # confirmed_edges grow. Without this job firing, the static cold
    # values from settings.* are used forever.
    scheduler.add_interval(
        "refresh_thresholds",
        make_refresh_thresholds_job(),
        seconds=300,
    )

    # ---- S3.11: Telegram digests + alerts evaluator ------------------- #
    # Three new periodic jobs feeding the Telegram bot:
    #  * hourly_digest — pushes a rolling 60min summary IF activity exists
    #  * daily_digest — pushes a full snapshot every day at the configured
    #                   UTC hour (default 23:00)
    #  * alerts_eval — evaluates configurable threshold rules every 60s
    # All gated on the bot being enabled; otherwise the jobs are no-ops.
    try:
        from src.telegram_bot import digest as digest_mod

        async def _hourly_digest_job() -> None:
            if not settings.TELEGRAM_DIGEST_HOURLY_ENABLED:
                return
            if telegram_bot.notifier is None:
                return
            payload = await digest_mod.build_hourly_digest(
                redis_client=redis_client, paper_trader=paper_trader
            )
            if payload is None:
                return
            from src.telegram_bot import formatters
            text = formatters.format_digest_hourly(payload)
            await telegram_bot.notifier.push(text, tier=2)  # INFO tier

        async def _daily_digest_job() -> None:
            if not settings.TELEGRAM_DIGEST_DAILY_ENABLED:
                return
            if telegram_bot.notifier is None:
                return
            payload = await digest_mod.build_daily_digest(
                redis_client=redis_client, paper_trader=paper_trader
            )
            from src.telegram_bot import formatters
            text = formatters.format_digest_daily(payload)
            await telegram_bot.notifier.push(text, tier=1)  # ALERT tier

        async def _alerts_eval_job() -> None:
            if telegram_bot.alerts_mgr is None or telegram_bot.notifier is None:
                return
            fired = await telegram_bot.alerts_mgr.evaluate(paper_trader=paper_trader)
            for _rule, msg in fired:
                await telegram_bot.notifier.push(msg, tier=1)  # ALERT tier

        scheduler.add_interval(
            "telegram_hourly_digest",
            _hourly_digest_job,
            seconds=3600,
        )
        scheduler.add_cron(
            "telegram_daily_digest",
            _daily_digest_job,
            hour=settings.TELEGRAM_DIGEST_DAILY_HOUR_UTC,
        )
        scheduler.add_interval(
            "telegram_alerts_eval",
            _alerts_eval_job,
            seconds=60,
        )
    except Exception as exc:  # pragma: no cover — graceful degrade
        logger.warning(f"Engine: skipping Telegram digest/alerts wiring: {exc}")
    # Round 9 (The Web) — nightly multivariate Hawkes refit. Lives at
    # 03:30 UTC, right after the R5 bivariate batch window so the two
    # fitters don't fight for DB / CPU. The job is gated on the R9
    # daemon module being importable; if the module is stripped from a
    # deployment (e.g. test env), we silently skip the registration.
    try:
        from src.follower_volume.daemon import FollowerVolumeDaemon  # noqa: F401

        async def _mvhawkes_nightly() -> None:
            daemon = FollowerVolumeDaemon()
            await daemon.run_one_pass()

        scheduler.add_cron(
            "mvhawkes_nightly",
            _mvhawkes_nightly,
            hour=getattr(settings, "MVHAWKES_BATCH_HOUR_UTC", 3),
            minute=getattr(settings, "MVHAWKES_BATCH_MINUTE_UTC", 30),
        )
    except Exception as exc:  # pragma: no cover — graceful degrade
        logger.warning(
            f"Scheduler: skipping mvhawkes_nightly registration: {exc}"
        )
    # Round 10 (The Truth Test) — nightly 2SLS pass. Runs at 04:00 UTC,
    # AFTER R9's 03:30 batch so the daemon can read freshly-written
    # multivariate_hawkes_fits rows for the side-by-side comparison.
    # Gated on the R10 daemon module being importable so a stripped
    # test env doesn't crash the engine startup.
    try:
        from src.causal.daemon import CausalDaemon  # noqa: F401

        async def _causal_nightly() -> None:
            daemon = CausalDaemon()
            await daemon.run_one_pass()

        scheduler.add_cron(
            "causal_nightly",
            _causal_nightly,
            hour=getattr(settings, "CAUSAL_DAEMON_BATCH_HOUR_UTC", 4),
        )
    except Exception as exc:  # pragma: no cover — graceful degrade
        logger.warning(
            f"Scheduler: skipping causal_nightly registration: {exc}"
        )

    # Round 10 supplement — InstrumentRegistry hourly pass.
    # Without this, the causal_nightly job runs against an empty
    # instrumental_events table and always reports estimated=0. Per the
    # 2026-05-17 LAB diagnostic, this wiring gap is the dominant reason
    # R10 has produced 0 estimates lifetime.
    #
    # We register ONLY RelatedMarketResolver here (pure-SQL, no external
    # API dep). LeaderGasQuirkDetector requires R7 mempool_observations
    # which is still empty — adding it now would just NO-OP. The other
    # detectors (NewsEventDetector, OracleUpdateDetector) need external
    # data sources / RPC subscriptions that are out of scope for this
    # job.
    try:
        from src.causal.instruments import InstrumentRegistry
        from src.causal.instruments_sql import RelatedMarketResolver

        async def _causal_instruments_hourly() -> None:
            # 2026-05-17 perf tuning: the default RelatedMarketResolver
            # (30-day lookback, min_co=5) is a wallet x market self-join
            # on trades_observed (millions of rows, never ANALYZEd) and
            # blows past the 60s statement_timeout. We tighten:
            #   * lookback 30d → 1d  : limits the inner CTE to the most
            #     recent active wallets x markets, the dominant signal.
            #   * min_co 5 → 10      : prunes the cartesian product to
            #     genuinely-shared market pairs (instrument quality is
            #     more important than coverage for the first-stage IV).
            # The job runs hourly so the 1-day window slides; we never
            # miss long-tail shared pairs we'd otherwise catch in a
            # 30-day window. Re-tune when trades_observed has been
            # ANALYZEd and the planner picks a hash join.
            registry = InstrumentRegistry()
            registry.register(
                RelatedMarketResolver(
                    lookback_days=1,
                    min_co_occurrences=10,
                    max_pairs=200,
                )
            )
            summary = await registry.run_one_pass()
            for det_name, entry in summary.get("by_detector", {}).items():
                logger.info(
                    f"instruments[{det_name}]: detected={entry.get('events_detected', 0)} "
                    f"persisted={entry.get('events_persisted', 0)} "
                    f"error={entry.get('error') or 'none'}"
                )

        scheduler.add_interval(
            "causal_instruments_hourly",
            _causal_instruments_hourly,
            seconds=3600,
        )
    except Exception as exc:  # pragma: no cover — graceful degrade
        logger.warning(
            f"Scheduler: skipping causal_instruments_hourly registration: {exc}"
        )

    # Pillar 2 (audit 2026-05-17) — Gamma reconciliation nightly.
    # Runs at 04:00 UTC, AFTER backfill_resolved_outcomes has had a
    # chance to populate markets.resolved_outcome from the closed-page
    # paginator. The reconciliation pass replays every paper trade
    # closed within the last 30 days, computes the theoretical PnL
    # Polymarket would settle, and UPSERTs into paper_close_divergences
    # when |db - truth| > 2 USDC. Publishes paper:audit:divergence to
    # Redis with the top-3-worst when at least one new divergence is
    # inscribed — the Telegram notifier picks it up (ALERT tier).
    try:
        from scripts.reconciliation import reconcile_closed_trades
        from src.database import connection as _db_conn

        async def _reconcile_closed_trades_job() -> None:
            pool = _db_conn._pool
            if pool is None:
                logger.warning(
                    "Scheduler: reconcile_closed_trades skipped — DB pool not initialized"
                )
                return
            # Reuse the PriceOracle's owned aiohttp session — it's
            # already kept alive for Gamma /markets cache; no need to
            # spin up a separate connection pool for nightly work.
            session = await price_oracle._ensure_session()
            await reconcile_closed_trades(
                pool=pool,
                redis_client=redis_client,
                http_session=session,
            )

        scheduler.add_cron(
            "reconcile_closed_trades",
            _reconcile_closed_trades_job,
            hour=4,
            minute=0,
        )
    except Exception as exc:  # pragma: no cover — graceful degrade
        logger.warning(
            f"Scheduler: skipping reconcile_closed_trades registration: {exc}"
        )
    await scheduler.start()
    # ------------------------------------------------------------------- #

    try:
        await stop_event.wait()
    except Exception as e:
        # Surface fatal crashes to Telegram before we tear down.
        logger.exception("Intelligence Engine crashed")
        await _publish_crash(redis_client, "engine", e)
        raise
    finally:
        try:
            await health_monitor.stop()
        except Exception:
            pass
        await scheduler.stop()
        await watchdog.stop_all()
        # Components are owned by the watchdog now, but we still call
        # their stop() hooks so internal state (subscriptions, polling)
        # is unwound cleanly.
        await profiler.stop()
        await confidence.stop()
        await paper_trader.stop()
        await graph.stop()
        await telegram_bot.stop()
        try:
            await orderbook_observer.stop()
        except Exception:
            pass
        try:
            await get_runtime_config().stop_pubsub()
        except Exception:
            pass
        # Pillar 1: release the PriceOracle's owned aiohttp session.
        try:
            await price_oracle.aclose()
        except Exception:
            pass
        await close_pool()
        await redis_client.aclose()
        logger.info("Intelligence Engine stopped")


if __name__ == "__main__":
    asyncio.run(main())
