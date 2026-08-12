"""Filesystem locations for the app's data, resolved so they work both when running
from source and when frozen into a PyInstaller .exe.

- ``DATA_DIR``   per-user writable folder (DB, logs, screenshots, Chrome profile).
- ``RESOURCE_DIR`` read-only bundled assets (the web/ UI). Under PyInstaller this is
  the temp extraction dir (``sys._MEIPASS``); from source it is the project root.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path


def _resource_dir() -> Path:
    # PyInstaller unpacks bundled data into a temp dir exposed as sys._MEIPASS.
    base = getattr(sys, "_MEIPASS", None)
    if base:
        return Path(base)
    return Path(__file__).resolve().parent.parent


def _data_dir() -> Path:
    # Keep user data out of the (read-only when frozen) install dir: use %LOCALAPPDATA%.
    root = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or str(Path.home())
    d = Path(root) / "TMDesktop"
    d.mkdir(parents=True, exist_ok=True)
    return d


RESOURCE_DIR = _resource_dir()
WEB_DIR = RESOURCE_DIR / "web"

DATA_DIR = _data_dir()
DB_PATH = DATA_DIR / "tmdesktop.sqlite3"
CONFIG_PATH = DATA_DIR / "config.json"
SCREENSHOT_DIR = DATA_DIR / "screenshots"
LOG_DIR = DATA_DIR / "logs"
CHROME_PROFILE_DIR = DATA_DIR / "chrome-profile"

for _d in (SCREENSHOT_DIR, LOG_DIR):
    _d.mkdir(parents=True, exist_ok=True)
