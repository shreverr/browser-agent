"""Camoufox (Firefox) browser wrapper.

Camoufox is a hardened Firefox build with anti-fingerprinting baked into the
binary, so no stealth plugin is wired in here.

Perception lives in `aria.py`: every turn `get_state()` walks Playwright's
computed accessibility tree per frame, turns each ref'd node into an indexed
control, and records where that control lives so actions can reach it — even
inside a cross-origin iframe.
"""

from __future__ import annotations

import base64
import re
from typing import Optional, TypedDict
from urllib.parse import urlparse

from camoufox.sync_api import Camoufox

from ..config import ALLOWED_DOMAINS, STATE_FILE
from . import aria, playwright_patch
from .dom import LABEL_JS, OVERLAY_RECT_SCRIPT, VIEWPORT_SCRIPT, ElementInfo

# STATE_FILE persists cookies + localStorage across runs, so a one-time
# human/CAPTCHA check (e.g. Cloudflare clearance) or login carries over.
# ALLOWED_DOMAINS, when non-empty, is the navigation boundary `navigate()` enforces.

MAX_CONTROLS = 150  # cap the element list per turn
MAX_LABEL_BACKFILLS = 25  # cap the per-turn DOM lookups for nameless controls


class PageState(TypedDict):
    url: str
    title: str
    elements: list[ElementInfo]  # controls only, globally indexed (action surface)
    nodes: list[ElementInfo]  # ordered controls + headings + status text (for render)


class _Target(TypedDict):
    frame: object  # playwright Frame
    ref: str  # aria-ref id (eN) from that frame's latest aria_snapshot


def _in_rect(box, rect) -> bool:
    """Is the centre of an aria [box=x,y,w,h] inside an overlay rect? Both are in
    viewport CSS pixels, so they share a coordinate space."""
    if not box or not rect:
        return False
    x, y, w, h = box
    cx, cy = x + w / 2, y + h / 2
    return rect["x"] <= cx <= rect["x"] + rect["w"] and rect["y"] <= cy <= rect["y"] + rect["h"]


def _origin(url: str):
    p = urlparse(url or "")
    return (p.scheme, p.netloc)


def _intersects_viewport(box, vw: float, vh: float, margin: float = 40.0) -> bool:
    """Does an aria [box=x,y,w,h] overlap the viewport (with a small margin)? Used
    to keep the outline to on-screen controls (aria_snapshot returns the whole page)."""
    x, y, w, h = box
    return (x + w) >= -margin and x <= vw + margin and (y + h) >= -margin and y <= vh + margin


class BrowserSession:
    def __init__(self) -> None:
        self._camoufox = None  # the Camoufox context-manager instance
        self.browser = None
        self.context = None
        self.page = None
        # Maps the global element index shown to the model -> the frame it lives
        # in and its aria-ref within that frame. Lets actions reach elements
        # inside iframes. Rebuilt by every get_state().
        self._targets: list[_Target] = []

    def launch(self, headless: bool) -> None:
        # Heal a Playwright Firefox driver crash before the driver spawns.
        playwright_patch.apply()
        # Camoufox handles anti-fingerprinting natively. locale is set at the
        # Camoufox level so the spoofed fingerprint stays internally consistent;
        # geoip is off since no proxy is in use. humanize gives natural cursor
        # motion — a good fit for an interactive agent.
        self._camoufox = Camoufox(
            headless=headless,
            humanize=True,
            locale="en-IN",
            geoip=False,
            # Camoufox owns window/viewport sizing (its fingerprint must stay
            # consistent), so set the size here rather than as a Playwright
            # `viewport` on new_context — the latter is rejected by Camoufox's
            # patched Juggler protocol.
            window=(1280, 900),
        )
        self.browser = self._camoufox.__enter__()  # yields a Playwright Browser
        self.context = self.browser.new_context(
            # Let Camoufox's `window` govern size; disable Playwright's default
            # viewport emulation, whose setDefaultViewport call Camoufox rejects.
            no_viewport=True,
            timezone_id="Asia/Kolkata",
            # Geolocation defaults to New Delhi.
            geolocation={"latitude": 28.6139, "longitude": 77.209},
            # Firefox only honours a narrow permission set — geolocation +
            # notifications. (clipboard/camera/microphone throw under Firefox.)
            permissions=["geolocation", "notifications"],
            storage_state=str(STATE_FILE) if STATE_FILE.exists() else None,
        )
        self.page = self.context.new_page()

    def _settle(self) -> None:
        # Best-effort wait for the page to stop moving. networkidle can hang on
        # sites with long-polling, so cap it and move on.
        try:
            self.page.wait_for_load_state("domcontentloaded", timeout=8000)
        except Exception:
            pass
        self.page.wait_for_timeout(400)

    def _locator(self, index: int):
        if index < 0 or index >= len(self._targets):
            raise ValueError(f"No element with index {index} on the current page")
        t = self._targets[index]
        # aria-ref resolves against that frame's most recent aria_snapshot (which
        # get_state took this turn) back to the exact live element — works for
        # divs, shadow DOM, and cross-origin iframes alike.
        return t["frame"].locator(f'aria-ref={t["ref"]}')

    def _snapshot_frames(self):
        """The frames worth snapshotting: the main document plus any frame that is
        cross-origin to its parent (e.g. a consent/cookie wall). A frame's
        aria_snapshot already includes its SAME-origin children, so including
        those would count their content twice."""
        for frame in self.page.frames:
            parent = frame.parent_frame
            if parent is None or _origin(frame.url) != _origin(parent.url):
                yield frame

    def _label_backfill(self, frame, ref: str) -> str:
        """Resolve a name for the few controls aria leaves nameless (icon buttons,
        name/id-only inputs) by asking the DOM element itself."""
        try:
            return frame.locator(f"aria-ref={ref}").evaluate(LABEL_JS) or ""
        except Exception:
            return ""

    def get_state(self) -> PageState:
        elements: list[ElementInfo] = []  # controls only (the action surface)
        nodes: list[ElementInfo] = []  # ordered controls + headings + text
        self._targets = []

        # A blocking modal may have no role=dialog, so also find the top overlay's
        # box and flag controls that fall inside it (main frame only).
        overlay_rect = None
        vw = vh = 0
        try:
            overlay_rect = self.page.evaluate(OVERLAY_RECT_SCRIPT)
            vp = self.viewport()
            vw, vh = vp.get("w", 0), vp.get("h", 0)
        except Exception:
            pass
        main_frame = self.page.main_frame
        backfills = 0

        for frame in self._snapshot_frames():
            try:
                snap = frame.locator("body").aria_snapshot(mode="ai", boxes=True)
            except Exception:
                continue  # detached / not-yet-loaded / no body
            # aria_snapshot isn't viewport-limited; keep the outline to what's on
            # screen. Only the main frame is filtered — iframe boxes are in their
            # own coordinate space.
            clip_to_viewport = frame is main_frame and vw and vh

            for n in aria.flatten(aria.parse(snap)):
                if n["kind"] != "control":  # heading / status text
                    nodes.append({"kind": n["kind"], "name": n.get("name", ""), "overlay": n.get("overlay", False)})
                    continue
                if len(elements) >= MAX_CONTROLS:
                    continue
                box = n.get("box")
                if clip_to_viewport and box and not _intersects_viewport(box, vw, vh):
                    continue
                if not n.get("name") and backfills < MAX_LABEL_BACKFILLS:
                    backfills += 1
                    n["name"] = self._label_backfill(frame, n["ref"]) or n["name"]
                ctrl: ElementInfo = {
                    "kind": "control",
                    "index": len(elements),
                    "role": n.get("role", ""),
                    "name": n.get("name", ""),
                    "text": aria.leaf(n),
                    "overlay": bool(n.get("overlay")) or _in_rect(box, overlay_rect),
                    "box": box,
                }
                elements.append(ctrl)
                nodes.append(ctrl)  # same dict — shared so render sees the global index
                self._targets.append({"frame": frame, "ref": n["ref"]})
        try:
            title = self.page.title()
        except Exception:
            title = ""
        return {"url": self.page.url, "title": title, "elements": elements, "nodes": nodes}

    def viewport(self) -> dict:
        try:
            return self.page.evaluate(VIEWPORT_SCRIPT)
        except Exception:
            return {"w": 1280, "h": 900}

    def navigate(self, url: str) -> str:
        if not re.match(r"^https?://", url, re.I):
            url = "https://" + url
        if ALLOWED_DOMAINS:
            host = (urlparse(url).hostname or "").lower()
            if not any(host == d or host.endswith("." + d) for d in ALLOWED_DOMAINS):
                return (
                    f"BLOCKED: navigation to '{host}' is not allowed. Permitted domains: "
                    f"{', '.join(ALLOWED_DOMAINS)}. Stay on the allowed site(s) and use their "
                    "own links/search to get where you need."
                )
        self.page.goto(url, wait_until="domcontentloaded", timeout=30000)
        self._settle()
        # Cookie/consent popups often appear a beat after load — give them a
        # moment so they show up in the very next element snapshot.
        self.page.wait_for_timeout(900)
        return f"Navigated to {url}"

    def click(self, index: int) -> str:
        el = self._locator(index)
        el.scroll_into_view_if_needed(timeout=5000)
        el.click(timeout=8000)
        self._settle()
        return f"Clicked element [{index}]"

    def click_at(self, x: float, y: float) -> str:
        """Click a raw viewport coordinate (CSS px) — a vision fallback for a
        target the model can see in the screenshot but that has no index."""
        self.page.mouse.click(float(x), float(y))
        self._settle()
        return f"Clicked at ({int(x)}, {int(y)})"

    def _enter_text(self, el, text: str) -> None:
        el.scroll_into_view_if_needed(timeout=5000)
        el.click(timeout=8000)
        el.fill("")  # clear any existing value
        el.press_sequentially(text, delay=20)

    def type(self, index: int, text: str, submit: bool) -> str:
        el = self._locator(index)
        self._enter_text(el, text)
        if submit:
            el.press("Enter")
            self._settle()
            return f'Typed "{text}" into [{index}] and pressed Enter'
        return f'Typed "{text}" into [{index}]'

    # <input> types that are not text entry — never filled, never clicked.
    _NONTEXT_INPUTS = ("button", "submit", "reset", "checkbox", "radio", "file", "image")
    _FIELD_INFO_JS = (
        "e => ({tag: (e.tagName||'').toLowerCase(), "
        "type: (e.getAttribute && (e.getAttribute('type')||'')).toLowerCase(), "
        "editable: e.isContentEditable === true})"
    )

    def fill_form(self, fields: list) -> str:
        """Fill several fields in ONE call — much faster than one `type` per turn.
        `fields` = [{index, text, submit?}]. Stops early if the form re-renders
        mid-batch (stale refs) and tells the agent to re-read the element list."""
        if not fields:
            return "No fields provided."
        done: list[str] = []
        for n, f in enumerate(fields):
            try:
                idx = int(f["index"])
                text = str(f.get("text", ""))
                submit = bool(f.get("submit", False))
            except Exception:
                done.append(f"(skipped malformed field #{n}: {f!r})")
                continue
            try:
                el = self._locator(idx)
                # Stale-ref guard: if the tagged element is gone, the form
                # re-rendered after a previous field — stop and let the agent re-read.
                if el.count() == 0:
                    done.append(
                        f"[{idx}] not found — the form changed after a previous field. "
                        "Stopped; re-read the element list and fill the remaining fields."
                    )
                    break
                # Only ever touch text-entry fields. Refuse buttons/links/etc.
                # WITHOUT clicking them — otherwise the focus-click in _enter_text
                # would fire a button (e.g. "Place Order"), bypassing the click
                # path's confirmation guard. The model must `click` those separately.
                info = el.evaluate(self._FIELD_INFO_JS)
                fillable = (
                    info["editable"]
                    or info["tag"] == "textarea"
                    or (info["tag"] == "input" and info["type"] not in self._NONTEXT_INPUTS)
                )
                if not fillable:
                    done.append(
                        f"[{idx}] is a <{info['tag'] or 'element'}>, not a text field — skipped "
                        "(not filled or clicked). Use `click` for buttons/links, or "
                        "`select_option` for dropdowns."
                    )
                    continue
                self._enter_text(el, text)
                if submit:
                    el.press("Enter")
                    self._settle()
                    done.append(f'[{idx}] = "{text}" (submitted)')
                else:
                    done.append(f'[{idx}] = "{text}"')
            except Exception as e:
                done.append(f"[{idx}] failed ({e}); stopped — re-read the list and continue.")
                break
        self._settle()
        return "Filled fields:\n" + "\n".join(done)

    def type_otp(self, first_index: int, code: str) -> str:
        el = self._locator(first_index)
        el.scroll_into_view_if_needed(timeout=5000)
        el.click(timeout=8000)
        # Type each character with a short delay. Most OTP inputs auto-advance
        # focus on each keystroke so the subsequent digits land in the right boxes.
        for ch in code:
            self.page.keyboard.type(ch, delay=60)
            self.page.wait_for_timeout(50)
        self._settle()
        return f"Entered OTP code into digit fields starting at [{first_index}]"

    def select_option(self, index: int, value: str) -> str:
        el = self._locator(index)
        el.scroll_into_view_if_needed(timeout=5000)
        # Try matching by visible label first, then by value/text.
        try:
            el.select_option(label=value, timeout=6000)
        except Exception:
            el.select_option(value, timeout=6000)
        self._settle()
        return f'Selected "{value}" in [{index}]'

    def scroll(self, direction: str) -> str:
        dy = 700 if direction == "down" else -700
        self.page.mouse.wheel(0, dy)
        self.page.wait_for_timeout(400)
        return f"Scrolled {direction}"

    def back(self) -> str:
        self.page.go_back(wait_until="domcontentloaded", timeout=15000)
        self._settle()
        return "Went back"

    def read_page_text(self) -> str:
        text = self.page.evaluate("() => document.body ? document.body.innerText : ''")
        return re.sub(r"\n{3,}", "\n\n", text or "").strip()[:8000]

    def read_html(self) -> str:
        # Cleaned, truncated HTML — a richer fallback view when the labeled-
        # element list isn't enough (e.g. the agent is stuck and needs to see
        # raw structure).
        html = self.page.evaluate(
            """() => {
                const clone = document.documentElement.cloneNode(true);
                clone.querySelectorAll("script,style,noscript,svg,link,meta,template")
                    .forEach((n) => n.remove());
                return clone.outerHTML;
            }"""
        )
        return re.sub(r"\s{2,}", " ", html or "")[:14000]

    _CAPTCHA_PHRASES = re.compile(
        r"are you (a )?human|verify (you|that you) are human|i'?m not a robot|"
        r"checking your browser|checking if the site connection is secure|"
        r"just a moment|complete the captcha|unusual traffic|"
        r"needs to review the security|security check|press and hold",
        re.I,
    )
    _CAPTCHA_PROVIDERS = re.compile(
        r"recaptcha|hcaptcha|turnstile|challenges\.cloudflare|geo\.captcha", re.I
    )

    def detect_captcha(self) -> bool:
        """Is an ACTIVE human-verification challenge up? A Cloudflare / "are you
        human" interstitial, or a visibly sized captcha-provider iframe the user
        must interact with — not a passive invisible reCAPTCHA badge."""
        for f in self.page.frames:
            try:
                text = f.evaluate("() => document.body ? document.body.innerText.slice(0, 2000) : ''")
                if self._CAPTCHA_PHRASES.search(text or ""):
                    return True
            except Exception:
                pass  # cross-origin race / detached
            if f is not self.page.main_frame and self._CAPTCHA_PROVIDERS.search(f.url):
                try:
                    box = f.frame_element().bounding_box()
                    if box and box["width"] > 80 and box["height"] > 50:
                        return True
                except Exception:
                    pass
        return False

    def screenshot(self, path: str) -> None:
        try:
            self.page.screenshot(path=path)
        except Exception:
            pass

    def screenshot_b64(self) -> Optional[str]:
        """Viewport screenshot as a data URL, for sending to a multimodal model.
        JPEG (small) with a PNG fallback; returns None on any failure."""
        try:
            data = self.page.screenshot(type="jpeg", quality=60)
            mime = "jpeg"
        except Exception:
            try:
                data = self.page.screenshot()
                mime = "png"
            except Exception:
                return None
        return f"data:image/{mime};base64," + base64.b64encode(data).decode()

    def close(self) -> None:
        try:
            STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            if self.context is not None:
                self.context.storage_state(path=str(STATE_FILE))
        except Exception:
            pass  # best effort
        try:
            if self._camoufox is not None:
                self._camoufox.__exit__(None, None, None)
        except Exception:
            pass
