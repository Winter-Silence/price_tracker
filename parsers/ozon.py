import re
import asyncio

from parsers.base import BaseParser, SearchResult
from utils.logger import logger


class OzonParser(BaseParser):
    marketplace = "ozon"
    delay_min: float = 5.0
    delay_max: float = 12.0
    _root_url: str = "https://www.ozon.ru/"
    _captcha_detected: bool = False

    @classmethod
    def can_handle(cls, url: str) -> bool:
        return "ozon.ru" in url

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

            title = await self._eval(page, "document.title")
            if title and ("captcha" in title.lower() or "antibot" in title.lower()):
                logger.warning("Captcha detected at %s", url)
                await self._take_screenshot(page, "captcha")
                return None

            content = await self._eval(page, "document.body.innerText")
            if "нет соединения" in content.lower():
                logger.warning("Ozon 'no connection' page at %s", url)
                await self._take_screenshot(page, "no_connection")
                return None

            prices = await self._eval(page, '''
                (() => {
                    let result = { standard: null, card: null, premium: null };

                    function extractNumbers(text) {
                        if (!text) return null;
                        let m = text.match(/\\d[\\d\\u2009\\u00a0\\s]*\\d/);
                        return m ? m[0].replace(/[^\\d]/g, '') : null;
                    }

                    let priceWidget = document.querySelector('[data-widget="webPrice"]');
                    if (priceWidget) {
                        let text = priceWidget.innerText;
                        // Standard price is usually the first large price
                        let parts = text.split('\\n').filter(s => s.trim());
                        for (let p of parts) {
                            let nums = extractNumbers(p);
                            if (nums) {
                                if (!result.standard) result.standard = nums;
                                break;
                            }
                        }
                    }

                    // Try to find card price — often in a separate block with "Ozon Карта"
                    let allText = document.body.innerText;
                    let cardMatch = allText.match(/Ozon\\s*Карта[\\s\\S]*?(\\d[\\d\\u2009\\u00a0\\s]*\\d)/i);
                    if (cardMatch) {
                        result.card = extractNumbers(cardMatch[1]);
                    }

                    // Try to find premium/subscription price
                    let premiumEl = document.querySelector('[data-widget="webPrice"] [class*="premium"]')
                        || document.querySelector('[class*="premium"] [class*="price"]');
                    if (premiumEl) {
                        let p = extractNumbers(premiumEl.innerText);
                        if (p) result.premium = p;
                    }

                    // Fallback: look for any price-like patterns near "Premium" or "Подписка"
                    if (!result.premium) {
                        let pm = allText.match(/(?:Premium|подписк)[\\s\\S]{0,100}?(\\d[\\d\\u2009\\u00a0\\s]*\\d)/i);
                        if (pm) result.premium = extractNumbers(pm[1]);
                    }

                    return result;
                })()
            ''')

            result = {}
            if prices.get("standard"):
                p = float(prices["standard"])
                result["standard"] = p

            if prices.get("card"):
                p = float(prices["card"])
                result["card"] = p

            if prices.get("premium"):
                p = float(prices["premium"])
                result["premium"] = p

            if not result:
                logger.warning("No prices found at %s", url)
                await self._take_screenshot(page, "no_price")
                self.register_parse_failure()
                return None

            logger.debug("Parsed prices %s from %s", result, url)
            self.register_parse_success()
            return result

        except Exception as exc:
            logger.error("Ozon parser failed for %s: %s", url, exc)
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
        self._captcha_detected = False
        try:
            await asyncio.sleep(10)

            title = await self._eval(page, "document.title")
            if title and ("captcha" in title.lower() or "antibot" in title.lower()):
                logger.warning("Ozon captcha detected at %s", search_url)
                self._captcha_detected = True
                await self._take_screenshot(page, "captcha_search")
                return None

            content = await self._eval(page, "document.body.innerText")
            if content and "нет соединения" in content.lower():
                logger.warning("Ozon 'no connection' page at %s", search_url)
                await self._take_screenshot(page, "no_connection_search")
                return None

            cards = await self._eval(page, '''
                (() => {
                    function cleanNum(text) {
                        if (!text) return null;
                        let m = text.match(/\\d[\\d\\u2009\\u00a0\\s]*\\d/);
                        return m ? m[0].replace(/[^\\d]/g, '') : null;
                    }

                    const nodes = Array.from(
                        document.querySelectorAll('[data-testid="tile"], [class*="tile"], [data-widget="searchResultsV2"] a[href*="/product/"]')
                    );
                    const seen = new Set();
                    const results = [];

                    function pushCard(node, linkEl) {
                        if (!node || !linkEl) return;
                        const href = linkEl.getAttribute('href');
                        if (!href || seen.has(href)) return;

                        let title = linkEl.innerText ? linkEl.innerText.trim() : null;
                        if (!title) {
                            let tEl = node.querySelector('[class*="title"], h2, h3');
                            if (tEl) title = tEl.innerText.trim();
                        }

                        let priceEl = node.querySelector('[class*="price"] [style*="font-size"], [class*="price"] > span');
                        let price = priceEl ? priceEl.innerText.trim() : null;
                        if (!price) {
                            let m = node.innerText.match(/(\\d[\\d\\u2009\\u00a0\\s]*\\d)/);
                            if (m) price = m[0];
                        }

                        const num = cleanNum(price);
                        if (!title || !href || num === null) return;

                        seen.add(href);
                        const url = href.startsWith('http') ? href.split('?')[0] : ('https://www.ozon.ru' + href.split('?')[0]);
                        results.push({ title: title, price: parseFloat(num), url: url });
                    }

                    for (const n of nodes) {
                        if (n.tagName === 'A' && n.getAttribute('href') && n.getAttribute('href').includes('/product/')) {
                            pushCard(n.closest('[data-testid="tile"], [class*="tile"], div') || n, n);
                        } else {
                            const link = n.querySelector('a[href*="/product/"]') || n;
                            pushCard(n, link);
                        }
                    }
                    return results;
                })()
            ''')

            if not cards:
                logger.warning("Ozon search: no cards found at %s", search_url)
                await self._take_screenshot(page, "no_search_cards")
                return None

            from parsers.base import matches_title_filter
            matched = [c for c in cards if matches_title_filter(c["title"], title_filter)]
            if not matched:
                logger.info("Ozon search: 0 matched (filter=%s) from %d cards at %s",
                            title_filter, len(cards), search_url)
                return None

            matched.sort(key=lambda c: c["price"])
            best = matched[0]
            logger.info(
                "Ozon search: selected '%s' at %.2f (filter=%s; matched=%d/%d)",
                best["title"], best["price"], title_filter, len(matched), len(cards),
            )
            return SearchResult(
                price=best["price"],
                product_url=best["url"],
                product_title=best["title"],
            )

        except Exception as exc:
            logger.error("Ozon search parser failed for %s: %s", search_url, exc)
            await self._take_screenshot(page, "search_error")
            return None
        finally:
            await self._close()
