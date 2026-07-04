from telegram import Bot
from db.database import db_connection
from utils.logger import logger
from bot.bot_instance import get_bot


async def send_alert_notification(
    user_id: int, link_id: int, current_price: float, threshold_price: float
):
    async with db_connection() as conn:
        cursor = await conn.execute(
            "SELECT ml.url, ml.marketplace, p.name, u.telegram_id "
            "FROM marketplace_links ml "
            "JOIN products p ON p.id = ml.product_id "
            "JOIN users u ON u.id = ? "
            "WHERE ml.id = ?",
            (user_id, link_id),
        )
        row = await cursor.fetchone()

    if not row:
        logger.warning("Link %d not found for notification", link_id)
        return

    text = (
        f"🔔 <b>Цена упала!</b>\n\n"
        f"📦 {row['name']}\n"
        f"🏪 {row['marketplace']}\n"
        f"💰 Текущая цена: {current_price:.2f}₽\n"
        f"🎯 Твоя цель: {threshold_price:.2f}₽\n\n"
        f"🔗 <a href='{row['url']}'>Перейти к товару</a>"
    )

    try:
        bot = get_bot()
        if bot is None:
            logger.error("Bot not initialized, cannot send alert for link_id=%d", link_id)
            return
        await bot.send_message(
            chat_id=row["telegram_id"],
            text=text,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        logger.info("Alert sent for link_id=%d", link_id)
    except Exception as exc:
        logger.error("Failed to send alert for link_id=%d: %s", link_id, exc)
