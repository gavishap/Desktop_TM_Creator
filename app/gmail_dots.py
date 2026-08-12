"""Gmail dot-alias generation (ported from the VPS ``email_aliases.py``).

Gmail ignores dots in the local part, so every dotted variant of an address
(``john.doe`` == ``jo.hndoe`` == ``johndoe``) lands in the same inbox. That lets one
Gmail back many signups that look distinct to Ticketmaster while all verification mail
arrives in one place. We use the same **single-dot** enumeration the Sheet used
(dot one letter in, two in, ...) so a base's variants are consumed in a stable order.
"""
from __future__ import annotations

_GMAIL_DOMAINS = {"gmail.com", "googlemail.com"}


def is_gmail(email: str) -> bool:
    return email.strip().lower().rpartition("@")[2] in _GMAIL_DOMAINS


def canonical_gmail(email: str) -> str:
    """Dotless, lower-cased Gmail address used to group variations."""
    local, _, domain = email.strip().lower().partition("@")
    if domain not in _GMAIL_DOMAINS:
        return email.strip().lower()
    return local.replace(".", "") + "@" + domain


def gmail_single_dot_variants(email: str) -> list[str]:
    """Every single-dot placement of the dotless local part, ordered 1 letter in, 2 in, ..."""
    local, _, domain = email.strip().lower().partition("@")
    if domain not in _GMAIL_DOMAINS:
        return []
    base = local.replace(".", "")
    if len(base) < 2:
        return []
    return [f"{base[:k]}.{base[k:]}@{domain}" for k in range(1, len(base))]


def next_unused_variants(base_email: str, taken: set[str], n: int) -> list[str]:
    """Up to ``n`` single-dot variants of ``base_email`` not already in ``taken``."""
    out = []
    for v in gmail_single_dot_variants(base_email):
        if v not in taken:
            out.append(v)
            if len(out) >= n:
                break
    return out
