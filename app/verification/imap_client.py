"""Gmail IMAP reading for Ticketmaster email verification codes.

``ImapClient`` is one inbox connection. ``ImapPoller`` is what the run actually uses: it
logs into every inbox **once**, keeps the sockets alive, scans them on a single background
loop, and caches the codes it finds. Account flows then read the cache instead of opening
their own connections, so the number of IMAP logins is "one per inbox" rather than
"concurrent accounts x inboxes x attempts" — which is what makes Gmail start refusing.

Codes are matched to an account by the message's To: header, because an iCloud Hide My
Email alias forwards into whichever shared inbox the operator pointed it at.
"""
from __future__ import annotations

import asyncio
import datetime
import email
import email.utils
import imaplib
import time
from dataclasses import dataclass, field
from email.header import decode_header
from typing import Callable, Optional

from app.code_parser import extract_code

TM_SENDER_FRAGMENTS = ["ticketmaster", "noreply", "livenation"]
_SUBJECT_KEYWORDS = ["verification", "verify", "code", "confirm", "ticketmaster", "authentication"]


class ImapClient:
    def __init__(self, host: str, port: int, username: str, password: str,
                 poll_interval: int = 5, timeout: int = 150):
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.poll_interval = poll_interval
        self.timeout = timeout
        self._conn: Optional[imaplib.IMAP4_SSL] = None

    def connect(self):
        self._conn = imaplib.IMAP4_SSL(self.host, self.port)
        self._conn.login(self.username, self.password)

    def disconnect(self):
        if self._conn:
            try:
                self._conn.logout()
            except Exception:
                pass
            self._conn = None

    def noop(self) -> bool:
        try:
            self._conn.noop()
            return True
        except Exception:
            return False

    def _decode(self, raw: str) -> str:
        out = []
        for data, charset in decode_header(raw):
            if isinstance(data, bytes):
                out.append(data.decode(charset or "utf-8", errors="replace"))
            else:
                out.append(data)
        return " ".join(out)

    def _is_tm(self, msg) -> bool:
        frm = self._decode(msg.get("From", "")).lower()
        subj = self._decode(msg.get("Subject", "")).lower()
        return any(f in frm for f in TM_SENDER_FRAGMENTS) or any(k in subj for k in _SUBJECT_KEYWORDS)

    def _to_matches(self, msg, target: str) -> bool:
        return target.lower() in self._decode(msg.get("To", "")).lower()

    def _msg_ts(self, msg) -> float:
        try:
            return email.utils.parsedate_to_datetime(msg.get("Date", "")).timestamp()
        except Exception:
            return 0.0

    def _recent_uids(self, limit: int) -> list[bytes]:
        self._conn.select("INBOX")
        # One day back: the server reads SINCE in its own timezone, so asking for "today"
        # can miss mail the server still dates as yesterday.
        since = (datetime.date.today() - datetime.timedelta(days=1)).strftime("%d-%b-%Y")
        _, nums = self._conn.uid("search", None, f"SINCE {since}")
        if not nums or not nums[0]:
            return []
        return list(reversed(nums[0].split()[-limit:]))

    def _fetch_message(self, uid: bytes):
        _, data = self._conn.uid(
            "fetch", uid,
            "(BODY.PEEK[TEXT] BODY.PEEK[HEADER.FIELDS (FROM TO SUBJECT DATE)])",
        )
        if not data:
            return None, b""
        header_bytes = body_bytes = b""
        for part in data:
            if isinstance(part, tuple):
                if b"HEADER" in part[0]:
                    header_bytes = part[1]
                elif b"TEXT" in part[0]:
                    body_bytes = part[1]
        if not header_bytes and not body_bytes:
            return None, b""
        return email.message_from_bytes(header_bytes + b"\r\n" + body_bytes), body_bytes

    def scan_new(self, skip_uids: set, limit: int = 15) -> tuple[list, list[dict]]:
        """Read the newest messages we haven't seen and return any TM codes in them.

        Pure read: it mutates nothing shared and marks nothing seen — the poller owns the
        processed-UID set and the cache. A dead socket raises so the poller reconnects.
        """
        examined: list = []
        entries: list[dict] = []
        for uid in self._recent_uids(limit):
            if uid in skip_uids:
                continue
            examined.append(uid)
            msg, body_bytes = self._fetch_message(uid)
            if msg is None or not self._is_tm(msg):
                continue
            code = extract_code(body_bytes.decode("utf-8", errors="replace"))
            if not code:
                continue
            entries.append({
                "to_header": self._decode(msg.get("To", "")).lower(),
                "code": code,
                "email_ts": self._msg_ts(msg),
                "uid": uid,
            })
        return examined, entries

    def fetch_code(self, target_email: str, sent_after: float = 0) -> str | None:
        """One-shot read of a single inbox (used by the setup test, not by the run)."""
        try:
            if not self.noop():
                self.connect()
        except Exception:
            return None
        for uid in self._recent_uids(8):
            msg, body_bytes = self._fetch_message(uid)
            if msg is None or not self._is_tm(msg):
                continue
            if target_email and not self._to_matches(msg, target_email):
                continue
            if sent_after and (ts := self._msg_ts(msg)) and ts < (sent_after - 60):
                continue
            code = extract_code(body_bytes.decode("utf-8", errors="replace"))
            if code:
                try:
                    self._conn.uid("store", uid, "+FLAGS", "(\\Seen)")
                except Exception:
                    pass
                return code
        return None


@dataclass
class _CodeEntry:
    to_header: str
    code: str
    email_ts: float
    found_at: float
    source: str
    uid: bytes


@dataclass
class _Inbox:
    client: ImapClient
    connected: bool = False
    last_activity: float = 0.0
    next_login_attempt: float = 0.0
    processed_uids: set = field(default_factory=set)
    dead: bool = False  # auth permanently rejected — stop touching it until restart


class ImapPoller:
    """One long-lived, shared IMAP reader for the whole run.

    Exposes the same ``await wait_for_code(target_email, sent_after)`` the flow used to
    call on a per-account client, so the signup flow is unchanged.
    """

    CACHE_TTL = 1800        # keep a discovered code around this long
    LOOKUP_INTERVAL = 2.0   # how often wait_for_code re-checks the in-memory cache

    def __init__(self, host: str, port: int, accounts: list[dict], poll_interval: int = 5,
                 timeout: int = 150, keepalive_interval: int = 60,
                 relogin_backoff: int = 300, log: Optional[Callable] = None):
        self.host = host
        self.port = port
        self.poll_interval = poll_interval
        self.timeout = timeout
        self.keepalive_interval = keepalive_interval
        self.relogin_backoff = relogin_backoff
        self.log = log or (lambda *a, **k: None)
        self._inboxes: dict[str, _Inbox] = {}
        self._cache: list[_CodeEntry] = []
        self._task: Optional[asyncio.Task] = None
        self._running = False
        for a in accounts or []:
            self.add_inbox(a.get("username", ""), a.get("password", ""))

    @property
    def inbox_count(self) -> int:
        return len(self._inboxes)

    def add_inbox(self, username: str, password: str) -> None:
        """Register an inbox. Safe to call mid-run — the loop connects it next cycle."""
        username = (username or "").strip()
        if not username or not (password or "").strip() or username.lower() in self._inboxes:
            return
        self._inboxes[username.lower()] = _Inbox(client=ImapClient(
            self.host, self.port, username, password, self.poll_interval, self.timeout))

    async def start(self) -> None:
        if not self._inboxes:
            return
        self._running = True
        await asyncio.gather(*(self._ensure_connected(ib, initial=True)
                               for ib in list(self._inboxes.values())))
        live = sum(1 for ib in self._inboxes.values() if ib.connected)
        self.log(f"Email inboxes connected: {live}/{len(self._inboxes)}")
        self._task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        for ib in self._inboxes.values():
            await asyncio.to_thread(ib.client.disconnect)
            ib.connected = False

    # ── connections (every inbox-state write happens on the loop, never in a thread) ──
    @staticmethod
    def _do_connect(client: ImapClient) -> tuple[bool, str]:
        # Close-then-open so a stale socket is never leaked; leaked sockets are exactly
        # what triggers "too many simultaneous connections".
        try:
            client.disconnect()
        except Exception:
            pass
        try:
            client.connect()
            return True, ""
        except Exception as e:
            return False, f"{type(e).__name__}: {str(e)[:120]}"

    async def _ensure_connected(self, ib: _Inbox, initial: bool = False) -> bool:
        if ib.connected:
            return True
        now = time.time()
        if ib.dead or (not initial and now < ib.next_login_attempt):
            return False
        ok, err = await asyncio.to_thread(self._do_connect, ib.client)
        if ok:
            ib.connected = True
            ib.last_activity = now
        elif "AUTHENTICATIONFAILED" in err.upper():
            ib.dead = True
            self.log(f"Inbox {ib.client.username}: password rejected — skipping it. "
                     f"Fix the Gmail app password and restart.", "error")
        else:
            ib.next_login_attempt = now + self.relogin_backoff
            self.log(f"Inbox {ib.client.username} login failed: {err} "
                     f"(retrying in {self.relogin_backoff // 60}m)", "warn")
        return ib.connected

    async def _run_loop(self) -> None:
        while self._running:
            cycle_start = time.time()
            try:
                for ib in list(self._inboxes.values()):
                    await self._poll_inbox(ib)
                self._prune_cache()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                # Never let the loop die — that would starve every account of codes.
                self.log(f"Email poller hiccup: {type(e).__name__}: {str(e)[:100]}", "warn")
            await asyncio.sleep(max(1.0, self.poll_interval - (time.time() - cycle_start)))

    async def _poll_inbox(self, ib: _Inbox) -> None:
        if not await self._ensure_connected(ib):
            return
        if time.time() - ib.last_activity >= self.keepalive_interval:
            if not await asyncio.to_thread(ib.client.noop):
                ib.connected = False
                ib.next_login_attempt = 0.0  # dropped socket, reconnect immediately
                return
        try:
            examined, entries = await asyncio.to_thread(ib.client.scan_new, ib.processed_uids)
        except Exception as e:
            ib.connected = False
            ib.next_login_attempt = 0.0
            self.log(f"Inbox {ib.client.username} read error: {type(e).__name__}: "
                     f"{str(e)[:80]} — reconnecting", "warn")
            return
        ib.last_activity = time.time()
        ib.processed_uids.update(examined)
        src = ib.client.username
        for e in entries:
            if any(c.source == src and c.uid == e["uid"] for c in self._cache):
                continue
            self._cache.append(_CodeEntry(
                to_header=e["to_header"], code=e["code"], email_ts=e["email_ts"],
                found_at=time.time(), source=src, uid=e["uid"],
            ))

    def _prune_cache(self) -> None:
        cutoff = time.time() - self.CACHE_TTL
        if any(c.found_at < cutoff for c in self._cache):
            self._cache = [c for c in self._cache if c.found_at >= cutoff]

    # ── read side, used by the signup flow ────────────────────────────────────
    def _lookup(self, target_email: str, sent_after: float) -> Optional[str]:
        key = (target_email or "").lower()
        best: Optional[_CodeEntry] = None
        for c in self._cache:
            if key and key not in c.to_header:
                continue
            if sent_after and c.email_ts and c.email_ts < (sent_after - 60):
                continue
            if best is None or (c.email_ts, c.found_at) > (best.email_ts, best.found_at):
                best = c
        return best.code if best else None

    async def wait_for_code(self, target_email: str, sent_after: float = 0) -> Optional[str]:
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            code = self._lookup(target_email, sent_after)
            if code:
                return code
            await asyncio.sleep(self.LOOKUP_INTERVAL)
        return None
