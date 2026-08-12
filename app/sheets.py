"""Pushes finished work back to the office Google Sheet.

The sheet stays the record the wider team reads, so two things travel one way out
of the local database into it: an account row once it reaches verified (appended to
the accounts tab in the same layout the VPS app used), and every iCloud alias the
factory mints (appended to the pool_emails tab, same as the VPS factory did).

Nothing is ever read back into the database — the app is authoritative — and rows
that are still waiting are deliberately left out, because the VPS daemon claims any
accounts-tab row that isn't verified yet.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from app.storage import VERIFIED

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

POOL_TAB = "pool_emails"

# Header aliases, matching the VPS app so a sheet with any of these spellings works.
INPUT_COLUMNS = {
    "email": ["email", "e-mail", "email address"],
    "phone": ["phone", "phone number", "phone_number", "mobile"],
    "first_name": ["first_name", "first name", "firstname", "first"],
    "last_name": ["last_name", "last name", "lastname", "last"],
    "zip_code": ["zip", "zip_code", "zip code", "postal"],
    "imap_password": ["imap_password", "imap password", "imap_pass", "app password"],
}

OUTPUT_COLUMNS = [
    "tm_password", "email_verified", "phone_verified", "status", "error", "timestamp",
    "created_from", "address", "city", "state",
]


def _find_col(headers: list[str], aliases: list[str]) -> int | None:
    for i, h in enumerate(headers):
        if h and h.strip().lower() in aliases:
            return i
    return None


def _zip_for_sheet(zip_code: str) -> str:
    """Keep leading zeros intact — Sheets would otherwise eat them."""
    z = (zip_code or "").strip()
    return f"'{z}" if z.startswith("0") else z


def _next_free_row(rows: list, email_col: int = 0) -> int:
    """The row right after the last one that carries an email.

    Anchoring on the email column instead of "any non-empty cell" is deliberate. A write that
    half-lands (the office sheet collected 57 rows of stray address cells during a Sheets quota
    storm) would otherwise become the anchor and strand every later account hundreds of rows
    below the real data. Since rows are written full width, those leftovers get reclaimed.
    """
    last = 1
    for i, row in enumerate(rows[1:], start=2):
        if email_col < len(row) and row[email_col].strip():
            last = i
    return last + 1


class SheetSync:
    """One office sheet. Every method is blocking and safe to call off the UI thread."""

    def __init__(self, credentials_file: str, spreadsheet_id: str):
        self.credentials_file = credentials_file
        self.spreadsheet_id = spreadsheet_id
        self._ss = None
        self._accounts = None
        self._pool = None

    # ── connection ─────────────────────────────────────────────────────────---
    def _open(self):
        if self._ss is not None:
            return
        import gspread
        from google.oauth2.service_account import Credentials

        path = Path(self.credentials_file)
        if not path.exists():
            raise FileNotFoundError(f"Sheet credentials not found: {path}")
        creds = Credentials.from_service_account_file(str(path), scopes=SCOPES)
        self._ss = gspread.authorize(creds).open_by_key(self.spreadsheet_id)
        self._accounts = self._ss.sheet1

    def check(self) -> str:
        """Connect and report the sheet's name, so Settings can prove it works."""
        self._open()
        return f"{self._ss.title} / tab {self._accounts.title}"

    # ── accounts tab ───────────────────────────────────────────────────────---
    def push_verified(self, accounts: list) -> dict:
        """Append verified accounts that aren't on the sheet yet; refresh ones that are.

        Costs one read plus, at most, two writes no matter how many rows move.
        """
        self._open()
        ws = self._accounts
        rows = ws.get_all_values()
        headers = [str(h).strip() for h in (rows[0] if rows else [])]
        lower = [h.lower() for h in headers]

        inputs = {f: i for f, al in INPUT_COLUMNS.items()
                  if (i := _find_col(headers, al)) is not None}
        # Column A is the email column on every office sheet; on at least one of them the
        # header cell has been overwritten by hand, so don't insist on reading it.
        inputs.setdefault("email", 0)
        # The VPS pipelines refuse to read the tab at all without that header word, so put it
        # back — unless row 1 looks like an account, in which case the header row was deleted
        # and overwriting would destroy data.
        stale = headers[0].strip().lower() if headers else ""
        if stale != "email" and "@" not in stale:
            ws.update(values=[["email"]], range_name="A1", value_input_option="USER_ENTERED")

        outputs, appended_headers = {}, []
        next_col = len(headers)
        for name in OUTPUT_COLUMNS:
            if name in lower:
                outputs[name] = lower.index(name)
            else:
                outputs[name] = next_col
                appended_headers.append(name)
                next_col += 1
        if appended_headers:
            ws.update(values=[appended_headers], range_name=_a1(1, len(headers) + 1),
                      value_input_option="USER_ENTERED")

        email_col = inputs["email"]
        existing = {}
        for i, row in enumerate(rows[1:], start=2):
            if email_col < len(row) and (e := row[email_col].strip().lower()):
                existing.setdefault(e, i)
        start = _next_free_row(rows, email_col)

        width = max(next_col, len(headers))
        # The run's outcome is ours to state. Everything else on a row that already exists
        # is only ever filled in when blank, so a hand edit in the office is never clobbered.
        ours = {outputs[k] for k in ("status", "email_verified", "phone_verified")}
        updates, new_rows, touched = [], [], []

        for acct in accounts:
            cells = self._row_values(acct, inputs, outputs, width)
            row_idx = existing.get((acct.email or "").strip().lower())
            if row_idx is None:
                new_rows.append(cells)
                touched.append(start + len(new_rows) - 1)
                continue
            current = rows[row_idx - 1] if row_idx - 1 < len(rows) else []
            changes = []
            for col, value in enumerate(cells):
                if not value:
                    continue
                was = current[col].strip() if col < len(current) else ""
                if value == was or (was and col not in ours):
                    continue
                changes.append({"range": _a1(row_idx, col + 1), "values": [[value]]})
            if changes:
                updates += changes
                touched.append(row_idx)

        if new_rows:
            need = start + len(new_rows) - 1 - ws.row_count
            if need > 0:
                ws.add_rows(need + 100)
            updates.append({"range": f"A{start}", "values": new_rows})
        if updates:
            self._ss.values_batch_update(
                {"valueInputOption": "USER_ENTERED", "data": updates})
        if touched:
            self._highlight(ws, touched)

        return {"appended": len(new_rows), "updated": len(touched) - len(new_rows)}

    def _row_values(self, acct, inputs: dict, outputs: dict, width: int) -> list:
        cells = [""] * width
        for field, value in (
            ("email", acct.email),
            ("phone", acct.phone),
            ("first_name", acct.first_name),
            ("last_name", acct.last_name),
            ("zip_code", _zip_for_sheet(acct.zip_code)),
            ("imap_password", acct.imap_password),
        ):
            col = inputs.get(field)
            if col is not None and value:
                cells[col] = value
        stamp = _stamp(acct.verified_at or acct.updated_at)
        for field, value in (
            ("tm_password", acct.tm_password),
            ("email_verified", "Y" if acct.email_verified else "N"),
            ("phone_verified", "Y" if acct.phone_verified else "N"),
            ("status", acct.status),
            ("timestamp", stamp),
            ("created_from", acct.created_from),
            ("address", acct.address),
            ("city", acct.city),
            ("state", acct.state),
        ):
            col = outputs.get(field)
            if col is not None and value:
                cells[col] = value
        return cells

    def _highlight(self, ws, rows: list[int]) -> None:
        """Green out finished rows, the way the VPS app marked them."""
        green = {"red": 0.0, "green": 1.0, "blue": 0.0}
        requests = [{
            "repeatCell": {
                "range": {"sheetId": ws.id, "startRowIndex": r - 1, "endRowIndex": r,
                          "startColumnIndex": 0, "endColumnIndex": 26},
                "cell": {"userEnteredFormat": {"backgroundColor": green}},
                "fields": "userEnteredFormat.backgroundColor",
            },
        } for r in sorted(set(rows))]
        try:
            self._ss.batch_update({"requests": requests})
        except Exception:
            pass  # colour is cosmetic, never fail a sync over it

    # ── pool_emails tab ────────────────────────────────────────────────────---
    def push_pool(self, aliases: list) -> int:
        """Append newly minted iCloud aliases, and tick the ones that got used."""
        self._open()
        ws = self._pool_tab()
        rows = ws.get_all_values()
        seen = {}
        for i, row in enumerate(rows[1:], start=2):
            if row and (e := row[0].strip().lower()):
                seen[e] = (i, row[2].strip().upper() if len(row) > 2 else "")

        new_rows, ticks = [], []
        first_free = _next_free_row(rows)

        for a in aliases:
            key = a["email"].strip().lower()
            created = _stamp(a.get("created_at")) or ""
            if key not in seen:
                new_rows.append([a["email"], created, bool(a.get("used")),
                                 a.get("used_on") or "", a.get("created_from") or ""])
            elif a.get("used") and seen[key][1] not in ("TRUE", "Y", "YES"):
                ticks.append({"range": f"C{seen[key][0]}:D{seen[key][0]}",
                              "values": [[True, a.get("used_on") or ""]]})

        if new_rows:
            need = first_free + len(new_rows) - ws.row_count
            if need > 0:
                ws.add_rows(need + 100)
            ticks.append({"range": f"A{first_free}", "values": new_rows})
        if ticks:
            ws.batch_update(ticks, value_input_option="USER_ENTERED")
        return len(new_rows)

    def _pool_tab(self):
        import gspread
        if self._pool is not None:
            return self._pool
        try:
            ws = self._ss.worksheet(POOL_TAB)
        except gspread.WorksheetNotFound:
            ws = self._ss.add_worksheet(title=POOL_TAB, rows=500, cols=5)
        if not ws.row_values(1):
            ws.update(values=[["email", "created_at", "used", "used_on", "created_from"]],
                      range_name="A1:E1", value_input_option="USER_ENTERED")
        self._pool = ws
        return ws


def _a1(row: int, col: int) -> str:
    letters = ""
    while col:
        col, rem = divmod(col - 1, 26)
        letters = chr(65 + rem) + letters
    return f"{letters}{row}"


def _stamp(epoch) -> str:
    if not epoch:
        return ""
    return datetime.fromtimestamp(float(epoch), tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


class SheetSyncWorker:
    """Keeps the sheet current in the background, quietly and cheaply.

    Wakes on an interval, and only talks to Google when the local rows that belong
    on the sheet have actually changed since the last successful push.
    """

    def __init__(self, cfg, store, bus, interval: int = 90):
        self.cfg, self.store, self.bus = cfg, store, bus
        self.interval = interval
        self._pushed = None
        self._client: SheetSync | None = None
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_error = ""
        self.last_run = 0.0

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        self.nudge()   # catch up on whatever finished while the app was closed

    def nudge(self) -> None:
        self._wake.set()

    def _loop(self) -> None:
        while True:
            self._wake.wait(self.interval)
            self._wake.clear()
            try:
                self.sync()
            except Exception as e:
                self.last_error = f"{type(e).__name__}: {str(e)[:160]}"

    # ── the actual work ────────────────────────────────────────────────────---
    def _payload(self):
        verified = [a for a in self.store.all_accounts() if a.status == VERIFIED]
        return verified, self.store.list_pool()

    def sync(self, force: bool = False) -> dict:
        sheets = getattr(self.cfg, "sheets", None)
        if not sheets or not sheets.spreadsheet_id or not sheets.credentials_file:
            return {"ok": False, "error": "No Google Sheet configured for this team."}

        verified, aliases = self._payload()
        fingerprint = (
            tuple(sorted((a.email, a.status, a.tm_password or "") for a in verified)),
            tuple(sorted((a["email"], bool(a["used"])) for a in aliases)),
            sheets.spreadsheet_id,
        )
        if not force and fingerprint == self._pushed:
            return {"ok": True, "skipped": True}

        if (self._client is None
                or self._client.spreadsheet_id != sheets.spreadsheet_id):
            self._client = SheetSync(sheets.credentials_file, sheets.spreadsheet_id)
        client = self._client
        result = client.push_verified(verified) if verified else {"appended": 0, "updated": 0}
        pooled = client.push_pool(aliases) if aliases else 0

        self._pushed = fingerprint
        self.last_error = ""
        self.last_run = time.time()
        if result["appended"] or pooled:
            self.bus.log(
                f"Google Sheet: added {result['appended']} verified account(s) "
                f"and {pooled} iCloud alias(es).")
        return {"ok": True, "appended": result["appended"],
                "updated": result["updated"], "aliases": pooled}
