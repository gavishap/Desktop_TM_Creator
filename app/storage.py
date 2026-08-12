"""Local SQLite store — replaces the Google Sheet as the work queue + results log.

One table, ``accounts``. Each row is one Ticketmaster account to create. The UI adds
rows; the orchestrator picks up any row whose status is pending/needs_retry and writes
back progress, the generated TM password, and the final status/error. Everything stays
on the operator's disk (``%LOCALAPPDATA%/TMDesktop/tmdesktop.sqlite3``).
"""
from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from app.paths import DB_PATH

# Terminal statuses the orchestrator will not re-pick.
PENDING = "pending"
IN_PROGRESS = "in_progress"
EMAIL_VERIFIED = "email_verified"
PHONE_VERIFIED = "phone_verified"
VERIFIED = "verified"
FAILED = "failed"
NEEDS_RETRY = "needs_retry"

RUNNABLE = (PENDING, NEEDS_RETRY)
TERMINAL = (VERIFIED,)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS accounts (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    email          TEXT NOT NULL,
    phone          TEXT NOT NULL DEFAULT '',
    imap_password  TEXT NOT NULL DEFAULT '',
    first_name     TEXT NOT NULL DEFAULT '',
    last_name      TEXT NOT NULL DEFAULT '',
    address        TEXT NOT NULL DEFAULT '',
    city           TEXT NOT NULL DEFAULT '',
    state          TEXT NOT NULL DEFAULT '',
    zip_code       TEXT NOT NULL DEFAULT '',
    tm_password    TEXT NOT NULL DEFAULT '',
    created_from   TEXT NOT NULL DEFAULT '',
    status         TEXT NOT NULL DEFAULT 'pending',
    email_verified INTEGER NOT NULL DEFAULT 0,
    phone_verified INTEGER NOT NULL DEFAULT 0,
    error          TEXT NOT NULL DEFAULT '',
    attempts       INTEGER NOT NULL DEFAULT 0,
    throttle_strikes INTEGER NOT NULL DEFAULT 0,
    retry_after    REAL NOT NULL DEFAULT 0,
    created_at     REAL NOT NULL DEFAULT 0,
    updated_at     REAL NOT NULL DEFAULT 0,
    verified_at    REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS pool_emails (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    email        TEXT NOT NULL UNIQUE,
    created_from TEXT NOT NULL DEFAULT '',
    label        TEXT NOT NULL DEFAULT '',
    queued       INTEGER NOT NULL DEFAULT 0,
    used         INTEGER NOT NULL DEFAULT 0,
    used_on      TEXT NOT NULL DEFAULT '',
    created_at   REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS numbers (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    phone       TEXT NOT NULL UNIQUE,
    used        INTEGER NOT NULL DEFAULT 0,
    account_id  INTEGER,
    created_at  REAL NOT NULL DEFAULT 0
);
"""

# Columns added after the first release — applied to databases created by older builds.
_MIGRATIONS = {
    "accounts": {
        "address": "TEXT NOT NULL DEFAULT ''",
        "city": "TEXT NOT NULL DEFAULT ''",
        "state": "TEXT NOT NULL DEFAULT ''",
        "created_from": "TEXT NOT NULL DEFAULT ''",
        "throttle_strikes": "INTEGER NOT NULL DEFAULT 0",
        "retry_after": "REAL NOT NULL DEFAULT 0",
        "verified_at": "REAL NOT NULL DEFAULT 0",
    },
    "pool_emails": {
        "queued": "INTEGER NOT NULL DEFAULT 0",
        "used_on": "TEXT NOT NULL DEFAULT ''",
    },
}


@dataclass
class Account:
    email: str
    phone: str = ""
    imap_password: str = ""
    first_name: str = ""
    last_name: str = ""
    address: str = ""
    city: str = ""
    state: str = ""
    zip_code: str = ""
    tm_password: str = ""
    created_from: str = ""
    status: str = PENDING
    email_verified: bool = False
    phone_verified: bool = False
    error: str = ""
    attempts: int = 0
    throttle_strikes: int = 0
    retry_after: float = 0.0
    id: int = 0
    created_at: float = 0.0
    updated_at: float = 0.0
    verified_at: float = 0.0

    def as_dict(self) -> dict:
        return {
            "id": self.id, "email": self.email, "phone": self.phone,
            "first_name": self.first_name, "last_name": self.last_name,
            "address": self.address, "city": self.city, "state": self.state,
            "zip_code": self.zip_code, "tm_password": self.tm_password,
            "created_from": self.created_from,
            "status": self.status, "email_verified": self.email_verified,
            "phone_verified": self.phone_verified, "error": self.error,
            "attempts": self.attempts, "has_imap": bool(self.imap_password),
            "retry_after": self.retry_after, "throttle_strikes": self.throttle_strikes,
            "created_at": self.created_at, "verified_at": self.verified_at,
        }


class Store:
    """Thread-safe SQLite wrapper. The automation runs on a background thread and the
    UI bridge on the main thread, so every call grabs a lock and its own cursor."""

    def __init__(self, path=DB_PATH):
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.executescript(_SCHEMA)
        self._migrate()
        self._conn.commit()
        self._lock = threading.Lock()

    def _migrate(self) -> None:
        for table, columns in _MIGRATIONS.items():
            have = {r["name"] for r in self._conn.execute(f"PRAGMA table_info({table})")}
            for name, ddl in columns.items():
                if name not in have:
                    self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")

    # ── writes ────────────────────────────────────────────────────────────────
    def add_account(self, email: str, phone: str = "", imap_password: str = "",
                    first_name: str = "", last_name: str = "", zip_code: str = "",
                    created_from: str = "") -> int:
        now = time.time()
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO accounts (email, phone, imap_password, first_name, last_name, "
                "zip_code, created_from, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (email.strip(), phone.strip(), imap_password.strip(),
                 first_name, last_name, zip_code, created_from, now, now),
            )
            self._conn.commit()
            return cur.lastrowid

    def save_account(self, a: Account) -> None:
        a.updated_at = time.time()
        with self._lock:
            self._conn.execute(
                "UPDATE accounts SET email=?, phone=?, imap_password=?, first_name=?, "
                "last_name=?, address=?, city=?, state=?, zip_code=?, tm_password=?, "
                "created_from=?, status=?, email_verified=?, phone_verified=?, error=?, "
                "attempts=?, throttle_strikes=?, retry_after=?, updated_at=?, created_at=?, "
                "verified_at=? WHERE id=?",
                (a.email, a.phone, a.imap_password, a.first_name, a.last_name, a.address,
                 a.city, a.state, a.zip_code, a.tm_password, a.created_from, a.status,
                 int(a.email_verified), int(a.phone_verified), a.error, a.attempts,
                 a.throttle_strikes, a.retry_after, a.updated_at, a.created_at,
                 a.verified_at, a.id),
            )
            self._conn.commit()

    def reset_account(self, account_id: int) -> None:
        """Clear status/error so the row is re-picked (mirror of the Sheet's retest recipe)."""
        with self._lock:
            self._conn.execute(
                "UPDATE accounts SET status=?, error='', email_verified=0, phone_verified=0, "
                "throttle_strikes=0, retry_after=0, updated_at=? WHERE id=?",
                (PENDING, time.time(), account_id),
            )
            self._conn.commit()

    def reset_by_status(self, statuses: tuple) -> int:
        """Bulk retest — the local stand-in for ``scripts/reset_rows.py --status failed``."""
        if not statuses:
            return 0
        marks = ",".join("?" * len(statuses))
        with self._lock:
            cur = self._conn.execute(
                f"UPDATE accounts SET status=?, error='', email_verified=0, phone_verified=0, "
                f"throttle_strikes=0, retry_after=0, updated_at=? WHERE status IN ({marks})",
                (PENDING, time.time(), *statuses),
            )
            self._conn.commit()
            return cur.rowcount

    def recover_stale(self) -> int:
        """Rows left mid-flight by a crash/force-close: re-queue them at startup."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE accounts SET status=?, error='interrupted', retry_after=0, updated_at=? "
                "WHERE status=?",
                (NEEDS_RETRY, time.time(), IN_PROGRESS),
            )
            self._conn.commit()
            return cur.rowcount

    def delete_account(self, account_id: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM accounts WHERE id=?", (account_id,))
            self._conn.commit()

    # ── reads ───────────────────────────────────────────────────────────────--
    def _row_to_account(self, r: sqlite3.Row) -> Account:
        return Account(
            id=r["id"], email=r["email"], phone=r["phone"], imap_password=r["imap_password"],
            first_name=r["first_name"], last_name=r["last_name"], address=r["address"],
            city=r["city"], state=r["state"], zip_code=r["zip_code"],
            tm_password=r["tm_password"], created_from=r["created_from"], status=r["status"],
            email_verified=bool(r["email_verified"]), phone_verified=bool(r["phone_verified"]),
            error=r["error"], attempts=r["attempts"],
            throttle_strikes=r["throttle_strikes"], retry_after=r["retry_after"],
            created_at=r["created_at"], updated_at=r["updated_at"],
            verified_at=r["verified_at"],
        )

    def get(self, account_id: int) -> Optional[Account]:
        with self._lock:
            r = self._conn.execute("SELECT * FROM accounts WHERE id=?", (account_id,)).fetchone()
        return self._row_to_account(r) if r else None

    def all_accounts(self) -> list[Account]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM accounts ORDER BY id").fetchall()
        return [self._row_to_account(r) for r in rows]

    def runnable(self) -> list[Account]:
        """Rows ready to run right now — a throttled row stays out until its wait is up."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM accounts WHERE status IN (?,?) AND email!='' "
                "AND retry_after<=? ORDER BY id",
                (*RUNNABLE, time.time()),
            ).fetchall()
        return [self._row_to_account(r) for r in rows]

    def next_retry_at(self, account_ids: Optional[list[int]] = None) -> float:
        """Earliest future ``retry_after`` among scheduled rows; 0 when nothing is waiting."""
        sql = ("SELECT MIN(retry_after) m FROM accounts WHERE status IN (?,?) "
               "AND email!='' AND retry_after>?")
        args: list = [*RUNNABLE, time.time()]
        if account_ids:
            sql += f" AND id IN ({','.join('?' * len(account_ids))})"
            args += list(account_ids)
        with self._lock:
            r = self._conn.execute(sql, args).fetchone()
        return r["m"] or 0.0

    def used_full_names(self) -> set[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT first_name, last_name FROM accounts "
                "WHERE first_name!='' AND last_name!=''").fetchall()
        return {f"{r['first_name']} {r['last_name']}".lower() for r in rows}

    # ── email-factory pool ──────────────────────────────────────────────────--
    def add_pool_email(self, email: str, created_from: str = "", label: str = "") -> bool:
        """Store a freshly minted HME alias. Returns False if it already exists."""
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO pool_emails (email, created_from, label, created_at) "
                    "VALUES (?,?,?,?)",
                    (email.strip(), created_from.strip(), label.strip(), time.time()),
                )
                self._conn.commit()
                return True
            except sqlite3.IntegrityError:
                return False

    def list_pool(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM pool_emails ORDER BY id DESC").fetchall()
        return [dict(r) for r in rows]

    def count_pool(self, used: Optional[bool] = None, queued: Optional[bool] = None) -> int:
        where, args = [], []
        if used is not None:
            where.append("used=?")
            args.append(int(used))
        if queued is not None:
            where.append("queued=?")
            args.append(int(queued))
        sql = "SELECT COUNT(*) c FROM pool_emails"
        if where:
            sql += " WHERE " + " AND ".join(where)
        with self._lock:
            r = self._conn.execute(sql, args).fetchone()
        return r["c"]

    def delete_pool_email(self, pool_id: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM pool_emails WHERE id=?", (pool_id,))
            self._conn.commit()

    def mark_pool_queued(self, email: str) -> None:
        """An account row now exists for this alias (it is spoken for, not yet proven)."""
        with self._lock:
            self._conn.execute(
                "UPDATE pool_emails SET queued=1 WHERE lower(email)=?", (email.strip().lower(),))
            self._conn.commit()

    def mark_pool_used(self, email: str) -> None:
        """The alias produced a verified Ticketmaster account — stamp it like the Sheet did."""
        with self._lock:
            self._conn.execute(
                "UPDATE pool_emails SET used=1, queued=1, used_on=? WHERE lower(email)=?",
                (time.strftime("%Y-%m-%d"), email.strip().lower()),
            )
            self._conn.commit()

    # ── phone-number pool ───────────────────────────────────────────────────-
    def add_number(self, phone: str) -> bool:
        with self._lock:
            try:
                self._conn.execute(
                    "INSERT INTO numbers (phone, created_at) VALUES (?,?)",
                    (phone.strip(), time.time()))
                self._conn.commit()
                return True
            except sqlite3.IntegrityError:
                return False

    def list_numbers(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM numbers ORDER BY id").fetchall()
        return [dict(r) for r in rows]

    def unused_numbers_count(self) -> int:
        with self._lock:
            r = self._conn.execute("SELECT COUNT(*) c FROM numbers WHERE used=0").fetchone()
        return r["c"]

    def take_number(self, account_id: int) -> str:
        """Claim the oldest unused number for an account. Returns '' if none free."""
        with self._lock:
            r = self._conn.execute(
                "SELECT id, phone FROM numbers WHERE used=0 ORDER BY id LIMIT 1").fetchone()
            if not r:
                return ""
            self._conn.execute("UPDATE numbers SET used=1, account_id=? WHERE id=?",
                               (account_id, r["id"]))
            self._conn.commit()
            return r["phone"]

    def claim_number(self, phone: str, account_id: int) -> None:
        """Tie a specific number to a specific account (used when restoring a pool)."""
        with self._lock:
            self._conn.execute("UPDATE numbers SET used=1, account_id=? WHERE phone=?",
                               (account_id, phone.strip()))
            self._conn.commit()

    def delete_number(self, number_id: int) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM numbers WHERE id=?", (number_id,))
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()
