"""Jivetel portal SMS scraper — reads Ticketmaster codes by phone number.

Adapted from the original VPS client. Differences for the desktop app:
- Launches the operator's installed Chrome (``channel="chrome"``) headlessly instead of
  a downloaded Chromium (we don't ship a Chromium binary). Falls back to bundled
  Chromium only if the channel is unavailable.
- Credentials come from the app's local settings, entered once by the operator.

Matching is by phone number, so one Jivetel login can serve many accounts as long as
each account has a distinct number — and a shared login across machines is safe because
each machine only ever looks up its own numbers.
"""
from __future__ import annotations

import asyncio
import re
import time
from typing import Optional

from playwright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright

from app.code_parser import extract_code
from app.config import JivetelConfig

JIVETEL_MESSAGES_URL = "https://portal.jivetel.net/portal/messages"
TM_SHORT_CODE = "77598"
# Each conversation row carries its destination number only inside the click handler
# (preventPropRecentRow(event, this, "77598", "77598", true, "12675946234", "<hash>")). The
# visible text leaves it out on most rows, so that attribute is what we match on.
_ONCLICK_NUM = re.compile(r'preventPropRecentRow\([^)]*?"1?(\d{10})"')


def _normalize_phone(raw: str) -> str:
    digits = re.sub(r"\D", "", raw)
    if digits.startswith("1") and len(digits) == 11:
        digits = digits[1:]
    return digits


class JivetelSmsClient:
    def __init__(self, config: JivetelConfig, channel: str = "chrome"):
        self.config = config
        self.channel = channel
        self._pw: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        self._logged_in = False
        self.last_status = ""
        self.last_numbers: set[str] = set()

    async def init(self):
        self._pw = await async_playwright().start()
        try:
            self._browser = await self._pw.chromium.launch(headless=True, channel=self.channel)
        except Exception:
            self._browser = await self._pw.chromium.launch(headless=True)
        self._context = await self._browser.new_context()
        self._page = await self._context.new_page()

    async def login(self) -> bool:
        if not self._page:
            raise RuntimeError("call init() first")
        url = self.config.portal_url or JIVETEL_MESSAGES_URL
        await self._page.goto(url, wait_until="domcontentloaded")
        await self._page.wait_for_timeout(1500)

        if "/portal/home" in self._page.url or "/portal/messages" in self._page.url:
            self._logged_in = True
            return True
        try:
            u = await self._page.wait_for_selector(
                '#LoginUsername, input[name="data[Login][username]"]', timeout=10000)
            if u:
                await u.fill(self.config.username)
            p = await self._page.wait_for_selector(
                '#LoginPassword, input[name="data[Login][password]"]', timeout=5000)
            if p:
                await p.fill(self.config.password)
            s = await self._page.wait_for_selector('input[type="submit"], button[type="submit"]', timeout=5000)
            if s:
                await s.click()
            await self._page.wait_for_load_state("networkidle", timeout=15000)
            await self._page.wait_for_timeout(1500)
            if "portal" in self._page.url:
                self._logged_in = True
                if "/portal/messages" not in self._page.url:
                    await self._page.goto(JIVETEL_MESSAGES_URL, wait_until="domcontentloaded")
                    await self._page.wait_for_timeout(1200)
                return True
            return False
        except Exception:
            return False

    async def _conversations(self) -> tuple[dict, set]:
        """Ticketmaster threads keyed by destination number, plus every number on the page."""
        threads, numbers = {}, set()
        for el in await self._page.query_selector_all("table.contact-row-table[onclick]"):
            m = _ONCLICK_NUM.search(await el.get_attribute("onclick") or "")
            if not m:
                continue
            numbers.add(m.group(1))
            text = re.sub(r"\s+", " ", (await el.inner_text()) or "").strip()
            if TM_SHORT_CODE in text:
                threads.setdefault(m.group(1), (el, text))
        return threads, numbers

    async def _find_row(self, phone: str):
        target = _normalize_phone(phone)
        threads, numbers = await self._conversations()
        self.last_numbers = numbers
        if target in threads:
            return threads[target]
        # Fallback for rows that do spell the number out, in case the markup changes again.
        for row in await self._page.query_selector_all("tr"):
            text = re.sub(r"\s+", " ", (await row.inner_text()) or "").strip()
            if TM_SHORT_CODE in text and target in re.sub(r"\D", "", text):
                return row, text
        return None, None

    async def _code_from(self, row, row_text: str) -> str | None:
        # The list row already shows the newest message of the thread, so the code is normally
        # right there; opening the thread is only a fallback.
        if row_text and (code := extract_code(row_text)):
            return code
        try:
            await row.click()
            await self._page.wait_for_timeout(1500)
            for line in reversed((await self._page.inner_text("body")).split("\n")):
                line = line.strip()
                if not line:
                    continue
                if "ticketmaster" in line.lower() or re.search(r"\d{6}\s+is your", line, re.IGNORECASE):
                    if code := extract_code(line):
                        return code
        except Exception:
            pass
        return None

    async def _code_for_phone(self, phone: str) -> str | None:
        if not self._page:
            return None
        row, row_text = await self._find_row(phone)
        return await self._code_from(row, row_text) if row else None

    async def _goto_messages(self):
        if self._page and "/portal/messages" not in self._page.url:
            await self._page.goto(JIVETEL_MESSAGES_URL, wait_until="domcontentloaded")
            await self._page.wait_for_timeout(1200)

    async def get_current_code(self, phone: str) -> str | None:
        """Baseline snapshot of the latest code BEFORE we trigger a new send."""
        await self._goto_messages()
        return await self._code_for_phone(phone)

    async def wait_for_code(self, phone: str, baseline_code: str | None = None) -> str | None:
        """Poll for a NEW code that differs from the baseline. Aborts early if the number
        never receives any SMS (bad number). ``last_status`` says why we gave up."""
        deadline = time.time() + self.config.timeout
        attempts = 0
        ever_found = False
        last_seen: str | None = None
        self.last_status = ""
        while time.time() < deadline:
            attempts += 1
            if self._page:
                await self._page.goto(JIVETEL_MESSAGES_URL, wait_until="domcontentloaded")
                await self._page.wait_for_timeout(1200)
            row, row_text = await self._find_row(phone)
            if row:
                ever_found = True
                code = await self._code_from(row, row_text)
                if code:
                    last_seen = code
                    if code != baseline_code:
                        return code
            elif not ever_found and attempts >= 6:
                self.last_status = "this number has no Ticketmaster messages in Jivetel"
                return None
            await asyncio.sleep(self.config.poll_interval)
        # Timeout: if TM resent the same digits, use them as a last resort.
        if ever_found and last_seen and last_seen == baseline_code:
            return last_seen
        self.last_status = ("no new code arrived for this number" if ever_found
                            else "this number has no Ticketmaster messages in Jivetel")
        return None

    async def close(self):
        for c in (self._context, self._browser):
            try:
                if c:
                    await c.close()
            except Exception:
                pass
        try:
            if self._pw:
                await self._pw.stop()
        except Exception:
            pass
        self._page = self._context = self._browser = self._pw = None
        self._logged_in = False
