"""The OpenRouter tool-use loop. Ported from src/agent.ts.

A persistent agent: one browser session and one conversation history that
survive across multiple `run()` calls, so follow-up prompts continue in the same
browser with full context (Claude Code / opencode style).
"""

from __future__ import annotations

import json
import os
import re
from typing import Any, Callable

from openai import OpenAI

from .browser import BrowserSession, PageState
from .memory import format_memory, load_memory, save_memory

MODEL = os.environ.get("AGENT_MODEL") or "deepseek/deepseek-v4-flash"
CHECKER_MODEL = os.environ.get("AGENT_CHECKER_MODEL") or MODEL
MAX_STEPS = int(os.environ.get("AGENT_MAX_STEPS") or 50)
CHECK_EVERY = int(os.environ.get("AGENT_CHECK_EVERY") or 10)
# Vision: send a page screenshot to the model each turn. Needs a multimodal
# AGENT_MODEL. On by default; set AGENT_VISION=0 to disable.
VISION = os.environ.get("AGENT_VISION", "1") != "0"

# Deterministic safety: clicking/submitting an element whose label contains one
# of these phrases requires explicit human confirmation first — enforced in code,
# not left to the model's judgement (prompt injection can't bypass it).
_DEFAULT_CONFIRM = (
    "place order,place your order,pay now,proceed to pay,buy now,complete purchase,"
    "confirm order,confirm & pay,confirm and pay,submit order,delete,transfer"
)
CONFIRM_KEYWORDS = [
    k.strip().lower()
    for k in os.environ.get("AGENT_CONFIRM_KEYWORDS", _DEFAULT_CONFIRM).split(",")
    if k.strip()
]

VISION_NOTE = (
    "You also receive a screenshot of the current page each turn (a user message with the image). "
    "Use it to SEE what the text element-list can't convey: the visual layout, which dialog/popup is "
    "actually on top and blocking, which field is highlighted or required, and any error/validation "
    "messages. Cross-check the screenshot with the numbered element list, but always act by the [index] "
    "from the list."
)

SYSTEM = """You are a browser automation agent. You control a real Firefox browser to accomplish the user's task.

Each turn you receive the current page: its URL, title, and a semantic outline of the interactive elements visible in the viewport, each with a role, a label, and an [index] you act by, like:
  [0] textbox "Search"
  [1] button "Sign in"
  [2] link "Cart (3)"
  [3] clickable "Home • 2.1 km, MG Road"
A role of `clickable` is a custom clickable element (a div/row the site wired up itself, e.g. a saved address or a product card) — treat it EXACTLY like a button and click it by its index. An element marked `(disabled)` is greyed-out and does nothing when clicked: do NOT click it — instead do whatever ENABLES it first (e.g. select a saved-address row or a required option), after which it becomes active.

Act by calling exactly ONE tool per turn. With the tool call, ALWAYS include a brief one-line note (a few words, plain language) of what you're doing right now — e.g. "Opening amazon.in", "Dismissing the cookie banner", "Searching for guitars under ₹30k", "Comparing the top 3". This keeps the user informed.

Guidelines:
- Navigate like a human: use the site's own links, buttons, menus, and search box to get around. Use `navigate` (typing a URL) only to open the initial site or a URL the user gave — do NOT guess deep-link URLs (e.g. don't hand-build a search-results URL; use the search box instead).
- If a CAPTCHA or "verify you are human" check appears, do NOT try to solve it or keep clicking — it is handled for you (the user is asked to solve it). Just continue once the page has moved past it.
- Only elements currently in the viewport are listed. If what you need isn't shown, scroll to reveal more.
- After every action the page list refreshes and indices may change — always read the new list before acting.
- To search or fill a field, use `type` (set submit=true to press Enter).
- OTP / verification codes: when you see a series of inputs marked [OTP-digit], do NOT type each digit one at a time. Use `type_otp` once with the first box's index and the complete code — it fills all boxes in one action.
- For a dropdown shown as `combobox "…" | options=[…]`, use `select_option` with the exact option text from that `options=[...]` list. Do NOT `click`/`type` it. For a custom dropdown with no `options=[...]`, click it to open, then click the option from the refreshed list.
- Use `read_page` when the task needs information from the page body text rather than a control to click.
- If the labeled element list looks incomplete, or you've tried something twice without progress, call `get_html` to inspect the raw page structure and find the right control.
- Coordinate clicking is a LAST RESORT: if — and only if — you can clearly see a target in the screenshot that has NO [index] in the list, use `click_at` with its pixel x,y. Whenever the target IS in the list, click it by [index] instead; never guess coordinates for something that's already listed.
- Filling forms & required info: sites often demand details before checkout — a delivery address, pincode, contact number, etc. For a form with 2+ fields, fill them all in ONE `fill_form` call (list every field) instead of one `type` per turn — it's far faster. Scroll to reveal fields below the fold; for a custom (non-`select`) dropdown, click it to open then click the option from the refreshed list. If a form needs information you don't have and can't get from user memory (e.g. a full delivery address), STOP and use `ask_user` to get it — never invent an address, name, or payment detail — then `remember` what they tell you for next time. If the site shows a validation error ("add an address", "this field is required"), read it and handle that specific field; do NOT simply re-click the button you already pressed.
- Be efficient: each tool call takes a few seconds, so minimise round-trips — batch independent field fills with `fill_form`, and don't re-read the page more than needed.
- A "[SUPERVISOR]" message may appear if you're detected repeating actions without progress. Take it seriously: do NOT repeat your last action — change approach (interact with the open dialog instead of closing it, scroll, pick a different element, read the HTML, go back, or try a different site).
- Dialogs/panels/drawers are NORMAL and usually IMPORTANT — modern sites put the cart, delivery-location picker, address list, product options, and sign-in inside them. When the page state shows a "🪟 DIALOG / PANEL" block, that overlay is the ACTIVE surface: interact with it to make progress (click its buttons, pick an option, fill its fields). Treat it as the main content, NOT as junk to close.
  - Only a "⚠️ COOKIE / CONSENT / PROMO POPUP" block is a genuine blocker to dismiss first (Accept/Reject/Close/✕/No thanks).
  - A "🪟 … form fields" dialog should be filled in and submitted.
- NEVER get into a close/reopen loop. If you dismissed or clicked something and the same dialog is still shown, do NOT click close again — read what it is asking for and act on that: e.g. it says "Add Address to proceed" → click that, then add the address; it's a location picker → set the location (use current location or type the delivery address); it lists saved addresses → click one. Closing a required dialog just blocks your own task.
- When the task is complete, call `done` with a clear answer/summary. If you get stuck, call `done` and explain what blocked you.

Asking the user (like a careful assistant):
- Use `ask_user` when the task is genuinely ambiguous, when you must choose between meaningfully different options the user likely cares about (e.g. which of two products to buy), or before any irreversible/consequential action (placing an order, sending a message, deleting something, anything costing money or requiring login credentials you don't have).
- Do NOT ask about things you can reasonably default (apply the India defaults below) or trivial choices — be decisive there. Ask only when a wrong guess would waste real effort or money.

User memory:
- Facts you have saved about this user are injected at the top of each task message. Use them automatically — don't ask for info you already have.
- Proactively save anything the user shares that would be useful later: phone number, delivery address, UPI ID, name, city, seat/food preferences, etc. Call `remember` as soon as you learn it, even if the user doesn't explicitly ask you to remember it.
- To update a fact, call `remember` again with the same key and the new value. To delete a fact, call `forget`.

Default context — assume INDIA unless the user clearly states otherwise:
- Location: India. Currency: Indian Rupees (₹, INR). A bare amount or "k" means INR — "30k" / "under 30000" = ₹30,000. Never assume or convert to USD.
- Shopping: prefer Indian sites — amazon.in (NOT amazon.com), flipkart.com. Set price filters in INR.
- Search: use Google India (google.co.in) and prefer India-relevant results.
- Use metric units, Indian English, and IST (Asia/Kolkata) for any dates/times.
- "near me", local availability, delivery, and services all mean India.
- If the user explicitly names another country, currency, retailer, or site, follow that instead — explicit beats default."""


tools: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "navigate",
            "description": (
                "Load a URL directly. Use this SPARINGLY — mainly to open the initial site "
                "(e.g. its homepage) or a URL the user explicitly gave. Prefer clicking "
                "links/buttons and using on-page search to move around within a site; do NOT "
                "guess or hand-construct deep-link URLs."
            ),
            "parameters": {
                "type": "object",
                "properties": {"url": {"type": "string", "description": "The URL to open"}},
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "click",
            "description": "Click an element by its index.",
            "parameters": {
                "type": "object",
                "properties": {"index": {"type": "integer", "description": "Element [index] to click"}},
                "required": ["index"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "type",
            "description": "Type text into an input/textarea by its index.",
            "parameters": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer", "description": "Element [index] to type into"},
                    "text": {"type": "string", "description": "Text to enter"},
                    "submit": {"type": "boolean", "description": "Press Enter afterwards (e.g. to run a search)"},
                },
                "required": ["index", "text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fill_form",
            "description": (
                "Fill MULTIPLE text fields in ONE call — much faster than one `type` per turn. Use this "
                "for any form with 2+ fields (delivery address, sign-up, checkout, etc.). Only include "
                "actual text-entry fields (textboxes) — NOT buttons, links or dropdowns; click the "
                "Save/Submit button separately with `click` afterwards, and use `select_option` for "
                "dropdowns. Set submit=true only on a field that should press Enter (usually none)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "fields": {
                        "type": "array",
                        "description": "The fields to fill, in order",
                        "items": {
                            "type": "object",
                            "properties": {
                                "index": {"type": "integer", "description": "Element [index] to fill"},
                                "text": {"type": "string", "description": "Text to enter"},
                                "submit": {"type": "boolean", "description": "Press Enter after this field"},
                            },
                            "required": ["index", "text"],
                        },
                    }
                },
                "required": ["fields"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "type_otp",
            "description": (
                "Fill a split OTP / verification-code input (multiple single-digit boxes). Pass "
                "the index of the FIRST digit box and the complete code string. Do NOT use `type` "
                "for OTP fields — it only fills one box."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer", "description": "Index of the first OTP digit input"},
                    "code": {"type": "string", "description": 'The full OTP or verification code, e.g. "123456"'},
                },
                "required": ["index", "code"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "select_option",
            "description": (
                "Choose an option in a native dropdown (an element shown as `select`). Pass the "
                "exact option text from its options=[...] list."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer", "description": "Element [index] of the select"},
                    "value": {"type": "string", "description": "The exact option text to choose"},
                },
                "required": ["index", "value"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "scroll",
            "description": "Scroll the page up or down by roughly one viewport.",
            "parameters": {
                "type": "object",
                "properties": {"direction": {"type": "string", "enum": ["up", "down"]}},
                "required": ["direction"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "go_back",
            "description": "Go back to the previous page.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "click_at",
            "description": (
                "LAST-RESORT click at pixel coordinates (x, y) in the screenshot. Use ONLY when you can "
                "clearly see a target in the screenshot that has NO [index] in the element list. Prefer "
                "`click` with an [index] whenever the target is listed — it's far more reliable than "
                "guessing coordinates."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "x": {"type": "number", "description": "Horizontal pixel from the left edge"},
                    "y": {"type": "number", "description": "Vertical pixel from the top edge"},
                },
                "required": ["x", "y"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_page",
            "description": "Return the visible text content of the current page for reading/extraction.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_html",
            "description": (
                "Return the page's raw (cleaned) HTML. Use this as a fallback when the labeled "
                "element list seems incomplete or you're stuck and need to see the underlying "
                "structure."
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ask_user",
            "description": (
                "Ask the user a question and wait for their reply. Use when the task is ambiguous, "
                "when a meaningful choice needs their input, or before an irreversible/consequential "
                "action."
            ),
            "parameters": {
                "type": "object",
                "properties": {"question": {"type": "string", "description": "The question to ask the user"}},
                "required": ["question"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "remember",
            "description": (
                "Permanently save a fact about this user for future sessions. Use a short "
                "snake_case key (e.g. 'phone', 'delivery_address', 'upi_id', 'preferred_seat'). "
                "Call this proactively whenever the user shares personal details or preferences."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "Short label for the fact, e.g. 'phone'"},
                    "value": {"type": "string", "description": "The value to store"},
                },
                "required": ["key", "value"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "forget",
            "description": "Remove a previously saved user fact from memory.",
            "parameters": {
                "type": "object",
                "properties": {"key": {"type": "string", "description": "The key to delete"}},
                "required": ["key"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "done",
            "description": "Finish the task and return the final answer or summary to the user.",
            "parameters": {
                "type": "object",
                "properties": {"answer": {"type": "string", "description": "The final answer or summary"}},
                "required": ["answer"],
            },
        },
    },
]


# Text-entry input types that mark an overlay as a form to fill (vs. a
# cookie/consent/promo popup, which is just buttons/links to dismiss).
_FILLABLE_INPUT_TYPES = ("text", "email", "tel", "number", "password", "search", "url", "date")


def _is_fillable(e: dict) -> bool:
    if e.get("role") in ("textbox", "searchbox", "combobox"):
        return True
    if e.get("tag") in ("textarea", "select"):
        return True
    if e.get("tag") == "input":
        return e.get("type", "") in _FILLABLE_INPUT_TYPES
    return False


# Text that marks an overlay as a *dismissable interruption* (cookie/consent/
# privacy banners, newsletter/promo interstitials) rather than on-task UI like a
# cart drawer, location picker, address list, or product-options sheet.
_DISMISS_HINTS = (
    "cookie", "consent", "gdpr", "we use cookies", "accept all", "accept cookies",
    "manage preferences", "privacy policy", "newsletter", "subscribe",
    "sign up for", "% off your first", "no thanks", "maybe later", "allow all",
)


def _overlay_kind(overlays: list[dict]) -> str:
    """Classify an open overlay: 'form' (has fields to fill), 'dismiss' (a
    cookie/consent/promo blocker), or 'engage' (on-task dialog to interact with —
    the default for modern sites, where cart/location/address/options live in
    modals)."""
    if any(_is_fillable(e) for e in overlays):
        return "form"
    blob = " ".join(e.get("text", "") for e in overlays).lower()
    if any(h in blob for h in _DISMISS_HINTS):
        return "dismiss"
    return "engage"


def render_state(state: PageState) -> str:
    elements = state["elements"]
    # Fall back to a controls-only node stream if an older caller built a
    # PageState without `nodes`.
    nodes = state.get("nodes") or [dict(e, kind="control") for e in elements]
    overlays = [e for e in elements if e.get("overlay")]
    out = f"URL: {state['url']}\nTitle: {state['title']}\n\n"

    if overlays:
        kind = _overlay_kind(overlays)
        if kind == "form":
            out += (
                "🪟 A DIALOG / PANEL with form fields is open. If it's part of your task (sign-in, "
                "delivery address, payment, location), FILL IT IN — type into the fields and submit; do NOT "
                "close it. If a field needs info you don't have, use ask_user. Only dismiss it if it's an "
                "unrelated interruption (newsletter/promo):\n"
            )
        elif kind == "dismiss":
            out += (
                "⚠️ A COOKIE / CONSENT / PROMO POPUP is blocking the page. Dismiss it FIRST — accept, "
                "close, or reject it (Accept/Agree/Close/✕/No thanks) before doing anything else:\n"
            )
        else:  # engage
            out += (
                "🪟 A DIALOG / PANEL is open (e.g. cart, delivery-location picker, address list, product "
                "options). This is the ACTIVE part of the page and almost certainly holds what your task "
                "needs — INTERACT with it: click its buttons/options/links to move forward (e.g. 'Add "
                "Address', 'Select location', 'Proceed', a saved address, a quantity, an item). Do NOT "
                "reflexively close it; only click ✕/close if it is clearly unrelated to your task:\n"
            )
        out += "\n".join(f"[{e['index']}] {e['text']}" for e in overlays) + "\n\n"

    # Main page as an indented semantic outline: headings and status text give
    # context; form controls are grouped under their form label; each control
    # keeps its [index] so the model acts by index as before.
    out += "Page outline:\n"
    lines: list[str] = []
    cur_group: str | None = None
    has_control = False
    for n in nodes:
        if n.get("overlay"):
            continue  # shown in the banner above
        kind = n.get("kind")
        if kind == "heading":
            cur_group = None
            lines.append(f"# {n.get('name', '')}")
        elif kind == "text":
            cur_group = None
            lines.append(f"- {n.get('name', '')}")
        else:  # control
            has_control = True
            group = n.get("group") or ""
            if group != cur_group:
                cur_group = group
                if group:
                    lines.append(f'{group} (form):')
            indent = "  " if cur_group else ""
            lines.append(f"{indent}[{n['index']}] {n['text']}")
    out += "\n".join(lines) if has_control else "(no interactive elements)"
    return out


def execute(browser: BrowserSession, name: str, inp: dict[str, Any]) -> str:
    if name == "navigate":
        return browser.navigate(str(inp.get("url")))
    if name == "click":
        return browser.click(int(inp["index"]))
    if name == "type":
        return browser.type(int(inp["index"]), str(inp.get("text")), bool(inp.get("submit")))
    if name == "fill_form":
        return browser.fill_form(inp.get("fields") or [])
    if name == "type_otp":
        return browser.type_otp(int(inp["index"]), str(inp.get("code")))
    if name == "select_option":
        return browser.select_option(int(inp["index"]), str(inp.get("value")))
    if name == "click_at":
        return browser.click_at(float(inp.get("x", 0)), float(inp.get("y", 0)))
    if name == "scroll":
        return browser.scroll("up" if inp.get("direction") == "up" else "down")
    if name == "go_back":
        return browser.back()
    if name == "read_page":
        return "Page text:\n" + browser.read_page_text()
    if name == "get_html":
        return "Page HTML:\n" + browser.read_html()
    return f"Unknown tool: {name}"


# Prompts the user mid-task and returns their typed answer.
AskFn = Callable[[str], str]


def looks_like_loop(actions: list[str]) -> bool:
    """Cheap, free loop heuristics over recent action signatures."""
    if len(actions) >= 3 and all(a == actions[-1] for a in actions[-3:]):
        return True  # same action 3x in a row
    if len(actions) >= 4:
        a, b, c, d = actions[-4:]
        if a == c and b == d and a != b:
            return True  # A-B-A-B oscillation
    return False


def looks_stalled(state_sigs: list[str]) -> bool:
    """Page hasn't changed across the last few steps despite taking actions."""
    if len(state_sigs) < 4:
        return False
    last4 = state_sigs[-4:]
    return all(s == last4[0] for s in last4)


def _looks_like_vision_error(err: Exception) -> bool:
    """Heuristic: did the API reject the request because of image input (model
    isn't multimodal)? Kept loose so we degrade gracefully rather than crash."""
    s = str(err).lower()
    return any(k in s for k in ("image", "vision", "multimodal", "modalit", "image_url"))


def _is_anthropic(model: str) -> bool:
    m = (model or "").lower()
    return "anthropic/" in m or "claude" in m


def _as_int(v):
    try:
        return int(v)
    except Exception:
        return None


def _repeat_guard_step(action_sig, sig, last_action_sig, last_state_sig, recent_sigs, stuck_repeats):
    """Anti-repeat bookkeeping for one browser action (pure, so it's testable).

    `sig` is the page signature AFTER the action. An action is "stuck" if it
    repeats the previous action AND the page either didn't change or bounced back
    to a recently-seen state (an open/close toggle). Returns (stuck, new_count).
    Callers refuse to execute the same action once the count reaches 2."""
    stuck = action_sig == last_action_sig and (sig == last_state_sig or sig in recent_sigs)
    return stuck, (stuck_repeats + 1 if stuck else 0)


def supervise(
    client: OpenAI, task: str, recent_actions: list[str], state_text: str
) -> dict[str, Any]:
    """A second model that judges whether the browser agent is stuck/looping and,
    if so, returns concrete steering advice."""
    system = (
        "You supervise a browser agent. Given the task, its recent actions, and the current page, "
        "decide if it is stuck in a loop or making no real progress. Reply with ONLY compact JSON: "
        '{"looping": boolean, "advice": "one or two sentences of concrete steering, else empty"}. '
        "Note: dialogs/panels (cart, delivery-location picker, address form, product options) are usually "
        "REQUIRED UI, not junk — if the agent is opening/closing or repeatedly dismissing the same dialog, "
        "tell it to STOP closing it and instead interact with it (click the action it names like 'Add "
        "Address' / 'Select location', pick a saved address, or fill the fields). Other good advice: scroll, "
        "try a different element, call get_html to see raw structure, go back, or try a different site/URL."
    )
    user = (
        f"Task: {task}\n\nRecent actions (oldest first):\n{chr(10).join(recent_actions)}\n\n"
        f"Current page:\n{state_text}\n\nIs it looping or stuck, and what should it do differently?"
    )

    try:
        r = client.chat.completions.create(
            model=CHECKER_MODEL,
            max_tokens=300,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        raw = r.choices[0].message.content or ""
        match = re.search(r"\{[\s\S]*\}", raw)
        if not match:
            return {"looping": False, "advice": ""}
        parsed = json.loads(match.group(0))
        return {"looping": bool(parsed.get("looping")), "advice": str(parsed.get("advice", ""))}
    except Exception:
        return {"looping": False, "advice": ""}


# --- history compaction (token optimization) -------------------------------
# Every turn re-sends the whole message history, and each action appends a full
# page snapshot (element list) plus occasional raw-HTML dumps. Old copies are
# pure redundancy — the model is told to always act on the *current* page — yet
# they get re-billed on every subsequent request, so context (and cost) grows
# every step. We keep only the LATEST page snapshot and the LATEST HTML dump in
# full and rewrite the stale ones to short markers. This keeps per-request
# context roughly flat without changing what the model perceives this turn.
_PAGE_MARK = "\n\nCurrent page:\n"
_PAGE_NOTE = "\n\n[earlier page state omitted — act on the current page shown below]"
_HTML_PREFIX = "Page HTML:\n"
_SUP_MARK = "may have missed:\n\n"
_HTML_NOTE = "[outdated HTML omitted — call get_html again if you need it]"
_SHOT_LABEL = (
    "📸 Screenshot of the current page — use it with the element list above: it shows the visual "
    "layout, which popup/dialog is on top, which fields are required or show errors, and anything the "
    "text list misses. Still act by the [index] numbers from the list."
)
_SHOT_OMITTED = "[previous screenshot omitted — see the latest screenshot below]"


class Agent:
    def __init__(self) -> None:
        self.browser = BrowserSession()
        self._vision_on = VISION  # may flip off at runtime if the model rejects images
        system = SYSTEM + ("\n\n" + VISION_NOTE if VISION else "")
        # Prefix caching: the system prompt is a large static prefix. On Anthropic
        # models (via OpenRouter) mark it cacheable; Google/OpenAI cache implicitly
        # so a plain string is fine there.
        sys_content: Any = system
        if _is_anthropic(MODEL):
            sys_content = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
        self.messages: list[dict[str, Any]] = [{"role": "system", "content": sys_content}]
        self.memory = load_memory()
        self.client = OpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=os.environ.get("OPENROUTER_API_KEY"),
            default_headers={
                "HTTP-Referer": "https://github.com/local/browser-agent",
                "X-Title": "browser-agent",
            },
        )

    def start(self, headless: bool) -> None:
        self.browser.launch(headless)
        if VISION:
            print(
                f"👁️  Vision ON — sending a screenshot each turn. "
                f"AGENT_MODEL must be multimodal (current: {MODEL}). Set AGENT_VISION=0 to disable."
            )

    def close(self) -> None:
        self.browser.close()

    def _attach_screenshot(self) -> None:
        """Append a user message carrying a screenshot of the current page, so a
        multimodal model can see it alongside the text element list. No-op if
        vision is off or the capture fails. Old screenshots are pruned to the
        latest by _compact_history so image tokens stay bounded."""
        if not self._vision_on:
            return
        shot = self.browser.screenshot_b64()
        if not shot:
            return
        vp = self.browser.viewport()
        label = _SHOT_LABEL + (
            f" The image is {int(vp.get('w', 1280))}×{int(vp.get('h', 900))} px; if you use "
            "click_at, x goes 0→right and y goes 0→bottom in that space."
        )
        self.messages.append(
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": label},
                    {"type": "image_url", "image_url": {"url": shot}},
                ],
            }
        )

    def _disable_vision(self) -> None:
        """Turn vision off mid-run and strip existing image messages (used when
        the model rejects image input)."""
        self._vision_on = False
        for m in self.messages:
            if isinstance(m.get("content"), list) and any(
                isinstance(p, dict) and p.get("type") == "image_url" for p in m["content"]
            ):
                m["content"] = _SHOT_OMITTED

    def _complete(self):
        """One chat completion over the running history, with a graceful text-only
        retry if the model turns out not to accept images."""
        kwargs = dict(
            model=MODEL,
            max_tokens=4000,
            messages=self.messages,
            tools=tools,
            tool_choice="auto",
            parallel_tool_calls=False,
        )
        try:
            return self.client.chat.completions.create(**kwargs)
        except Exception as err:
            if self._vision_on and _looks_like_vision_error(err):
                print(
                    "\n⚠️  The model rejected image input — disabling vision and retrying text-only. "
                    "Use a multimodal AGENT_MODEL to enable vision."
                )
                self._disable_vision()
                return self.client.chat.completions.create(**kwargs)
            raise

    def _compact_history(self) -> None:
        """Strip stale page snapshots / HTML dumps from history, keeping only the
        most recent of each. Rewrites content in place (roles + tool_call_ids are
        preserved, so the tool-call contract stays intact)."""
        msgs = self.messages
        pages = [
            i for i, m in enumerate(msgs)
            if isinstance(m.get("content"), str) and _PAGE_MARK in m["content"]
        ]
        for i in pages[:-1]:  # every page snapshot except the latest
            msgs[i]["content"] = msgs[i]["content"].split(_PAGE_MARK, 1)[0] + _PAGE_NOTE
        htmls = [
            i for i, m in enumerate(msgs)
            if isinstance(m.get("content"), str)
            and (m["content"].startswith(_HTML_PREFIX) or _SUP_MARK in m["content"])
        ]
        for i in htmls[:-1]:  # every raw-HTML dump except the latest
            c = msgs[i]["content"]
            msgs[i]["content"] = (
                _HTML_PREFIX + _HTML_NOTE
                if c.startswith(_HTML_PREFIX)
                else c.split(_SUP_MARK, 1)[0] + _HTML_NOTE
            )
        # Screenshots are large — keep only the latest image; replace older
        # screenshot messages with a tiny text marker.
        shots = [
            i for i, m in enumerate(msgs)
            if isinstance(m.get("content"), list)
            and any(isinstance(p, dict) and p.get("type") == "image_url" for p in m["content"])
        ]
        for i in shots[:-1]:
            msgs[i]["content"] = _SHOT_OMITTED

    def run(self, task: str, ask: AskFn) -> str:
        """Run one task to completion, keeping the browser + history for follow-ups."""
        initial = self.browser.get_state()
        latest_state_text = render_state(initial)
        latest_elements = initial["elements"]  # for the sensitive-action guard
        confirmed_actions: set[str] = set()
        mem_text = format_memory(self.memory)
        self.messages.append(
            {
                "role": "user",
                "content": (
                    (f"What you know about this user:\n{mem_text}\n\n" if mem_text else "")
                    + f"Task: {task}{_PAGE_MARK}{latest_state_text}"
                ),
            }
        )

        actions: list[str] = []  # action signatures, for loop heuristics
        state_sigs: list[str] = []  # page signatures after each real action
        last_steer = -99
        last_action_sig = None  # previous browser action, for the anti-repeat guard
        last_state_sig = None
        stuck_repeats = 0  # consecutive futile repeats of the same action

        for step in range(1, MAX_STEPS + 1):
            # Pause for a human if a CAPTCHA / verification challenge is up.
            if self.browser.detect_captcha():
                print("\n🧩 CAPTCHA / human-verification detected. Waiting for you to solve it…")
                ans = ask(
                    "Solve the verification in the browser window, then press Enter to continue (or type 'skip')."
                )
                fresh = self.browser.get_state()
                latest_state_text = render_state(fresh)
                latest_elements = fresh["elements"]
                skipped = " (skipped)" if re.search(r"skip", ans, re.I) else ""
                self.messages.append(
                    {
                        "role": "user",
                        "content": (
                            f"[SYSTEM] The user has handled the CAPTCHA / verification{skipped}. "
                            "Continue the task — do not try to solve captchas yourself. "
                            f"Current page:\n{latest_state_text}"
                        ),
                    }
                )

            # ALWAYS send a fresh screenshot of the current page each turn (when
            # vision is on), captured now — after the previous turn's actions have
            # fully settled. This is what lets the model see everything, including
            # stacked / "double" modals and anything that renders a beat late; not
            # just on turns where an action changed the DOM.
            self._attach_screenshot()

            # Drop stale page snapshots / HTML dumps so we don't re-send them
            # (keeps only the latest screenshot too, so image tokens stay bounded).
            self._compact_history()

            response = self._complete()

            if os.environ.get("AGENT_DEBUG_TOKENS"):
                u = getattr(response, "usage", None)
                if u:
                    print(
                        f"   ⚙️  tokens step {step}: prompt={u.prompt_tokens} "
                        f"completion={u.completion_tokens} total={u.total_tokens}"
                    )

            message = response.choices[0].message if response.choices else None
            if message is None:
                return "(no response from model)"
            self.messages.append(message.model_dump(exclude_none=True))

            if message.content and message.content.strip():
                print(f"\n💭 {message.content.strip()}")

            calls = message.tool_calls or []
            if len(calls) == 0:
                # Model ended without a tool call — treat its text as the result.
                return (message.content or "").strip() or "(agent stopped without a final answer)"

            did_tool = False

            for call in calls:
                if getattr(call, "type", "function") != "function":
                    continue
                try:
                    inp = json.loads(call.function.arguments) if call.function.arguments else {}
                except Exception:
                    inp = {}
                print(f"\n🔧 {call.function.name}({json.dumps(inp)})  [step {step}/{MAX_STEPS}]")

                if call.function.name == "done":
                    self.browser.screenshot("run-final.png")
                    self.messages.append({"role": "tool", "tool_call_id": call.id, "content": "done"})
                    return str(inp.get("answer", "(no answer provided)"))

                if call.function.name == "ask_user":
                    answer = ask(str(inp.get("question", "(the agent has a question)")))
                    self.messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.id,
                            "content": f"User answered: {answer.strip()}" if answer.strip() else "User gave no answer.",
                        }
                    )
                    continue  # no page changed; let the model react to the answer

                if call.function.name == "remember":
                    key = str(inp.get("key", "")).strip()
                    value = str(inp.get("value", "")).strip()
                    if key:
                        self.memory[key] = value
                        save_memory(self.memory)
                        print(f"\n💾 Remembered: {key} = {value}")
                    self.messages.append(
                        {"role": "tool", "tool_call_id": call.id, "content": f"Saved: {key} = {value}"}
                    )
                    continue

                if call.function.name == "forget":
                    key = str(inp.get("key", "")).strip()
                    if key and key in self.memory:
                        del self.memory[key]
                        save_memory(self.memory)
                        print(f"\n🗑️  Forgot: {key}")
                    self.messages.append(
                        {"role": "tool", "tool_call_id": call.id, "content": f"Deleted: {key}"}
                    )
                    continue

                # --- deterministic sensitive-action confirmation (code-enforced,
                # not the model's choice — a prompt injection can't skip it) ---
                if CONFIRM_KEYWORDS and call.function.name in ("click", "fill_form"):
                    targets = []
                    if call.function.name == "click":
                        ci = _as_int(inp.get("index"))
                        if ci is not None and 0 <= ci < len(latest_elements):
                            targets.append(latest_elements[ci]["text"])
                    else:  # fill_form — only a field that presses Enter can submit
                        for fld in inp.get("fields") or []:
                            if fld.get("submit"):
                                fi = _as_int(fld.get("index"))
                                if fi is not None and 0 <= fi < len(latest_elements):
                                    targets.append(latest_elements[fi]["text"])
                    hit = next(
                        (t for t in targets if any(k in t.lower() for k in CONFIRM_KEYWORDS)), None
                    )
                    if hit and f"{call.function.name}:{hit.lower()}" not in confirmed_actions:
                        print(f"\n🛑 Consequential action detected: {hit}")
                        ans = ask(
                            f'⚠️ About to do something consequential: "{hit}". '
                            'Type "yes" to proceed, anything else to skip.'
                        )
                        if re.match(r"\s*(y|yes|ok|sure|proceed|confirm|go)\b", ans, re.I):
                            confirmed_actions.add(f"{call.function.name}:{hit.lower()}")
                        else:
                            self.messages.append(
                                {
                                    "role": "tool",
                                    "tool_call_id": call.id,
                                    "content": (
                                        f'The user DECLINED this action ("{hit}"). Do NOT perform it. '
                                        "Ask the user how they want to proceed, or choose a different action."
                                    ),
                                }
                            )
                            did_tool = True
                            continue

                # --- browser action, with a hard anti-repeat guard ---
                action_sig = f"{call.function.name} {json.dumps(inp)}"
                if action_sig == last_action_sig and stuck_repeats >= 2:
                    # Refuse to run the same futile action yet again — the soft
                    # supervisor nudge isn't enough once a model is looping, so
                    # give it firm in-band feedback and force a different move.
                    result = (
                        f"⚠️ Not repeating `{action_sig}` — you've done this exact action several times "
                        "and the page keeps ending up the same. It is NOT working. Do something DIFFERENT "
                        "now: pick another element from the list, scroll, or call get_html to find the right "
                        "control. If a dialog / location picker is open, act on what it asks for (set the "
                        "location, click a saved address, fill the fields) — do not toggle it open/closed."
                    )
                    executed = False
                else:
                    try:
                        result = execute(self.browser, call.function.name, inp)
                    except Exception as err:
                        result = f"Action failed: {err}"
                    executed = True

                is_read = call.function.name in ("read_page", "get_html")
                content = result
                if not is_read:
                    state = self.browser.get_state()
                    latest_state_text = render_state(state)
                    latest_elements = state["elements"]
                    sig = (
                        f"{state['url']}#{len(state['elements'])}#"
                        + "|".join(e["text"] for e in state["elements"][:6])
                    )
                    # "Stuck" = same action as last time that changed nothing OR
                    # bounced back to a recently-seen state (an open/close toggle).
                    stuck, stuck_repeats = _repeat_guard_step(
                        action_sig, sig, last_action_sig, last_state_sig, state_sigs[-5:], stuck_repeats
                    )
                    last_action_sig, last_state_sig = action_sig, sig
                    if executed and stuck:
                        result = (
                            "⚠️ That action did not move things forward (the page is unchanged, or back "
                            "to a state you already saw). Do NOT repeat it — try a different element or "
                            "approach.\n" + result
                        )
                    content = f"{result}{_PAGE_MARK}{latest_state_text}"
                    actions.append(f"{action_sig} @ {state['url']}")
                    state_sigs.append(sig)
                else:
                    actions.append(call.function.name)
                self.messages.append({"role": "tool", "tool_call_id": call.id, "content": content})
                did_tool = True

            # --- supervisor / anti-loop check ---
            if did_tool:
                due = step % CHECK_EVERY == 0
                suspect = looks_like_loop(actions) or looks_stalled(state_sigs)
                if (due or suspect) and step - last_steer >= 2:
                    verdict = supervise(self.client, task, actions[-8:], latest_state_text)
                    if verdict["looping"] and verdict["advice"].strip():
                        last_steer = step
                        print(f"\n🧭 supervisor: {verdict['advice'].strip()}")
                        html = self.browser.read_html()
                        self.messages.append(
                            {
                                "role": "user",
                                "content": (
                                    "[SUPERVISOR] You appear to be stuck or repeating actions without progress. "
                                    f"{verdict['advice'].strip()}\n\nStop and try a genuinely different approach. "
                                    "Below is the page's raw HTML — use it to find controls or links the element "
                                    f"list may have missed:\n\n{html}"
                                ),
                            }
                        )

        return f"Reached the step limit ({MAX_STEPS}) without finishing."
