"""The browser layer: everything that knows Playwright's and Camoufox's shapes.

- `session.py` — `BrowserSession`, the one object the tools act through.
- `aria.py` — parses the `aria_snapshot` text into the nodes the model sees.
- `dom.py` — the few JS snippets that must run inside the page.
- `playwright_patch.py` — a driver bug workaround, applied on launch.

Deliberately no imports here: `from browser_agent.browser import aria` then costs
nothing, so the pure parser stays testable without a browser installed. Callers
name the module they want — `from .browser.session import BrowserSession`.
"""
