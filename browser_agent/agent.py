"""The OpenRouter tool-use loop.

A persistent agent: one browser session and one conversation history that
survive across multiple `run()` calls, so follow-up prompts continue in the same
browser with full context (Claude Code / opencode style).

Each step: snapshot the page -> ask the model for exactly one tool call ->
execute it -> feed back the result plus the fresh page. Three guards wrap that
loop: a code-enforced confirmation prompt for consequential clicks, a hard
anti-repeat refusal, and a second "supervisor" model that steers the agent out
of loops. Every string the model reads lives in `prompts.py`.
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable

from openai import OpenAI

from . import prompts
from .browser.session import BrowserSession, PageState
from .config import (
    API_KEY,
    CHECK_EVERY,
    CHECKER_MODEL,
    CONFIRM_KEYWORDS,
    DEBUG_TOKENS,
    MAX_STEPS,
    MODEL,
    VISION,
)
from .memory import format_memory, load_memory, save_memory
from .tools import READ_TOOLS, TOOLS, execute

# Prompts the user mid-task and returns their typed answer.
AskFn = Callable[[str], str]


# --- rendering the page for the model --------------------------------------

# Text that marks an overlay as a *dismissable interruption* (cookie/consent/
# privacy banners, newsletter/promo interstitials) rather than on-task UI like a
# cart drawer, location picker, address list, or product-options sheet.
_DISMISS_HINTS = (
    "cookie", "consent", "gdpr", "we use cookies", "accept all", "accept cookies",
    "manage preferences", "privacy policy", "newsletter", "subscribe",
    "sign up for", "% off your first", "no thanks", "maybe later", "allow all",
)
# Roles that mean "there is something to type or choose here". aria maps every
# text-entry element onto one of these (<textarea> -> textbox, <select> ->
# combobox, <input type=email> -> textbox), so the role alone is enough.
_FILLABLE_ROLES = ("textbox", "searchbox", "combobox")


def _overlay_kind(overlays: list[dict]) -> str:
    """Classify an open overlay: 'form' (has fields to fill), 'dismiss' (a
    cookie/consent/promo blocker), or 'engage' (on-task dialog to interact with —
    the default for modern sites, where cart/location/address/options live in
    modals)."""
    if any(e.get("role") in _FILLABLE_ROLES for e in overlays):
        return "form"
    blob = " ".join(e.get("text", "") for e in overlays).lower()
    if any(h in blob for h in _DISMISS_HINTS):
        return "dismiss"
    return "engage"


_BANNERS = {
    "form": prompts.FORM_BANNER,
    "dismiss": prompts.DISMISS_BANNER,
    "engage": prompts.ENGAGE_BANNER,
}


def render_state(state: PageState) -> str:
    """PageState -> the text block the model reads: URL, title, an overlay banner
    if a dialog is open, then a semantic outline where every control keeps the
    [index] the model acts by."""
    overlays = [e for e in state["elements"] if e.get("overlay")]
    out = f"URL: {state['url']}\nTitle: {state['title']}\n\n"

    if overlays:
        out += _BANNERS[_overlay_kind(overlays)]
        out += "\n".join(f"[{e['index']}] {e['text']}" for e in overlays) + "\n\n"

    # Headings and status text give context; controls carry their index.
    lines = []
    for n in state["nodes"]:
        if n.get("overlay"):
            continue  # shown in the banner above
        if n["kind"] == "heading":
            lines.append(f"# {n.get('name', '')}")
        elif n["kind"] == "text":
            lines.append(f"- {n.get('name', '')}")
        else:
            lines.append(f"[{n['index']}] {n['text']}")
    has_control = any(n["kind"] == "control" and not n.get("overlay") for n in state["nodes"])
    return out + "Page outline:\n" + ("\n".join(lines) if has_control else "(no interactive elements)")


# --- stuck detection -------------------------------------------------------


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


def repeat_guard_step(action_sig, sig, last_action_sig, last_state_sig, recent_sigs, stuck_repeats):
    """Anti-repeat bookkeeping for one browser action (pure, so it's testable).

    `sig` is the page signature AFTER the action. An action is "stuck" if it
    repeats the previous action AND the page either didn't change or bounced back
    to a recently-seen state (an open/close toggle). Returns (stuck, new_count).
    Callers refuse to execute the same action once the count reaches 2."""
    stuck = action_sig == last_action_sig and (sig == last_state_sig or sig in recent_sigs)
    return stuck, (stuck_repeats + 1 if stuck else 0)


def page_signature(state: PageState) -> str:
    """A cheap fingerprint of a page, for spotting "nothing changed"."""
    return f"{state['url']}#{len(state['elements'])}#" + "|".join(
        e["text"] for e in state["elements"][:6]
    )


def supervise(client: OpenAI, task: str, recent_actions: list[str], state_text: str) -> dict[str, Any]:
    """A second model that judges whether the browser agent is stuck/looping and,
    if so, returns concrete steering advice."""
    try:
        r = client.chat.completions.create(
            model=CHECKER_MODEL,
            max_tokens=300,
            messages=[
                {"role": "system", "content": prompts.SUPERVISOR_SYSTEM},
                {
                    "role": "user",
                    "content": prompts.SUPERVISOR_USER.format(
                        task=task, actions="\n".join(recent_actions), state=state_text
                    ),
                },
            ],
        )
        match = re.search(r"\{[\s\S]*\}", r.choices[0].message.content or "")
        if not match:
            return {"looping": False, "advice": ""}
        parsed = json.loads(match.group(0))
        return {"looping": bool(parsed.get("looping")), "advice": str(parsed.get("advice", ""))}
    except Exception:
        return {"looping": False, "advice": ""}


# --- small helpers ---------------------------------------------------------


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


def _has_image(msg: dict) -> bool:
    return isinstance(msg.get("content"), list) and any(
        isinstance(p, dict) and p.get("type") == "image_url" for p in msg["content"]
    )


class Agent:
    def __init__(self) -> None:
        self.browser = BrowserSession()
        self._vision_on = VISION  # may flip off at runtime if the model rejects images
        system = prompts.SYSTEM + ("\n\n" + prompts.VISION_NOTE if VISION else "")
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
            api_key=API_KEY,
            default_headers={
                "HTTP-Referer": "https://github.com/local/browser-agent",
                "X-Title": "browser-agent",
            },
        )

    def start(self, headless: bool) -> None:
        self.browser.launch(headless)
        if self._vision_on:
            print(
                f"👁️  Vision ON — sending a screenshot each turn. "
                f"AGENT_MODEL must be multimodal (current: {MODEL}). Set AGENT_VISION=0 to disable."
            )

    def close(self) -> None:
        self.browser.close()

    # --- model I/O ---------------------------------------------------------

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
        label = prompts.SHOT_LABEL + prompts.SHOT_COORDS.format(
            w=int(vp.get("w", 1280)), h=int(vp.get("h", 900))
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
            if _has_image(m):
                m["content"] = prompts.SHOT_OMITTED

    def _complete(self):
        """One chat completion over the running history, with a graceful text-only
        retry if the model turns out not to accept images."""
        kwargs = dict(
            model=MODEL,
            max_tokens=4000,
            messages=self.messages,
            tools=TOOLS,
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
        """Strip stale page snapshots / HTML dumps / screenshots from history,
        keeping only the most recent of each. Rewrites content in place (roles +
        tool_call_ids are preserved, so the tool-call contract stays intact)."""
        msgs = self.messages

        def text_indices(predicate):
            return [i for i, m in enumerate(msgs) if isinstance(m.get("content"), str) and predicate(m["content"])]

        for i in text_indices(lambda c: prompts.PAGE_MARK in c)[:-1]:
            msgs[i]["content"] = msgs[i]["content"].split(prompts.PAGE_MARK, 1)[0] + prompts.PAGE_NOTE

        def is_html(c):
            return c.startswith(prompts.HTML_PREFIX) or prompts.SUP_MARK in c

        for i in text_indices(is_html)[:-1]:
            c = msgs[i]["content"]
            msgs[i]["content"] = (
                prompts.HTML_PREFIX + prompts.HTML_NOTE
                if c.startswith(prompts.HTML_PREFIX)
                else c.split(prompts.SUP_MARK, 1)[0] + prompts.HTML_NOTE
            )

        # Screenshots are large — keep only the latest image.
        for i in [i for i, m in enumerate(msgs) if _has_image(m)][:-1]:
            msgs[i]["content"] = prompts.SHOT_OMITTED

    # --- per-step pieces of run() -----------------------------------------

    def _pause_for_captcha(self, ask: AskFn) -> PageState:
        """Hand the browser to the human until the challenge is cleared, then
        resync the model on the page it lands on."""
        print("\n🧩 CAPTCHA / human-verification detected. Waiting for you to solve it…")
        answer = ask(prompts.CAPTCHA_PAUSE)
        state = self.browser.get_state()
        self.messages.append(
            {
                "role": "user",
                "content": prompts.CAPTCHA_RESUMED.format(
                    skipped=" (skipped)" if re.search(r"skip", answer, re.I) else "",
                    state=render_state(state),
                ),
            }
        )
        return state

    def _meta_tool_result(self, name: str, inp: dict, ask: AskFn) -> str | None:
        """Handle the tools the loop owns rather than the browser. Returns the
        tool-message content, or None if `name` isn't one of them."""
        if name == "ask_user":
            answer = ask(str(inp.get("question", "(the agent has a question)"))).strip()
            return f"User answered: {answer}" if answer else "User gave no answer."
        if name == "remember":
            key = str(inp.get("key", "")).strip()
            value = str(inp.get("value", "")).strip()
            if key:
                self.memory[key] = value
                save_memory(self.memory)
                print(f"\n💾 Remembered: {key} = {value}")
            return f"Saved: {key} = {value}"
        if name == "forget":
            key = str(inp.get("key", "")).strip()
            if key in self.memory:
                del self.memory[key]
                save_memory(self.memory)
                print(f"\n🗑️  Forgot: {key}")
            return f"Deleted: {key}"
        return None

    @staticmethod
    def _confirm_target(name: str, inp: dict, elements: list) -> str | None:
        """The label of a consequential element this call would activate, if any.
        Code-enforced, not the model's choice — a prompt injection can't skip it."""
        if not CONFIRM_KEYWORDS or name not in ("click", "fill_form"):
            return None
        if name == "click":
            indices = [_as_int(inp.get("index"))]
        else:  # fill_form — only a field that presses Enter can submit
            indices = [_as_int(f.get("index")) for f in inp.get("fields") or [] if f.get("submit")]
        labels = [elements[i]["text"] for i in indices if i is not None and 0 <= i < len(elements)]
        return next((t for t in labels if any(k in t.lower() for k in CONFIRM_KEYWORDS)), None)

    def _maybe_steer(self, task: str, actions: list, state_text: str) -> bool:
        """Ask the supervisor whether the agent is stuck; if so, inject steering
        advice plus the raw HTML. Returns True if it steered."""
        verdict = supervise(self.client, task, actions[-8:], state_text)
        if not (verdict["looping"] and verdict["advice"].strip()):
            return False
        print(f"\n🧭 supervisor: {verdict['advice'].strip()}")
        self.messages.append(
            {
                "role": "user",
                "content": prompts.SUPERVISOR_INJECT.format(
                    advice=verdict["advice"].strip(), html=self.browser.read_html()
                ),
            }
        )
        return True

    # --- the loop ---------------------------------------------------------

    def run(self, task: str, ask: AskFn) -> str:
        """Run one task to completion, keeping the browser + history for follow-ups."""
        state = self.browser.get_state()
        state_text = render_state(state)
        elements = state["elements"]  # for the sensitive-action guard
        confirmed: set[str] = set()  # sensitive actions the user already approved
        mem_text = format_memory(self.memory)
        self.messages.append(
            {
                "role": "user",
                "content": (f"What you know about this user:\n{mem_text}\n\n" if mem_text else "")
                + f"Task: {task}{prompts.PAGE_MARK}{state_text}",
            }
        )

        actions: list[str] = []  # action signatures, for loop heuristics
        state_sigs: list[str] = []  # page signatures after each real action
        last_steer = -99
        last_action_sig = None  # previous browser action, for the anti-repeat guard
        last_state_sig = None
        stuck_repeats = 0  # consecutive futile repeats of the same action

        for step in range(1, MAX_STEPS + 1):
            if self.browser.detect_captcha():
                state = self._pause_for_captcha(ask)
                state_text, elements = render_state(state), state["elements"]

            # ALWAYS send a fresh screenshot each turn (when vision is on),
            # captured now — after the previous turn's actions have fully settled.
            # This is what lets the model see everything, including stacked
            # modals and anything that renders a beat late.
            self._attach_screenshot()
            self._compact_history()

            response = self._complete()
            self._debug_tokens(step, response)

            message = response.choices[0].message if response.choices else None
            if message is None:
                return "(no response from model)"
            self.messages.append(message.model_dump(exclude_none=True))
            if message.content and message.content.strip():
                print(f"\n💭 {message.content.strip()}")

            calls = [c for c in (message.tool_calls or []) if getattr(c, "type", "function") == "function"]
            if not calls:
                # Model ended without a tool call — treat its text as the result.
                return (message.content or "").strip() or "(agent stopped without a final answer)"

            acted = False  # a browser action ran this step (gates the supervisor)

            for call in calls:
                name = call.function.name
                try:
                    inp = json.loads(call.function.arguments) if call.function.arguments else {}
                except Exception:
                    inp = {}
                print(f"\n🔧 {name}({json.dumps(inp)})  [step {step}/{MAX_STEPS}]")

                if name == "done":
                    self.browser.screenshot("run-final.png")
                    self._reply(call, "done")
                    return str(inp.get("answer", "(no answer provided)"))

                meta = self._meta_tool_result(name, inp, ask)
                if meta is not None:
                    self._reply(call, meta)  # no page changed; let the model react
                    continue

                # Consequential click/submit: require an explicit human "yes" once.
                target = self._confirm_target(name, inp, elements)
                if target and f"{name}:{target.lower()}" not in confirmed:
                    print(f"\n🛑 Consequential action detected: {target}")
                    if re.match(r"\s*(y|yes|ok|sure|proceed|confirm|go)\b", ask(prompts.CONFIRM_ASK.format(label=target)), re.I):
                        confirmed.add(f"{name}:{target.lower()}")
                    else:
                        self._reply(call, prompts.CONFIRM_DECLINED.format(label=target))
                        acted = True
                        continue

                action_sig = f"{name} {json.dumps(inp)}"
                # Refuse to run the same futile action yet again — the soft
                # supervisor nudge isn't enough once a model is looping.
                refusing = action_sig == last_action_sig and stuck_repeats >= 2
                if refusing:
                    result = prompts.REPEAT_REFUSAL.format(action=action_sig)
                else:
                    try:
                        result = execute(self.browser, name, inp)
                    except Exception as err:
                        result = f"Action failed: {err}"

                if name in READ_TOOLS:
                    actions.append(name)
                    self._reply(call, result)
                else:
                    state = self.browser.get_state()
                    state_text, elements = render_state(state), state["elements"]
                    sig = page_signature(state)
                    stuck, stuck_repeats = repeat_guard_step(
                        action_sig, sig, last_action_sig, last_state_sig, state_sigs[-5:], stuck_repeats
                    )
                    last_action_sig, last_state_sig = action_sig, sig
                    if stuck and not refusing:
                        result = prompts.STUCK_WARNING + result
                    actions.append(f"{action_sig} @ {state['url']}")
                    state_sigs.append(sig)
                    self._reply(call, f"{result}{prompts.PAGE_MARK}{state_text}")
                acted = True

            if acted:
                due = step % CHECK_EVERY == 0
                suspect = looks_like_loop(actions) or looks_stalled(state_sigs)
                if (due or suspect) and step - last_steer >= 2:
                    if self._maybe_steer(task, actions, state_text):
                        last_steer = step

        return f"Reached the step limit ({MAX_STEPS}) without finishing."

    def _reply(self, call, content: str) -> None:
        """Answer one tool call. Every call MUST get exactly one reply or the next
        request is malformed."""
        self.messages.append({"role": "tool", "tool_call_id": call.id, "content": content})

    @staticmethod
    def _debug_tokens(step: int, response) -> None:
        if not DEBUG_TOKENS:
            return
        u = getattr(response, "usage", None)
        if u:
            print(
                f"   ⚙️  tokens step {step}: prompt={u.prompt_tokens} "
                f"completion={u.completion_tokens} total={u.total_tokens}"
            )
