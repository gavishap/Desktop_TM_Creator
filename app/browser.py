"""Chrome session driver — uses the operator's *installed* Google Chrome via Playwright.

Design choices that matter for beating Ticketmaster's bot wall:
- ``channel="chrome"`` launches the real Chrome binary already on the machine (its real
  UA, TLS, and engine), not a downloaded Chromium. Combined with the operator's own
  residential IP (no proxy), this is the fingerprint TM's Kasada/EPS layer accepts.
- A fresh, temporary profile per session means cookies/localStorage start empty every
  attempt — so "clear cookies on block" is simply: close and relaunch. The operator's own
  Chrome profile is never opened, so their own logins are neither used nor disturbed. On
  top of that, ``start()`` clears cookies and cache browser-wide before the first page
  loads, so a run can never inherit state even if Chrome hands back a reused profile.
- Non-headless by default so the operator can watch and, if TM ever shows a manual
  challenge, complete it by hand in the same window.
"""
from __future__ import annotations

from typing import Optional

from playwright.async_api import Browser, BrowserContext, Page, async_playwright

from app.config import BrowserConfig
from app.paths import SCREENSHOT_DIR


class ChromeSession:
    def __init__(self, config: BrowserConfig):
        self.config = config
        self._pw = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        self._logs: list[str] = []
        self.cookies_at_start = -1

    @property
    def page(self) -> Page:
        if not self._page:
            raise RuntimeError("Session not started — call start() first")
        return self._page

    async def start(self):
        self._pw = await async_playwright().start()
        launch_kwargs = {
            "headless": self.config.headless,
            "channel": self.config.channel,  # "chrome" -> the installed Google Chrome
            "args": ["--disable-blink-features=AutomationControlled", "--start-maximized"],
        }
        try:
            self._browser = await self._pw.chromium.launch(**launch_kwargs)
        except Exception:
            # Chrome channel not found (not installed / non-standard path): fall back to
            # bundled Chromium so the app still runs, just with a Chromium fingerprint.
            launch_kwargs.pop("channel", None)
            self._browser = await self._pw.chromium.launch(**launch_kwargs)

        self._context = await self._browser.new_context(
            viewport=None,  # follow the real (maximized) window size
            locale="en-US",
        )
        self._page = await self._context.new_page()
        self.cookies_at_start = await self._wipe_browser_state()

        self._page.on("console", lambda m: self._logs.append(f"[console:{m.type}] {m.text}"[:300]))
        self._page.on("pageerror", lambda e: self._logs.append(f"[js_error] {e}"[:300]))
        self._page.on("requestfailed", lambda r: self._logs.append(f"[net_fail] {r.url[:120]}"))

    async def _wipe_browser_state(self) -> int:
        """Empty every cookie and cache entry in this Chrome, then report what's left.

        The temp profile should already be empty; doing it anyway costs nothing and means a
        run can never start with somebody else's Ticketmaster session attached.
        """
        try:
            cdp = await self._context.new_cdp_session(self._page)
            for method in ("Network.clearBrowserCookies", "Network.clearBrowserCache"):
                await cdp.send(method)
            await cdp.detach()
        except Exception:
            pass
        await self.clear_cookies()
        try:
            return len(await self._context.cookies())
        except Exception:
            return -1

    async def clear_cookies(self):
        """Wipe cookies + storage without tearing down the whole browser."""
        if self._context:
            try:
                await self._context.clear_cookies()
                await self._page.evaluate(
                    "() => { try { localStorage.clear(); sessionStorage.clear(); } catch(e){} }"
                )
            except Exception:
                pass

    def get_and_clear_logs(self) -> list[str]:
        logs = self._logs.copy()
        self._logs.clear()
        return logs

    async def screenshot(self, name: str) -> str:
        path = SCREENSHOT_DIR / f"{name}.png"
        try:
            await self._page.screenshot(path=str(path), full_page=True)
        except Exception:
            pass
        return str(path)

    async def close(self):
        for closer in (self._context, self._browser):
            try:
                if closer:
                    await closer.close()
            except Exception:
                pass
        try:
            if self._pw:
                await self._pw.stop()
        except Exception:
            pass
        self._page = self._context = self._browser = self._pw = None
