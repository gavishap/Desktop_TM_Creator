"use strict";

// The Python bridge (window.pywebview.api) isn't ready until pywebview fires its event.
let apiReady = false;
let lastVersion = -1;
let activePromptId = null;
const selected = new Set();
let lastAccountIds = [];
const poolSelected = new Set();
let currentView = "accounts";

const $ = (id) => document.getElementById(id);

window.addEventListener("pywebviewready", async () => {
  apiReady = true;
  await askForTeam();
  poll();
  setInterval(poll, 700);
});

// ── first run: pick the team whose logins this copy should use ───────────────
async function askForTeam() {
  const choices = await api("credential_choices");
  if (!choices || !choices.length) return;
  $("teamList").innerHTML = choices.map((c, i) => {
    const bits = [];
    if (c.jivetel) bits.push(`Jivetel <b>${esc(c.jivetel)}</b>`);
    if (c.apple_ids.length) bits.push(`Apple ID <b>${esc(c.apple_ids[0])}</b>` +
      (c.apple_ids.length > 1 ? ` +${c.apple_ids.length - 1} more` : ""));
    bits.push(`${c.inboxes.length} email inbox${c.inboxes.length === 1 ? "" : "es"}`);
    return `<div class="base-row"><span class="base-name">${bits.join(" · ")}
      <span class="base-tally">${esc(c.name)}</span></span>
      <button class="btn primary small" data-team="${i}">Use this</button></div>`;
  }).join("");
  $("teamOverlay").classList.remove("hidden");
  await new Promise((done) => {
    $("teamList").querySelectorAll("[data-team]").forEach((b) => {
      b.onclick = async () => {
        const r = await api("load_credential_file", choices[b.dataset.team].path);
        if (!r.ok) return window.alert(r.error);
        $("teamOverlay").classList.add("hidden");
        done();
      };
    });
  });
}

// ── run controls ────────────────────────────────────────────────────────────
$("btnStart").onclick = () => api("start_run");
$("btnStop").onclick = () => api("stop_run");
$("btnAddToggle").onclick = () => $("addBox").classList.toggle("hidden");
$("btnResetFailed").onclick = async () => {
  const res = await api("reset_failed");
  if (res && res.reset === 0) window.alert("Nothing to reset — no failed rows.");
  poll();
};
$("btnExport").onclick = async () => {
  const res = await api("export_csv");
  if (res && res.path) window.alert("Saved a spreadsheet (CSV) you can open in Excel:\n\n" + res.path);
};
$("btnRunSel").onclick = async () => {
  const ids = [...selected];
  if (!ids.length) return;
  const res = await api("run_accounts", ids);
  if (res && res.ok === false && res.error) window.alert(res.error);
  selected.clear();
  poll();
};
$("selAll").onclick = (e) => {
  if (e.target.checked) lastAccountIds.forEach((id) => selected.add(id));
  else selected.clear();
  poll();
};
window.toggleSel = (id, on) => {
  if (on) selected.add(id); else selected.delete(id);
  updateSelUI();
};
$("btnAdd").onclick = async () => {
  const text = $("addText").value;
  if (!text.trim()) return;
  await api("add_accounts", text);
  $("addText").value = "";
  $("addBox").classList.add("hidden");
  poll();
};

// ── screen switching ──────────────────────────────────────────────────────--
const VIEWS = { accounts: "viewAccounts", icloud: "viewIcloud", gmail: "viewGmail", numbers: "viewNumbers" };
document.querySelectorAll(".navbtn").forEach((b) => {
  b.onclick = () => showView(b.dataset.view);
});

function showView(name) {
  currentView = name;
  Object.entries(VIEWS).forEach(([key, id]) => $(id).classList.toggle("hidden", key !== name));
  document.querySelectorAll(".navbtn").forEach((b) => b.classList.toggle("active", b.dataset.view === name));
  poll();
}

// ── iCloud email factory ──────────────────────────────────────────────────--
$("fcSaveApple").onclick = async () => {
  const email = $("fcAppleId").value.trim();
  const pw = $("fcApplePw").value;
  const res = await api("save_icloud", email, pw);
  if (res && res.ok === false) return window.alert(res.error);
  $("fcApplePw").value = "";
  poll();
};
$("fcGenerate").onclick = async () => {
  const count = parseInt($("fcCount").value, 10) || 0;
  const res = await api("factory_generate", count, $("fcReset").checked, $("fcAppleUse").value);
  if (res && res.ok === false) window.alert(res.error);
  poll();
};
$("fcCancel").onclick = () => api("factory_cancel");
$("fcMove").onclick = async () => {
  const ids = [...poolSelected];
  if (!ids.length) return;
  await api("pool_to_accounts", ids);
  poolSelected.clear();
  poll();
};
$("poolSelAll").onclick = (e) => {
  poolSelected.clear();
  if (e.target.checked) (window.__pool || []).forEach((p) => { if (!p.used) poolSelected.add(p.id); });
  poll();
};
window.togglePool = (id, on) => { if (on) poolSelected.add(id); else poolSelected.delete(id); updateFcUI(); };
window.delPool = async (id) => { await api("delete_pool_email", id); poll(); };

function renderFactory(state) {
  const fc = state.factory || {};
  window.__pool = state.pool || [];
  const accts = state.icloud_accounts || [];
  $("icloudList").innerHTML = accts.length
    ? accts.map((a) => `<span class="chip">${esc(a.email)}<button class="chip-x" onclick="delIcloud('${esc(a.email)}')">✕</button></span>`).join("")
    : '<span class="hint">No Apple ID yet — add one below.</span>';

  const use = $("fcAppleUse");
  const keep = use.value;
  use.innerHTML = '<option value="">any Apple ID</option>' +
    accts.map((a) => `<option value="${esc(a.email)}">${esc(a.email)}</option>`).join("");
  if ([...use.options].some((o) => o.value === keep)) use.value = keep;

  const running = fc.running;
  $("fcGenerate").classList.toggle("hidden", running);
  $("fcCancel").classList.toggle("hidden", !running);
  const verified = window.__pool.filter((p) => p.used).length;
  const loose = window.__pool.filter((p) => !p.queued).length;
  $("fcUnused").textContent = window.__pool.length;
  $("fcProgress").innerHTML = running
    ? `<span class="spin">●</span> ${esc(fc.status || "working")} — <b>${fc.created}/${fc.target}</b> made`
    : `Pool: <b>${window.__pool.length}</b> made · ${verified} verified${loose ? ` · ${loose} not in Accounts yet` : ""}.${
        fc.status && fc.status !== "idle" ? " · " + esc(fc.status) : ""}`;

  const live = new Set(window.__pool.map((p) => p.id));
  [...poolSelected].forEach((id) => { if (!live.has(id)) poolSelected.delete(id); });
  $("poolEmpty").classList.toggle("hidden", window.__pool.length > 0);
  $("poolBody").innerHTML = window.__pool.map((p) => `
    <tr class="${poolSelected.has(p.id) ? "sel" : ""}">
      <td class="c"><input type="checkbox" ${p.queued ? "disabled" : ""} ${poolSelected.has(p.id) ? "checked" : ""} onclick="togglePool(${p.id}, this.checked)" /></td>
      <td class="email"><span class="cellcopy"><span class="val">${esc(p.email)}</span><button class="icon-btn copy" title="Copy" onclick="copyCell(this)">⧉</button></span></td>
      <td>${esc(p.created_from || "—")}</td>
      <td>${p.used ? `<span class="tick">✓ verified ${esc(p.used_on || "")}</span>`
        : p.queued ? '<span class="hint">in Accounts</span>' : '<span class="hint">not queued</span>'}</td>
      <td><button class="icon-btn" title="Delete" onclick="delPool(${p.id})">✕</button></td>
    </tr>`).join("");
  updateFcUI();
}

function updateFcUI() {
  const btn = $("fcMove");
  if (btn) btn.disabled = poolSelected.size === 0;
  if (btn) btn.textContent = poolSelected.size ? `Add ${poolSelected.size} to Accounts` : "Add selected to Accounts";
}
window.delIcloud = async (email) => { await api("delete_icloud", email); poll(); };

// ── gmail dot generator + numbers ─────────────────────────────────────────--
$("gmAddBase").onclick = async () => {
  const v = $("gmBase").value.trim();
  if (!v) return;
  await api("add_gmail_bases", v);
  $("gmBase").value = "";
  poll();
};
$("gmGenerate").onclick = async () => {
  const count = parseInt($("gmCount").value, 10) || 0;
  const res = await api("generate_gmail_emails", count, [...pickedBases]);
  $("gmMsg").textContent = (res && (res.message || res.error)) || "";
  poll();
};
$("numAdd").onclick = async () => {
  const v = $("numInput").value.trim();
  if (!v) return;
  await api("add_numbers", v);
  $("numInput").value = "";
  poll();
};
$("numAssign").onclick = async () => { await api("assign_numbers"); poll(); };
window.delBase = async (b) => { pickedBases.delete(b); await api("delete_gmail_base", b); poll(); };
window.delNumber = async (id) => { await api("delete_number", id); poll(); };

// Which bases the next batch draws from. Bases accumulate from past accounts, so the
// operator ticks the handful they actually want rather than spraying across all of them.
const pickedBases = new Set(JSON.parse(localStorage.getItem("pickedBases") || "[]"));
let basesSeen = [];

function savePicked() {
  localStorage.setItem("pickedBases", JSON.stringify([...pickedBases]));
  renderGmail({ gmail_bases: basesSeen });
}
window.pickBase = (b, on) => { on ? pickedBases.add(b) : pickedBases.delete(b); savePicked(); };
$("gmAll").onclick = () => { basesSeen.forEach((b) => pickedBases.add(b.base)); savePicked(); };
$("gmNone").onclick = () => { pickedBases.clear(); savePicked(); };

function renderGmail(state) {
  const bases = state.gmail_bases || [];
  basesSeen = bases;
  // first run: nothing chosen yet, so behave like before and offer them all
  if (!localStorage.getItem("pickedBases") && bases.length) {
    bases.forEach((b) => pickedBases.add(b.base));
    localStorage.setItem("pickedBases", JSON.stringify([...pickedBases]));
  }
  const picked = bases.filter((b) => pickedBases.has(b.base));
  const free = picked.reduce((s, b) => s + b.remaining, 0);
  $("basesList").innerHTML = bases.length
    ? bases.map((b) => `<label class="base-row ${b.remaining ? "" : "exhausted"}">
        <input type="checkbox" ${pickedBases.has(b.base) ? "checked" : ""}
          onchange="pickBase('${esc(b.base)}', this.checked)" />
        <span class="base-name">${esc(b.base)}</span>
        <span class="base-tally">${b.used}/${b.total} used · ${b.remaining
          ? `<b>${b.remaining}</b> free`
          : '<b class="warn-text">exhausted</b>'}</span>
        <button class="chip-x" title="Remove base" onclick="delBase('${esc(b.base)}')">✕</button></label>`).join("")
    : '<span class="hint">No bases yet. Add one above, or they appear automatically from your accounts.</span>';
  $("gmSummary").innerHTML = bases.length
    ? `— ${picked.length} of ${bases.length} ticked, <b>${free}</b> fresh variation(s) available`
    : "";
}

function renderNumbers(state) {
  const numbers = state.numbers || [];
  const byId = {};
  (state.accounts || []).forEach((a) => { byId[a.id] = a.email; });
  $("numUnused").textContent = state.unused_numbers || 0;
  $("numEmpty").classList.toggle("hidden", numbers.length > 0);
  $("numBody").innerHTML = numbers.map((n) => `
    <tr>
      <td>${esc(n.phone)}</td>
      <td class="c">${n.used ? '<span class="tick">✓</span>' : '<span class="cross">·</span>'}</td>
      <td class="email">${esc(byId[n.account_id] || (n.used ? "—" : ""))}</td>
      <td>${n.used ? "" : `<button class="icon-btn" title="Delete" onclick="delNumber(${n.id})">✕</button>`}</td>
    </tr>`).join("");
}

// ── settings ────────────────────────────────────────────────────────────────
$("btnSettings").onclick = async () => {
  const cfg = await api("get_config");
  $("setConcurrent").value = cfg.max_concurrent;
  $("setJitter").value = cfg.launch_jitter;
  $("setHeadless").checked = cfg.browser.headless;
  $("setRetries").value = cfg.block.max_retries;
  waitLadder = cfg.block.cooldowns || [];
  showRetriesHint();
  $("setJvUser").value = cfg.jivetel.username || "";
  $("setJvPass").value = cfg.jivetel.password || "";
  await renderCredFiles();
  await renderInboxes();
  await renderSheet();
  $("settingsOverlay").classList.remove("hidden");
};

// The waits themselves aren't editable; the number of retries decides how far down them a
// row travels, so spell the chosen ones out rather than leaving the operator guessing.
let waitLadder = [];
function showRetriesHint() {
  const n = parseInt($("setRetries").value, 10) || 0;
  const spell = (s) => s < 3600 ? `${Math.round(s / 60)}m`
    : (s % 3600 ? `${Math.floor(s / 3600)}h ${Math.round((s % 3600) / 60)}m` : `${s / 3600}h`);
  $("setRetriesHint").textContent = !n || !waitLadder.length ? "One try only, no waiting."
    : "Waits " + Array.from({ length: n }, (_, i) =>
        spell(waitLadder[Math.min(i, waitLadder.length - 1)])).join(", then ") + ".";
}
$("setRetries").oninput = showRetriesHint;

async function renderSheet() {
  const s = await api("sheet_status");
  $("setSheetState").innerHTML = !s.enabled
    ? "Not linked — load your team's credentials file above."
    : `Linked to sheet <code>${esc(s.spreadsheet_id.slice(0, 12))}…</code>`
      + (s.has_key ? "" : ' <b class="warn-text">service_account.json is missing from the project folder</b>')
      + (s.last_run ? ` · last synced ${new Date(s.last_run * 1000).toLocaleTimeString()}` : " · not synced yet")
      + (s.error ? ` · <b class="warn-text">${esc(s.error)}</b>` : "");
  $("setSheetSync").disabled = !s.enabled;
  $("setSheetTest").disabled = !s.enabled;
}
$("setSheetSync").onclick = async () => {
  $("setSheetMsg").textContent = "Syncing…";
  const r = (await api("sync_sheet")) || {};
  $("setSheetMsg").textContent = r.message || r.error || "Nothing new to send.";
  renderSheet();
};
$("setSheetTest").onclick = async () => {
  $("setSheetMsg").textContent = "Checking…";
  const r = (await api("test_sheet")) || {};
  $("setSheetMsg").textContent = r.ok ? `Connected: ${r.message}`
    : (r.error || "Couldn't reach the sheet.");
};

async function renderCredFiles() {
  const files = await api("list_credential_files");
  const sel = $("setCredFile");
  sel.innerHTML = files.length
    ? files.map((f) => {
        const who = f.jivetel || (f.apple_ids || [])[0] || `${(f.inboxes || []).length} inboxes`;
        return `<option value="${esc(f.path)}">${esc(f.name)} — ${esc(who)}</option>`;
      }).join("")
    : `<option value="">No credentials file found next to the app</option>`;
  $("setCredLoad").disabled = !files.length;
}
$("setCredLoad").onclick = async () => {
  const path = $("setCredFile").value;
  if (!path) return;
  const r = await api("load_credential_file", path);
  if (!r.ok) return alert(r.error);
  const cfg = await api("get_config");
  $("setJvUser").value = cfg.jivetel.username || "";
  $("setJvPass").value = cfg.jivetel.password || "";
  await renderInboxes();
  await renderSheet();
};

async function renderInboxes() {
  const list = await api("list_inboxes");
  $("setInboxList").innerHTML = list.length
    ? list.map((i) => `<div class="base-row"><span class="base-name">${esc(i.username)}</span>
        <button class="btn ghost small" data-inbox="${esc(i.username)}">Remove</button></div>`).join("")
    : `<div class="hint">No inboxes yet — codes will have to be typed in by hand.</div>`;
  $("setInboxList").querySelectorAll("[data-inbox]").forEach((b) => {
    b.onclick = async () => { await api("delete_inbox", b.dataset.inbox); renderInboxes(); };
  });
}
$("setInboxAdd").onclick = async () => {
  const r = await api("save_inbox", $("setInboxUser").value, $("setInboxPass").value);
  if (!r.ok) return alert(r.error);
  $("setInboxUser").value = ""; $("setInboxPass").value = "";
  renderInboxes();
};
$("setInboxTest").onclick = async () => {
  const btn = $("setInboxTest"); btn.disabled = true; btn.textContent = "Testing…";
  const r = await api("test_inboxes");
  btn.disabled = false; btn.textContent = "Test all";
  alert(r.results.map((x) => `${x.ok ? "OK" : "FAILED"}  ${x.username}${x.ok ? "" : " — " + x.error}`)
    .join("\n") || "No inboxes configured.");
};
$("setCancel").onclick = () => $("settingsOverlay").classList.add("hidden");
$("setSave").onclick = async () => {
  await api("save_config", {
    max_concurrent: parseInt($("setConcurrent").value, 10) || 1,
    launch_jitter: parseFloat($("setJitter").value) || 0,
    browser: { headless: $("setHeadless").checked },
    block: { max_retries: parseInt($("setRetries").value, 10) || 0 },
    jivetel: { username: $("setJvUser").value, password: $("setJvPass").value },
  });
  $("settingsOverlay").classList.add("hidden");
};

// ── code prompt ───────────────────────────────────────────────────────────--
$("promptSubmit").onclick = submitPrompt;
$("promptCancel").onclick = async () => {
  if (activePromptId) await api("cancel_prompt", activePromptId);
  hidePrompt();
};
$("promptInput").addEventListener("keydown", (e) => { if (e.key === "Enter") submitPrompt(); });

async function submitPrompt() {
  const code = $("promptInput").value.trim();
  if (!code || !activePromptId) return;
  await api("submit_code", activePromptId, code);
  hidePrompt();
}
function hidePrompt() {
  $("promptOverlay").classList.add("hidden");
  $("promptInput").value = "";
  activePromptId = null;
}

// ── polling + render ──────────────────────────────────────────────────────--
async function poll() {
  if (!apiReady) return;
  const state = await api("get_state");
  if (!state) return;
  render(state);
}

function render(state) {
  // run flag
  const running = state.running;
  window.__running = running;
  $("btnStart").disabled = running;
  $("btnStop").disabled = !running;
  $("runDot").className = "dot " + (running ? "running" : "idle");

  // counts
  const c = state.counts || {};
  $("counts").innerHTML = [
    ["total", "Total"], ["verified", "Verified"], ["failed", "Failed"],
    ["in_progress", "Running"], ["pending", "Pending"],
  ].filter(([k]) => c[k]).map(([k, label]) =>
    `<span class="pill">${label} <b>${c[k]}</b></span>`).join("");

  // prompts (show the first open one)
  const prompt = (state.prompts || [])[0];
  if (prompt && prompt.prompt_id !== activePromptId) {
    activePromptId = prompt.prompt_id;
    const titles = { sms: "Enter SMS code", email: "Enter email code", "2fa": "Enter Apple ID code" };
    $("promptTitle").textContent = titles[prompt.kind] || "Enter verification code";
    $("promptLabel").textContent = prompt.label;
    $("promptOverlay").classList.remove("hidden");
    $("promptInput").focus();
  } else if (!prompt && activePromptId && !$("promptOverlay").classList.contains("hidden")) {
    hidePrompt();
  }

  renderLogs(state.logs || []);
  if (currentView === "accounts") renderAccounts(state.accounts || [], state.now || 0);
  else if (currentView === "icloud") renderFactory(state);
  else if (currentView === "gmail") renderGmail(state);
  else if (currentView === "numbers") renderNumbers(state);
}

function renderAccounts(accounts, now) {
  lastAccountIds = accounts.map((a) => a.id);
  const live = new Set(lastAccountIds);
  [...selected].forEach((id) => { if (!live.has(id)) selected.delete(id); });
  $("emptyState").classList.toggle("hidden", accounts.length > 0);
  const body = $("acctBody");
  body.innerHTML = accounts.map((a, i) => {
    const place = [a.city, a.state].filter(Boolean).join(" ");
    const addr = [a.address, place].filter(Boolean).join(", ");
    const wait = a.retry_after > now ? Math.ceil((a.retry_after - now) / 60) : 0;
    const waiting = wait > 90 ? `${Math.floor(wait / 60)}h ${wait % 60}m` : wait ? `${wait}m` : "";
    return `
    <tr class="${selected.has(a.id) ? "sel" : ""}">
      <td class="c"><input type="checkbox" class="rowsel" ${selected.has(a.id) ? "checked" : ""} onclick="toggleSel(${a.id}, this.checked)" /></td>
      <td>${i + 1}</td>
      <td class="email" title="${esc(a.email)}${a.created_from ? " · from " + esc(a.created_from) : ""}">
        <span class="cellcopy"><span class="val">${esc(a.email)}</span>${a.has_imap ? " 🔑" : ""}<button class="icon-btn copy" title="Copy email" onclick="copyCell(this)">⧉</button></span>
      </td>
      <td>${esc(a.phone || "—")}</td>
      <td class="name">${esc(`${a.first_name || ""} ${a.last_name || ""}`.trim() || "—")}</td>
      <td class="addr" title="${esc(addr)}">${esc(addr || "—")}</td>
      <td>${esc(a.zip_code || "—")}</td>
      <td class="pw">${a.tm_password
        ? `<span class="cellcopy"><code>${esc(a.tm_password)}</code><button class="icon-btn copy" title="Copy password" onclick="copyCell(this)">⧉</button></span>`
        : '<span class="cross">·</span>'}</td>
      <td><span class="badge ${a.status}">${a.status.replace(/_/g, " ")}</span>${
        waiting ? `<span class="wait" title="Waiting before its next try — it comes back by itself, leave the app open">⏳ ${waiting}</span>` : ""}</td>
      <td class="c">${a.email_verified ? '<span class="tick">✓</span>' : '<span class="cross">·</span>'}</td>
      <td class="c">${a.phone_verified ? '<span class="tick">✓</span>' : '<span class="cross">·</span>'}</td>
      <td class="date">${esc(shortDate(a.verified_at || a.created_at))}</td>
      <td class="note" title="${esc(a.error || "")}">${esc(a.error || "")}</td>
      <td>
        <div class="row-actions">
          ${a.status === "verified" ? "" :
            `<button class="icon-btn run" title="Run this account now" onclick="runAcct(${a.id})" ${window.__running ? "disabled" : ""}>▶</button>`}
          <button class="icon-btn" title="Reset to pending" onclick="resetAcct(${a.id})">↺</button>
          <button class="icon-btn" title="Delete" onclick="delAcct(${a.id})">✕</button>
        </div>
      </td>
    </tr>`;
  }).join("");
  updateSelUI();
}

function shortDate(ts) {
  if (!ts) return "—";
  const d = new Date(ts * 1000);
  return `${d.getMonth() + 1}/${d.getDate()} ${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
}

function updateSelUI() {
  const btn = $("btnRunSel");
  if (btn) btn.disabled = selected.size === 0 || !!window.__running;
  const all = $("selAll");
  if (all) all.checked = lastAccountIds.length > 0 && selected.size === lastAccountIds.length;
  const label = $("btnRunSel");
  if (label) label.textContent = selected.size ? `Run selected (${selected.size})` : "Run selected";
}

// ── activity log: foldable, with a one-line status bar that's always on ─────
let lastLogCount = -1;

function showLogs(on) {
  document.body.classList.toggle("logs-off", !on);
  localStorage.setItem("logsOpen", on ? "1" : "0");
  if (on) lastLogCount = -1;   // force a repaint of the box we just revealed
  poll();
}
showLogs(localStorage.getItem("logsOpen") === "1");
$("btnLogs").onclick = () => showLogs(document.body.classList.contains("logs-off"));
$("logsClose").onclick = () => showLogs(false);
$("statusBar").onclick = () => showLogs(true);

function renderLogs(logs) {
  const latest = logs[logs.length - 1];
  const msg = $("statusMsg");
  msg.textContent = latest ? `${latest.t}  ${latest.message}` : "Ready.";
  msg.className = `status-msg ${latest ? latest.level : ""}`;
  if (logs.length === lastLogCount || document.body.classList.contains("logs-off")) return;
  lastLogCount = logs.length;
  const box = $("logBox");
  box.innerHTML = logs.map((l) =>
    `<div class="log-line ${l.level}"><span class="ts">${l.t}</span><span class="msg">${esc(l.message)}</span></div>`
  ).join("");
  box.scrollTop = box.scrollHeight;
}

window.resetAcct = async (id) => { await api("reset_account", id); poll(); };
window.delAcct = async (id) => { await api("delete_account", id); poll(); };
window.runAcct = async (id) => {
  const res = await api("run_account", id);
  if (res && res.ok === false && res.error) window.alert(res.error);
  poll();
};
window.copyCell = (btn) => {
  const host = btn.parentElement;
  const el = host.querySelector(".val") || host.querySelector("code");
  if (!el) return;
  copyText(el.textContent.trim());
  const prev = btn.textContent;
  btn.textContent = "✓";
  setTimeout(() => { btn.textContent = prev; }, 1000);
};

function copyText(text) {
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text);
      return;
    }
  } catch (e) { /* fall through to legacy copy */ }
  const ta = document.createElement("textarea");
  ta.value = text;
  document.body.appendChild(ta);
  ta.select();
  try { document.execCommand("copy"); } catch (e) { /* ignore */ }
  document.body.removeChild(ta);
}

// ── helpers ────────────────────────────────────────────────────────────────-
async function api(method, ...args) {
  try {
    return await window.pywebview.api[method](...args);
  } catch (e) {
    console.error(method, e);
    return null;
  }
}
function esc(s) {
  return String(s == null ? "" : s)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}
