"""Bridge for verification codes that need a human.

When the flow needs an email or SMS code and can't read it automatically (no Gmail
app password, IMAP timed out, or it's an SMS the operator reads off their phone), it
awaits ``request_manual``. That opens a prompt in the UI and blocks on an asyncio
Future. The pywebview bridge calls ``submit`` from the main thread when the operator
types the code, which resolves the Future thread-safely.
"""
from __future__ import annotations

import asyncio
import uuid
from typing import Optional

from app.events import EventBus


class CodeBroker:
    def __init__(self, bus: EventBus):
        self.bus = bus
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._pending: dict[str, asyncio.Future] = {}

    def set_loop(self, loop: asyncio.AbstractEventLoop):
        self._loop = loop

    async def request_manual(self, account_id: int, kind: str, label: str,
                             timeout: float) -> Optional[str]:
        """kind = 'email' | 'sms'. Returns the operator-entered code, or None on timeout."""
        loop = asyncio.get_running_loop()
        prompt_id = uuid.uuid4().hex
        fut: asyncio.Future = loop.create_future()
        self._pending[prompt_id] = fut
        self.bus.open_prompt(prompt_id, account_id, kind, label)
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            return None
        finally:
            self._pending.pop(prompt_id, None)
            self.bus.close_prompt(prompt_id)

    def submit(self, prompt_id: str, code: str) -> bool:
        """Called from the UI thread. Resolves the waiting Future on the asyncio loop."""
        fut = self._pending.get(prompt_id)
        if not fut or fut.done() or not self._loop:
            return False
        self._loop.call_soon_threadsafe(fut.set_result, (code or "").strip())
        return True

    def cancel(self, prompt_id: str) -> bool:
        fut = self._pending.get(prompt_id)
        if not fut or fut.done() or not self._loop:
            return False
        self._loop.call_soon_threadsafe(fut.set_result, None)
        return True

    def cancel_all(self):
        for pid in list(self._pending):
            self.cancel(pid)
