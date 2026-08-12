"""Entry point — opens the native desktop window and wires up the API bridge.

Run from source:  python main.py
Frozen (.exe):     TM Desktop.exe  (double-click)
"""
from __future__ import annotations

import sys

import webview

from app.api import Api
from app.paths import WEB_DIR


def main():
    api = Api()
    webview.create_window(
        title="TM Desktop — Account Creator",
        # A plain path (not a file:// URI) makes pywebview serve the UI over a local HTTP
        # server. WebView2 refuses file:// URLs containing spaces, which would leave the
        # window blank for anyone who unzips into "C:\Users\John Smith\...".
        url=str(WEB_DIR / "index.html"),
        js_api=api,
        width=1180,
        height=820,
        min_size=(940, 640),
    )
    # gui=None lets pywebview pick the platform default (EdgeChromium on Windows).
    webview.start(debug="--debug" in sys.argv, http_server=True)


if __name__ == "__main__":
    main()
