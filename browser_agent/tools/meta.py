"""Tools the agent loop handles itself — they talk to the user or the loop, not
the page, so they are declared (schema only) and never reach `execute()`.

Imported last, which keeps `done` at the end of the list the model sees.
"""

from __future__ import annotations

from .registry import declare

declare(
    "ask_user",
    "Ask the user a question and wait for their reply. Use when the task is ambiguous, "
    "when a meaningful choice needs their input, or before an irreversible/consequential "
    "action.",
    {"question": {"type": "string", "description": "The question to ask the user"}},
    ["question"],
)

declare(
    "remember",
    "Permanently save a fact about this user for future sessions. Use a short "
    "snake_case key (e.g. 'phone', 'delivery_address', 'upi_id', 'preferred_seat'). "
    "Call this proactively whenever the user shares personal details or preferences.",
    {
        "key": {"type": "string", "description": "Short label for the fact, e.g. 'phone'"},
        "value": {"type": "string", "description": "The value to store"},
    },
    ["key", "value"],
)

declare(
    "forget",
    "Remove a previously saved user fact from memory.",
    {"key": {"type": "string", "description": "The key to delete"}},
    ["key"],
)

declare(
    "done",
    "Finish the task and return the final answer or summary to the user.",
    {"answer": {"type": "string", "description": "The final answer or summary"}},
    ["answer"],
)
