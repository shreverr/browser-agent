"""Camoufox (Firefox) browser wrapper.

Ported from src/browser.ts. Camoufox is a hardened Firefox build with
anti-fingerprinting baked into the binary, so it replaces the old
playwright-extra + puppeteer-extra-plugin-stealth stack entirely — no stealth
plugin is wired in here.

The public method names / return-string contracts match the TS version because
the agent loop depends on them.
"""

from __future__ import annotations

import base64
import os
import re
from pathlib import Path
from typing import Optional, TypedDict
from urllib.parse import urlparse

from camoufox.sync_api import Camoufox

from . import aria
from .dom import LABEL_JS, OVERLAY_RECT_SCRIPT, VIEWPORT_SCRIPT, ElementInfo

# Persist cookies + localStorage across runs so a one-time human/CAPTCHA check
# (e.g. Cloudflare clearance) or login carries over to later sessions.
STATE_FILE = Path(os.environ.get("AGENT_STATE_FILE", ".profile/state.json"))

# Optional deterministic safety boundary: restrict navigation to these domains
# (comma-separated in AGENT_ALLOWED_DOMAINS). Empty = unrestricted.
ALLOWED_DOMAINS = [
    d.strip().lower() for d in os.environ.get("AGENT_ALLOWED_DOMAINS", "").split(",") if d.strip()
]


# Original (buggy) Firefox page-error dispatch in Playwright's bundled driver.
# When a page throws an uncaught JS error whose `location` is undefined (some
# sites, e.g. Swiggy Instamart, do this), the driver runs `pageError.location.url`
# and crashes the whole Node driver process — killing the agent mid-task.
_PAGEERROR_ORIG = (
    '        this.addObjectListener(BrowserContext.Events.PageError, (pageError, page) => {\n'
    '          this._dispatchEvent("pageError", {\n'
    "            error: serializeError(pageError.error),\n"
    "            page: PageDispatcher.from(this, page),\n"
    "            location: {\n"
    "              url: pageError.location.url,\n"
    "              line: pageError.location.lineNumber,\n"
    "              column: pageError.location.columnNumber\n"
    "            }\n"
    "          });\n"
    "        });"
)
# Fixed: omit `location` when absent (else it fails the driver's string-type
# validation), and wrap the whole dispatch so no malformed page error can ever
# crash the driver.
_PAGEERROR_FIXED = (
    '        this.addObjectListener(BrowserContext.Events.PageError, (pageError, page) => {\n'
    "          try {\n"
    '            this._dispatchEvent("pageError", {\n'
    "              error: serializeError(pageError.error),\n"
    "              page: PageDispatcher.from(this, page),\n"
    "              location: pageError.location ? {\n"
    "                url: pageError.location.url,\n"
    "                line: pageError.location.lineNumber,\n"
    "                column: pageError.location.columnNumber\n"
    "              } : undefined\n"
    "            });\n"
    "          } catch (e) {}\n"
    "        });"
)


def _patch_playwright_firefox_pageerror() -> None:
    """Make the Playwright Firefox page-error dispatch crash-proof. Idempotent
    and best-effort (a failure just leaves the original behaviour)."""
    try:
        import playwright

        bundle = (
            Path(playwright.__file__).resolve().parent
            / "driver" / "package" / "lib" / "coreBundle.js"
        )
        text = bundle.read_text(encoding="utf-8")
        if "location: pageError.location ?" in text:
            return  # already patched
        # Handle a fresh install (original) or the earlier null-safe intermediate.
        intermediate = _PAGEERROR_ORIG.replace("pageError.location.", "pageError.location?.")
        for src in (_PAGEERROR_ORIG, intermediate):
            if src in text:
                bundle.write_text(text.replace(src, _PAGEERROR_FIXED), encoding="utf-8")
                return
    except Exception:
        pass


class PageState(TypedDict):
    url: str
    title: str
    elements: list[ElementInfo]  # controls only, globally indexed (action surface)
    nodes: list[ElementInfo]  # ordered controls + headings + status text (for render)


class _Target(TypedDict):
    frame: object  # playwright Frame
    ref: str  # aria-ref id (eN) from that frame's latest aria_snapshot


def _leaf(n: dict) -> str:
    """Render one control as the `[i] <text>` leaf the model reads, e.g.
    `button "Save & Place Order" (disabled)` or `clickable "Home • 207 km …"`."""
    s = f'{n.get("role", "")} "{n.get("name", "")}"'
    st = []
    if n.get("disabled"):
        st.append("disabled")
    if n.get("checked") in (True, "true", "mixed"):
        st.append("checked")
    exp = n.get("expanded")
    if exp == "true":
        st.append("expanded")
    elif exp == "false":
        st.append("collapsed")
    if n.get("selected"):
        st.append("selected")
    if st:
        s += " (" + ", ".join(st) + ")"
    opts = n.get("options") or []
    if opts:
        s += " | options=[" + " | ".join(opts) + "]"
    return s


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
        # in and its local index within that frame. Lets actions reach elements
        # inside iframes.
        self._targets: list[_Target] = []
        # Bumped every get_state(); used to detect stale refs during batched fills.
        self._snapshot_version = 0

    def launch(self, headless: bool) -> None:
        # Heal a Playwright Firefox driver crash before the driver spawns.
        _patch_playwright_firefox_pageerror()
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

    def get_state(self) -> PageState:
        elements: list[ElementInfo] = []  # controls only (the action surface)
        nodes: list[ElementInfo] = []  # ordered controls + headings + text
        self._targets = []
        self._snapshot_version += 1

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
        backfills = 0  # capped per-turn label lookups for nameless controls

        # Perceive via Playwright's computed accessibility tree, per frame (main
        # doc + iframes, incl. cross-origin consent/cookie walls). Every ref'd
        # node becomes an indexed control; ``_targets`` maps the global index to
        # that frame + aria-ref so actions can reach it (even inside iframes).
        for frame in self.page.frames:
            # A frame's aria_snapshot already includes its SAME-origin child
            # iframes, so only snapshot the main frame plus frames that are
            # cross-origin to their parent (e.g. a consent/cookie wall) — else
            # same-origin iframe content gets counted twice.
            parent = frame.parent_frame
            if parent is not None and _origin(frame.url) == _origin(parent.url):
                continue
            try:
                snap = frame.locator("body").aria_snapshot(mode="ai", boxes=True)
            except Exception:
                continue  # detached / not-yet-loaded / no body
            in_viewport = frame is main_frame and vw and vh
            for n in aria.flatten(aria.parse(snap)):
                if n["kind"] == "control":
                    if len(elements) >= 150:
                        continue
                    # aria_snapshot isn't viewport-limited; keep the outline to
                    # what's on screen (main frame; iframe boxes use their own
                    # coord space so we keep those). Boxless controls are kept.
                    box = n.get("box")
                    if in_viewport and box and not _intersects_viewport(box, vw, vh):
                        continue
                    # Backfill a label for the few controls aria leaves nameless
                    # (icon buttons, name/id-only inputs) using the DOM element.
                    if not n.get("name") and backfills < 25:
                        backfills += 1
                        try:
                            nm = frame.locator(f"aria-ref={n['ref']}").evaluate(LABEL_JS)
                            if nm:
                                n["name"] = nm
                        except Exception:
                            pass
                    ov = bool(n.get("overlay")) or _in_rect(n.get("box"), overlay_rect)
                    ctrl: ElementInfo = {
                        "kind": "control",
                        "index": len(elements),
                        "tag": "",
                        "role": n.get("role", ""),
                        "type": "",
                        "name": n.get("name", ""),
                        "text": _leaf(n),
                        "group": "",
                        "overlay": ov,
                        "box": n.get("box"),
                    }
                    elements.append(ctrl)
                    nodes.append(ctrl)  # same dict — shared so render sees the global index
                    self._targets.append({"frame": frame, "ref": n["ref"]})
                else:  # heading / status text
                    nodes.append(
                        {
                            "kind": n["kind"],
                            "level": n.get("level", 0),
                            "name": n.get("name", ""),
                            "overlay": n.get("overlay", False),
                        }
                    )
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

    def type(self, index: int, text: str, submit: bool) -> str:
        el = self._locator(index)
        el.scroll_into_view_if_needed(timeout=5000)
        el.click(timeout=8000)
        el.fill("")  # clear any existing value
        el.press_sequentially(text, delay=20)
        if submit:
            el.press("Enter")
            self._settle()
            return f'Typed "{text}" into [{index}] and pressed Enter'
        return f'Typed "{text}" into [{index}]'

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
                # WITHOUT clicking them — otherwise the focus-click below would
                # fire a button (e.g. "Place Order"), bypassing the click path's
                # confirmation guard. The model must `click` those separately.
                info = el.evaluate(
                    "e => ({tag: (e.tagName||'').toLowerCase(), "
                    "type: (e.getAttribute && (e.getAttribute('type')||'')).toLowerCase(), "
                    "editable: e.isContentEditable === true})"
                )
                _NONTEXT = ("button", "submit", "reset", "checkbox", "radio", "file", "image")
                fillable = (
                    info["editable"]
                    or info["tag"] == "textarea"
                    or (info["tag"] == "input" and info["type"] not in _NONTEXT)
                )
                if not fillable:
                    kind = info["tag"] or "element"
                    done.append(
                        f"[{idx}] is a <{kind}>, not a text field — skipped (not filled or clicked). "
                        "Use `click` for buttons/links, or `select_option` for dropdowns."
                    )
                    continue
                el.scroll_into_view_if_needed(timeout=5000)
                el.click(timeout=8000)
                el.fill("")
                el.press_sequentially(text, delay=20)
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
        # focus on each keystroke so the subsequent digits land in the right
        # boxes.
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
        text = self.page.evaluate(
            "() => document.body ? document.body.innerText : ''"
        )
        text = re.sub(r"\n{3,}", "\n\n", text or "").strip()
        return text[:8000]

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
        # Detect an ACTIVE human-verification challenge (not a passive invisible
        # reCAPTCHA badge): a Cloudflare/"are you human" interstitial, or a
        # visibly sized captcha-provider iframe the user must interact with.
        for f in self.page.frames:
            # 1) interstitial / challenge text
            try:
                text = f.evaluate(
                    "() => document.body ? document.body.innerText.slice(0, 2000) : ''"
                )
                if self._CAPTCHA_PHRASES.search(text or ""):
                    return True
            except Exception:
                pass  # cross-origin race / detached
            # 2) a visibly sized captcha-provider iframe
            if f is not self.page.main_frame and self._CAPTCHA_PROVIDERS.search(f.url):
                try:
                    el = f.frame_element()
                    box = el.bounding_box()
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
