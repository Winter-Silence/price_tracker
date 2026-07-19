import re
import asyncio

from parsers.base import BaseParser, SearchResult
from utils.logger import logger


def _clean_price(text: str) -> float | None:
    cleaned = re.sub(r"[^\d.]", "", text.replace("\u2009", "").replace("\u00a0", ""))
    if not cleaned:
        return None
    return float(cleaned)


class WildberriesParser(BaseParser):
    marketplace = "wildberries"
    delay_min: float = 2.0
    delay_max: float = 5.0
    _root_url: str = "https://www.wildberries.ru/"

    @classmethod
    def can_handle(cls, url: str) -> bool:
        return "wildberries.ru" in url or "wildberries.by" in url

    async def get_price(self, url: str) -> float | None:
        tiers = await self.get_price_tiers(url)
        if tiers is None:
            return None
        return tiers.get("standard")

    async def get_price_tiers(self, url: str) -> dict[str, float] | None:
        await self._random_delay()
        page = await self._get_page(url)
        try:
            await asyncio.sleep(8)

            prices = await self._eval(page, '''
                (() => {
                    function extract(sel) {
                        let el = document.querySelector(sel);
                        return el ? el.innerText.trim() : null;
                    }

                    let standard = extract('.price-block__final-price')
                        || extract('.product-page__price-wrap ins');

                    let card = extract('.price-block__card-price')
                        || extract('.price-block__wallet-price');

                    let club = extract('.price-block__club-price');

                    let fallback = null;
                    if (!standard) {
                        let m = document.body.innerText.match(/(\\d[\\d\\s]*\\d)\\s*₽/);
                        if (m) fallback = m[0];
                    }

                    return {
                        standard: standard || fallback,
                        card: card,
                        club: club
                    };
                })()
            ''')

            result = {}
            if prices.get("standard"):
                p = _clean_price(prices["standard"])
                if p is not None:
                    result["standard"] = p

            if prices.get("card"):
                p = _clean_price(prices["card"])
                if p is not None:
                    result["card"] = p

            if prices.get("club"):
                p = _clean_price(prices["club"])
                if p is not None:
                    result["wb_club"] = p

            if not result:
                logger.warning("No prices found at %s", url)
                await self._take_screenshot(page, "no_price")
                self.register_parse_failure()
                return None

            logger.debug("Parsed prices %s from %s", result, url)
            self.register_parse_success()
            return result

        except Exception as exc:
            logger.error("WB parser failed for %s: %s", url, exc)
            await self._take_screenshot(page, "error")
            self.register_parse_failure()
            return None
        finally:
            await self._close()

    async def get_cheapest_from_search(
        self, search_url: str, title_filter: str,
    ) -> SearchResult | None:
        await self._random_delay()
        page = await self._get_page(search_url)
        try:
            await asyncio.sleep(10)

            captcha_title = await self._eval(page, "document.title")
            if captcha_title and "captcha" in captcha_title.lower():
                logger.warning("WB captcha detected at %s", search_url)
                await self._take_screenshot(page, "captcha_search")
                return None

            cards = await self._eval(page, '''
                (() => {
                    function cleanNum(text) {
                        if (!text) return null;
                        let m = text.match(/\\d[\\d\\u2009\\u00a0\\s]*\\d/);
                        return m ? m[0].replace(/[^\\d]/g, '') : null;
                    }

                    const nodes = Array.from(
                        document.querySelectorAll('.product-card, [class*="product-card"], [data-card-id]')
                    );
                    const results = [];
                    for (const n of nodes) {
                        if (!n || !n.getBoundingClientRect || n.getBoundingClientRect().height === 0) continue;

                        let titleEl = n.querySelector('.product-card__name, [class*="goods-name"], .catalog-item__title');
                        let priceEl = n.querySelector('.price-block__final-price, .product-card__price, .price-block__wallet-price, .price-block__card-price');
                        let linkEl = n.querySelector('a.product-card__link, a[href*="/catalog/"], a[href*="/basket/"]');

                        let title = titleEl ? titleEl.innerText.trim() : null;
                        let price = priceEl ? priceEl.innerText.trim() : null;
                        let href = linkEl ? linkEl.getAttribute('href') : null;

                        if (!price) {
                            let m = n.innerText.match(/(\\d[\\d\\u2009\\u00a0\\s]*\\d)\\s*₽/);
                            if (m) price = m[0];
                        }

                        const num = cleanNum(price);
                        if (!title || !href || num === null) continue;

                        const url = href.startsWith('http') ? href.split('?')[0] : ('https://www.wildberries.ru' + href.split('?')[0]);
                        results.push({ title: title, price: parseFloat(num), url: url });
                    }
                    return results;
                })()
            ''')

            if not cards:
                logger.warning("WB search: no cards found at %s", search_url)
                await self._take_screenshot(page, "no_search_cards")
                self.register_parse_failure()
                return None

            from parsers.base import matches_title_filter
            matched = [c for c in cards if matches_title_filter(c["title"], title_filter)]
            if not matched:
                logger.info("WB search: 0 matched (filter=%s) from %d cards at %s",
                            title_filter, len(cards), search_url)
                return None

            matched.sort(key=lambda c: c["price"])
            best = matched[0]
            logger.info(
                "WB search: selected '%s' at %.2f (filter=%s; matched=%d/%d)",
                best["title"], best["price"], title_filter, len(matched), len(cards),
            )
            self.register_parse_success()
            return SearchResult(
                price=best["price"],
                product_url=best["url"],
                product_title=best["title"],
            )

        except Exception as exc:
            logger.error("WB search parser failed for %s: %s", search_url, exc)
            await self._take_screenshot(page, "search_error")
            self.register_parse_failure()
            return None
        finally:
            await self._close()
