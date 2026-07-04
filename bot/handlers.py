from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

from db.database import (
    db_connection,
    get_or_create_user,
    add_product,
    add_marketplace_link,
    add_alert,
    get_user_products,
    get_product_by_id,
    get_price_history,
    delete_link,
    get_user_alerts,
    update_alert_threshold,
)
from parsers import get_parser
from utils.logger import logger

ADD_URL, ADD_TARGET_PRICE, ADD_NAME = range(3)
HISTORY_LINK = range(1)
LINK_SELECT, LINK_URL, LINK_TARGET_PRICE = range(3, 6)
THRESHOLD_SELECT, THRESHOLD_INPUT = range(6, 8)
LINK_SELECT, LINK_URL, LINK_TARGET_PRICE = range(3, 6)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        await update.message.reply_text(
            "👋 Привет! Я бот для отслеживания цен на маркетплейсах.\n\n"
            "Команды:\n"
            "/add — добавить новый товар для отслеживания\n"
            "/link — привязать ссылку к существующему товару\n"
            "/threshold — изменить пороговую цену уведомления\n"
            "/list — список твоих товаров\n"
            "/delete — удалить товар\n"
            "/history — история цен товара\n"
            "/help — помощь"
        )
    except Exception as exc:
        logger.error("Failed to send /start reply: %s", exc)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        await update.message.reply_text(
            "📖 <b>Помощь</b>\n\n"
            "/add — добавить новый товар: название + ссылка + порог цены\n"
            "/link — привязать ссылку к уже существующему товару\n"
            "/threshold — изменить пороговую цену уведомления (или 0 — отключить)\n"
            "/list — показать все твои товары с текущими ценами\n"
            "/delete — удалить товар (с подтверждением)\n"
            "/history — история цен товара\n\n"
            "Поддерживаемые магазины: Wildberries, Ozon, Citilink",
            parse_mode="HTML"
        )
    except Exception as exc:
        logger.error("Failed to send /help reply: %s", exc)


async def add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔗 Пришли ссылку на товар (Wildberries, Ozon, Citilink)")
    return ADD_URL


async def add_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()
    parser = get_parser(url)
    if not parser:
        await update.message.reply_text("❌ Неподдерживаемый магазин. Поддерживаются: Wildberries, Ozon, Citilink")
        return ConversationHandler.END

    context.user_data["url"] = url
    context.user_data["marketplace"] = parser.marketplace
    await update.message.reply_text("📦 Введи название товара (для удобства)")
    return ADD_NAME


async def add_name(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["name"] = update.message.text.strip()
    await update.message.reply_text("💰 Введи пороговую цену (или 0, чтобы просто отслеживать)")
    return ADD_TARGET_PRICE


async def add_target_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        target_price = float(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ Введи число")
        return ADD_TARGET_PRICE

    url = context.user_data["url"]
    marketplace = context.user_data["marketplace"]
    name = context.user_data["name"]
    telegram_id = update.effective_user.id

    user_id = await get_or_create_user(telegram_id)
    product_id = await add_product(name, user_id)
    link_id = await add_marketplace_link(product_id, marketplace, url)

    if target_price > 0:
        await add_alert(user_id, link_id, target_price)

    await update.message.reply_text("✅ Товар добавлен! Буду проверять цену каждый час.")
    return ConversationHandler.END


async def list_products(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    user_id = await get_or_create_user(telegram_id)
    products = await get_user_products(user_id)

    if not products:
        await update.message.reply_text("📭 У тебя пока нет товаров. Добавь через /add")
        return

    text = "📦 <b>Твои товары:</b>\n\n"
    for p in products:
        price = f"{p['last_price']:.0f}₽" if p["last_price"] else "—"
        text += (
            f"🔹 {p['name']}\n"
            f"   🏪 {p['marketplace']} | Цена: {price}\n"
            f"   🔗 ID связи: {p['link_id']} | ID товара: {p['id']}\n\n"
        )

    await update.message.reply_text(text, parse_mode="HTML")


async def delete_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    user_id = await get_or_create_user(telegram_id)
    products = await get_user_products(user_id)

    if not products:
        await update.message.reply_text("📭 Нечего удалять")
        return

    keyboard = [
        [InlineKeyboardButton(
            f"🗑 {p['name']}",
            callback_data=f"del_{p['link_id']}",
        )]
        for p in products
    ]
    await update.message.reply_text(
        "Выбери товар для удаления:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def delete_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    link_id = int(query.data.split("_")[1])

    keyboard = [
        [
            InlineKeyboardButton("✅ Да, удалить", callback_data=f"confirm_del_{link_id}"),
            InlineKeyboardButton("❌ Отмена", callback_data="cancel_del"),
        ]
    ]
    await query.edit_message_text(
        "Точно удалить этот товар?",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def delete_execute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    link_id = int(query.data.split("_")[2])
    telegram_id = update.effective_user.id

    user_id = await get_or_create_user(telegram_id)
    await delete_link(link_id, user_id)
    await query.edit_message_text("✅ Товар удалён (деактивирован)")


async def cancel_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("❌ Отменено")


async def history_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    user_id = await get_or_create_user(telegram_id)
    products = await get_user_products(user_id)

    if not products:
        await update.message.reply_text("📭 У тебя пока нет товаров")
        return ConversationHandler.END

    keyboard = [
        [InlineKeyboardButton(
            f"📊 {p['name']}",
            callback_data=f"hist_{p['link_id']}",
        )]
        for p in products
    ]
    await update.message.reply_text(
        "Выбери товар для просмотра истории цен:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return HISTORY_LINK


async def history_show(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    link_id = int(query.data.split("_")[1])
    records = await get_price_history(link_id)

    if not records:
        await query.edit_message_text("📭 Истории цен пока нет")
        return ConversationHandler.END

    text = "📊 <b>История цен:</b>\n\n"
    for r in records:
        text += f"💰 {r['price']:.2f}₽ — {r['recorded_at']}\n"

    await query.edit_message_text(text, parse_mode="HTML")
    return ConversationHandler.END


# ===== /link command =====

async def link_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    user_id = await get_or_create_user(telegram_id)
    products = await get_user_products(user_id)

    if not products:
        await update.message.reply_text("📭 У тебя пока нет товаров. Добавь через /add")
        return ConversationHandler.END

    keyboard = [
        [InlineKeyboardButton(
            f"📦 {p['name']}",
            callback_data=f"linkprod_{p['id']}",
        )]
        for p in products
    ]
    await update.message.reply_text(
        "🔗 Выбери товар, к которому привязать ссылку:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return LINK_SELECT


async def link_select_product(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    product_id = int(query.data.split("_")[1])

    product = await get_product_by_id(product_id)
    if not product:
        await query.edit_message_text("❌ Товар не найден")
        return ConversationHandler.END

    context.user_data["link_product_id"] = product_id
    context.user_data["link_product_name"] = product["name"]
    await query.edit_message_text(
        f"📦 Товар: <b>{product['name']}</b>\n\n"
        "🔗 Пришли ссылку на товар (Wildberries, Ozon, Citilink):",
        parse_mode="HTML"
    )
    return LINK_URL


async def link_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()
    parser = get_parser(url)
    if not parser:
        await update.message.reply_text("❌ Неподдерживаемый магазин. Поддерживаются: Wildberries, Ozon, Citilink")
        return ConversationHandler.END

    context.user_data["link_url"] = url
    context.user_data["link_marketplace"] = parser.marketplace
    await update.message.reply_text("💰 Введи пороговую цену (или 0, чтобы просто отслеживать):")
    return LINK_TARGET_PRICE


async def link_target_price(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        target_price = float(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ Введи число")
        return LINK_TARGET_PRICE

    product_id = context.user_data["link_product_id"]
    product_name = context.user_data["link_product_name"]
    url = context.user_data["link_url"]
    marketplace = context.user_data["link_marketplace"]
    telegram_id = update.effective_user.id

    user_id = await get_or_create_user(telegram_id)
    link_id = await add_marketplace_link(product_id, marketplace, url)

    if target_price > 0:
        await add_alert(user_id, link_id, target_price)

    await update.message.reply_text(
        f"✅ Ссылка привязана к товару <b>{product_name}</b>!\n"
        f"🏪 Маркетплейс: {marketplace}\n"
        "Буду проверять цену каждый час.",
        parse_mode="HTML"
    )
    return ConversationHandler.END


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("❌ Отменено")
    return ConversationHandler.END


# ===== /threshold command =====

async def threshold_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    user_id = await get_or_create_user(telegram_id)
    alerts = await get_user_alerts(user_id)

    if not alerts:
        await update.message.reply_text("📭 У тебя нет активных уведомлений о цене. Добавь через /add или /link")
        return ConversationHandler.END

    keyboard = [
        [InlineKeyboardButton(
            f"📦 {a['product_name']} | 🏪 {a['marketplace']} | 💰 {a['threshold_price']:.0f}₽",
            callback_data=f"thresh_{a['id']}",
        )]
        for a in alerts
    ]
    await update.message.reply_text(
        "🔔 Выбери уведомление для изменения порога:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return THRESHOLD_SELECT


async def threshold_select(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    alert_id = int(query.data.split("_")[1])

    context.user_data["threshold_alert_id"] = alert_id
    await query.edit_message_text(
        "💰 Введи новый пороговую цену (или 0, чтобы отключить уведомление):"
    )
    return THRESHOLD_INPUT


async def threshold_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        new_threshold = float(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ Введи число")
        return THRESHOLD_INPUT

    alert_id = context.user_data["threshold_alert_id"]
    telegram_id = update.effective_user.id
    user_id = await get_or_create_user(telegram_id)

    updated = await update_alert_threshold(alert_id, user_id, new_threshold)
    if not updated:
        await update.message.reply_text("❌ Не удалось обновить. Уведомление не найдено.")
        return ConversationHandler.END

    if new_threshold > 0:
        await update.message.reply_text(f"✅ Порог обновлён: {new_threshold:.0f}₽")
    else:
        await update.message.reply_text("✅ Уведомление отключено (порог = 0)")

    return ConversationHandler.END


def setup_handlers(application: Application):
    add_conv = ConversationHandler(
        entry_points=[CommandHandler("add", add_start)],
        states={
            ADD_URL: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_url)],
            ADD_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_name)],
            ADD_TARGET_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_target_price)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    history_conv = ConversationHandler(
        entry_points=[CommandHandler("history", history_start)],
        states={
            HISTORY_LINK: [CallbackQueryHandler(history_show, pattern=r"^hist_\d+$")],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    link_conv = ConversationHandler(
        entry_points=[CommandHandler("link", link_start)],
        states={
            LINK_SELECT: [CallbackQueryHandler(link_select_product, pattern=r"^linkprod_\d+$")],
            LINK_URL: [MessageHandler(filters.TEXT & ~filters.COMMAND, link_url)],
            LINK_TARGET_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, link_target_price)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    threshold_conv = ConversationHandler(
        entry_points=[CommandHandler("threshold", threshold_start)],
        states={
            THRESHOLD_SELECT: [CallbackQueryHandler(threshold_select, pattern=r"^thresh_\d+$")],
            THRESHOLD_INPUT: [MessageHandler(filters.TEXT & ~filters.COMMAND, threshold_input)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(add_conv)
    application.add_handler(link_conv)
    application.add_handler(threshold_conv)
    application.add_handler(CommandHandler("list", list_products))
    application.add_handler(CommandHandler("delete", delete_start))
    application.add_handler(history_conv)
    application.add_handler(CallbackQueryHandler(delete_confirm, pattern=r"^del_\d+$"))
    application.add_handler(CallbackQueryHandler(delete_execute, pattern=r"^confirm_del_\d+$"))
    application.add_handler(CallbackQueryHandler(cancel_delete, pattern=r"^cancel_del$"))
