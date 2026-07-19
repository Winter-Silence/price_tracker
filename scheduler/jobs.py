import asyncio
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
)
from parsers import PARSERS
from bot.notifications import send_alert_notification, send_search_alert_notification
from utils.logger import logger


def _get_poll_interval() -> int:
    return int(os.getenv("POLL_INTERVAL_MINUTES", "60"))


INTER_DOMAIN_DELAY: dict[str, tuple[float, float]] = {
    "ozon.ru": (15.0, 30.0),
}
DEFAULT_INTER_DOMAIN_DELAY = (2.0, 5.0)


def _get_domain(url: str) -> str:
    try:
        return urlparse(url).netloc.lower()
    except Exception:
        return ""


async def poll_prices():
    logger.info("poll_prices started")
    links = await get_active_links()
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


def start_scheduler():
    interval = _get_poll_interval()
    task = asyncio.create_task(_run_periodic())
    logger.info("Scheduler started with interval %d minutes", interval)
    return task
