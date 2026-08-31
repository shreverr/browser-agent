"""Tools that move around the page: navigation, pointer, scrolling."""

from __future__ import annotations

from typing import Any

from ..browser.session import BrowserSession
from .registry import tool


@tool(
    "navigate",
    "Load a URL directly. Use this SPARINGLY — mainly to open the initial site "
    "(e.g. its homepage) or a URL the user explicitly gave. Prefer clicking "
    "links/buttons and using on-page search to move around within a site; do NOT "
    "guess or hand-construct deep-link URLs.",
    {"url": {"type": "string", "description": "The URL to open"}},
    ["url"],
)
def navigate(browser: BrowserSession, inp: dict[str, Any]) -> str:
    return browser.navigate(str(inp.get("url")))


@tool(
    "click",
    "Click an element by its index.",
    {"index": {"type": "integer", "description": "Element [index] to click"}},
    ["index"],
)
def click(browser: BrowserSession, inp: dict[str, Any]) -> str:
    return browser.click(int(inp["index"]))


@tool(
    "scroll",
    "Scroll the page up or down by roughly one viewport.",
    {"direction": {"type": "string", "enum": ["up", "down"]}},
    ["direction"],
)
def scroll(browser: BrowserSession, inp: dict[str, Any]) -> str:
    return browser.scroll("up" if inp.get("direction") == "up" else "down")


@tool("go_back", "Go back to the previous page.", {})
def go_back(browser: BrowserSession, inp: dict[str, Any]) -> str:
    return browser.back()


@tool(
    "click_at",
    "LAST-RESORT click at pixel coordinates (x, y) in the screenshot. Use ONLY when you can "
    "clearly see a target in the screenshot that has NO [index] in the element list. Prefer "
    "`click` with an [index] whenever the target is listed — it's far more reliable than "
    "guessing coordinates.",
    {
        "x": {"type": "number", "description": "Horizontal pixel from the left edge"},
        "y": {"type": "number", "description": "Vertical pixel from the top edge"},
    },
    ["x", "y"],
)
def click_at(browser: BrowserSession, inp: dict[str, Any]) -> str:
    return browser.click_at(float(inp.get("x", 0)), float(inp.get("y", 0)))
