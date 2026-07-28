"""Tiny persistent user-memory store (a flat key -> value JSON file).

Mirrors src/memory.ts: facts the agent learns about the user (phone, address,
UPI id, preferences, ...) persist across sessions at .profile/memory.json.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

MEMORY_FILE = Path(os.environ.get("AGENT_MEMORY_FILE", ".profile/memory.json"))

# A UserMemory is just a dict[str, str].


def load_memory() -> dict[str, str]:
    if not MEMORY_FILE.exists():
        return {}
    try:
        return json.loads(MEMORY_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_memory(mem: dict[str, str]) -> None:
    MEMORY_FILE.parent.mkdir(parents=True, exist_ok=True)
    MEMORY_FILE.write_text(json.dumps(mem, indent=2), encoding="utf-8")


def format_memory(mem: dict[str, str]) -> str:
    entries = [(k, v) for k, v in mem.items() if v != ""]
    if not entries:
        return ""
    return "\n".join(f"  {k}: {v}" for k, v in entries)
