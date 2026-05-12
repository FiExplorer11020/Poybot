"""
Centralized configuration via pydantic-settings.
ALL constants from CLAUDE.md § 9, overridable via .env.

Usage:
    from src.config import settings
"""

from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    # ------------------------------------------------------------------ #
    # Database                                                            #
    # ------------------------------------------------------------------ #
    DATABASE_URL: str = "postgresql://polymarket:polymarket_dev_password@localhost:5432/polymarket"
    REDIS_URL: str = "redis://localhost:6379/0"
    DB_POOL_MIN: int = 2
    DB_POOL_MAX: int = 10

    # ------------------------------------------------------------------ #
    # Falcon API (CLAUDE.md § 5 + § 9)                                    #
    # ------------------------------------------------------------------ #
    FALCON_API_KEY: str = ""
    # Phase 3 Task B: comma-separated list of API keys for the FalconKeyPool.
    # If empty, the pool falls back to a single-key list built from
    # FALCON_API_KEY — backward-compatible. Every key has its own per-key
    # token bucket so total sustained throughput is N × FALCON_RPM_REFILL_PER_SEC
    # without violating Falcon's documented 60 RPM per-key cap.
    FALCON_API_KEYS: str = ""
    FALCON_API_URL: str = (
        "https://narrative.agent.heisenberg.so/api/v2/semantic/retrieve/parameterized"
    )
    FALCON_REFRESH_INTERVAL_S: int = 1800  # 30min — was 3600, faster convergence
    FALCON_CACHE_TTL_S: int = 172800  # 48h — survive Falcon downtime
    FALCON_MAX_REQUESTS_PER_MINUTE: int = 60
    # Phase 3 Task B: adaptive per-key token bucket. The legacy
    # FALCON_MAX_REQUESTS_PER_MINUTE is kept for backward compatibility with
    # existing call sites but the new client uses the (capacity, refill)
    # pair below per key. Defaults match the documented 60 RPM contract:
    # bucket starts full (burst of 60 calls), then 1 token/sec sustains
    # 60/min indefinitely.
    FALCON_RPM_BUCKET_CAPACITY: int = 60
    FALCON_RPM_REFILL_PER_SEC: float = 1.0
    # Backoff window in seconds after a 429: refill rate is halved for this
    # many seconds, then restored. Linear "be a good citizen" adaptive layer;
    # NOT a retry-on-429 mechanism (that would defeat the purpose).
    FALCON_BACKOFF_S: int = 60
    # Request-coalescing TTL: an in-flight call's resolved future is kept
    # for this many seconds so duplicate (agent_id, params) calls return
    # the cached result. Independent of the 48h Redis cache — this is a
    # short-window in-process dedup, only useful when two coroutines race
    # on the same params.
    FALCON_COALESCE_TTL_S: float = 30.0
    # Conditional-GET soft expiry. Once a cached payload is older than this
    # but still inside FALCON_CACHE_TTL_S, the next call performs a
    # revalidating request with If-None-Match / If-Modified-Since. A 304
    # restores TTL without burning rate-limit budget meaningfully (some
    # APIs don't charge 304s; on Falcon we still pay 1 token but skip the
    # JSON payload).
    FALCON_CONDITIONAL_REVALIDATE_S: int = 3600
    # Phase 1 Task F (audit HP-2 fix #1): in-flight Falcon HTTP concurrency.
    # The previous Semaphore(1) serialised every Falcon call across the whole
    # process; the actual ceiling is the 60 RPM token bucket below
    # (`FALCON_MAX_REQUESTS_PER_MINUTE`). With 8 in flight, the rate limiter
    # is the bound (correct), and stall recovery is ~8× faster — one slow
    # call no longer freezes every other agent_id. Validated 1..32: above 32
    # the rate limiter would just queue everyone anyway and cancellation
    # latency dominates. Override via env FALCON_MAX_CONCURRENCY.
    FALCON_MAX_CONCURRENCY: int = 8
    # Phase 1 Task F (audit HP-1 fix #2): wallet-trade backfill fan-out.
    # `_backfill_wallet_trades` iterates ~200 leader wallets each cycle with
    # an 8 s per-request timeout; serial worst-case is 200 × 8 s = 26 min.
    # With 20 concurrent workers and the 60 RPM Falcon limiter sustaining
    # ~1 call/s, throughput stays at ~60/min but a single stuck wallet no
    # longer blocks the other 19 — that's where the audit's "~16×" claim
    # comes from. Separate from FALCON_MAX_CONCURRENCY because backfill is
    # one logical batch and may want different bounds than ad-hoc Falcon
    # calls. Validated 1..64. Override via env REGISTRY_BACKFILL_CONCURRENCY.
    REGISTRY_BACKFILL_CONCURRENCY: int = 20

    # ------------------------------------------------------------------ #
    # Leader Registry (CLAUDE.md § 9)                                     #
    # ------------------------------------------------------------------ #
    INITIAL_LEADER_COUNT: int = 200
    MAX_LEADER_COUNT: int = 2000
    MIN_FALCON_SCORE: float = 0.0

    # ------------------------------------------------------------------ #
    # Trade Observer (CLAUDE.md § 9)                                      #
    # ------------------------------------------------------------------ #
    POLYMARKET_WS_URL: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    # Increased from 50 → 200 to capture more market depth. Polymarket has
    # ~1900 active markets at any time; 50 was way too narrow. With 200 we
    # cover the top tier by 24h volume + leader-active markets, which is
    # where 90%+ of leader signal lives.
    TOP_MARKETS_COUNT: int = 200
    # HP-1 fix #1 (audit docs/audit/04_perf_hotpaths.md): cut from 30 s → 5 s.
    # The CLOB market WS channel carries no wallet attribution, so every
    # leader-attributed trade is gated by this REST poll. At 30 s the median
    # leader-trade-to-react latency was ~16 s; at 5 s we expect ~2-3 s p50.
    # Bounded [MIN, MAX] in the post-init validator so an env override can't
    # tighten to a hammering loop or drift back to 30 s by accident.
    TRADE_OBSERVER_POLL_INTERVAL_S: int = 5
    TRADE_OBSERVER_POLL_INTERVAL_S_MIN: int = 1
    TRADE_OBSERVER_POLL_INTERVAL_S_MAX: int = 60
    # HP-1 fix #3: bounded queue between WS+REST producers and the dedicated
    # DB writer task. 10k @ ~1 KB/record ≈ 10 MB worst case. At burst the WS
    # coroutine should not block on Postgres — if the queue fills, drop the
    # trade and increment `observer_queue_drops_total`. Drop-on-full is
    # better than producer-side deadlock that cascades into WS pong misses.
    TRADE_OBSERVER_QUEUE_MAX: int = 10_000
    # Batch sizing: drain up to 200 rows OR 100 ms, whichever first. 200 was
    # chosen as the largest batch that still fits in a single asyncpg frame
    # without pushing parse/plan past the per-statement budget; 100 ms is
    # the target tail latency for trade-to-DB-commit (well below the 5 s
    # poll cadence so a batch never spans poll cycles in steady state).
    TRADE_OBSERVER_BATCH_MAX: int = 200
    TRADE_OBSERVER_BATCH_FLUSH_MS: int = 100
    # 500 → 1500: more historical depth on each leader_markets backfill so
    # we don't miss trades on lower-volume markets a leader is active on.
    DATA_API_GLOBAL_TRADES_LIMIT: int = 1500
    DATA_API_RECENT_LEADER_MARKETS: int = 500
    WEBSOCKET_PING_INTERVAL_S: int = 30
    WEBSOCKET_PONG_TIMEOUT_S: int = 10

    # ------------------------------------------------------------------ #
    # Phase 3 Round 1 — Data Continuity Backbone (Agent A)                #
    # ------------------------------------------------------------------ #
    # The "10-30 min pauses between continuous data gathering" pathology
    # had three root causes: (a) REST polling used time-window queries
    # with edge-case gaps, (b) leader-registry refresh was driven by a
    # FALCON_REFRESH_INTERVAL_S=1800 wall-clock timer with nothing in
    # between, (c) WS reconnect backoff could stall without any
    # event-stream freshness check. Phase 3 R1 replaces all three.

    # Cursor-driven REST polling. The cursor is a monotonic
    # `(timestamp_s, last_tx_hash)` tuple persisted in Redis as
    # `observer:cursor:trades:<source>`. On boot, if the cursor is
    # missing, we fall back to "now minus CURSOR_BOOTSTRAP_LOOKBACK_S".
    # TTL is long (the cursor is the ingestion ground-truth — losing it
    # forces a re-poll window that's bounded by this lookback).
    OBSERVER_CURSOR_TTL_S: int = 86400 * 14  # 14 days — survives a long outage
    OBSERVER_CURSOR_BOOTSTRAP_LOOKBACK_S: int = 300  # 5 min — explicit log when used

    # WS freshness watchdog. The watchdog wakes every WS_FRESHNESS_TICK_S
    # and inspects each channel's `observer:ws:last_msg:<channel>` Redis
    # key. If any channel has been silent for >`WS_CHANNEL_STALE_S`, log
    # WARNING + increment `polybot_ws_channel_stale_total{channel}` and
    # trigger a reconnect for that channel. 60 s is the right number for
    # an active Polymarket subscription — under normal load we see
    # price_change at sub-second cadence; 60 s of nothing is anomalous.
    WS_FRESHNESS_TICK_S: int = 10
    WS_CHANNEL_STALE_S: int = 60

    # WS reconnect backfill cap. On reconnect we backfill
    # `min(now - last_seen_trade_ts, WS_BACKFILL_MAX_HOURS)`. The old
    # hardcoded "fetch 1h history" was either too greedy (long downtimes
    # spent Falcon agent-556 quota reprocessing irrelevant trades) or
    # too thin (sub-hour reconnect storms missed the latest signal).
    # The clamp is the safety net.
    WS_BACKFILL_MAX_HOURS: float = 24.0

    # Event-driven Falcon refresh. The base FALCON_REFRESH_INTERVAL_S
    # (1800 s = 30 min) stays as the FLOOR — worst-case staleness upper
    # bound. On top of that, we trigger an incremental
    # `refresh_wallet(wallet, reason=...)` when:
    #   * The trade observer sees a high-volume trade by an unknown wallet
    #     (>= EVENT_REFRESH_MIN_USDC), OR
    #   * The trade observer sees EVENT_REFRESH_UNKNOWN_TRADES consecutive
    #     trades from a wallet not in the active leader set.
    # EVENT_REFRESH_COOLDOWN_S prevents the same wallet from being
    # refreshed more often than that (cheap in-memory map keyed by wallet).
    EVENT_REFRESH_MIN_USDC: float = 5_000.0
    EVENT_REFRESH_UNKNOWN_TRADES: int = 5
    EVENT_REFRESH_COOLDOWN_S: int = 300

    # Falcon daily budget guardrail for event-driven refreshes. The
    # counter lives at Redis `falcon:budget:YYYYMMDD` with TTL 25h. We
    # decrement before each refresh; if the budget is exhausted we skip
    # the refresh and increment
    # `event_driven_refreshes_total{result="budget_exhausted"}`. 500/day
    # ≈ 1 refresh every ~3 min on average, well inside the 60 RPM Falcon
    # ceiling (which is the hard limit; this is a *soft* ceiling so a
    # surge of unknown wallets doesn't blow the whole day's quota).
    FALCON_DAILY_BUDGET: int = 500

    # ------------------------------------------------------------------ #
    # Position Tracker — persistent state cap (Phase 2 Task C)            #
    # ------------------------------------------------------------------ #
    # Hard upper bound on the in-memory _open_positions dict. Persistent
    # state in `position_tracker_state` makes the dict unbounded across
    # restarts; this cap defends against runaway growth in a single
    # process (e.g. a leader watchlist explosion or a corrupted state
    # row that prevents CLOSE matching). On overflow we evict the OLDEST
    # OpenPosition by open_time and log a warning so ops can investigate.
    MAX_OPEN_POSITIONS_TRACKED: int = 10_000

    # ------------------------------------------------------------------ #
    # Graph Engine (CLAUDE.md § 9)                                        #
    # ------------------------------------------------------------------ #
    FOLLOWER_WINDOW_S: int = 300
    # Lowered from 5/0.7 → 3/0.6 for cold start. With 314 edges and only
    # 1 confirmed under the strict criterion, we'd never bootstrap the
    # follower graph. The looser thresholds will surface candidate edges
    # that the Hawkes batch can later validate or reject more rigorously.
    MIN_CO_OCCURRENCES: int = 3
    MIN_SAME_DIRECTION_RATE: float = 0.6
    HAWKES_LOOKBACK_DAYS: int = 30

    # ------------------------------------------------------------------ #
    # Profiler (CLAUDE.md § 9)                                            #
    # ------------------------------------------------------------------ #
    EWMA_LAMBDA: float = 0.94
    MIN_TRADES_FOR_PROFILE: int = 20
    # P2/P3 thresholds were calibrated for a high-frequency trader. Real
    # Polymarket leaders are swing traders — at 9h of observation, top
    # leaders had only 4 resolved positions (out of 136 trades observed).
    # Reaching the original 100 would take ~37 days; 500 takes 6 months.
    # Lowered cold-start floors so the cascade actually advances:
    #   P2 (Bayesian Ridge): 30 resolved (was 100)
    #   P3 (LightGBM):       150 resolved (was 500)
    # The adaptive scheduler in get_effective_thresholds() can re-tighten
    # these once the system has accumulated enough volume.
    MIN_RESOLVED_FOR_ERROR_P2: int = 30
    MIN_RESOLVED_FOR_ERROR_P3: int = 150

    # ------------------------------------------------------------------ #
    # Confidence Engine (CLAUDE.md § 9)                                   #
    # ------------------------------------------------------------------ #
    # Cold-start floors. Original (50/5/50/0.75) blocked all signals for
    # weeks. New values get the bot trading earlier with paper-only safety
    # (no real money at risk). Runtime adaptive multipliers in
    # get_effective_thresholds() will tighten these as data accumulates.
    FOLLOW_MIN_TRADES: int = 25
    FOLLOW_MIN_FOLLOWERS: int = 3
    FADE_MIN_RESOLVED: int = 25
    FADE_MIN_CONFIDENCE: float = 0.65
    THOMPSON_EXPLORATION_FLOOR: float = 0.15
    LIVE_DECISION_MAX_TRADE_AGE_S: int = 120

    # ------------------------------------------------------------------ #
    # Paper Trading + Risk (CLAUDE.md § 9)                                #
    # ------------------------------------------------------------------ #
    PAPER_TRADING: bool = True
    PAPER_CAPITAL_USDC: float = 10_000
    MAX_POSITION_PCT: float = 0.02  # Max 2% of capital per trade (Kelly hard cap)
    FADE_SIZE_RATIO: float = 0.50  # FADE position = 50% of equivalent FOLLOW
    MAX_MARKET_EXPOSURE_PCT: float = 0.25
    MIN_POSITION_USDC: float = 50.0
    PAPER_REENTRY_COOLDOWN_S: int = 300
    INVALID_LEARNING_CLOSE_WINDOW_S: int = 300
    # ── Mutable risk defaults (overridable at runtime via /api/risk/update) ──
    # The dashboard's Risk & Config cockpit edits the RuntimeConfig overrides
    # in Redis; on first boot the values below are used.
    KELLY_FRACTION: float = 0.50
    MAX_DRAWDOWN_STOP_PCT: float = 0.20
    MAX_CONCURRENT_POSITIONS: int = 10
    MAX_CONSECUTIVE_LOSSES: int = 5
    MAX_RECENT_LOSSES_PER_MARKET: int = 3

    # ------------------------------------------------------------------ #
    # Live Trading (S2.6) — Polymarket CLOB execution                    #
    # ------------------------------------------------------------------ #
    # Master safety flag. While True, LiveTrader still subscribes to
    # decisions and writes a `live_trades` row, but its `status` is
    # 'shadow' and NO order is sent to the CLOB. Flip to false on the
    # production VM ONLY after the docs/live-trading-setup checklist is
    # complete.
    LIVE_TRADING_DRY_RUN: bool = True
    # CLOB endpoint and chain.
    POLYMARKET_CLOB_URL: str = "https://clob.polymarket.com"
    POLYMARKET_CHAIN_ID: int = 137  # Polygon mainnet
    # Wallet — populated on the VM only. Empty string = "no wallet
    # configured" and forces dry-run regardless of LIVE_TRADING_DRY_RUN.
    POLYMARKET_PRIVATE_KEY: str = ""
    # Magic / proxy wallet that holds the USDC (CLOB orders are signed
    # by POLYMARKET_PRIVATE_KEY but funds settle from the funder).
    POLYMARKET_FUNDER_ADDRESS: str = ""
    # Limit order placement: BUY at mid + slippage_bps, SELL at mid - slippage_bps.
    # 50 bps = 0.5%. On Polymarket scale this is ~0.005 USDC per share.
    LIVE_SLIPPAGE_BPS: int = 50
    # If a limit order isn't filled within this many seconds, cancel and reprice.
    LIVE_ORDER_TIMEOUT_S: int = 30
    # Max number of cancel/reprice attempts per signal before giving up.
    LIVE_ORDER_MAX_RETRIES: int = 3
    # How often we poll the CLOB for fills on open orders.
    LIVE_FILL_POLL_INTERVAL_S: float = 2.0

    # ------------------------------------------------------------------ #
    # Decision Router (S2.7) — paper / live / dual routing                #
    # ------------------------------------------------------------------ #
    # Master mode at boot. Override at runtime by writing one of
    # {"paper","live","dual"} to the Redis key TRADING_MODE_OVERRIDE_KEY
    # — the router checks this on every routing decision so we never
    # require a redeploy to flip mode (e.g. via Telegram / API endpoint).
    # Valid values: "paper", "live", "dual".
    TRADING_MODE: str = "paper"
    # Redis key checked at every route() call. If set to a valid mode,
    # it overrides TRADING_MODE for that call; if missing/invalid, the
    # router falls back to TRADING_MODE.
    TRADING_MODE_OVERRIDE_KEY: str = "trading:mode_override"
    # Live-side filters applied on top of the upstream RiskManager. These
    # are ONLY for the live channel — paper is unfiltered. Rationale: we
    # may want paper to validate ALL signals (good benchmark) but only
    # the highest-confidence ones to spend real USDC.
    LIVE_FILTER_CONFIDENCE_MIN: float = 0.6
    LIVE_FILTER_SIZE_MIN_USDC: float = 10.0
    # Comma-separated list of market_ids that are allowed for live
    # trading. Empty string = no allowlist (any market).
    LIVE_MARKET_ALLOWLIST: str = ""

    # ------------------------------------------------------------------ #
    # Telegram Bot (S3.9) — push alerts + interactive commands            #
    # ------------------------------------------------------------------ #
    # Master flag. False = bot service does not start, no notifications,
    # no commands. Default off so a fresh checkout never tries to hit
    # Telegram with empty creds.
    TELEGRAM_ENABLED: bool = False
    # BotFather token. Obtain via @BotFather on Telegram. Empty = disabled
    # (we treat empty token as TELEGRAM_ENABLED=false regardless of flag).
    TELEGRAM_BOT_TOKEN: str = ""
    # Comma-separated allowlist of Telegram chat_ids that are authorized
    # to (a) receive alerts, (b) send commands. Anything else is ignored.
    # MUST be populated before enabling the bot — otherwise we'd accept
    # commands from any user who finds the bot.
    TELEGRAM_CHAT_IDS: str = ""
    # If true, the bot replies to commands from authorized chat_ids; if
    # false, the bot is "alerts-only" and silently ignores incoming
    # commands. Useful while debugging or for read-only deployments.
    TELEGRAM_COMMANDS_ENABLED: bool = True
    # Long-polling timeout (seconds) — Telegram Bot API getUpdates long
    # poll. Higher = fewer requests but slower shutdown response.
    TELEGRAM_POLL_TIMEOUT_S: int = 30
    # Rate-limit on outbound notifications: max per minute. Telegram caps
    # at ~30/sec for bots; we throttle far below that to be polite during
    # storms (e.g. flurry of TP/SL closes).
    TELEGRAM_MAX_NOTIFICATIONS_PER_MINUTE: int = 20

    # ------------------------------------------------------------------ #
    # Batch Processing (CLAUDE.md § 9)                                    #
    # ------------------------------------------------------------------ #
    BATCH_HOUR_UTC: int = 3
    BATCH_HAWKES_LEADERS: int = 200
    RETENTION_TRADES_DAYS: int = 90

    # ------------------------------------------------------------------ #
    # Scheduler + Watchdog (S3.10)                                        #
    # ------------------------------------------------------------------ #
    # APScheduler is now the single source of truth for periodic work.
    # Each interval below feeds a job; setting one to 0 disables that job.

    # How often the watchdog probes registered components. Cheap (Redis +
    # task.done() check), so 30s is comfortable.
    WATCHDOG_HEARTBEAT_INTERVAL_S: int = 30
    # If a component hasn't pinged its heartbeat in this many seconds we
    # consider it frozen even if its asyncio.Task hasn't crashed. Should
    # be > component's busy-loop sleep; default 2 minutes.
    WATCHDOG_HEARTBEAT_TIMEOUT_S: int = 120
    # Max consecutive restarts per component before we give up and
    # publish engine:crash + trip stop_event. Resets to 0 every time the
    # component runs cleanly for WATCHDOG_RESTART_RESET_S.
    WATCHDOG_MAX_RESTARTS: int = 3
    # Backoff between restart attempts (linear: i × backoff). Linear is
    # fine — we expect transient infra issues, not retry storms.
    WATCHDOG_RESTART_BACKOFF_S: int = 10
    # If a component runs uninterrupted for this long, its restart counter
    # is forgiven (we're stable again — don't punish a 4th flake on day 12).
    WATCHDOG_RESTART_RESET_S: int = 600

    # Hourly refresh of the Gamma API top-markets list. Tokens are written
    # to a Redis set the observer subscribes to. Set to 0 to disable.
    REFRESH_MARKETS_INTERVAL_S: int = 3600
    # Periodic killswitch state refresh — bypasses the 2s Redis TTL cache
    # so manual DB edits propagate. Set to 0 to disable.
    KILLSWITCH_SYNC_INTERVAL_S: int = 300
    # Hour at which the daily Redis cleanup runs. Default 04:00 UTC, one
    # hour after the nightly batch so Hawkes refit etc. is finished first.
    REDIS_CLEANUP_HOUR_UTC: int = 4

    # ------------------------------------------------------------------ #
    # Backups → Cloudflare R2 (S4.12)                                     #
    # ------------------------------------------------------------------ #
    # Master switch. False = the backups container starts up but
    # logs "disabled" and idles. Used in dev (no R2 creds available).
    BACKUPS_ENABLED: bool = False
    # Cron hour in UTC. Default 05:00 — runs after nightly_batch (03:00)
    # and redis_cleanup (04:00) so the dump captures the post-batch
    # state.
    BACKUP_HOUR_UTC: int = 5
    # R2 connection. R2 is S3-compatible — the endpoint URL has the
    # form `https://<account_id>.r2.cloudflarestorage.com`.
    R2_ENDPOINT_URL: str = ""
    R2_ACCESS_KEY_ID: str = ""
    R2_SECRET_ACCESS_KEY: str = ""
    R2_BUCKET: str = "polymarket-backups"
    # Object-key prefix inside the bucket. Allows a single bucket to
    # host multiple environments (postgres/, postgres-staging/, ...).
    R2_KEY_PREFIX: str = "postgres/"
    # GFS retention bounds. 7 daily + 4 weekly + 3 monthly ≈ 14 objects.
    BACKUP_RETENTION_DAILY: int = 7
    BACKUP_RETENTION_WEEKLY: int = 4
    BACKUP_RETENTION_MONTHLY: int = 3
    # Day of week (0=Mon, 6=Sun) treated as the weekly anchor. Sunday
    # by default — most weekly snapshots in industry land here.
    BACKUP_WEEKLY_DOW: int = 6
    # pg_dump invocation — give it 30 min ceiling. A 50 MB DB dumps in
    # <30s; 30 min protects against runaway processes on a hosed VM.
    BACKUP_PG_DUMP_TIMEOUT_S: int = 1800
    # Local scratch directory for the dump file before upload.
    BACKUP_LOCAL_SCRATCH_DIR: str = "/tmp"

    # ------------------------------------------------------------------ #
    # Logging                                                             #
    # ------------------------------------------------------------------ #
    # Minimum severity emitted by loguru. Override per-environment via env:
    #   LOG_LEVEL=DEBUG   → noisy, dev only
    #   LOG_LEVEL=INFO    → default, production-safe
    #   LOG_LEVEL=WARNING → quiet (Oracle Free 24GB VM with limited disk)
    LOG_LEVEL: str = "INFO"
    # Optional path to a rotating file sink. If empty, logs go to stderr only.
    # On the Oracle Cloud VM we'll set this to e.g. /var/log/polymarket-bot/app.log
    LOG_FILE: str = ""
    # Loguru rotation spec — "daily", "100 MB", "1 week", etc. See loguru docs.
    LOG_FILE_ROTATION: str = "daily"
    # How many rotated files to keep before deleting the oldest.
    LOG_FILE_RETENTION: str = "14 days"

    # ------------------------------------------------------------------ #
    # Validators (Phase 1 Task F)                                         #
    # ------------------------------------------------------------------ #
    @field_validator("FALCON_MAX_CONCURRENCY")
    @classmethod
    def _validate_falcon_concurrency(cls, v: int) -> int:
        if not 1 <= v <= 32:
            raise ValueError(
                f"FALCON_MAX_CONCURRENCY must be in [1, 32], got {v}. "
                "The 60 RPM rate limiter is the real cap; values above 32 "
                "just queue under the limiter."
            )
        return v

    @field_validator("REGISTRY_BACKFILL_CONCURRENCY")
    @classmethod
    def _validate_backfill_concurrency(cls, v: int) -> int:
        if not 1 <= v <= 64:
            raise ValueError(
                f"REGISTRY_BACKFILL_CONCURRENCY must be in [1, 64], got {v}. "
                "Higher than 64 doesn't help — the Falcon RPM limiter is the bound."
            )
        return v

    @field_validator("FALCON_RPM_BUCKET_CAPACITY")
    @classmethod
    def _validate_falcon_bucket_capacity(cls, v: int) -> int:
        # Phase 3 Task B: documented Falcon contract is 60 RPM per key. We
        # don't crash if the operator raises this — they may have a private
        # contract — but we warn (the FalconClient logs at construction).
        # Hard lower bound: 1 (otherwise the client deadlocks immediately).
        # Hard upper bound: 10_000 (sanity ceiling, catches typos).
        if v < 1 or v > 10_000:
            raise ValueError(
                f"FALCON_RPM_BUCKET_CAPACITY must be in [1, 10000], got {v}."
            )
        return v

    @field_validator("FALCON_RPM_REFILL_PER_SEC")
    @classmethod
    def _validate_falcon_refill(cls, v: float) -> float:
        if v <= 0.0 or v > 100.0:
            raise ValueError(
                f"FALCON_RPM_REFILL_PER_SEC must be in (0, 100], got {v}."
            )
        return v

    @field_validator("FALCON_BACKOFF_S")
    @classmethod
    def _validate_falcon_backoff(cls, v: int) -> int:
        if v < 1 or v > 3600:
            raise ValueError(f"FALCON_BACKOFF_S must be in [1, 3600], got {v}.")
        return v

    @field_validator("FALCON_COALESCE_TTL_S")
    @classmethod
    def _validate_falcon_coalesce_ttl(cls, v: float) -> float:
        if v < 0.0 or v > 600.0:
            raise ValueError(
                f"FALCON_COALESCE_TTL_S must be in [0, 600], got {v}."
            )
        return v

    @field_validator("FALCON_CONDITIONAL_REVALIDATE_S")
    @classmethod
    def _validate_falcon_revalidate(cls, v: int) -> int:
        if v < 0 or v > 172800:
            raise ValueError(
                f"FALCON_CONDITIONAL_REVALIDATE_S must be in [0, 172800], got {v}."
            )
        return v

    @field_validator("TRADE_OBSERVER_POLL_INTERVAL_S")
    @classmethod
    def _validate_observer_poll_interval(cls, v: int, info) -> int:
        # Phase 1 Task O / HP-1 fix #1. Bounds are themselves env-overridable
        # for ops debugging (e.g. tighten MIN to 1 in a load-test env), but
        # the runtime value must fall inside the configured window. Reads
        # MIN/MAX from `info.data` so test envs that override either bound
        # are honoured; falls back to the class defaults (1, 60) otherwise.
        lo = int(info.data.get("TRADE_OBSERVER_POLL_INTERVAL_S_MIN", 1))
        hi = int(info.data.get("TRADE_OBSERVER_POLL_INTERVAL_S_MAX", 60))
        if not lo <= v <= hi:
            raise ValueError(
                f"TRADE_OBSERVER_POLL_INTERVAL_S must be in [{lo}, {hi}], got {v}. "
                "Below MIN risks rate-limit bans; above MAX defeats the HP-1 "
                "freshness goal."
            )
        return v

    # ------------------------------------------------------------------ #
    # Round 6 (The Spine) — Multi-RPC abstraction (src/rpc/)              #
    # ------------------------------------------------------------------ #
    # Comma-separated ordered list of provider names, lowest priority
    # first. Wave 2 reads each name as a section key for the matching
    # *_URL / *_API_KEY env var (e.g. provider 'alchemy' → ALCHEMY_RPC_URL,
    # ALCHEMY_RPC_API_KEY). The local Erigon entry has no API key and
    # uses an effectively-infinite token bucket. See ROUND_6_THE_SPINE.md
    # § 3.2.
    RPC_PROVIDER_PRIORITIES: str = "local_erigon,alchemy,quicknode"
    # Per-provider connection URLs. Empty = "provider not configured";
    # the pool simply skips empty entries at startup.
    LOCAL_ERIGON_RPC_URL: str = "http://10.0.0.2:8545"  # private network IP
    LOCAL_ERIGON_WS_URL: str = "ws://10.0.0.2:8546"
    ALCHEMY_RPC_URL: str = ""
    ALCHEMY_RPC_API_KEY: str = ""
    QUICKNODE_RPC_URL: str = ""
    QUICKNODE_RPC_API_KEY: str = ""
    # Circuit-breaker tuning. 5 consecutive failures trip the breaker;
    # 60 s cooldown before the HALF_OPEN probe. See
    # src/rpc/circuit_breaker.py.
    RPC_CIRCUIT_BREAKER_THRESHOLD: int = 5
    RPC_CIRCUIT_BREAKER_COOLDOWN_S: float = 60.0
    # How often the ProviderPool's background health-check loop probes
    # each provider with a cheap eth_blockNumber call and inserts a
    # row into rpc_health_history (migration 023).
    RPC_HEALTHCHECK_INTERVAL_S: int = 60

    # ------------------------------------------------------------------ #
    # Round 6 — On-chain CLOB listener (src/onchain/)                     #
    # ------------------------------------------------------------------ #
    # Polymarket CTF Exchange contract on Polygon mainnet. The official
    # address from Polymarket's docs — pin here so a misconfigured env
    # can't silently point the listener at the wrong contract.
    # Wave 2 verifies this against the Etherscan-verified contract.
    POLYMARKET_CLOB_CONTRACT_ADDRESS: str = (
        "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E"
    )
    # Alert threshold for the polybot_chain_blocks_behind gauge. Polygon
    # produces a block every ~2s, so 30 blocks = ~1 min behind. Default
    # to 30 — the operator gets pinged before the lag crosses the
    # § 3.1 "node must be at chain-head within 60s" invariant.
    CHAIN_HEAD_LAG_ALERT_BLOCKS: int = 30
    # How far back the listener subscribes from on first boot when
    # chain_sync_state is empty (or on a "reset cursor" recovery).
    # 256 blocks ≈ 8 min of history, enough to bridge a typical
    # rollout window without re-decoding hours of unrelated traffic.
    CHAIN_BOOTSTRAP_LOOKBACK_BLOCKS: int = 256
    # Batch-commit cadence: persist the chain_sync_state cursor after
    # this many blocks OR this many seconds, whichever first. Keeps the
    # cursor close enough to head that a crash never replays more than
    # ~5s of work, while still batching DB writes.
    CHAIN_BATCH_COMMIT_BLOCKS: int = 50
    CHAIN_BATCH_COMMIT_INTERVAL_S: float = 5.0

    # ------------------------------------------------------------------ #
    # Round 6 — Wallet Universe Crawler (src/crawler/)                    #
    # ------------------------------------------------------------------ #
    # Tier-decision volume thresholds. Wallets with 30d USDC volume at
    # or above the FULL threshold sit in tier 0 (full Falcon refresh
    # daily); at or above PERIODIC sit in tier 1 (weekly Falcon);
    # everyone else in tier 2 (on-chain only). See
    # src/crawler/depth_tiers.py::expected_tier.
    WALLET_UNIVERSE_FULL_TIER_VOLUME_THRESHOLD_USDC: float = 1_000_000.0
    WALLET_UNIVERSE_PERIODIC_TIER_VOLUME_THRESHOLD_USDC: float = 50_000.0
    # How often the AdaptiveDepth nightly review runs. Default once
    # daily (86400 s); set lower in test envs.
    WALLET_UNIVERSE_REVIEW_INTERVAL_S: int = 86_400

    # ------------------------------------------------------------------ #
    # Round 6 — Cold storage (src/cold_storage/)                          #
    # ------------------------------------------------------------------ #
    # Local-disk root for the Parquet tree. On production this is the
    # mount of the Hetzner volume (or a Storage Box bind-mount).
    COLD_EXPORT_BASE_PATH: str = "/data/cold"
    # Comma-separated list of tables to export nightly. Order is the
    # iteration order in ColdExporter.run_nightly. Default mirrors
    # ROUND_6_THE_SPINE.md § 3.6.
    COLD_EXPORT_TABLES: str = (
        "trades_observed,"
        "book_quality_snapshots,"
        "orderbook_features_minute,"
        "decision_log,"
        "positions_reconstructed"
    )
    # Retention for Parquet files. Default 0 = "keep everything";
    # cold storage is cheap and the goal of this tier is research
    # depth, not retention pressure.
    COLD_RETENTION_DAYS: int = 0

    # ------------------------------------------------------------------ #
    # Round 6 — Coverage reconciler (src/monitoring/)                     #
    # ------------------------------------------------------------------ #
    # Width of each reconciliation window in seconds. The reconciler
    # wakes every WINDOW_S and compares the previous WINDOW_S of trades
    # across every source. 300 s = 5 min matches the spec.
    COVERAGE_RECONCILER_WINDOW_S: int = 300
    # Minimum acceptable coverage_ratio{source} before the
    # TradeIngestionCoverageLow alert fires. 0.95 = "REST/WS must see
    # at least 95% of the trades on-chain ingestion catches".
    COVERAGE_ALERT_THRESHOLD: float = 0.95

    @field_validator("RPC_CIRCUIT_BREAKER_THRESHOLD")
    @classmethod
    def _validate_rpc_breaker_threshold(cls, v: int) -> int:
        # Round 6 § 3.2: 5 consecutive failures is the documented contract.
        # Hard bounds keep a misconfigured env from disabling the breaker
        # (v=0) or hiding real-world flakiness (v=10000).
        if not 1 <= v <= 100:
            raise ValueError(
                f"RPC_CIRCUIT_BREAKER_THRESHOLD must be in [1, 100], got {v}."
            )
        return v

    @field_validator("RPC_CIRCUIT_BREAKER_COOLDOWN_S")
    @classmethod
    def _validate_rpc_breaker_cooldown(cls, v: float) -> float:
        if not 1.0 <= v <= 3600.0:
            raise ValueError(
                f"RPC_CIRCUIT_BREAKER_COOLDOWN_S must be in [1, 3600], got {v}."
            )
        return v

    @field_validator("COVERAGE_ALERT_THRESHOLD")
    @classmethod
    def _validate_coverage_alert_threshold(cls, v: float) -> float:
        # Coverage is a ratio in [0, 1]; an operator setting 1.0 disables
        # the alert (everything is "low coverage"), and a negative would
        # be silently always-firing.
        if not 0.0 <= v <= 1.0:
            raise ValueError(
                f"COVERAGE_ALERT_THRESHOLD must be in [0, 1], got {v}."
            )
        return v

    # ------------------------------------------------------------------ #
    # Round 7 (The Front Door) — Mempool watcher + pre-signed order pool  #
    # ------------------------------------------------------------------ #
    # See docs/ROUND_7_MEMPOOL_AND_PREFILL.md § 3.5-3.6 for the spec.
    #
    # POLYMARKET_CLOB_CONTRACT_ADDRESS is REUSED from the R6 onchain
    # block above — DO NOT redeclare it here. The mempool tx decoder
    # uses the same address as the on-chain log decoder.

    # Size bucket schedule for PreSignedPool. The pool pre-signs one
    # order per (market × token × direction × bucket) combination; on
    # fire we pick the largest bucket <= the leader's intended size.
    # 4 buckets is the architect's pick — adding more buckets squares
    # the pool size (~3200 → ~6400 at 6 buckets) for diminishing
    # alpha-capture gains.
    PREFILL_POOL_SIZE_BUCKETS_USDC: list[int] = [500, 2000, 10_000, 50_000]
    # How long a single pre-signed order stays valid before the
    # rotation task drops it. 5 min is the architect's pick: matches
    # the natural rotation cadence and gives the pool a 10× safety
    # margin vs PREFILL_ROTATION_INTERVAL_S below.
    PREFILL_ORDER_VALIDITY_S: int = 300
    # How often the rotation background task scans for expired sigs
    # AND triggers re-signs to keep the pool warm. 30 s × 10 cycles
    # = 5 min — exactly one full validity window per re-sign cadence.
    PREFILL_ROTATION_INTERVAL_S: int = 30
    # How many markets the pool warms at boot. Top-100 by 24h volume
    # captures >95% of leader signal; below 100 we'd start missing
    # mid-tier markets where some leaders specialise.
    PREFILL_TOP_MARKETS: int = 100
    # Headline latency budget for the IntentRouter (intent_received →
    # fire_complete). R7 § 6 acceptance gate is p50 < 250 ms. We
    # surface this as a config knob so the dashboard alert can read
    # the same number the code targets.
    MEMPOOL_INTENT_LATENCY_BUDGET_MS: int = 250
    # How often the WatchedWalletIndex rebuild loop polls
    # wallet_universe. Matches the R6 AdaptiveDepth review cadence
    # so tier transitions propagate to the bloom within 5 min.
    WATCHED_WALLET_INDEX_REFRESH_S: int = 300
    # Master shadow-mode switch. Wave-2 plumbs this through
    # RuntimeConfig.get_value('prefill_live_enabled', default=False)
    # so the operator can flip it at runtime via /api/risk/update.
    # The static default here is the safe "shadow mode" — fire only
    # paper trades on detected intents until the 30-day soak proves
    # the strategy works on paper.
    PREFILL_LIVE_ENABLED: bool = False

    @field_validator("PREFILL_POOL_SIZE_BUCKETS_USDC")
    @classmethod
    def _validate_prefill_buckets(cls, v: list[int]) -> list[int]:
        # Architect contract: ascending, positive, at most 8 buckets.
        # Empty list disables prefill entirely (operator can flip to []
        # to short-circuit the path without redeploying).
        if not isinstance(v, list):
            raise ValueError(
                f"PREFILL_POOL_SIZE_BUCKETS_USDC must be a list, got {type(v)}"
            )
        if len(v) > 8:
            raise ValueError(
                f"PREFILL_POOL_SIZE_BUCKETS_USDC: at most 8 buckets, got {len(v)}"
            )
        if v != sorted(v):
            raise ValueError(
                f"PREFILL_POOL_SIZE_BUCKETS_USDC must be ascending, got {v}"
            )
        if any(b <= 0 for b in v):
            raise ValueError(
                f"PREFILL_POOL_SIZE_BUCKETS_USDC must be all positive, got {v}"
            )
        return v

    @field_validator("PREFILL_ORDER_VALIDITY_S")
    @classmethod
    def _validate_prefill_validity(cls, v: int) -> int:
        # Architect bounds: validity must comfortably exceed rotation
        # interval (safety margin) and not balloon past 1 h (stale
        # signatures are a liability). 60 s lower bound matches the
        # smallest reasonable rotation cadence.
        if not 60 <= v <= 3600:
            raise ValueError(
                f"PREFILL_ORDER_VALIDITY_S must be in [60, 3600], got {v}."
            )
        return v

    @field_validator("PREFILL_ROTATION_INTERVAL_S")
    @classmethod
    def _validate_prefill_rotation(cls, v: int) -> int:
        # Bounds keep the rotation reasonable. >300 s would lose the
        # safety-margin property; <5 s would hammer the signing path.
        if not 5 <= v <= 300:
            raise ValueError(
                f"PREFILL_ROTATION_INTERVAL_S must be in [5, 300], got {v}."
            )
        return v

    @field_validator("PREFILL_TOP_MARKETS")
    @classmethod
    def _validate_prefill_top_markets(cls, v: int) -> int:
        if not 0 <= v <= 1000:
            raise ValueError(
                f"PREFILL_TOP_MARKETS must be in [0, 1000], got {v}. "
                "0 disables warm; 1000 is far past the realistic top tier."
            )
        return v

    @field_validator("MEMPOOL_INTENT_LATENCY_BUDGET_MS")
    @classmethod
    def _validate_mempool_latency_budget(cls, v: int) -> int:
        # Architect bounds: < 50 ms is unachievable on a Polygon-public
        # mempool. > 5 s means we've lost the alpha (followers see
        # confirmation in 5-10 s under the R3 5s poll cadence). The
        # acceptance gate from R7 § 6 is 250 ms p50.
        if not 50 <= v <= 5000:
            raise ValueError(
                f"MEMPOOL_INTENT_LATENCY_BUDGET_MS must be in [50, 5000], got {v}."
            )
        return v

    @field_validator("WATCHED_WALLET_INDEX_REFRESH_S")
    @classmethod
    def _validate_watched_wallet_refresh(cls, v: int) -> int:
        # Below 30 s the rebuild becomes the dominant SQL load on
        # wallet_universe. Above 1h tier transitions take too long to
        # propagate to the bloom.
        if not 30 <= v <= 3600:
            raise ValueError(
                f"WATCHED_WALLET_INDEX_REFRESH_S must be in [30, 3600], got {v}."
            )
        return v

    # ───── Round 8 (The Lens) — strategy classifier ────────────────────
    # See docs/ROUND_8_STRATEGY_CLASSIFIER.md §§ 3.5 + 7.D for the spec.
    #
    # STRATEGY_CLASSIFIER_REFRESH_INTERVAL_H: how often the daemon
    # re-classifies tier-0/1 wallets. Spec § 3.5 fires daily; 24 h
    # matches that and keeps the cost bounded (~2k wallets × 1 s/wallet
    # = ~30 min wall time per pass, well within budget).
    STRATEGY_CLASSIFIER_REFRESH_INTERVAL_H: int = 24
    # STRATEGY_DRIFT_JS_THRESHOLD: Jensen-Shannon divergence at which we
    # flag a wallet for strategy-shift. Spec § 3.5 default is 0.3 (so
    # that a true class flip — directional → market_maker — fires
    # reliably but minor noise in strategy_probs does not).
    STRATEGY_DRIFT_JS_THRESHOLD: float = 0.3
    # STRATEGY_DRIFT_MIN_BASELINE_SAMPLES: cold-start guard. The drift
    # detector silently no-ops until the wallet has at least this many
    # rows in leader_strategy_history. 5 samples ~= 5 days of daemon
    # operation, enough to stabilise the baseline mean.
    STRATEGY_DRIFT_MIN_BASELINE_SAMPLES: int = 5
    # Where the trained classifier pickle lives on disk. The daemon
    # falls back to a uniform-prior dummy when this path is missing.
    STRATEGY_CLASSIFIER_MODEL_PATH: str = "models/strategy_classifier.pkl"
    # Unsupervised explorer thresholds — see § 3.4. Clusters with size
    # >= MIN AND mean-supervised-confidence <= MAX surface as candidate
    # new classes.
    STRATEGY_CLUSTER_MIN_SIZE: int = 20
    STRATEGY_CLUSTER_MAX_SUPERVISED_CONFIDENCE: float = 0.5

    @field_validator("STRATEGY_CLASSIFIER_REFRESH_INTERVAL_H")
    @classmethod
    def _validate_strategy_refresh_interval(cls, v: int) -> int:
        # < 1 h is wasteful (no new data accumulates that fast). > 168 h
        # (one week) means drift detection lags too much to be useful.
        if not 1 <= v <= 168:
            raise ValueError(
                f"STRATEGY_CLASSIFIER_REFRESH_INTERVAL_H must be in [1, 168], got {v}."
            )
        return v

    @field_validator("STRATEGY_DRIFT_JS_THRESHOLD")
    @classmethod
    def _validate_strategy_drift_threshold(cls, v: float) -> float:
        # JS divergence is bounded in [0, 1] under log_2. Operator-tunable
        # but values < 0.05 fire on noise; > 0.7 effectively disables drift.
        if not 0.05 <= v <= 0.7:
            raise ValueError(
                f"STRATEGY_DRIFT_JS_THRESHOLD must be in [0.05, 0.7], got {v}."
            )
        return v

    # ───── Round 9 (The Web) — Multivariate Hawkes + Kalman ────────────
    # See docs/ROUND_9_MULTIVARIATE_HAWKES.md for the full spec.
    #
    # MVHAWKES_LOOKBACK_DAYS: trailing-window the multivariate fitter
    # consumes. 30 days matches the R5 bivariate window so the two
    # fitters operate on the same data slice.
    MVHAWKES_LOOKBACK_DAYS: int = 30
    # Initial β seed for the multivariate fitter (sec^-1). 0.01 ≈ 100 s
    # half-life — slower than R5 (5-min half-life). Population-level
    # excitation is longer-lasting because many followers stretch the
    # tail; the optimiser is free to walk away from this seed.
    MVHAWKES_BETA_INITIAL: float = 0.01
    # BIC k_penalty for the multivariate fitter. Spec § 2.3 sets it to
    # the number of free α entries; on the default block-sparse mask
    # with K=4 pools, k_penalty = 2K = 8. Operator may override for
    # research.
    MVHAWKES_BIC_K_PENALTY: int = 8
    # How often the daemon refits all leaders. 86400 s = once per day;
    # the systemd-launched daemon does an immediate first pass then
    # sleeps on this interval.
    MVHAWKES_REFRESH_INTERVAL_S: int = 86400
    # KALMAN_OBSERVATION_WINDOW_S: the time window we observe to compute
    # the actual follower-volume burst that y_observed measures. 1800 s
    # = 30 min, per spec § 3.2.
    KALMAN_OBSERVATION_WINDOW_S: int = 1800
    # VOLUME_ANTICIPATION_THRESHOLD_USDC: minimum predicted
    # follower-pool volume for the volume_anticipation entry policy to
    # fire. 5000 USDC is a conservative gate — the goal is to enter on
    # forecast trades, not on every leader signal.
    VOLUME_ANTICIPATION_THRESHOLD_USDC: float = 5000.0
    # Cron hour:minute (UTC) for the nightly multivariate Hawkes refit
    # job. 03:30 sits after the R5 bivariate window (03:00) so the two
    # fitters don't fight for DB / CPU.
    MVHAWKES_BATCH_HOUR_UTC: int = 3
    MVHAWKES_BATCH_MINUTE_UTC: int = 30

    @field_validator("MVHAWKES_LOOKBACK_DAYS")
    @classmethod
    def _validate_mvhawkes_lookback(cls, v: int) -> int:
        if not 7 <= v <= 365:
            raise ValueError(
                f"MVHAWKES_LOOKBACK_DAYS must be in [7, 365], got {v}."
            )
        return v

    @field_validator("MVHAWKES_BIC_K_PENALTY")
    @classmethod
    def _validate_mvhawkes_k_penalty(cls, v: int) -> int:
        if not 1 <= v <= 100:
            raise ValueError(
                f"MVHAWKES_BIC_K_PENALTY must be in [1, 100], got {v}."
            )
        return v

    # ───── Round 10 (The Truth Test) — Causal inference layer ──────────
    # See docs/ROUND_10_CAUSAL_INFERENCE.md for the full spec.
    #
    # CAUSAL_2SLS_BOOTSTRAP_N: number of resamples for the bootstrap CI
    # on the 2SLS ATE coefficient. 1000 is the spec § 3.2 default;
    # test fixtures drop to 100 for speed. Below 100 the percentile CI
    # is essentially noise; above 5000 returns diminish (per
    # Efron-Tibshirani's bootstrap CI saturation rule of thumb).
    CAUSAL_2SLS_BOOTSTRAP_N: int = 1000
    # CAUSAL_WU_HAUSMAN_THRESHOLD: p-value below which the gate
    # considers the IV correction to be doing "real work". Per spec
    # § 6 the acceptance criterion is p < 0.05 for >= 70% of pairs.
    CAUSAL_WU_HAUSMAN_THRESHOLD: float = 0.05
    # CAUSAL_FIRST_STAGE_F_MIN: weak-instrument floor on the first-stage
    # F-statistic. Below this the IVEstimate.convergence is flagged
    # 'weak_instruments' and the gate treats the estimate as missing.
    # Staiger-Stock (1997) -> 10.
    CAUSAL_FIRST_STAGE_F_MIN: float = 10.0
    # CAUSAL_GATE_FOLLOW_PENALTY: multiplier applied to follow_confidence
    # when the causal gate triggers (no causal evidence / IV correction
    # disagrees with Hawkes). 0.5 = halve the confidence — the spec
    # § 3.5 example value.
    CAUSAL_GATE_FOLLOW_PENALTY: float = 0.5
    # CAUSAL_DAEMON_BATCH_HOUR_UTC: hour-of-day to run the nightly
    # 2SLS pass. 04:00 UTC sits AFTER R9's 03:30 batch so the daemon
    # can read the freshly-written multivariate_hawkes_fits rows for
    # the side-by-side comparison.
    CAUSAL_DAEMON_BATCH_HOUR_UTC: int = 4
    # CAUSAL_BIN_SECONDS: bin width for the (L, F, Z) intensity
    # histograms in the daemon's matrix construction. 300 s = 5 min
    # matches the FOLLOWER_WINDOW_S used elsewhere in the bot — keeps
    # the IV time-grid commensurate with the Hawkes excitation scale.
    CAUSAL_BIN_SECONDS: int = 300

    @field_validator("CAUSAL_2SLS_BOOTSTRAP_N")
    @classmethod
    def _validate_causal_bootstrap(cls, v: int) -> int:
        if not 10 <= v <= 10_000:
            raise ValueError(
                f"CAUSAL_2SLS_BOOTSTRAP_N must be in [10, 10000], got {v}."
            )
        return v

    @field_validator("CAUSAL_WU_HAUSMAN_THRESHOLD")
    @classmethod
    def _validate_causal_wh_threshold(cls, v: float) -> float:
        if not 0.0 < v < 1.0:
            raise ValueError(
                f"CAUSAL_WU_HAUSMAN_THRESHOLD must be in (0, 1), got {v}."
            )
        return v

    @field_validator("CAUSAL_FIRST_STAGE_F_MIN")
    @classmethod
    def _validate_causal_f_min(cls, v: float) -> float:
        if not 1.0 <= v <= 1000.0:
            raise ValueError(
                f"CAUSAL_FIRST_STAGE_F_MIN must be in [1, 1000], got {v}."
            )
        return v

    @field_validator("CAUSAL_GATE_FOLLOW_PENALTY")
    @classmethod
    def _validate_causal_follow_penalty(cls, v: float) -> float:
        if not 0.0 <= v <= 1.0:
            raise ValueError(
                f"CAUSAL_GATE_FOLLOW_PENALTY must be in [0, 1], got {v}."
            )
        return v

    # ───── Round 11 (The Microscope) — CLOB Book L3 + Microstructure ──
    # See docs/ROUND_11_CLOB_BOOK_MICROSTRUCTURE.md for the spec.
    #
    # The L3 book observer subscribes to Polymarket's full order-event
    # firehose for the top-N markets. R11 § 2.3 sizes the system at
    # ~5,000 events/sec peak across the top-100 — the daemon's whole
    # design is sized to that load.

    # CLOB_BOOK_TOP_MARKETS: how many markets the L3 subscriber tracks.
    # 100 per § 2.3 — captures the volume that matters; below that we'd
    # miss mid-tier markets where iceberg/spoof patterns also appear.
    CLOB_BOOK_TOP_MARKETS: int = 100
    # CLOB_BOOK_QUEUE_MAXSIZE: bounded asyncio.Queue between the WS
    # reader and the DB writer. 50,000 events × ~150 bytes ≈ 7.5 MB
    # of in-flight memory — comfortably under the 500 MB envelope.
    # Under overload the OLDEST event is dropped (spec § 3.1) so the
    # WS reader never blocks.
    CLOB_BOOK_QUEUE_MAXSIZE: int = 50_000
    # CLOB_BOOK_PARTITION_GRANULARITY: 'hour' — see migration 032 § 2.3.
    # Daily partitions would put each one at ~13 GB and the DROP pass
    # would be load-bearing on busy days.
    CLOB_BOOK_PARTITION_GRANULARITY: str = "hour"
    # CLOB_BOOK_RETENTION_DAYS: how long the hot tier keeps L3 events.
    # 30 days × 13 GB/day = ~390 GB — fits on a Hetzner 500 GB volume.
    # Cold-tier Parquet export (R6 § 3.6) carries the long tail.
    CLOB_BOOK_RETENTION_DAYS: int = 30
    # CLOB_BOOK_DB_BATCH_SIZE: rows flushed per executemany. Mirrors the
    # R3 trade_observer pattern; tuned to keep DB latency below the WS
    # consumer rate.
    CLOB_BOOK_DB_BATCH_SIZE: int = 500
    # CLOB_BOOK_DB_BATCH_INTERVAL_S: how long the writer waits to fill a
    # batch before flushing what it has. Trades freshness for write
    # efficiency; 0.5 s matches the per-minute rollup cadence so the
    # rollup never sees a write more than ~0.5 s stale.
    CLOB_BOOK_DB_BATCH_INTERVAL_S: float = 0.5
    # CLOB_BOOK_STREAM_NAME: Redis Stream the observer publishes to.
    # Consumed by src.microstructure.daemon for real-time derivation.
    CLOB_BOOK_STREAM_NAME: str = "book:events:stream"
    # CLOB_BOOK_STREAM_MAXLEN: bound the stream length so a consumer
    # outage doesn't OOM Redis. ~5 minutes of peak events: 5000/s × 300s
    # = 1.5M entries. We use approximate trim (`~`) for speed.
    CLOB_BOOK_STREAM_MAXLEN: int = 1_500_000

    # Microstructure deriver — see § 3.2 of the spec.
    MICROSTRUCTURE_ROLLUP_BUCKET_S: int = 60
    # Iceberg detector window — § 3.2.A. 60 s rolling per (wallet, price).
    MICROSTRUCTURE_ICEBERG_WINDOW_S: int = 60
    # Minimum number of same-(wallet, price) placements in the window to
    # flag an iceberg. 3 = "two refills after the first". Below 3 the
    # false-positive rate from natural latency-induced retries dominates.
    MICROSTRUCTURE_ICEBERG_MIN_REFILLS: int = 3
    # Spoof detector — § 3.2.B. An order placed-then-cancelled within
    # this window with zero fill is a spoof candidate. 5 s matches the
    # spec.
    MICROSTRUCTURE_SPOOF_CANCEL_LIMIT_S: int = 5
    # Spoof size percentile gate — § 3.2.B says "large order (>= 95th
    # pct)". The deriver tracks a rolling size distribution per market+
    # token and applies this percentile gate.
    MICROSTRUCTURE_SPOOF_SIZE_PERCENTILE: float = 0.95
    # OFI rolling window — § 3.2.C.
    MICROSTRUCTURE_OFI_WINDOW_S: int = 5
    # Cancel-to-fill ratio window — § 3.2.E. The on-line tracker uses 30
    # minutes (high-cardinality enough that anything longer would blow
    # memory; shorter would miss the market-maker pattern).
    MICROSTRUCTURE_CANCEL_TO_FILL_WINDOW_S: int = 1800
    # Wallet signature batch lookback — § 3.2 nightly rollup. 30 days
    # matches the cold-tier retention so we never read past the boundary.
    MICROSTRUCTURE_SIGNATURE_LOOKBACK_DAYS: int = 30
    # Minimum n_orders_30d for a wallet's signature to be considered
    # "trustworthy" by readers (e.g. R8 features). Below this the
    # readers fall back to None — too thin to inform the classifier.
    MICROSTRUCTURE_SIGNATURE_MIN_ORDERS: int = 50

    @field_validator("CLOB_BOOK_QUEUE_MAXSIZE")
    @classmethod
    def _validate_clob_book_queue_maxsize(cls, v: int) -> int:
        # Below 1k we lose all backpressure value (every burst drops);
        # above 1M we'd blow the daemon's 500 MB envelope on a backlog.
        if not 1_000 <= v <= 1_000_000:
            raise ValueError(
                f"CLOB_BOOK_QUEUE_MAXSIZE must be in [1000, 1_000_000], got {v}."
            )
        return v

    @field_validator("CLOB_BOOK_RETENTION_DAYS")
    @classmethod
    def _validate_clob_book_retention_days(cls, v: int) -> int:
        # 1 day floor — anything less and the rollup batch can't read
        # its own input. 365 day ceiling — at 13 GB/day past 30 days
        # this stops fitting on any sensible volume.
        if not 1 <= v <= 365:
            raise ValueError(
                f"CLOB_BOOK_RETENTION_DAYS must be in [1, 365], got {v}."
            )
        return v

    @field_validator("MICROSTRUCTURE_ROLLUP_BUCKET_S")
    @classmethod
    def _validate_microstructure_bucket(cls, v: int) -> int:
        # Per-second buckets would explode microstructure_features
        # cardinality (~84M rows/day across the top-100). Per-hour
        # would lose all granularity. 1 min is the architect's pick;
        # 5 s ≤ v ≤ 300 s is the operator-tunable window.
        if not 5 <= v <= 300:
            raise ValueError(
                f"MICROSTRUCTURE_ROLLUP_BUCKET_S must be in [5, 300], got {v}."
            )
        return v

    @field_validator("MICROSTRUCTURE_SPOOF_SIZE_PERCENTILE")
    @classmethod
    def _validate_microstructure_spoof_percentile(cls, v: float) -> float:
        if not 0.5 <= v <= 0.999:
            raise ValueError(
                f"MICROSTRUCTURE_SPOOF_SIZE_PERCENTILE must be in [0.5, 0.999], got {v}."
            )
        return v


settings = Settings()


# ============================================================================
# Adaptive thresholds — system-maturity-aware
# ============================================================================
# Conceptual model:
#   - In COLD START (few profiles, few resolutions, few edges), strict
#     thresholds prevent ALL signals from emerging. We need permissive
#     gates so the bot can paper-trade and accumulate decision outcomes
#     (which are the only way Thompson Sampling learns).
#   - As the system matures (more profiles with data, more resolutions,
#     more confirmed edges), we *gradually* tighten thresholds because
#     we can now afford selectivity — there's enough signal that the bot
#     should pick the highest-quality opportunities, not all of them.
#
# Maturity ∈ [0, 1] is computed from observable state:
#   - profile_density:  profiles_with_data / 200 (target watchlist size)
#   - resolution_count: total resolved / 5000 (rough mature-state floor)
#   - edge_density:     confirmed_edges / 50  (mature graph size)
#
# Each contributes 1/3 to the maturity score. Saturated at 1.0.
#
# Threshold interpolation: linear between cold (low) and mature (high)
# values. The cold values are what's in `settings` already; mature values
# are 2-3× higher to enforce quality once data exists.
#
# This is INTENTIONALLY simple. We can swap in percentile-based or
# Bayesian-shrinkage logic later if calibration drifts.

ADAPTIVE_RANGES: dict[str, tuple[float, float]] = {
    # name → (cold_value, mature_value)
    "FOLLOW_MIN_TRADES":         (25.0, 50.0),
    "FOLLOW_MIN_FOLLOWERS":      (3.0,  5.0),
    "FADE_MIN_RESOLVED":         (25.0, 50.0),
    "FADE_MIN_CONFIDENCE":       (0.65, 0.75),
    "MIN_CO_OCCURRENCES":        (3.0,  5.0),
    "MIN_SAME_DIRECTION_RATE":   (0.6,  0.7),
    "MIN_RESOLVED_FOR_ERROR_P2": (30.0, 100.0),
    "MIN_RESOLVED_FOR_ERROR_P3": (150.0, 500.0),
}


def compute_system_maturity(
    profiles_with_data: int,
    resolved_total: int,
    confirmed_edges: int,
) -> float:
    """Score in [0,1] reflecting how much data the system has accumulated.
    0 = empty, 1 = mature (enough data that strict thresholds are achievable).
    """
    profile_score = min(1.0, profiles_with_data / 200.0)
    resolution_score = min(1.0, resolved_total / 5000.0)
    edge_score = min(1.0, confirmed_edges / 50.0)
    return round((profile_score + resolution_score + edge_score) / 3.0, 4)


def get_effective_thresholds(
    profiles_with_data: int = 0,
    resolved_total: int = 0,
    confirmed_edges: int = 0,
) -> dict[str, float]:
    """Returns the runtime-effective threshold values, interpolated from
    the cold/mature ranges by current system maturity.

    Callers (confidence_engine, error_model, graph_engine) should call
    this on each cycle to get up-to-date values rather than reading the
    static settings.* directly. Static settings represent the COLD floor.
    """
    m = compute_system_maturity(profiles_with_data, resolved_total, confirmed_edges)
    out: dict[str, float] = {"_maturity": m}
    for name, (cold, mature) in ADAPTIVE_RANGES.items():
        out[name] = cold + (mature - cold) * m
    return out


# Module-level cache of effective thresholds, refreshed by the engine
# scheduler on a periodic job. Modules that gate decisions (confidence_
# engine, error_model, graph_engine, behavior_profiler) read from here
# rather than `settings.*` directly. Initialized to the static cold-start
# floors so the bot starts somewhere sensible if the refresh job hasn't
# fired yet.
EFFECTIVE_THRESHOLDS: dict[str, float] = {
    "FOLLOW_MIN_TRADES":         float(settings.FOLLOW_MIN_TRADES),
    "FOLLOW_MIN_FOLLOWERS":      float(settings.FOLLOW_MIN_FOLLOWERS),
    "FADE_MIN_RESOLVED":         float(settings.FADE_MIN_RESOLVED),
    "FADE_MIN_CONFIDENCE":       float(settings.FADE_MIN_CONFIDENCE),
    "MIN_CO_OCCURRENCES":        float(settings.MIN_CO_OCCURRENCES),
    "MIN_SAME_DIRECTION_RATE":   float(settings.MIN_SAME_DIRECTION_RATE),
    "MIN_RESOLVED_FOR_ERROR_P2": float(settings.MIN_RESOLVED_FOR_ERROR_P2),
    "MIN_RESOLVED_FOR_ERROR_P3": float(settings.MIN_RESOLVED_FOR_ERROR_P3),
    "_maturity": 0.0,
}


def refresh_effective_thresholds(
    profiles_with_data: int,
    resolved_total: int,
    confirmed_edges: int,
) -> dict[str, float]:
    """Recompute and update EFFECTIVE_THRESHOLDS in-place. Returns the
    new dict for logging."""
    new_values = get_effective_thresholds(
        profiles_with_data=profiles_with_data,
        resolved_total=resolved_total,
        confirmed_edges=confirmed_edges,
    )
    EFFECTIVE_THRESHOLDS.update(new_values)
    return EFFECTIVE_THRESHOLDS


def eff(name: str, fallback: float | int | None = None) -> float:
    """Read a threshold from the effective cache. Falls back to the
    static settings.* if the name isn't in the adaptive set, or to the
    explicit `fallback` arg."""
    if name in EFFECTIVE_THRESHOLDS:
        return EFFECTIVE_THRESHOLDS[name]
    if fallback is not None:
        return float(fallback)
    return float(getattr(settings, name, 0))
