"""TM Desktop — a fully self-contained Ticketmaster account creator.

Everything runs on the operator's own machine: their installed Chrome, their own
IP (no proxies), local SQLite storage (no Google Sheets), and a native desktop UI.
It mirrors the original VPS automation's signup flow but strips out every remote
dependency so it can be zipped and handed to anyone to run by double-clicking.
"""

__version__ = "1.0.0"
