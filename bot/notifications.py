from db.database import db_connection
from parsers.base import TIER_LABELS
from utils.logger import logger
from bot.bot_instance import get_bot


async def send_avito_new_item_notification(
    user_id: int,
    search_link_id: int,
    product_name: str,
    search_url: str,
    items: list,
):
    """Notify the user about newly appearing ads on an Avito search listing.

    Each item carries .url, .title and .price so the notification shows the
    price and a direct link to the fresh ad.
    """
    async with db_connection() as conn:
        cursor = await conn.execute(
            "SELECT u.telegram_id FROM users u WHERE u.id = ?",
            (user_id,),
        )
        row = await cursor.fetchone()

    if not row:
        logger.warning("User %d not found for avito notification", user_id)
        return

    lines = ["🆕 <b>Новый товар на Avito!</b>", "", f"📦 {product_name}"]
    if len(items) == 1:
        it = items[0]
        lines.append("")
        price = f"{it.price:.0f} ₽" if it.price else "—"
        lines.append(f"🏷 {it.title}")
        lines.append(f"💰 Цена: {price}")
        lines.append("")
        lines.append(f"🔗 <a href='{it.url}'>Открыть объявление</a>")
    else:
        lines.append("")
        for idx, it in enumerate(items, start=1):
            price = f"{it.price:.0f} ₽" if it.price else "—"
            lines.append(
                f"{idx}. <a href='{it.url}'>{it.title}</a> — {price}"
            )
    lines.append(f"🔍 <a href='{search_url}'>Открыть поиск</a>")

    text = "\n".join(lines)

    try:
        bot = get_bot()
        if bot is None:
            logger.error(
                "Bot not initialized, cannot send avito notification for search_link_id=%d",
                search_link_id,
            )
            return
        await bot.send_message(
            chat_id=row["telegram_id"],
            text=text,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        logger.info("Avito new-item notification sent for search_link_id=%d", search_link_id)
    except Exception as exc:
        logger.error(
            "Failed to send avito notification for search_link_id=%d: %s",
            search_link_id, exc,
        )


async def send_alert_notification(
    user_id: int, link_id: int, current_price: float,
    threshold_price: float, privilege_type: str = "standard",
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

    tier_label = TIER_LABELS.get(privilege_type, privilege_type)
    if privilege_type == "standard":
        text = (
            f"🔔 <b>Цена упала!</b>\n\n"
            f"📦 {row['name']}\n"
            f"🏪 {row['marketplace']}\n"
            f"💰 Текущая цена: {current_price:.2f}₽\n"
            f"🎯 Твоя цель: {threshold_price:.2f}₽\n\n"
            f"🔗 <a href='{row['url']}'>Перейти к товару</a>"
        )
    else:
        text = (
            f"🔔 <b>Цена упала!</b>\n\n"
            f"📦 {row['name']}\n"
            f"🏪 {row['marketplace']}\n"
            f"💳 Тип цены: {tier_label}\n"
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


async def send_search_alert_notification(
    user_id: int, search_link_id: int,
    current_price: float, threshold_price: float,
    resolved_url: str, resolved_title: str,
    prev_resolved_url: str | None, marketplace: str,
    privilege_type: str = "standard",
):
    async with db_connection() as conn:
        cursor = await conn.execute(
            "SELECT sl.search_url, sl.title_filter, p.name, u.telegram_id "
            "FROM search_links sl "
            "JOIN products p ON p.id = sl.product_id "
            "JOIN users u ON u.id = ? "
            "WHERE sl.id = ?",
            (user_id, search_link_id),
        )
        row = await cursor.fetchone()

    if not row:
        logger.warning("Search link %d not found for notification", search_link_id)
        return

    seller_changed = prev_resolved_url is not None and prev_resolved_url != resolved_url

    lines = [
        "🔔 <b>Найден дешёвый товар!</b>",
        "",
        f"📦 {row['name']}",
        f"🔍 Поиск: \"{row['title_filter']}\"",
        f"🏪 {marketplace}",
        f"💰 Текущая цена: {current_price:.2f}₽",
        f"🎯 Твоя цель: {threshold_price:.2f}₽",
    ]
    if privilege_type != "standard":
        tier_label = TIER_LABELS.get(privilege_type, privilege_type)
        lines.append(f"💳 Тип цены: {tier_label}")

    if seller_changed:
        lines.append("")
        lines.append("🔀 <i>Подешевевший вариант у другого продавца!</i>")

    lines.append("")
    lines.append(f"🔗 <a href='{resolved_url}'>{resolved_title}</a>")
    lines.append(f"🔍 <a href='{row['search_url']}'>Открыть поиск</a>")

    text = "\n".join(lines)

    try:
        bot = get_bot()
        if bot is None:
            logger.error("Bot not initialized, cannot send search alert for search_link_id=%d", search_link_id)
            return
        await bot.send_message(
            chat_id=row["telegram_id"],
            text=text,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        logger.info("Search alert sent for search_link_id=%d", search_link_id)
    except Exception as exc:
        logger.error("Failed to send search alert for search_link_id=%d: %s", search_link_id, exc)
