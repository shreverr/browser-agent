"""The agent's action surface.

Each tool is declared beside its body in one of the submodules below, grouped by
what it acts on. Adding a capability = one function in the matching module (plus
a `BrowserSession` method in `browser/session.py` if it touches the page).

The three names the agent loop imports are derived from the registry rather than
hand-maintained, so a new tool can't drift out of sync with its own flags.
"""

from __future__ import annotations

from typing import Any

# Imported for their side effect: each declares its tools into the registry.
# This is also the order the model sees the tools in, so the lines are grouped
# deliberately rather than alphabetically — `meta` last keeps `done` at the end.
# `tests/test_tools.py` fails if something reorders them.
from . import page  # noqa: F401
from . import forms  # noqa: F401
from . import read  # noqa: F401
from . import meta  # noqa: F401
from .registry import REGISTRY, execute

TOOLS: list[dict[str, Any]] = [t["schema"] for t in REGISTRY.values()]
# Tools that only read the page: the loop skips the post-action state refresh.
READ_TOOLS: tuple[str, ...] = tuple(n for n, t in REGISTRY.items() if t["read_only"])
# Tools handled inside the agent loop, not by execute().
AGENT_TOOLS: tuple[str, ...] = tuple(n for n, t in REGISTRY.items() if t["run"] is None)

__all__ = ["AGENT_TOOLS", "READ_TOOLS", "REGISTRY", "TOOLS", "execute"]
