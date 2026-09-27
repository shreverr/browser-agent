// Trace rendering for ea-1 Traces (browser_agent/evals/analysis/TRACE_FORMAT.md), ported from
// the prototype's layout D: `#seq type` step lines, observation and model_call collapsed, and
// blobs (what the model saw) fetched only when a line is expanded.
// Every string from a Trace is untrusted: it goes through esc() before reaching innerHTML.

export const esc = (s) =>
  String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
export const usd = (x) => (typeof x === "number" ? "$" + (x < 0.01 ? x.toFixed(4) : x.toFixed(2)) : "—");
const num = (x) => (typeof x === "number" ? x.toLocaleString() : "?");
const clip = (s, n) => { s = String(s ?? ""); return s.length > n ? s.slice(0, n) + "…" : s; };

export function safeUrl(u) {
  return typeof u === "string" && /^https?:\/\//i.test(u) ? u : null;
}
export function urlLink(u) {
  const ok = safeUrl(u);
  return ok ? `<a href="${esc(ok)}" target="_blank" rel="noopener noreferrer nofollow">${esc(clip(ok, 120))}</a>` : esc(u);
}

// ---------- blob loading ----------
const blobCache = new Map();
export async function fetchBlob(ref) {
  if (!ref || !ref.sha256) return null;
  if (!blobCache.has(ref.sha256)) {
    blobCache.set(ref.sha256, (async () => {
      const r = await fetch(`/api/blobs/${encodeURIComponent(ref.sha256)}`, { credentials: "same-origin" });
      if (!r.ok) throw new Error(`blob ${ref.sha256.slice(0, 12)}: HTTP ${r.status}`);
      const text = await r.text();
      if ((ref.media_type || "").includes("json")) { try { return { json: JSON.parse(text), text }; } catch { /* fall through */ } }
      return { text };
    })());
  }
  try { return await blobCache.get(ref.sha256); } catch (e) { blobCache.delete(ref.sha256); throw e; }
}

function refAttr(refs) { return esc(JSON.stringify(refs)); }
function lazy(kind, refs, label) {
  const list = (Array.isArray(refs) ? refs : [refs]).filter(Boolean);
  if (!list.length) return "";
  return `<details class="lazy" data-lazy="${kind}" data-refs="${refAttr(list)}"><summary>${esc(label)}</summary><div class="body muted">loading…</div></details>`;
}

function renderContent(content) {
  if (content == null) return "";
  if (typeof content === "string") return `<pre>${esc(content)}</pre>`;
  if (Array.isArray(content)) {
    return content.map((p) => {
      if (p && p.type === "text") return `<pre>${esc(p.text)}</pre>`;
      if (p && (p.type === "image_url" || p.type === "image")) {
        const url = p.image_url?.url ?? "";
        return `<div class="muted small">[image${url.startsWith("data:") ? `, ${Math.round(url.length * 0.75 / 1024)} KB` : ""}]</div>`;
      }
      return `<pre>${esc(JSON.stringify(p, null, 2))}</pre>`;
    }).join("");
  }
  return `<pre>${esc(JSON.stringify(content, null, 2))}</pre>`;
}

function renderMessage(m, i, last) {
  if (!m || typeof m !== "object") return `<pre>${esc(String(m))}</pre>`;
  const extra = [];
  if (m.tool_calls) extra.push(`<pre>${esc(m.tool_calls.map((c) => `${c.function?.name ?? c.name}(${c.function?.arguments ?? c.arguments ?? ""})`).join("\n"))}</pre>`);
  if (m.tool_call_id) extra.push(`<div class="muted small">tool_call_id ${esc(m.tool_call_id)}</div>`);
  const role = `${i}: ${m.role ?? "?"}${m.name ? " · " + m.name : ""}`;
  const size = typeof m.content === "string" ? ` · ${num(m.content.length)} chars` : "";
  return `<details class="msg"${last ? " open" : ""}><summary><span class="msg-role">${esc(role)}${esc(size)}</span></summary>${renderContent(m.content)}${extra.join("")}</details>`;
}

// Fill a lazy <details> the first time it opens.
export async function loadLazy(det) {
  if (det.dataset.loaded) return;
  det.dataset.loaded = "1";
  const body = det.querySelector(":scope > .body");
  let refs = [];
  try { refs = JSON.parse(det.dataset.refs || "[]"); } catch { refs = []; }
  try {
    const blobs = await Promise.all(refs.map(fetchBlob));
    if (!blobs.length) { body.textContent = "(nothing captured)"; return; }
    if (det.dataset.lazy === "messages") {
      body.innerHTML = blobs.map((b, i) => renderMessage(b?.json ?? b?.text, i, i === blobs.length - 1)).join("");
    } else {
      body.innerHTML = blobs.map((b) => (b?.json !== undefined ? `<pre>${esc(JSON.stringify(b.json, null, 2))}</pre>` : `<pre>${esc(b?.text ?? "")}</pre>`)).join("");
    }
    body.classList.remove("muted");
  } catch (e) {
    body.textContent = String(e.message || e);
    delete det.dataset.loaded;
  }
}

// ---------- event lines ----------
function summary(e) {
  switch (e.type) {
    case "observation":
      return `${e.title ? clip(e.title, 80) + " · " : ""}${clip(e.url, 100)} · ${num(e.n_controls)} controls${e.truncated ? " · truncated" : ""}`;
    case "model_call": {
      const u = e.usage || {};
      return [e.role, e.served_model || e.requested_model, `${num(u.prompt_tokens)} in / ${num(u.completion_tokens)} out`,
        e.context_pct != null ? `ctx ${e.context_pct}%` : null, e.cost_usd != null ? usd(e.cost_usd) : null,
        e.latency_ms != null ? `${num(e.latency_ms)} ms` : null, e.finish_reason, e.retries ? `${e.retries} retries` : null,
        e.error ? `✗ ${clip(e.error, 80)}` : null].filter(Boolean).join(" · ");
    }
    case "tool_call": return `${e.name}(${clip(JSON.stringify(e.arguments ?? {}), 300)})`;
    case "action_result": return `${e.name ?? ""} · ${e.status ?? "?"}${e.error_code ? " · " + e.error_code : ""}${e.message ? " — " + clip(e.message, 160) : ""}`;
    case "compaction": return `replaced ${(e.replaced || []).length} message(s)`;
    case "user_exchange":
      return `${e.kind}: “${clip(e.question, 200)}” → “${clip(e.reply, 200)}” (${e.source})${e.counts_as_intervention ? " · counts as intervention" : ""}`;
    case "intervention": return `${e.source} · ${e.trigger}${e.action_refused ? " · action refused" : ""}`;
    case "memory_write": return `${e.op} ${e.key}${e.op === "remember" ? " = " + clip(JSON.stringify(e.value), 120) : ""}`;
    case "vision_fallback": return String(e.error ?? "");
    case "blocked_navigation": return `${clip(e.url, 120)} — ${e.message ?? ""}`;
    case "trial_end": return `${e.status}${e.terminal_reason ? " / " + e.terminal_reason : ""}${e.answer ? " — “" + clip(e.answer, 200) + "”" : ""}`;
    default: return clip(JSON.stringify(e), 300);
  }
}

function body(e) {
  const s = esc(summary(e));
  switch (e.type) {
    case "observation":
      return `<details class="lazy" data-lazy="text" data-refs="${refAttr([e.rendered_ref].filter(Boolean))}"><summary>${s}</summary><div class="body muted">${e.rendered_ref ? "loading…" : "(no rendered text)"}</div></details>${e.structured_ref ? " " + lazy("text", e.structured_ref, "structured") : ""}`;
    case "model_call": {
      const calls = (e.raw_tool_calls || []).map((c) => `${c.name}(${c.arguments ?? ""})`).join("\n");
      const u = e.usage || {};
      const meta = { requested_model: e.requested_model, served_model: e.served_model, provider: e.provider, finish_reason: e.finish_reason,
        usage: u, context_length: e.context_length, request_sha256: e.request_sha256 };
      return `<details><summary>${s}</summary><div class="body">
        ${e.content ? `<div class="muted small">assistant text</div><pre>${esc(e.content)}</pre>` : ""}
        ${calls ? `<div class="muted small">raw tool call${(e.raw_tool_calls || []).length > 1 ? "s" : ""}</div><pre>${esc(calls)}</pre>` : ""}
        ${e.error ? `<pre class="bad-t">${esc(e.error)}</pre>` : ""}
        ${lazy("messages", e.message_refs, `messages the model saw (${(e.message_refs || []).length})`)}
        ${lazy("text", e.tools_ref, "tool schemas")}
        <details><summary>call metadata</summary><pre>${esc(JSON.stringify(meta, null, 2))}</pre></details>
      </div></details>`;
    }
    case "tool_call": {
      const full = JSON.stringify(e.arguments ?? {}, null, 2);
      return full.length > 300 ? `<details><summary>${s}</summary><pre>${esc(full)}</pre></details>` : s;
    }
    case "action_result":
      if (e.rendered_ref) return `<details class="lazy" data-lazy="text" data-refs="${refAttr([e.rendered_ref])}"><summary>${s}</summary><div class="body muted">loading…</div></details>`;
      return e.rendered ? `<details><summary>${s}</summary><div class="body"><pre>${esc(e.rendered)}</pre></div></details>` : s;
    case "compaction":
      return `<details><summary>${s}</summary><div class="body">${(e.replaced || []).map((r) =>
        `<div>message ${esc(r.index)}: ${lazy("messages", r.before_ref, "before")} ${lazy("messages", r.after_ref, "after")}</div>`).join("")}</div></details>`;
    case "intervention":
      return e.injected_text_ref ? `<details class="lazy" data-lazy="text" data-refs="${refAttr([e.injected_text_ref])}"><summary>${s}</summary><div class="body muted">loading…</div></details>` : s;
    case "blocked_navigation":
      return `${urlLink(e.url)} — ${esc(e.message ?? "")}`;
    default:
      return s;
  }
}

export function eventLine(e) {
  const unscripted = e.type === "user_exchange" && e.source === "unscripted_fallback";
  const flag = unscripted ? ' <span class="chip bad">unscripted_fallback</span>' : "";
  return `<div class="a-ev ev-${esc(e.type)}${unscripted ? " ev-unscripted" : ""}" id="ev-${esc(e.seq)}" data-seq="${esc(e.seq)}">
    <span class="a-evt"><button type="button" class="seq" data-action="set-seq" data-seq="${esc(e.seq)}" title="Mark #${esc(e.seq)} as the first thing that went wrong">#${esc(e.seq)}</button> ${esc(e.type)}</span>
    ${body(e)}${flag}<span class="marks"></span></div>`;
}

// ---------- the whole Trace ----------
export function renderTrace(trial) {
  const h = trial.header || {};
  const t = h.totals || {};
  const task = h.task || {};
  const v = h.variant || {};
  const cell = task.cell || {};
  const cellTags = Object.entries(cell).flatMap(([k, val]) => (Array.isArray(val) ? val : [val]).map((x) => `<span class="chip">${esc(k)}: ${esc(x)}</span>`)).join(" ");
  const status = `${h.status}${h.terminal_reason ? " / " + h.terminal_reason : ""}`;
  const statusCls = h.status === "completed" ? (h.terminal_reason === "done" ? "good" : "warn") : h.status === "infra_error" ? "infra" : "bad";
  const rules = (h.simulated_user || []).map((r) => `<tr><td><code>${esc(r.id)}</code></td><td><code>${esc(r.match)}</code></td><td>${esc(r.reply)}</td></tr>`).join("");
  const memory = h.initial_memory && Object.keys(h.initial_memory).length ? `<details><summary class="muted">initial memory (${Object.keys(h.initial_memory).length})</summary><pre>${esc(JSON.stringify(h.initial_memory, null, 2))}</pre></details>` : "";
  const byStep = new Map();
  for (const e of trial.events) { if (!byStep.has(e.step)) byStep.set(e.step, []); byStep.get(e.step).push(e); }
  const steps = [...byStep.entries()].map(([n, evs]) => `<div class="a-step"><div class="a-stepn">step ${esc(n)}</div><div>${evs.map(eventLine).join("")}</div></div>`).join("");
  return `<article class="a-trace">
    <div class="a-thead"><b>${esc(h.trial_id)}</b> · <span class="chip ${statusCls}">${esc(status)}</span> · ${esc(t.steps ?? "?")} steps · ${usd(t.cost_usd)} · peak ctx ${esc(t.peak_context_pct ?? "?")}% · ${esc(t.wall_s ?? "?")}s
      <div class="muted">${esc(v.model ?? "")}${v.code_revision ? " · " + esc(v.code_revision) : ""} · batch ${esc(h.batch_id)} · attempt ${esc(h.attempt ?? "?")} · ${esc(h.automation_outcome ?? "")}${h.unscripted_fallback ? ' · <b class="bad-t">?unscripted</b>' : ""}${v.vision === false ? " · vision off" : ""}${h.infra ? ` · <b class="bad-t">infra: ${esc(h.infra.class)}</b> — ${esc(h.infra.message)}` : ""}</div></div>
    <div class="a-task">${esc(task.text)}</div>
    <div class="row small">${task.start_url ? "start " + urlLink(task.start_url) : ""} ${cellTags}</div>
    ${task.notes ? `<div class="muted small">task notes: ${esc(task.notes)}</div>` : ""}
    ${rules ? `<details open><summary class="muted">Simulated user rules (${(h.simulated_user || []).length})</summary><table class="a-rules"><thead><tr><th>id</th><th>match</th><th>reply</th></tr></thead><tbody>${rules}</tbody></table></details>` : '<div class="muted small">No Simulated user rules: every ask_user is unscripted.</div>'}
    ${memory}
    ${h.answer ? `<div class="a-answer">done: “${esc(h.answer)}”</div>` : ""}
    ${h.infra?.traceback ? `<details><summary class="muted">infra traceback</summary><pre>${esc(h.infra.traceback)}</pre></details>` : ""}
    <p class="muted small">Click a step's <b>#seq</b> to mark the first thing that went wrong on your note.</p>
    ${steps}
  </article>`;
}
