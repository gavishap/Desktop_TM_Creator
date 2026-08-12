"""Runner — drives account creation on a background asyncio thread.

Replaces the Sheets daemon. On ``start()`` it spins up its own event loop in a thread and
processes every runnable account (respecting ``max_concurrent``). Progress, logs, and code
prompts flow to the UI through the shared EventBus / CodeBroker.

An attempt that doesn't finish never blocks the queue: the row records when it is next due
and is handed back, so the Chrome slot goes to the next account and the wait survives
closing the app. Two cases have their own schedule:
  * a **failure or bot block** is about this session — cookies are gone with the browser and
    the row comes back after 5m, 20m, 1h10m, 2h30m, 5h (as far as Settings allows);
  * a **throttle** is Ticketmaster rate-limiting the whole internet connection — half an
    hour or more, and after enough throttles in a row the app stands down completely rather
    than hammering an IP that is already unhappy.
"""
from __future__ import annotations

import asyncio
import random
import secrets
import string
import threading
import time
from typing import Optional

from app.browser import ChromeSession
from app.config import AppConfig
from app.events import EventBus
from app.identity import fill_identity
from app.signup.flow import FlowError, SignupFlow, TmBotBlock, TmThrottled
from app.storage import (EMAIL_VERIFIED, FAILED, IN_PROGRESS, NEEDS_RETRY, VERIFIED,
                         Account, Store)
from app.verification.code_broker import CodeBroker
from app.verification.imap_client import ImapPoller
from app.verification.jivetel_sms import JivetelSmsClient


def _spell(seconds: float) -> str:
    """Seconds as something an operator reads at a glance: "20m", "1h 10m", "5h"."""
    mins = int(seconds // 60)
    if mins < 60:
        return f"{max(1, mins)}m"
    return f"{mins // 60}h" if mins % 60 == 0 else f"{mins // 60}h {mins % 60:02d}m"


def _gen_password(length: int = 16) -> str:
    alphabet = string.ascii_letters + string.digits + "!@#$%&*"
    while True:
        pw = "".join(secrets.choice(alphabet) for _ in range(length))
        if (any(c.isupper() for c in pw) and any(c.islower() for c in pw)
                and any(c.isdigit() for c in pw) and any(c in "!@#$%&*" for c in pw)):
            return pw


class Runner:
    def __init__(self, store: Store, config: AppConfig, bus: EventBus, broker: CodeBroker):
        self.store = store
        self.config = config
        self.bus = bus
        self.broker = broker
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._main_task: Optional[asyncio.Task] = None
        self._account_ids: Optional[list[int]] = None
        self._poller: Optional[ImapPoller] = None
        self._pause_until = 0.0     # everything stands down until this time
        self._throttle_streak = 0   # consecutive throttles across the run
        self._pause_count = 0       # how many standdowns so far (each one lasts longer)

    # ── lifecycle (called from the UI/main thread) ────────────────────────────
    def start(self, account_ids: Optional[list[int]] = None):
        """Run all runnable accounts, or only ``account_ids`` when given (per-row Run)."""
        if self._thread and self._thread.is_alive():
            return
        self._account_ids = account_ids
        self._thread = threading.Thread(target=self._thread_main, daemon=True)
        self._thread.start()

    def stop(self):
        if self._loop and self._main_task:
            self._loop.call_soon_threadsafe(self._main_task.cancel)
        self.broker.cancel_all()

    def _thread_main(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self.broker.set_loop(self._loop)
        self._main_task = self._loop.create_task(self._main())
        try:
            self._loop.run_until_complete(self._main_task)
        except asyncio.CancelledError:
            pass
        finally:
            self.bus.set_running(False)
            self._loop.close()

    # ── the run ───────────────────────────────────────────────────────────---
    def _due_now(self) -> list[Account]:
        """Accounts ready to start right now — waiting rows stay out until their time."""
        if self._account_ids:
            now = time.time()
            return [a for a in (self.store.get(i) for i in self._account_ids)
                    if a and a.status not in (VERIFIED, IN_PROGRESS) and a.retry_after <= now]
        return self.store.runnable()

    async def _main(self):
        self.bus.set_running(True)
        self._pause_until = self._throttle_streak = self._pause_count = 0
        accounts = self._due_now()
        if not accounts and not self.store.next_retry_at(self._account_ids):
            self.bus.log("No pending accounts to process.", "warn")
            self.bus.set_running(False)
            return
        await self._start_poller(accounts)
        sem = asyncio.Semaphore(max(1, self.config.max_concurrent))

        async def _guard(acct: Account):
            async with sem:
                # Desync launches so concurrent sessions here — and other machines on the
                # same public IP — don't hit Ticketmaster's auth in lockstep.
                jitter = random.uniform(0, max(0.0, self.config.launch_jitter))
                if jitter:
                    await asyncio.sleep(jitter)
                await self._process_account(acct)

        try:
            while True:
                if accounts:
                    self.bus.log(f"Starting run: {len(accounts)} account(s), "
                                 f"{self.config.max_concurrent} at a time.")
                    await asyncio.gather(*(_guard(a) for a in accounts))
                # Rows waiting out a block or a throttle come back on their own — no clicking
                # required, as long as the app stays open.
                due_at = self.store.next_retry_at(self._account_ids)
                if not due_at:
                    break
                wait = max(1.0, due_at - time.time())
                self.bus.log(f"Nothing to do for {_spell(wait)} — the next account that is "
                             f"waiting comes back by itself. Leave the app open.", "warn")
                await asyncio.sleep(wait)
                accounts = self._due_now()
        finally:
            if self._poller:
                await self._poller.stop()
                self._poller = None
            self.bus.set_running(False)
            self.bus.log("Run finished.")

    async def _start_poller(self, accounts: list[Account]):
        """One IMAP login per inbox for the whole run, instead of one per attempt."""
        poller = ImapPoller(
            self.config.imap.host, self.config.imap.port, self.config.imap.accounts,
            poll_interval=self.config.imap.poll_interval, timeout=self.config.imap.timeout,
            keepalive_interval=self.config.imap.keepalive_interval,
            relogin_backoff=self.config.imap.relogin_backoff, log=self.bus.log,
        )
        for a in accounts:
            if a.imap_password:
                poller.add_inbox(a.email, a.imap_password)
        if not poller.inbox_count:
            self.bus.log("No email inbox configured — email codes will be typed by hand.",
                         "warn")
            return
        await poller.start()
        self._poller = poller

    # ── block / throttle policy ───────────────────────────────────────────---
    def _handle_block(self, account: Account, log, hard_block: bool) -> None:
        """Park a row that didn't finish until its next slot, or fail it once tries run out.

        The wait is written onto the row instead of being slept through here. The later steps
        are hours long: sleeping would hold a Chrome slot that another account could use, and
        the wait would be lost the moment the operator closed the app. Cookies need no extra
        wiping — the browser and its throwaway profile are already gone.
        """
        blk = self.config.block
        ladder = blk.cooldowns or [300]
        tries = blk.max_retries + 1
        account.block_strikes += 1
        if account.block_strikes >= tries:
            account.status = FAILED
            account.retry_after = 0
            self.store.save_account(account)
            log(f"Giving up after {tries} attempt(s): {account.error}", "error")
            return
        wait = ladder[min(account.block_strikes - 1, len(ladder) - 1)]
        account.status = NEEDS_RETRY
        account.retry_after = time.time() + wait
        self.store.save_account(account)
        log(f"{'Blocked' if hard_block else 'Did not finish'} — trying again in {_spell(wait)} "
            f"with a clean browser (try {account.block_strikes + 1} of {tries}).", "warn")

    async def _wait_out_standdown(self, log):
        remaining = self._pause_until - time.time()
        if remaining <= 0:
            return
        log(f"Standing down — starting in about {int(remaining // 60) + 1}m", "warn")
        while (remaining := self._pause_until - time.time()) > 0:
            await asyncio.sleep(min(remaining, 30))

    def _handle_throttle(self, account: Account, log) -> None:
        """Park a throttled row and, if TM keeps throttling, stop the whole app for a while."""
        blk = self.config.block
        account.throttle_strikes += 1
        self._throttle_streak += 1
        if account.throttle_strikes >= blk.max_throttle_strikes:
            account.status = FAILED
            account.retry_after = 0
            self.store.save_account(account)
            log(f"Throttled {account.throttle_strikes} times — giving up on this one.", "error")
        else:
            backoffs = blk.throttle_backoffs or [1800]
            wait = backoffs[min(account.throttle_strikes - 1, len(backoffs) - 1)]
            account.status = NEEDS_RETRY
            account.retry_after = time.time() + wait
            self.store.save_account(account)
            log(f"Ticketmaster is throttling this connection — back in the queue for "
                f"{wait // 60}m (strike {account.throttle_strikes}/"
                f"{blk.max_throttle_strikes}).", "warn")
        if self._throttle_streak >= max(1, blk.throttle_pause_after):
            pauses = blk.throttle_pauses or [1800]
            secs = pauses[min(self._pause_count, len(pauses) - 1)]
            self._pause_count += 1
            self._pause_until = time.time() + secs
            self._throttle_streak = 0
            self.bus.log(f"Throttled {blk.throttle_pause_after} times in a row — pausing "
                         f"everything for {secs // 60}m so the connection cools off.", "warn")

    async def _make_sms_client(self, log) -> Optional[JivetelSmsClient]:
        """Log into Jivetel once per attempt if configured; None -> flow prompts manually."""
        jv = self.config.jivetel
        if not (jv.enabled and jv.username and jv.password):
            return None
        client = JivetelSmsClient(jv, channel=self.config.browser.channel)
        try:
            await client.init()
            if await client.login():
                log("Jivetel logged in")
                return client
            log("Jivetel login failed — will prompt for SMS manually", "warn")
        except Exception as e:
            log(f"Jivetel error: {type(e).__name__}: {str(e)[:80]} — manual SMS", "warn")
        try:
            await client.close()
        except Exception:
            pass
        return None

    async def _process_account(self, account: Account):
        def log(msg: str, level: str = "info"):
            self.bus.log(msg, level, account_id=account.id)

        fill_identity(account, self.store.used_full_names())
        if not account.tm_password:
            account.tm_password = _gen_password()

        await self._wait_out_standdown(log)
        account.attempts += 1
        account.email_verified = False
        account.phone_verified = False
        account.status = IN_PROGRESS
        account.error = ""
        account.retry_after = 0
        self.store.save_account(account)
        log(f"Attempt {account.block_strikes + 1} of {self.config.block.max_retries + 1} "
            f"— launching Chrome")

        if account.imap_password and self._poller:
            self._poller.add_inbox(account.email, account.imap_password)
        sms = await self._make_sms_client(log)

        session = ChromeSession(self.config.browser)
        hard_block = throttled = False
        try:
            await session.start()
            log(f"Clean Chrome profile — cookies after wipe: {session.cookies_at_start}")
            flow = SignupFlow(session, self.config, self.broker, log,
                              imap=self._poller, sms=sms)
            await flow.run(account)

            if account.email_verified and (account.phone_verified or not account.phone):
                account.status = VERIFIED
                account.error = ""
                account.verified_at = time.time()
                self.store.save_account(account)
                # The alias behind this account is now proven spent (Sheet's "used").
                self.store.mark_pool_used(account.email)
                self._throttle_streak = 0
                log("VERIFIED", "success")
                return
            account.status = EMAIL_VERIFIED if account.email_verified else FAILED
            account.error = "incomplete_verification"
        except TmThrottled as e:
            account.error = str(e)
            throttled = True
            log(f"Throttled: {e}", "warn")
        except TmBotBlock as e:
            account.error = str(e)
            hard_block = True
            log(f"Blocked: {e}", "error")
        except FlowError as e:
            account.error = str(e)
            log(f"Failed: {e}", "error")
        except asyncio.CancelledError:
            account.status = NEEDS_RETRY
            self.store.save_account(account)
            raise
        except Exception as e:
            account.error = f"{type(e).__name__}: {str(e)[:150]}"
            log(f"Error: {account.error}", "error")
        finally:
            await session.close()
            if sms:
                await sms.close()

        if throttled:
            self._handle_throttle(account, log)
            return
        self._throttle_streak = 0
        self._handle_block(account, log, hard_block)
