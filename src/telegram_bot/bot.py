"""
TelegramBot orchestrator (S3.9 + S3.11).

Wires together:
  * a python-telegram-bot Application in long-polling mode,
  * the TelegramNotifier (Redis subscriber pushing alerts),
  * the AlertsManager (configurable threshold-based rules),
  * the command handlers (defined in commands.py and commands_extras.py).

Lifecycle is start() / stop(), called by src/engine/main.py. If the
bot is disabled (TELEGRAM_ENABLED=false, missing token, empty
allowlist), start() returns immediately — no exceptions, no log
spam. We design for "missing config = inert" because most dev
machines won't have a Telegram bot configured.

Public accessors used by the engine scheduler:
  * .notifier — push() for digests / ad-hoc alerts
  * .alerts_mgr — evaluate() for periodic rule checks
"""

from __future__ import annotations

import asyncio
import time
from typing import Optional

from loguru import logger

from src.config import settings
from src.control.killswitch import KillswitchService
from src.telegram_bot import commands, commands_extras
from src.telegram_bot.alerts import AlertsManager
from src.telegram_bot.auth import authorized_chat_ids, is_authorized, reload_allowlist
from src.telegram_bot.notifier import TelegramNotifier


class TelegramBot:
    """Owns the python-telegram-bot Application, TelegramNotifier, and
    AlertsManager. Public surface: start(), stop(), notifier, alerts_mgr."""

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
        self._alerts_mgr: Optional[AlertsManager] = None
        self._cmd_ctx = commands.CommandContext(
            redis_client=redis_client,
            killswitch=killswitch,
            paper_trader=paper_trader,
            live_trader=live_trader,
        )
        self._enabled = self._compute_enabled()
        self._running = False

    # ------------------------------------------------------------------ #
    # Public accessors                                                    #
    # ------------------------------------------------------------------ #

    @property
    def notifier(self) -> Optional[TelegramNotifier]:
        return self._notifier

    @property
    def alerts_mgr(self) -> Optional[AlertsManager]:
        return self._alerts_mgr

    @property
    def paper_trader(self):
        return self._paper_trader

    # ------------------------------------------------------------------ #
    # Configuration                                                       #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _compute_enabled() -> bool:
        """Bot only runs if EVERY required setting is populated."""
        if not settings.TELEGRAM_ENABLED:
            return False
        if not settings.TELEGRAM_BOT_TOKEN:
            logger.warning(
                "telegram: TELEGRAM_ENABLED=true but TELEGRAM_BOT_TOKEN empty — disabling"
            )
            return False
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
            logger.info("TelegramBot disabled (config), idling start coroutine")
            await asyncio.Event().wait()
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

        # --- S3.9 commands ---
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

        # --- S3.11 commands (extras) ---
        self._app.add_handler(
            CommandHandler("leaders", self._wrap_with_args(commands_extras.cmd_leaders))
        )
        self._app.add_handler(
            CommandHandler("leader", self._wrap_with_args(commands_extras.cmd_leader))
        )
        self._app.add_handler(
            CommandHandler("health", self._wrap(commands_extras.cmd_health))
        )
        self._app.add_handler(
            CommandHandler("trades", self._wrap_with_args(commands_extras.cmd_trades))
        )
        self._app.add_handler(CommandHandler("risk", self._wrap(commands_extras.cmd_risk)))
        self._app.add_handler(
            CommandHandler("digest", self._wrap_with_args(commands_extras.cmd_digest))
        )
        self._app.add_handler(
            CommandHandler("drift", self._wrap(commands_extras.cmd_drift))
        )
        self._app.add_handler(
            CommandHandler("market", self._wrap_with_args(commands_extras.cmd_market))
        )
        self._app.add_handler(
            CommandHandler("set", self._wrap_with_args(commands_extras.cmd_set))
        )
        self._app.add_handler(
            CommandHandler(
                "verbosity", self._wrap_with_args(commands_extras.cmd_verbosity)
            )
        )
        self._app.add_handler(
            CommandHandler("alert", self._wrap_with_args(commands_extras.cmd_alert))
        )

        # Application lifecycle.
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

        # Notifier (channel-subscriber + push).
        self._notifier = TelegramNotifier(
            redis_client=self._redis,
            send_fn=self._send,
        )
        await self._notifier.start()

        # AlertsManager (configurable thresholds).
        self._alerts_mgr = AlertsManager(redis_client=self._redis)
        await self._alerts_mgr.load()

        # Wire deps into CommandContext now that they exist.
        self._cmd_ctx.notifier = self._notifier
        self._cmd_ctx.alerts_mgr = self._alerts_mgr
        self._cmd_ctx.engine_started_at = time.time()

        # Keep this coroutine alive so the watchdog doesn't restart us.
        try:
            while self._running:
                await asyncio.sleep(60)
        except asyncio.CancelledError:
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
        await self._app.bot.send_message(chat_id=chat_id, text=text)
        # NOTE: errors raised here propagate to the notifier so it can
        # bump its exponential backoff. Earlier versions swallowed the
        # exception, which silently disabled the backoff feedback.

    # ------------------------------------------------------------------ #
    # Handler wrappers                                                    #
    # ------------------------------------------------------------------ #

    def _wrap(self, fn):
        """Adapt a (ctx) -> str handler. Auth-gates and catches errors."""

        async def adapter(update, context):  # noqa: ANN001
            chat_id = update.effective_chat.id if update.effective_chat else None
            if chat_id is None or not is_authorized(chat_id):
                logger.info(
                    f"telegram: ignoring command from unauthorized chat={chat_id}"
                )
                return
            try:
                reply = await fn(self._cmd_ctx)
            except Exception as e:
                logger.exception("telegram cmd crashed")
                reply = f"❌ Internal error: {e.__class__.__name__}: {e}"
            try:
                await self._send(chat_id, reply)
            except Exception as e:
                logger.warning(f"telegram cmd reply send failed: {e}")

        return adapter

    def _wrap_with_args(self, fn):
        """Same as `_wrap` but for handlers that take positional args."""

        async def adapter(update, context):  # noqa: ANN001
            chat_id = update.effective_chat.id if update.effective_chat else None
            if chat_id is None or not is_authorized(chat_id):
                logger.info(
                    f"telegram: ignoring command from unauthorized chat={chat_id}"
                )
                return
            args = list(context.args) if context and context.args else []
            try:
                reply = await fn(self._cmd_ctx, args)
            except Exception as e:
                logger.exception("telegram cmd-with-args crashed")
                reply = f"❌ Internal error: {e.__class__.__name__}: {e}"
            try:
                await self._send(chat_id, reply)
            except Exception as e:
                logger.warning(f"telegram cmd reply send failed: {e}")

        return adapter
