"""Pick up the team's credentials from the folder the app was handed over in.

Each office gets a copy of the ``ticketmaster-accounts`` project with the .exe dropped in
it, and that project already carries their logins in ``config/settings*.yaml`` — the same
files the VPS ran on. Rather than making someone retype a Jivetel login, four Gmail app
passwords and two Apple IDs on a brand-new laptop, the app finds that file on first run
and seeds itself from it.

Only the credentials are taken. Everything the VPS needed and the desktop app doesn't
(proxies, Google Sheets, Gemini, Xvfb) is ignored, and run tuning stays at the desktop
defaults, because five parallel Chrome windows on one home connection is a VPS setting,
not a laptop one.
"""
from __future__ import annotations

import sys
from pathlib import Path

import yaml

_FILE_GLOB = "settings*.yaml"
_SKIP = ("example",)   # settings.example.yaml is a template, not real credentials

# HME caps/delays the offices tuned on the VPS to keep Apple from flagging the Apple ID.
_ICLOUD_PACING = ("sms_code_timeout", "per_email_delay", "generate_reserve_gap",
                  "batch_size", "batch_cooldown", "hourly_cap_per_account",
                  "daily_cap_per_account", "default_cooldown_wait",
                  "escalated_cooldown_wait")


def _app_dir() -> Path:
    """The folder the app lives in — next to the .exe once frozen."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def _search_roots() -> list[Path]:
    """The app's own folder, then a few levels up, so the .exe finds the project's config
    whether it sits in the project root or a couple of folders deep inside it."""
    roots: list[Path] = []
    base = _app_dir()
    for cand in [base, *list(base.parents)[:3], Path.cwd()]:
        if cand not in roots:
            roots.append(cand)
    return roots


def find_settings_files() -> list[Path]:
    """Every credentials file we can see, nearest first, without duplicates."""
    found: list[Path] = []
    for root in _search_roots():
        for folder in (root / "config", root):
            if not folder.is_dir():
                continue
            for path in sorted(folder.glob(_FILE_GLOB)):
                if any(s in path.name.lower() for s in _SKIP):
                    continue
                if path.resolve() not in [f.resolve() for f in found]:
                    found.append(path)
    return found


def read_settings(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def describe(path: Path) -> dict:
    """Summarise a credentials file so an operator can tell whose team it belongs to.

    Filenames like ``settings_team2.yaml`` mean nothing to the person using the app, so the
    picker shows the logins instead: those are what they recognise.
    """
    try:
        data = read_settings(path)
    except Exception:
        return {"path": str(path), "name": path.name, "readable": False,
                "jivetel": "", "apple_ids": [], "inboxes": []}
    imap = data.get("imap") or {}
    inboxes = [a.get("username", "") for a in (imap.get("accounts") or [])
               if a.get("username") and not a["username"].lower().endswith("@icloud.com")]
    if not inboxes and imap.get("username"):
        inboxes = [imap["username"]]
    return {
        "path": str(path),
        "name": path.name,
        "readable": True,
        "jivetel": (data.get("jivetel") or {}).get("username", ""),
        "apple_ids": [a.get("email", "") for a in ((data.get("icloud") or {}).get("accounts") or [])],
        "inboxes": inboxes,
    }


def apply_settings(config, data: dict, path=None) -> dict:
    """Copy the credentials out of a VPS settings file into the app config.

    Returns a count of what was taken, so the operator can be told in plain words.
    """
    took = {"inboxes": 0, "apple_ids": 0, "jivetel": False}

    imap = data.get("imap") or {}
    inboxes = []
    for entry in imap.get("accounts") or []:
        user, pw = (entry.get("username") or "").strip(), (entry.get("password") or "").strip()
        # iCloud mailboxes are blocked by Apple and must never be polled; Gmail only.
        if user and pw and not user.lower().endswith("@icloud.com"):
            inboxes.append({"username": user, "password": pw})
    # Older files put a single inbox at the top level instead of under accounts.
    if not inboxes and imap.get("username") and imap.get("password"):
        inboxes = [{"username": imap["username"].strip(), "password": imap["password"].strip()}]
    if inboxes:
        config.imap.accounts = inboxes
        config.imap.host = imap.get("host") or config.imap.host
        config.imap.port = int(imap.get("port") or config.imap.port)
        took["inboxes"] = len(inboxes)

    jv = data.get("jivetel") or {}
    if jv.get("username") and jv.get("password"):
        config.jivetel.username = jv["username"].strip()
        config.jivetel.password = jv["password"]
        config.jivetel.portal_url = jv.get("portal_url") or config.jivetel.portal_url
        config.jivetel.enabled = True
        took["jivetel"] = True

    ic = data.get("icloud") or {}
    apple = [{"email": a["email"].strip(), "password": a["password"]}
             for a in (ic.get("accounts") or []) if a.get("email") and a.get("password")]
    if apple:
        config.icloud.accounts = apple
        config.icloud.enabled = True
        # Apple's tolerance doesn't care what machine the calls come from, so the office's
        # own HME pacing travels with their file. (Contrast max_concurrent, which stays at
        # the desktop default: five parallel Chrome windows is a VPS number, not a laptop's.)
        for key in _ICLOUD_PACING:
            if ic.get(key) is not None:
                current = getattr(config.icloud, key)
                setattr(config.icloud, key, type(current)(ic[key]))
        took["apple_ids"] = len(apple)

    took["sheet"] = _apply_sheet(config, data.get("sheets") or {}, path)

    if any((took["inboxes"], took["apple_ids"], took["jivetel"])):
        if path:
            config.credentials_file = str(path)
        config.save()
    return took


def _apply_sheet(config, sheets: dict, path) -> bool:
    """Point the app at this office's own Google Sheet.

    The three offices share one service account but have separate spreadsheets, so the
    id travels with the team file. The key file itself is referenced relative to the
    project root in those files, which is meaningless from wherever the .exe was
    started, so resolve it against the folder the team file was actually found in.
    """
    sheet_id = (sheets.get("spreadsheet_id") or "").strip()
    if not sheet_id:
        return False
    config.sheets.spreadsheet_id = sheet_id

    named = Path(sheets.get("credentials_file") or "config/service_account.json")
    roots = [Path(path).parent, Path(path).parent.parent] if path else []
    for cand in [named, *(r / named.name for r in roots), *(r / named for r in roots),
                 *(r / "config" / named.name for r in _search_roots())]:
        if cand.is_file():
            config.sheets.credentials_file = str(cand.resolve())
            return True
    return False


def ensure_sheet(config) -> str:
    """Fill in the sheet link for an install that was configured before it existed."""
    if config.sheets.spreadsheet_id and config.sheets.credentials_file:
        return ""
    path = Path(config.credentials_file) if config.credentials_file else None
    if not path or not path.is_file():
        return ""
    try:
        data = read_settings(path)
    except Exception:
        return ""
    if not _apply_sheet(config, data.get("sheets") or {}, path):
        return ""
    config.save()
    return config.sheets.spreadsheet_id


def accounts_file(settings_path) -> Path | None:
    """A team's already-created accounts, shipped beside their credentials file.

    ``config/settings.yaml`` -> ``config/settings.accounts.csv``. Same columns as the app's
    own Export CSV, so a machine that has the history can hand it to one that doesn't.
    """
    p = Path(settings_path)
    csv_path = p.parent / (p.stem + ".accounts.csv")
    return csv_path if csv_path.is_file() else None


def has_credentials(config) -> bool:
    return bool(config.imap.accounts or config.jivetel.username or config.icloud.accounts)


def seed_if_empty(config) -> tuple[str, dict] | None:
    """First run on a new machine: load the credentials file automatically.

    Only when there is exactly one. If the folder carries every team's file, guessing would
    silently sign someone in as the wrong office, so the app asks instead (see
    ``pending_choices``).
    """
    if has_credentials(config):
        return None
    files = find_settings_files()
    if len(files) != 1:
        return None
    try:
        took = apply_settings(config, read_settings(files[0]), files[0])
    except Exception:
        return None
    if any((took["inboxes"], took["apple_ids"], took["jivetel"])):
        return str(files[0]), took
    return None


def relink_file(config) -> str:
    """Reconnect an install that was configured before the app tracked its team file.

    Without that link nothing is written back to the project folder, so finished accounts
    would only ever live in this machine's database. Match the configured logins against
    the files present and record the one that fits; stay out of it if it is ambiguous.
    """
    if config.credentials_file or not has_credentials(config):
        return ""
    jivetel = (config.jivetel.username or "").strip().lower()
    inboxes = {a.get("username", "").strip().lower() for a in config.imap.accounts}
    matches = [p for p in find_settings_files()
               if (lambda d: (jivetel and d["jivetel"].strip().lower() == jivetel)
                   or bool(inboxes & {i.strip().lower() for i in d["inboxes"]}))(describe(p))]
    if len(matches) != 1:
        return ""
    config.credentials_file = str(matches[0])
    config.save()
    return str(matches[0])


def pending_choices(config) -> list[dict]:
    """The teams to offer at startup — empty once anything is configured."""
    if has_credentials(config):
        return []
    files = find_settings_files()
    return [describe(p) for p in files] if len(files) > 1 else []
