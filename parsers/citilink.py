import re
import asyncio

from parsers.base import BaseParser, SearchResult
from utils.logger import logger


class CitilinkParser(BaseParser):
    marketplace = "citilink"
    _root_url: str = "https://www.citilink.ru/"

    @classmethod
    def can_handle(cls, url: str) -> bool:
        return "citilink.ru" in url

    async def get_price(self, url: str) -> float | None:
        tiers = await self.get_price_tiers(url)
        if tiers is None:
            return None
        return tiers.get("standard")

    async def get_price_tiers(self, url: str) -> dict[str, float] | None:
        await self._random_delay()
        page = await self._get_page(url)
        try:
            await asyncio.sleep(5)

            prices = await self._eval(page, '''
                (() => {
                    function cleanNum(text) {
                        if (!text) return null;
                        let m = text.match(/\\d[\\d\\s]*\\d/);
                        return m ? m[0].replace(/[^\\d]/g, '') : null;
                    }

                    let standard = null;
                    let el = document.querySelector('[data-meta="price"]');
                    if (el) standard = cleanNum(el.innerText);
                    if (!standard) {
                        el = document.querySelector('.product-price__value');
                        if (el) standard = cleanNum(el.innerText);
                    }

                    let card = null;
                    let cardEl = document.querySelector('.product-price__value_type_card')
                        || document.querySelector('[class*="card_price"]')
                        || document.querySelector('[class*="price-card"]');
                    if (cardEl) {
                        card = cleanNum(cardEl.innerText);
                    }

                    // Fallback: look for "по карте" in nearby text
                    if (!card) {
                        let allText = document.body.innerText;
                        let match = allText.match(/по\\s*карте[\\s\\S]{0,50}?(\\d[\\d\\s]*\\d)/i);
                        if (match) card = cleanNum(match[1]);
                    }

                    return { standard, card };
                })()
            ''')

            result = {}
            if prices.get("standard"):
                result["standard"] = float(prices["standard"])
            if prices.get("card"):
                result["card"] = float(prices["card"])

            if not result:
                logger.warning("No prices found at %s", url)
                return None

            logger.debug("Parsed prices %s from %s", result, url)
            return result

        except Exception as exc:
            logger.error("Citilink parser failed for %s: %s", url, exc)
            return None
        finally:
            await self._close()

    async def get_cheapest_from_search(
        self, search_url: str, title_filter: str,
    ) -> SearchResult | None:
        await self._random_delay()
        page = await self._get_page(search_url)
        try:
            await asyncio.sleep(7)

            cards = await self._eval(page, '''
                (() => {
                    function cleanNum(text) {
                        if (!text) return null;
                        let m = text.match(/\\d[\\d\\s]*\\d/);
                        return m ? m[0].replace(/[^\\d]/g, '') : null;
                    }

                    const nodes = Array.from(
                        document.querySelectorAll('.product_data, [data-product-id], .catalog-item, .ProductCard')
                    );
                    const results = [];
                    for (const n of nodes) {
                        let titleEl = n.querySelector('.ProductCardHeader_title, [class*="title"] a, [class*="title__"], .link_type_product');
                        let priceEl = n.querySelector('.product-price__value, [data-meta="price"], [class*="price__value"]');
                        let linkEl = n.querySelector('a.link_type_product, a[href*="/product/"], a[href*="/catalog/"]');

                        let title = titleEl ? titleEl.innerText.trim() : null;
                        let price = priceEl ? priceEl.innerText.trim() : null;
                        let href = linkEl ? linkEl.getAttribute('href') : null;

                        if (!title) {
                            let t = n.querySelector('a');
                            if (t) title = t.innerText.trim();
                        }
                        if (!href && n.tagName === 'A') href = n.getAttribute('href');
                        if (!price) {
                            let m = n.innerText.match(/(\\d[\\d\\s]*\\d)/);
                            if (m) price = m[0];
                        }

                        const num = cleanNum(price);
                        if (!title || !href || num === null) continue;

                        const url = href.startsWith('http') ? href.split('?')[0] : ('https://www.citilink.ru' + href.split('?')[0]);
                        results.push({ title: title, price: parseFloat(num), url: url });
                    }
                    return results;
                })()
            ''')

            if not cards:
                logger.warning("Citilink search: no cards found at %s", search_url)
                return None

            from parsers.base import matches_title_filter
            matched = [c for c in cards if matches_title_filter(c["title"], title_filter)]
            if not matched:
                logger.info("Citilink search: 0 matched (filter=%s) from %d cards at %s",
                            title_filter, len(cards), search_url)
                return None

            matched.sort(key=lambda c: c["price"])
            best = matched[0]
            logger.info(
                "Citilink search: selected '%s' at %.2f (filter=%s; matched=%d/%d)",
                best["title"], best["price"], title_filter, len(matched), len(cards),
            )
            return SearchResult(
                price=best["price"],
                product_url=best["url"],
                product_title=best["title"],
            )

        except Exception as exc:
            logger.error("Citilink search parser failed for %s: %s", search_url, exc)
            return None
        finally:
            await self._close()
