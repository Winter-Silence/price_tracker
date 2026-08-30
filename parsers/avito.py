import asyncio
from dataclasses import dataclass

from parsers.base import BaseParser, SearchResult
from utils.logger import logger

AVITO_ROOT = "https://www.avito.ru"


@dataclass
class AvitoItem:
    url: str
    title: str
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

        seen.add(url);
        items.push({ url: url, title: title, price: price });
    }
    return items;
})()
"""


class AvitoParser(BaseParser):
    marketplace = "avito"
    delay_min: float = 6.0
    delay_max: float = 12.0
    _root_url: str = "https://www.avito.ru/"
    _captcha_detected: bool = False

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
        """
        await self._random_delay()
        page = await self._get_page(search_url)
        self._captcha_detected = False
        try:
            await asyncio.sleep(6)

            title = await self._eval(page, "document.title")
            if title and ("captcha" in title.lower() or "доступ ограничен" in title.lower()):
                self._captcha_detected = True
                logger.warning("Avito bot-block at %s", search_url)
                await self._take_screenshot(page, "blocked_search")
                return None

            content = await self._eval(page, "document.body.innerText")
            if content and "доступ ограничен" in content.lower():
                self._captcha_detected = True
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
                items.append(AvitoItem(url=r["url"], title=r["title"], price=price))

            self.register_parse_success()
            logger.info("Avito search: parsed %d items from %s", len(items), search_url)
            return items

        except Exception as exc:
            logger.error("Avito parser failed for %s: %s", search_url, exc)
            await self._take_screenshot(page, "error")
            self.register_parse_failure()
            return None
        finally:
            await self._close()
