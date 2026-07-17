import random
import asyncio
import shutil
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import nodriver as uc

from utils.logger import logger

# nodriver adds many suspicious Chrome flags by default that trigger
# Ozon's WAF (--remote-allow-origins=*, --disable-infobars, etc).
# Override with only essential flags.
uc.Config._default_browser_args = [
    "--no-first-run",
]

SCREENSHOTS_DIR = Path("screenshots")

REAL_CHROME = shutil.which("google-chrome-stable") or shutil.which("google-chrome") or "/opt/google/chrome/google-chrome"

CHROME_ARGS = [
    "--window-size=1920,1080",
]


TIER_LABELS: dict[str, str] = {
    "standard": "Стандартная",
    "card": "По карте",
    "premium": "По подписке",
    "wb_club": "WB Клуб",
}


@dataclass
class SearchResult:
    price: float
    product_url: str
    product_title: str
    tiers: dict[str, float] | None = field(default=None)


def matches_title_filter(card_title: str, title_filter: str) -> bool:
    """
    Token containment check (case-insensitive, words order agnostic):
    every non-empty token of title_filter must appear as a substring
    inside card_title.
    """
    if not title_filter or not title_filter.strip():
        return True
    title_lower = card_title.lower()
    tokens = [t for t in title_filter.lower().split() if t]
    return all(tok and tok in title_lower for tok in tokens)


class BaseParser(ABC):
    marketplace: str = ""
    delay_min: float = 1.0
    delay_max: float = 3.0
    homepage_delay_min: float = 2.0
    homepage_delay_max: float = 4.0
    _root_url: str = ""

    # Threshold for "parser can't find prices this many times in a row".
    #admins are notified once the counter reaches this value.
    FAILURE_THRESHOLD: int = 5

    def __init__(self):
        self._consecutive_failures: int = 0

    @classmethod
    @abstractmethod
    def can_handle(cls, url: str) -> bool:
        pass

    @abstractmethod
    async def get_price(self, url: str) -> float | None:
        pass

    async def get_price_tiers(self, url: str) -> dict[str, float] | None:
        price = await self.get_price(url)
        if price is None:
            return None
        return {"standard": price}

    @abstractmethod
    async def get_cheapest_from_search(
        self, search_url: str, title_filter: str,
    ) -> SearchResult | None:
        pass

    async def start_session(self):
        self._browser = await uc.start(
            browser_executable_path=REAL_CHROME,
            headless=False,
            browser_args=CHROME_ARGS,
        )
        self._session_active = True
        self._current_page = None

        if self._root_url:
            self._current_page = await self._browser.get(self._root_url)
            homepage_delay = random.uniform(self.homepage_delay_min, self.homepage_delay_max)
            await asyncio.sleep(homepage_delay)

        logger.debug("Browser session started for %s", self.marketplace)

    async def end_session(self):
        if not getattr(self, "_session_active", False):
            return
        if hasattr(self, "_browser") and self._browser:
            try:
                await self._browser.aclose()
                if hasattr(self._browser, "_process") and self._browser._process:
                    try:
                        await asyncio.wait_for(self._browser._process.wait(), timeout=5.0)
                    except asyncio.TimeoutError:
                        self._browser._process.kill()
                        await self._browser._process.wait()
            except RuntimeError:
                pass
        self._session_active = False
        self._current_page = None
        logger.debug("Browser session ended for %s", self.marketplace)

    async def _setup_browser(self):
        await self.start_session()

    async def _random_delay(self):
        delay = random.uniform(self.delay_min, self.delay_max)
        await asyncio.sleep(delay)

    async def _get_page(self, url: str):
        if not getattr(self, "_session_active", False):
            await self._setup_browser()
        self._current_page = await self._browser.get(url)
        await asyncio.sleep(3)
        return self._current_page

    async def _close_page(self, page):
        pass

    async def _close(self):
        if getattr(self, "_session_active", False):
            await self.end_session()

    async def _take_screenshot(self, page, label: str = "error") -> str | None:
        try:
            SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"{self.marketplace}_{label}_{ts}.png"
            path = SCREENSHOTS_DIR / filename
            await page.save_screenshot(str(path))
            logger.info("Screenshot saved: %s", path)
            return str(path)
        except Exception as exc:
            logger.error("Failed to take screenshot: %s", exc)
            return None

    def register_parse_failure(self) -> bool:
        """Increment the consecutive-failures counter.

        Returns True if the counter has just reached FAILURE_THRESHOLD
        (this transition should trigger a service notification). Returns
        False otherwise (still below threshold or already past it).
        """
        self._consecutive_failures += 1
        reached = self._consecutive_failures == self.FAILURE_THRESHOLD
        if reached:
            logger.error(
                "%s parser failed %d times in a row — likely layout change",
                self.marketplace, self._consecutive_failures,
            )
        else:
            logger.debug(
                "%s consecutive failures: %d",
                self.marketplace, self._consecutive_failures,
            )
        return reached

    def register_parse_success(self):
        """Reset the consecutive-failures counter on a successful parse."""
        if self._consecutive_failures > 0:
            logger.debug(
                "%s parse success — resetting failures counter from %d",
                self.marketplace, self._consecutive_failures,
            )
        self._consecutive_failures = 0
