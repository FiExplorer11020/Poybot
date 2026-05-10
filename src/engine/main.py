"""Entry point for the Intelligence Engine (Confidence + PaperTrader + RiskManager + TelegramBot)."""

import asyncio
import json
import signal
import traceback

import redis.asyncio as redis_async
from loguru import logger

from src.config import settings
from src.control.killswitch import get_killswitch
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
from src.profiler.behavior_profiler import BehaviorProfiler
from src.profiler.error_model import ErrorModel
from src.telegram_bot import TelegramBot

# S3.9: any unhandled exception in main() publishes here so the
# Telegram notifier alerts the operator before the process dies.
ENGINE_CRASH_CHANNEL = "engine:crash"


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

    error_model = ErrorModel()
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
    risk_manager = RiskManager()
    paper_trader = PaperTrader(
        redis_client=redis_client,
        confidence_engine=confidence,
        risk_manager=risk_manager,  # FIX 4
    )
    # GraphEngine: subscribes to trades:observed, builds the leader→follower
    # social graph in `follower_edges`. Without this, FOLLOW_MIN_FOLLOWERS=5
    # is never reached and FOLLOW signals stay locked behind cold-start
    # forever. The hot path is O(1) per trade; warm-start hydrates from
    # the last 30 minutes of trades_observed so a restart doesn't lose
    # in-flight follower windows.
    graph = GraphEngine(redis_client=redis_client)
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
            await get_runtime_config().stop_pubsub()
        except Exception:
            pass
        await close_pool()
        await redis_client.aclose()
        logger.info("Intelligence Engine stopped")


if __name__ == "__main__":
    asyncio.run(main())
