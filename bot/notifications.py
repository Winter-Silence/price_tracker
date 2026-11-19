import html

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
) -> bool:
    """Notify the user about newly appearing ads on an Avito search listing.

    Each item carries .url, .title and .price so the notification shows the
    price and a direct link to the fresh ad.

    Возвращает True, если сообщение реально доставлено, иначе False
    (пользователь не найден, бот не инициализирован или произошла ошибка).
    """
    async with db_connection() as conn:
        cursor = await conn.execute(
            "SELECT u.telegram_id FROM users u WHERE u.id = ?",
            (user_id,),
        )
        row = await cursor.fetchone()

    if not row:
        logger.warning("User %d not found for avito notification", user_id)
        return False

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
            return False
        await bot.send_message(
            chat_id=row["telegram_id"],
            text=text,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        logger.info("Avito new-item notification sent for search_link_id=%d", search_link_id)
        return True
    except Exception as exc:
        logger.error(
            "Failed to send avito notification for search_link_id=%d: %s",
            search_link_id, exc,
        )
        return False


async def send_book_lot_notification(
    user_id: int,
    search_link_id: int,
    item,
    details,
    report,
) -> bool:
    """Уведомить о лоте, в котором найдены книги из эталонного списка.

    item — AvitoItem (url/title/price), details — AvitoItemDetails,
    report — books.matcher.MatchReport. Показываем оценку стоимости, но
    подчёркиваем, что это ориентир, а не гарантия цены.
    """
    async with db_connection() as conn:
        cursor = await conn.execute(
            "SELECT u.telegram_id FROM users u WHERE u.id = ?",
            (user_id,),
        )
        row = await cursor.fetchone()

    if not row:
        logger.warning("User %d not found for book notification", user_id)
        return False

    title = html.escape(item.title or details.title or "Объявление Avito")
    price = item.price or details.price
    price_text = f"{price:.0f} ₽" if price else "не указана"
    date_text = details.posted_at_text or "не определена"
    reviews_text = details.reviews_text or "не указаны"

    lines = [
        "📚 <b>Книга из списка на Avito</b>",
        "",
        f"🏷 {title}",
        f"💰 Цена лота: {price_text}",
        f"🕒 Опубликовано: {html.escape(date_text)}",
        f"👤 Продавец: {html.escape(reviews_text)}",
        "",
    ]

    for match in report.matches:
        book = match.book
        lines.append(f"• {html.escape(book.label)}")
        # У изданий без цен оценки нет — строку не показываем.
        if book.price_min is not None and book.price_max is not None:
            estimate = f"{book.price_min:,}–{book.price_max:,} ₽".replace(",", " ")
            lines.append(f"   💵 Оценка: {estimate}")
        lines.append(f"   🔎 {match.label}")
        if match.needs_year:
            lines.append("   ❓ Год не определён — уточните у продавца")

    if len(report.matches) > 1 or report.needs_year:
        low, high = report.estimate()
        if low > 0 and high > 0:
            total = f"{low:,}–{high:,} ₽".replace(",", " ")
            lines.append("")
            lines.append(f"💵 Суммарная оценка по лоту: {total}")

    if report.needs_year:
        variants = sorted({
            (m.book.year_from, m.book.year_to)
            for m in report.matches
            if m.book.group
        })
        if variants:
            years = " / ".join(
                str(low) if low == high else f"{low}–{high}"
                for low, high in variants
            )
            lines.append(f"📅 Подходят варианты: {years}")

    lines += [
        "",
        f"🔗 <a href='{item.url}'>Открыть объявление</a>",
        "<i>Оценка — ориентир по эталонному списку, не гарантия цены.</i>",
    ]

    try:
        bot = get_bot()
        if bot is None:
            logger.error(
                "Bot not initialized, cannot send book notification for search_link_id=%d",
                search_link_id,
            )
            return False
        await bot.send_message(
            chat_id=row["telegram_id"],
            text="\n".join(lines),
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        logger.info(
            "Book notification sent for search_link_id=%d (%d matches)",
            search_link_id, len(report.matches),
        )
        return True
    except Exception as exc:
        logger.error(
            "Failed to send book notification for search_link_id=%d: %s",
            search_link_id, exc,
        )
        return False


async def send_book_llm_status_notification(user_id: int, text: str) -> bool:
    """Сообщить пользователю о состоянии LLM-проверки спорных лотов.

    Используется, когда бесплатные модели исчерпали квоту: платить нельзя,
    но пользователь должен знать, что проверка приостановлена.
    """
    async with db_connection() as conn:
        cursor = await conn.execute(
            "SELECT u.telegram_id FROM users u WHERE u.id = ?",
            (user_id,),
        )
        row = await cursor.fetchone()

    if not row:
        logger.warning("User %d not found for llm status notification", user_id)
        return False

    bot = get_bot()
    if bot is None:
        logger.error("Bot not initialized, cannot send llm status notification")
        return False

    try:
        await bot.send_message(
            chat_id=row["telegram_id"],
            text=text,
            parse_mode="HTML",
            disable_web_page_preview=True,
        )
        logger.info("LLM status notification sent to user %d", user_id)
        return True
    except Exception as exc:
        logger.error(
            "Failed to send llm status notification to user %d: %s",
            user_id, exc,
        )
        return False


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
