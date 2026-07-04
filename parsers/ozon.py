import re
import asyncio

from parsers.base import BaseParser
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
        await self._random_delay()
        page = await self._get_page(url)
        try:
            await asyncio.sleep(8)

            title = await page.evaluate("document.title")
            if "captcha" in title.lower() or "antibot" in title.lower():
                logger.warning("Captcha detected at %s", url)
                await self._take_screenshot(page, "captcha")
                return None

            content = await page.evaluate("document.body.innerText")
            if "нет соединения" in content.lower():
                logger.warning("Ozon 'no connection' page at %s", url)
                await self._take_screenshot(page, "no_connection")
                return None

            price_text = await page.evaluate('''
                (() => {
                    let el = document.querySelector('[data-widget="webPrice"]');
                    return el ? el.innerText : null;
                })()
            ''')

            if not price_text:
                logger.warning("Price element not found at %s", url)
                await self._take_screenshot(page, "no_price_element")
                return None

            price_clean = re.sub(r"[^\d.]", "", price_text.split("\n")[0].replace("\u2009", "").replace("\u00a0", ""))
            if not price_clean:
                logger.warning("Could not parse price from: %s", price_text)
                return None

            price = float(price_clean)
            logger.debug("Parsed price: %.2f from %s", price, url)
            return price

        except Exception as exc:
            logger.error("Ozon parser failed for %s: %s", url, exc)
            await self._take_screenshot(page, "error")
            return None
        finally:
            await self._close()
