import re
import asyncio

from parsers.base import BaseParser
from utils.logger import logger


class WildberriesParser(BaseParser):
    marketplace = "wildberries"
    delay_min: float = 2.0
    delay_max: float = 5.0
    _root_url: str = "https://www.wildberries.ru/"

    @classmethod
    def can_handle(cls, url: str) -> bool:
        return "wildberries.ru" in url or "wildberries.by" in url

    async def get_price(self, url: str) -> float | None:
        await self._random_delay()
        page = await self._get_page(url)
        try:
            await asyncio.sleep(8)

            price_text = await page.evaluate('''
                (() => {
                    let el = document.querySelector('.price-block__final-price');
                    if (el) return el.innerText;
                    let ins = document.querySelector('.product-page__price-wrap ins');
                    if (ins) return ins.innerText;
                    let match = document.body.innerText.match(/(\\d[\\d\\s]*\\d)\\s*₽/);
                    return match ? match[0] : null;
                })()
            ''')

            if not price_text:
                logger.warning("Price element not found at %s", url)
                await self._take_screenshot(page, "no_price_element")
                return None

            price_clean = re.sub(r"[^\d.]", "", price_text.replace("\u2009", "").replace("\u00a0", ""))
            if not price_clean:
                logger.warning("Could not parse price from: %s", price_text)
                return None

            price = float(price_clean)
            logger.debug("Parsed price: %.2f from %s", price, url)
            return price

        except Exception as exc:
            logger.error("WB parser failed for %s: %s", url, exc)
            await self._take_screenshot(page, "error")
            return None
        finally:
            await self._close()
