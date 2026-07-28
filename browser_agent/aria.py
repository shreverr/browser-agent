"""Parse Playwright's ``aria_snapshot(mode="ai", boxes=True)`` output.

That call returns a YAML-ish, indentation-nested tree that Playwright computes
in-page (so it works on Firefox/Camoufox, unlike the removed native
``accessibility.snapshot()``). Sample lines::

    - generic [active] [ref=e1] [box=8,20,1264,240]:
      - heading "Select delivery address" [level=2] [ref=e2] [box=8,20,1264,29]
      - button "Real Button" [ref=e3] [box=8,69,84,22]
      - generic [ref=e4] [cursor=pointer] [box=12,95,1256,37]: Home • 207 km, HSR
      - button "Add Address to proceed" [disabled] [ref=e7] [box=8,218,163,22]

Grammar of one line: ``- <role> ["<name>"] [attr]... [: inline text]``. A node is
actionable when it carries a ``[ref=eN]`` (Playwright grants one to any visible
node that receives pointer events — including ``role=generic`` React onClick
divs), which we act on via ``frame.locator("aria-ref=eN")``.

``parse()`` -> nested tree of dicts; ``flatten()`` -> ordered list of our node
model (``control`` / ``heading`` / ``text``).
"""

from __future__ import annotations

import re

_LINE = re.compile(r"^(?P<indent> *)- (?P<rest>.*)$")
_HEAD = re.compile(
    r'^(?P<role>[^\s"\[:]+)'
    r'(?:\s+"(?P<name>(?:[^"\\]|\\.)*)")?'
    r"(?P<attrs>(?:\s*\[[^\]]*\])*)"
    r"\s*(?::\s?(?P<inline>.*))?$"
)

# Roles that are inherently actionable controls (get an index even without a
# cursor:pointer hint).
INTERACTIVE = {
    "button", "link", "textbox", "searchbox", "combobox", "checkbox", "radio",
    "switch", "tab", "menuitem", "menuitemcheckbox", "menuitemradio", "option",
    "slider", "spinbutton", "treeitem",
}
# Roles we fold into a `clickable` label when they become a control via cursor.
_GENERIC = {"generic", "text", "img", "paragraph", "list", "listitem", "cell", "region"}
# Container roles whose option children we roll up rather than list separately.
_OPTION_CONTAINERS = {"combobox", "listbox", "menu"}


def _attrs(s: str) -> dict:
    d: dict = {}
    for m in re.finditer(r"\[([^\]]*)\]", s or ""):
        kv = m.group(1)
        if "=" in kv:
            k, v = kv.split("=", 1)
            d[k] = v
        else:
            d[kv] = True
    return d


def _unesc(s: str) -> str:
    return (s or "").replace('\\"', '"').replace("\\\\", "\\")


def _box(s):
    if not s:
        return None
    parts = str(s).split(",")
    if len(parts) != 4:
        return None
    try:
        return tuple(float(x) for x in parts)
    except ValueError:
        return None


def parse(text: str) -> list:
    """String snapshot -> nested list of node dicts (each with `children`)."""
    root = {"depth": -1, "children": []}
    stack = [root]
    for raw in (text or "").splitlines():
        m = _LINE.match(raw)
        if not m:
            # Continuation line (e.g. wrapped long text) — append to current node.
            if len(stack) > 1 and raw.strip():
                cur = stack[-1]
                cur["inline"] = ((cur.get("inline") or "") + " " + raw.strip()).strip()
            continue
        depth = len(m.group("indent")) // 2
        hm = _HEAD.match(m.group("rest").rstrip())
        if not hm:
            continue
        a = _attrs(hm.group("attrs"))
        node = {
            "depth": depth,
            "role": hm.group("role"),
            "name": _unesc(hm.group("name")) if hm.group("name") is not None else "",
            "inline": (hm.group("inline") or "").strip(),
            "ref": a.get("ref") if isinstance(a.get("ref"), str) else None,
            "cursor_pointer": a.get("cursor") == "pointer",
            "box": _box(a.get("box")),
            "level": int(a["level"]) if str(a.get("level", "")).isdigit() else 0,
            "disabled": bool(a.get("disabled")),
            "checked": a.get("checked"),
            "expanded": a.get("expanded"),
            "selected": bool(a.get("selected")),
            "children": [],
        }
        while len(stack) > 1 and stack[-1]["depth"] >= depth:
            stack.pop()
        stack[-1]["children"].append(node)
        stack.append(node)
    return root["children"]


def _merged_text(node: dict) -> str:
    # Skip Playwright property nodes like `- /url: https://…` (role starts "/").
    if node.get("role", "").startswith("/"):
        return ""
    parts = []
    if node.get("inline"):
        parts.append(node["inline"])
    if node.get("name"):
        parts.append(node["name"])
    for c in node.get("children", []):
        t = _merged_text(c)
        if t:
            parts.append(t)
    return " ".join(parts).strip()


def _options(node: dict) -> list:
    out = []
    def walk(n):
        for c in n.get("children", []):
            if c["role"] == "option":
                lbl = (c.get("name") or c.get("inline") or "").strip()
                if lbl:
                    out.append(lbl)
            walk(c)
    walk(node)
    return out[:25]


def flatten(tree: list) -> list:
    """Nested tree -> ordered list of {kind, role, name, ref, box, state...} nodes."""
    out: list = []

    def walk(node: dict, under_dialog: bool):
        role = node["role"]
        is_dialog = role in ("dialog", "alertdialog")
        overlay = under_dialog or is_dialog
        ref = node.get("ref")
        clickable = node.get("cursor_pointer") and not is_dialog
        is_control = bool(ref) and (role in INTERACTIVE or clickable)

        skip_option_kids = False
        if is_control:
            disp_role = role if role in INTERACTIVE else "clickable"
            name = node.get("name") or node.get("inline") or _merged_text(node)
            opts = _options(node) if role in _OPTION_CONTAINERS else []
            skip_option_kids = role in _OPTION_CONTAINERS
            out.append(
                {
                    "kind": "control",
                    "role": disp_role,
                    "name": name.strip()[:200],
                    "ref": ref,
                    "box": node.get("box"),
                    "overlay": overlay,
                    "disabled": node.get("disabled", False),
                    "checked": node.get("checked"),
                    "expanded": node.get("expanded"),
                    "selected": node.get("selected", False),
                    "options": opts,
                }
            )
        elif role == "heading":
            nm = (node.get("name") or node.get("inline")).strip()
            if nm:
                out.append({"kind": "heading", "level": node.get("level", 0), "name": nm, "overlay": overlay})
        elif role in ("alert", "status"):
            t = _merged_text(node)
            if t:
                out.append({"kind": "text", "name": t[:200], "overlay": overlay})

        for c in node.get("children", []):
            if skip_option_kids and c["role"] == "option":
                continue
            walk(c, overlay)

    for n in tree:
        walk(n, False)
    return out
