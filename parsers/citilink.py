import re
import asyncio

from parsers.base import BaseParser
from utils.logger import logger


class CitilinkParser(BaseParser):
    marketplace = "citilink"
    _root_url: str = "https://www.citilink.ru/"

    @classmethod
    def can_handle(cls, url: str) -> bool:
        return "citilink.ru" in url

    async def get_price(self, url: str) -> float | None:
        await self._random_delay()
        page = await self._get_page(url)
        try:
            await asyncio.sleep(5)

            price_text = await page.evaluate('''
                (() => {
                    let el = document.querySelector('[data-meta="price"]');
                    if (el) return el.innerText;
                    el = document.querySelector('.product-price__value');
                    if (el) return el.innerText;
                    return null;
                })()
            ''')

            if not price_text:
                logger.warning("Price element not found at %s", url)
                return None

            price_clean = re.sub(r"[^\d]", "", price_text)
            if not price_clean:
                return None

            price = float(price_clean)
            logger.debug("Parsed price: %.2f from %s", price, url)
            return price

        except Exception as exc:
            logger.error("Citilink parser failed for %s: %s", url, exc)
            return None
        finally:
            await self._close()
