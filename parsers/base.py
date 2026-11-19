import random
import asyncio
import shutil
import os
import socket
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import nodriver as uc
import nodriver.cdp.page as cdp_page
import nodriver.cdp.runtime as cdp_runtime

from utils.chrome_cleanup import (
    async_sweep_chrome_tree,
    profile_marker_set,
    profile_matches_args,
)
from utils.logger import logger

# nodriver adds many suspicious Chrome flags by default that trigger
# Ozon's WAF (--remote-allow-origins=*, --disable-infobars, etc).
# Override with only essential flags.
uc.Config._default_browser_args = [
    "--no-first-run",
]

# Chrome profile directory — outside /tmp to avoid filling it up
CHROME_PROFILE_DIR = Path(os.getenv("CHROME_PROFILE_DIR", "./chrome_profile"))

# Default Avito profile for direct parser use. The scheduler resolves the
# price/books profiles explicitly after loading the environment.
AVITO_PROFILE_DIR = Path(os.getenv("AVITO_PROFILE_DIR", "./chrome_profile_avito"))

SCREENSHOTS_DIR = Path("screenshots")

REAL_CHROME = shutil.which("google-chrome-stable") or shutil.which("google-chrome") or "/opt/google/chrome/google-chrome"

CHROME_ARGS = [
    "--window-size=1920,1080",
    "--disable-blink-features=AutomationControlled",
    "--disable-dev-shm-usage",
    "--disable-gpu",
    "--no-zygote",
    "--no-sandbox",
]

# Заглушка «Доступ ограничен» может означать и блокировку по IP, а не профиля.
# Чтобы не пересоздавать профиль (а с ним и ~200 МБ бэкапа) каждый цикл,
# авто-сброс профиля ограничен по времени.
PROFILE_RESET_MIN_INTERVAL = 30 * 60  # секунд
PROFILE_BACKUP_PREFIX = ".blocked."
PROFILE_RESET_MARKER_SUFFIX = ".reset_at"

# Антидетект-скрипт (CDP Page.addScriptToEvaluateOnNewDocument), который
# маскирует сигнатуры автоматизации. Сейчас НЕ инжектится: _inject_stealth()
# — пустая заглушка (см. её докстринг). Константа оставлена для справки.
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

    // --- navigator.deviceMemory ---
    Object.defineProperty(navigator, 'deviceMemory', {
        get: () => 8,
    });

    // --- Permissions API ---
    // Some bots return 'denied' for notifications; real browsers return 'default'.
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

    def __init__(
        self,
        profile_dir: Path | str | None = None,
        profile_markers: tuple[str, ...] | None = None,
    ):
        self._consecutive_failures = 0
        default_profile = (
            AVITO_PROFILE_DIR if self.marketplace == "avito" else CHROME_PROFILE_DIR
        )
        raw_profile = str(profile_dir or default_profile)
        self._profile_dir = Path(raw_profile).expanduser().resolve()
        self._profile_markers = profile_marker_set(
            raw_profile, profile_markers or ())
        self._session_quarantined = False

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

    def _process_live(self, pid: int) -> bool:
        try:
            os.kill(pid, 0)
        except (ProcessLookupError, PermissionError):
            return pid > 0 and os.path.exists(f"/proc/{pid}")
        except Exception:
            return False
        return True

    def _process_is_our_chrome(self, pid: int) -> bool:
        try:
            with open(f"/proc/{pid}/cmdline", "rb") as f:
                raw = f.read()
            args = [
                item.decode("utf-8", errors="replace")
                for item in raw.split(b"\0")
                if item
            ]
            return profile_matches_args(
                args, self._profile_dir, self._profile_markers)
        except Exception:
            return False

    def _remove_stale_lock_files(self, profile_dir: Path) -> None:
        for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
            (profile_dir / name).unlink(missing_ok=True)
        # Clean up an orphaned /tmp socket directory that Chrome left behind.
        try:
            parent = profile_dir.parent
            for d in sorted(parent.glob(".com.google.Chrome.*")):
                if d.is_dir() and not any(d.iterdir()):
                    shutil.rmtree(d, ignore_errors=True)
        except Exception:
            pass

    def _reset_profile_dir(self, reason: str) -> bool:
        """Пересоздать профиль Chrome с нуля (старый каталог — в бэкап).

        Нужно, когда маркетплейс блокирует сам профиль (куки/сессию), а не IP:
        Qrator в этом случае отдаёт заглушку «Доступ ограничен» на любой запрос
        с этими cookies, и без сброса бот из блокировки уже не выйдет — это
        проверялось на Avito (профиль с отработанными cookies блокировался
        даже спустя 12 часов паузы, чистый профиль работал сразу).
        """
        profile_dir = self._profile_dir
        backup_dir: Path | None = None
        try:
            now = time.time()
            marker = profile_dir.parent / f"{profile_dir.name}{PROFILE_RESET_MARKER_SUFFIX}"
            if marker.exists():
                try:
                    last_reset = float(marker.read_text().strip() or 0)
                except ValueError:
                    last_reset = 0.0
                if now - last_reset < PROFILE_RESET_MIN_INTERVAL:
                    logger.info(
                        "%s: сброс профиля %s пропущен — прошло %.0f с из %d с",
                        reason, profile_dir, now - last_reset,
                        PROFILE_RESET_MIN_INTERVAL,
                    )
                    return False

            marker.write_text(str(now))

            if profile_dir.exists():
                backup_dir = (
                    profile_dir.parent
                    / f"{profile_dir.name}{PROFILE_BACKUP_PREFIX}"
                    f"{datetime.now().strftime('%Y%m%d_%H%M%S')}"
                )
                shutil.move(str(profile_dir), str(backup_dir))

            profile_dir.mkdir(parents=True, exist_ok=True)

            if backup_dir is not None:
                logger.warning(
                    "%s: профиль %s заменён на чистый, старый сохранён в %s",
                    reason, profile_dir, backup_dir,
                )
            else:
                logger.warning(
                    "%s: создан чистый профиль %s", reason, profile_dir,
                )

            # Бэкапы весят по ~200 МБ — оставляем только последний.
            for old in sorted(profile_dir.parent.glob(
                f"{profile_dir.name}{PROFILE_BACKUP_PREFIX}*",
            )):
                if old != backup_dir:
                    shutil.rmtree(old, ignore_errors=True)

            return True

        except Exception as e:
            logger.error("Не удалось сбросить профиль %s: %s", profile_dir, e)
            return False

    async def _check_and_reset_chrome_profile(
        self, profile_dir: Path | None = None,
    ) -> bool:
        """Detect and clear stale Chrome singleton locks so the profile can be reused.

        Chrome refuses to start when `chrome_profile/SingletonLock` points to
        a process on a **different host** (e.g. a profile synced from dev via
        `fab sync-profile`) or to a dead PID — even though the PID is gone.
        In those cases we remove **only the lock files**, preserving cookies
        (e.g. the trusted Avito session).

        If the lock names a live Chrome on the same machine that actually
        owns this profile, terminate only that profile's Chrome processes and
        clear its locks. Cookies and the trusted session are never deleted here.
        """
        if profile_dir is None:
            profile_dir = self._profile_dir
        try:
            if not profile_dir.exists():
                return False

            lock_path = profile_dir / "SingletonLock"
            # SingletonLock — висячая символическая ссылка (имя "host-pid"),
            # поэтому exists() для неё всегда False.
            if not (lock_path.exists() or lock_path.is_symlink()):
                return False

            target = lock_path.resolve().name if lock_path.is_symlink() else lock_path.name
            hostname, _, pid_str = str(target).rpartition("-")
            pid = int(pid_str) if pid_str.isdigit() else None
            local_hostname = socket.gethostname()

            stale = False
            should_kill = False

            if not hostname or hostname != local_hostname:
                # Lock created on another machine — Chrome blocks the profile.
                logger.warning(
                    "Foreign SingletonLock %s (%s) — clearing stale lock files",
                    target, hostname or "(none)",
                )
                stale = True
            elif pid is None:
                stale = True
            elif not self._process_live(pid):
                logger.warning(
                    "SingletonLock %s points to dead pid %d — removing stale lock files",
                    target, pid,
                )
                stale = True
            elif self._process_is_our_chrome(pid):
                should_kill = True
            else:
                # PID is alive but it is NOT our Chrome (PID reuse / foreign).
                # We must not kill it; treat the lock as stale and remove it.
                logger.warning(
                    "SingletonLock %s references alien pid %d — removing stale lock files",
                    target, pid,
                )
                stale = True

            if should_kill:
                logger.warning(
                    "Terminating orphan Chrome (pid=%d) holding profile %s",
                    pid, profile_dir,
                )
                # Kill only processes launched with this exact profile. The
                # trusted cookies/session stay on disk.
                await async_sweep_chrome_tree(
                    profile_dir, self._profile_markers)
                self._remove_stale_lock_files(profile_dir)
                logger.info("Cleared Chrome locks for %s after process sweep", profile_dir)
                return False

            if stale:
                # Перед удалением lock-файлов проверяем, не использует ли
                # профиль живой Chrome (например, запущенный старой версией
                # с относительным путём). Убиваем только Chrome этого
                # профиля, cookies не трогаем.
                killed = await async_sweep_chrome_tree(
                    profile_dir, self._profile_markers)
                if killed:
                    logger.warning(
                        "Killed %d Chrome process(es) still using profile %s",
                        killed, profile_dir,
                    )
                self._remove_stale_lock_files(profile_dir)
                logger.info("Cleared stale singleton locks for %s", profile_dir)
                return False

            return False

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
            title = await self._eval(test_page, "document.title")
            if not title or "Example Domain" not in title:
                raise Exception(f"Unexpected page title: {title}")
            logger.debug("Browser verification successful - page title: %s", title)
        except Exception as e:
            logger.error(f"Browser verification failed: {e}")
            raise

    async def _inject_stealth(self):
        """Inject anti-detection JS via CDP before any page scripts execute.

        Currently **disabled** because the fake ``navigator.plugins`` override
        triggers Wildberries WAF ("Проверяем браузер" challenge) while not
        improving Ozon or Avito success rates.  Re-enable once the patches
        are refined to avoid marketplace-specific fingerprint checks.

        CDP Page domain commands must be sent to a **page** target, not the
        browser-level target (``-32601``).  Use ``main_tab.send()``.
        """
        pass  # stealth disabled — see comment above

    def _raise_if_quarantined(self) -> None:
        if self._session_quarantined:
            raise RuntimeError(
                f"Chrome session for profile {self._profile_dir} is quarantined")

    async def start_session(self):
        workspace_home = Path("./chrome_home")
        workspace_home.mkdir(parents=True, exist_ok=True)
        os.environ["HOME"] = str(workspace_home)
        os.environ["XDG_CONFIG_HOME"] = str(workspace_home / ".config")
        logger.debug("start_session %s | HOME=%s | CHROME_ARGS=%s", self.marketplace, os.environ["HOME"], CHROME_ARGS)

        # Check and reset stale Chrome singleton locks (keeps cookies).
        await self._check_and_reset_chrome_profile()

        # Retry logic for browser startup.
        max_retries = 3
        for attempt in range(max_retries):
            try:
                self._raise_if_quarantined()
                self._profile_dir.mkdir(parents=True, exist_ok=True)
                logger.debug("uc.start attempt %d/%d for %s", attempt + 1, max_retries, self.marketplace)
                self._browser = await asyncio.wait_for(
                    uc.start(
                        browser_executable_path=REAL_CHROME,
                        headless=False,
                        browser_args=CHROME_ARGS,
                        no_sandbox=True,
                        user_data_dir=self._profile_dir,
                    ),
                    timeout=60,
                )
                self._raise_if_quarantined()
                logger.debug("uc.start succeeded on attempt %d", attempt + 1)

                await asyncio.wait_for(self._verify_browser_works(), timeout=45)
                self._raise_if_quarantined()
                break
            except Exception as e:
                logger.warning("uc.start failed on attempt %d/%d for %s: %s", attempt + 1, max_retries, self.marketplace, e)
                # uc.start may already have spawned Chrome before raising.
                # Sweep this profile before retrying so no orphan keeps its lock.
                await self._cleanup_failed_start()
                if self._session_quarantined:
                    raise
                if attempt == max_retries - 1:
                    raise
                await asyncio.sleep(2 ** attempt)

        self._session_active = True
        self._current_page = None

        await self._inject_stealth()
        self._raise_if_quarantined()

        if self._root_url:
            self._current_page = await self._browser.get(self._root_url)
            self._raise_if_quarantined()
            homepage_delay = random.uniform(self.homepage_delay_min, self.homepage_delay_max)
            await asyncio.sleep(homepage_delay)

        logger.debug("Browser session started for %s", self.marketplace)

    async def sweep_profile_chrome(self) -> int:
        """Kill only Chrome processes using this profile and clear its locks."""
        killed = await async_sweep_chrome_tree(
            self._profile_dir, self._profile_markers)
        self._remove_stale_lock_files(self._profile_dir)
        return killed

    async def _cleanup_failed_start(self) -> None:
        """Remove a partially started Chrome without deleting profile data."""
        try:
            await asyncio.wait_for(self.close_browser(), timeout=5.0)
        except asyncio.TimeoutError:
            logger.warning(
                "Partial browser cleanup timed out for %s", self.marketplace)
        except Exception as exc:
            logger.warning(
                "Partial browser cleanup failed for %s: %s",
                self.marketplace, exc,
            )
        await self.sweep_profile_chrome()

    async def close_browser(self):
        """Закрыть браузер даже если сессия ещё не была помечена активной.

        Нужен после сбоя запуска: Chrome мог уже появиться, но проверка браузера
        не прошла и ``_session_active`` ещё не выставлен.
        """
        browser = getattr(self, "_browser", None)
        if not browser:
            self._session_active = False
            self._current_page = None
            return
        try:
            await browser.aclose()
        except RuntimeError:
            pass
        except Exception as exc:
            logger.warning("Browser cleanup failed for %s: %s", self.marketplace, exc)
        process = getattr(browser, "_process", None)
        if process:
            try:
                await asyncio.wait_for(process.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        self._browser = None
        self._session_active = False
        self._current_page = None

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
        self._browser = None

        # Belt-and-braces: nodriver's aclose only terminates the MAIN bot
        # Chrome PID; the real Chrome binary leaves a whole tree of descendants
        # (renderer / GPU / network / utility), each still carrying
        # --user-data-dir=<profile>.  Sweeping by profile marker kills the
        # entire tree so orphans never accumulate across poll cycles
        # (previously observed as ~7 GB of stuck chrome on the server).
        try:
            await async_sweep_chrome_tree(
                self._profile_dir, self._profile_markers)
        except Exception as exc:
            logger.warning("Chrome tree sweep failed for %s: %s", self.marketplace, exc)

        # Маркетплейс заблокировал сам профиль (не IP) — пересоздаём его,
        # иначе каждый следующий цикл будет получать ту же заглушку.
        if getattr(self, "_profile_blocked", False):
            self._profile_blocked = False
            self._reset_profile_dir(
                f"{self.marketplace}: маркетплейс вернул страницу блокировки",
            )

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