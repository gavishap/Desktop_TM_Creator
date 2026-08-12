"""Local identity generation — names, street address, ZIP/city/state. No external API.

The VPS project used Gemini for names and the Sheet for addresses. Here everything is
derived on-device: a first/last name from the built-in pools (avoiding recent repeats and
any full name already in the database), a street address from a fixed pool, and a
ZIP/city/state matched to the account's phone area code via the ``zipcodes`` dataset, so
the profile geo is consistent with the number.
"""
from __future__ import annotations

import random
import re
from collections import Counter, deque

import phonenumbers
import zipcodes

from app.names import FIRST_NAMES, LAST_NAMES, STREET_ADDRESSES

_recent_first: deque[str] = deque(maxlen=100)
_recent_last: deque[str] = deque(maxlen=100)
_recent_addresses: deque[str] = deque(maxlen=150)

_area_index: dict[str, list[dict]] | None = None


def random_name(taken: set[str] | None = None) -> tuple[str, str]:
    """A first/last pair that avoids recent repeats and never reuses a full name in
    ``taken`` (the full names already stored), so two accounts are never twins."""
    taken = taken or set()
    for _ in range(60):
        first = random.choice([n for n in FIRST_NAMES if n not in _recent_first] or FIRST_NAMES)
        last = random.choice([n for n in LAST_NAMES if n not in _recent_last] or LAST_NAMES)
        if f"{first} {last}".lower() not in taken:
            break
    _recent_first.append(first)
    _recent_last.append(last)
    return first, last


def random_address() -> str:
    pool = [a for a in STREET_ADDRESSES if a not in _recent_addresses] or STREET_ADDRESSES
    address = random.choice(pool)
    _recent_addresses.append(address)
    return address


def normalize_zip(zip_code: str) -> str:
    zip_code = (zip_code or "").strip().lstrip("'")
    if not zip_code:
        return ""
    digits = re.sub(r"\D", "", zip_code.split("-")[0])
    if len(digits) == 4:
        return digits.zfill(5)
    if len(digits) >= 5:
        return digits[:5]
    return zip_code


def _get_area_index() -> dict[str, list[dict]]:
    global _area_index
    if _area_index is None:
        idx: dict[str, list[dict]] = {}
        for record in zipcodes.list_all():
            for ac in record.get("area_codes") or []:
                idx.setdefault(str(ac), []).append(record)
        _area_index = idx
    return _area_index


def _area_code(phone: str) -> str:
    digits = re.sub(r"\D", "", phone or "")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) >= 10:
        return digits[:3]
    try:
        national = str(phonenumbers.parse(phone, "US").national_number)
        return national[:3] if len(national) >= 10 else ""
    except Exception:
        return ""


def location_from_phone(phone: str) -> tuple[str, str, str]:
    """Return (zip, city, state) matched to the phone's area code; ('', '', '') if unknown."""
    try:
        ac = _area_code(phone)
        if not ac:
            return "", "", ""
        candidates = _get_area_index().get(ac, [])
        standard = [z for z in candidates if z.get("active") and z.get("zip_code_type") == "STANDARD"]
        pool = standard or candidates
        if not pool:
            return "", "", ""
        # The dataset tags a handful of out-of-state ZIPs with a borrowed area code (a few
        # NJ ZIPs carry 212). Keep only the state the area code really belongs to, so the
        # number and the ZIP never tell different stories.
        states = Counter(z.get("state") for z in pool)
        main_state = states.most_common(1)[0][0]
        pool = [z for z in pool if z.get("state") == main_state]
        rec = random.choice(pool)
        return normalize_zip(rec.get("zip_code", "")), rec.get("city", ""), rec.get("state", "")
    except Exception:
        return "", "", ""


def zip_matches_phone(zip_code: str, phone: str) -> bool:
    """True when the ZIP's own record lists the phone's area code."""
    ac = _area_code(phone)
    zip_code = normalize_zip(zip_code)
    if not ac or not zip_code:
        return False
    try:
        for rec in zipcodes.filter_by(zip_code=zip_code) or []:
            return ac in {str(x) for x in (rec.get("area_codes") or [])}
    except Exception:
        pass
    return False


def city_state_from_zip(zip_code: str) -> tuple[str, str]:
    try:
        for rec in zipcodes.filter_by(zip_code=normalize_zip(zip_code)) or []:
            return rec.get("city", ""), rec.get("state", "")
    except Exception:
        pass
    return "", ""


def fill_identity(account, taken_names: set[str] | None = None) -> None:
    """Fill in whatever the account is missing: name, street address, ZIP/city/state.

    Ticketmaster's form only asks for the ZIP, but the address and city/state are kept on
    the row because that is what the exported account list is expected to carry.
    """
    if not account.first_name or not account.last_name:
        f, l = random_name(taken_names)
        account.first_name = account.first_name or f
        account.last_name = account.last_name or l
    if not account.address:
        account.address = random_address()
    # The ZIP comes from the phone and nothing else. It is the only geo TM is given, so a
    # Boston number filed under a Manhattan ZIP is exactly the contradiction that reads as
    # fake. Nothing is invented before a number exists, and the ZIP is re-derived if the
    # number arrives later or changes — but a ZIP is never wiped just because the area code
    # is missing from the dataset.
    if account.phone and not zip_matches_phone(account.zip_code, account.phone):
        zip_code, city, state = location_from_phone(account.phone)
        if zip_code:
            account.zip_code, account.city, account.state = zip_code, city, state
    if account.zip_code and not account.city:
        # A ZIP was already set (or the lookup came up empty) — the city has to come from
        # that ZIP, otherwise the row claims a town that doesn't match its own ZIP.
        account.city, account.state = city_state_from_zip(account.zip_code)


def provision_account(store, email: str, created_from: str = "", imap_password: str = "",
                      phone: str = "") -> "object":
    """Create one ready-to-run account row: identity filled in and a phone number claimed
    from the pool. Every path that adds an account (paste, iCloud factory, Gmail
    generator) goes through here so no row ever lands half-built."""
    account_id = store.add_account(email, phone=phone, imap_password=imap_password,
                                   created_from=created_from)
    account = store.get(account_id)
    if not account.phone:
        account.phone = store.take_number(account_id)
    fill_identity(account, store.used_full_names())
    store.save_account(account)
    return account
