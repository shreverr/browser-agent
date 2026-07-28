# browser-agent

A general-purpose browser agent. Give it a task in plain English; it drives a
real [Camoufox](https://camoufox.com/) (a hardened, anti-detect Firefox) browser
to get it done. Models are called through [OpenRouter](https://openrouter.ai), so
you can use any model it serves.

It works by looping: read the page's interactive elements (plus a screenshot) →
ask the model (default `anthropic/claude-opus-4.8`) which single action to take →
execute it with Playwright/Camoufox → repeat until the model calls `done`.

It also:
- **Sees the page** — each turn it sends a screenshot alongside the element
  list, so the model can tell which dialog is on top, which fields are required
  or erroring, and read anything the DOM text misses. Vision is **on by default**
  and needs a multimodal `AGENT_MODEL`; set `AGENT_VISION=0` to turn it off.
- **Narrates** a one-liner each step so you can follow along.
- **Asks you** when something's ambiguous or before a consequential/irreversible
  action — just like Claude Code.
- **Sees & handles overlays** — detection pierces shadow DOM and walks every
  iframe; popups/consent walls/modals are flagged and pushed to the top of the
  list with a "dismiss this first" instruction, so they don't get ignored.
- **Works native dropdowns** — `<select>` options are listed and chosen via a
  dedicated `select_option` action (not a flaky click).
- **Doesn't get stuck** — a second *supervisor* model periodically reviews recent
  actions; if the agent is looping or making no progress it injects concrete
  steering plus the page's raw HTML so the agent can break out.
- **Waits on CAPTCHAs** — when a human-verification challenge appears, it pauses
  and asks you to solve it in the (visible) browser window, then resyncs and
  carries on. It won't try to solve captchas itself.
- **Navigates like a human** — prefers links, buttons, and on-page search; it
  only types a URL to open the initial site or one you gave, never guessed
  deep-links.

## Setup

Requires Python 3.10+. Using [uv](https://docs.astral.sh/uv/) (recommended):

```bash
uv sync                          # create venv + install deps
uv run python -m camoufox fetch  # download the Camoufox browser binary
cp .env.example .env             # then add your OPENROUTER_API_KEY
```

Or with plain pip:

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e .
python -m camoufox fetch
cp .env.example .env             # then add your OPENROUTER_API_KEY
```

## Run

**Interactive mode** (Claude Code / opencode style) — just run with no task:

```bash
browser-agent          # or: uv run browser-agent
```

You get a prompt. Type tasks; follow-ups continue in the **same** browser
session with full context.

```
❯ recommend a guitar under 30k
❯ now compare the top 2 on warranty
❯ /exit
```

**One-shot mode** — pass the task as an argument:

```bash
browser-agent "find the top post on Hacker News and summarize it"
```

By default the browser is visible so you can watch. Run headless with
`HEADLESS=true`.

### India by default

Unless you say otherwise, the agent assumes an **Indian context**: prices are in
₹ INR (`30k` = ₹30,000, never USD), shopping defaults to `amazon.in` /
`flipkart.com`, search uses `google.co.in`, and units/timezone are metric / IST.
Name another country, currency, or site explicitly to override it.

## If you're getting blocked

Sites detect automated browsers. Camoufox ships anti-fingerprinting baked into
the Firefox binary (it masks `navigator.webdriver`, headless tells,
WebGL/canvas/font fingerprints, and more) — no add-on stealth layer required. If
a site still blocks you, in order of effectiveness:

1. **Run headed** (the default — don't set `HEADLESS=true`). A visible browser
   is much harder to flag than a headless one.
2. **Solve the check once.** The session (cookies, Cloudflare clearance, logins)
   is saved to `.profile/state.json` and reused on the next run — so clear a
   CAPTCHA or log in manually one time and it carries over.
3. Heavily protected sites (some airlines, ticketing) use commercial bot walls
   that no stealth setup reliably beats. Prefer a site with an official API or a
   lighter anti-bot posture for those.

## How it works

| File                     | Role |
| ------------------------ | ---- |
| `browser_agent/aria.py`    | Parses Playwright's `aria_snapshot(mode="ai")` accessibility tree into the indexed control / heading / status-text nodes the model sees. |
| `browser_agent/dom.py`     | Small in-page JS helpers the snapshot doesn't provide: top-overlay box, viewport size, and a label backfill for controls the tree leaves nameless. |
| `browser_agent/browser.py` | Camoufox/Playwright wrapper: navigate, click, click_at, type, fill_form, scroll, back, read, screenshot. |
| `browser_agent/agent.py`   | The OpenRouter tool-use loop. Defines the tools and drives the conversation. |
| `browser_agent/cli.py`     | Entry point. |
| `browser_agent/memory.py`  | Persistent user-facts store (`.profile/memory.json`). |

Each turn the model sees a compact **semantic outline** built from Playwright's
computed accessibility tree (`aria_snapshot`, which works on Firefox unlike the
browser's native tree). Every actionable node — including custom clickable
`<div>`s that sites wire up with JS (`role: clickable`), which plain
role/selector scans miss — gets a readable label (with a small backfill for the
few the tree leaves nameless) and an `[index]` the model acts on via Playwright
`aria-ref`. Disabled controls are shown greyed so the agent enables them first
instead of dead-clicking. When a target is visible only in the screenshot with
no index, the model can fall back to `click_at` (pixel coordinates). Indices
refresh after every action.

## Tuning

Environment variables (all optional):

- `HEADLESS=true` — hide the browser window.
- `AGENT_VISION=0` — turn off screenshots (vision is on by default). With vision
  on, `AGENT_MODEL` must be multimodal.
- `AGENT_MODEL` — any OpenRouter model slug (default `anthropic/claude-opus-4.8`).
  Browse slugs at <https://openrouter.ai/models>. The model must support tool
  calling (and vision/images while `AGENT_VISION` is on).
- `AGENT_MAX_STEPS` — cap the action loop (default `50`).
- `AGENT_CHECK_EVERY` — how often (in steps) the supervisor reviews progress
  (default `10`). It also runs immediately when a loop is detected heuristically.
- `AGENT_CHECKER_MODEL` — model for the supervisor (default: same as `AGENT_MODEL`).
  Set a cheaper/faster slug here to lower cost, e.g. `google/gemini-2.5-flash`.
- `CAMOUFOX_INSTALL_DIR` — override where the Camoufox binary is stored.
- `AGENT_ALLOWED_DOMAINS` — comma-separated allowlist; navigation off these domains
  is blocked (deterministically, in code). Empty = unrestricted.
- `AGENT_CONFIRM_KEYWORDS` — comma-separated phrases (default covers pay / place order
  / delete / transfer). Clicking or submitting an element whose label matches one
  requires an explicit human "yes" first — enforced in code, not model judgement.

## Efficiency

- **Batched form-filling** — `fill_form` fills many fields in one round-trip instead of
  one `type` per turn (much faster on checkout/sign-up forms).
- **Bounded context** — only the latest page snapshot, HTML dump, and screenshot are kept
  in history; older copies are pruned each turn, so cost stays roughly flat over long tasks.
- **Prefix caching** — the static system prompt is marked cacheable on Anthropic models
  (Google/OpenAI cache implicitly), cutting cost on extended sessions.

## Extending

Add a new capability by adding a method to `BrowserSession`, a tool definition in
`browser_agent/agent.py`, and a branch in the `execute()` dispatch. Good
candidates: file downloads, tab management, or attaching screenshots to give the
model vision alongside the DOM.
# browser-agent
