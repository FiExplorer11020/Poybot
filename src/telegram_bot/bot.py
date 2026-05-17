"""
TelegramBot orchestrator (S3.9).

Wires together:
  * a python-telegram-bot Application in long-polling mode,
  * the TelegramNotifier (Redis subscriber pushing alerts),
  * the command handlers (defined in commands.py).

Lifecycle is start() / stop(), called by src/engine/main.py. If the
bot is disabled (TELEGRAM_ENABLED=false, missing token, empty
allowlist), start() returns immediately — no exceptions, no log
spam. We design for "missing config = inert" because most dev
machines won't have a Telegram bot configured.

Why python-telegram-bot:
  * Async-native (asyncio.Application).
  * Built-in long-polling with timeout + dropping pending updates.
  * CommandHandler primitive that makes us not parse raw updates.

We deliberately keep the framework in this single file. commands.py
and notifier.py never import telegram.* — they're framework-agnostic
and unit-testable without a Telegram client.
"""

from __future__ import annotations

import asyncio
from typing import Optional

from loguru import logger

from src.config import settings
from src.control.killswitch import KillswitchService
from src.telegram_bot import commands
from src.telegram_bot.auth import authorized_chat_ids, is_authorized, reload_allowlist
from src.telegram_bot.notifier import TelegramNotifier


class TelegramBot:
    """Owns the python-telegram-bot Application + the Redis notifier.
    Public surface: start(), stop()."""

    def __init__(
        self,
        *,
        redis_client,
        killswitch: KillswitchService,
        paper_trader=None,
        live_trader=None,
    ) -> None:
        self._redis = redis_client
        self._killswitch = killswitch
        self._paper_trader = paper_trader
        self._live_trader = live_trader
        self._app = None  # type: ignore[assignment]
        self._notifier: Optional[TelegramNotifier] = None
        self._cmd_ctx = commands.CommandContext(
            redis_client=redis_client,
            killswitch=killswitch,
            paper_trader=paper_trader,
            live_trader=live_trader,
        )
        self._enabled = self._compute_enabled()
        self._running = False

    # ------------------------------------------------------------------ #
    # Configuration                                                       #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _compute_enabled() -> bool:
        """Bot only runs if EVERY required setting is populated. We don't
        want a half-configured deploy to silently skip alerts."""
        if not settings.TELEGRAM_ENABLED:
            return False
        if not settings.TELEGRAM_BOT_TOKEN:
            logger.warning(
                "telegram: TELEGRAM_ENABLED=true but TELEGRAM_BOT_TOKEN empty — disabling"
            )
            return False
        # Refresh allowlist cache then check.
        reload_allowlist()
        if not authorized_chat_ids():
            logger.warning(
                "telegram: TELEGRAM_ENABLED=true but TELEGRAM_CHAT_IDS empty — disabling"
            )
            return False
        return True

    # ------------------------------------------------------------------ #
    # Lifecycle                                                           #
    # ------------------------------------------------------------------ #

    async def start(self) -> None:
        if not self._enabled:
            logger.info("TelegramBot disabled (config), skipping start")
            return
        if self._running:
            return
        self._running = True

        try:
            from telegram.ext import Application, CommandHandler
        except ImportError:
            logger.error(
                "telegram: python-telegram-bot not installed; "
                "TELEGRAM_ENABLED=true but the dep is missing"
            )
            self._running = False
            return

        self._app = (
            Application.builder().token(settings.TELEGRAM_BOT_TOKEN).build()
        )

        # Register command handlers — every handler is wrapped so we
        # auth-gate, log, and never let a handler crash kill the loop.
        self._app.add_handler(CommandHandler("start", self._wrap(commands.cmd_help)))
        self._app.add_handler(CommandHandler("help", self._wrap(commands.cmd_help)))
        self._app.add_handler(CommandHandler("status", self._wrap(commands.cmd_status)))
        self._app.add_handler(CommandHandler("pnl", self._wrap(commands.cmd_pnl)))
        self._app.add_handler(
            CommandHandler("positions", self._wrap(commands.cmd_positions))
        )
        self._app.add_handler(
            CommandHandler("summary", self._wrap(commands.cmd_summary))
        )
        self._app.add_handler(
            CommandHandler("mode", self._wrap_with_args(commands.cmd_mode))
        )
        self._app.add_handler(
            CommandHandler(
                "killswitch", self._wrap_with_args(commands.cmd_killswitch)
            )
        )
        self._app.add_handler(CommandHandler("pause", self._wrap(commands.cmd_pause)))
        self._app.add_handler(CommandHandler("resume", self._wrap(commands.cmd_resume)))

        # Start the Application (initialize -> start -> updater.start_polling).
        await self._app.initialize()
        await self._app.start()
        if settings.TELEGRAM_COMMANDS_ENABLED:
            await self._app.updater.start_polling(
                timeout=settings.TELEGRAM_POLL_TIMEOUT_S,
                drop_pending_updates=True,
            )
            logger.info("TelegramBot polling started")
        else:
            logger.info("TelegramBot started in alerts-only mode (commands disabled)")

        # Start the notifier — it uses our send method so all outbound
        # messages funnel through the same throttle and chat allowlist.
        self._notifier = TelegramNotifier(
            redis_client=self._redis,
            send_fn=self._send,
        )
        await self._notifier.start()

        # Keep this coroutine alive so the engine watchdog doesn't think we
        # crashed: python-telegram-bot's start_polling() is non-blocking
        # (the polling runs in background tasks owned by the Application),
        # so without this loop, start() would return and the watchdog would
        # interpret the resolved Task as a crash → restart loop.
        try:
            while self._running:
                await asyncio.sleep(60)
        except asyncio.CancelledError:
            # stop() flips _running and the watchdog/main cancels us; that's
            # the expected shutdown path, swallow the cancellation cleanly.
            raise

    async def stop(self) -> None:
        if not self._running:
            return
        self._running = False
        if self._notifier is not None:
            await self._notifier.stop()
        if self._app is not None:
            try:
                if self._app.updater and self._app.updater.running:
                    await self._app.updater.stop()
                await self._app.stop()
                await self._app.shutdown()
            except Exception:
                logger.exception("TelegramBot: error during shutdown")
        logger.info("TelegramBot stopped")

    # ------------------------------------------------------------------ #
    # Send (used by notifier + handlers)                                  #
    # ------------------------------------------------------------------ #

    async def _send(self, chat_id: int, text: str) -> None:
        """Single point for outbound messages. Imported by the notifier
        as `send_fn` so the throttle + allowlist live here only once."""
        if self._app is None:
            return
        try:
            await self._app.bot.send_message(chat_id=chat_id, text=text)
        except Exception as e:
            logger.warning(f"telegram send to {chat_id} failed: {e}")

    # ------------------------------------------------------------------ #
    # Handler wrappers                                                    #
    # ------------------------------------------------------------------ #

    def _wrap(self, fn):
        """Adapt a (ctx) -> str handler into a python-telegram-bot
        CommandHandler signature (update, context). Auth-gates and
        catches every exception."""

        async def adapter(update, context):  # noqa: ANN001 (PTB types)
            chat_id = update.effective_chat.id if update.effective_chat else None
            if chat_id is None or not is_authorized(chat_id):
                logger.info(f"telegram: ignoring command from unauthorized chat={chat_id}")
                return
            try:
                reply = await fn(self._cmd_ctx)
            except Exception as e:
                logger.exception("telegram cmd crashed")
                reply = f"❌ Internal error: {e.__class__.__name__}: {e}"
            await self._send(chat_id, reply)

        return adapter

    def _wrap_with_args(self, fn):
        """Same as `_wrap` but for handlers that take positional args
        from the command (e.g. /mode dual)."""

        async def adapter(update, context):  # noqa: ANN001
            chat_id = update.effective_chat.id if update.effective_chat else None
            if chat_id is None or not is_authorized(chat_id):
                logger.info(f"telegram: ignoring command from unauthorized chat={chat_id}")
                return
            args = list(context.args) if context and context.args else []
            try:
                reply = await fn(self._cmd_ctx, args)
            except Exception as e:
                logger.exception("telegram cmd-with-args crashed")
                reply = f"❌ Internal error: {e.__class__.__name__}: {e}"
            await self._send(chat_id, reply)

        return adapter
