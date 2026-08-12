"""Extract 6-digit verification codes from email bodies (copied from the original)."""
from __future__ import annotations

import re

_CODE_PATTERNS = [
    re.compile(r"\b(\d{6})\b"),
    re.compile(r"code[:\s]+(\d{6})", re.IGNORECASE),
    re.compile(r"(\d{6})\s+is your Ticketmaster code", re.IGNORECASE),
    re.compile(r"verification[:\s]+(\d{6})", re.IGNORECASE),
    re.compile(r"(\d{6})\s+is your.*code", re.IGNORECASE),
]


def extract_code(text: str) -> str | None:
    for pattern in _CODE_PATTERNS[1:]:
        m = pattern.search(text)
        if m:
            return m.group(1)
    m = _CODE_PATTERNS[0].search(text)
    return m.group(1) if m else None
