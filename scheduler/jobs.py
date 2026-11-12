import asyncio
import json
import os
import random
from collections import defaultdict
from urllib.parse import urlparse

from db.database import (
    get_active_links,
    save_price,
    get_user_privileges_for_marketplace,
    mark_product_triggered,
    reset_product_triggered,
    get_active_search_links_with_product,
    save_search_price,
    get_active_avito_search_links,
    get_avito_item_state,
    sync_avito_items,
    mark_missing_avito_items,
    mark_avito_items_notified,
    get_avito_lot_check,
    save_avito_lot_check,
    record_avito_lot_error,
)
from parsers import PARSERS
from parsers.base import matches_title_filter
from bot.notifications import (
    send_alert_notification,
    send_search_alert_notification,
    send_avito_new_item_notification,
    send_book_lot_notification,
)
from bot.admin_notify import send_admin_notification
from books.filters import (
    is_fresh,
    is_wholesale_seller,
    parse_posting_date,
    parse_reviews_count,
)
from books.matcher import match_text
from books.prefilter import lot_title_interesting
from utils.logger import logger


def _get_poll_interval() -> int:
    return int(os.getenv("POLL_INTERVAL_MINUTES", "120"))


def _get_avito_poll_interval() -> int:
    return int(os.getenv("AVITO_POLL_INTERVAL_MINUTES", "5"))


def _get_avito_block_retry_minutes() -> int:
    return max(1, int(os.getenv("AVITO_BLOCK_RETRY_MINUTES", "5")))


def _get_avito_block_max_attempts() -> int:
    return max(1, int(os.getenv("AVITO_BLOCK_MAX_ATTEMPTS", "3")))


def _avito_block_state(
    blocked: bool, attempts: int, max_attempts: int,
) -> tuple[int, bool]:
    """Обновить серию блокировок Avito; вернуть (счётчик, пора ли уведомлять)."""
    if not blocked:
        return 0, False
    attempts += 1
    return attempts, attempts == max_attempts


INTER_DOMAIN_DELAY: dict[str, tuple[float, float]] = {
    "ozon.ru": (15.0, 30.0),
}
DEFAULT_INTER_DOMAIN_DELAY = (2.0, 5.0)

# Сколько подряд опросов объявление должно отсутствовать в выдаче, чтобы
# считать его пропавшим (защита от флапа и частично отрендеренной страницы).
AVITO_MISS_THRESHOLD = 2

# Книжный режим: сколько карточек объявлений открывать за один цикл.
# Открытие карточки — дорогая операция (6–12 с), поэтому ограничиваем.
AVITO_BOOKS_MAX_CHECKS = int(os.getenv("AVITO_BOOKS_MAX_CHECKS", "15"))
AVITO_BOOKS_DRY_RUN = os.getenv(
    "AVITO_BOOKS_DRY_RUN", "0").strip().lower() in ("1", "true", "yes")

# Задержка между открытием страниц Avito, чтобы не бить по сайту пачкой.
AVITO_PAGE_DELAY_SECONDS = 3.0


def _get_domain(url: str) -> str:
    try:
        return urlparse(url).netloc.lower()
    except Exception:
        return ""


async def poll_prices():
    logger.info("poll_prices started")
    links = await get_active_links()

    # Avito не отслеживает цену конкретного товара (`get_price` у него всегда
    # None) — за ним следит poll_avito_search. Если оставить такую ссылку здесь,
    # джоба всё равно поднимет сессию на профиле chrome_profile_avito и
    # столкнётся с poll_avito_search на SingletonLock.
    links = [link for link in links if link.get("marketplace") != "avito"]
    logger.info("poll_prices: got %d active links", len(links))

    # Track latest effective price per link during this poll run.
    latest_price_by_link: dict[int, float] = {}

    domain_links: dict[str, list[dict]] = defaultdict(list)
    for link in links:
        domain = _get_domain(link["url"])
        domain_links[domain].append(link)

    for domain, domain_link_list in domain_links.items():
        sample_url = domain_link_list[0]["url"]
        parser_cls = None
        for p in PARSERS:
            if p.can_handle(sample_url):
                parser_cls = p
                break

        if parser_cls is None:
            logger.warning("No parser for domain: %s", domain)
            continue

        parser = parser_cls()

        needs_browser_session = hasattr(parser, "start_session") and callable(getattr(parser, "start_session"))

        if needs_browser_session:
            try:
                await parser.start_session()
            except Exception as exc:
                logger.error("Failed to start browser session for %s: %s", domain, exc)
                continue

        captcha_hit = False

        for link in domain_link_list:
            if captcha_hit and hasattr(parser, "_captcha_detected") and parser._captcha_detected:
                logger.warning(
                    "Skipping %s — captcha detected earlier for %s",
                    link["url"], domain,
                )
                continue

            link_id = link["id"]
            url = link["url"]
            marketplace = link["marketplace"]

            try:
                prices = await parser.get_price_tiers(url)
                if prices is None:
                    logger.warning("Failed to parse price for %s", url)
                    if hasattr(parser, "_captcha_detected") and parser._captcha_detected:
                        captcha_hit = True
                    continue

                for tier_type, tier_price in prices.items():
                    await save_price(link_id, tier_price, tier_type)
                logger.info("Saved prices %s for link_id=%d", prices, link_id)

                threshold_price = link.get("threshold_price") or 0
                alert_active = bool(link.get("alert_active"))
                if not alert_active or threshold_price <= 0:
                    continue

                user_id = link["user_id"]
                triggered_at = link.get("triggered_at")

                user_tiers = await get_user_privileges_for_marketplace(
                    user_id, marketplace,
                )

                effective_price = prices.get("standard")
                effective_tier = "standard"
                if effective_price is None and prices:
                    effective_price = min(prices.values())

                for tier in user_tiers:
                    if tier in prices and prices[tier] < effective_price:
                        effective_price = prices[tier]
                        effective_tier = tier

                latest_price_by_link[link_id] = effective_price

                last_price = link.get("last_price") or 0
                if last_price > 0 and effective_price < last_price * 0.5:
                    logger.warning(
                        "Suspicious price drop link_id=%d: %.2f -> %.2f (>50%%), skipping alert",
                        link_id, last_price, effective_price,
                    )
                    continue

                should_trigger = effective_price <= threshold_price
                product_id = link["product_id"]

                if should_trigger and triggered_at is None:
                    await send_alert_notification(
                        user_id, link_id,
                        effective_price, threshold_price,
                        effective_tier,
                    )
                    await mark_product_triggered(product_id)

            except Exception as exc:
                logger.error("Parser failed for %s: %s", url, exc)

            delay_range = INTER_DOMAIN_DELAY.get(domain, DEFAULT_INTER_DOMAIN_DELAY)
            delay = random.uniform(*delay_range)
            logger.debug("Inter-request delay %.1fs for %s", delay, domain)
            await asyncio.sleep(delay)

        if needs_browser_session:
            try:
                await parser.end_session()
            except Exception as exc:
                logger.error("Failed to end browser session for %s", domain, exc)

    # Re-evaluate triggers: a product is "out of trigger" only when every
    # polled link is above the threshold. This avoids flapping alerts when
    # one of several marketplaces briefly slips above the threshold while
    # another still holds below it.
    product_triggered: dict[int, bool] = {}
    for link in links:
        threshold_price = link.get("threshold_price") or 0
        if threshold_price <= 0 or not link.get("alert_active"):
            continue
        product_id = link["product_id"]
        link_id = link["id"]
        if link_id not in latest_price_by_link:
            continue
        below = latest_price_by_link[link_id] <= threshold_price
        product_triggered[product_id] = product_triggered.get(product_id, False) or below

    for product_id, any_below in product_triggered.items():
        if not any_below:
            await reset_product_triggered(product_id)


async def poll_search_prices():
    """Poll search pages and find the cheapest matching card.
    Saves the best price and triggers alerts exactly like poll_prices does for concrete products.
    Privileges (card/premium) are ignored for search results in this first iteration
    because the search listing usually exposes only a single (standard) price per card.
    """
    logger.info("poll_search_prices started")
    search_links = await get_active_search_links_with_product()

    # Ссылки Avito цену не ищут — за ними следит отдельная джоба
    # poll_avito_search. Если оставить их здесь, две джобы поднимут
    # AvitoParser одновременно на одном профиле chrome_profile_avito и
    # вторая сессия упадёт на SingletonLock ("Failed to connect to browser").
    search_links = [
        link for link in search_links
        if link.get("marketplace") != "avito"
    ]
    logger.info("poll_search_prices: got %d active search links", len(search_links))
    if not search_links:
        return

    # Track latest price per search link to reset product trigger at the end
    # (mirrors the product-links poller logic).
    latest_price_by_search_link: dict[int, float] = {}
    # Track which product had at least one search link below threshold this run.
    product_any_below: dict[int, bool] = {}

    domain_links: dict[str, list[dict]] = defaultdict(list)
    for link in search_links:
        domain = _get_domain(link["search_url"])
        domain_links[domain].append(link)

    for domain, domain_link_list in domain_links.items():
        sample_url = domain_link_list[0]["search_url"]
        parser_cls = None
        for p in PARSERS:
            if p.can_handle(sample_url):
                parser_cls = p
                break

        if parser_cls is None:
            logger.warning("No parser for search domain: %s", domain)
            continue

        parser = parser_cls()

        needs_browser_session = hasattr(parser, "start_session") and callable(getattr(parser, "start_session"))
        if needs_browser_session:
            try:
                await parser.start_session()
            except Exception as exc:
                logger.error("Failed to start browser session for search %s: %s", domain, exc)
                continue

        captcha_hit = False

        for link in domain_link_list:
            if captcha_hit and hasattr(parser, "_captcha_detected") and parser._captcha_detected:
                logger.warning(
                    "Skipping search %s — captcha detected earlier for %s",
                    link["search_url"], domain,
                )
                continue

            link_id = link["id"]
            search_url = link["search_url"]
            title_filter = link["title_filter"]
            marketplace = link["marketplace"]

            try:
                result = await parser.get_cheapest_from_search(search_url, title_filter)
                if result is None:
                    logger.warning(
                        "Search parse returned nothing for %s (filter=%s)",
                        search_url, title_filter,
                    )
                    if hasattr(parser, "_captcha_detected") and parser._captcha_detected:
                        captcha_hit = True
                    continue

                await save_search_price(
                    link_id, result.price, result.product_url, result.product_title,
                )
                logger.info(
                    "Saved search price %.2f for search_link_id=%d (%s)",
                    result.price, link_id, result.product_title,
                )

                threshold_price = link.get("threshold_price") or 0
                alert_active = bool(link.get("alert_active"))
                product_id = link["product_id"]
                user_id = link["user_id"]
                triggered_at = link.get("triggered_at")

                latest_price_by_search_link[link_id] = result.price

                if not alert_active or threshold_price <= 0:
                    continue

                effective_price = result.price
                effective_tier = "standard"

                should_trigger = effective_price <= threshold_price
                product_any_below[product_id] = (
                    product_any_below.get(product_id, False) or should_trigger
                )

                if should_trigger and triggered_at is None:
                    await send_search_alert_notification(
                        user_id, link_id,
                        effective_price, threshold_price,
                        result.product_url, result.product_title,
                        link.get("last_resolved_url"),
                        marketplace,
                        effective_tier,
                    )
                    await mark_product_triggered(product_id)

            except Exception as exc:
                logger.error("Search parser failed for %s: %s", search_url, exc)

            delay_range = INTER_DOMAIN_DELAY.get(domain, DEFAULT_INTER_DOMAIN_DELAY)
            delay = random.uniform(*delay_range)
            logger.debug("Inter-request delay %.1fs for search %s", delay, domain)
            await asyncio.sleep(delay)

        if needs_browser_session:
            try:
                await parser.end_session()
            except Exception as exc:
                logger.error("Failed to end browser session for search %s", domain, exc)

    # Reset triggers for products whose every polled search link is above
    # the threshold (mirrors the product-links reset logic).
    for link in search_links:
        threshold_price = link.get("threshold_price") or 0
        if threshold_price <= 0 or not link.get("alert_active"):
            continue
        product_id = link["product_id"]
        if product_id in product_any_below:
            continue
        link_id = link["id"]
        if link_id not in latest_price_by_search_link:
            continue
        if latest_price_by_search_link[link_id] > threshold_price:
            await reset_product_triggered(product_id)


async def _load_avito_listing(parser, link: dict, captcha_hit: bool):
    """Общая часть опроса: разобрать выдачу, синхронизировать БД.

    Возвращает (payload, captcha_hit). payload = None при ошибке/капче.
    При первом опросе payload["first_run"] = True, объявления уже помечены
    уведомлёнными, карточки открывать не нужно.
    """
    if captcha_hit and hasattr(parser, "_captcha_detected") and parser._captcha_detected:
        logger.warning(
            "Skipping avito %s — captcha detected earlier",
            link["search_url"],
        )
        return None, captcha_hit

    search_link_id = link["id"]
    search_url = link["search_url"]

    try:
        items = await parser.get_all_items_from_search(search_url)
        if items is None:
            logger.warning("Avito parse returned nothing for %s", search_url)
            if hasattr(parser, "_captcha_detected") and parser._captcha_detected:
                captcha_hit = True
            return None, captcha_hit

        state_before = await get_avito_item_state(search_link_id)
        first_run = not state_before
        current_urls = {it.url for it in items}

        synced = await sync_avito_items(search_link_id, items)

        # Защита от частично отрендеренной/урезанной выдачи: если
        # пришло вдвое меньше объявлений, чем отслеживается, пропажу
        # не фиксируем (иначе следующий цикл устроит шторм уведомлений).
        active_count = sum(1 for r in state_before.values() if r["is_active"])
        if len(items) * 2 < active_count:
            logger.warning(
                "Avito %s: подозрительно мало объявлений (%d при %d "
                "активных) — пропажу не фиксируем",
                search_url, len(items), active_count,
            )
        else:
            deactivated = await mark_missing_avito_items(
                search_link_id, current_urls, AVITO_MISS_THRESHOLD,
            )
            if deactivated:
                logger.info(
                    "Avito %s: %d объявлен(ий) пропало из выдачи "
                    "для search_link_id=%d",
                    search_url, deactivated, search_link_id,
                )

        if first_run:
            # Первый опрос — просто запоминаем текущую выдачу.
            await mark_avito_items_notified(
                [r["id"] for r in synced.values()])
            logger.info(
                "Avito %s: первый опрос, запомнено %d объявлений "
                "без уведомлений (search_link_id=%d)",
                search_url, len(synced), search_link_id,
            )

        return {
            "items": items,
            "state_before": state_before,
            "synced": synced,
            "first_run": first_run,
        }, captcha_hit

    except Exception as exc:
        logger.error("Avito parser failed for %s: %s", search_url, exc)
        return None, captcha_hit


async def _poll_avito_link(parser, link: dict, captcha_hit: bool) -> bool:
    """Обработать одну поисковую ссылку Avito.

    Возвращает обновлённый флаг captcha_hit.
    """
    prepared, captcha_hit = await _load_avito_listing(parser, link, captcha_hit)
    if prepared is None or prepared["first_run"]:
        return captcha_hit

    items = prepared["items"]
    state_before = prepared["state_before"]
    synced = prepared["synced"]
    search_link_id = link["id"]
    search_url = link["search_url"]
    user_id = link["user_id"]
    product_name = link["product_name"]

    try:
        title_filter = link.get("title_filter", "")
        candidates = [
            it for it in items
            if synced.get(it.url, {}).get("id") is not None
            and synced[it.url]["notified_at"] is None
            and matches_title_filter(it.title, title_filter)
        ]

        # Только свежие лоты: Avito держит в выдаче и старые/продлённые
        # объявления, а после долгого простоя снапшот устаревает — без этой
        # проверки в одно уведомление попадает вся страница.
        pending = []
        stale_ids: list[int] = []
        unknown_dates = 0
        for it in candidates:
            try:
                posted_at = parse_posting_date(it.posted_at_text)
            except ValueError:
                # Странный текст даты не должен ронять обработку всей ссылки.
                posted_at = None
            if posted_at is None:
                unknown_dates += 1
                if it.posted_at_text:
                    logger.debug(
                        "Avito %s: не распознана дата %r",
                        search_url, it.posted_at_text,
                    )
            if is_fresh(posted_at):
                pending.append(it)
            else:
                stale_ids.append(synced[it.url]["id"])

        if candidates and unknown_dates * 2 > len(candidates):
            logger.warning(
                "Avito %s: дата не распознана у %d из %d лотов — "
                "фильтр свежести может не работать (проверь "
                "[data-marker=item-date])",
                search_url, unknown_dates, len(candidates),
            )

        if stale_ids:
            await mark_avito_items_notified(stale_ids)
            logger.debug(
                "Avito %s: отсеяно %d несвежих объявлений для search_link_id=%d",
                search_url, len(stale_ids), search_link_id,
            )

        if not pending:
            logger.debug(
                "Avito %s: нет новых объявлений для search_link_id=%d",
                search_url, search_link_id,
            )
            return captcha_hit

        logger.info(
            "Avito %s: %d новых объявлен(ий) для search_link_id=%d",
            search_url, len(pending), search_link_id,
        )

        # Лот, пришедший в выдачу после пропажи, получил notified_at = NULL
        # (sync сбрасывает его у неактивных строк) и попал в pending. Учитываем
        # такие «вернувшиеся» объявления новыми только если цена реально упала
        # ниже прежней — иначе молчим и считаем уведомление обработанным.
        notifiable: list = []
        skipped_ids: list[int] = []
        for it in pending:
            prev = state_before.get(it.url)
            if prev is not None and prev.get("is_active") == 0:
                prev_price = prev.get("price") or 0
                if it.price is not None and prev_price > 0 and it.price < prev_price:
                    notifiable.append(it)
                else:
                    skipped_ids.append(synced[it.url]["id"])
            else:
                notifiable.append(it)

        if skipped_ids:
            await mark_avito_items_notified(skipped_ids)

        if not notifiable:
            return captcha_hit

        sent = await send_avito_new_item_notification(
            user_id=user_id,
            search_link_id=search_link_id,
            product_name=product_name,
            search_url=search_url,
            items=notifiable,
        )
        if sent:
            await mark_avito_items_notified(
                [synced[it.url]["id"] for it in notifiable])
        else:
            logger.warning(
                "Avito %s: уведомление не доставлено, повторим в "
                "следующем цикле (search_link_id=%d)",
                search_url, search_link_id,
            )
    except Exception as exc:
        logger.error("Avito parser failed for %s: %s", search_url, exc)

    return captcha_hit


async def _poll_books_link(
    parser, link: dict, captcha_hit: bool, budget: list[int],
) -> bool:
    """Обработать книжную поисковую ссылку: открыть новые лоты и проверить."""
    prepared, captcha_hit = await _load_avito_listing(parser, link, captcha_hit)
    if prepared is None or prepared["first_run"]:
        return captcha_hit

    items = prepared["items"]
    synced = prepared["synced"]
    search_link_id = link["id"]
    search_url = link["search_url"]
    user_id = link["user_id"]

    pending = [
        it for it in items
        if synced.get(it.url, {}).get("id") is not None
        and synced[it.url]["notified_at"] is None
    ]
    if not pending:
        logger.debug(
            "Avito books %s: нет новых лотов (search_link_id=%d)",
            search_url, search_link_id,
        )
        return captcha_hit

    async def mark_done(item_url: str) -> None:
        await mark_avito_items_notified([synced[item_url]["id"]])

    # Заголовочный фильтр: карточки открываем только у лотов, в заголовке
    # которых есть хоть один признак интересной книги. Иначе массовый
    # скрапинг карточек приводит к бану Avito.
    interesting: list = []
    gate_skipped = 0
    for it in pending:
        verdict, reason = lot_title_interesting(it.title)
        if verdict:
            logger.debug(
                "Avito books: заголовок интересен (%s) %s", reason, it.url)
            interesting.append(it)
            continue
        gate_skipped += 1
        logger.debug(
            "Avito books: в заголовке нет маркеров, пропускаю без открытия "
            "%s | %s", it.url, it.title,
        )
        await save_avito_lot_check(
            search_link_id, it.url, "skipped", skip_reason="title_gate")
        await mark_done(it.url)

    if gate_skipped:
        logger.info(
            "Avito books %s: отсеяно по заголовку %d лот(ов), "
            "к проверке %d (search_link_id=%d)",
            search_url, gate_skipped, len(interesting), search_link_id,
        )

    pending = interesting
    if not pending:
        return captcha_hit

    logger.info(
        "Avito books %s: %d новых лот(ов) на проверку (search_link_id=%d)",
        search_url, len(pending), search_link_id,
    )

    for it in pending:
        if budget[0] <= 0:
            logger.info(
                "Avito books: лимит проверок на цикл исчерпан, "
                "остальные лоты проверим в следующий раз",
            )
            break

        check = await get_avito_lot_check(it.url)
        if check is not None and check["status"] == "dry_run" and AVITO_BOOKS_DRY_RUN:
            # Уже проверяли в этом режиме — не тратим лимит и не шумим.
            continue
        if check is not None and (
            check["status"] in ("matched", "skipped")
            or (check["status"] == "error" and (check["attempts"] or 0) >= 3)
        ):
            logger.debug("Avito books: лот уже проверен, пропускаю %s", it.url)
            await mark_done(it.url)
            continue

        budget[0] -= 1
        details = await parser.get_item_details(it.url)

        if details is None:
            attempts = await record_avito_lot_error(
                search_link_id, it.url, "не удалось разобрать карточку")
            logger.warning(
                "Avito books: карточка не разобрана (%d/3) %s",
                attempts, it.url,
            )
            if attempts >= 3:
                # Лимит попыток исчерпан — снимаем лот с проверки сразу,
                # а не следующим циклом.
                await mark_done(it.url)
            if hasattr(parser, "_captcha_detected") and parser._captcha_detected:
                captcha_hit = True
                break
            continue

        reviews = parse_reviews_count(details.reviews_text)
        if is_wholesale_seller(reviews):
            logger.info(
                "Avito books: оптовый продавец (%s отзывов), пропускаю %s",
                reviews, it.url,
            )
            await save_avito_lot_check(
                search_link_id, it.url, "skipped", skip_reason="reviews")
            await mark_done(it.url)
            continue

        posted_at = parse_posting_date(details.posted_at_text)
        if not is_fresh(posted_at):
            logger.info(
                "Avito books: объявление старше 24 ч (%s), пропускаю %s",
                details.posted_at_text, it.url,
            )
            await save_avito_lot_check(
                search_link_id, it.url, "skipped", skip_reason="stale")
            await mark_done(it.url)
            continue

        text = f"{it.title}\n{details.description}"
        raw_report = match_text(text)
        # Совпадение «только по названию» (Робинзон Крузо без года и
        # издательства) — это, скорее всего, современное переиздание.
        # Уведомляем только при подтверждении: год, издательство/серия,
        # автор или примета издания.
        report = raw_report.confirmed()
        if report.is_empty:
            skip_reason = "no_match" if raw_report.is_empty else "no_evidence"
            logger.debug(
                "Avito books: совпадений с подтверждением нет (%s) %s",
                skip_reason, it.url,
            )
            await save_avito_lot_check(
                search_link_id, it.url, "skipped", skip_reason=skip_reason)
            await mark_done(it.url)
            continue

        match_titles = "; ".join(m.book.title for m in report.matches)
        logger.info(
            "Avito books: найдено %d книг(и) в %s: %s",
            len(report.matches), it.url, match_titles,
        )

        if AVITO_BOOKS_DRY_RUN:
            logger.info(
                "Avito books DRY-RUN: %s | matches=%s | confidence=%s "
                "(состояние БД не меняется)",
                it.url,
                [
                    {
                        "index": m.book.index,
                        "title": m.book.title,
                        "confidence": m.confidence,
                        "needs_year": m.needs_year,
                    }
                    for m in report.matches
                ],
                report.confidence,
            )
            # В dry-run не помечаем лот уведомлённым: после выключения режима
            # он должен быть обработан как новый, а не потерян.
            await save_avito_lot_check(
                search_link_id, it.url, "dry_run",
                confidence=report.confidence,
            )
            continue

        sent = await send_book_lot_notification(
            user_id=user_id,
            search_link_id=search_link_id,
            item=it,
            details=details,
            report=report,
        )
        if not sent:
            # Сбой доставки не должен крутить лот в pending бесконечно:
            # считаем попытки и после третьей сдаёмся.
            attempts = await record_avito_lot_error(
                search_link_id, it.url, "уведомление не доставлено")
            if attempts >= 3:
                logger.warning(
                    "Avito books: уведомление по %s не доставлено 3 раза, "
                    "снимаю лот с проверки", it.url)
                await mark_done(it.url)
            else:
                logger.warning(
                    "Avito books: уведомление не доставлено (%d/3), "
                    "повторю в следующем цикле %s", attempts, it.url,
                )
            continue

        result_json = json.dumps(
            [
                {
                    "index": m.book.index,
                    "title": m.book.title,
                    "confidence": m.confidence,
                    "needs_year": m.needs_year,
                }
                for m in report.matches
            ],
            ensure_ascii=False,
        )
        await save_avito_lot_check(
            search_link_id, it.url, "matched",
            confidence=report.confidence, result_json=result_json,
        )
        await mark_done(it.url)

    return captcha_hit


async def poll_avito_search():
    """Poll Avito search listings and notify the user about new ads.

    Avito search URLs are city/category listings where the same search query
    may gain new ads over time. Tracked items live in avito_search_items;
    a new ad is one with notified_at = NULL (first poll only records the
    current listing without notifying). An ad that disappeared from the
    listing for AVITO_MISS_THRESHOLD consecutive polls is deactivated, and
    when it reappears its notified_at is reset so the user is notified again —
    but only if the price actually dropped below the previous one.
    """
    logger.info("poll_avito_search started")
    search_links = await get_active_avito_search_links()
    logger.info("poll_avito_search: got %d active avito links", len(search_links))
    if not search_links:
        return False

    domain_links: dict[str, list[dict]] = defaultdict(list)
    for link in search_links:
        domain = _get_domain(link["search_url"])
        domain_links[domain].append(link)

    blocked = False
    for domain, domain_link_list in domain_links.items():
        sample_url = domain_link_list[0]["search_url"]
        parser_cls = None
        for p in PARSERS:
            if p.can_handle(sample_url):
                parser_cls = p
                break

        if parser_cls is None:
            logger.warning("No parser for avito domain: %s", domain)
            continue

        parser = parser_cls()

        needs_browser_session = hasattr(parser, "start_session") and callable(getattr(parser, "start_session"))

        if needs_browser_session:
            try:
                await parser.start_session()
            except Exception as exc:
                logger.error("Failed to start browser session for avito %s: %s", domain, exc)
                continue

        captcha_hit = False
        books_budget = [AVITO_BOOKS_MAX_CHECKS]

        for link in domain_link_list:
            if link.get("track_mode") == "books":
                captcha_hit = await _poll_books_link(
                    parser, link, captcha_hit, books_budget)
            else:
                captcha_hit = await _poll_avito_link(parser, link, captcha_hit)

            await asyncio.sleep(AVITO_PAGE_DELAY_SECONDS)
            logger.debug("Пауза %.0fs между страницами Avito", AVITO_PAGE_DELAY_SECONDS)

        await asyncio.sleep(AVITO_PAGE_DELAY_SECONDS)
        logger.debug("Пауза %.0fs между доменами Avito", AVITO_PAGE_DELAY_SECONDS)

        # Флаг нужно прочитать до end_session(): он сбрасывает его вместе с
        # пересозданием профиля.
        blocked = blocked or getattr(parser, "_profile_blocked", False)

        if needs_browser_session:
            try:
                await parser.end_session()
            except Exception as exc:
                logger.error("Failed to end browser session for avito %s: %s", domain, exc)

    return blocked


async def _run_avito_periodic():
    interval = _get_avito_poll_interval()
    retry_interval = _get_avito_block_retry_minutes()
    max_attempts = _get_avito_block_max_attempts()
    logger.info("Avito polling started, interval %d minutes", interval)
    block_attempts = 0
    while True:
        start = asyncio.get_event_loop().time()
        blocked = False
        try:
            blocked = await poll_avito_search()
        except Exception as exc:
            logger.error("avito polling failed: %s", exc)
        elapsed = asyncio.get_event_loop().time() - start

        block_attempts, notify = _avito_block_state(
            blocked, block_attempts, max_attempts)
        if notify:
            logger.error(
                "Avito blocked %d times in a row — notifying admin",
                block_attempts,
            )
            await send_admin_notification(
                "⚠️ <b>Avito блокирует запросы</b>\n\n"
                f"Заглушка «Доступ ограничен» получена {block_attempts} раз(а) подряд.\n"
                "Похоже, IP-адрес попал под антибот-защиту.\n"
                f"Следующая проверка через {retry_interval} мин."
            )

        wait_minutes = retry_interval if blocked else interval
        sleep_time = max(1, wait_minutes * 60 - elapsed)
        logger.info(
            "Avito poll cycle took %.1fs, %s, next in %.1fs",
            elapsed, "blocked — retry" if blocked else "ok", sleep_time,
        )
        await asyncio.sleep(sleep_time)


async def _run_periodic():
    interval = _get_poll_interval()
    logger.info("Price polling started, interval %d minutes", interval)
    while True:
        try:
            await poll_prices()
            await poll_search_prices()
        except Exception as exc:
            logger.error("polling failed: %s", exc)
        await asyncio.sleep(interval * 60)


def start_scheduler() -> list:
    interval = _get_poll_interval()
    task = asyncio.create_task(_run_periodic())
    avito_task = asyncio.create_task(_run_avito_periodic())
    logger.info("Scheduler started with interval %d minutes", interval)
    return [task, avito_task]
