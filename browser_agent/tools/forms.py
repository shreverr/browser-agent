"""Tools that put text or a choice into a control."""

from __future__ import annotations

from typing import Any

from ..browser.session import BrowserSession
from .registry import tool


@tool(
    "type",
    "Type text into an input/textarea by its index.",
    {
        "index": {"type": "integer", "description": "Element [index] to type into"},
        "text": {"type": "string", "description": "Text to enter"},
        "submit": {"type": "boolean", "description": "Press Enter afterwards (e.g. to run a search)"},
    },
    ["index", "text"],
)
def type_text(browser: BrowserSession, inp: dict[str, Any]) -> str:
    return browser.type(int(inp["index"]), str(inp.get("text")), bool(inp.get("submit")))


@tool(
    "fill_form",
    "Fill MULTIPLE text fields in ONE call — much faster than one `type` per turn. Use this "
    "for any form with 2+ fields (delivery address, sign-up, checkout, etc.). Only include "
    "actual text-entry fields (textboxes) — NOT buttons, links or dropdowns; click the "
    "Save/Submit button separately with `click` afterwards, and use `select_option` for "
    "dropdowns. Set submit=true only on a field that should press Enter (usually none).",
    {
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
    ["fields"],
)
def fill_form(browser: BrowserSession, inp: dict[str, Any]) -> str:
    return browser.fill_form(inp.get("fields") or [])


@tool(
    "type_otp",
    "Fill a split OTP / verification-code input (multiple single-digit boxes). Pass "
    "the index of the FIRST digit box and the complete code string. Do NOT use `type` "
    "for OTP fields — it only fills one box.",
    {
        "index": {"type": "integer", "description": "Index of the first OTP digit input"},
        "code": {"type": "string", "description": 'The full OTP or verification code, e.g. "123456"'},
    },
    ["index", "code"],
)
def type_otp(browser: BrowserSession, inp: dict[str, Any]) -> str:
    return browser.type_otp(int(inp["index"]), str(inp.get("code")))


@tool(
    "select_option",
    "Choose an option in a native dropdown (an element shown as `select`). Pass the "
    "exact option text from its options=[...] list.",
    {
        "index": {"type": "integer", "description": "Element [index] of the select"},
        "value": {"type": "string", "description": "The exact option text to choose"},
    },
    ["index", "value"],
)
def select_option(browser: BrowserSession, inp: dict[str, Any]) -> str:
    return browser.select_option(int(inp["index"]), str(inp.get("value")))
