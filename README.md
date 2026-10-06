# browser-agent

Async browser agent for installed Chrome or Chromium. Give it task in plain
English; it observes one active tab through Chromium DevTools Protocol (CDP),
asks an OpenRouter model for one tool action, executes action, then repeats
until model calls `done`.

Browser session owns one Chrome process and one isolated writable profile.
Interactive tasks share that session. Semantic controls use observation-scoped
target handles, so stale page references fail instead of acting on different
control.

## Requirements

- Python 3.11 or newer
- `uv` (recommended) or `pip`
- Installed Chrome or Chromium; browser binaries are never downloaded
- OpenRouter API key

## Setup

```bash
uv sync
cp .env.example .env
# Add OPENROUTER_API_KEY to .env
```

Without `uv`:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e .
cp .env.example .env
```

## Run

Interactive mode keeps one browser session across prompts:

```bash
uv run browser-agent
```

One-shot mode owns one browser session for one task:

```bash
uv run browser-agent "find top post on Hacker News and summarize it"
```

Browser is headed by default. Set `HEADLESS=true` for headless operation. Set
`AGENT_START_URL` to change the startup page (default `https://www.google.com`;
empty disables startup navigation). Web searches use Google's page search box.
Set
`AGENT_BROWSER_EXECUTABLE` when automatic installed-browser discovery should not
choose browser executable. See [.env.example](.env.example) and
[operator guide](docs/operator.md) for full configuration and failure behavior.

For a local setup with Fortress and CloakBrowser installed, switch browsers with
`python3 scripts/switch_browser.py cloakbrowser` or
`python3 scripts/switch_browser.py fortress`, then restart the agent. The switcher
reads local installation paths from `.profile/browser-switch/settings.json` and
selects separate state stores so profiles from different Chromium versions do
not mix. CloakBrowser uses its upstream stealth launch arguments through a local
launcher; its `humanize=True` mouse behavior is not part of the nodriver
integration. Browser downloads and launcher setup remain local and ignored by Git.

## Behavior

- Semantic observations include bounded context, indexed controls, frame
  breadcrumbs, unsupported-region reasons, and screenshot metadata.
- Every control index resolves to target handle qualified by observation.
  Obtaining newer observation invalidates older handles.
- Browser actions serialize. Mutations never retry after dispatch. Unconfirmed
  mutation returns `uncertain`.
- New action-created child tab becomes active only when ownership is
  unambiguous.
- Verification challenges pause for human intervention. Task may later finish,
  but automation outcome remains failed.
- Episode profiles become restorable checkpoints only after verified browser
  shutdown. Checkpoints are encrypted and immutable.
- Action evidence is redacted before content-addressed persistence. Secret-like
  field values, authorization headers, cookies, and sensitive query parameters
  are not stored in logical artifacts.

## Project layout

```text
browser_agent/
  cli.py                    async interactive and one-shot entry point
  agent.py                  model/tool loop and workflow guards
  config.py                 harness environment translation
  tools/                    model-facing tool schemas
  browser/
    interface.py            public BrowserSession protocol
    models.py               observations, actions, outcomes, errors, config
    nodriver_session.py     lifecycle and state-adapter orchestration
    nodriver_runtime.py     installed-browser launch and CDP ownership
    nodriver_semantics.py   bounded semantic observations
    nodriver_actions.py     native CDP actions
    nodriver_frames.py      frame and owning-session routing
    transport.py            internal CDP seam
  state/                    profiles, checkpoints, redaction, artifacts
tests/
```

Imports point `cli -> agent -> tools -> browser`; browser and state contracts do
not import agent code. External callers depend on `browser_agent.browser`
exports, not transport details.

## Configuration

Core browser code accepts immutable `BrowserConfig`; it does not read environment
variables. CLI translates these variables:

- `OPENROUTER_API_KEY`: required model API key.
- `HEADLESS=true`: run browser without visible window.
- `AGENT_BROWSER_EXECUTABLE`: explicit Chrome/Chromium executable.
- `AGENT_VISION=0`: disable model screenshot input.
- `AGENT_MODEL`: OpenRouter tool-capable model slug.
- `AGENT_CHECKER_MODEL`: optional supervisor model slug.
- `AGENT_MAX_STEPS`: task action limit, default `50`.
- `AGENT_CHECK_EVERY`: supervisor interval, default `10`.
- `AGENT_ALLOWED_DOMAINS`: comma-separated navigation allowlist.
- `AGENT_CONFIRM_KEYWORDS`: labels requiring human confirmation.
- `AGENT_STATE_ROOT`: local artifact and checkpoint root.
- `AGENT_STATE_ENCRYPTION_KEY`: base64-encoded 32-byte checkpoint key.
- `AGENT_MEMORY_FILE`: persistent user-fact file.

Host locale, timezone, geolocation, and permissions remain unchanged unless
harness explicitly supplies overrides. Agent has no hard-coded regional context.

## Development

```bash
uv lock --check
uv run ruff format --check browser_agent tests
uv run ruff check browser_agent tests
uv run pyright
uv run pytest tests/test_models.py tests/test_browser_contract.py tests/test_state_contract.py
uv run pytest
uv run python scripts/cutover_audit.py
```

Real-browser tests use installed Chrome/Chromium and skip only when none exists.
CI matrix and release acceptance requirements live in
[cutover evidence](docs/cutover.md). Contract traceability lives in
[requirements matrix](docs/requirements-matrix.md).

## More documentation

- [Architecture](docs/architecture.md)
- [Operator guide](docs/operator.md)
- [Checkpoint contract](docs/checkpoints.md)
- [Known incompatibilities](docs/incompatibilities.md)
- [Rollback](docs/rollback.md)
- [Cutover and release evidence](docs/cutover.md)
