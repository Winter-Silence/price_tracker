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
    get_user_privileges,
    add_user_privilege,
    remove_user_privilege,
    add_search_link,
    get_user_search_links,
    delete_search_link,
    get_search_link_by_id,
    get_search_price_history,
)
from parsers import get_parser, MARKETPLACE_TIERS
from parsers.base import TIER_LABELS
from utils.logger import logger

ADD_URL, ADD_MODE, ADD_NAME, ADD_TARGET_PRICE, ADD_TITLE_FILTER = range(5)
HISTORY_LINK, HISTORY_TIER = range(10, 12)
HISTORY_SEARCH_LINK = 13
LINK_SELECT, LINK_URL, LINK_TARGET_PRICE = range(20, 23)
THRESHOLD_SELECT, THRESHOLD_INPUT = range(30, 32)
PRIVILEGES_MENU, PRIVILEGES_MARKETPLACE = range(40, 42)
MARKETPLACE_NAMES = {
    "wildberries": "Wildberries",
    "ozon": "Ozon",
    "citilink": "Citilink",
}


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        await update.message.reply_text(
            "👋 Привет! Я бот для отслеживания цен на маркетплейсах.\n\n"
            "Команды:\n"
            "/add — добавить новый товар для отслеживания\n"
            "/link — привязать ссылку к существующему товару\n"
            "/threshold — изменить пороговую цену уведомления\n"
            "/privileges — настроить привилегии на маркетплейсах\n"
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
            "/add — добавить товар. Можно отслеживать конкретную\n"
            "    ссылку на товар или <b>страницу поиска</b> с сортировкой по\n"
            "    цене — бот найдёт самый дешёвый товар, подходящий под\n"
            "    поисковую строку.\n"
            "/link — привязать ссылку к уже существующему товару\n"
            "/threshold — изменить пороговую цену уведомления (или 0 — отключить)\n"
            "/privileges — указать какие привилегии у тебя есть на маркетплейсах (скидка по карте, подписка), чтобы бот учитывал их при расчёте цены\n"
            "/list — показать все твои товары с текущими ценами\n"
            "/delete — удалить товар (с подтверждением)\n"
            "/history — история цен товара\n\n"
            "Поддерживаемые магазины: Wildberries, Ozon, Citilink",
            parse_mode="HTML"
        )
    except Exception as exc:
        logger.error("Failed to send /help reply: %s", exc)


async def add_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🔗 Пришли ссылку на товар или на страницу поиска с сортировкой по цене "
        "(Wildberries, Ozon, Citilink)"
    )
    return ADD_URL


async def add_url(update: Update, context: ContextTypes.DEFAULT_TYPE):
    url = update.message.text.strip()
    parser = get_parser(url)
    if not parser:
        await update.message.reply_text("❌ Неподдерживаемый магазин. Поддерживаются: Wildberries, Ozon, Citilink")
        return ConversationHandler.END

    context.user_data["url"] = url
    context.user_data["marketplace"] = parser.marketplace

    keyboard = [
        [InlineKeyboardButton("📦 Конкретный товар", callback_data="addmode_product")],
        [InlineKeyboardButton("🔍 Поиск самого дешёвого", callback_data="addmode_search")],
    ]
    await update.message.reply_text(
        "📦 Это конкретный товар, или хочешь искать самый дешёвый "
        "среди похожих товаров на странице?",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return ADD_MODE


async def add_mode(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    mode = query.data.split("_", 1)[1]
    context.user_data["add_mode"] = mode

    if mode == "search":
        await query.edit_message_text(
            "🔍 Введи поисковую строку — ключевые слова, которые "
            "должны быть в названии товара (например, \"Sony WH-1000XM5\")"
        )
        return ADD_TITLE_FILTER
    else:
        await query.edit_message_text("📦 Введи название товара (для удобства)")
        return ADD_NAME


async def add_title_filter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    title_filter = update.message.text.strip()
    if not title_filter:
        await update.message.reply_text("❌ Введи непустую строку")
        return ADD_TITLE_FILTER
    context.user_data["title_filter"] = title_filter
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
    mode = context.user_data.get("add_mode", "product")
    telegram_id = update.effective_user.id

    user_id = await get_or_create_user(telegram_id)
    product_id = await add_product(name, user_id)

    if mode == "search":
        title_filter = context.user_data.get("title_filter", "")
        link_id = await add_search_link(product_id, marketplace, url, title_filter)
        link_kind = "search"
    else:
        link_id = await add_marketplace_link(product_id, marketplace, url)
        link_kind = "product"

    if target_price > 0:
        await add_alert(user_id, link_id, target_price, link_kind=link_kind)

    if mode == "search":
        await update.message.reply_text(
            "✅ Поиск добавлен! Буду проверять цены каждый час и "
            "найду самый дешёвый товар по запросу."
        )
    else:
        await update.message.reply_text("✅ Товар добавлен! Буду проверять цену каждый час.")
    return ConversationHandler.END


async def list_products(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    user_id = await get_or_create_user(telegram_id)
    products = await get_user_products(user_id)
    search_products = await get_user_search_links(user_id)
    privileges = await get_user_privileges(user_id)

    if not products and not search_products:
        await update.message.reply_text("📭 У тебя пока нет товаров. Добавь через /add")
        return

    priv_by_mp: dict[str, list[str]] = {}
    for p in privileges:
        priv_by_mp.setdefault(p["marketplace"], []).append(p["privilege_type"])

    text = "📦 <b>Твои товары:</b>\n\n"
    for p in products:
        marketplace = p["marketplace"]
        price = f"{p['last_price']:.0f}₽" if p["last_price"] else "—"

        line = f"🔹 {p['name']}\n   🏪 {marketplace} | Цена: {price}\n"

        user_tiers = priv_by_mp.get(marketplace, [])
        if user_tiers and p["last_price"]:
            tiers_text = []
            for tier in user_tiers:
                tiers_text.append(TIER_LABELS.get(tier, tier))
            line += f"   💳 Привилегии: {', '.join(tiers_text)}\n"

        line += f"   🔗 ID связи: {p['link_id']}\n\n"
        text += line

    if search_products:
        text += "\n🔍 <b>Поисковые запросы:</b>\n\n"
        for sp in search_products:
            marketplace = sp["marketplace"]
            price = f"{sp['last_price']:.0f}₽" if sp["last_price"] else "—"
            line = (
                f"🔹 {sp['name']}\n"
                f"   🏪 {marketplace} | Цена: {price}\n"
                f"   🔍 Запрос: \"{sp['title_filter']}\"\n"
            )
            if sp["last_resolved_title"]:
                line += f"   📦 Найден: {sp['last_resolved_title']}\n"
            line += f"   🔗 ID: {sp['search_link_id']}\n\n"
            text += line

    await update.message.reply_text(text, parse_mode="HTML")


async def delete_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    user_id = await get_or_create_user(telegram_id)
    products = await get_user_products(user_id)
    search_products = await get_user_search_links(user_id)

    if not products and not search_products:
        await update.message.reply_text("📭 Нечего удалять")
        return

    keyboard = [
        [InlineKeyboardButton(
            f"🗑 {p['name']}",
            callback_data=f"del_{p['link_id']}",
        )]
        for p in products
    ]
    for sp in search_products:
        keyboard.append([
            InlineKeyboardButton(
                f"🔍🗑 {sp['name']} ({sp['title_filter']})",
                callback_data=f"delsrch_{sp['search_link_id']}",
            )
        ])
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


async def delete_search_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    link_id = int(query.data.split("_")[1])

    keyboard = [
        [
            InlineKeyboardButton("✅ Да, удалить", callback_data=f"confirm_delsrch_{link_id}"),
            InlineKeyboardButton("❌ Отмена", callback_data="cancel_del"),
        ]
    ]
    await query.edit_message_text(
        "Точно удалить этот поисковый запрос?",
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


async def delete_search_execute(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    link_id = int(query.data.split("_")[2])
    telegram_id = update.effective_user.id

    user_id = await get_or_create_user(telegram_id)
    await delete_search_link(link_id, user_id)
    await query.edit_message_text("✅ Поисковый запрос удалён (деактивирован)")


async def cancel_delete(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("❌ Отменено")


async def history_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    user_id = await get_or_create_user(telegram_id)
    products = await get_user_products(user_id)
    search_products = await get_user_search_links(user_id)

    if not products and not search_products:
        await update.message.reply_text("📭 У тебя пока нет товаров")
        return ConversationHandler.END

    keyboard = [
        [InlineKeyboardButton(
            f"📊 {p['name']}",
            callback_data=f"hist_{p['link_id']}",
        )]
        for p in products
    ]
    for sp in search_products:
        keyboard.append([
            InlineKeyboardButton(
                f"🔍 {sp['name']} ({sp['title_filter']})",
                callback_data=f"histsrch_{sp['search_link_id']}",
            )
        ])
    await update.message.reply_text(
        "Выбери товар для просмотра истории цен:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return HISTORY_LINK


async def history_show(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    link_id = int(query.data.split("_")[1])
    context.user_data["history_link_id"] = link_id

    # Show tier selection
    keyboard = [
        [InlineKeyboardButton("💰 Все цены", callback_data="hist_tier_all")],
        [InlineKeyboardButton(f"📄 {TIER_LABELS.get('standard', 'Стандартная')}", callback_data="hist_tier_standard")],
        [InlineKeyboardButton(f"💳 {TIER_LABELS.get('card', 'По карте')}", callback_data="hist_tier_card")],
    ]

    records = await get_price_history(link_id, limit=5)
    text = "📊 <b>История цен:</b>\n\n"
    if records:
        for r in records:
            tier_label = TIER_LABELS.get(r["privilege_type"], r["privilege_type"])
            text += f"💰 {r['price']:.2f}₽ ({tier_label}) — {r['recorded_at']}\n"
    else:
        text += "Нет записей\n"

    text += "\nВыбери тип цены для просмотра:"
    await query.edit_message_text(
        text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return HISTORY_TIER


async def history_show_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    link_id = int(query.data.split("_")[1])
    context.user_data["history_search_link_id"] = link_id

    records = await get_search_price_history(link_id, limit=15)

    text = "📊 <b>История поиска (самый дешёвый найденный товар):</b>\n\n"
    if not records:
        text += "Нет записей"
    else:
        for r in records:
            line = f"💰 {r['price']:.2f}₽ — {r['recorded_at']}\n"
            if r["resolved_title"]:
                line += f"    📦 {r['resolved_title']}\n"
            if r["resolved_url"]:
                title = r["resolved_title"][:40] if r["resolved_title"] else "товар"
                line += f"    🔗 <a href='{r['resolved_url']}'>{title}</a>\n"
            text += line + "\n"

    keyboard = [[InlineKeyboardButton("🔙 Назад", callback_data="hist_search_back")]]
    await query.edit_message_text(
        text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard),
        disable_web_page_preview=True,
    )
    return HISTORY_SEARCH_LINK


async def history_search_back(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    telegram_id = update.effective_user.id
    user_id = await get_or_create_user(telegram_id)
    products = await get_user_products(user_id)
    search_products = await get_user_search_links(user_id)

    keyboard = [
        [InlineKeyboardButton(
            f"📊 {p['name']}",
            callback_data=f"hist_{p['link_id']}",
        )]
        for p in products
    ]
    for sp in search_products:
        keyboard.append([
            InlineKeyboardButton(
                f"🔍 {sp['name']} ({sp['title_filter']})",
                callback_data=f"histsrch_{sp['search_link_id']}",
            )
        ])
    await query.edit_message_text(
        "Выбери товар для просмотра истории цен:",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return HISTORY_LINK


async def history_show_tier(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    link_id = context.user_data.get("history_link_id")
    if not link_id:
        await query.edit_message_text("❌ Ошибка: товар не выбран")
        return ConversationHandler.END

    data = query.data
    if data.startswith("hist_tier_"):
        tier_type = data.split("_", 2)[2]
        if tier_type == "all":
            privilege_type = None
        else:
            privilege_type = tier_type
    else:
        privilege_type = None

    records = await get_price_history(link_id, privilege_type=privilege_type, limit=15)

    if privilege_type:
        tier_label = TIER_LABELS.get(privilege_type, privilege_type)
        text = f"📊 <b>История цен: {tier_label}</b>\n\n"
    else:
        text = "📊 <b>История цен (все)</b>\n\n"

    if not records:
        text += "Нет записей"
    else:
        for r in records:
            t_label = TIER_LABELS.get(r["privilege_type"], r["privilege_type"])
            text += f"💰 {r['price']:.2f}₽ ({t_label}) — {r['recorded_at']}\n"

    keyboard = [[InlineKeyboardButton("🔙 Назад к типам", callback_data="hist_back")]]
    await query.edit_message_text(
        text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return HISTORY_TIER


async def history_back(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Go back to tier selection in history."""
    query = update.callback_query
    await query.answer()
    link_id = context.user_data.get("history_link_id")

    keyboard = [
        [InlineKeyboardButton("💰 Все цены", callback_data="hist_tier_all")],
        [InlineKeyboardButton(f"📄 {TIER_LABELS.get('standard', 'Стандартная')}", callback_data="hist_tier_standard")],
        [InlineKeyboardButton(f"💳 {TIER_LABELS.get('card', 'По карте')}", callback_data="hist_tier_card")],
    ]

    records = await get_price_history(link_id, limit=5)
    text = "📊 <b>История цен:</b>\n\n"
    if records:
        for r in records:
            tier_label = TIER_LABELS.get(r["privilege_type"], r["privilege_type"])
            text += f"💰 {r['price']:.2f}₽ ({tier_label}) — {r['recorded_at']}\n"
    else:
        text += "Нет записей\n"

    text += "\nВыбери тип цены для просмотра:"
    await query.edit_message_text(
        text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return HISTORY_TIER


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


# ===== /privileges command =====


async def privileges_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    telegram_id = update.effective_user.id
    user_id = await get_or_create_user(telegram_id)
    privileges = await get_user_privileges(user_id)

    priv_by_mp: dict[str, list[str]] = {}
    for p in privileges:
        priv_by_mp.setdefault(p["marketplace"], []).append(p["privilege_type"])

    lines = ["<b>Твои привилегии:</b>\n"]
    for mp_name, mp_label in MARKETPLACE_NAMES.items():
        tiers = priv_by_mp.get(mp_name, [])
        if tiers:
            labels = [TIER_LABELS.get(t, t) for t in tiers]
            lines.append(f"  {mp_label}: {', '.join(labels)}")
        else:
            lines.append(f"  {mp_label}: не указаны")
    lines.append("\nВыбери маркетплейс для настройки:")

    keyboard = [
        [InlineKeyboardButton(
            f"{MARKETPLACE_NAMES[mp]}",
            callback_data=f"priv_mp_{mp}",
        )]
        for mp in MARKETPLACE_TIERS
    ]
    keyboard.append([InlineKeyboardButton("❌ Закрыть", callback_data="priv_close")])

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return PRIVILEGES_MENU


async def privileges_marketplace(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    marketplace = query.data.split("_")[2]
    context.user_data["priv_marketplace"] = marketplace

    telegram_id = update.effective_user.id
    user_id = await get_or_create_user(telegram_id)
    privileges = await get_user_privileges(user_id)
    active_tiers = {p["privilege_type"] for p in privileges if p["marketplace"] == marketplace}

    mp_label = MARKETPLACE_NAMES.get(marketplace, marketplace)
    lines = [f"<b>{mp_label}</b>\nВыбери привилегии:"]

    keyboard = []
    for tier in MARKETPLACE_TIERS.get(marketplace, []):
        label = TIER_LABELS.get(tier, tier)
        status = "✅" if tier in active_tiers else "⬜"
        keyboard.append([
            InlineKeyboardButton(
                f"{status} {label}",
                callback_data=f"priv_toggle_{tier}",
            )
        ])

    keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data="priv_back")])

    await query.edit_message_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return PRIVILEGES_MARKETPLACE


async def privileges_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    tier_type = query.data.split("_")[2]
    marketplace = context.user_data.get("priv_marketplace", "")

    telegram_id = update.effective_user.id
    user_id = await get_or_create_user(telegram_id)
    privileges = await get_user_privileges(user_id)
    active_tiers = {p["privilege_type"] for p in privileges if p["marketplace"] == marketplace}

    if tier_type in active_tiers:
        await remove_user_privilege(user_id, marketplace, tier_type)
    else:
        await add_user_privilege(user_id, marketplace, tier_type)

    # Re-fetch and re-render
    privileges = await get_user_privileges(user_id)
    active_tiers = {p["privilege_type"] for p in privileges if p["marketplace"] == marketplace}

    mp_label = MARKETPLACE_NAMES.get(marketplace, marketplace)
    lines = [f"<b>{mp_label}</b>\nВыбери привилегии:"]

    keyboard = []
    for tier in MARKETPLACE_TIERS.get(marketplace, []):
        label = TIER_LABELS.get(tier, tier)
        status = "✅" if tier in active_tiers else "⬜"
        keyboard.append([
            InlineKeyboardButton(
                f"{status} {label}",
                callback_data=f"priv_toggle_{tier}",
            )
        ])

    keyboard.append([InlineKeyboardButton("🔙 Назад", callback_data="priv_back")])

    await query.edit_message_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return PRIVILEGES_MARKETPLACE


async def privileges_back(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    telegram_id = update.effective_user.id
    user_id = await get_or_create_user(telegram_id)
    privileges = await get_user_privileges(user_id)

    priv_by_mp: dict[str, list[str]] = {}
    for p in privileges:
        priv_by_mp.setdefault(p["marketplace"], []).append(p["privilege_type"])

    lines = ["<b>Твои привилегии:</b>\n"]
    for mp_name, mp_label in MARKETPLACE_NAMES.items():
        tiers = priv_by_mp.get(mp_name, [])
        if tiers:
            labels = [TIER_LABELS.get(t, t) for t in tiers]
            lines.append(f"  {mp_label}: {', '.join(labels)}")
        else:
            lines.append(f"  {mp_label}: не указаны")
    lines.append("\nВыбери маркетплейс для настройки:")

    keyboard = [
        [InlineKeyboardButton(
            f"{MARKETPLACE_NAMES[mp]}",
            callback_data=f"priv_mp_{mp}",
        )]
        for mp in MARKETPLACE_TIERS
    ]
    keyboard.append([InlineKeyboardButton("❌ Закрыть", callback_data="priv_close")])

    await query.edit_message_text(
        "\n".join(lines),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )
    return PRIVILEGES_MENU


async def privileges_close(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text("✅ Настройки привилегий сохранены")
    return ConversationHandler.END


def setup_handlers(application: Application):
    add_conv = ConversationHandler(
        entry_points=[CommandHandler("add", add_start)],
        states={
            ADD_URL: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_url)],
            ADD_MODE: [CallbackQueryHandler(add_mode, pattern=r"^addmode_(product|search)$")],
            ADD_TITLE_FILTER: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_title_filter)],
            ADD_NAME: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_name)],
            ADD_TARGET_PRICE: [MessageHandler(filters.TEXT & ~filters.COMMAND, add_target_price)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    history_conv = ConversationHandler(
        entry_points=[CommandHandler("history", history_start)],
        states={
            HISTORY_LINK: [
                CallbackQueryHandler(history_show, pattern=r"^hist_\d+$"),
                CallbackQueryHandler(history_show_search, pattern=r"^histsrch_\d+$"),
            ],
            HISTORY_TIER: [
                CallbackQueryHandler(history_show_tier, pattern=r"^hist_tier_\w+$"),
                CallbackQueryHandler(history_back, pattern=r"^hist_back$"),
            ],
            HISTORY_SEARCH_LINK: [
                CallbackQueryHandler(history_search_back, pattern=r"^hist_search_back$"),
            ],
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

    privileges_conv = ConversationHandler(
        entry_points=[CommandHandler("privileges", privileges_start)],
        states={
            PRIVILEGES_MENU: [
                CallbackQueryHandler(privileges_marketplace, pattern=r"^priv_mp_\w+$"),
                CallbackQueryHandler(privileges_close, pattern=r"^priv_close$"),
            ],
            PRIVILEGES_MARKETPLACE: [
                CallbackQueryHandler(privileges_toggle, pattern=r"^priv_toggle_\w+$"),
                CallbackQueryHandler(privileges_back, pattern=r"^priv_back$"),
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(add_conv)
    application.add_handler(link_conv)
    application.add_handler(threshold_conv)
    application.add_handler(privileges_conv)
    application.add_handler(CommandHandler("list", list_products))
    application.add_handler(CommandHandler("delete", delete_start))
    application.add_handler(history_conv)
    application.add_handler(CallbackQueryHandler(delete_confirm, pattern=r"^del_\d+$"))
    application.add_handler(CallbackQueryHandler(delete_search_confirm, pattern=r"^delsrch_\d+$"))
    application.add_handler(CallbackQueryHandler(delete_execute, pattern=r"^confirm_del_\d+$"))
    application.add_handler(CallbackQueryHandler(delete_search_execute, pattern=r"^confirm_delsrch_\d+$"))
    application.add_handler(CallbackQueryHandler(cancel_delete, pattern=r"^cancel_del$"))
