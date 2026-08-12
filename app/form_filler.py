"""Human-like form interaction (typing cadence, cursor motion, dwell).

Copied from the original project and re-pointed at the desktop ``BrowserConfig``.
The wander/dwell behaviour matters: TM's auth page runs NuData behavioral biometrics,
and a motionless, instant submit scores as a bot.
"""
from __future__ import annotations

import asyncio
import random

from playwright.async_api import Page

from app.config import BrowserConfig


class FormFiller:
    def __init__(self, page: Page, config: BrowserConfig):
        self.page = page
        self.config = config

    async def type_text(self, selector: str, text: str, clear_first: bool = True):
        el = await self.page.wait_for_selector(selector, timeout=30000)
        if not el:
            raise RuntimeError(f"Field not found: {selector}")
        await self._move_to_and_click(selector)
        await asyncio.sleep(random.uniform(0.1, 0.2))
        if clear_first:
            await self.page.keyboard.press("Control+a")
            await asyncio.sleep(random.uniform(0.02, 0.08))
            await self.page.keyboard.press("Backspace")
            await asyncio.sleep(random.uniform(0.05, 0.1))
        await self._type_chars(text)

    async def type_text_on_element(self, el, text: str, clear_first: bool = True):
        box = await el.bounding_box()
        if box:
            x = box["x"] + box["width"] * random.uniform(0.25, 0.75)
            y = box["y"] + box["height"] * random.uniform(0.25, 0.75)
            await self.page.mouse.move(x, y, steps=random.randint(3, 8))
            await asyncio.sleep(random.uniform(0.02, 0.08))
        await el.click()
        await asyncio.sleep(random.uniform(0.1, 0.2))
        if clear_first:
            await self.page.keyboard.press("Control+a")
            await asyncio.sleep(random.uniform(0.02, 0.08))
            await self.page.keyboard.press("Backspace")
            await asyncio.sleep(random.uniform(0.05, 0.1))
        await self._type_chars(text)

    async def _type_chars(self, text: str):
        for char in text:
            delay_ms = random.randint(self.config.typing_delay_min, self.config.typing_delay_max)
            await self.page.keyboard.type(char, delay=delay_ms)
            if random.random() < 0.02:
                await asyncio.sleep(random.uniform(0.1, 0.3))

    async def click(self, selector: str):
        await self._move_to_and_click(selector)

    async def _move_to_and_click(self, selector: str):
        el = await self.page.wait_for_selector(selector, timeout=15000)
        if not el:
            raise RuntimeError(f"Element not found: {selector}")
        box = await el.bounding_box()
        if box:
            x = box["x"] + box["width"] * random.uniform(0.25, 0.75)
            y = box["y"] + box["height"] * random.uniform(0.25, 0.75)
            await self.page.mouse.move(x, y, steps=random.randint(3, 8))
            await asyncio.sleep(random.uniform(0.02, 0.08))
        await el.click()

    async def pause(self, min_s: float | None = None, max_s: float | None = None):
        lo = min_s or self.config.action_delay_min
        hi = max_s or self.config.action_delay_max
        await asyncio.sleep(random.uniform(lo, hi))

    async def wander_mouse(self, n_moves: int = 4):
        vp = self.page.viewport_size or {"width": 1280, "height": 800}
        for _ in range(n_moves):
            tx = random.uniform(vp["width"] * 0.1, vp["width"] * 0.9)
            ty = random.uniform(vp["height"] * 0.1, vp["height"] * 0.9)
            await self.page.mouse.move(tx, ty, steps=random.randint(12, 30))
            await asyncio.sleep(random.uniform(0.12, 0.45))

    async def human_dwell(self, min_s: float, max_s: float):
        loop = asyncio.get_event_loop()
        deadline = loop.time() + random.uniform(min_s, max_s)
        while loop.time() < deadline:
            await self.wander_mouse(random.randint(1, 3))
            if random.random() < 0.4:
                await self.page.mouse.wheel(0, random.randint(80, 300))
                await asyncio.sleep(random.uniform(0.3, 0.8))
                await self.page.mouse.wheel(0, -random.randint(40, 150))
            await asyncio.sleep(random.uniform(0.2, 0.6))
