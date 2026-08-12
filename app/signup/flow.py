"""Ticketmaster signup flow — desktop edition.

Same step sequence as the original VPS flow (auth page -> email -> profile -> email
code -> phone -> SMS code -> done), with three changes for the enclosed app:
  1. Runs on the operator's real Chrome (no proxy, no Camoufox).
  2. Verification codes come from Gmail IMAP when available, otherwise the operator is
     prompted in the UI (email and SMS both).
  3. Bot-blocks raise ``TmBotBlock``; the orchestrator wipes cookies, waits, and retries.
"""
from __future__ import annotations

import asyncio
import time
from typing import Callable, Optional

from playwright.async_api import TimeoutError as PlaywrightTimeoutError

from app.browser import ChromeSession
from app.config import AppConfig
from app.form_filler import FormFiller
from app.storage import Account
from app.verification.code_broker import CodeBroker
from app.verification.imap_client import ImapPoller


class TmThrottled(Exception):
    """TM's 'Almost there' interstitial never cleared."""


class TmBotBlock(Exception):
    """TM served a bot-block / paused page — cookies should be wiped before retrying."""


class FlowError(Exception):
    """A soft failure (timeout, blank page) — retryable but not necessarily a hard block."""


TM_AUTH_URL = (
    "https://auth.ticketmaster.com/as/authorization.oauth2"
    "?client_id=8bf7204a7e97.web.ticketmaster.us&response_type=code"
    "&scope=openid%20profile%20phone%20email%20tm"
    "&redirect_uri=https://identity.ticketmaster.com/exchange"
    "&visualPresets=tm&lang=en-us&placementId=mytmlogin"
    "&hideLeftPanel=false&integratorId=prd1741.iccp&intSiteToken=tm-us"
)

_SEL = {
    "email_input": 'input[type="email"], input[name="email"], input[autocomplete="email"]',
    # Target the submit button precisely: the "Continue With Apple"/"Continue with Google"
    # buttons also contain the word "Continue" and appear earlier in the DOM, so a
    # has-text("Continue") match clicks the wrong one. type="submit" + exact text avoid that.
    "continue_btn": 'button[type="submit"], button:text-is("Continue")',
    "first_name": 'input[name="firstName"], input[autocomplete="given-name"], input[placeholder*="First" i]',
    "last_name": 'input[name="lastName"], input[autocomplete="family-name"], input[placeholder*="Last" i]',
    "password": 'input[type="password"], input[name="password"]',
    "zip_code": 'input[name="zip"], input[name="postalCode"], input[placeholder*="zip" i], input[placeholder*="postal" i]',
    "phone_input": 'input[type="tel"], input[name="phone"], input[autocomplete="tel"]',
    "send_code_btn": 'button:has-text("Send Code"), button:has-text("Send"), button:has-text("Get Code")',
    "submit_btn": 'button[type="submit"], button:text-is("Continue"), button:has-text("Create"), button:text-is("Next")',
}

_BOT_BLOCK_SIGNALS = [
    "your browsing activity has been paused",
    "unusual behavior",
    "access denied",
    "pardon our interruption",
    "please verify you are a human",
]

_EMAIL_SELECTORS = [
    'input[type="email"]', 'input[name="email"]', 'input[autocomplete="email"]',
    'input[id*="email" i]', 'input[placeholder*="email" i]', 'input[aria-label*="email" i]',
]


class SignupFlow:
    def __init__(
        self,
        session: ChromeSession,
        config: AppConfig,
        broker: CodeBroker,
        log: Callable[[str], None],
        imap: Optional[ImapPoller] = None,
        sms=None,
    ):
        self.session = session
        self.config = config
        self.broker = broker
        self.log = log
        self.imap = imap
        self.sms = sms  # JivetelSmsClient | None
        self.filler = FormFiller(session.page, config.browser)

    async def run(self, account: Account) -> Account:
        page = self.session.page

        await self._step_navigate()
        await self._screenshot(account, "01_auth_page")

        self.log(f"Entering email: {account.email}")
        await self._step_enter_email(account.email)
        await self._screenshot(account, "02_after_email")

        page_text = (await self._body(page))
        if any(k in page_text for k in ("sign up", "create", "password")):
            self.log("Sign-up form detected — filling profile")
            await self._step_fill_profile(account)
            await self._screenshot(account, "03_after_profile")
            post = await self._body(page)
            if "please acknowledge" in post or ("sign up" in post and "next" in post):
                await self._screenshot(account, "03_stuck_on_signup")
                raise FlowError("profile_form_not_submitted")

        await self.filler.pause(1.0, 2.0)
        post_body = await self._body(page)
        has_verify = any(k in post_body for k in ("verify", "confirm", "almost there", "email", "phone"))
        if not has_verify and len(post_body.strip()) < 300:
            await self._screenshot(account, "03_blank_page")
            raise TmBotBlock("blank_page_after_profile")

        email_send_time = time.time()
        if "verify your account" in post_body or "verify my email" in post_body:
            self.log("Clicking Verify My Email")
            try:
                btn = await page.wait_for_selector(
                    'button:has-text("Verify My Email"), a:has-text("Verify My Email")', timeout=8000)
                if btn:
                    await btn.click()
                    await self.filler.pause(1.5, 3.0)
            except Exception:
                pass

        await self._screenshot(account, "05_before_email_code")
        pre_wait = await self._body(page)
        if not any(k in pre_wait for k in ("verify", "code", "resend", "email", "confirm")):
            await self._screenshot(account, "05_blank_before_email_wait")
            raise TmBotBlock("blank_page_before_email_wait")

        await self._request_email_code(page, pre_wait)

        self.log("Waiting for email verification code")
        email_code = await self._get_email_code(account, email_send_time)
        if not email_code:
            await self._screenshot(account, "05_email_code_timeout")
            raise FlowError("email_verification_timeout")

        self.log(f"Entering email code: {email_code}")
        await self._step_enter_otp(email_code)
        await self._screenshot(account, "06_after_email_code")
        account.email_verified = True

        # Phone verification
        await self.filler.pause(0.5, 1.5)
        page_text4 = await self._body(page)
        if "add my phone" in page_text4 or "add & verify" in page_text4:
            self.log("Clicking Add My Phone")
            try:
                btn = await page.wait_for_selector(
                    'button:has-text("Add My Phone"), button:has-text("Add & Verify")', timeout=8000)
                if btn:
                    await btn.click(force=True, timeout=10000)
                    await self.filler.pause(1.5, 3.0)
            except Exception:
                pass

        await self._screenshot(account, "07_before_phone")
        page_text5 = await self._body(page)
        if "phone" in page_text5 and account.phone:
            # Snapshot the newest code on Jivetel BEFORE we trigger a send, so we can
            # tell a fresh code apart from a stale one already in the thread.
            sms_baseline = None
            if self.sms:
                try:
                    sms_baseline = await self.sms.get_current_code(account.phone)
                    self.log(f"SMS baseline: {sms_baseline or 'none'}")
                except Exception:
                    pass

            self.log(f"Entering phone: {account.phone}")
            await self._step_enter_phone(account.phone)
            await self._screenshot(account, "08_after_phone")

            self.log("Waiting for SMS code")
            sms_code = await self._get_sms_code(account, sms_baseline)
            if not sms_code:
                await self._screenshot(account, "08_sms_timeout")
                raise FlowError("sms_verification_timeout")

            self.log(f"Entering SMS code: {sms_code}")
            await self._step_enter_otp(sms_code)
            await self._screenshot(account, "09_after_sms_code")
            account.phone_verified = True
        else:
            self.log("No phone step shown — continuing")
            await self._screenshot(account, "09_no_phone_step")

        await self._finalize(account)
        await self._screenshot(account, "12_final")
        self.log(f"Final URL: {page.url[:90]}")
        return account

    # ── navigation ─────────────────────────────────────────────────────────---
    async def _body(self, page) -> str:
        try:
            return (await page.inner_text("body")).lower()
        except Exception:
            return ""

    async def _check_bot_block(self, page):
        body = await self._body(page)
        for sig in _BOT_BLOCK_SIGNALS:
            if sig in body:
                raise TmBotBlock(f"tm_bot_block: {sig}")

    async def _dismiss_cookie_banner(self, page):
        for sel in ('button:has-text("Accept All")', 'button:has-text("Accept all")',
                    "#onetrust-accept-btn-handler"):
            try:
                btn = page.locator(sel).first
                if await btn.is_visible(timeout=400):
                    await btn.click(timeout=3000)
                    await asyncio.sleep(0.5)
                    return
            except Exception:
                continue

    async def _wait_for_email_field(self, page, timeout: float = 45.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            await self._check_bot_block(page)
            await self._dismiss_cookie_banner(page)
            for sel in _EMAIL_SELECTORS:
                try:
                    el = await page.query_selector(sel)
                    if el and await el.is_visible():
                        return el
                except Exception:
                    continue
            await asyncio.sleep(1.0)
        await self._check_bot_block(page)
        if len((await self._body(page)).strip()) < 300:
            raise TmBotBlock("blank_or_unloaded_auth_page")
        raise FlowError("auth_email_field_not_found")

    async def _step_navigate(self):
        page = self.session.page
        self.log("Loading TM auth page")
        for attempt in range(2):
            try:
                await page.goto(TM_AUTH_URL, wait_until="commit", timeout=60000)
                break
            except PlaywrightTimeoutError:
                if attempt == 0:
                    self.log("Auth navigation stalled — retrying once")
                    continue
                raise TmBotBlock("auth_page_timeout")
        try:
            await page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass
        await self._check_bot_block(page)
        await self._wait_for_interstitial(page)
        await self._wait_for_email_field(page)
        await self._warm_behavior(page)
        await self.filler.pause(0.5, 1.0)

    async def _warm_behavior(self, page):
        """Dwell + move the cursor so NuData/Kasada see human signal before we submit."""
        ready = False
        for _ in range(20):
            try:
                ready = await page.evaluate(
                    "() => !!(window.KPSDK && window.KPSDK.isReady && window.KPSDK.isReady())")
            except Exception:
                ready = False
            if ready:
                break
            await asyncio.sleep(0.5)
        self.log(f"Kasada ready={ready}; warming behavior")
        await self.filler.human_dwell(5.0, 9.0)

    async def _wait_for_interstitial(self, page):
        await self._check_bot_block(page)
        body = await self._body(page)
        if "almost there" not in body:
            return
        self.log("TM interstitial ('Almost there') — waiting up to 90s")
        for i in range(18):
            await asyncio.sleep(5)
            body = await self._body(page)
            if "almost there" not in body:
                self.log(f"Interstitial cleared after {(i + 1) * 5}s")
                return
        raise TmThrottled("interstitial_not_cleared")

    # ── steps ──────────────────────────────────────────────────────────────---
    async def _step_enter_email(self, email: str):
        page = self.session.page
        el = await self._wait_for_email_field(page, timeout=15)
        await self.filler.type_text_on_element(el, email)
        await self.filler.pause(0.8, 1.6)
        await self.filler.wander_mouse(2)
        try:
            await self.filler.click(_SEL["continue_btn"])
        except Exception:
            await page.keyboard.press("Enter")
        await self.filler.pause(2.0, 3.0)
        try:
            await page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass

    async def _step_enter_otp(self, code: str):
        page = self.session.page
        otp_selectors = [
            'input[inputmode="numeric"]', 'input[autocomplete="one-time-code"]',
            'input[placeholder*="code" i]', 'input[aria-label*="code" i]',
            'input[name*="code" i]', 'input[type="text"][maxlength]', 'input[type="tel"]',
        ]
        el = None
        for sel in otp_selectors:
            try:
                el = await page.wait_for_selector(sel, timeout=5000)
                if el and await el.is_visible():
                    break
                el = None
            except Exception:
                continue
        if el:
            await el.click()
            await asyncio.sleep(0.3)
            await el.fill("")
            await self.filler.type_text_on_element(el, code)
        else:
            singles = await page.query_selector_all('input[maxlength="1"]')
            if singles and len(singles) >= len(code):
                for i, digit in enumerate(code):
                    await singles[i].fill(digit)
                    await asyncio.sleep(0.12)
            else:
                raise FlowError("otp_input_not_found")
        await self.filler.pause(0.3, 0.8)
        for btn_sel in ('button:has-text("Confirm Code")', 'button:has-text("Confirm")',
                        'button:text-is("Continue")', 'button:has-text("Submit")',
                        'button[type="submit"]'):
            try:
                btn = await page.query_selector(btn_sel)
                if btn and await btn.is_visible() and await btn.is_enabled():
                    await btn.click()
                    break
            except Exception:
                continue
        else:
            await page.keyboard.press("Enter")
        await self.filler.pause(1.5, 3.0)
        try:
            await page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass

    async def _step_fill_profile(self, account: Account):
        page = self.session.page
        fields = [
            (_SEL["password"], account.tm_password),
            (_SEL["first_name"], account.first_name),
            (_SEL["last_name"], account.last_name),
            (_SEL["zip_code"], account.zip_code or "10001"),
        ]
        for selector, value in fields:
            try:
                el = await page.wait_for_selector(selector, timeout=5000)
                if el and await el.is_visible():
                    await self.filler.type_text(selector, value)
                    await self.filler.pause(0.2, 0.5)
            except Exception:
                continue

        # Terms checkbox — target the input directly (labels contain links).
        checked = False
        try:
            cb = await page.query_selector('input[type="checkbox"]')
            if cb:
                if not await cb.is_checked():
                    await cb.check(force=True)
                    await self.filler.pause(0.2, 0.4)
                checked = await cb.is_checked()
        except Exception:
            pass
        if not checked:
            try:
                checked = await page.evaluate("""() => {
                    const cb = document.querySelector('input[type="checkbox"]');
                    if (!cb) return false;
                    const set = Object.getOwnPropertyDescriptor(
                        window.HTMLInputElement.prototype, 'checked').set;
                    set.call(cb, true);
                    cb.dispatchEvent(new Event('input', { bubbles: true }));
                    cb.dispatchEvent(new Event('change', { bubbles: true }));
                    return cb.checked;
                }""")
            except Exception:
                pass

        try:
            next_btn = await page.wait_for_selector('button:has-text("Next"), button[type="submit"]', timeout=5000)
            if next_btn:
                for _ in range(20):
                    if await next_btn.is_enabled():
                        break
                    await asyncio.sleep(0.5)
                await next_btn.click(force=not await next_btn.is_enabled())
                await self.filler.pause(1.5, 3.0)
                try:
                    await page.wait_for_load_state("networkidle", timeout=15000)
                except Exception:
                    pass
        except Exception as e:
            self.log(f"Profile submit issue: {e}")

    async def _step_enter_phone(self, phone: str):
        page = self.session.page
        try:
            await self.filler.type_text(_SEL["phone_input"], phone)
            await self.filler.pause(0.3, 0.8)
            try:
                el = await page.query_selector(_SEL["send_code_btn"])
                if el and await el.is_visible():
                    await self.filler.click(_SEL["send_code_btn"])
                else:
                    await self.filler.click(_SEL["submit_btn"])
            except Exception:
                await page.keyboard.press("Enter")
            await self.filler.pause(1.5, 3.0)
        except Exception as e:
            self.log(f"Phone entry issue: {e}")

    async def _request_email_code(self, page, body: str) -> bool:
        """Click Resend / Send Code when TM is offering it.

        TM doesn't always send the code on its own. Without this the account sits through
        the entire IMAP wait for a mail that was never sent, then fails as a timeout.
        """
        if "resend" not in body and "send code" not in body:
            return False
        try:
            btn = await page.query_selector(
                'button:has-text("Resend"), button:has-text("Send Code")')
            if btn and await btn.is_visible():
                await btn.click()
                self.log("Asked Ticketmaster to send the email code")
                await self.filler.pause(1.0, 2.0)
                return True
        except Exception:
            pass
        return False

    async def _finalize(self, account: Account):
        await self._click_done()
        # Dismiss "Add a Passkey" prompt if shown.
        page = self.session.page
        body = await self._body(page)
        if "passkey" in body or "not right now" in body:
            try:
                btn = await page.wait_for_selector(
                    'button:has-text("Not Right Now"), a:has-text("Not Right Now")', timeout=8000)
                if btn and await btn.is_visible():
                    await btn.click()
            except Exception:
                pass
        await self._wait_for_login_redirect()

    async def _click_done(self) -> int:
        """Submit the final Done, retrying when TM reports a spurious OTP error.

        Returns how many times Done was clicked (used by the tests).
        """
        page = self.session.page
        clicks = 0
        await self.filler.pause(0.5, 1.0)
        for attempt in range(4):
            body = await self._body(page)
            if "done" not in body and "confirm your account" not in body:
                break
            # TM sometimes rejects the OTP on the final submit even though it was right;
            # a short wait and another click clears it, so don't abandon the account.
            if attempt and "error" in body and "otp" in body:
                self.log(f"OTP error on Done ({attempt + 1}/4) — waiting and retrying")
                await self.filler.pause(3.0, 5.0)
            try:
                btn = await page.wait_for_selector('button:has-text("Done"), a:has-text("Done")', timeout=8000)
                if btn and await btn.is_visible():
                    await btn.click()
                    clicks += 1
                    await self.filler.pause(2.0, 4.0)
                    try:
                        await page.wait_for_load_state("networkidle", timeout=20000)
                    except Exception:
                        pass
                    after = await self._body(page)
                    if "error" not in after or "otp" not in after:
                        break
            except Exception:
                break
        return clicks

    async def _wait_for_login_redirect(self):
        page = self.session.page
        self.log("Waiting for post-login redirect")
        for _ in range(40):
            await asyncio.sleep(0.5)
            url = page.url
            if "ticketmaster.com" in url and "auth.ticketmaster.com" not in url:
                self.log(f"Redirected to TM: {url[:80]}")
                await asyncio.sleep(3)
                body = await self._body(page)
                if any(s in body for s in ("access denied", "unusual activity", "cloudflare")):
                    raise TmBotBlock("post_login_bot_detected")
                return
        raise FlowError("post_login_redirect_timeout")

    # ── verification codes ───────────────────────────────────────────────────
    async def _get_email_code(self, account: Account, sent_after: float) -> Optional[str]:
        if self.imap:
            try:
                code = await self.imap.wait_for_code(account.email, sent_after=sent_after)
                if code:
                    return code
                self.log("IMAP timed out — asking operator for the email code")
            except Exception as e:
                self.log(f"IMAP error ({e}) — asking operator for the email code")
        return await self.broker.request_manual(
            account.id, "email",
            f"Enter the verification code Ticketmaster emailed to {account.email}",
            timeout=self.config.block.manual_code_timeout,
        )

    async def _get_sms_code(self, account: Account, baseline: Optional[str] = None) -> Optional[str]:
        if self.sms:
            try:
                code = await self.sms.wait_for_code(account.phone, baseline_code=baseline)
                if code:
                    return code
                why = getattr(self.sms, "last_status", "") or "timed out"
                self.log(f"Jivetel: {why} — asking operator for the SMS code")
            except Exception as e:
                self.log(f"Jivetel error ({e}) — asking operator for the SMS code")
        return await self.broker.request_manual(
            account.id, "sms",
            f"Enter the SMS code Ticketmaster texted to {account.phone}",
            timeout=self.config.block.manual_code_timeout,
        )

    async def _screenshot(self, account: Account, label: str):
        tag = account.email.split("@")[0].replace(".", "_")
        await self.session.screenshot(f"{tag}_{label}")
