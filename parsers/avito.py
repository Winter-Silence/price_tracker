import asyncio
import re
from dataclasses import dataclass

from parsers.base import BaseParser, SearchResult
from utils.logger import logger

AVITO_ROOT = "https://www.avito.ru"


@dataclass
class AvitoItem:
    url: str
    title: str
    price: float | None
    posted_at_text: str = ""


@dataclass
class AvitoItemDetails:
    """Содержимое карточки объявления для книжного режима."""

    url: str
    title: str
    description: str
    posted_at_text: str
    reviews_text: str
    price: float | None


# JavaScript that walks the search-results listing and returns every ad card.
# Avito renders the listing server-side inside div[data-marker="catalog-serp"].
# Each ad is a div[data-marker="item"] with:
#   - a[data-marker="item-title"]  → title <h3> + href to the ad
#   - meta[itemprop="price"]       → price in `content`
# The canonical title link is a[data-marker="item-title"]; we only fall back to
# generic ad links (href containing "item_") when that marker is absent, and we
# deliberately ignore other <a> elements (photo grid, "Ещё фото", etc.).
_AVITO_ITEMS_JS = """
(() => {
    function cleanPrice(text) {
        if (!text) return null;
        const m = text.toString().replace(/\\s/g, '').match(/\\d+/);
        return m ? parseInt(m[0], 10) : null;
    }

    function absoluteUrl(href) {
        if (!href) return null;
        if (href.startsWith('http')) return href;
        return 'https://www.avito.ru' + href;
    }

    const seen = new Set();
    const items = [];

    let titleLinks = Array.from(
        document.querySelectorAll('a[data-marker="item-title"]')
    );

    // Fallback only when the canonical marker is missing entirely.
    if (titleLinks.length === 0) {
        titleLinks = Array.from(document.querySelectorAll('a[href*="item_"]'));
    }

    for (const link of titleLinks) {
        const href = link.getAttribute('href');
        if (!href) continue;
        if (!/^\\/[^/]+\\//.test(href) && !href.includes('/?')) continue;

        const url = absoluteUrl(href.split('?')[0]);
        if (!url || seen.has(url)) continue;

        let title = (link.getAttribute('title') || link.innerText || '').trim();
        if (!title) {
            const h = link.querySelector('h3');
            if (h) title = (h.innerText || '').trim();
        }
        if (!title) continue;

        let price = null;
        const card = link.closest('[data-marker="item"], [itemtype="http://schema.org/Product"]') || link;
        const priceEl = card.querySelector('meta[itemprop="price"]');
        if (priceEl) {
            price = cleanPrice(priceEl.getAttribute('content'));
        }
        if (price === null) {
            const priceDiv = card.querySelector('[data-marker="item-price"]');
            if (priceDiv) price = cleanPrice(priceDiv.innerText);
        }

        let dateText = '';
        const dateEl = card.querySelector('[data-marker="item-date"]');
        if (dateEl) {
            dateText = (dateEl.innerText || dateEl.textContent || '').trim();
        }

        seen.add(url);
        items.push({
            url: url, title: title, price: price, date_text: dateText,
        });
    }
    return items;
})()
"""


# Карточка объявления: описание, дата публикации и отзывы продавца.
# Часть полей Avito отдаёт только в JSON-LD (Product/datePosted), поэтому
# после DOM-селекторов пробуем schema.org-разметку.
_AVITO_ITEM_JS = """
(() => {
    const txt = (sel) => {
        const el = document.querySelector(sel);
        return el ? (el.innerText || el.textContent || '').trim() : '';
    };
    const attr = (sel, name) => {
        const el = document.querySelector(sel);
        return el ? (el.getAttribute(name) || '').trim() : '';
    };

    let title = txt('[data-marker="item-view/title-info"]') || txt('h1');
    let description = txt('[data-marker="item-view/item-description"]');
    let dateText = txt('[data-marker="item-view/item-date"]');
    // Счётчик отзывов: сейчас Avito держит его в rating-caption/rating
    // («6334 отзыва»), старый seller-info/reviews оставлен как fallback.
    let reviewsText = txt('[data-marker="rating-caption/rating"]')
        || txt('[data-marker="seller-info/reviews"]');
    let priceRaw = attr('meta[itemprop="price"]', 'content')
        || txt('[data-marker="item-view/item-price"]');

    if (!description || !dateText || !priceRaw) {
        const scripts = Array.from(
            document.querySelectorAll('script[type="application/ld+json"]')
        );
        for (const script of scripts) {
            try {
                const data = JSON.parse(script.textContent || '');
                const nodes = Array.isArray(data)
                    ? data
                    : (data['@graph'] || [data]);
                for (const node of nodes) {
                    if (!node || typeof node !== 'object') continue;
                    if (!description && typeof node.description === 'string') {
                        description = node.description.trim();
                    }
                    if (!dateText && typeof node.datePosted === 'string') {
                        dateText = node.datePosted.trim();
                    }
                    if (!title && typeof node.name === 'string') {
                        title = node.name.trim();
                    }
                    if (!priceRaw && node.offers) {
                        const offer = Array.isArray(node.offers)
                            ? node.offers[0]
                            : node.offers;
                        if (offer && offer.price != null) {
                            priceRaw = String(offer.price);
                        }
                    }
                }
            } catch (e) {
                // Битый JSON-LD — не повод падать, идём дальше.
            }
        }
    }

    if (!reviewsText) {
        // Запасной путь: ссылка «N отзывов» в блоке продавца.
        const link = document.querySelector('a[href="#open-reviews-list"]');
        if (link) {
            reviewsText = (link.innerText || link.textContent || '').trim();
        }
    }

    if (!reviewsText) {
        // Последний шанс: вытащить «N отзыв(ов)» из всего блока продавца.
        const seller = txt('[data-marker="item-view/seller-info"]');
        const match = seller.match(/\d[\d\s]*\s*отзыв/);
        if (match) {
            reviewsText = match[0].trim();
        }
    }

    return {
        title: title,
        description: description,
        date_text: dateText,
        reviews_text: reviewsText,
        price_text: priceRaw,
    };
})()
"""


class AvitoParser(BaseParser):
    marketplace = "avito"
    delay_min: float = 6.0
    delay_max: float = 12.0
    _root_url: str = "https://www.avito.ru/"
    _captcha_detected: bool = False
    # True, когда пришла заглушка «Доступ ограничен»: маркетплейс блокирует
    # сам профиль (cookies/сессию), и профиль надо пересоздать. Отличается от
    # капчи: капча может быть штатной проверкой и профиль не портит.
    _profile_blocked: bool = False

    @classmethod
    def can_handle(cls, url: str) -> bool:
        return "avito.ru" in url

    async def get_price(self, url: str) -> float | None:
        # Avito tracking is listing-level (new-item detection), not single-product price.
        return None

    async def get_price_tiers(self, url: str) -> dict[str, float] | None:
        return None

    async def get_cheapest_from_search(
        self, search_url: str, title_filter: str,
    ) -> SearchResult | None:
        # Not applicable for Avito new-item tracking; use get_all_items_from_search.
        return None

    async def get_all_items_from_search(self, search_url: str) -> list[AvitoItem] | None:
        """Return every ad currently present on the Avito search listing page.

        Returns None when the page could not be parsed (captcha / bot-block).

        На заглушке «Доступ ограничен» поднимается `_profile_blocked` — такой
        ответ означает, что маркетплейс заблокировал сам профиль (cookies), и
        `end_session()` пересоздаст его с нуля.
        """
        await self._random_delay()
        page = await self._get_page(search_url)
        self._captcha_detected = False
        self._profile_blocked = False
        try:
            await asyncio.sleep(6)

            title = await self._eval(page, "document.title")
            if title and ("captcha" in title.lower() or "доступ ограничен" in title.lower()):
                self._captcha_detected = True
                self._profile_blocked = "доступ ограничен" in title.lower()
                logger.warning("Avito bot-block at %s", search_url)
                await self._take_screenshot(page, "blocked_search")
                return None

            content = await self._eval(page, "document.body.innerText")
            if content and "доступ ограничен" in content.lower():
                self._captcha_detected = True
                self._profile_blocked = True
                logger.warning("Avito bot-block text at %s", search_url)
                await self._take_screenshot(page, "blocked_search")
                return None

            raw = await self._eval(page, _AVITO_ITEMS_JS)
            if not raw:
                logger.warning("Avito search: no items found at %s", search_url)
                await self._take_screenshot(page, "no_search_items")
                self.register_parse_failure()
                return []

            items = []
            for r in raw:
                price = float(r["price"]) if isinstance(r.get("price"), (int, float)) and r["price"] else None
                items.append(AvitoItem(
                    url=r["url"],
                    title=r["title"],
                    price=price,
                    posted_at_text=(r.get("date_text") or "").strip(),
                ))

            self.register_parse_success()
            logger.info("Avito search: parsed %d items from %s", len(items), search_url)
            return items

        except Exception as exc:
            logger.error("Avito parser failed for %s: %s", search_url, exc)
            await self._take_screenshot(page, "error")
            self.register_parse_failure()
            return None

    async def get_item_details(self, url: str) -> AvitoItemDetails | None:
        """Открыть карточку объявления и вернуть описание, дату и отзывы.

        Возвращает None при капче/блокировке и при ошибке разбора. Карточки
        открываются по одной в той же вкладке — список выдачи к этому моменту
        уже разобран и хранится в памяти вызывающего кода.
        """
        await self._random_delay()
        page = await self._get_page(url)
        self._captcha_detected = False
        self._profile_blocked = False
        try:
            await asyncio.sleep(3)

            title = await self._eval(page, "document.title")
            if title and ("captcha" in title.lower() or "доступ ограничен" in title.lower()):
                self._captcha_detected = True
                self._profile_blocked = "доступ ограничен" in title.lower()
                logger.warning("Avito bot-block at item %s", url)
                await self._take_screenshot(page, "blocked_item")
                return None

            content = await self._eval(page, "document.body.innerText")
            if content and "доступ ограничен" in content.lower():
                self._captcha_detected = True
                self._profile_blocked = True
                logger.warning("Avito bot-block text at item %s", url)
                await self._take_screenshot(page, "blocked_item")
                return None

            raw = await self._eval(page, _AVITO_ITEM_JS)
            if not raw or not (
                raw.get("description") or raw.get("date_text") or raw.get("title")
            ):
                logger.warning("Avito item: no details at %s", url)
                await self._take_screenshot(page, "no_item_details")
                self.register_parse_failure()
                return None

            price = None
            price_text = raw.get("price_text") or ""
            digits = re.sub(r"\D", "", price_text)
            if digits:
                price = float(digits)

            details = AvitoItemDetails(
                url=url,
                title=(raw.get("title") or "").strip(),
                description=(raw.get("description") or "").strip(),
                posted_at_text=(raw.get("date_text") or "").strip(),
                reviews_text=(raw.get("reviews_text") or "").strip(),
                price=price,
            )
            self.register_parse_success()
            logger.debug(
                "Avito item details: %s (date=%r, reviews=%r)",
                url, details.posted_at_text, details.reviews_text,
            )
            return details

        except Exception as exc:
            logger.error("Avito item parser failed for %s: %s", url, exc)
            await self._take_screenshot(page, "item_error")
            self.register_parse_failure()
            return None
