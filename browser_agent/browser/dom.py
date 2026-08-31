"""Small JS helpers that run INSIDE the page.

Perception itself comes from Playwright's ``aria_snapshot(mode="ai")`` (parsed in
``aria.py``), which already surfaces clickable generics with refs, cursor and
box hints. These helpers cover only what that snapshot doesn't give us:

- ``OVERLAY_RECT_SCRIPT`` — the bounding box of the top blocking overlay, so we
  can flag controls inside a modal even when it has no ``role=dialog``.
- ``VIEWPORT_SCRIPT`` — the CSS-pixel viewport size, for the ``click_at`` note.
- ``LABEL_JS`` — a compact label cascade to backfill a name for the few controls
  the aria snapshot leaves nameless (icon buttons, ``name``/``id``-only inputs).

Note: kept as plain JS strings so Playwright injects them verbatim.
"""

from __future__ import annotations

from typing import TypedDict


class ElementInfo(TypedDict, total=False):
    """One node of the page as the model sees it. Controls carry `index`, `text`
    and `box`; headings and status text carry only `name`."""

    kind: str  # "control" | "heading" | "text"
    index: int  # global index the model acts by, assigned Python-side
    role: str  # button/link/textbox/checkbox/radio/combobox/clickable/...
    name: str  # label (from aria, or LABEL_JS backfill)
    text: str  # rendered leaf string, e.g. button "Save & Place Order"
    overlay: bool  # inside a modal/popup/overlay that is likely blocking the page
    box: object  # (x, y, w, h) viewport CSS px, from the aria snapshot


OVERLAY_RECT_SCRIPT = r"""(() => {
  function rectOf(el) {
    const r = el.getBoundingClientRect();
    if (r.width < 80 || r.height < 80) return null;
    return { x: r.left, y: r.top, w: r.width, h: r.height, area: r.width * r.height };
  }
  // Fast path: semantic dialogs.
  let best = null, bestScore = -1;
  for (const el of document.querySelectorAll('[role=dialog],[role=alertdialog],[aria-modal="true"]')) {
    const rr = rectOf(el);
    if (rr && rr.area > bestScore) { bestScore = rr.area; best = rr; }
  }
  if (best) return best;
  // Fallback: a fixed/sticky, high-z-index container (a role-less modal).
  bestScore = -1;
  let checked = 0;
  for (const el of document.querySelectorAll("div,section,aside,form")) {
    if (checked++ > 4000) break;
    let z = 0;
    try {
      const cs = getComputedStyle(el);
      if (cs.position !== "fixed" && cs.position !== "sticky") continue;
      z = parseInt(cs.zIndex, 10) || 0;
    } catch (e) { continue; }
    if (z < 1000) continue;
    const rr = rectOf(el);
    if (!rr) continue;
    const score = z * 1e7 + rr.area;
    if (score > bestScore) { bestScore = score; best = rr; }
  }
  return best;
})()"""

VIEWPORT_SCRIPT = "() => ({ w: window.innerWidth, h: window.innerHeight })"

# Compact label resolver for the few controls the aria snapshot leaves nameless
# (icon buttons with no aria-name, inputs with only name=/id=). Applied per
# element via `frame.locator("aria-ref=eN").evaluate(LABEL_JS)`. The cascade runs
# most-authoritative first: aria-label, aria-labelledby, <label>, own text,
# value/placeholder/title, icon hints, then name/id/data-* as a last resort.
LABEL_JS = r"""(el) => {
  const clip = s => (s||'').replace(/\s+/g,' ').trim().slice(0,120);
  const hum = s => (s||'').replace(/[_\-]+/g,' ').replace(/([a-z0-9])([A-Z])/g,'$1 $2').replace(/\s+/g,' ').trim().toLowerCase();
  const t = e => e ? clip(e.innerText||e.textContent||'') : '';
  const a = n => { try { return el.getAttribute(n)||''; } catch(e){ return ''; } };
  let s;
  if ((s=clip(a('aria-label')))) return s;
  const lb = a('aria-labelledby');
  if (lb) { const root=(el.getRootNode&&el.getRootNode())||document;
    const txt=lb.split(/\s+/).map(id=>{let e=null;try{e=root.getElementById(id)}catch(_){} return e?t(e):''}).filter(Boolean).join(' ');
    if (clip(txt)) return clip(txt); }
  if (el.id) { const root=(el.getRootNode&&el.getRootNode())||document;
    try { const l=root.querySelector('label[for="'+CSS.escape(el.id)+'"]'); if(l&&t(l)) return t(l); } catch(_){} }
  const anc = el.closest && el.closest('label'); if (anc && t(anc)) return t(anc);
  const tag = el.tagName.toLowerCase();
  if (tag!=='select' && t(el)) return t(el);
  if ((tag==='input'||tag==='textarea')) { try { if (clip(el.value)) return clip(el.value); } catch(_){} }
  if ((s=clip(a('placeholder')))) return s;
  if ((s=clip(a('title')))) return s;
  try { const st=el.querySelector('svg title, title'); if(st&&t(st)) return t(st); } catch(_){}
  try { const im=el.querySelector('img[alt]'); if(im&&clip(im.getAttribute('alt'))) return clip(im.getAttribute('alt')); } catch(_){}
  const use = el.querySelector && el.querySelector('use');
  if (use) { const h=(use.getAttribute('href')||use.getAttribute('xlink:href')||'').replace(/^#/,'').replace(/^icon[-_]?/i,''); if(h) return hum(h); }
  const cls = (a('class')||'').toLowerCase();
  const KNOWN = ['close','search','cart','menu','back','remove','delete','edit','add','next','prev','filter','account','profile','wishlist','favorite','share','settings','notification','location','hamburger'];
  for (const k of KNOWN) if (cls.includes(k)) return k;
  if ((s=hum(a('name')))) return s;
  if ((s=hum(el.id))) return s;
  for (const at of (el.attributes||[])) { if (/^data-(testid|test-id|test|qa|action|name|icon|label)$/i.test(at.name) && at.value) return hum(at.value); }
  let p=el.previousElementSibling, hop=0; while(p&&hop<3){ if(t(p)) return t(p); p=p.previousElementSibling; hop++; }
  return '';
}"""
