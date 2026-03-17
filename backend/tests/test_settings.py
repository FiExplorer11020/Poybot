from app.core.settings import Settings


def test_settings_load_env_aliases() -> None:
    settings = Settings(POSTGRES_DSN="postgresql+asyncpg://u:p@localhost:5432/x", DEFAULT_PAGE_SIZE=20)
    assert settings.postgres_dsn.endswith("/x")
    assert settings.default_page_size == 20
