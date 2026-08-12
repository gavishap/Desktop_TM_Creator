# TM Desktop — self-contained Ticketmaster account creator

A standalone Windows desktop app that creates verified Ticketmaster accounts **entirely
on the operator's own computer**: their installed Google Chrome, their own internet
connection (no proxies), a local database, and a native window UI. Finished accounts are
copied back into the office's Google Sheet, but nothing is ever *read* from it.
Zip it, hand it to anyone, they unzip and double-click — no setup, no accounts to wire
up on a server.

It mirrors the signup flow of the original VPS automation (auth page → email → profile →
email code → phone → SMS code → done), but strips out every remote dependency.

---

## Why it works where the VPS didn't

Ticketmaster's bot wall (Kasada + NuData behavioral scoring + their EPS abuse layer)
flags datacenter/residential **proxy** IPs and non-standard browsers. In testing, a
**real Chrome on a real residential IP** cleared it. This app is exactly that: it drives
the operator's own installed Chrome over their own connection, with human-like typing,
cursor motion, and dwell time so NuData scores the session as human.

---

## For the operator (using the packaged app)

The app is shipped inside a copy of the `ticketmaster-accounts` project folder, which
already carries the team's logins in `config/settings*.yaml`. **On first run the app finds
that file by itself** and loads the Gmail inboxes, the Apple IDs and the Jivetel login, so
there is nothing to type in on a new laptop.

If the folder holds more than one team's file, the app asks **"Which team is this computer
for?"** on first launch and lists them by their Jivetel login and Apple ID. Pick one and
it's remembered. That means every team can be sent the same zip.

**Accounts travel with the folder, in both directions.** A CSV named after the credentials
file — `config/settings.accounts.csv` next to `config/settings.yaml` — holds the team's
accounts:

- **On the way in:** it is imported when that team is chosen, so finished accounts and
  their Ticketmaster passwords appear on the new machine. Emails already in the local
  database are skipped, so importing can never duplicate or overwrite local work.
- **On the way out:** every account created afterwards is written straight back to it,
  within a second of finishing. Nothing to remember and no button to press.

So zipping the project folder and opening it on another computer carries the whole history
with it. (**Export CSV** still exists for a spreadsheet you can open in Excel; it writes to
your app-data folder and is separate from this.)

One caveat: two computers should not share one project folder over a network drive at the
same time, since each writes its own full list and the last one to save wins. Passing the
folder from machine to machine is fine — the import runs before anything is written back,
so nothing is lost.

1. Unzip and double-click **`TM Desktop.exe`**. (Google Chrome must be installed.) The
   green line at the bottom confirms what it loaded, e.g. *"Loaded 4 email inbox(es),
   2 Apple ID(s), the Jivetel login from settings.yaml."*
2. Open **⚙ Settings** and check the top three sections:
   - **Credentials file** — if the folder holds more than one team's file, pick yours and
     click **Load**.
   - **Email inboxes** — the Gmail accounts codes arrive in. **Test all** proves each app
     password still works. Add or remove inboxes here at any time.
   - **SMS (Jivetel)** — the portal login the app reads SMS codes from. Leave blank to type
     SMS codes by hand instead.
3. Click **+ Add** and paste your list, one account per line:
   ```
   email, phone, gmail_app_password
   alias1@icloud.com, 4075551234, abcd efgh ijkl mnop
   alias2@icloud.com, 3125559876
   ```
   - **email** — the address the account is created with (e.g. an iCloud Hide My Email
     alias that forwards to Gmail, or any email you can read).
   - **phone** — the number provisioned in Jivetel that will receive the SMS.
   - **gmail_app_password** — *optional*. If given, the app reads the emailed code
     automatically from that Gmail inbox. If left out, the app pops up a box and you type
     the emailed code in yourself.
4. Click **Start**. A Chrome window opens and drives the signup. Watch it live.
5. Codes are handled automatically: the **email** code from Gmail (if an app password was
   given), the **SMS** code from Jivetel. If either can't be read, a prompt appears and
   you type it in from your email or phone.
6. Results are saved locally and shown in the table: name, street address, city/state, ZIP,
   the generated Ticketmaster password, and the date. Use **▶** to run one row, **↺** to
   retry a row, **✕** to delete it, **Reset failed** to put every failed row back in the
   queue at once, and **Export CSV** for the whole list as a spreadsheet.

**If Ticketmaster blocks a row**, the browser closes (so its cookies are gone) and the row
goes back in the queue with a countdown, each wait longer than the last: **5 minutes, 20
minutes, 1 hour 10 minutes, 2 hours 30 minutes, 5 hours.** Coming straight back after a
block is what turns a soft block into a sticky one, so the long waits are the point.
**Retries after a fail** in Settings decides how many of those waits a row gets (5 by
default = all of them; 2 would stop after the 20-minute one). Waiting rows don't hold up
anything — the next account starts straight away and they come back by themselves as long as
the app is open. After the last try the row is marked failed and you can reset it any time.

**If Ticketmaster throttles the connection** (the "Almost there" page that never clears),
that's your internet address being rate-limited, not the browser — so the row is parked
instead of retried. It shows **⏳ 30m** and comes back on its own when the wait is up; the
second time it waits an hour, the third time the row is failed. If three accounts in a row
get throttled the app pauses everything for half an hour, because pushing harder on a
throttled address is what gets it flagged. No clicking is needed for any of this.

If the app is force-closed mid-run, the rows that were in flight are put back in the queue
the next time it opens.

Your data lives in `%LOCALAPPDATA%\TMDesktop\` (database, logs, screenshots).

### Running several at once / running a selection

- **Start** runs every pending row, several at a time. How many run in parallel is
  **Concurrent Chrome windows** in Settings (1–5) — set it to 3 and three Chrome windows
  work at the same time.
- To run only some rows, tick their checkboxes (or the header box to select all) and click
  **Run selected**. They run together, up to the same concurrency limit.
- The per-row **▶** button runs just that one account.

### The four screens

The bar under the title switches screens: **Accounts**, **iCloud Emails**, **Gmail Emails**,
**Phone Numbers**.

The **Activity** button in the top bar folds the log panel in and out, and remembers your
choice. It's folded away by default so the table gets the full width. The strip along the
bottom always shows the newest line (coloured red/amber when something goes wrong), and
clicking it opens the full log. The dot on the Activity button is green while a run is
going. Code prompts appear as their own box regardless, so nothing gets missed.

### iCloud Emails (create "Hide My Email" addresses)

The **iCloud Emails** screen mints `@icloud.com` aliases from your Apple ID the same way the
old Google-Sheet factory did — no browser, straight through Apple's API.

1. **Apple ID** — normally already filled in from the credentials file. To add one, type the
   Apple ID email + the **normal password you sign in to icloud.com with** and click **Save**
   (needs an active iCloud+ plan). An *app-specific* password will not work here: those only
   authorise mail apps, and this app never reads iCloud mailboxes. You can add more than one
   Apple ID; the factory rotates between them and respects Apple's limits (5/hour, 25/day
   each) so the account isn't flagged.
2. **Generate** — enter how many aliases you want and click **Generate**. Progress shows
   live ("created 3/10"). The dropdown beside the count chooses which Apple ID to mint on;
   left on **any Apple ID** it starts at the top of the list and moves to the next once that
   one is capped, so pick a specific ID when you want to work one minter on its own.
3. **Apple code** — the first time (or when Apple's trust expires) a box pops up asking for
   the code Apple texts/pushes to your device. Type it in and it continues. Tick **fresh
   login** to force a new sign-in.
4. **Straight into Accounts** — each alias is added to the Accounts queue as it's minted,
   already carrying a name, street address, ZIP and a phone number from the pool. Nothing to
   move by hand; select the new rows and run them.
5. **Pool** — the table is the record of what was made. Each row shows whether it's *in
   Accounts* or *verified* (with the date it produced a working account), the same way the
   Sheet's staging tab tracked it. Copy with **⧉**, delete with **✕**. Aliases minted before
   this was automatic can still be pushed over with **Add … to Accounts**.

> The alias forwards mail to whatever Gmail you set as its **Forward To** in iCloud — set
> that once in iCloud settings so the app can read Ticketmaster's email code from Gmail.

### Gmail Emails + Phone Numbers

Gmail ignores dots, so `s.pencer…` and `sp.encer…` both land in `spencer…`'s inbox. The
**Gmail Emails** screen mints fresh dotted addresses from your own Gmails — no pasting.

- **Step 1 — pick the bases.** The list holds every base the app knows: the ones it found in
  your existing accounts plus any you add in the box (comma or space separated). Each shows
  how many single-dot variations exist, how many are used and how many are still free. Tick
  the ones this batch should draw from — **only ticked bases are used** — and the line beside
  the heading keeps a running count of ticked bases and free variations. *Tick all* /
  *Untick all* are there for speed, and the choice is remembered between sessions.
- **Step 2 — how many.** Type a number and click. The app makes that many **new** variations
  it has never used before, spread evenly across the ticked bases, each with a name, street
  address and ZIP, and adds them to Accounts. It never repeats a variation or reuses a full
  name, and says so if the ticked bases run out of fresh ones.
- **Phone Numbers screen** — paste the numbers you have; the app tracks which are used and
  which account each went to. When you generate emails (or run a row that has no number), it
  automatically takes the next unused number, attaches it to the row, and fills the ZIP from
  that number's area code. Use **Assign to email rows** to fill number-less rows on demand.

So the normal flow is: add numbers once → click **Generate period emails** → the new rows
come out already paired with a number, name, and ZIP → select them and **Run selected**.

### Google Sheet (what goes back to the office)

The app keeps its own database, but the office's sheet stays the record everyone else reads,
so two things are copied into it automatically, in the background, roughly every minute:

- **Verified accounts** land on the **TM Accounts** tab in the same layout the VPS used
  (email, phone, name, ZIP, password, Y/Y, `verified`, timestamp, address, city, state), in
  the first free row, and the row is turned green. A row that is already on the sheet is
  never duplicated: the run's outcome is filled in on the existing row instead, and cells
  the office typed in by hand are left alone.
- **Every iCloud alias** the factory mints goes onto the **pool_emails** tab with the Apple
  ID that made it — same as the old email factory — and its *used* box gets ticked once the
  account built from it verifies.

Rows that are still waiting are deliberately **not** sent: a VPS daemon polling the same
sheet would grab them and try to create them through its blocked proxies. They travel with
the folder in `config/<team>.accounts.csv` instead.

Which sheet is used comes from the team's own credentials file, and the key
(`config/service_account.json`) ships in the folder, so there is nothing to set up. Settings
shows the link status with **Sync now** and **Test connection** buttons. If the machine is
offline the app carries on as normal and catches up on the next sync.

### Running on several computers at once (same office / same internet)

Each machine runs its own independent copy — its own database, its own settings, its own
Chrome. There's no shared file or server, so they never conflict. Two things to know when
2–3 machines share one internet connection (one public IP):

- **Give each machine a different set of accounts** (different emails *and* different
  phone numbers). The app doesn't dedupe across machines, so overlapping lists would
  create the same account twice.
- **The app auto-desyncs the shared IP.** Every account waits a random *launch jitter*
  (Settings, default 8s) before its Chrome opens, so three machines that press Start
  together don't hit Ticketmaster's auth in lockstep. Keep jitter above 0 in this setup;
  raise it (e.g. 15–20s) if you ever see blocks. Keep **Concurrent windows = 1** per
  machine so the shared IP handles a handful of signups at a time, not a burst.
- **Jivetel:** matching is by phone number, so machines can share one Jivetel login
  safely — each only ever reads its own numbers. If the portal logs sessions out on each
  other, give each machine its own Jivetel login instead.

---

## For the developer (run from source / build the .exe)

```powershell
git clone https://github.com/gavishap/Desktop_TM_Creator.git
cd Desktop_TM_Creator
py -m venv venv
venv\Scripts\pip install -r requirements.txt
venv\Scripts\python main.py            # run from source
```

**Credentials are deliberately not in the repo.** They are handed over separately as a
`config/` folder — drop it in next to `main.py` (or next to the .exe) and the app finds it
by itself, no setup screen and no restart needed:

```
config/
  settings.yaml          Johnny      ─┐ Jivetel login, Gmail inboxes, Apple IDs,
  settings_team2.yaml    Office 1     ├─ and that office's spreadsheet id
  settings_team3.yaml    Office 2    ─┘
  service_account.json   Google key shared by all three sheets
  settings.accounts.csv  that team's account history (optional, imported on load)
  settings.numbers.csv   phone pool          (optional)
  settings.pool.csv      iCloud aliases      (optional)
```

With more than one team file present, the app asks **"Which team is this computer for?"**
on first launch. Without `service_account.json` everything still works — the app simply
can't copy results into the Google Sheet, and it starts doing so within seconds of the file
appearing in `config/`.

Build the distributable:

```powershell
build.bat                              # -> dist\TM-Desktop.zip
```

### Project layout
```
main.py                 native window entry (pywebview)
app/
  api.py                JS<->Python bridge (methods the UI calls)
  orchestrator.py       background runner: block/backoff, concurrency
  signup/flow.py        the Ticketmaster signup steps (Chrome)
  browser.py            drives installed Chrome via Playwright
  storage.py            local SQLite queue + results
  identity.py, names.py names + zip/city/state from area code (offline)
  verification/         Gmail IMAP + manual code prompt bridge
  factory/factory.py    iCloud Hide My Email minter (icloud-hme API, 2FA via UI)
  credentials.py        finds config/settings*.yaml in the shipped folder and seeds config
  events.py, config.py, paths.py
web/                    HTML/CSS/JS UI (offline, no CDN)
build.spec, build.bat   PyInstaller packaging
```

### Settings (⚙ in the app)
- **Credentials file** — the `config/settings*.yaml` files found in the folder the app was
  shipped in. Loading one copies over the Gmail inboxes, Apple IDs and Jivetel login. This
  happens automatically the first time the app runs.
- **Email inboxes** — the Gmail accounts (address + app password) the IMAP poller watches
  for Ticketmaster codes. **Test all** logs into each one and reports which still work.
- **Concurrent Chrome windows** — how many accounts run at once (default 1).
- **Launch jitter** — random delay before each account starts (desyncs a shared IP).
- **Headless** — hide Chrome (not recommended; you can't help with challenges).
- **Retries after a fail** — how many retries a row gets before it's marked failed, and so
  how far down the wait ladder it travels (5 by default: 5m, 20m, 1h10m, 2h30m, 5h). The
  waits themselves live in the code rather than in the saved settings, so a new build changes
  them on every machine instead of only on fresh installs; the box spells out which ones
  apply as you change the number.
- **Jivetel** — portal login used to read SMS codes by phone number.
- **Google Sheet** — which office sheet finished work is copied into, when it last synced,
  plus **Sync now** and **Test connection**.

Apple IDs are entered on the **iCloud Emails** screen, not here. Every
secret (Jivetel, Gmail app passwords, Apple ID passwords) stays in
`%LOCALAPPDATA%\TMDesktop\config.json` on the operator's own machine.

### Handover checklist
1. `build.bat` → copy `dist\TM Desktop\` into the team's `ticketmaster-accounts` folder.
2. Make sure that folder still contains their `config/settings*.yaml` — those are kept out
   of the repo, so a fresh `git clone` will **not** have them.
3. Make sure `config/service_account.json` is in there too — without it nothing reaches the
   Google Sheet (the app still runs, it just can't copy anything back).
4. `config/<settings file stem>.accounts.csv` carries that team's accounts. The app keeps
   it current by itself; it only needs creating by hand the very first time.
5. Zip and send. On their machine: unzip, install Chrome if missing, double-click the exe.
