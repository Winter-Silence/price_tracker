import asyncio
import os
import random
from collections import defaultdict
from urllib.parse import urlparse

from db.database import (
    get_active_links,
    save_price,
    get_triggered_alerts,
    mark_alert_triggered,
    reset_alerts_above_threshold,
)
from parsers import get_parser, PARSERS
from bot.notifications import send_alert_notification
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

            try:
                price = await parser.get_price(url)
                if price is None:
                    logger.warning("Failed to parse price for %s", url)
                    if hasattr(parser, "_captcha_detected") and parser._captcha_detected:
                        captcha_hit = True
                    continue

                await save_price(link_id, price)
                logger.info("Saved price %.2f for link_id=%d", price, link_id)

                await reset_alerts_above_threshold(link_id, price)

                triggered = await get_triggered_alerts(link_id, price)
                for alert in triggered:
                    await send_alert_notification(
                        alert["user_id"], link_id, price, alert["threshold_price"],
                    )
                    await mark_alert_triggered(alert["id"])

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


async def _run_periodic():
    interval = _get_poll_interval()
    logger.info("Price polling started, interval %d minutes", interval)
    while True:
        try:
            await poll_prices()
        except Exception as exc:
            logger.error("poll_prices failed: %s", exc)
        await asyncio.sleep(interval * 60)


def start_scheduler():
    interval = _get_poll_interval()
    task = asyncio.create_task(_run_periodic())
    logger.info("Scheduler started with interval %d minutes", interval)
    return task
