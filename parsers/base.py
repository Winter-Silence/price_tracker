import random
import asyncio
import shutil
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import nodriver as uc
import nodriver.cdp.page as cdp_page
import nodriver.cdp.runtime as cdp_runtime

from utils.logger import logger

# nodriver adds many suspicious Chrome flags by default that trigger
# Ozon's WAF (--remote-allow-origins=*, --disable-infobars, etc).
# Override with only essential flags.
uc.Config._default_browser_args = [
    "--no-first-run",
]

# Chrome profile directory — outside /tmp to avoid filling it up
CHROME_PROFILE_DIR = Path(os.getenv("CHROME_PROFILE_DIR", "./chrome_profile"))

SCREENSHOTS_DIR = Path("screenshots")

REAL_CHROME = shutil.which("google-chrome-stable") or shutil.which("google-chrome") or "/opt/google/chrome/google-chrome"

CHROME_ARGS = [
    "--window-size=1920,1080",
    "--disable-blink-features=AutomationControlled",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--no-zygote",
]

# JavaScript injected via CDP Page.addScriptToEvaluateOnNewDocument
# before any page scripts execute. Removes automation indicators that
# Ozon's WAF uses to detect headless/controlled browsers.
STEALTH_JS = """
(() => {
    // --- navigator.webdriver ---
    // Chrome controlled by DevTool protocol sets this to true.
    delete Object.getPrototypeOf(navigator).webdriver;

    // --- chrome.runtime ---
    // Real Chrome has window.chrome with a runtime object.
    if (!window.chrome) window.chrome = {};
    if (!window.chrome.runtime) {
        window.chrome.runtime = {
            connect: function() {},
            sendMessage: function() {},
        };
    }

    // --- Automation indicators ---
    // These are injected by ChromeDriver/Selenium/etc.
    const automationProps = [
        '__webdriver_evaluate', '__selenium_unwrapped',
        '__driver_evaluate', '__webdriver_unwrapped',
        '__fxdriver_evaluate', '__fxdriver_unwrapped',
        '_phantom', '__phantomas', '__nightmare',
        'callSelenium', '_selenium',
        'domAutomation', 'domAutomationController',
        'Awesomium',
    ];
    for (const prop of automationProps) {
        if (window[prop] !== undefined) {
            Object.defineProperty(window, prop, { get: () => undefined });
        }
    }

    // --- cdc_* ChromeDriver variables ---
    // ChromeDriver injects cdc_adoQpoasnfa76pfcZLmcfl_* vars on window.
    for (const key of Object.keys(window)) {
        if (key.startsWith('cdc_')) {
            Object.defineProperty(window, key, { get: () => undefined });
        }
    }

    // --- navigator.plugins ---
    // Headless Chrome has 0 plugins; real Chrome has at least 3.
    Object.defineProperty(navigator, 'plugins', {
        get: () => {
            const plugins = [
                { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer',
                  description: 'Portable Document Format',
                  length: 1,
                  item: function(i) { return this[i]; },
                  namedItem: function(n) { return null; },
                  [Symbol.iterator]: function*() { yield this[0]; } },
                { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai',
                  description: '',
                  length: 1,
                  item: function(i) { return this[i]; },
                  namedItem: function(n) { return null; },
                  [Symbol.iterator]: function*() { yield this[0]; } },
                { name: 'Native Client', filename: 'internal-nacl-plugin',
                  description: '',
                  length: 2,
                  item: function(i) { return this[i]; },
                  namedItem: function(n) { return null; },
                  [Symbol.iterator]: function*() { yield this[0]; yield this[1]; } },
            ];
            plugins.length = 3;
            plugins.refresh = function() {};
            return plugins;
        },
    });

    // --- navigator.languages ---
    Object.defineProperty(navigator, 'languages', {
        get: () => ['ru-RU', 'ru', 'en-US', 'en'],
    });

    // --- navigator.hardwareConcurrency ---
    Object.defineProperty(navigator, 'hardwareConcurrency', {
        get: () => 8,
    });

    # --- navigator.deviceMemory ---
    Object.defineProperty(navigator, 'deviceMemory', {
        get: () => 8,
    });

    # --- Permissions API ---
    # Some bots return 'denied' for notifications; real browsers return 'default'.
    const originalQuery = window.Permissions && window.Permissions.prototype.query;
    if (originalQuery) {
        window.Permissions.prototype.query = function(params) {
            if (params && params.name === 'notifications') {
                return Promise.resolve({ state: Notification.permission || 'default' });
            }
            return originalQuery.call(this, params);
        };
    }
})();
"""


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


def _deserialize(value):
    """Convert nodriver's deep serialization format to native Python objects.

    Deep serialization represents:
      - objects  as [[key, {type, value}], ...]
      - arrays   as [{type, value}, ...]
      - scalars  as plain values
    """
    if isinstance(value, dict):
        if "type" in value and "value" in value:
            return _deserialize(value["value"])
        return {k: _deserialize(v) for k, v in value.items()}
    if isinstance(value, list):
        if (
            value
            and isinstance(value[0], list)
            and len(value[0]) == 2
            and isinstance(value[0][1], dict)
            and "type" in value[0][1]
        ):
            return {item[0]: _deserialize(item[1]) for item in value}
        return [_deserialize(item) for item in value]
    return value


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
        self._consecutive_failures = 0

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

    def _check_and_reset_chrome_profile(self):
        """Check if Chrome profile seems corrupted and reset if needed.
        
        Returns True if profile was reset, False otherwise.
        """
        try:
            # Check if profile directory exists
            if not CHROME_PROFILE_DIR.exists():
                return False
                
            # Check for lock files that might indicate a crashed Chrome process
            lock_files = list(CHROME_PROFILE_DIR.glob("*/.com.google.chrome.*")) + \
                        list(CHROME_PROFILE_DIR.glob("*.lock")) + \
                        list(CHROME_PROFILE_DIR.glob("lockfile")) + \
                        list(CHROME_PROFILE_DIR.glob(".org.chromium.Chromium.*")) + \
                        list(CHROME_PROFILE_DIR.glob("Singleton*"))
            
            # If we find lock files, the profile might be corrupted
            if lock_files:
                logger.warning(f"Found potential Chrome profile lock files: {[f.name for f in lock_files[:5]]}")
                # Back up the corrupted profile
                backup_dir = CHROME_PROFILE_DIR.parent / f"{CHROME_PROFILE_DIR.name}.corrupted.{datetime.now().strftime('%Y%m%d_%H%M%S')}"
                shutil.move(str(CHROME_PROFILE_DIR), str(backup_dir))
                logger.info(f"Backed up potentially corrupted Chrome profile to {backup_dir}")
                
                # Create fresh profile directory
                CHROME_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
                logger.info("Created fresh Chrome profile directory")
                return True
                
        except Exception as e:
            logger.warning(f"Error checking Chrome profile: {e}")
        return False

    async def _verify_browser_works(self):
        """Verify that the browser is working correctly by navigating to a simple page.
        
        Raises an exception if the browser is not working properly.
        """
        try:
            # Navigate to a simple, reliable page
            test_page = await self._browser.get("https://www.example.com")
            # Wait a bit for page to load
            await asyncio.sleep(2)
            # Check that we can get the page title
            title = await test_page.get_title()
            if not title or "Example Domain" not in title:
                raise Exception(f"Unexpected page title: {title}")
            logger.debug("Browser verification successful - page title: %s", title)
        except Exception as e:
            logger.error(f"Browser verification failed: {e}")
            raise

    async def _inject_stealth(self):
        """Inject anti-detection JS via CDP before any page scripts execute."""
        try:
            await self._browser.send(cdp_page.enable())
            await self._browser.send(
                cdp_page.add_script_to_evaluate_on_new_document(
                    source=STEALTH_JS,
                )
            )
        except Exception as exc:
            logger.warning("Stealth injection failed: %s", exc)

    async def start_session(self):
        print(f"[DEBUG] start_session called for {self.marketplace}")
        print(f"[DEBUG] HOME: {os.environ.get('HOME', 'NOT SET')}")
        print(f"[DEBUG] XDG_CONFIG_HOME: {os.environ.get('XDG_CONFIG_HOME', 'NOT SET')}")
        print(f"[DEBUG] DISPLAY: {os.environ.get('DISPLAY', 'NOT SET')}")
        print(f"[DEBUG] REAL_CHROME: {REAL_CHROME}")
        print(f"[DEBUG] CHROME_PROFILE_DIR: {CHROME_PROFILE_DIR}")
        print(f"[DEBUG] CHROME_ARGS: {CHROME_ARGS}")
        
        # Set HOME and XDG_CONFIG_HOME to workspace directories to avoid sandbox violations
        workspace_home = Path("./chrome_home")
        workspace_home.mkdir(parents=True, exist_ok=True)
        os.environ["HOME"] = str(workspace_home)
        os.environ["XDG_CONFIG_HOME"] = str(workspace_home / ".config")
        print(f"[DEBUG] Setting HOME to: {os.environ.get('HOME')}")
        print(f"[DEBUG] Setting XDG_CONFIG_HOME to: {os.environ.get('XDG_CONFIG_HOME')}")
        
        # Check and reset Chrome profile if needed
        self._check_and_reset_chrome_profile()
        
        # Retry logic for browser startup
        max_retries = 3
        for attempt in range(max_retries):
            try:
                CHROME_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
                print(f"[DEBUG] About to call uc.start (attempt {attempt + 1}/{max_retries})")
                self._browser = await uc.start(
                    browser_executable_path=REAL_CHROME,
                    headless=False,
                    browser_args=CHROME_ARGS,
                    no_sandbox=True,
                    user_data_dir=CHROME_PROFILE_DIR,
                )
                print(f"[DEBUG] uc.start succeeded on attempt {attempt + 1}")
                
                # Verify the browser is working correctly
                await self._verify_browser_works()
                
                break  # Success, exit retry loop
            except Exception as e:
                print(f"[DEBUG] uc.start failed on attempt {attempt + 1}: {e}")
                if attempt == max_retries - 1:  # Last attempt
                    print(f"[DEBUG] All {max_retries} attempts failed")
                    raise  # Re-raise the last exception
                # Wait before retrying
                await asyncio.sleep(2 ** attempt)  # Exponential backoff
                
        self._session_active = True
        self._current_page = None

        await self._inject_stealth()

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

    async def _eval(self, page, expression: str):
        """Evaluate JS expression, bypassing nodriver's deep serialization.

        nodriver 0.50+ adds deep serialization by default which converts JS
        objects to [[key, {type, value}], ...] lists.  We call CDP directly
        with return_by_value=True and no serialization_options so that
        remote_object.value contains the native Python object.
        """
        remote_object, errors = await page.send(
            cdp_runtime.evaluate(
                expression=expression,
                user_gesture=True,
                return_by_value=True,
                allow_unsafe_eval_blocked_by_csp=True,
            )
        )
        if errors:
            raise RuntimeError(f"JS evaluation error: {errors}")
        if remote_object:
            if remote_object.value is not None:
                return remote_object.value
            if remote_object.deep_serialized_value:
                return _deserialize(remote_object.deep_serialized_value.value)
            return remote_object.description
        return None

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