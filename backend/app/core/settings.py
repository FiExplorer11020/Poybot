from functools import lru_cache

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", case_sensitive=False)

    app_name: str = "Poybot Backend"
    env: str = "dev"
    debug: bool = True
    log_level: str = "INFO"

    api_prefix: str = "/api/v1"
    api_auth_token: str | None = None
    live_ws_token: str | None = None
    enable_rate_limit: bool = True
    rate_limit_requests: int = 120
    rate_limit_window_seconds: int = 60

    postgres_dsn: str = Field(
        default="sqlite+aiosqlite:///poybot.db",
        alias="POSTGRES_DSN",
    )
    redis_url: str = "redis://localhost:6379/0"

    polymarket_gamma_base_url: str = "https://gamma-api.polymarket.com"
    polymarket_clob_rest_base_url: str = "https://clob.polymarket.com"
    polymarket_clob_ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    polymarket_trading_enabled: bool = False
    polymarket_trading_mode: str = "dry_run"
    polymarket_order_endpoint: str = "/order"
    polymarket_api_key: str | None = None
    polymarket_api_secret: str | None = None
    polymarket_api_passphrase: str | None = None
    polymarket_private_key: str | None = None
    backtest_results_dir: str = "data/backtests"

    default_page_size: int = 25
    max_page_size: int = 100

    @field_validator("debug", mode="before")
    @classmethod
    def normalize_debug(cls, value):
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in {"release", "prod", "production", "false", "0", "off", "no"}:
                return False
            if lowered in {"debug", "dev", "true", "1", "on", "yes"}:
                return True
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
