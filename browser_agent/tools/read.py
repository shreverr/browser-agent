"""Tools that only read the page.

All `read_only=True`: they can't change anything, so the agent loop skips the
state refresh and the anti-repeat bookkeeping after them.
"""

from __future__ import annotations

from typing import Any

from ..browser.session import BrowserSession
from .registry import tool


@tool(
    "read_page",
    "Return the visible text content of the current page for reading/extraction.",
    {},
    read_only=True,
)
def read_page(browser: BrowserSession, inp: dict[str, Any]) -> str:
    return "Page text:\n" + browser.read_page_text()


@tool(
    "get_html",
    "Return the page's raw (cleaned) HTML. Use this as a fallback when the labeled "
    "element list seems incomplete or you're stuck and need to see the underlying "
    "structure.",
    {},
    read_only=True,
)
def get_html(browser: BrowserSession, inp: dict[str, Any]) -> str:
    return "Page HTML:\n" + browser.read_html()
