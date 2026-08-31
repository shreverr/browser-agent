"""Self-check for the agent loop's pure logic: page rendering, stuck detection,
and history compaction. No browser and no network — everything here is a pure
function or a method that only touches `self.messages`.

Run with the project venv (it imports the package, which needs openai/camoufox):

    .venv/bin/python tests/test_agent.py
"""

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from browser_agent import prompts  # noqa: E402
from browser_agent.agent import (  # noqa: E402
    Agent,
    looks_like_loop,
    looks_stalled,
    page_signature,
    render_state,
    repeat_guard_step,
)


def _state(controls, headings=(), url="https://example.com"):
    """Build a PageState the way BrowserSession.get_state does — `nodes` holds the
    same dicts as `elements`, so both views see one index."""
    elements = [
        {"kind": "control", "index": i, "role": role, "name": name, "text": text, "overlay": overlay}
        for i, (role, name, text, overlay) in enumerate(controls)
    ]
    nodes = [{"kind": "heading", "name": h, "overlay": False} for h in headings] + elements
    return {"url": url, "title": "T", "elements": elements, "nodes": nodes}


def test_render_state():
    # A dialog holding a text field is a form to fill.
    form = _state([("textbox", "Pincode", 'textbox "Pincode"', True)])
    assert prompts.FORM_BANNER in render_state(form)

    # Buttons plus cookie wording is a blocker to dismiss.
    cookie = _state([("button", "Accept all", 'button "Accept all cookies"', True)])
    assert prompts.DISMISS_BANNER in render_state(cookie)

    # Any other dialog is on-task UI to engage with — the modern-site default.
    cart = _state([("button", "Proceed", 'button "Proceed"', True)])
    assert prompts.ENGAGE_BANNER in render_state(cart)

    # Non-overlay controls render in the outline with their index; overlay
    # controls appear only in the banner, never twice.
    mixed = _state(
        [("button", "Search", 'button "Search"', False), ("button", "Close", 'button "Close"', True)],
        headings=["Results"],
    )
    out = render_state(mixed)
    assert "# Results" in out and '[0] button "Search"' in out
    assert out.count('[1] button "Close"') == 1

    assert "(no interactive elements)" in render_state(_state([]))
    assert page_signature(mixed) != page_signature(_state([]))


def test_stuck_detection():
    assert looks_like_loop(["a", "a", "a"])
    assert looks_like_loop(["x", "a", "b", "a", "b"])  # A-B-A-B oscillation
    assert not looks_like_loop(["a", "b", "c"])
    assert not looks_like_loop(["a", "a"])  # too short to judge

    assert looks_stalled(["s", "s", "s", "s"])
    assert not looks_stalled(["s", "s", "s"])  # too short
    assert not looks_stalled(["s", "s", "s", "t"])

    # Same action, page unchanged -> stuck, and the count climbs to the refusal
    # threshold of 2.
    stuck, n = repeat_guard_step("click 1", "P", "click 1", "P", [], 0)
    assert (stuck, n) == (True, 1)
    stuck, n = repeat_guard_step("click 1", "P", "click 1", "P", [], n)
    assert (stuck, n) == (True, 2)
    # Bouncing back to a recently-seen state (an open/close toggle) also counts.
    assert repeat_guard_step("click 1", "P", "click 1", "Q", ["P"], 0) == (True, 1)
    # A different action, or real progress, resets the count.
    assert repeat_guard_step("click 2", "P", "click 1", "P", [], 5) == (False, 0)
    assert repeat_guard_step("click 1", "NEW", "click 1", "P", [], 5) == (False, 0)


def test_compact_history():
    """Only the latest page snapshot, HTML dump and screenshot stay in full.

    This also guards the coupling between prompts.py's wording and the markers
    the compactor searches for: if SUPERVISOR_INJECT stops containing SUP_MARK,
    the old HTML stops being pruned and the last assertion below fails.
    """
    def image(tag):
        return {"role": "user", "content": [{"type": "image_url", "image_url": {"url": tag}}]}

    msgs = [
        {"role": "tool", "content": "Clicked [1]" + prompts.PAGE_MARK + "OLD PAGE"},
        image("old.jpg"),
        {"role": "tool", "content": prompts.HTML_PREFIX + "OLD HTML"},
        {"role": "tool", "content": "Clicked [2]" + prompts.PAGE_MARK + "NEW PAGE"},
        image("new.jpg"),
        {"role": "user", "content": prompts.SUPERVISOR_INJECT.format(advice="scroll", html="SUP HTML")},
    ]
    Agent._compact_history(SimpleNamespace(messages=msgs))

    assert "OLD PAGE" not in msgs[0]["content"] and msgs[0]["content"].endswith(prompts.PAGE_NOTE)
    assert "Clicked [1]" in msgs[0]["content"], "the action result itself must survive"
    assert "NEW PAGE" in msgs[3]["content"], "the latest page must stay in full"
    assert msgs[1]["content"] == prompts.SHOT_OMITTED
    assert msgs[4]["content"][0]["image_url"]["url"] == "new.jpg", "latest screenshot stays"
    assert msgs[2]["content"] == prompts.HTML_PREFIX + prompts.HTML_NOTE
    assert "SUP HTML" in msgs[5]["content"], "the latest HTML dump is the supervisor's"

    # Idempotent: a second pass must not corrupt what it already pruned.
    before = [m["content"] for m in msgs]
    Agent._compact_history(SimpleNamespace(messages=msgs))
    assert [m["content"] for m in msgs] == before


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"  ok  {name}")
    print("OK")
