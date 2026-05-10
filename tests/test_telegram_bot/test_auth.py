"""
Tests for the chat_id allowlist (S3.9).

The allowlist is a frozenset cached at module level; tests monkey-patch
settings.TELEGRAM_CHAT_IDS and call reload_allowlist() before each
assertion so we don't pick up stale state from another test.
"""

from __future__ import annotations

import pytest

from src.telegram_bot import auth


@pytest.fixture(autouse=True)
def _reset_allowlist():
    """Drop the lru_cache on every test. Without this, the first test's
    allowlist would freeze for the rest of the session."""
    auth.reload_allowlist()
    yield
    auth.reload_allowlist()


def test_parse_allowlist_empty_string():
    assert auth.parse_allowlist("") == frozenset()


def test_parse_allowlist_single_id():
    assert auth.parse_allowlist("12345") == frozenset({12345})


def test_parse_allowlist_multiple_ids():
    assert auth.parse_allowlist("1,2,3") == frozenset({1, 2, 3})


def test_parse_allowlist_strips_whitespace():
    assert auth.parse_allowlist(" 1 , 2 , 3 ") == frozenset({1, 2, 3})


def test_parse_allowlist_skips_invalid_tokens():
    """Bad tokens log a warning and are dropped — but the rest of the
    allowlist must survive a typo."""
    assert auth.parse_allowlist("1,oops,2") == frozenset({1, 2})


def test_parse_allowlist_skips_empty_tokens():
    assert auth.parse_allowlist("1,,2,") == frozenset({1, 2})


def test_is_authorized_false_when_empty(monkeypatch):
    monkeypatch.setattr(auth.settings, "TELEGRAM_CHAT_IDS", "")
    auth.reload_allowlist()
    assert auth.is_authorized(123) is False


def test_is_authorized_true_when_match(monkeypatch):
    monkeypatch.setattr(auth.settings, "TELEGRAM_CHAT_IDS", "100,200")
    auth.reload_allowlist()
    assert auth.is_authorized(100) is True
    assert auth.is_authorized(200) is True


def test_is_authorized_false_when_mismatch(monkeypatch):
    monkeypatch.setattr(auth.settings, "TELEGRAM_CHAT_IDS", "100,200")
    auth.reload_allowlist()
    assert auth.is_authorized(999) is False


def test_authorized_chat_ids_returns_frozenset(monkeypatch):
    monkeypatch.setattr(auth.settings, "TELEGRAM_CHAT_IDS", "1,2,3")
    auth.reload_allowlist()
    out = auth.authorized_chat_ids()
    assert isinstance(out, frozenset)
    assert out == frozenset({1, 2, 3})


def test_reload_picks_up_changes(monkeypatch):
    monkeypatch.setattr(auth.settings, "TELEGRAM_CHAT_IDS", "1")
    auth.reload_allowlist()
    assert auth.is_authorized(1)
    monkeypatch.setattr(auth.settings, "TELEGRAM_CHAT_IDS", "2")
    auth.reload_allowlist()
    assert not auth.is_authorized(1)
    assert auth.is_authorized(2)
