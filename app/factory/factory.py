"""iCloud Hide My Email factory — desktop port of the VPS ``ApiFactory``.

Mints @icloud.com aliases through Apple's HME API (``icloud-hme``), no browser and no
Google Sheet. It runs on its own background thread (all its work is blocking HTTP +
sleeps, so no asyncio here), writes each alias into the local ``pool_emails`` table,
and reports progress through the shared ``EventBus``. 2FA codes are collected from the
UI via a per-request queue (the operator reads the code Apple texts/pushes to their
device and types it into the same prompt used for signup codes).

The per-account hourly/daily caps, batch cooldown, escalating rate-limit backoff, and
session re-auth are ported from the production factory so Apple doesn't flag the ID.
"""
from __future__ import annotations

import queue
import random
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from app.config import AppConfig
from app.events import EventBus
from app.identity import provision_account
from app.paths import DATA_DIR
from app.storage import Store

_SESSION_DIR = DATA_DIR / "icloud_sessions"
_SESSION_DIR.mkdir(parents=True, exist_ok=True)


class FactoryCancelled(Exception):
    pass


class AccountLocked(Exception):
    def __init__(self, email: str, reason: str = ""):
        self.email = email
        self.reason = reason
        super().__init__(f"Account {email} locked: {reason}")


class SessionExpired(Exception):
    """HTTP 401 — session token expired, re-auth may fix it."""


class RateLimited(Exception):
    def __init__(self, retry_after: int = 0, message: str = ""):
        self.retry_after = retry_after
        super().__init__(message or f"Rate limited, retry after {retry_after}s")


@dataclass
class AccountState:
    email: str
    password: str
    session: object = None
    generator: object = None
    cooldown_until: float = 0.0
    consecutive_rate_limits: int = 0
    locked: bool = False
    reauth_attempts: int = 0
    hourly_log: list = field(default_factory=list)
    daily_log: list = field(default_factory=list)


class EmailFactory:
    """Creates HME aliases and drops them into the local pool. Thread + UI-driven 2FA."""

    def __init__(self, store: Store, config: AppConfig, bus: EventBus):
        self.store = store
        self.config = config
        self.cfg = config.icloud
        self.bus = bus
        self._thread: threading.Thread | None = None
        self._cancel = threading.Event()
        self._pending: dict[str, queue.Queue] = {}  # 2FA prompt_id -> answer queue
        self._consecutive_failures = 0

    # ── lifecycle (called from the UI thread) ─────────────────────────────────
    def start(self, count: int, reset_login: bool = False, only: str = "") -> dict:
        if self._thread and self._thread.is_alive():
            return {"ok": False, "error": "Factory already running."}
        if count <= 0:
            return {"ok": False, "error": "Enter how many emails to generate."}
        if not self._usable(only):
            return {"ok": False,
                    "error": (f"{only} isn't set up on this computer."
                              if only else "Add an Apple ID (email + password) first.")}
        self._cancel.clear()
        self._thread = threading.Thread(
            target=self._run, args=(count, reset_login, only), daemon=True)
        self._thread.start()
        return {"ok": True}

    def _usable(self, only: str = "") -> list:
        """Apple IDs this run may mint on — all of them, or just the one that was picked.

        Rotation always starts at the top of the list, so without a pick the second Apple ID
        never gets touched until the first is capped. Naming one lets an operator work a
        specific minter (say, to spread load or to leave one alone).
        """
        want = (only or "").strip().lower()
        return [a for a in (self.cfg.accounts or [])
                if a.get("email") and a.get("password")
                and (not want or a["email"].strip().lower() == want)]

    def cancel(self):
        self._cancel.set()
        for pid in list(self._pending):
            self.cancel_prompt(pid)

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    # ── 2FA prompt bridge (answered from the UI thread) ───────────────────────
    def submit_code(self, prompt_id: str, code: str) -> bool:
        q = self._pending.get(prompt_id)
        if not q:
            return False
        q.put((code or "").strip())
        return True

    def cancel_prompt(self, prompt_id: str) -> bool:
        q = self._pending.get(prompt_id)
        if not q:
            return False
        q.put(None)
        return True

    def _wait_for_2fa(self, email: str) -> str:
        prompt_id = uuid.uuid4().hex
        q: queue.Queue = queue.Queue()
        self._pending[prompt_id] = q
        self.bus.set_factory(status=f"2FA needed for {email}")
        self.bus.open_prompt(prompt_id, 0, "2fa",
                             f"Apple ID {email}: enter the code Apple sent to your device")
        try:
            code = q.get(timeout=self.cfg.sms_code_timeout)
        except queue.Empty:
            raise RuntimeError(f"timed out waiting for 2FA code for {email}")
        finally:
            self._pending.pop(prompt_id, None)
            self.bus.close_prompt(prompt_id)
        if code is None:
            raise FactoryCancelled("2FA prompt cancelled")
        return code

    # ── rate tracking ─────────────────────────────────────────────────────---
    def _prune_logs(self, acct: AccountState):
        now = time.time()
        acct.hourly_log = [t for t in acct.hourly_log if t > now - 3600]
        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        acct.daily_log = [t for t in acct.daily_log if t > today]

    def _hourly(self, acct):
        self._prune_logs(acct)
        return len(acct.hourly_log)

    def _daily(self, acct):
        self._prune_logs(acct)
        return len(acct.daily_log)

    def _record(self, acct):
        now = time.time()
        acct.hourly_log.append(now)
        acct.daily_log.append(now)

    def _hourly_cap_wait(self, acct) -> int:
        """Seconds until this account's oldest create ages out of the rolling hour.

        Sitting out a flat hour would idle an Apple ID that is minutes from having a slot
        free again, so wait only as long as the window actually needs (plus a minute).
        """
        if not acct.hourly_log:
            return 60
        return max(60, int(min(acct.hourly_log) + 3600 - time.time()) + 60)

    def _email_delay(self) -> float:
        return max(10, self.cfg.per_email_delay + random.uniform(-15, 15))

    def _gap(self) -> float:
        return max(2, self.cfg.generate_reserve_gap + random.uniform(-3, 3))

    def _batch_cooldown(self) -> int:
        base = self.cfg.batch_cooldown
        return max(60, int(base + base * random.uniform(-0.2, 0.2)))

    def _rate_wait(self, acct) -> int:
        if acct.consecutive_rate_limits == 0:
            return self.cfg.default_cooldown_wait
        if acct.consecutive_rate_limits == 1:
            return self.cfg.escalated_cooldown_wait
        if acct.consecutive_rate_limits == 2:
            return 7200
        return 10800

    def _set_cooldown(self, acct) -> int:
        wait = self._rate_wait(acct)
        acct.consecutive_rate_limits += 1
        acct.cooldown_until = time.time() + wait
        return wait

    def _available(self, acct) -> bool:
        if acct.locked:
            return False
        if self._daily(acct) >= self.cfg.daily_cap_per_account:
            return False
        return time.time() >= acct.cooldown_until

    def _pick(self, accounts):
        for a in accounts:
            if self._available(a):
                return a
        return None

    def _earliest(self, accounts):
        best, best_time, all_capped = None, float("inf"), True
        for a in accounts:
            if a.locked:
                continue
            if self._daily(a) >= self.cfg.daily_cap_per_account:
                continue
            all_capped = False
            if a.cooldown_until < best_time:
                best, best_time = a, a.cooldown_until
        if best:
            return best, max(0, best_time - time.time())
        if all_capped:
            unlocked = [a for a in accounts if not a.locked]
            if unlocked:
                tomorrow = datetime.now().replace(
                    hour=0, minute=5, second=0, microsecond=0) + timedelta(days=1)
                return unlocked[0], max(60, (tomorrow - datetime.now()).total_seconds())
        return None, 0

    def _sleep(self, seconds: float):
        """Interruptible sleep — returns early (and raises) if the operator cancels."""
        end = time.time() + seconds
        while time.time() < end:
            if self._cancel.is_set():
                raise FactoryCancelled("cancelled during wait")
            time.sleep(min(2, max(0.1, end - time.time())))

    # ── auth ──────────────────────────────────────────────────────────────---
    def _session_path(self, email: str) -> str:
        safe = email.replace("@", "_at_").replace(".", "_")
        return str(_SESSION_DIR / f"icloud_session_{safe}.json")

    def _authenticate(self, email: str, password: str):
        from icloud_hme import ICloudSession, HideMyEmailGenerator
        from icloud_hme.icloud_auth import AUTH_ENDPOINT

        session = ICloudSession(credentials_file=self._session_path(email), quiet=True)
        if session.load_session() and session.validate_session():
            self.bus.log(f"iCloud: restored session for {email}")
            return session, HideMyEmailGenerator(session, quiet=True)

        self.bus.log(f"iCloud: logging in {email}")
        ok, message = session._srp_authenticate(email, password)
        if not ok:
            if "locked" in message.lower() or "forbidden" in message.lower():
                raise AccountLocked(email, message)
            raise RuntimeError(f"login failed: {message}")

        if message.startswith("2FA_REQUIRED"):
            headers = session._get_auth_headers()
            headers["Accept"] = "application/json"
            resp = session.session.get(f"{AUTH_ENDPOINT}/auth", headers=headers)
            try:
                data = resp.json()
            except Exception:
                data = {}
            phone_id, mode = self._find_phone(data)
            if phone_id:
                h = session._get_auth_headers()
                h["Accept"] = "application/json"
                put = session.session.put(f"{AUTH_ENDPOINT}/verify/phone", headers=h,
                                          json={"phoneNumber": {"id": phone_id}, "mode": "sms"})
                session._update_auth_state(put)
                self.bus.log(f"iCloud: SMS code requested for {email}")
            else:
                self.bus.log("iCloud: code sent to your trusted Apple devices", "warn")

            code = self._wait_for_2fa(email)

            if phone_id:
                h = session._get_auth_headers()
                h["Accept"] = "application/json, text/plain, */*"
                vr = session.session.post(
                    f"{AUTH_ENDPOINT}/verify/phone/securitycode", headers=h,
                    json={"phoneNumber": {"id": phone_id},
                          "securityCode": {"code": code}, "mode": mode or "sms"})
                session._update_auth_state(vr)
                if vr.status_code not in (200, 204):
                    raise RuntimeError(f"SMS verify failed: {vr.status_code} {vr.text[:120]}")
            else:
                vok, vmsg = session._verify_2fa_code(code)
                if not vok:
                    raise RuntimeError(f"2FA verify failed: {vmsg}")
            session._trust_device()

        login_ok, login_msg = session._account_login()
        if not login_ok:
            raise RuntimeError(f"account login failed: {login_msg}")
        session.save_session()
        self.bus.log(f"iCloud: {email} authenticated", "success")
        return session, HideMyEmailGenerator(session, quiet=True)

    @staticmethod
    def _find_phone(data: dict):
        tpn = data.get("trustedPhoneNumber")
        if tpn:
            return tpn.get("id"), tpn.get("pushMode", "sms")
        tpns = data.get("trustedPhoneNumbers", [])
        if tpns:
            return tpns[0].get("id"), tpns[0].get("pushMode", "sms")
        pnv = data.get("phoneNumberVerification", {})
        tpn2 = pnv.get("trustedPhoneNumber")
        if tpn2:
            return tpn2.get("id"), tpn2.get("pushMode", "sms")
        tpns2 = pnv.get("trustedPhoneNumbers", [])
        if tpns2:
            return tpns2[0].get("id"), tpns2[0].get("pushMode", "sms")
        return None, "sms"

    # ── direct HME calls ───────────────────────────────────────────────────--
    def _generate(self, gen) -> str:
        url = gen._get_service_url()
        resp = gen.session.session.post(
            f"{url}/v1/hme/generate", params=gen._get_request_params(),
            json={"langCode": "en-us"})
        return self._handle_hme(resp, "generate", lambda b: b.get("result", {}).get("hme", ""))

    def _reserve(self, gen, email: str, label: str) -> bool:
        url = gen._get_service_url()
        resp = gen.session.session.post(
            f"{url}/v1/hme/reserve", params=gen._get_request_params(),
            json={"hme": email, "label": label, "note": ""})
        return self._handle_hme(resp, "reserve", lambda b: True)

    @staticmethod
    def _handle_hme(resp, op: str, on_success):
        if resp.status_code == 200:
            body = resp.json()
            if body.get("success"):
                return on_success(body)
            err = body.get("error", {})
            msg = err.get("errorMessage", "")
            if err.get("retryAfter") or "limit" in msg.lower() or "try again" in msg.lower():
                raise RateLimited(err.get("retryAfter", 0), f"{op} rate-limited: {msg}")
            if "locked" in msg.lower() or "disabled" in msg.lower():
                raise AccountLocked("", msg)
            raise RuntimeError(f"{op} failed: {msg or body}")
        if resp.status_code == 429:
            raise RateLimited(int(resp.headers.get("Retry-After", 0)), f"HTTP 429 on {op}")
        if resp.status_code == 401:
            raise SessionExpired(f"HTTP 401 on {op}")
        if resp.status_code == 403:
            raise AccountLocked("", f"HTTP 403 on {op}")
        raise RuntimeError(f"{op} HTTP {resp.status_code}: {resp.text[:120]}")

    # ── main loop ─────────────────────────────────────────────────────────---
    def _run(self, count: int, reset_login: bool, only: str = ""):
        created: list[str] = []
        self.bus.set_factory(running=True, status="starting", created=0, target=count)
        try:
            usable = self._usable(only)
            if reset_login:
                for a in usable:
                    Path(self._session_path(a["email"])).unlink(missing_ok=True)
                self.bus.log("iCloud: cleared saved sessions (fresh login this run)")

            accounts = [AccountState(email=a["email"], password=a["password"]) for a in usable]
            self.bus.log(f"iCloud: minting on {', '.join(a.email for a in accounts)}")

            for acct in accounts:
                if self._cancel.is_set():
                    raise FactoryCancelled("cancelled before start")
                self.bus.set_factory(status=f"authenticating {acct.email}")
                try:
                    acct.session, acct.generator = self._authenticate(acct.email, acct.password)
                except AccountLocked as e:
                    self.bus.log(f"iCloud: {acct.email} is locked — {e.reason}", "error")
                    acct.locked = True
                except FactoryCancelled:
                    raise
                except Exception as e:
                    self.bus.log(f"iCloud: auth failed for {acct.email}: {e}", "error")
                    acct.locked = True

            active = [a for a in accounts if not a.locked]
            if not active:
                raise RuntimeError("all Apple IDs failed to authenticate")

            self._loop(active, accounts, count, created)
        except FactoryCancelled as e:
            self.bus.log(f"Email factory stopped: {e} ({len(created)}/{count} made).", "warn")
        except Exception as e:
            self.bus.log(f"Email factory error: {type(e).__name__}: {e}", "error")
            self.bus.set_factory(status=f"error: {e}")
        finally:
            unused = self.store.count_pool(used=False)
            self.bus.set_factory(running=False,
                                 status=f"done — {len(created)} made, {unused} unused in pool",
                                 created=len(created))
            self.bus.log(f"Email factory finished: {len(created)}/{count} created.",
                         "success" if created else "warn")

    def _loop(self, active, accounts, count, created):
        self._consecutive_failures = 0
        while len(created) < count:
            if self._cancel.is_set():
                raise FactoryCancelled(f"stopped at {len(created)}/{count}")

            acct = self._pick(active)
            if acct is None:
                nxt, wait = self._earliest(active)
                if nxt is None:
                    raise RuntimeError("all Apple IDs exhausted")
                mins = int(wait) // 60
                # A wait measured in hours means the day's quota is gone, not a short rest.
                why = ("all Apple IDs at their daily cap — resumes tomorrow"
                       if wait > 7200 else "all Apple IDs cooling down")
                self.bus.set_factory(status=f"{why} ({mins}m)")
                self.bus.log(f"iCloud: {why}, waiting {mins}m "
                             f"({len(created)}/{count} made)", "warn")
                self._sleep(wait)
                continue

            if self._hourly(acct) >= self.cfg.hourly_cap_per_account:
                wait = self._hourly_cap_wait(acct)
                acct.cooldown_until = time.time() + wait
                self.bus.log(f"iCloud: {acct.email} hit its hourly cap "
                             f"({self.cfg.hourly_cap_per_account}/h) — resting "
                             f"{wait // 60}m", "warn")
                continue

            # keep the session fresh
            try:
                if not acct.session.validate_session():
                    self.bus.set_factory(status=f"re-authenticating {acct.email}")
                    acct.session, acct.generator = self._authenticate(acct.email, acct.password)
            except Exception as e:
                self.bus.log(f"iCloud: session check for {acct.email}: {e}", "warn")

            batch = min(self.cfg.batch_size, count - len(created))
            self.bus.set_factory(status=f"generating on {acct.email} ({len(created)}/{count})")
            batch_made, rate_hit = 0, False

            for i in range(batch):
                if self._cancel.is_set():
                    raise FactoryCancelled(f"stopped at {len(created)}/{count}")
                if self._hourly(acct) >= self.cfg.hourly_cap_per_account:
                    acct.cooldown_until = time.time() + self._hourly_cap_wait(acct)
                    break
                try:
                    label = f"tm-{datetime.now():%m%d}-{len(created) + 1}"
                    email_addr = self._generate(acct.generator)
                    if not email_addr:
                        raise RuntimeError("generate returned empty")
                    self._sleep(self._gap())
                    if not self._reserve(acct.generator, email_addr, label):
                        raise RuntimeError(f"reserve failed for {email_addr}")
                except SessionExpired:
                    acct.reauth_attempts += 1
                    if acct.reauth_attempts > 2:
                        acct.locked = True
                        active[:] = [a for a in accounts if not a.locked]
                        if not active:
                            raise AccountLocked(acct.email, "repeated session expiry")
                        break
                    try:
                        acct.session, acct.generator = self._authenticate(acct.email, acct.password)
                        continue
                    except Exception as e:
                        self.bus.log(f"iCloud: re-auth failed for {acct.email}: {e}", "error")
                        acct.locked = True
                        active[:] = [a for a in accounts if not a.locked]
                        if not active:
                            raise AccountLocked(acct.email, "re-auth failed")
                        break
                except AccountLocked as e:
                    self.bus.log(f"iCloud: {acct.email} LOCKED: {e}", "error")
                    acct.locked = True
                    active[:] = [a for a in accounts if not a.locked]
                    if not active:
                        raise
                    break
                except RateLimited as e:
                    rate_hit = True
                    wait = self._set_cooldown(acct)
                    self.bus.log(f"iCloud: {acct.email} rate-limited — resting "
                                 f"{wait // 60}m (strike {acct.consecutive_rate_limits})", "warn")
                    break
                except FactoryCancelled:
                    raise
                except Exception as e:
                    if any(w in str(e).lower() for w in ("locked", "forbidden", "disabled")):
                        acct.locked = True
                        active[:] = [a for a in accounts if not a.locked]
                        if not active:
                            raise AccountLocked(acct.email, str(e))
                        break
                    self._consecutive_failures += 1
                    self.bus.log(f"iCloud: {acct.email} failed "
                                 f"({self._consecutive_failures}/5): {e}", "error")
                    if self._consecutive_failures >= 5:
                        raise RuntimeError(f"5 consecutive failures: {e}")
                    self._sleep(random.uniform(10, 30))
                    continue

                # success
                self._consecutive_failures = 0
                acct.consecutive_rate_limits = 0
                acct.reauth_attempts = 0
                self._record(acct)
                created.append(email_addr)
                batch_made += 1
                self.store.add_pool_email(email_addr, created_from=acct.email, label=label)
                # Straight into the queue with a name, address and a phone number from the
                # pool — the alias is ready to run without anyone moving it by hand.
                row = provision_account(self.store, email_addr, created_from=acct.email)
                self.store.mark_pool_queued(email_addr)
                self.bus.set_factory(created=len(created),
                                     status=f"created {email_addr} ({len(created)}/{count})")
                self.bus.log(
                    f"iCloud: ✓ {email_addr} → account #{row.id} "
                    f"({row.first_name} {row.last_name}{', ' + row.phone if row.phone else ''})"
                    f" ({len(created)}/{count})", "success")
                try:
                    acct.session.save_session()
                except Exception:
                    pass
                if i < batch - 1 and len(created) < count:
                    self._sleep(self._email_delay())

            if batch_made > 0 and not rate_hit and len(created) < count:
                rest = self._batch_cooldown()
                acct.cooldown_until = time.time() + rest
                self.bus.log(f"iCloud: {acct.email} batch done — resting {rest // 60}m", "info")
