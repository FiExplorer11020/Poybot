"""
Centralized configuration via pydantic-settings.
ALL constants from CLAUDE.md § 9, overridable via .env.

Usage:
    from src.config import settings
"""

from pathlib import Path

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
    FALCON_API_URL: str = (
        "https://narrative.agent.heisenberg.so/api/v2/semantic/retrieve/parameterized"
    )
    FALCON_REFRESH_INTERVAL_S: int = 3600
    FALCON_CACHE_TTL_S: int = 172800  # 48h — survive Falcon downtime
    FALCON_MAX_REQUESTS_PER_MINUTE: int = 60

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
    TOP_MARKETS_COUNT: int = 50
    TRADE_OBSERVER_POLL_INTERVAL_S: int = 30
    DATA_API_GLOBAL_TRADES_LIMIT: int = 500
    DATA_API_RECENT_LEADER_MARKETS: int = 200
    WEBSOCKET_PING_INTERVAL_S: int = 30
    WEBSOCKET_PONG_TIMEOUT_S: int = 10

    # ------------------------------------------------------------------ #
    # Graph Engine (CLAUDE.md § 9)                                        #
    # ------------------------------------------------------------------ #
    FOLLOWER_WINDOW_S: int = 300
    MIN_CO_OCCURRENCES: int = 5
    MIN_SAME_DIRECTION_RATE: float = 0.7
    HAWKES_LOOKBACK_DAYS: int = 30

    # ------------------------------------------------------------------ #
    # Profiler (CLAUDE.md § 9)                                            #
    # ------------------------------------------------------------------ #
    EWMA_LAMBDA: float = 0.94
    MIN_TRADES_FOR_PROFILE: int = 20
    MIN_RESOLVED_FOR_ERROR_P2: int = 100
    MIN_RESOLVED_FOR_ERROR_P3: int = 500

    # ------------------------------------------------------------------ #
    # Confidence Engine (CLAUDE.md § 9)                                   #
    # ------------------------------------------------------------------ #
    FOLLOW_MIN_TRADES: int = 50
    FOLLOW_MIN_FOLLOWERS: int = 5
    FADE_MIN_RESOLVED: int = 50
    FADE_MIN_CONFIDENCE: float = 0.75
    THOMPSON_EXPLORATION_FLOOR: float = 0.10
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

    # ------------------------------------------------------------------ #
    # Batch Processing (CLAUDE.md § 9)                                    #
    # ------------------------------------------------------------------ #
    BATCH_HOUR_UTC: int = 3
    BATCH_HAWKES_LEADERS: int = 200
    RETENTION_TRADES_DAYS: int = 90

    # ------------------------------------------------------------------ #
    # Logging                                                             #
    # ------------------------------------------------------------------ #
    LOG_LEVEL: str = "INFO"


settings = Settings()
