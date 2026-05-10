"""
Telegram bot service (S3.9).

Two responsibilities:

  * Notifier — subscribes to Redis channels (`positions:paper_opened`,
    `positions:paper_closed`, `positions:live_opened`,
    `positions:live_closed`, `control:killswitch_changed`,
    `engine:crash`) and pushes formatted messages to the operator's
    chat.

  * Commands — exposes /status, /pnl, /positions, /mode,
    /killswitch, /pause, /resume so the operator can drive the bot
    from a phone without SSH'ing into the VM.

Both share a single python-telegram-bot Application configured in
long-polling mode (no inbound port required — ideal for the Oracle
Cloud Free VM).

The bot is gated by `TELEGRAM_ENABLED + TELEGRAM_BOT_TOKEN +
TELEGRAM_CHAT_IDS`; missing any of those starts the service in a
no-op mode. Failure of the Telegram service must NEVER take down
the engine — every method swallows network errors and logs them.
"""

from src.telegram_bot.bot import TelegramBot

__all__ = ["TelegramBot"]
