import asyncio
import os
import random
from collections import defaultdict
from urllib.parse import urlparse

from db.database import (
    get_active_links,
    save_price,
    get_alerts_for_link,
    get_user_privileges_for_marketplace,
    mark_alert_triggered,
    reset_alert_triggered,
    get_active_search_links,
    save_search_price,
)
from parsers import get_parser, PARSERS
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

                alerts = await get_alerts_for_link(link_id)
                for alert in alerts:
                    user_tiers = await get_user_privileges_for_marketplace(
                        alert["user_id"], marketplace,
                    )

                    effective_price = prices.get("standard")
                    effective_tier = "standard"
                    if effective_price is None and prices:
                        effective_price = min(prices.values())

                    for tier in user_tiers:
                        if tier in prices and prices[tier] < effective_price:
                            effective_price = prices[tier]
                            effective_tier = tier

                    should_trigger = effective_price <= alert["threshold_price"]

                    if should_trigger and alert["triggered_at"] is None:
                        await send_alert_notification(
                            alert["user_id"], link_id,
                            effective_price, alert["threshold_price"],
                            effective_tier,
                        )
                        await mark_alert_triggered(alert["id"])
                    elif not should_trigger and alert["triggered_at"] is not None:
                        await reset_alert_triggered(alert["id"])

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
                logger.error("Failed to end browser session for %s: %s", domain, exc)


async def poll_search_prices():
    """Poll search pages and find the cheapest matching card.
    Saves the best price and triggers alerts exactly like poll_prices does for concrete products.
    Privileges (card/premium) are ignored for search results in this first iteration
    because the search listing usually exposes only a single (standard) price per card.
    """
    logger.info("poll_search_prices started")
    search_links = await get_active_search_links()
    logger.info("poll_search_prices: got %d active search links", len(search_links))
    if not search_links:
        return

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

                alerts = await get_alerts_for_link(link_id, link_kind="search")
                for alert in alerts:
                    effective_price = result.price
                    effective_tier = "standard"

                    should_trigger = effective_price <= alert["threshold_price"]

                    if should_trigger and alert["triggered_at"] is None:
                        await send_search_alert_notification(
                            alert["user_id"], link_id,
                            effective_price, alert["threshold_price"],
                            result.product_url, result.product_title,
                            link.get("last_resolved_url"),
                            marketplace,
                            effective_tier,
                        )
                        await mark_alert_triggered(alert["id"])
                    elif not should_trigger and alert["triggered_at"] is not None:
                        await reset_alert_triggered(alert["id"])

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
                logger.error("Failed to end browser session for search %s: %s", domain, exc)


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
