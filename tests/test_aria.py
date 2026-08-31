"""Self-check for the aria snapshot parser.

Imports only `browser_agent.browser.aria`, so it runs without a browser or a
network — and without camoufox installed.

    .venv/bin/python tests/test_aria.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from browser_agent.browser import aria  # noqa: E402

# The snapshot shape aria.py documents, plus a dialog, a combobox and a
# cursor-only clickable div.
SNAPSHOT = """- generic [active] [ref=e1] [box=8,20,1264,240]:
  - heading "Select delivery address" [level=2] [ref=e2] [box=8,20,1264,29]
  - button "Real Button" [ref=e3] [box=8,69,84,22]
  - generic [ref=e4] [cursor=pointer] [box=12,95,1256,37]: Home • 207 km, HSR
  - button "Add Address to proceed" [disabled] [ref=e7] [box=8,218,163,22]
  - dialog "Cart" [ref=e8] [box=0,0,400,600]:
    - textbox "Coupon" [ref=e10] [box=0,30,200,20]
    - combobox "Qty" [expanded=false] [ref=e11] [box=0,60,100,20]:
      - option "1" [ref=e12]
      - option "2" [ref=e13]
  - status [ref=e15]: Item added to cart
  - generic [ref=e17] [box=0,400,50,20]: no ref-less pointer, so not a control
"""


def test_aria():
    nodes = aria.flatten(aria.parse(SNAPSHOT))
    kinds = [n["kind"] for n in nodes]
    assert kinds.count("heading") == 1, kinds
    assert "text" in kinds, "a status node must survive as text"

    controls = [n for n in nodes if n["kind"] == "control"]
    by_name = {n["name"]: n for n in controls}

    # A ref'd cursor:pointer div is actionable, presented as `clickable`.
    assert by_name["Home • 207 km, HSR"]["role"] == "clickable"
    # A real button keeps its role and its disabled state.
    assert by_name["Add Address to proceed"]["disabled"] is True
    assert "(disabled)" in aria.leaf(by_name["Add Address to proceed"])
    # Combobox options roll up into the leaf instead of being listed separately.
    assert aria.leaf(by_name["Qty"]) == 'combobox "Qty" (collapsed) | options=[1 | 2]'
    assert "1" not in by_name, "option children must not become their own controls"
    # Everything inside the dialog is flagged as overlay; nothing outside is.
    assert by_name["Coupon"]["overlay"] is True
    assert by_name["Real Button"]["overlay"] is False
    # A ref'd node that is neither interactive nor pointer-cursored is not a control.
    assert not any("not a control" in n["name"] for n in controls)


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"  ok  {name}")
    print("OK")
