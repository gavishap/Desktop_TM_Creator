"""Tiny thread-safe event bus shared between the automation thread and the UI.

The automation runs on a background asyncio thread; the pywebview bridge answers UI
polls on the main thread. Rather than push into JS (fragile across threads), the UI
polls ``snapshot()`` a few times a second. The bus holds a rolling log, the run flag,
and any open verification-code prompts the operator needs to answer.
"""
from __future__ import annotations

import threading
import time
from collections import deque


class EventBus:
    def __init__(self, max_logs: int = 400):
        self._lock = threading.Lock()
        self._logs: deque[dict] = deque(maxlen=max_logs)
        self._prompts: dict[str, dict] = {}
        self._running = False
        # Email-factory progress, shown in its own panel independent of the signup run.
        self._factory = {"running": False, "status": "idle", "created": 0, "target": 0}
        self._version = 0  # bumps on every change so the UI knows to re-render

    def _bump(self):
        self._version += 1

    # ── logging ────────────────────────────────────────────────────────────---
    def log(self, message: str, level: str = "info", account_id: int | None = None):
        with self._lock:
            self._logs.append({
                "t": time.strftime("%H:%M:%S"),
                "level": level,
                "account_id": account_id,
                "message": str(message)[:500],
            })
            self._bump()

    # ── run flag ───────────────────────────────────────────────────────────---
    def set_running(self, running: bool):
        with self._lock:
            self._running = running
            self._bump()

    @property
    def running(self) -> bool:
        with self._lock:
            return self._running

    # ── code prompts ──────────────────────────────────────────────────────---
    def open_prompt(self, prompt_id: str, account_id: int, kind: str, label: str):
        with self._lock:
            self._prompts[prompt_id] = {
                "prompt_id": prompt_id, "account_id": account_id,
                "kind": kind, "label": label, "opened_at": time.time(),
            }
            self._bump()

    def close_prompt(self, prompt_id: str):
        with self._lock:
            self._prompts.pop(prompt_id, None)
            self._bump()

    # ── email-factory progress ─────────────────────────────────────────────---
    def set_factory(self, running: bool | None = None, status: str | None = None,
                    created: int | None = None, target: int | None = None):
        with self._lock:
            if running is not None:
                self._factory["running"] = running
            if status is not None:
                self._factory["status"] = status
            if created is not None:
                self._factory["created"] = created
            if target is not None:
                self._factory["target"] = target
            self._bump()

    @property
    def factory_running(self) -> bool:
        with self._lock:
            return self._factory["running"]

    # ── snapshot for the UI ────────────────────────────────────────────────---
    def snapshot(self) -> dict:
        with self._lock:
            return {
                "version": self._version,
                "running": self._running,
                "factory": dict(self._factory),
                "logs": list(self._logs),
                "prompts": list(self._prompts.values()),
            }
