"""The tool registry: one dict holding every tool's schema, run function, and flags.

A tool is declared next to its body, so adding a capability is one function in
one file — no separate schema list, dispatch chain, or metadata tuple to keep in
sync. `tools/__init__.py` derives `TOOLS` / `READ_TOOLS` / `AGENT_TOOLS` from
what has registered itself here.
"""

from __future__ import annotations

from typing import Any, Callable

# name -> {"schema": openai tool schema, "run": fn | None, "read_only": bool}
# Insertion-ordered, which is the order the model sees the tools in; it follows
# the submodule import order in `__init__.py`.
REGISTRY: dict[str, dict[str, Any]] = {}

# A tool body: (browser, arguments) -> the result string the model sees.
RunFn = Callable[[Any, dict[str, Any]], str]


def _schema(name: str, description: str, properties: dict | None, required: list[str] | None) -> dict:
    return {
        "type": "function",
        "function": {
            "name": name,
            "description": description,
            "parameters": {
                "type": "object",
                "properties": properties or {},
                **({"required": required} if required else {}),
            },
        },
    }


def _register(name: str, entry: dict) -> None:
    if name in REGISTRY:
        raise ValueError(f"duplicate tool name: {name}")
    REGISTRY[name] = entry


def tool(
    name: str,
    description: str,
    properties: dict | None = None,
    required: list[str] | None = None,
    *,
    read_only: bool = False,
):
    """Register a tool the browser runs. Decorates a `(browser, inp) -> str` body.

    `read_only=True` marks a tool that cannot change the page, so the agent loop
    skips the post-action state refresh and the anti-repeat bookkeeping.
    """

    def deco(fn: RunFn) -> RunFn:
        _register(name, {"schema": _schema(name, description, properties, required), "run": fn, "read_only": read_only})
        return fn

    return deco


def declare(
    name: str,
    description: str,
    properties: dict | None = None,
    required: list[str] | None = None,
) -> None:
    """Register a tool the agent loop handles itself: a schema with no body.

    `done`, `ask_user`, `remember` and `forget` never reach `execute()` —
    `Agent.run` intercepts them, because they talk to the user or the loop
    rather than the page.
    """
    _register(name, {"schema": _schema(name, description, properties, required), "run": None, "read_only": False})


def execute(browser, name: str, inp: dict[str, Any]) -> str:
    """Run one browser tool and return the result string the model sees."""
    entry = REGISTRY.get(name)
    if entry is None or entry["run"] is None:
        return f"Unknown tool: {name}"
    return entry["run"](browser, inp)
