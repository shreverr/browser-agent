// Annotation viewer: Trial list, Trace view, my note + others' notes, taxonomy.
// Live-updates from /api/events (SSE). No framework, no build step.
import { esc, usd, renderTrace, loadLazy } from "./trace.js";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const enc = encodeURIComponent;
const VERDICTS = [["fail", "fail"], ["pass_but_bad", "pass, but bad"], ["ok", "ok"]];
const GRADABILITY = ["unknown", "code", "judge"];

const S = {
  me: null,
  trials: [],
  modes: [],
  filters: { coded: "uncoded", status: "", cell: "", author: "", mode: "" },
  current: null, // trial_id on screen
  trial: null, // {header, events, n_events}
  notes: [], // every note on the current Trial, with assignments
  draft: null, // my note being edited: {verdict, first_bad_seq, text}
  draftTrial: null,
  dirty: false,
  saving: null, // promise of the in-flight save
  saveAgain: false,
  saveTimer: null,
  sideTab: "note",
  mobileTab: "trials",
  renaming: null, // mode id with an open rename box
  pendingTax: false,
  drill: new Map(), // mode id -> notes in it
};

// ---------- helpers ----------
function fmtDetail(d) {
  if (d == null) return "error";
  if (typeof d === "string") return d;
  if (Array.isArray(d)) return d.join("; ");
  if (d.errors) return `${d.error}: ${d.errors.map((e) => e.error).join("; ")}`;
  return d.error ? `${d.error}${d.missing ? ` (${d.missing.length})` : ""}` : JSON.stringify(d);
}

async function api(method, path, body) {
  const opt = { method, credentials: "same-origin", headers: { "X-Requested-With": "annotate" } };
  if (body !== undefined) { opt.headers["Content-Type"] = "application/json"; opt.body = JSON.stringify(body); }
  const r = await fetch(path, opt);
  if (r.status === 401) { showGate("signin"); throw new Error("signed out"); }
  const ct = r.headers.get("content-type") || "";
  const data = ct.includes("json") ? await r.json() : await r.text();
  if (!r.ok) throw new Error(typeof data === "object" ? fmtDetail(data.detail) : String(data).slice(0, 200));
  return data;
}

let toastTimer;
function toast(msg) {
  const el = $("#toast");
  el.textContent = msg;
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, 2800);
}
const fail = (e) => toast(String(e.message || e));

function timeAgo(iso) {
  if (!iso) return "";
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 60) return "just now";
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  return new Date(iso).toLocaleDateString();
}
const activeModes = () => S.modes.filter((m) => !m.merged_into);
const myNote = () => S.notes.find((n) => n.author === S.me) || null;
const trialSuffix = (id) => { const p = id.split("."); return p.length > 2 ? "." + p.slice(-2).join(".") : ""; };
const trialTask = (id) => { const p = id.split("."); return p.length > 2 ? p.slice(0, -2).join(".") : id; };

// ---------- theme (per-viewer convenience only) ----------
const THEMES = ["auto", "light", "dark"];
let theme = "auto";
try { theme = localStorage.getItem("annotate-theme") || "auto"; } catch { /* storage blocked */ }
function applyTheme() {
  if (theme === "auto") delete document.documentElement.dataset.theme;
  else document.documentElement.dataset.theme = theme;
  $("#theme").textContent = "theme: " + theme;
  try { localStorage.setItem("annotate-theme", theme); } catch { /* ignore */ }
}

// ---------- gate ----------
function showGate(kind) {
  $("#app").hidden = true;
  $(".tabs-mobile").hidden = true;
  const g = $("#gate");
  g.hidden = false;
  if (kind === "denied") {
    g.innerHTML = `<h1>Not on the allowlist</h1><p>Your GitHub login can't use this server. Ask the owner to add you to <code>ANNOTATE_ALLOWED_GITHUB</code>.</p>`;
  } else if (kind === "error") {
    g.innerHTML = `<h1>Can't reach the server</h1><p>Try again in a moment. (Free Render instances take a minute to wake up.)</p><a class="btn" href="/">Retry</a>`;
  } else {
    g.innerHTML = `<h1>Error analysis</h1><p>Traces here are unredacted pages from the live web. Sign in with an allowlisted GitHub account.</p><a class="btn" href="/auth/login">Sign in with GitHub</a>`;
  }
}

// ---------- panels (mobile tabs + side tabs) ----------
function setMobileTab(tab) {
  S.mobileTab = tab;
  if (tab === "note" || tab === "taxonomy") S.sideTab = tab;
  $("#list-col").classList.toggle("active", tab === "trials");
  $("#trace-col").classList.toggle("active", tab === "trace");
  $("#side-col").classList.toggle("active", tab === "note" || tab === "taxonomy");
  $$("[data-mtab]").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.mtab === tab)));
  setSideTab(S.sideTab);
}
function setSideTab(tab) {
  S.sideTab = tab;
  $("#note-pane").hidden = tab !== "note";
  $("#tax-pane").hidden = tab !== "taxonomy";
  $$("[data-stab]").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.stab === tab)));
}
const isNarrow = () => window.matchMedia("(max-width: 1000px)").matches;

// ---------- data ----------
async function loadTrials() { S.trials = (await api("GET", "/api/trials")).trials; }
async function loadModes() { S.modes = (await api("GET", "/api/modes")).modes; }
async function loadNotes() {
  if (!S.current) return;
  const id = S.current;
  const notes = (await api("GET", `/api/trials/${enc(id)}/notes`)).notes;
  if (id !== S.current) return;
  S.notes = notes;
  if (!S.dirty && !S.saving) S.draft = draftFrom(myNote());
}
function draftFrom(n) { return { verdict: n?.verdict ?? null, first_bad_seq: n?.first_bad_seq ?? null, text: n?.text ?? "" }; }

// ---------- Trial list ----------
function filtered() {
  const f = S.filters;
  return S.trials.filter((t) => {
    if (t.trial_id === S.current) return true; // never yank the open Trial out of the list
    if (f.coded === "uncoded" && t.coded) return false;
    if (f.coded === "coded" && !t.coded) return false;
    if (f.coded === "unnoted" && t.my_note) return false;
    if (f.status && t.status !== f.status && t.terminal_reason !== f.status) return false;
    if (f.cell && !t.cell.includes(f.cell)) return false;
    if (f.author && !(t.authors || []).includes(f.author)) return false;
    if (f.mode && !t.modes.some((m) => m.id === f.mode && m.accepted > 0)) return false;
    return true;
  });
}

function options(values, current, label) {
  const opts = [...new Set(values)].filter(Boolean).sort();
  return `<option value="">${esc(label)}</option>` + opts.map((v) => `<option value="${esc(v)}"${v === current ? " selected" : ""}>${esc(v)}</option>`).join("");
}

function renderFilters() {
  const f = S.filters;
  const coded = [["uncoded", "uncoded"], ["unnoted", "not noted by me"], ["coded", "coded"], ["", "all Trials"]];
  $("#filters").innerHTML = `
    <select id="f-coded" data-filter="coded" aria-label="coding">${coded.map(([v, l]) => `<option value="${v}"${f.coded === v ? " selected" : ""}>${l}</option>`).join("")}</select>
    <select data-filter="status" aria-label="status">${options(S.trials.flatMap((t) => [t.status, t.terminal_reason]), f.status, "any status")}</select>
    <select data-filter="cell" aria-label="cell">${options(S.trials.flatMap((t) => t.cell), f.cell, "any cell")}</select>
    <select data-filter="author" aria-label="note author">${options(S.trials.flatMap((t) => t.authors || []), f.author, "any author")}</select>
    <select data-filter="mode" aria-label="mode"><option value="">any mode</option>${activeModes().map((m) => `<option value="${esc(m.id)}"${m.id === f.mode ? " selected" : ""}>${esc(m.name)}</option>`).join("")}</select>
    <span class="count muted small"></span>`;
}

function statusChip(t) {
  const label = t.terminal_reason ? `${t.status} / ${t.terminal_reason}` : t.status;
  const cls = t.status === "completed" ? (t.terminal_reason === "done" ? "good" : "warn") : t.status === "infra_error" ? "infra" : "bad";
  return `<span class="chip ${cls}">${esc(label)}${t.infra_class ? " · " + esc(t.infra_class) : ""}</span>`;
}

function renderList() {
  const rows = filtered();
  const uncoded = S.trials.filter((t) => !t.coded).length;
  const mine = S.trials.filter((t) => t.my_note).length;
  $("#summary").textContent = `${S.trials.length} Trials · ${uncoded} uncoded · ${mine} noted by you`;
  const count = $("#filters .count");
  if (count) count.textContent = `${rows.length} shown`;
  $("#trial-list").innerHTML = rows.length ? rows.map((t) => {
    const my = t.my_note ? `<span class="chip good" title="your note">you: ${esc(t.my_note.verdict || "noted")}${t.my_note.first_bad_seq != null ? " #" + esc(t.my_note.first_bad_seq) : ""}</span>` : '<span class="chip">no note from you</span>';
    const others = t.other_note_count ? `<span class="chip">+${t.other_note_count} other${t.other_note_count > 1 ? "s" : ""}</span>` : "";
    const modes = t.modes.map((m) => `<span class="chip mode${m.accepted ? "" : " proposed"}">${esc(m.name)}${m.accepted > 1 ? " ×" + m.accepted : ""}</span>`).join("");
    return `<button type="button" role="listitem" class="t-row${t.trial_id === S.current ? " sel" : ""}" data-action="open-trial" data-id="${esc(t.trial_id)}" aria-current="${t.trial_id === S.current}">
      <div class="t-top"><span class="t-id">${esc(trialTask(t.trial_id))}<span class="muted">${esc(trialSuffix(t.trial_id))}</span></span> ${statusChip(t)}${t.unscripted_fallback ? ' <span class="chip bad">?unscripted</span>' : ""}</div>
      <div class="t-meta small">${t.cell.map((c) => `<span class="chip">${esc(c.split(":").slice(1).join(":"))}</span>`).join("")} <span class="muted">${esc(t.steps ?? "?")} steps · ${usd(t.cost_usd)}</span></div>
      ${t.answer ? `<div class="t-ans small">“${esc(t.answer)}”</div>` : ""}
      <div class="t-notes small">${my}${others}${modes}</div>
    </button>`;
  }).join("") : '<p class="muted pad">No Trials match. Try "all Trials".</p>';
}

// ---------- Trace ----------
async function openTrial(id, seq) {
  if (S.dirty) await saveNote();
  S.current = id;
  S.trial = null;
  S.notes = [];
  S.draft = draftFrom(null);
  S.draftTrial = id;
  S.dirty = false;
  try { history.replaceState(null, "", `#trial=${enc(id)}`); } catch { /* ignore */ }
  renderList();
  $("#trace").innerHTML = '<p class="muted pad">Loading Trace…</p>';
  renderNote();
  if (isNarrow()) setMobileTab("trace");
  try {
    const [trial] = await Promise.all([api("GET", `/api/trials/${enc(id)}`), loadNotes()]);
    if (id !== S.current) return;
    S.trial = trial;
    $("#trace").innerHTML = renderTrace(trial);
    $("#trace-col").scrollTop = 0;
    renderNote();
    updateMarkers();
    if (seq != null) jumpTo(seq);
  } catch (e) {
    $("#trace").innerHTML = `<p class="bad-t pad">${esc(e.message)}</p>`;
  }
}

function updateMarkers() {
  $$("#trace .a-ev.bad-mine").forEach((el) => el.classList.remove("bad-mine"));
  $$("#trace .marks").forEach((el) => { el.innerHTML = ""; });
  const mark = (seq, html, mine) => {
    const el = document.getElementById(`ev-${seq}`);
    if (!el) return;
    if (mine) el.classList.add("bad-mine");
    el.querySelector(".marks").insertAdjacentHTML("beforeend", html);
  };
  if (S.draft?.first_bad_seq != null) mark(S.draft.first_bad_seq, ' <span class="chip bad">first bad · you</span>', true);
  for (const n of S.notes) {
    if (n.author !== S.me && n.first_bad_seq != null) mark(n.first_bad_seq, ` <span class="chip warn">first bad · ${esc(n.author)}</span>`, false);
  }
}

function jumpTo(seq) {
  if (isNarrow()) setMobileTab("trace");
  const el = document.getElementById(`ev-${seq}`);
  if (!el) return;
  el.scrollIntoView({ block: "center", behavior: "smooth" });
  el.classList.add("flash");
  setTimeout(() => el.classList.remove("flash"), 1400);
}

// ---------- my note ----------
function setSaveState(text) { const el = $("#save-state"); if (el) el.textContent = text; }

function onDraftChange() {
  S.dirty = true;
  setSaveState("editing…");
  clearTimeout(S.saveTimer);
  S.saveTimer = setTimeout(() => saveNote(), 700);
}

async function saveNote() {
  clearTimeout(S.saveTimer);
  if (S.saving) { S.saveAgain = true; return S.saving; }
  if (!S.dirty || !S.draftTrial) return;
  const trialId = S.draftTrial;
  const draft = { ...S.draft };
  setSaveState("saving…");
  S.saving = (async () => {
    try {
      const saved = await api("PUT", `/api/trials/${enc(trialId)}/note`, draft);
      if (trialId === S.current) {
        const i = S.notes.findIndex((n) => n.author === S.me);
        const merged = { ...saved, assignments: i >= 0 ? S.notes[i].assignments : [] };
        if (i >= 0) S.notes[i] = merged; else S.notes.push(merged);
        if (JSON.stringify(draft) === JSON.stringify(S.draft)) S.dirty = false;
        setSaveState(S.dirty ? "editing…" : "saved " + new Date().toLocaleTimeString());
      }
      return saved;
    } catch (e) {
      setSaveState("not saved: " + e.message);
      throw e;
    } finally {
      S.saving = null;
      if (S.saveAgain) { S.saveAgain = false; if (S.dirty) saveNote(); }
    }
  })();
  return S.saving;
}

async function ensureMyNote() {
  const mine = myNote();
  if (mine) return mine.id;
  S.dirty = true;
  const saved = await saveNote();
  return (saved || myNote())?.id;
}

function chip(a, { canDecide, canRemove }) {
  const who = a.proposed_by === "claude" ? "Claude" : a.proposed_by.replace(/^human:/, "");
  const title = `${a.state} · proposed by ${who}${a.decided_by ? " · decided by " + a.decided_by : ""}${a.rationale ? " · " + a.rationale : ""}`;
  const decide = a.state !== "accepted" && canDecide
    ? `<button type="button" data-action="accept" data-asg="${a.id}" aria-label="accept ${esc(a.mode_name)}" title="accept">✓</button>${a.state === "proposed" ? `<button type="button" data-action="reject" data-asg="${a.id}" aria-label="reject ${esc(a.mode_name)}" title="reject">✗</button>` : ""}`
    : "";
  const remove = canRemove ? `<button type="button" data-action="remove" data-asg="${a.id}" aria-label="remove ${esc(a.mode_name)}" title="remove">×</button>` : "";
  return `<span class="chip mode ${esc(a.state)}" title="${esc(title)}">${esc(a.mode_name)}${a.proposed_by === "claude" && a.state === "proposed" ? " · Claude" : ""}${decide}${remove}</span>`;
}

function modeSelect(id, exclude, label) {
  const ms = activeModes().filter((m) => !exclude.has(m.id));
  return `<select id="${id}" aria-label="${esc(label)}"><option value="">${esc(label)}</option>${ms.map((m) => `<option value="${esc(m.id)}">${esc(m.name)}</option>`).join("")}</select>`;
}

function renderNote() {
  const pane = $("#note-pane");
  if (!S.current) { pane.innerHTML = '<p class="muted">Pick a Trial to write your note.</p>'; return; }
  if (!$("#my-form") || pane.dataset.trial !== S.current) {
    pane.dataset.trial = S.current;
    pane.innerHTML = `
      <h3>My note <span class="muted small">(${esc(S.me)})</span></h3>
      <form id="my-form" autocomplete="off">
        <div class="verdicts" role="radiogroup" aria-label="verdict">
          ${VERDICTS.map(([v, l]) => `<label><input type="radio" name="verdict" value="${v}"> ${l}</label>`).join("")}
        </div>
        <div class="row">
          <label for="fbs">first bad step #</label>
          <input id="fbs" type="number" min="0" inputmode="numeric" size="5">
          <button type="button" data-action="jump-mine">jump</button>
          <button type="button" data-action="clear-seq">clear</button>
        </div>
        <label for="note-text" class="muted small">What went wrong first, in your own words (open coding)</label>
        <textarea id="note-text" rows="5" placeholder="e.g. applied the 'CCS' filter but the page kept showing all plugs; it never checked the result"></textarea>
        <div id="save-state" class="save-state" aria-live="polite"></div>
      </form>
      <div id="my-chips"></div>
      <h3>Others' notes</h3>
      <div id="others"></div>`;
    fillMyForm();
  } else if (!S.dirty && !pane.contains(document.activeElement)) {
    fillMyForm();
  }
  renderMyChips();
  renderOthers();
}

function fillMyForm() {
  const d = S.draft || draftFrom(null);
  $$('#my-form input[name="verdict"]').forEach((r) => { r.checked = r.value === d.verdict; });
  const fbs = $("#fbs");
  fbs.value = d.first_bad_seq ?? "";
  if (S.trial) fbs.max = String(S.trial.n_events - 1);
  $("#note-text").value = d.text;
}

function renderMyChips() {
  const el = $("#my-chips");
  if (!el) return;
  const mine = myNote();
  const asg = mine?.assignments || [];
  const taken = new Set(asg.filter((a) => a.state !== "rejected").map((a) => a.mode_id));
  el.innerHTML = `
    <div class="chips" aria-label="modes on my note">${asg.length ? asg.map((a) => chip(a, { canDecide: true, canRemove: true })).join("") : '<span class="muted small">Not in any mode yet.</span>'}</div>
    <div class="row">${modeSelect("assign-mode", taken, "assign my note to a mode…")}<button type="button" data-action="assign">assign</button></div>
    <details class="new-mode"><summary>new mode from this note</summary>
      <div class="row"><input id="nm-name" placeholder="mode name" aria-label="new mode name" maxlength="200"></div>
      <div class="row"><label>gradability <select id="nm-grad">${GRADABILITY.map((g) => `<option>${g}</option>`).join("")}</select></label></div>
      <textarea id="nm-def" rows="2" placeholder="definition: what counts, what doesn't" aria-label="new mode definition"></textarea>
      <div class="row"><button type="button" data-action="create-assign">create and assign</button></div>
    </details>`;
}

function renderOthers() {
  const el = $("#others");
  if (!el) return;
  const others = S.notes.filter((n) => n.author !== S.me && (n.text || n.verdict || n.first_bad_seq != null));
  el.innerHTML = others.length ? others.map((n) => {
    const taken = new Set(n.assignments.filter((a) => a.state !== "rejected").map((a) => a.mode_id));
    return `<div class="note-card">
      <div><span class="who">${esc(n.author)}</span> ${n.verdict ? `<span class="chip ${n.verdict === "ok" ? "good" : n.verdict === "fail" ? "bad" : "warn"}">${esc(n.verdict)}</span>` : ""}
        ${n.first_bad_seq != null ? `<button type="button" class="link" data-action="jump" data-seq="${esc(n.first_bad_seq)}">first bad #${esc(n.first_bad_seq)}</button>` : ""}
        <span class="muted small">${esc(timeAgo(n.updated_at))}</span></div>
      ${n.text ? `<div class="text">${esc(n.text)}</div>` : ""}
      <div class="chips">${n.assignments.map((a) => chip(a, { canDecide: a.proposed_by === "claude", canRemove: a.proposed_by === "human:" + S.me })).join("")}</div>
      <div class="row small">${modeSelect("suggest-" + n.id, taken, "suggest a mode…")}<button type="button" data-action="suggest" data-note="${n.id}">suggest</button></div>
    </div>`;
  }).join("") : '<p class="muted small">No one else has noted this Trial yet.</p>';
}

// ---------- taxonomy ----------
function renderTax() {
  const pane = $("#tax-pane");
  const focused = document.activeElement;
  if (pane.contains(focused) && focused.matches("input, textarea, select")) { S.pendingTax = true; return; }
  S.pendingTax = false;
  const openDrill = new Set($$("details[data-drill][open]", pane).map((d) => d.dataset.drill));
  const newOpen = $("#tax-new", pane)?.open;
  const ms = activeModes();
  const coded = S.trials.filter((t) => t.coded).length;
  pane.innerHTML = `
    <div class="row"><b>${ms.length} modes</b><span class="muted">· ${coded} of ${S.trials.length} Trials coded</span><span class="spacer"></span><a href="/api/taxonomy/export" download="taxonomy.yaml">export taxonomy.yaml</a></div>
    <details id="tax-new" class="new-mode"${newOpen ? " open" : ""}><summary>new mode</summary>
      <div class="row"><input id="tm-name" placeholder="mode name" aria-label="mode name" maxlength="200"></div>
      <div class="row"><label>gradability <select id="tm-grad">${GRADABILITY.map((g) => `<option>${g}</option>`).join("")}</select></label></div>
      <textarea id="tm-def" rows="2" placeholder="definition" aria-label="definition"></textarea>
      <div class="row"><button type="button" data-action="create-mode">create</button></div>
    </details>
    ${ms.map((m) => modeCard(m, ms, openDrill.has(m.id))).join("") || '<p class="muted">No modes yet. Name one from a note.</p>'}`;
  for (const id of openDrill) loadDrill(id);
}

function modeCard(m, ms, drillOpen) {
  const head = S.renaming === m.id
    ? `<input class="m-rename" data-rename="${esc(m.id)}" value="${esc(m.name)}" aria-label="new name"><button type="button" data-action="rename-save" data-mode="${esc(m.id)}">save</button><button type="button" data-action="rename-cancel">cancel</button>`
    : `<span class="m-name">${esc(m.name)}</span><button type="button" class="link" data-action="rename" data-mode="${esc(m.id)}">rename</button>`;
  const ex = m.examples.map((e) => `<li><button type="button" class="link" data-action="open-trial" data-id="${esc(e.trial_id)}"${e.seq != null ? ` data-seq="${esc(e.seq)}"` : ""}>${esc(e.trial_id)}${e.seq != null ? " #" + esc(e.seq) : ""}</button>${e.caption ? ` <span class="muted">${esc(e.caption)}</span>` : ""} <button type="button" class="link danger" data-action="unpin" data-example="${e.id}" aria-label="unpin">unpin</button></li>`).join("");
  const targets = ms.filter((x) => x.id !== m.id);
  return `<div class="mode-card" data-mode="${esc(m.id)}">
    <div class="m-head">${head}<span class="chip">${m.trial_count} Trial${m.trial_count === 1 ? "" : "s"}</span>${m.proposed_count ? `<span class="chip mode proposed">+${m.proposed_count} proposed</span>` : ""}</div>
    <div class="row"><label>gradability <select data-grad="${esc(m.id)}">${GRADABILITY.map((g) => `<option${g === m.gradability ? " selected" : ""}>${g}</option>`).join("")}</select></label>
      <span class="muted small">by ${esc(m.created_by)}</span></div>
    <textarea data-def="${esc(m.id)}" rows="2" placeholder="definition: what counts, what doesn't" aria-label="definition of ${esc(m.name)}">${esc(m.definition)}</textarea>
    <div class="small">examples${ex ? `<ul>${ex}</ul>` : ": none"} <button type="button" data-action="pin" data-mode="${esc(m.id)}"${S.current ? "" : " disabled"}>pin current Trial</button></div>
    ${targets.length ? `<div class="row small"><select data-merge="${esc(m.id)}" aria-label="merge target"><option value="">merge into…</option>${targets.map((x) => `<option value="${esc(x.id)}">${esc(x.name)}</option>`).join("")}</select><button type="button" data-action="merge" data-mode="${esc(m.id)}">merge</button></div>` : ""}
    ${m.merged_from.length ? `<div class="muted small">merged from: ${m.merged_from.map((x) => esc(x.name)).join(", ")}</div>` : ""}
    <details data-drill="${esc(m.id)}"${drillOpen ? " open" : ""}><summary>notes in this mode (${m.note_count})</summary><div class="body muted small">loading…</div></details>
  </div>`;
}

async function loadDrill(modeId) {
  const det = $(`details[data-drill="${CSS.escape(modeId)}"]`);
  if (!det) return;
  const body = det.querySelector(".body");
  try {
    if (!S.drill.has(modeId)) S.drill.set(modeId, (await api("GET", `/api/notes?mode=${enc(modeId)}`)).notes);
    const notes = S.drill.get(modeId);
    body.classList.remove("muted");
    body.innerHTML = notes.length ? `<ul>${notes.map((n) => `<li><button type="button" class="link" data-action="open-trial" data-id="${esc(n.trial_id)}"${n.first_bad_seq != null ? ` data-seq="${esc(n.first_bad_seq)}"` : ""}>${esc(n.trial_id)}</button> <b>${esc(n.author)}</b>${n.verdict ? " · " + esc(n.verdict) : ""}: ${esc(n.text.length > 160 ? n.text.slice(0, 160) + "…" : n.text)}</li>`).join("")}</ul>` : "No accepted notes yet.";
  } catch (e) { body.textContent = e.message; }
}

// ---------- live updates ----------
let pending = [];
let refreshTimer;
function onChange(ev) {
  pending.push(ev);
  clearTimeout(refreshTimer);
  refreshTimer = setTimeout(refresh, 250);
}
async function refresh() {
  const evs = pending;
  pending = [];
  const touchesCurrent = evs.some((e) => e.kind === "resync" || e.kind === "mode" || e.kind === "example" || e.trial_id === S.current || (e.trial_ids || []).includes(S.current));
  S.drill.clear();
  try {
    await Promise.all([loadTrials(), loadModes(), touchesCurrent ? loadNotes() : null]);
  } catch (e) { return fail(e); }
  renderFilters();
  renderList();
  if (S.current) { renderNote(); updateMarkers(); }
  renderTax();
}

let es;
function setLive(on) { const el = $("#live"); el.textContent = on ? "live" : "offline"; el.className = "live " + (on ? "on" : "off"); }
function connect() {
  es = new EventSource("/api/events");
  let wasDown = false;
  es.addEventListener("hello", () => { setLive(true); if (wasDown) onChange({ kind: "resync" }); });
  es.onmessage = (m) => { try { onChange(JSON.parse(m.data)); } catch { /* ignore */ } };
  es.onerror = () => {
    setLive(false);
    wasDown = true;
    if (es.readyState === EventSource.CLOSED) { // e.g. 401 after the session expired
      setTimeout(async () => { const r = await fetch("/api/me").catch(() => null); if (r?.ok) connect(); else if (r?.status === 401) showGate("signin"); }, 5000);
    }
  };
}

// ---------- actions ----------
async function act(action, el) {
  const d = el.dataset;
  switch (action) {
    case "open-trial": return openTrial(d.id, d.seq != null ? Number(d.seq) : undefined);
    case "set-seq": {
      if (!S.trial) return;
      const seq = Number(d.seq);
      S.draft.first_bad_seq = S.draft.first_bad_seq === seq ? null : seq;
      $("#fbs").value = S.draft.first_bad_seq ?? "";
      updateMarkers();
      onDraftChange();
      return toast(S.draft.first_bad_seq == null ? "first bad step cleared" : `first bad step: #${seq}`);
    }
    case "clear-seq": S.draft.first_bad_seq = null; $("#fbs").value = ""; updateMarkers(); return onDraftChange();
    case "jump-mine": return S.draft?.first_bad_seq != null && jumpTo(S.draft.first_bad_seq);
    case "jump": return jumpTo(Number(d.seq));
    case "assign": {
      const mode = $("#assign-mode").value;
      if (!mode) return toast("pick a mode first");
      const noteId = await ensureMyNote();
      await api("POST", "/api/assignments", { note_id: noteId, mode_id: mode });
      return refreshNow();
    }
    case "create-assign": {
      const name = $("#nm-name").value.trim();
      if (!name) return toast("name the mode");
      const m = await api("POST", "/api/modes", { name, gradability: $("#nm-grad").value, definition: $("#nm-def").value });
      const noteId = await ensureMyNote();
      await api("POST", "/api/assignments", { note_id: noteId, mode_id: m.id });
      return refreshNow();
    }
    case "suggest": {
      const mode = $(`#suggest-${d.note}`).value;
      if (!mode) return toast("pick a mode first");
      await api("POST", "/api/assignments", { note_id: Number(d.note), mode_id: mode });
      return refreshNow();
    }
    case "accept": case "reject":
      await api("POST", `/api/assignments/${d.asg}/${action}`);
      return refreshNow();
    case "remove":
      await api("DELETE", `/api/assignments/${d.asg}`);
      return refreshNow();
    case "create-mode": {
      const name = $("#tm-name").value.trim();
      if (!name) return toast("name the mode");
      await api("POST", "/api/modes", { name, gradability: $("#tm-grad").value, definition: $("#tm-def").value });
      document.activeElement?.blur();
      return refreshNow();
    }
    case "rename": S.renaming = d.mode; renderTax(); return $(`input[data-rename="${CSS.escape(d.mode)}"]`)?.focus();
    case "rename-cancel": S.renaming = null; document.activeElement?.blur(); return renderTax();
    case "rename-save": {
      const input = $(`input[data-rename="${CSS.escape(d.mode)}"]`);
      await api("PATCH", `/api/modes/${enc(d.mode)}`, { name: input.value });
      S.renaming = null;
      document.activeElement?.blur();
      return refreshNow();
    }
    case "pin": {
      if (!S.current) return;
      const caption = window.prompt("Caption for this example (optional)", "");
      if (caption === null) return;
      await api("POST", `/api/modes/${enc(d.mode)}/examples`, { trial_id: S.current, seq: S.draft?.first_bad_seq ?? null, caption });
      return refreshNow();
    }
    case "unpin": await api("DELETE", `/api/examples/${d.example}`); return refreshNow();
    case "merge": {
      const target = $(`select[data-merge="${CSS.escape(d.mode)}"]`).value;
      if (!target) return toast("pick the mode to merge into");
      const src = S.modes.find((m) => m.id === d.mode);
      const dst = S.modes.find((m) => m.id === target);
      if (!window.confirm(`Merge "${src.name}" (${src.trial_count} Trials) into "${dst.name}" (${dst.trial_count} Trials)?\n\nEvery assignment and example moves to "${dst.name}". "${src.name}" is kept only as a merged alias. This can't be undone from the UI.`)) return;
      document.activeElement?.blur();
      const r = await api("POST", `/api/modes/${enc(d.mode)}/merge`, { into: target });
      toast(`merged: ${r.moved} moved, ${r.deduped} already there`);
      return refreshNow();
    }
  }
}
const refreshNow = () => { onChange({ kind: "resync" }); };

async function saveMode(id, patch) {
  try { await api("PATCH", `/api/modes/${enc(id)}`, patch); toast("saved"); } catch (e) { fail(e); }
}

function wire() {
  document.addEventListener("click", (e) => {
    const t = e.target.closest("[data-action]");
    if (t && !t.disabled) { e.preventDefault(); act(t.dataset.action, t).catch(fail); return; }
    const m = e.target.closest("[data-mtab]");
    if (m) setMobileTab(m.dataset.mtab);
    const s = e.target.closest("[data-stab]");
    if (s) setSideTab(s.dataset.stab);
  });
  document.addEventListener("change", (e) => {
    const t = e.target;
    if (t.dataset.filter) { S.filters[t.dataset.filter] = t.value; renderList(); return; }
    if (t.name === "verdict") { S.draft.verdict = t.value; return onDraftChange(); }
    if (t.id === "fbs") {
      const v = t.value === "" ? null : Number(t.value);
      if (v != null && (!Number.isInteger(v) || v < 0 || (S.trial && v >= S.trial.n_events))) { toast("not a step in this Trace"); return; }
      S.draft.first_bad_seq = v; updateMarkers(); return onDraftChange();
    }
    if (t.dataset.grad) return saveMode(t.dataset.grad, { gradability: t.value });
    if (t.dataset.def) return saveMode(t.dataset.def, { definition: t.value });
  });
  document.addEventListener("input", (e) => {
    if (e.target.id === "note-text") { S.draft.text = e.target.value; onDraftChange(); }
  });
  document.addEventListener("submit", (e) => e.preventDefault());
  document.addEventListener("toggle", (e) => {
    const det = e.target;
    if (!(det instanceof HTMLDetailsElement) || !det.open) return;
    if (det.classList.contains("lazy")) loadLazy(det);
    if (det.dataset.drill) loadDrill(det.dataset.drill);
  }, true);
  $("#tax-pane").addEventListener("focusout", () => setTimeout(() => { if (S.pendingTax) renderTax(); }, 0));
  document.addEventListener("keydown", (e) => {
    const t = e.target;
    if (t.dataset?.rename && e.key === "Enter") { e.preventDefault(); act("rename-save", { dataset: { mode: t.dataset.rename } }).catch(fail); return; }
    if (t.dataset?.rename && e.key === "Escape") { act("rename-cancel", t); return; }
    if (t.closest("input, textarea, select, [contenteditable]") || e.metaKey || e.ctrlKey || e.altKey) return;
    if (e.key === "j" || e.key === "k") {
      const rows = filtered();
      const i = rows.findIndex((r) => r.trial_id === S.current);
      const next = rows[Math.max(0, Math.min(rows.length - 1, i + (e.key === "j" ? 1 : -1)))];
      if (next && next.trial_id !== S.current) openTrial(next.trial_id).catch(fail);
    }
  });
  window.addEventListener("pagehide", () => {
    if (!S.dirty || !S.draftTrial) return;
    fetch(`/api/trials/${enc(S.draftTrial)}/note`, { method: "PUT", keepalive: true, credentials: "same-origin",
      headers: { "Content-Type": "application/json", "X-Requested-With": "annotate" }, body: JSON.stringify(S.draft) });
  });
  $("#theme").addEventListener("click", () => { theme = THEMES[(THEMES.indexOf(theme) + 1) % THEMES.length]; applyTheme(); });
  $("#signout").addEventListener("click", async () => { await fetch("/auth/logout", { method: "POST" }); location.href = "/"; });
}

async function boot() {
  applyTheme();
  let r;
  try { r = await fetch("/api/me", { credentials: "same-origin" }); } catch { return showGate("error"); }
  if (r.status === 401) return showGate("signin");
  if (r.status === 403) return showGate("denied");
  if (!r.ok) return showGate("error");
  S.me = (await r.json()).login;
  $("#whoami").textContent = S.me;
  $("#signout").hidden = false;
  $("#app").hidden = false;
  wire();
  setMobileTab("trials");
  try { await Promise.all([loadTrials(), loadModes()]); } catch (e) { return fail(e); }
  renderFilters();
  renderList();
  renderNote();
  renderTax();
  connect();
  const m = /trial=([^&]+)/.exec(location.hash);
  if (m) openTrial(decodeURIComponent(m[1])).catch(fail);
}

boot();
