"""
Tests for the TelegramBot orchestrator (S3.9).

We don't spin up a real Telegram Application — that would require a
real bot token and reach out to api.telegram.org. Instead we focus on
the orchestrator's *configuration logic* and the *handler wrappers*,
which are the bits we wrote on top of python-telegram-bot.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.telegram_bot import auth, bot as bot_module


# --------------------------------------------------------------------------- #
# _compute_enabled                                                             #
# --------------------------------------------------------------------------- #


def test_disabled_when_flag_false(monkeypatch):
    monkeypatch.setattr(bot_module.settings, "TELEGRAM_ENABLED", False)
    monkeypatch.setattr(bot_module.settings, "TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setattr(bot_module.settings, "TELEGRAM_CHAT_IDS", "1")
    auth.reload_allowlist()
    assert bot_module.TelegramBot._compute_enabled() is False


def test_disabled_when_token_empty(monkeypatch):
    monkeypatch.setattr(bot_module.settings, "TELEGRAM_ENABLED", True)
    monkeypatch.setattr(bot_module.settings, "TELEGRAM_BOT_TOKEN", "")
    monkeypatch.setattr(bot_module.settings, "TELEGRAM_CHAT_IDS", "1")
    auth.reload_allowlist()
    assert bot_module.TelegramBot._compute_enabled() is False


def test_disabled_when_allowlist_empty(monkeypatch):
    monkeypatch.setattr(bot_module.settings, "TELEGRAM_ENABLED", True)
    monkeypatch.setattr(bot_module.settings, "TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setattr(bot_module.settings, "TELEGRAM_CHAT_IDS", "")
    auth.reload_allowlist()
    assert bot_module.TelegramBot._compute_enabled() is False


def test_enabled_when_all_present(monkeypatch):
    monkeypatch.setattr(bot_module.settings, "TELEGRAM_ENABLED", True)
    monkeypatch.setattr(bot_module.settings, "TELEGRAM_BOT_TOKEN", "token")
    monkeypatch.setattr(bot_module.settings, "TELEGRAM_CHAT_IDS", "1")
    auth.reload_allowlist()
    assert bot_module.TelegramBot._compute_enabled() is True


# --------------------------------------------------------------------------- #
# start() short-circuits when disabled                                         #
# --------------------------------------------------------------------------- #


async def test_start_short_circuits_when_disabled(monkeypatch):
    monkeypatch.setattr(bot_module.settings, "TELEGRAM_ENABLED", False)
    auth.reload_allowlist()
    bot = bot_module.TelegramBot(
        redis_client=MagicMock(),
        killswitch=MagicMock(),
    )
    # Should return without raising, without instantiating any Application
    await bot.start()
    assert bot._app is None
    assert bot._notifier is None
    # stop() must be a no-op too
    await bot.stop()


# --------------------------------------------------------------------------- #
# Handler wrappers                                                             #
# --------------------------------------------------------------------------- #


def _make_bot(monkeypatch, allowed_ids: str = "111"):
    monkeypatch.setattr(bot_module.settings, "TELEGRAM_CHAT_IDS", allowed_ids)
    auth.reload_allowlist()
    bot = bot_module.TelegramBot(
        redis_client=MagicMock(),
        killswitch=MagicMock(),
    )
    bot._send = AsyncMock()  # type: ignore[assignment]
    return bot


async def test_wrap_calls_handler_for_authorized_chat(monkeypatch):
    bot = _make_bot(monkeypatch, "111")
    handler = AsyncMock(return_value="hello")
    adapter = bot._wrap(handler)
    update = SimpleNamespace(effective_chat=SimpleNamespace(id=111))
    await adapter(update, context=None)
    handler.assert_awaited_once_with(bot._cmd_ctx)
    bot._send.assert_awaited_once_with(111, "hello")


async def test_wrap_silently_ignores_unauthorized_chat(monkeypatch):
    bot = _make_bot(monkeypatch, "111")
    handler = AsyncMock(return_value="hello")
    adapter = bot._wrap(handler)
    update = SimpleNamespace(effective_chat=SimpleNamespace(id=999))
    await adapter(update, context=None)
    handler.assert_not_awaited()
    bot._send.assert_not_awaited()


async def test_wrap_catches_handler_crash(monkeypatch):
    bot = _make_bot(monkeypatch, "111")

    async def crashing(_ctx):
        raise RuntimeError("boom")

    adapter = bot._wrap(crashing)
    update = SimpleNamespace(effective_chat=SimpleNamespace(id=111))
    # Must NOT raise — and must send a friendly error reply.
    await adapter(update, context=None)
    bot._send.assert_awaited_once()
    chat_id, text = bot._send.await_args.args
    assert chat_id == 111
    assert "Internal error" in text
    assert "RuntimeError" in text


async def test_wrap_with_args_passes_command_args(monkeypatch):
    bot = _make_bot(monkeypatch, "111")
    handler = AsyncMock(return_value="ok")
    adapter = bot._wrap_with_args(handler)
    update = SimpleNamespace(effective_chat=SimpleNamespace(id=111))
    context = SimpleNamespace(args=["dual"])
    await adapter(update, context=context)
    handler.assert_awaited_once_with(bot._cmd_ctx, ["dual"])


async def test_wrap_with_args_handles_no_args(monkeypatch):
    bot = _make_bot(monkeypatch, "111")
    handler = AsyncMock(return_value="ok")
    adapter = bot._wrap_with_args(handler)
    update = SimpleNamespace(effective_chat=SimpleNamespace(id=111))
    context = SimpleNamespace(args=None)
    await adapter(update, context=context)
    handler.assert_awaited_once_with(bot._cmd_ctx, [])


async def test_wrap_with_args_blocks_unauthorized(monkeypatch):
    bot = _make_bot(monkeypatch, "111")
    handler = AsyncMock(return_value="ok")
    adapter = bot._wrap_with_args(handler)
    update = SimpleNamespace(effective_chat=SimpleNamespace(id=42))
    context = SimpleNamespace(args=["dual"])
    await adapter(update, context=context)
    handler.assert_not_awaited()
    bot._send.assert_not_awaited()


# --------------------------------------------------------------------------- #
# _send tolerates a missing app                                                #
# --------------------------------------------------------------------------- #


async def test_send_noop_when_app_none(monkeypatch):
    bot = _make_bot(monkeypatch, "111")
    # Restore real _send, drop the AsyncMock
    real_send = bot_module.TelegramBot._send.__get__(bot, bot_module.TelegramBot)
    # No app set — must return without raising
    bot._app = None
    await real_send(111, "hello")  # should not raise
