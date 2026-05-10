"""
chat_id allowlist for the Telegram bot (S3.9).

The bot accepts commands and pushes alerts ONLY to chat_ids in
`settings.TELEGRAM_CHAT_IDS` (comma-separated). Any incoming command
from a non-allowlisted chat is silently ignored — we don't want to
leak that the bot is even running, and we definitely don't want a
random Telegram user typing /killswitch and stopping live trading.
"""

from __future__ import annotations

from functools import lru_cache

from loguru import logger

from src.config import settings


def parse_allowlist(raw: str) -> frozenset[int]:
    """Parse the comma-separated TELEGRAM_CHAT_IDS env var into a set
    of int chat_ids. Bad entries are skipped with a warning so a
    typo doesn't take the whole allowlist down."""
    if not raw:
        return frozenset()
    out: set[int] = set()
    for token in raw.split(","):
        token = token.strip()
        if not token:
            continue
        try:
            out.add(int(token))
        except ValueError:
            logger.warning(f"telegram: ignoring invalid chat_id in allowlist: {token!r}")
    return frozenset(out)


@lru_cache(maxsize=1)
def _cached_allowlist() -> frozenset[int]:
    return parse_allowlist(settings.TELEGRAM_CHAT_IDS)


def is_authorized(chat_id: int) -> bool:
    """Return True iff the chat_id is in the configured allowlist.
    The allowlist is cached at module level — call `reload_allowlist()`
    to pick up env mutations (typically only useful in tests)."""
    return chat_id in _cached_allowlist()


def reload_allowlist() -> None:
    """Drop the cached allowlist so the next is_authorized call re-reads
    settings.TELEGRAM_CHAT_IDS. Tests use this after monkeypatching."""
    _cached_allowlist.cache_clear()


def authorized_chat_ids() -> frozenset[int]:
    """Return the current allowlist (read-only). Used by the notifier
    when broadcasting alerts to every operator."""
    return _cached_allowlist()
