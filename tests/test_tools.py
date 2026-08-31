"""Self-check for the tool registry.

These are the invariants the registry is there to enforce — before it, the same
facts lived in two hand-maintained tuples and an if/elif chain that could drift
apart silently.

    .venv/bin/python tests/test_tools.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from browser_agent import tools  # noqa: E402


def test_every_tool_is_runnable_or_handled_by_the_loop():
    for name, entry in tools.REGISTRY.items():
        runnable = entry["run"] is not None
        assert runnable != (name in tools.AGENT_TOOLS), f"{name}: needs a body xor loop handling"
        # A read-only tool must have a body — the loop never handles reads itself.
        assert not entry["read_only"] or runnable, name

    assert tools.READ_TOOLS == ("read_page", "get_html")
    assert set(tools.AGENT_TOOLS) == {"ask_user", "remember", "forget", "done"}


def test_schemas_are_well_formed_and_unique():
    names = [t["function"]["name"] for t in tools.TOOLS]
    assert len(names) == len(set(names)) == len(tools.REGISTRY)
    assert names == list(tools.REGISTRY), "TOOLS must stay in registration order"
    # `done` last, `navigate` first: the submodule import order in __init__.py.
    # If something alphabetised those imports, this is what catches it.
    assert names[0] == "navigate" and names[-1] == "done", names

    for t in tools.TOOLS:
        fn = t["function"]
        assert t["type"] == "function" and fn["description"].strip()
        params = fn["parameters"]
        assert params["type"] == "object" and isinstance(params["properties"], dict)
        # Every required key must actually be a declared property.
        assert set(params.get("required", [])) <= set(params["properties"]), fn["name"]


def test_unknown_tool_is_reported_not_raised():
    # The model can hallucinate a tool name; that must come back as a string it
    # can read and recover from, not an exception that kills the run.
    assert tools.execute(None, "teleport", {}) == "Unknown tool: teleport"
    # An agent-handled tool reaching execute() is the same kind of mistake.
    assert tools.execute(None, "done", {"answer": "x"}) == "Unknown tool: done"


def test_duplicate_registration_is_refused():
    try:
        tools.registry.declare("click", "a second click")
    except ValueError as err:
        assert "duplicate tool name" in str(err)
    else:
        raise AssertionError("registering an existing name must fail loudly")


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_"):
            fn()
            print(f"  ok  {name}")
    print("OK")
