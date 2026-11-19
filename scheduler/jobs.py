import asyncio
import json
import os
import random
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
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
    mark_avito_deep_scan_done,
    set_avito_deep_scan_page,
    lot_id_from_url,
    get_avito_lot_check,
    save_avito_lot_check,
    record_avito_lot_error,
    get_pending_avito_llm_checks,
    update_avito_lot_check,
    mark_avito_item_notified,
)
from parsers import PARSERS
from parsers.base import matches_title_filter
from bot.notifications import (
    send_alert_notification,
    send_search_alert_notification,
    send_avito_new_item_notification,
    send_book_lot_notification,
    send_book_llm_status_notification,
)
from bot.admin_notify import send_admin_notification
from books.catalog import get_catalog
from books.filters import (
    is_fresh,
    is_wholesale_seller,
    parse_posting_date,
    parse_reviews_count,
)
from books.llm_verifier import (
    Candidate,
    LlmRateLimited,
    LlmUnavailable,
    verify_lot,
)
from books.matcher import (
    BookMatch,
    MatchReport,
    MEDIUM,
    STRONG,
    WEAK,
    match_text,
)
from books.prefilter import lot_title_interesting
from utils.chrome_cleanup import async_sweep_chrome_tree, profile_marker_set
from utils.logger import logger


def _get_poll_interval() -> int:
    return int(os.getenv("POLL_INTERVAL_MINUTES", "120"))


def _get_avito_poll_interval() -> int:
    return int(os.getenv("AVITO_POLL_INTERVAL_MINUTES", "5"))


AVITO_PROFILE_DEFAULTS = {
    False: "./chrome_profile_avito",
    True: "./chrome_profile_avito_books",
}


def _avito_profile_raw(*, books: bool) -> str:
    """Настроенный (как в .env) путь профиля Avito для режима."""
    key = "AVITO_BOOKS_PROFILE_DIR" if books else "AVITO_PROFILE_DIR"
    return os.getenv(key) or AVITO_PROFILE_DEFAULTS[books]


def _resolve_avito_profile_dir(*, books: bool) -> Path:
    """Resolve the trusted Chrome profile for one Avito mode."""
    return Path(_avito_profile_raw(books=books)).expanduser().resolve()


def _avito_profile_markers(profile_dir: Path) -> tuple[str, ...]:
    """Exact ``--user-data-dir`` path tokens that may identify this profile.

    Besides the absolute path we keep the configured relative token (the old
    bot version launched Chrome with e.g. ``chrome_profile_avito``), so
    leftover Chrome processes of the previous version are found and killed.
    """
    markers = set(profile_marker_set(profile_dir))
    for books in (False, True):
        raw = _avito_profile_raw(books=books)
        if Path(raw).expanduser().resolve() == profile_dir:
            markers.update((raw, os.path.normpath(raw)))
    return tuple(sorted(markers))


def _avito_profiles_conflict() -> bool:
    """Ценовой и книжный профили Avito совпадают — изоляция невозможна."""
    return (
        _resolve_avito_profile_dir(books=False)
        == _resolve_avito_profile_dir(books=True)
    )


def _get_avito_books_poll_interval() -> int:
    """Как часто обходить книжные ссылки, в минутах."""
    return max(1, int(os.getenv("AVITO_BOOKS_POLL_INTERVAL_MINUTES", "5")))


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
AVITO_BOOKS_MAX_CHECKS = int(os.getenv("AVITO_BOOKS_MAX_CHECKS", "8"))
AVITO_BOOKS_DRY_RUN = os.getenv(
    "AVITO_BOOKS_DRY_RUN", "0").strip().lower() in ("1", "true", "yes")


def _get_avito_books_max_pages() -> int:
    """Сколько страниц одной выдачи обходить за раз, пока не дойдём до своих."""
    return max(1, int(os.getenv("AVITO_BOOKS_MAX_PAGES", "10")))


def _get_avito_books_pages_per_cycle() -> int:
    """Лимит страниц на цикл, общий для всех книжных ссылок домена."""
    return max(1, int(os.getenv("AVITO_BOOKS_PAGES_PER_CYCLE", "4")))


def _get_avito_books_page_delay() -> tuple[float, float]:
    low = float(os.getenv("AVITO_BOOKS_PAGE_DELAY_MIN", "5"))
    high = float(os.getenv("AVITO_BOOKS_PAGE_DELAY_MAX", "7"))
    return low, max(low, high)


def _lot_key(url: str):
    """Ключ объявления: номер лота, иначе сам URL."""
    return lot_id_from_url(url) or url

# Задержка между открытием страниц Avito, чтобы не бить по сайту пачкой.
AVITO_PAGE_DELAY_SECONDS = 3.0

# Таймаут одного запроса к Avito. Зависший Chrome не должен держать профиль:
# за ним ждёт ценовой опрос /add.
AVITO_REQUEST_TIMEOUT_SECONDS = 75.0

# LLM-проверка спорных лотов: год или издательство не указаны, либо в
# каталоге несколько изданий одного названия. Сколько кандидатов из
# каталога показываем модели за один вызов.
AVITO_BOOKS_LLM_MAX_CANDIDATES = 3
# Пауза после исчерпания бесплатной квоты моделей (если модель не сказала,
# сколько ждать).
LLM_RATE_LIMIT_COOLDOWN_SECONDS = 30 * 60
# До какого момента (monotonic) не пытаемся снова звать модели.
_llm_rate_limited_until = 0.0


def _get_avito_books_llm_enabled() -> bool:
    """Включена ли LLM-проверка спорных лотов (по умолчанию — да)."""
    return os.getenv(
        "AVITO_BOOKS_LLM_ENABLED", "1").strip().lower() in ("1", "true", "yes")


def _get_avito_books_llm_batch() -> int:
    """Сколько спорных лотов проверять за один проход очереди."""
    return max(1, int(os.getenv("AVITO_BOOKS_LLM_BATCH", "3")))


def _get_avito_books_llm_interval() -> int:
    """Как часто разбирать очередь LLM-проверки, в минутах."""
    return max(1, int(os.getenv("AVITO_BOOKS_LLM_INTERVAL_MINUTES", "10")))


def _llm_queue_payload(item, details, candidates: list[BookMatch]) -> str:
    """Данные лота для очереди LLM: текст объявления и кандидаты каталога."""
    price = item.price if item.price is not None else details.price
    return json.dumps(
        {
            "title": item.title or details.title or "",
            "description": details.description or "",
            "price": price,
            "posted_at_text": details.posted_at_text or "",
            "reviews_text": details.reviews_text or "",
            "candidates": [
                m.book.index
                for m in candidates[:AVITO_BOOKS_LLM_MAX_CANDIDATES]
            ],
        },
        ensure_ascii=False,
    )


def _get_domain(url: str) -> str:
    try:
        return urlparse(url).netloc.lower()
    except Exception:
        return ""


class AvitoQuarantined(RuntimeError):
    """Профиль Avito попал в карантин: текущий проход по домену прерывается."""


class _AvitoBrowser:
    """One refcounted Chrome session for one absolute profile directory."""

    def __init__(
        self, profile_dir: Path, *, markers: tuple[str, ...] = (),
        on_discard=None,
    ) -> None:
        self.profile_dir = Path(profile_dir).expanduser().resolve()
        self.markers = tuple(markers) or _avito_profile_markers(self.profile_dir)
        self._lock = asyncio.Lock()
        self._lifecycle_lock = asyncio.Lock()
        self._users = 0
        self._parser = None
        self._background_tasks: set[asyncio.Task] = set()
        self._blocked = False
        self._quarantining = False
        self._discarded = False
        self._on_discard = on_discard

    @asynccontextmanager
    async def session(self, parser_cls):
        """Start one profile session and close it when the last user leaves."""
        parser = await self._acquire(parser_cls)
        try:
            yield parser
        finally:
            # Cleanup must survive task cancellation: otherwise the refcount
            # and a live Chrome on this profile would leak.
            await asyncio.shield(self._release())

    @asynccontextmanager
    async def price_pass(self):
        """Serialize price requests that use this profile."""
        await self._lock.acquire()
        try:
            yield
        finally:
            self._lock.release()

    async def books_call(self, fn, *args, **kwargs):
        """Run one bounded books request on this profile.

        A timeout quarantines this profile: its Chrome is killed, the browser
        is removed from the registry, and the current domain pass is aborted —
        the next cycle starts a fresh session.
        """
        return await self._bounded_call(
            fn, args, kwargs, label="книжный запрос Avito", acquire_lock=True)

    async def price_call(self, fn, *args, **kwargs):
        """Run one bounded price request (the caller already holds the lock)."""
        return await self._bounded_call(
            fn, args, kwargs, label="ценовой запрос Avito", acquire_lock=False)

    async def _bounded_call(self, fn, args, kwargs, *, label: str,
                            acquire_lock: bool):
        if acquire_lock:
            await self._lock.acquire()
        parser = self._parser
        try:
            return await self._await_bounded(fn(*args, **kwargs), label)
        except asyncio.TimeoutError as exc:
            logger.warning(
                "Avito: %s не уложился в %.0f с — карантин профиля %s",
                label, AVITO_REQUEST_TIMEOUT_SECONDS, self.profile_dir)
            await self._quarantine(parser)
            raise AvitoQuarantined(
                f"Avito profile {self.profile_dir} quarantined after {label}"
            ) from exc
        finally:
            if acquire_lock:
                self._lock.release()

    def mark_blocked(self) -> None:
        """Remember an Avito block until this profile is closed and reset."""
        self._blocked = True

    async def _await_bounded(self, awaitable, label: str):
        """Bound one operation and abandon a non-cooperative task on timeout."""
        task = asyncio.create_task(awaitable)
        try:
            done, _pending = await asyncio.wait(
                (task,), timeout=AVITO_REQUEST_TIMEOUT_SECONDS)
        except BaseException:
            task.cancel()
            self._background_tasks.add(task)
            task.add_done_callback(self._background_done)
            raise
        if not done:
            task.cancel()
            self._background_tasks.add(task)
            task.add_done_callback(self._background_done)
            logger.warning(
                "Avito: %s не уложился в %.0f с — отменяю",
                label, AVITO_REQUEST_TIMEOUT_SECONDS)
            raise asyncio.TimeoutError
        return task.result()

    def _background_done(self, task: asyncio.Task) -> None:
        self._background_tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            logger.warning("Avito: зависшая фоновая задача завершилась с ошибкой: %s", exc)

    async def _acquire(self, parser_cls):
        async with self._lifecycle_lock:
            if self._quarantining or self._discarded:
                raise RuntimeError(
                    f"Avito profile {self.profile_dir} is quarantined")
            self._users += 1
            if self._parser is not None:
                return self._parser
            parser = None
            try:
                parser = parser_cls(
                    profile_dir=self.profile_dir,
                    profile_markers=self.markers,
                )
                await self._await_bounded(
                    parser.start_session(), "запуск сессии Avito")
            except BaseException:
                self._users -= 1
                if parser is not None:
                    await asyncio.shield(self._quarantine(parser))
                raise
            self._parser = parser
            return self._parser

    async def _release(self) -> None:
        async with self._lifecycle_lock:
            self._users -= 1
            if self._users or self._parser is None:
                return
            parser, self._parser = self._parser, None
            await self._close(parser)

    async def _quarantine(self, parser) -> None:
        """Kill only this profile's Chrome and force a fresh session later."""
        self._quarantining = True
        if parser is not None:
            try:
                parser._session_quarantined = True
            except Exception:
                pass
        try:
            await self._cleanup_failed_start(parser)
        finally:
            if self._parser is parser:
                self._parser = None
            self._discarded = True
            self._quarantining = False
            if self._on_discard is not None:
                self._on_discard(self)

    async def _cleanup_failed_start(self, parser) -> None:
        """Sweep this profile without deleting its cookies or session."""
        if parser is not None:
            sweep = getattr(parser, "sweep_profile_chrome", None)
            if callable(sweep):
                try:
                    await sweep()
                except Exception as exc:
                    logger.error(
                        "Failed to sweep failed avito session %s: %s",
                        self.profile_dir, exc,
                    )
            else:
                await async_sweep_chrome_tree(self.profile_dir, self.markers)

            cleanup = getattr(parser, "close_browser", None)
            if cleanup is None:
                cleanup = getattr(parser, "end_session", None)
            if cleanup is not None:
                try:
                    await asyncio.wait_for(cleanup(), timeout=5.0)
                except asyncio.TimeoutError:
                    logger.error(
                        "Avito: cleanup after failed request timed out for %s",
                        self.profile_dir,
                    )
                except Exception as exc:
                    logger.error(
                        "Failed to clean up quarantined avito session: %s", exc)

    async def _close(self, parser) -> None:
        if self._blocked:
            # end_session() recreates only this profile after an Avito block.
            parser._profile_blocked = True
        try:
            await self._await_bounded(
                parser.end_session(), "закрытие сессии Avito")
        except asyncio.TimeoutError:
            logger.error("Avito: закрытие сессии не уложилось в таймаут")
            await self._quarantine(parser)
            return
        except Exception as exc:
            logger.error("Failed to end avito session: %s", exc)
            await self._quarantine(parser)
            return
        self._blocked = False


# Состояние ниже привязано к event loop: в проде loop один, в тестах каждый
# тест получает свой — там нужен свежий набор примитивов.
# shortcut: второй loop в одном процессе (только тесты) начинает с чистыми
# сессиями, живой Chrome из прошлого loop не переиспользуется.
_avito_browsers: dict[Path, _AvitoBrowser] = {}
_avito_browser_loop = None


def _discard_avito_browser(browser: _AvitoBrowser) -> None:
    current = _avito_browsers.get(browser.profile_dir)
    if current is browser:
        _avito_browsers.pop(browser.profile_dir, None)


def _get_avito_browser(profile_dir: Path | str | None = None) -> _AvitoBrowser:
    global _avito_browser_loop
    loop = asyncio.get_running_loop()
    if _avito_browser_loop is not loop:
        _avito_browsers.clear()
        _avito_browser_loop = loop
    resolved = (
        Path(profile_dir).expanduser().resolve()
        if profile_dir is not None
        else _resolve_avito_profile_dir(books=False)
    )
    browser = _avito_browsers.get(resolved)
    if browser is None or browser._discarded:
        browser = _AvitoBrowser(
            resolved, markers=_avito_profile_markers(resolved),
            on_discard=_discard_avito_browser)
        _avito_browsers[resolved] = browser
    return browser


# С какой книжной ссылки домена начинать обход: бюджет страниц/карточек общий,
# поэтому порядок ссылок крутится по кругу.
_books_rotation: dict[str, int] = {}


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


def _page_is_short(items: list, prev_count: int) -> bool:
    """Страница подозрительно короче предыдущей — похоже на недорендер."""
    return bool(items) and prev_count > 0 and len(items) * 2 < prev_count


# Статусы обхода страниц выдачи Avito.
SCAN_FRONTIER = "frontier"        # дошли до конца новых лотов
SCAN_BUDGET = "budget"            # исчерпан лимит страниц на цикл
SCAN_CAP = "cap"                  # дошли до предела глубины (10 страниц)
SCAN_INCOMPLETE = "incomplete"    # страница не разобралась или нестабильна

# О неполном покрытии выдачи админу сообщаем не чаще раза в 6 часов.
INCOMPLETE_NOTIFY_COOLDOWN = 6 * 3600
_incomplete_notified_at: dict[int, float] = {}


async def _notify_incomplete(search_link_id: int, search_url: str, reason: str) -> None:
    """Сообщить админу о неполном покрытии выдачи (не чаще раза в 6 ч)."""
    now = time.monotonic()
    last = _incomplete_notified_at.get(search_link_id)
    if last is not None and now - last < INCOMPLETE_NOTIFY_COOLDOWN:
        return
    _incomplete_notified_at[search_link_id] = now
    await send_admin_notification(
        "⚠️ <b>Avito: выдача книг просмотрена не полностью</b>\n\n"
        f"Ссылка: {search_url}\n{reason}"
    )


async def _walk_avito_pages(
    parser, link: dict, known_keys: set, page_budget: list[int], browser=None,
    *, covered_page: int = 1, coverage: bool = True,
) -> tuple[list | None, str, set[str], int, set[str]]:
    """Обойти страницы выдачи, пока не дойдём до уже известных лотов.

    known_keys — номера лотов, известные ДО обхода (снимок из БД).
    page_budget — общий на домен остаток страниц на цикл ([N]).
    covered_page — курсор глубины: сколько страниц ссылка уже смотрела.
    coverage=True — идёт первичное покрытие выдачи: после первой страницы
    идём с covered_page+1, не останавливаясь на странице из известных лотов.
    coverage=False — обычный режим: стр. 1 + обход вглубь до страницы,
    на которой все лоты уже известны.

    Возвращает (items, status, visited_urls, covered_after, deep_urls):
    items=None — выдачу не удалось разобрать; status — SCAN_*;
    visited_urls — URL реально открытых страниц;
    covered_after — новое значение курсора глубины;
    deep_urls — URL лотов со страниц глубже первой (историческая часть
    выдачи, её не нужно уведомлять при первом глубоком обходе).
    """
    search_url = link["search_url"]
    if browser is None:
        browser = _get_avito_browser(_resolve_avito_profile_dir(books=True))
    max_pages = _get_avito_books_max_pages()
    delay_low, delay_high = _get_avito_books_page_delay()
    items: list = []
    visited: set[str] = set()
    deep_urls: set[str] = set()
    seen_keys = set(known_keys)
    covered_after = max(1, int(covered_page or 1))
    prev_count = 0
    prev_short = False

    # Страница 1 — всегда: новые объявления появляются сверху. Дальше идём
    # вглубь: при первичном покрытии — с курсора, иначе — со страницы 2.
    tail_start = covered_after + 1 if coverage else 2
    pages = [1] + list(range(max(2, tail_start), max_pages + 1))

    for page_no in pages:
        if page_budget[0] <= 0:
            logger.info(
                "Avito books %s: лимит страниц на цикл исчерпан на стр. %d",
                search_url, page_no,
            )
            return items, SCAN_BUDGET, visited, covered_after, deep_urls
        page_budget[0] -= 1

        # Блокировка берётся на одну страницу: между страницами книжный обход
        # уступает ценовому режиму, а запрос ограничен таймаутом.
        page_items = await browser.books_call(
            parser.get_all_items_from_search, search_url, page=page_no)
        if page_items is None:
            if page_no == 1:
                return None, SCAN_INCOMPLETE, visited, covered_after, deep_urls
            logger.warning(
                "Avito books %s: стр. %d не разобралась — обход остановлен",
                search_url, page_no)
            return items, SCAN_INCOMPLETE, visited, covered_after, deep_urls

        short = _page_is_short(page_items, prev_count)
        if not page_items or short:
            first_urls = {it.url for it in page_items}
            await asyncio.sleep(random.uniform(delay_low, delay_high))
            await parser._random_delay()
            if page_budget[0] <= 0:
                logger.warning(
                    "Avito books %s: повтор стр. %d не выполнен — исчерпан "
                    "лимит запросов страниц", search_url, page_no)
                return items, SCAN_INCOMPLETE, visited, covered_after, deep_urls
            # Повтор — тоже реальный запрос, поэтому он расходует тот же лимит.
            page_budget[0] -= 1
            retry = await browser.books_call(
                parser.get_all_items_from_search, search_url, page=page_no)
            if not retry:
                if page_no == 1:
                    logger.warning(
                        "Avito books %s: первая страница пуста и после "
                        "повтора — обход прерван", search_url)
                    return None, SCAN_INCOMPLETE, visited, covered_after, deep_urls
                if prev_count > 0 and not prev_short:
                    # Предыдущая страница была полной, эта пуста даже после
                    # повтора — значит выдача просто закончилась.
                    logger.info(
                        "Avito books %s: выдача закончилась на стр. %d",
                        search_url, page_no)
                    return items, SCAN_FRONTIER, visited, covered_after, deep_urls
                logger.warning(
                    "Avito books %s: стр. %d пуста и после повтора — обход "
                    "остановлен, покрытие неполное", search_url, page_no)
                return items, SCAN_INCOMPLETE, visited, covered_after, deep_urls
            retry_short = _page_is_short(retry, prev_count)
            if retry_short and {it.url for it in retry} != first_urls:
                logger.warning(
                    "Avito books %s: стр. %d неполная и нестабильная — обход "
                    "остановлен, покрытие неполное", search_url, page_no)
                return items, SCAN_INCOMPLETE, visited, covered_after, deep_urls
            page_items = retry
            short = retry_short

        new_items = [
            it for it in page_items if _lot_key(it.url) not in seen_keys]
        for it in page_items:
            seen_keys.add(_lot_key(it.url))
            if it.url not in visited:
                visited.add(it.url)
                items.append(it)
                if page_no > 1:
                    deep_urls.add(it.url)
        prev_count = len(page_items)
        prev_short = short
        covered_after = max(covered_after, page_no)

        if page_no > 1 and not new_items:
            logger.info(
                "Avito books %s: на стр. %d только известные лоты — догнали "
                "свою выдачу", search_url, page_no)
            return items, SCAN_FRONTIER, visited, covered_after, deep_urls

        if page_no + 1 in pages:
            await asyncio.sleep(random.uniform(delay_low, delay_high))
            await parser._random_delay()

    return items, SCAN_CAP, visited, covered_after, deep_urls


async def _load_avito_listing(
    parser, link: dict, captcha_hit: bool, *,
    deep_scan: bool = False, page_budget: list[int] | None = None,
    browser=None,
):
    """Общая часть опроса: разобрать выдачу, синхронизировать БД.

    deep_scan — обходить страницы выдачи до уже известных лотов (книжный
    режим); page_budget — счётчик страниц на текущую ссылку [N].

    Возвращает (payload, captcha_hit). payload = None при ошибке/капче.
    payload["baseline"] = True: объявления запоминаются без уведомлений
    (первый опрос ссылки или первичное покрытие выдачи — базлайн).
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
        # Снимок известных лотов — ДО первого запроса, иначе новые лоты
        # текущего обхода попадут в «известные» и обход встанет на стр. 1.
        state_before = await get_avito_item_state(search_link_id)
        first_run = not state_before
        visited_urls: set[str] = set()
        scan_status = SCAN_FRONTIER
        coverage = deep_scan and not link.get("deep_scan_done")

        if deep_scan:
            covered_page = int(link.get("deep_scan_page") or 1)
            known_keys = set(state_before.keys())
            items, scan_status, visited_urls, covered_after, deep_urls = (
                await _walk_avito_pages(
                    parser, link, known_keys, page_budget or [0], browser,
                    covered_page=covered_page, coverage=coverage,
                )
            )
            if items is None:
                logger.warning("Avito parse returned nothing for %s", search_url)
                if hasattr(parser, "_captcha_detected") and parser._captcha_detected:
                    captcha_hit = True
                return None, captcha_hit
            if covered_after != covered_page:
                await set_avito_deep_scan_page(search_link_id, covered_after)
                link["deep_scan_page"] = covered_after
            if coverage and scan_status in (SCAN_FRONTIER, SCAN_CAP):
                # Первичное покрытие выдачи завершено — дальше работаем в
                # обычном режиме (стр. 1 + обход до конца новых лотов).
                await mark_avito_deep_scan_done(search_link_id)
                link["deep_scan_done"] = 1
                logger.info(
                    "Avito books %s: первичное покрытие выдачи завершено "
                    "(стр. %d, search_link_id=%d)",
                    search_url, covered_after, search_link_id,
                )
            if scan_status == SCAN_INCOMPLETE:
                await _notify_incomplete(
                    search_link_id, search_url,
                    "Обход остановился на недорендеренной странице.",
                )
            else:
                logger.debug(
                    "Avito books %s: обход завершён (%s), просмотрено %d лот(ов)",
                    search_url, scan_status, len(items),
                )
        else:
            # Ценовой запрос — тоже с таймаутом: зависший Chrome не должен
            # навсегда остановить ценовой опрос.
            if browser is None:
                browser = _get_avito_browser(
                    _resolve_avito_profile_dir(books=False))
            items = await browser.price_call(
                parser.get_all_items_from_search, search_url)
            if items is not None:
                visited_urls = {it.url for it in items}

        if items is None:
            logger.warning("Avito parse returned nothing for %s", search_url)
            if hasattr(parser, "_captcha_detected") and parser._captcha_detected:
                captcha_hit = True
            return None, captcha_hit

        synced = await sync_avito_items(search_link_id, items)

        if deep_scan:
            # В книжном режиме пропажу не фиксируем: обход страниц всегда
            # неполон (лимит цикла, кап глубины), и деактивация непросмотренных
            # лотов привела бы к тому, что они потом «вернутся» и принесут
            # повторные уведомления.
            logger.debug(
                "Avito books %s: пропажу не фиксирую (search_link_id=%d)",
                search_url, search_link_id,
            )
        else:
            # Защита от частично отрендеренной/урезанной выдачи: если
            # пришло меньше половины объявлений, чем отслеживается, пропажу
            # не фиксируем (иначе следующий цикл устроит шторм уведомлений).
            active_count = sum(
                1 for r in state_before.values() if r["is_active"])
            if len(items) * 2 < active_count:
                logger.warning(
                    "Avito %s: подозрительно мало объявлений (%d при %d "
                    "активных) — пропажу не фиксируем",
                    search_url, len(items), active_count,
                )
            else:
                deactivated = await mark_missing_avito_items(
                    search_link_id, visited_urls, AVITO_MISS_THRESHOLD,
                )
                if deactivated:
                    logger.info(
                        "Avito %s: %d объявлен(ий) пропало из выдачи "
                        "для search_link_id=%d",
                        search_url, deactivated, search_link_id,
                    )

        # Базлайн: первый опрос ссылки и первичное покрытие выдачи — без
        # уведомлений (coverage вычислен ДО обхода, до смены deep_scan_done).
        baseline = first_run or coverage

        if first_run:
            # Базлайн: запоминаем выдачу, никого не уведомляем.
            await mark_avito_items_notified(
                [r["id"] for r in synced.values()])
            logger.info(
                "Avito %s: первый опрос, запомнено %d объявлений без "
                "уведомлений (search_link_id=%d)",
                search_url, len(synced), search_link_id,
            )
        elif coverage:
            # Первичное покрытие: историческая часть выдачи (страницы глубже
            # первой) — базлайн, без уведомлений. Свежие лоты с первой
            # страницы остаются непомеченными и проверятся как обычно, когда
            # покрытие завершится.
            deep_ids = [
                synced[url]["id"] for url in deep_urls if url in synced
            ]
            if deep_ids:
                await mark_avito_items_notified(deep_ids)
            logger.info(
                "Avito %s: первичное покрытие выдачи, %d лот(ов) со страниц "
                "глубже первой запомнены без уведомлений (search_link_id=%d)",
                search_url, len(deep_ids), search_link_id,
            )

        return {
            "items": items,
            "state_before": state_before,
            "synced": synced,
            "first_run": first_run,
            "baseline": baseline,
        }, captcha_hit

    except AvitoQuarantined:
        # Профиль в карантине: текущий проход прерываем, следующий цикл
        # начнёт новую сессию.
        raise
    except Exception as exc:
        logger.error("Avito parser failed for %s: %s", search_url, exc)
        return None, captcha_hit


async def _poll_avito_link(
    parser, link: dict, captcha_hit: bool, browser=None,
) -> bool:
    """Обработать одну поисковую ссылку Avito.

    Возвращает обновлённый флаг captcha_hit.
    """
    prepared, captcha_hit = await _load_avito_listing(
        parser, link, captcha_hit, browser=browser)
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
            prev = state_before.get(_lot_key(it.url))
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
    page_budget: list[int], browser=None,
) -> bool:
    """Обработать книжную поисковую ссылку: открыть новые лоты и проверить.

    budget и page_budget — общие на домен остатки карточек и страниц.
    """
    if browser is None:
        browser = _get_avito_browser(_resolve_avito_profile_dir(books=True))
    prepared, captcha_hit = await _load_avito_listing(
        parser, link, captcha_hit, deep_scan=True, page_budget=page_budget,
        browser=browser)
    if prepared is None or prepared["baseline"]:
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

        # Ищем проверку по номеру лота: перевыложенный лот сменил slug,
        # но открывать карточку заново не нужно.
        check = await get_avito_lot_check(
            item_url=it.url, lot_id=lot_id_from_url(it.url))
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
        # Блокировка на одну карточку: между карточками книжный обход уступает,
        # а запрос ограничен таймаутом.
        details = await browser.books_call(
            parser.get_item_details, it.url)

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
            ambiguous = raw_report.ambiguous
            if ambiguous and _get_avito_books_llm_enabled():
                # Спорный лот: год/издательство не указаны или в каталоге
                # несколько изданий одного названия. Решает LLM — уведомление
                # придёт из очереди, а карточку заново не открываем.
                await save_avito_lot_check(
                    search_link_id, it.url, "llm_pending",
                    result_json=_llm_queue_payload(it, details, ambiguous),
                )
                await mark_done(it.url)
                logger.info(
                    "Avito books: лот %s ждёт LLM-проверки (%s)",
                    it.url,
                    ", ".join(
                        f"{m.book.index}:{m.book.title}"
                        for m in ambiguous[:AVITO_BOOKS_LLM_MAX_CANDIDATES]
                    ),
                )
                continue
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


async def _poll_price_links(
    parser, domain: str, links: list[dict], browser,
) -> None:
    """Ценовой проход домена: новые объявления для /add."""
    captcha_hit = False
    # Только ценовые запросы этого профиля сериализованы. Книжный режим
    # использует отдельный профиль и не ждёт эту блокировку.
    async with browser.price_pass():
        for link in links:
            captcha_hit = await _poll_avito_link(
                parser, link, captcha_hit, browser)
            await asyncio.sleep(AVITO_PAGE_DELAY_SECONDS)
            logger.debug("Пауза %.0fs между страницами Avito", AVITO_PAGE_DELAY_SECONDS)

        await asyncio.sleep(AVITO_PAGE_DELAY_SECONDS)
        logger.debug("Пауза %.0fs между доменами Avito", AVITO_PAGE_DELAY_SECONDS)


def _rotate_books_links(domain: str, books_links: list[dict]) -> list[dict]:
    """Круговой порядок книжных ссылок: бюджет домена достаётся не только первым."""
    start = _books_rotation.get(domain, 0) % len(books_links)
    _books_rotation[domain] = start + 1
    return books_links[start:] + books_links[:start]


async def _poll_books_links(
    parser, domain: str, links: list[dict], browser,
) -> None:
    """Книжный проход домена: обход выдачи и проверка карточек."""
    books_links = [link for link in links if link.get("track_mode") == "books"]
    if not books_links:
        return

    # Бюджеты общие на домен: неиспользованный остаток страниц/карточек
    # переходит следующей ссылке, а порядок ссылок крутится по кругу.
    books_budget = [AVITO_BOOKS_MAX_CHECKS]
    page_budget = [_get_avito_books_pages_per_cycle()]
    captcha_hit = False
    for link in _rotate_books_links(domain, books_links):
        captcha_hit = await _poll_books_link(
            parser, link, captcha_hit, books_budget, page_budget, browser)
        await asyncio.sleep(AVITO_PAGE_DELAY_SECONDS)
        logger.debug("Пауза %.0fs между страницами Avito", AVITO_PAGE_DELAY_SECONDS)

    await asyncio.sleep(AVITO_PAGE_DELAY_SECONDS)
    logger.debug("Пауза %.0fs между доменами Avito", AVITO_PAGE_DELAY_SECONDS)


async def _poll_avito_links(
    links: list[dict], handler, *, books: bool = False,
) -> bool:
    """Poll Avito domains with the trusted profile selected for this mode."""
    domain_links: dict[str, list[dict]] = defaultdict(list)
    for link in links:
        domain_links[_get_domain(link["search_url"])].append(link)

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

        profile_dir = _resolve_avito_profile_dir(books=books)
        browser = _get_avito_browser(profile_dir)
        try:
            async with browser.session(parser_cls) as parser:
                await handler(parser, domain, domain_link_list, browser)
                # Read the flag before end_session(): it resets the profile.
                if getattr(parser, "_profile_blocked", False):
                    browser.mark_blocked()
                    blocked = True
        except AvitoQuarantined as exc:
            # Профиль в карантине: остальные ссылки домена в этом цикле не
            # трогаем — следующий цикл начнёт новую сессию.
            logger.error(
                "Avito polling aborted for %s: %s", domain, exc)
        except Exception as exc:
            logger.error("Avito polling failed for %s: %s", domain, exc)

    return blocked


async def poll_avito_search():
    """Опрос ценовых ссылок Avito: новые объявления и уведомления.

    Avito search URLs are city/category listings where the same search query
    may gain new ads over time. Tracked items live in avito_search_items;
    a new ad is one with notified_at = NULL (first poll only records the
    current listing without notifying). An ad that disappeared from the
    listing for AVITO_MISS_THRESHOLD consecutive polls is deactivated, and
    when it reappears its notified_at is reset so the user is notified again —
    but only if the price actually dropped below the previous one.

    Книжные ссылки сюда не попадают: их обходит отдельная задача
    `poll_avito_books`, чтобы долгий /books не задерживал этот опрос.
    """
    logger.info("poll_avito_search started")
    links = [
        link for link in await get_active_avito_search_links()
        if link.get("track_mode") != "books"
    ]
    logger.info("poll_avito_search: got %d price avito links", len(links))
    if not links:
        return False
    return await _poll_avito_links(links, _poll_price_links, books=False)


async def poll_avito_books():
    """Книжный обход Avito — отдельная задача, не тормозящая ценовой опрос."""
    logger.info("poll_avito_books started")
    if _avito_profiles_conflict():
        logger.error(
            "Avito books disabled: AVITO_PROFILE_DIR and "
            "AVITO_BOOKS_PROFILE_DIR point to the same directory (%s)",
            _resolve_avito_profile_dir(books=True),
        )
        await send_admin_notification(
            "⚠️ <b>Avito: книжный режим отключён</b>\n\n"
            "AVITO_PROFILE_DIR и AVITO_BOOKS_PROFILE_DIR указывают на один "
            "каталог.\nУкажите разные каталоги, иначе профили не будут "
            "изолированы друг от друга."
        )
        return False
    links = [
        link for link in await get_active_avito_search_links()
        if link.get("track_mode") == "books"
    ]
    logger.info("poll_avito_books: got %d book avito links", len(links))
    if not links:
        return False
    return await _poll_avito_links(links, _poll_books_links, books=True)


async def poll_book_llm_queue() -> int:
    """Проверить спорные лоты через LLM и уведомить о подтверждённых.

    Очередь лежит в avito_lot_checks со статусом llm_pending. Вердикт даёт
    локальный OpenCode (цепочка kilo → nvidia → opencode, только бесплатные
    модели). При исчерпании квоты платить нельзя: ждём и сообщаем
    пользователю, какая модель исчерпала лимит.
    """
    global _llm_rate_limited_until

    if not _get_avito_books_llm_enabled():
        return 0
    if time.monotonic() < _llm_rate_limited_until:
        logger.debug("Avito books LLM: квота моделей исчерпана, ждём")
        return 0

    rows = await get_pending_avito_llm_checks(_get_avito_books_llm_batch())
    if not rows:
        return 0

    catalog = {book.index: book for book in get_catalog()}
    processed = 0

    for row in rows:
        try:
            payload = json.loads(row["result_json"] or "{}")
        except ValueError:
            payload = {}
        title = payload.get("title") or ""
        description = payload.get("description") or ""
        indices = [
            int(i) for i in payload.get("candidates", [])
            if isinstance(i, int) and i in catalog
        ][:AVITO_BOOKS_LLM_MAX_CANDIDATES]
        candidates = [Candidate.from_book(catalog[i]) for i in indices]

        if not candidates:
            logger.warning(
                "Avito books LLM: у лота %s нет кандидатов каталога",
                row["item_url"],
            )
            await update_avito_lot_check(
                row["id"], "skipped", skip_reason="llm_no_candidates")
            await mark_avito_item_notified(
                row["search_link_id"], row["item_url"], row["lot_id"])
            continue

        try:
            verdict = await verify_lot(candidates, title, description)
        except LlmRateLimited as exc:
            wait = exc.retry_after_s or LLM_RATE_LIMIT_COOLDOWN_SECONDS
            _llm_rate_limited_until = time.monotonic() + wait
            logger.warning(
                "Avito books LLM: лимит квоты %s/%s исчерпан, ждём %d с",
                exc.provider, exc.model, wait,
            )
            await send_book_llm_status_notification(
                row["user_id"],
                "⚠️ <b>Проверка книг через ИИ приостановлена</b>\n\n"
                f"Бесплатные модели ({exc.provider}/{exc.model}) исчерпали лимит.\n"
                "Покупать доступ мы не будем — вернёмся к проверке, когда "
                "лимит обновится.\n"
                "Найденные лоты не потеряны: они ждут в очереди.",
            )
            break
        except LlmUnavailable as exc:
            logger.warning("Avito books LLM: модели не ответили: %s", exc)
            attempts = (row["attempts"] or 0) + 1
            if attempts >= 3:
                await update_avito_lot_check(
                    row["id"], "skipped", skip_reason="llm_unavailable",
                    last_error=str(exc), bump_attempts=True)
                await mark_avito_item_notified(
                    row["search_link_id"], row["item_url"], row["lot_id"])
            else:
                await update_avito_lot_check(
                    row["id"], "llm_pending", last_error=str(exc),
                    bump_attempts=True)
            continue

        processed += 1
        verdict_json = json.dumps(
            {
                "verdict": verdict.verdict,
                "reason": verdict.reason,
                "books": [
                    {
                        "index": b.index,
                        "confidence": b.confidence,
                        "is_target_edition": b.is_target_edition,
                        "evidence": b.evidence,
                    }
                    for b in verdict.books
                ],
                "provider": verdict.provider,
                "model": verdict.model,
                "prompt_version": verdict.prompt_version,
            },
            ensure_ascii=False,
        )

        if verdict.verdict == "match":
            matches = []
            for book_verdict in verdict.books:
                if not book_verdict.is_target_edition:
                    continue
                book = catalog.get(book_verdict.index)
                if book is None:
                    continue
                confidence = {
                    "strong": STRONG, "medium": MEDIUM, "weak": WEAK,
                }.get(book_verdict.confidence, MEDIUM)
                evidence = tuple(book_verdict.evidence) or ("подтверждено ИИ",)
                matches.append(BookMatch(
                    book=book, confidence=confidence, evidence=evidence))

            if matches:
                report = MatchReport(matches=matches)
                item = SimpleNamespace(
                    url=row["item_url"], title=title, price=payload.get("price"))
                details = SimpleNamespace(
                    url=row["item_url"], title=title, description=description,
                    posted_at_text=payload.get("posted_at_text", ""),
                    reviews_text=payload.get("reviews_text", ""),
                    price=payload.get("price"),
                )
                sent = await send_book_lot_notification(
                    user_id=row["user_id"],
                    search_link_id=row["search_link_id"],
                    item=item,
                    details=details,
                    report=report,
                )
                if sent:
                    await update_avito_lot_check(
                        row["id"], "matched", confidence=report.confidence,
                        result_json=verdict_json)
                    await mark_avito_item_notified(
                        row["search_link_id"], row["item_url"], row["lot_id"])
                    logger.info(
                        "Avito books LLM: подтверждён лот %s (%s)",
                        row["item_url"],
                        ", ".join(m.book.title for m in matches),
                    )
                    continue
                # Уведомление не доставили — пробуем в следующем проходе.
                attempts = (row["attempts"] or 0) + 1
                if attempts >= 3:
                    await update_avito_lot_check(
                        row["id"], "error", skip_reason="llm_notify_failed",
                        last_error="уведомление не доставлено",
                        bump_attempts=True)
                    await mark_avito_item_notified(
                        row["search_link_id"], row["item_url"], row["lot_id"])
                else:
                    await update_avito_lot_check(
                        row["id"], "llm_pending",
                        last_error="уведомление не доставлено",
                        bump_attempts=True)
                continue

            # Совпадение есть, но целевое издание не подтверждено.
            await update_avito_lot_check(
                row["id"], "skipped", skip_reason="llm_reject",
                result_json=verdict_json)
            await mark_avito_item_notified(
                row["search_link_id"], row["item_url"], row["lot_id"])
            continue

        if verdict.verdict == "reject":
            logger.info("Avito books LLM: лот %s отклонён", row["item_url"])
            await update_avito_lot_check(
                row["id"], "skipped", skip_reason="llm_reject",
                result_json=verdict_json)
            await mark_avito_item_notified(
                row["search_link_id"], row["item_url"], row["lot_id"])
            continue

        # unclear: модель не смогла определить издание — пробуем позже.
        attempts = (row["attempts"] or 0) + 1
        if attempts >= 3:
            await update_avito_lot_check(
                row["id"], "skipped", skip_reason="llm_unclear",
                last_error=verdict.reason, result_json=verdict_json,
                bump_attempts=True)
            await mark_avito_item_notified(
                row["search_link_id"], row["item_url"], row["lot_id"])
        else:
            await update_avito_lot_check(
                row["id"], "llm_pending", last_error=verdict.reason,
                result_json=verdict_json, bump_attempts=True)

    return processed


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


async def _run_avito_books_periodic():
    """Книжный обход Avito отдельной задачей: свой интервал, свой бюджет."""
    interval = _get_avito_books_poll_interval()
    logger.info("Avito books polling started, interval %d minutes", interval)
    while True:
        start = asyncio.get_event_loop().time()
        try:
            blocked = await poll_avito_books()
            if blocked:
                # Пересоздаётся только книжный профиль в end_session().
                logger.warning(
                    "Avito books: заглушка «Доступ ограничен», книжный профиль "
                    "будет пересоздан")
        except Exception as exc:
            logger.error("avito books polling failed: %s", exc)
        elapsed = asyncio.get_event_loop().time() - start
        await asyncio.sleep(max(1, interval * 60 - elapsed))


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


async def _run_book_llm_periodic():
    """Разбор очереди спорных лотов — отдельной задачей, вне цикла опроса."""
    interval = _get_avito_books_llm_interval()
    logger.info("Avito books LLM queue started, interval %d minutes", interval)
    while True:
        try:
            await poll_book_llm_queue()
        except Exception as exc:
            logger.error("Avito books LLM queue failed: %s", exc)
        await asyncio.sleep(interval * 60)


def start_scheduler() -> list:
    interval = _get_poll_interval()
    task = asyncio.create_task(_run_periodic())
    avito_task = asyncio.create_task(_run_avito_periodic())
    llm_task = asyncio.create_task(_run_book_llm_periodic())
    tasks = [task, avito_task, llm_task]

    if _avito_profiles_conflict():
        logger.error(
            "Avito books task not started: AVITO_PROFILE_DIR and "
            "AVITO_BOOKS_PROFILE_DIR point to the same directory (%s)",
            _resolve_avito_profile_dir(books=True),
        )
        tasks.append(asyncio.create_task(send_admin_notification(
            "⚠️ <b>Avito: книжный режим отключён</b>\n\n"
            "AVITO_PROFILE_DIR и AVITO_BOOKS_PROFILE_DIR указывают на один "
            "каталог.\nУкажите разные каталоги, иначе профили не будут "
            "изолированы друг от друга."
        )))
    else:
        tasks.append(asyncio.create_task(_run_avito_books_periodic()))

    logger.info("Scheduler started with interval %d minutes", interval)
    return tasks
