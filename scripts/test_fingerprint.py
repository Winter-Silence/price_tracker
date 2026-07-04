import asyncio
import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv

load_dotenv()

TEST_URL = "https://www.ozon.ru/"

FINGERPRINT_JS = """
() => {
    const result = {};
    result.navigator = {
        userAgent: navigator.userAgent,
        language: navigator.language,
        languages: navigator.languages,
        platform: navigator.platform,
        hardwareConcurrency: navigator.hardwareConcurrency,
        deviceMemory: navigator.deviceMemory,
        maxTouchPoints: navigator.maxTouchPoints,
        webdriver: navigator.webdriver,
        pluginsCount: navigator.plugins?.length || 0,
        pluginsNames: Array.from(navigator.plugins || []).map(p => p.name),
        doNotTrack: navigator.doNotTrack,
        cookieEnabled: navigator.cookieEnabled,
    };
    result.screen = {
        width: screen.width,
        height: screen.height,
        availWidth: screen.availWidth,
        availHeight: screen.availHeight,
        colorDepth: screen.colorDepth,
        pixelDepth: screen.pixelDepth,
    };
    result.window = {
        innerWidth: window.innerWidth,
        innerHeight: window.innerHeight,
        outerWidth: window.outerWidth,
        outerHeight: window.outerHeight,
        devicePixelRatio: window.devicePixelRatio,
    };
    result.chrome = {
        exists: !!window.chrome,
        runtimeExists: !!window.chrome?.runtime,
    };
    result.features = {
        webgl: !!document.createElement('canvas').getContext('webgl'),
        webgl2: !!document.createElement('canvas').getContext('webgl2'),
    };

    // WebGL renderer
    try {
        const canvas = document.createElement('canvas');
        const gl = canvas.getContext('webgl');
        const debugInfo = gl.getExtension('WEBGL_debug_renderer_info');
        result.webgl = {
            vendor: gl.getParameter(debugInfo.UNMASKED_VENDOR_WEBGL),
            renderer: gl.getParameter(debugInfo.UNMASKED_RENDERER_WEBGL),
        };
    } catch (e) {
        result.webgl = { error: e.message };
    }

    // Connection
    result.connection = {};
    if (navigator.connection) {
        result.connection.effectiveType = navigator.connection.effectiveType;
        result.connection.rtt = navigator.connection.rtt;
        result.connection.downlink = navigator.connection.downlink;
    }

    // Permissions
    result.permissions = {};
    try {
        result.permissions.notification = Notification.permission;
    } catch(e) {}

    // Cdc
    result.cdc = {
        cdc_adoQpoasnfa76pfcZLmcfl_Array: typeof window.cdc_adoQpoasnfa76pfcZLmcfl_Array,
        cdc_adoQpoasnfa76pfcZLmcfl_Promise: typeof window.cdc_adoQpoasnfa76pfcZLmcfl_Promise,
        cdc_adoQpoasnfa76pfcZLmcfl_Symbol: typeof window.cdc_adoQpoasnfa76pfcZLmcfl_Symbol,
    };

    // Automation indicators
    result.automation = {
        __nightmare: typeof window.__nightmare,
        __phantomas: typeof window.__phantomas,
        _phantom: typeof window._phantom,
        phantom: typeof window.phantom,
        callSelenium: typeof window.callSelenium,
        _selenium: typeof window._selenium,
        __webdriver_evaluate: typeof window.__webdriver_evaluate,
        __selenium_unwrapped: typeof window.__selenium_unwrapped,
        __driver_evaluate: typeof window.__driver_evaluate,
        __webdriver_unwrapped: typeof window.__webdriver_unwrapped,
        __fxdriver_evaluate: typeof window.__fxdriver_evaluate,
        __fxdriver_unwrapped: typeof window.__fxdriver_unwrapped,
        domAutomation: typeof window.domAutomation,
        domAutomationController: typeof window.domAutomationController,
        Awesomium: typeof window.Awesomium,
    };

    return result;
}
"""


async def take_fingerprint(name, browser_type="chromium", launch_args=None, extra_init=None):
    from playwright.async_api import async_playwright
    pw = await async_playwright().start()

    launch_kwargs = {"headless": True}
    if launch_args:
        launch_kwargs["args"] = launch_args

    if browser_type == "firefox":
        browser = await pw.firefox.launch(**launch_kwargs)
    else:
        browser = pw.chromium
        if launch_args and "--use-real-chrome" in launch_args:
            launch_kwargs.pop("args", None)
            launch_kwargs["executable_path"] = "/usr/bin/google-chrome-stable"
            launch_kwargs["args"] = [a for a in (launch_args or []) if a != "--use-real-chrome"]
        browser = await browser.launch(**launch_kwargs)

    ctx = await browser.new_context(
        viewport={"width": 1920, "height": 1080},
        locale="ru-RU",
        timezone_id="Europe/Moscow",
    )

    if extra_init:
        await ctx.add_init_script(extra_init)

    page = await ctx.new_page()

    # Go to a neutral page first to measure fingerprint
    await page.goto("about:blank")
    fp = await page.evaluate(FINGERPRINT_JS)

    # Then try Ozon
    await asyncio.sleep(2)
    resp = await page.goto("https://www.ozon.ru/", wait_until="domcontentloaded")
    await asyncio.sleep(3)
    ozon_status = resp.status if resp else "None"
    ozan_title = await page.title()

    fp["ozon_status"] = ozon_status
    fp["ozon_title"] = ozan_title[:60]

    await ctx.close()
    await browser.close()
    await pw.stop()

    return fp


async def main():
    configs = [
        ("vanilla_chromium", "chromium", None, None),
        ("chromium_stealth", "chromium", ["--headless=new", "--disable-blink-features=AutomationControlled"], """
        () => {
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            Object.defineProperty(navigator, 'plugins', {
                get: () => { const p = [{name:'Chrome PDF Plugin',filename:'internal-pdf-viewer'},{name:'Chrome PDF Viewer',filename:'mhjfbmdgcfjbbpaeojofohoefgiehjai'},{name:'Native Client',filename:'internal-nacl-plugin'}]; p.length=3; return p; },
            });
            Object.defineProperty(navigator, 'languages', { get: () => ['ru-RU','ru','en-US','en'] });
            if(!window.chrome)window.chrome={};
            if(!window.chrome.runtime)window.chrome.runtime={connect:()=>{},sendMessage:()=>{}};
            Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 });
            Object.defineProperty(navigator, 'deviceMemory', { get: () => 8 });
        }
        """),
        ("real_chrome", "chromium", ["--use-real-chrome", "--headless=new", "--disable-blink-features=AutomationControlled"], """
        () => {
            Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
            if(!window.chrome)window.chrome={};
            if(!window.chrome.runtime)window.chrome.runtime={connect:()=>{},sendMessage:()=>{}};
        }
        """),
    ]

    results = {}
    for name, btype, args, init in configs:
        print(f"\n{'='*60}")
        print(f"Testing: {name}")
        print(f"{'='*60}")
        try:
            fp = await take_fingerprint(name, btype, args, init)
            results[name] = fp

            nav = fp["navigator"]
            print(f"  webdriver: {nav['webdriver']}")
            print(f"  plugins: {nav['pluginsCount']} — {nav['pluginsNames']}")
            print(f"  languages: {nav['languages']}")
            print(f"  hardwareConcurrency: {nav['hardwareConcurrency']}")
            print(f"  deviceMemory: {nav['deviceMemory']}")
            print(f"  chrome exists: {fp['chrome']['exists']}")
            print(f"  chrome.runtime: {fp['chrome']['runtimeExists']}")
            print(f"  webgl: {fp.get('webgl', {})}")
            print(f"  cdc: {fp['cdc']}")
            print(f"  automation keys: {[(k,v) for k,v in fp['automation'].items() if v != 'undefined']}")
            print(f"  OZON: status={fp['ozon_status']} title={fp['ozon_title']}")

        except Exception as exc:
            import traceback
            print(f"  ERROR: {exc}")
            traceback.print_exc()

    # Compare
    print(f"\n{'='*60}")
    print("COMPARISON — key differences")
    print(f"{'='*60}")
    for key in ["webdriver", "pluginsCount", "hardwareConcurrency", "deviceMemory"]:
        vals = {name: results[name]["navigator"][key] for name in results}
        if len(set(str(v) for v in vals.values())) > 1:
            print(f"  {key}: {vals}")


if __name__ == "__main__":
    asyncio.run(main())
