"""App configuration — small, JSON-persisted, all local.

Unlike the original project (YAML + Sheets + proxies + Gemini + iCloud factory),
the desktop app only needs a handful of knobs: how human-like the typing is, the
Gmail IMAP defaults for auto-reading codes, and the block-handling backoff. Anything
secret (per-inbox app passwords) is stored per-account in the local DB, not here.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields

from app.paths import CONFIG_PATH


@dataclass
class BrowserConfig:
    # channel="chrome" uses the operator's installed Google Chrome (their real
    # fingerprint + IP), which is what clears Ticketmaster's bot wall.
    channel: str = "chrome"
    headless: bool = False
    typing_delay_min: int = 80
    typing_delay_max: int = 200
    action_delay_min: float = 1.0
    action_delay_max: float = 4.0


@dataclass
class ImapConfig:
    # Gmail only. iCloud Mail can't be IMAP'd, so HME aliases must forward to a Gmail
    # inbox and we read the code there. host/port are Gmail's; per-account app
    # passwords live on each account row.
    host: str = "imap.gmail.com"
    port: int = 993
    poll_interval: int = 5
    timeout: int = 150
    keepalive_interval: int = 60   # NOOP an idle inbox this often so the socket survives
    relogin_backoff: int = 300     # wait this long before retrying a failed inbox login
    # Shared "catch-all" inboxes to poll when an account has no per-account app password.
    # Codes for many aliases forward here; we match by the message's To: recipient.
    # Each item: {"username": "inbox@gmail.com", "password": "app password"}.
    accounts: list = field(default_factory=list)


@dataclass
class JivetelConfig:
    # SMS provider portal. The operator enters these once; the app logs in and scrapes
    # the TM code by phone number (same as the original VPS automation). If left blank,
    # the app falls back to prompting the operator to type the SMS code by hand.
    enabled: bool = False
    portal_url: str = "https://portal.jivetel.net/portal/messages"
    username: str = ""
    password: str = ""
    poll_interval: int = 5
    timeout: int = 150


@dataclass
class ICloudConfig:
    # iCloud "Hide My Email" factory. The operator enters one or more Apple IDs (each
    # needs an active iCloud+ plan). The app mints HME aliases via Apple's API — no
    # browser — and stores them in the local pool. 2FA codes are typed into the UI.
    # Caps/cooldowns mirror the original VPS factory so Apple doesn't flag the account.
    enabled: bool = False
    accounts: list = field(default_factory=list)  # [{"email": .., "password": ..}]
    sms_code_timeout: int = 600          # how long the 2FA prompt waits (seconds)
    per_email_delay: int = 45            # base gap between emails in a batch (± jitter)
    generate_reserve_gap: float = 7.0    # gap between generate() and reserve()
    batch_size: int = 5                  # aliases per burst before resting
    batch_cooldown: int = 2700           # rest after a clean batch (± 20%)
    hourly_cap_per_account: int = 5      # max creates per rolling hour / Apple ID
    daily_cap_per_account: int = 25      # max creates per calendar day / Apple ID
    default_cooldown_wait: int = 3600    # first rate-limit backoff
    escalated_cooldown_wait: int = 5400  # second rate-limit backoff


@dataclass
class BlockConfig:
    # On a TM bot-block we wipe cookies and wait before retrying, escalating each time.
    max_retries: int = 2
    cooldowns: list = field(default_factory=lambda: [120, 300, 900])  # 2m, 5m, 15m
    manual_code_timeout: int = 300  # how long a UI code prompt stays open
    # A throttle ("Almost there" that never clears) is TM rate-limiting the IP, not a
    # fingerprint problem, so it gets its own much longer schedule: the row is put back in
    # the queue for later instead of being retried immediately, and after enough throttles
    # in a row the whole app stands down — hammering a throttled IP is how it gets flagged.
    throttle_backoffs: list = field(default_factory=lambda: [1800, 3600, 7200])  # 30m,1h,2h
    max_throttle_strikes: int = 3          # strikes before the row is failed for good
    throttle_pause_after: int = 3          # consecutive throttles that pause everything
    throttle_pauses: list = field(default_factory=lambda: [1800, 7200])  # 30m, then 2h


@dataclass
class SheetsConfig:
    # One-way mirror into the office's Google Sheet: verified accounts land on the
    # accounts tab, minted iCloud aliases on pool_emails. Waiting rows stay local —
    # the VPS daemon claims any accounts-tab row that isn't verified yet.
    enabled: bool = True
    credentials_file: str = ""   # service_account.json shipped in the project folder
    spreadsheet_id: str = ""     # per office, read from the team's settings file


@dataclass
class AppConfig:
    max_concurrent: int = 1  # one visible Chrome at a time by default
    # Base Gmail addresses the operator owns; the dot-generator mints unused single-dot
    # variants of these (bases are also auto-discovered from existing accounts).
    gmail_bases: list = field(default_factory=list)
    # Random 0..N second delay before each account launches. Desyncs concurrent sessions
    # on one machine AND multiple machines sharing one public IP, so TM never sees a
    # lockstep burst of signups from the same address.
    launch_jitter: float = 8.0
    # The team credentials file this copy was set up from. Finished accounts are mirrored
    # back next to it, so the project folder always carries the latest results with it.
    credentials_file: str = ""
    browser: BrowserConfig = field(default_factory=BrowserConfig)
    imap: ImapConfig = field(default_factory=ImapConfig)
    jivetel: JivetelConfig = field(default_factory=JivetelConfig)
    icloud: ICloudConfig = field(default_factory=ICloudConfig)
    sheets: SheetsConfig = field(default_factory=SheetsConfig)
    block: BlockConfig = field(default_factory=BlockConfig)

    # ── persistence ──────────────────────────────────────────────────────────
    @classmethod
    def load(cls) -> "AppConfig":
        if not CONFIG_PATH.exists():
            cfg = cls()
            cfg.save()
            return cfg
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        return cls(
            max_concurrent=raw.get("max_concurrent", 1),
            gmail_bases=raw.get("gmail_bases", []),
            launch_jitter=raw.get("launch_jitter", 8.0),
            credentials_file=raw.get("credentials_file", ""),
            browser=BrowserConfig(**_subset(raw.get("browser", {}), BrowserConfig)),
            imap=ImapConfig(**_subset(raw.get("imap", {}), ImapConfig)),
            jivetel=JivetelConfig(**_subset(raw.get("jivetel", {}), JivetelConfig)),
            icloud=ICloudConfig(**_subset(raw.get("icloud", {}), ICloudConfig)),
            sheets=SheetsConfig(**_subset(raw.get("sheets", {}), SheetsConfig)),
            block=BlockConfig(**_subset(raw.get("block", {}), BlockConfig)),
        )

    def save(self) -> None:
        CONFIG_PATH.write_text(json.dumps(asdict(self), indent=2), encoding="utf-8")


def _subset(raw: dict, dc) -> dict:
    """Keep only keys that are real fields of the dataclass (tolerates old configs)."""
    valid = {f.name for f in fields(dc)}
    return {k: v for k, v in raw.items() if k in valid}
