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
        await close_pool()
        await redis_client.aclose()
        logger.info("Intelligence Engine stopped")


if __name__ == "__main__":
    asyncio.run(main())
