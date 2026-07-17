import os

from utils.logger import logger
from bot.bot_instance import get_bot


def get_admin_chat_id() -> int | None:
    raw = os.getenv("ADMIN_TELEGRAM_ID")
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        logger.error("ADMIN_TELEGRAM_ID is not a valid integer: %s", raw)
        return None


async def send_admin_notification(text: str):
    """Send a service notification to the admin configured via ADMIN_TELEGRAM_ID.

    Falls back to WARNING log if the bot is not yet initialized or the admin
    chat id is not set. Never raises so callers can use it in finally/except.
    """
    chat_id = get_admin_chat_id()
    if chat_id is None:
        logger.warning("ADMIN_TELEGRAM_ID not set; admin notification not sent: %s", text)
        return

    bot = get_bot()
    if bot is None:
        logger.warning("Bot not initialized; admin notification not sent: %s", text)
        return

    try:
        await bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        logger.info("Admin notification sent: %s", text[:80])
    except Exception as exc:
        logger.error("Failed to send admin notification: %s", exc)
