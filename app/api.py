"""pywebview JS<->Python bridge.

Every method here is callable from the UI as ``window.pywebview.api.<name>(...)``.
The UI polls ``get_state`` a few times a second for the account list, logs, run flag,
and any open code prompts. Everything else (add/reset/delete accounts, start/stop the
run, submit a code) is a direct call.
"""
from __future__ import annotations

import csv
import re
import time
from dataclasses import asdict
from pathlib import Path

from app import credentials
from app.config import AppConfig
from app.events import EventBus
from app.factory.factory import EmailFactory
from app.gmail_dots import canonical_gmail, gmail_single_dot_variants, is_gmail
from app.identity import (city_state_from_zip, fill_identity, provision_account,
                          zip_matches_phone)
from app.orchestrator import Runner
from app.paths import DATA_DIR
from app.sheets import SheetSync, SheetSyncWorker
from app.storage import FAILED, NEEDS_RETRY, PENDING, VERIFIED, Store
from app.verification.code_broker import CodeBroker
from app.verification.imap_client import ImapClient


def _stamp(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if ts else ""


def _parse_stamp(text: str) -> float:
    try:
        return time.mktime(time.strptime(text.strip(), "%Y-%m-%d %H:%M"))
    except Exception:
        return 0.0


class Api:
    def __init__(self):
        self.store = Store()
        self.config = AppConfig.load()
        self.bus = EventBus()
        self.broker = CodeBroker(self.bus)
        self.runner = Runner(self.store, self.config, self.bus, self.broker)
        self.factory = EmailFactory(self.store, self.config, self.bus)
        self.sheet = SheetSyncWorker(self.config, self.store, self.bus)
        self._mirror_state = (None, 0.0)   # (accounts fingerprint, last write time)
        self._mirror_warned = False
        self._sheet_link_try = 0.0
        self._seed_credentials()
        relinked = credentials.relink_file(self.config)
        if relinked:
            self.bus.log(f"Accounts will be saved back to {Path(relinked).stem}.accounts.csv "
                         "in the project folder.")
        # A crash or force-close leaves rows stuck mid-flight; put them back in the queue.
        stale = self.store.recover_stale()
        if stale:
            self.bus.log(f"Recovered {stale} account(s) left running by the last session.",
                         "warn")
        self._backfill_identities()
        linked = credentials.ensure_sheet(self.config)
        if linked:
            self.bus.log("Linked to this team's Google Sheet.")
        if self.config.sheets.enabled and self.config.sheets.spreadsheet_id:
            self.sheet.start()

    def _seed_credentials(self) -> None:
        """Brand-new machine: take the logins from the project folder we were shipped in."""
        seeded = credentials.seed_if_empty(self.config)
        if not seeded:
            return
        path, took = seeded
        bits = [f"{took['inboxes']} email inbox(es)"] if took["inboxes"] else []
        if took["apple_ids"]:
            bits.append(f"{took['apple_ids']} Apple ID(s)")
        if took["jivetel"]:
            bits.append("the Jivetel login")
        self.bus.log(f"Loaded {', '.join(bits)} from {Path(path).name}.", "success")
        self._import_team_accounts(path)

    def _import_team_accounts(self, settings_path: str) -> int:
        """Load the team's existing accounts shipped beside their credentials file.

        Emails already in the database are skipped, so this is safe to re-run and can't
        duplicate or overwrite work done on this machine.
        """
        csv_path = credentials.accounts_file(settings_path)
        if not csv_path:
            return 0
        have = {a.email.lower() for a in self.store.all_accounts()}
        added = 0
        try:
            with open(csv_path, newline="", encoding="utf-8-sig") as f:
                for row in csv.DictReader(f):
                    email = (row.get("email") or "").strip()
                    if not email or email.lower() in have:
                        continue
                    acct = self.store.get(self.store.add_account(
                        email, phone=(row.get("phone") or "").strip(),
                        first_name=row.get("first_name", ""), last_name=row.get("last_name", ""),
                        zip_code=row.get("zip_code", ""),
                        created_from=row.get("created_from", "") or "imported"))
                    acct.address = row.get("address", "")
                    acct.city = row.get("city", "")
                    acct.state = row.get("state", "")
                    acct.tm_password = row.get("tm_password", "")
                    status = (row.get("status") or PENDING).strip().lower()
                    # "in progress" is only ever a snapshot of a run that was cut short.
                    acct.status = PENDING if status == "in_progress" else status
                    acct.email_verified = row.get("email_verified") in ("1", "true", "True")
                    acct.phone_verified = row.get("phone_verified") in ("1", "true", "True")
                    acct.error = row.get("error", "")
                    acct.attempts = int(row.get("attempts") or 0)
                    acct.created_at = _parse_stamp(row.get("created_at", "")) or acct.created_at
                    acct.verified_at = _parse_stamp(row.get("verified_at", ""))
                    self.store.save_account(acct)
                    have.add(email.lower())
                    added += 1
        except Exception as e:
            self.bus.log(f"Couldn't read {csv_path.name}: {e}", "warn")
            return 0
        if added:
            self.bus.log(f"Imported {added} existing account(s) from {csv_path.name}.",
                         "success")
        self._import_side_files(csv_path.parent / csv_path.name.replace(".accounts.csv", ""))
        return added

    def _import_side_files(self, stem: Path) -> None:
        """Restore the phone pool and the minted-alias list shipped with the folder."""
        by_email = {a.email.lower(): a.id for a in self.store.all_accounts()}
        numbers = Path(f"{stem}.numbers.csv")
        if numbers.is_file():
            taken = 0
            with open(numbers, newline="", encoding="utf-8-sig") as f:
                for row in csv.DictReader(f):
                    phone = re.sub(r"\D", "", row.get("phone") or "")
                    if len(phone) != 10 or not self.store.add_number(phone):
                        continue
                    taken += 1
                    if row.get("used") in ("1", "true", "True"):
                        self.store.claim_number(
                            phone, by_email.get((row.get("used_by") or "").lower(), 0))
            if taken:
                self.bus.log(f"Restored {taken} phone number(s) from the project folder.")

        pool = Path(f"{stem}.pool.csv")
        if pool.is_file():
            added = 0
            with open(pool, newline="", encoding="utf-8-sig") as f:
                for row in csv.DictReader(f):
                    email = (row.get("email") or "").strip()
                    if not email or not self.store.add_pool_email(
                            email, created_from=row.get("created_from", "")):
                        continue
                    added += 1
                    if row.get("queued") in ("1", "true", "True"):
                        self.store.mark_pool_queued(email)
                    if row.get("used") in ("1", "true", "True"):
                        self.store.mark_pool_used(email)
            if added:
                self.bus.log(f"Restored {added} iCloud alias record(s) from the project folder.")

    # ── credentials file ─────────────────────────────────────────────────────
    def credential_choices(self) -> list:
        """Teams to offer on first run, when the folder holds more than one team's file."""
        return credentials.pending_choices(self.config)

    def list_credential_files(self) -> list:
        """Credentials files sitting in the project folder, for the Settings dropdown."""
        return [credentials.describe(p) | {"folder": str(p.parent)}
                for p in credentials.find_settings_files()]

    def load_credential_file(self, path: str) -> dict:
        """Switch to another team's file (or re-read one after it was edited)."""
        try:
            took = credentials.apply_settings(
                self.config, credentials.read_settings(Path(path)), Path(path))
        except Exception as e:
            return {"ok": False, "error": f"Couldn't read that file: {e}"}
        if not any((took["inboxes"], took["apple_ids"], took["jivetel"])):
            return {"ok": False, "error": "No logins found in that file."}
        self.bus.log(f"Loaded credentials from {Path(path).name}: {took['inboxes']} inbox(es), "
                     f"{took['apple_ids']} Apple ID(s)"
                     f"{', Jivetel' if took['jivetel'] else ''}"
                     f"{', Google Sheet' if took.get('sheet') else ''}.", "success")
        imported = self._import_team_accounts(path)
        if self.config.sheets.enabled and self.config.sheets.spreadsheet_id:
            self.sheet.start()   # different office, so the mirror re-checks from scratch
            self.sheet.nudge()
        return {"ok": True, "imported": imported, **took}

    # ── email inboxes (the shared Gmail accounts codes forward to) ───────────
    def list_inboxes(self) -> list:
        return [{"username": a.get("username", "")} for a in self.config.imap.accounts]

    def save_inbox(self, username: str, password: str) -> dict:
        username = (username or "").strip()
        if not username or "@" not in username:
            return {"ok": False, "error": "Enter the Gmail address."}
        if username.lower().endswith("@icloud.com"):
            return {"ok": False, "error": "iCloud mailboxes can't be read — use the Gmail "
                                          "address the alias forwards to."}
        password = (password or "").replace(" ", "")
        accounts = list(self.config.imap.accounts)
        for a in accounts:
            if a.get("username", "").lower() == username.lower():
                if password:
                    a["password"] = password
                break
        else:
            if not password:
                return {"ok": False, "error": "Enter the Gmail app password."}
            accounts.append({"username": username, "password": password})
        self.config.imap.accounts = accounts
        self.config.save()
        self.bus.log(f"Saved email inbox {username}.")
        return {"ok": True}

    def delete_inbox(self, username: str) -> dict:
        u = (username or "").strip().lower()
        self.config.imap.accounts = [
            a for a in self.config.imap.accounts if a.get("username", "").lower() != u]
        self.config.save()
        return {"ok": True}

    def test_inboxes(self) -> dict:
        """Prove each Gmail app password still works before a run depends on it."""
        results = []
        for a in self.config.imap.accounts:
            client = ImapClient(self.config.imap.host, self.config.imap.port,
                                a.get("username", ""), a.get("password", ""))
            try:
                client.connect()
                results.append({"username": a.get("username", ""), "ok": True})
            except Exception as e:
                results.append({"username": a.get("username", ""), "ok": False,
                                "error": str(e)[:120]})
            finally:
                client.disconnect()
        good = sum(1 for r in results if r["ok"])
        self.bus.log(f"Email inbox check: {good}/{len(results)} connected.",
                     "success" if good == len(results) and results else "warn")
        return {"ok": True, "results": results}

    def _backfill_identities(self) -> None:
        """Rows created before the app kept an address get one now, so the exported list
        is complete instead of half-blank."""
        taken = self.store.used_full_names()
        filled = 0
        for a in self.store.all_accounts():
            # Also correct rows whose town doesn't belong to their own ZIP.
            city, state = city_state_from_zip(a.zip_code) if a.zip_code else ("", "")
            mismatched = bool(city) and a.city != city
            # A ZIP outside the phone's area code needs re-deriving — but only while the row
            # is still unused. On a verified account the stored ZIP is the record of what was
            # actually submitted to TM, and rewriting it would make our copy a lie.
            bad_zip = (a.status != VERIFIED and bool(a.phone)
                       and not zip_matches_phone(a.zip_code, a.phone))
            if a.address and a.city and not mismatched and not bad_zip:
                continue
            if mismatched:
                a.city, a.state = city, state
            fill_identity(a, taken)
            self.store.save_account(a)
            taken.add(f"{a.first_name} {a.last_name}".lower())
            filled += 1
        if filled:
            self.bus.log(f"Filled in the address details on {filled} older account(s).")

    # ── accounts ─────────────────────────────────────────────────────────────
    def add_accounts(self, text: str) -> dict:
        """Bulk add from pasted lines. Each line: email[, phone[, gmail_app_password]].
        Commas or tabs separate fields; blank lines and '#' comments are ignored."""
        added = 0
        for line in (text or "").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = [p.strip() for p in line.replace("\t", ",").split(",")]
            email = parts[0] if parts else ""
            if not email or "@" not in email:
                continue
            phone = parts[1] if len(parts) > 1 else ""
            imap_pw = parts[2].replace(" ", "") if len(parts) > 2 else ""
            provision_account(self.store, email, created_from="added by hand",
                              imap_password=imap_pw, phone=phone)
            added += 1
        self.bus.log(f"Added {added} account(s).")
        return {"added": added}

    def delete_account(self, account_id: int) -> dict:
        self.store.delete_account(int(account_id))
        return {"ok": True}

    def reset_account(self, account_id: int) -> dict:
        self.store.reset_account(int(account_id))
        self.bus.log(f"Reset account #{account_id} to pending.")
        return {"ok": True}

    def reset_failed(self) -> dict:
        """Put every failed / half-finished row back in the queue in one go."""
        n = self.store.reset_by_status((FAILED, NEEDS_RETRY, "email_verified",
                                        "phone_verified"))
        self.bus.log(f"Reset {n} account(s) back to pending.")
        return {"ok": True, "reset": n}

    _CSV_COLS = ["id", "email", "first_name", "last_name", "phone", "address", "city",
                 "state", "zip_code", "tm_password", "status", "email_verified",
                 "phone_verified", "created_from", "created_at", "verified_at",
                 "error", "attempts"]

    def _write_accounts_csv(self, path: Path) -> int:
        accounts = self.store.all_accounts()
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(self._CSV_COLS)
            for a in accounts:
                w.writerow([a.id, a.email, a.first_name, a.last_name, a.phone, a.address,
                            a.city, a.state, a.zip_code, a.tm_password, a.status,
                            int(a.email_verified), int(a.phone_verified), a.created_from,
                            _stamp(a.created_at), _stamp(a.verified_at), a.error,
                            a.attempts])
        return len(accounts)

    def export_csv(self) -> dict:
        """Dump every account (name, full address, password, dates) to a CSV — the
        Google-Sheet equivalent the operator can open in Excel."""
        path = DATA_DIR / "accounts_export.csv"
        n = self._write_accounts_csv(path)
        self.bus.log(f"Exported {n} account(s) to {path.name}.")
        return {"ok": True, "path": str(path)}

    def _write_side_csvs(self, stem: Path) -> None:
        """The phone pool and the minted-alias list, so they travel with the folder too."""
        by_id = {a.id: a.email for a in self.store.all_accounts()}
        with open(f"{stem}.numbers.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["phone", "used", "used_by", "created_at"])
            for n in self.store.list_numbers():
                w.writerow([n["phone"], int(bool(n["used"])),
                            by_id.get(n["account_id"], ""), _stamp(n["created_at"])])
        with open(f"{stem}.pool.csv", "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["email", "created_from", "queued", "used", "used_on", "created_at"])
            for p in self.store.list_pool():
                w.writerow([p["email"], p["created_from"], int(bool(p["queued"])),
                            int(bool(p["used"])), p["used_on"], _stamp(p["created_at"])])

    def _mirror_to_team_file(self, accounts: list) -> None:
        """Keep the team's CSVs in the project folder up to date.

        The database itself lives in %LOCALAPPDATA% and can't travel, so every result is
        written back beside the credentials file. Zip the project folder and the finished
        accounts, the phone pool and the iCloud aliases go with it — carry it to another
        computer and they are all there.
        """
        if not self.config.credentials_file:
            return
        fingerprint = hash((
            tuple((a["id"], a["status"], a["tm_password"], a["verified_at"]) for a in accounts),
            tuple((n["phone"], n["used"]) for n in self.store.list_numbers()),
            tuple((p["email"], p["used"], p["queued"]) for p in self.store.list_pool()),
        ))
        now = time.time()
        if fingerprint == self._mirror_state[0] or now - self._mirror_state[1] < 3:
            return
        settings = Path(self.config.credentials_file)
        try:
            self._write_accounts_csv(settings.parent / (settings.stem + ".accounts.csv"))
            self._write_side_csvs(settings.parent / settings.stem)
        except Exception as e:
            if not self._mirror_warned:
                self._mirror_warned = True
                self.bus.log(f"Couldn't save accounts back to the project folder: {e}", "warn")
            return
        self._mirror_state = (fingerprint, now)

    # ── run control ────────────────────────────────────────────────────────--
    def start_run(self) -> dict:
        if self.bus.running:
            return {"ok": False, "error": "already running"}
        self._assign_numbers()
        self.runner.start()
        return {"ok": True}

    def stop_run(self) -> dict:
        self.runner.stop()
        self.bus.log("Stop requested — finishing current step.", "warn")
        return {"ok": True}

    def run_account(self, account_id: int) -> dict:
        """Run a single account now (the per-row Run button)."""
        if self.bus.running:
            return {"ok": False, "error": "A run is already in progress."}
        a = self.store.get(int(account_id))
        if not a:
            return {"ok": False, "error": "not found"}
        if a.status == "verified":
            return {"ok": False, "error": "already verified"}
        self._assign_numbers()
        self.runner.start([int(account_id)])
        self.bus.log(f"Running account #{account_id} ({a.email}).")
        return {"ok": True}

    def run_accounts(self, account_ids: list) -> dict:
        """Run several selected accounts at once (up to max_concurrent in parallel)."""
        if self.bus.running:
            return {"ok": False, "error": "A run is already in progress."}
        ids = []
        for i in account_ids or []:
            a = self.store.get(int(i))
            if a and a.status != "verified":
                ids.append(int(i))
        if not ids:
            return {"ok": False, "error": "No runnable accounts selected."}
        self._assign_numbers()
        self.runner.start(ids)
        self.bus.log(f"Running {len(ids)} selected account(s), "
                     f"{self.config.max_concurrent} at a time.")
        return {"ok": True, "count": len(ids)}

    # ── email factory (iCloud Hide My Email) ─────────────────────────────────
    def get_icloud(self) -> dict:
        """Apple IDs configured for the factory (passwords never leave the backend)."""
        accts = [{"email": a.get("email", "")} for a in self.config.icloud.accounts]
        return {"accounts": accts, "running": self.factory.running,
                "pool_unqueued": self.store.count_pool(queued=False)}

    def save_icloud(self, email: str, password: str) -> dict:
        """Add or update one Apple ID (upsert by email). Call again to add more."""
        email = (email or "").strip()
        if not email or "@" not in email:
            return {"ok": False, "error": "Enter a valid Apple ID email."}
        accts = list(self.config.icloud.accounts)
        for a in accts:
            if a.get("email", "").lower() == email.lower():
                if password:
                    a["password"] = password
                break
        else:
            if not password:
                return {"ok": False, "error": "Enter the Apple ID password."}
            accts.append({"email": email, "password": password})
        self.config.icloud.accounts = accts
        self.config.icloud.enabled = True
        self.config.save()
        self.bus.log(f"iCloud: saved Apple ID {email}.")
        return {"ok": True}

    def delete_icloud(self, email: str) -> dict:
        email = (email or "").strip().lower()
        self.config.icloud.accounts = [
            a for a in self.config.icloud.accounts if a.get("email", "").lower() != email]
        self.config.save()
        return {"ok": True}

    def factory_generate(self, count: int, reset_login: bool = False, only: str = "") -> dict:
        return self.factory.start(int(count), bool(reset_login), (only or "").strip())

    def factory_cancel(self) -> dict:
        self.factory.cancel()
        self.bus.log("Email factory: stop requested.", "warn")
        return {"ok": True}

    def list_pool(self) -> list:
        return self.store.list_pool()

    def delete_pool_email(self, pool_id: int) -> dict:
        self.store.delete_pool_email(int(pool_id))
        return {"ok": True}

    def pool_to_accounts(self, pool_ids: list) -> dict:
        """Push pool emails into the Accounts queue as ready-to-run rows. New aliases go
        there by themselves; this is for leftovers minted before that was automatic."""
        added = 0
        wanted = {int(p) for p in pool_ids or []}
        for row in self.store.list_pool():
            if row["id"] in wanted and not row["queued"]:
                provision_account(self.store, row["email"], created_from=row["created_from"])
                self.store.mark_pool_queued(row["email"])
                added += 1
        self.bus.log(f"Moved {added} email(s) into Accounts, ready to run.")
        return {"ok": True, "added": added}

    # ── Gmail dot-variation generator ─────────────────────────────────────────
    def _taken_by_canon(self) -> dict:
        """canonical Gmail -> set of variant addresses already used by an account."""
        taken: dict[str, set] = {}
        for a in self.store.all_accounts():
            if is_gmail(a.email):
                taken.setdefault(canonical_gmail(a.email), set()).add(a.email.strip().lower())
        return taken

    def _gmail_bases(self) -> list[str]:
        """Base (canonical) Gmails: operator-added ones + any discovered from accounts."""
        bases: dict[str, bool] = {}
        for b in self.config.gmail_bases:
            if is_gmail(b):
                bases[canonical_gmail(b)] = True
        for a in self.store.all_accounts():
            if is_gmail(a.email):
                bases[canonical_gmail(a.email)] = True
        return list(bases.keys())

    def list_gmail_bases(self) -> list:
        """Per-base tally: how many single-dot variants exist / used / still free."""
        taken = self._taken_by_canon()
        out = []
        for base in self._gmail_bases():
            variants = gmail_single_dot_variants(base)
            used = len(taken.get(base, set()) & set(variants))
            out.append({"base": base, "total": len(variants),
                        "used": used, "remaining": len(variants) - used})
        out.sort(key=lambda x: x["base"])
        return out

    def add_gmail_bases(self, text: str) -> dict:
        existing = {canonical_gmail(b) for b in self.config.gmail_bases if is_gmail(b)}
        added = 0
        for tok in re.split(r"[\s,;]+", text or ""):
            tok = tok.strip()
            if tok and is_gmail(tok):
                c = canonical_gmail(tok)
                if c not in existing:
                    self.config.gmail_bases.append(c)
                    existing.add(c)
                    added += 1
        self.config.save()
        self.bus.log(f"Added {added} base Gmail(s).")
        return {"ok": True, "added": added}

    def delete_gmail_base(self, base: str) -> dict:
        c = canonical_gmail(base)
        self.config.gmail_bases = [
            b for b in self.config.gmail_bases if canonical_gmail(b) != c]
        self.config.save()
        return {"ok": True}

    def generate_gmail_emails(self, count: int, bases: list | None = None) -> dict:
        """Create ``count`` fresh period-Gmail accounts (next unused single-dot variants,
        spread evenly across the chosen bases), each with a generated name. A phone number
        is pulled from the pool automatically if one is free; ZIP is derived from it.

        ``bases`` is the operator's tick-list. Without it every known base is fair game,
        which is rarely what's wanted once the list has grown from past accounts.
        """
        count = int(count)
        if count <= 0:
            return {"ok": False, "error": "Enter how many emails to generate."}
        known = self._gmail_bases()
        if not known:
            return {"ok": False, "error": "Add at least one base Gmail first."}
        if bases is None:
            bases = known
        else:
            wanted = {canonical_gmail(b) for b in bases if is_gmail(b)}
            bases = [b for b in known if b in wanted]
            if not bases:
                return {"ok": False, "error": "Tick at least one base Gmail to generate from."}
        taken = self._taken_by_canon()
        # unused variants per base, then interleave so bases are consumed evenly
        per_base = []
        for base in sorted(bases):
            used = taken.get(base, set())
            per_base.append([v for v in gmail_single_dot_variants(base) if v not in used])
        interleaved = []
        for k in range(max((len(x) for x in per_base), default=0)):
            for lst in per_base:
                if k < len(lst):
                    interleaved.append(lst[k])
        chosen = interleaved[:count]
        if not chosen:
            return {"ok": False,
                    "error": f"Every variation of the {len(bases)} selected base(s) is "
                             "already used. Tick another base or add a new one."}
        assigned = 0
        for variant in chosen:
            row = provision_account(self.store, variant,
                                    created_from=canonical_gmail(variant))
            assigned += 1 if row.phone else 0
        short = len(chosen) < count
        msg = (f"Generated {len(chosen)} email(s) from {len(bases)} base(s); "
               f"assigned {assigned} phone number(s).")
        if short:
            msg += (f" Only {len(chosen)} variations were free on the selected bases — "
                    "tick more bases for the rest.")
        self.bus.log(msg)
        return {"ok": True, "created": len(chosen), "assigned": assigned,
                "short": short, "message": msg}

    # ── phone-number pool ──────────────────────────────────────────────────--
    def add_numbers(self, text: str) -> dict:
        added = 0
        for tok in re.split(r"[\s,;]+", text or ""):
            d = re.sub(r"\D", "", tok)
            if len(d) == 11 and d.startswith("1"):
                d = d[1:]
            if len(d) == 10 and self.store.add_number(d):
                added += 1
        self.bus.log(f"Added {added} phone number(s) to the pool.")
        return {"ok": True, "added": added}

    def delete_number(self, number_id: int) -> dict:
        self.store.delete_number(int(number_id))
        return {"ok": True}

    def assign_numbers(self) -> dict:
        n = self._assign_numbers()
        self.bus.log(f"Assigned {n} phone number(s) to number-less rows.")
        return {"ok": True, "assigned": n}

    def _assign_numbers(self) -> int:
        """Give each runnable, number-less account the next free pool number, then fill
        its ZIP from that number. Stops when the pool runs dry."""
        assigned = 0
        for a in self.store.runnable():
            if a.phone:
                continue
            phone = self.store.take_number(a.id)
            if not phone:
                break
            a.phone = phone
            fill_identity(a)
            self.store.save_account(a)
            assigned += 1
        return assigned

    # ── code prompts ───────────────────────────────────────────────────────--
    def submit_code(self, prompt_id: str, code: str) -> dict:
        # A prompt is either a signup code (broker) or a factory 2FA code — try both.
        ok = self.broker.submit(prompt_id, code) or self.factory.submit_code(prompt_id, code)
        return {"ok": ok}

    def cancel_prompt(self, prompt_id: str) -> dict:
        ok = self.broker.cancel(prompt_id) or self.factory.cancel_prompt(prompt_id)
        return {"ok": ok}

    # ── config ───────────────────────────────────────────────────────────────
    def get_config(self) -> dict:
        return asdict(self.config)

    def save_config(self, data: dict) -> dict:
        try:
            self.config.max_concurrent = int(data.get("max_concurrent", self.config.max_concurrent))
            if "launch_jitter" in data:
                self.config.launch_jitter = float(data.get("launch_jitter"))
            b = data.get("browser", {})
            self.config.browser.headless = bool(b.get("headless", self.config.browser.headless))
            self.config.browser.channel = b.get("channel", self.config.browser.channel) or "chrome"
            blk = data.get("block", {})
            self.config.block.max_retries = int(blk.get("max_retries", self.config.block.max_retries))
            jv = data.get("jivetel", {})
            self.config.jivetel.username = jv.get("username", self.config.jivetel.username).strip()
            self.config.jivetel.password = jv.get("password", self.config.jivetel.password)
            if jv.get("portal_url"):
                self.config.jivetel.portal_url = jv["portal_url"].strip()
            # Auto-enable when credentials are present.
            self.config.jivetel.enabled = bool(
                self.config.jivetel.username and self.config.jivetel.password)
            self.config.save()
            self.bus.log("Settings saved.")
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # ── google sheet mirror ──────────────────────────────────────────────────-
    def _relink_sheet(self) -> None:
        """Pick up ``config/service_account.json`` if it is dropped in while we're running.

        The key never ships inside the code repo, so a cloned copy starts unlinked. Watching
        for it here saves the operator from restarting the app after adding the file.
        """
        if time.time() - self._sheet_link_try < 15:
            return
        self._sheet_link_try = time.time()
        self.config.sheets.credentials_file = ""
        if credentials.ensure_sheet(self.config):
            self.bus.log("Found this team's Google Sheet key — finished accounts will be "
                         "copied over again.", "success")
            self.sheet.start()

    def sheet_status(self) -> dict:
        s = self.config.sheets
        if not (s.credentials_file and Path(s.credentials_file).is_file()):
            self._relink_sheet()
            s = self.config.sheets
        return {"enabled": bool(s.enabled and s.spreadsheet_id),
                "spreadsheet_id": s.spreadsheet_id,
                "has_key": bool(s.credentials_file and Path(s.credentials_file).is_file()),
                "last_run": self.sheet.last_run, "error": self.sheet.last_error}

    def sync_sheet(self) -> dict:
        """Push now, ignoring the 'nothing changed' shortcut (the Sync now button)."""
        s = self.config.sheets
        if not s.spreadsheet_id:
            return {"ok": False, "error": "No Google Sheet is linked to this team's file."}
        try:
            res = self.sheet.sync(force=True)
        except Exception as e:
            self.sheet.last_error = f"{type(e).__name__}: {str(e)[:160]}"
            return {"ok": False, "error": self.sheet.last_error}
        if not res.get("ok"):
            return res
        return {"ok": True,
                "message": f"Sheet updated: {res['appended']} account(s) added, "
                           f"{res['aliases']} iCloud alias(es) added."}

    def test_sheet(self) -> dict:
        s = self.config.sheets
        if not (s.spreadsheet_id and s.credentials_file):
            return {"ok": False, "error": "No sheet configured."}
        try:
            return {"ok": True, "message": SheetSync(
                s.credentials_file, s.spreadsheet_id).check()}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {str(e)[:200]}"}

    # ── state polled by the UI ────────────────────────────────────────────────
    def get_state(self) -> dict:
        accounts = [a.as_dict() for a in self.store.all_accounts()]
        self._mirror_to_team_file(accounts)
        snap = self.bus.snapshot()
        counts = {"total": len(accounts)}
        for a in accounts:
            counts[a["status"]] = counts.get(a["status"], 0) + 1
        numbers = self.store.list_numbers()
        return {"accounts": accounts, "counts": counts, "pool": self.store.list_pool(),
                "icloud_accounts": [{"email": a.get("email", "")}
                                    for a in self.config.icloud.accounts],
                "numbers": numbers,
                "unused_numbers": sum(1 for n in numbers if not n["used"]),
                "gmail_bases": self.list_gmail_bases(), "sheet": self.sheet_status(),
                "now": time.time(), **snap}
