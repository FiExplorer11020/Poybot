"""
Centralized loguru configuration driven by `settings.LOG_LEVEL`, `LOG_FILE`,
`LOG_FILE_ROTATION`, `LOG_FILE_RETENTION` (see `src/config.py`).

By default loguru ships with a stderr DEBUG sink that fires from import time.
Each entry point (api/main, observer/main, engine/main, registry/main) calls
`configure_logging()` once at startup to:

  1. Remove the default sink so there's only one stderr writer (no double logs).
  2. Re-add stderr at the configured level with a stable format.
  3. Optionally add a rotating file sink for the production VM.

The function is idempotent — calling it twice (e.g. uvicorn reload, or two
entry points sharing a process) won't accumulate sinks. A module-level guard
also protects against accidental re-entry.
"""

from __future__ import annotations

import sys
from typing import Optional

from loguru import logger

from src.config import settings

_DEFAULT_FORMAT = (
    "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
    "<level>{level: <8}</level> | "
    "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
    "<level>{message}</level>"
)
_VALID_LEVELS = {"TRACE", "DEBUG", "INFO", "SUCCESS", "WARNING", "ERROR", "CRITICAL"}

# Marker that lets us know we've already configured this process's logger.
# Loguru's `logger.remove()` is global, so if multiple modules import and call
# configure_logging() we collapse to one consistent setup rather than stacking.
_configured: bool = False


def _normalize_level(raw: Optional[str]) -> str:
    """Coerce a user-supplied level string into a valid loguru level.

    Falls back to INFO with a warning log entry on bad input — never raises,
    because we don't want a typo in `.env` to crash the process at boot.
    """
    if not raw:
        return "INFO"
    candidate = raw.strip().upper()
    if candidate not in _VALID_LEVELS:
        # We have to use the still-default loguru sink here; safe because this
        # path runs before we've reconfigured.
        logger.warning(
            f"LOG_LEVEL='{raw}' is not a valid loguru level "
            f"({sorted(_VALID_LEVELS)}); falling back to INFO."
        )
        return "INFO"
    return candidate


def configure_logging(force: bool = False) -> str:
    """Install the stderr (and optional file) sinks from settings.

    Returns the resolved level so callers can log it back for ops visibility.
    Pass `force=True` to re-install sinks even if already configured (useful
    for tests).
    """
    global _configured
    if _configured and not force:
        return _normalize_level(settings.LOG_LEVEL)

    level = _normalize_level(settings.LOG_LEVEL)

    # Wipe whatever loguru already had (the default DEBUG-stderr sink, plus
    # any prior call's sinks if force=True).
    logger.remove()

    logger.add(
        sys.stderr,
        level=level,
        format=_DEFAULT_FORMAT,
        backtrace=False,  # tracebacks belong in the file sink, not stderr
        diagnose=False,   # diagnose=True can leak local-variable values
        enqueue=False,
    )

    log_file = (settings.LOG_FILE or "").strip()
    if log_file:
        logger.add(
            log_file,
            level=level,
            format=_DEFAULT_FORMAT,
            rotation=settings.LOG_FILE_ROTATION or "daily",
            retention=settings.LOG_FILE_RETENTION or "14 days",
            compression="zip",
            backtrace=True,    # full tracebacks in file
            diagnose=False,    # but no variable values (security)
            enqueue=True,      # async-safe: writes happen on a background thread
        )

    _configured = True
    return level


def reset_for_testing() -> None:
    """Test-only: drop the configured flag so a subsequent configure_logging()
    re-runs from scratch. Production code should never call this."""
    global _configured
    _configured = False
    logger.remove()
