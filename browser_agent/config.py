"""Every environment knob, in one place.

`__init__.py` runs `load_dotenv()` before any submodule is imported, so these
module-level reads already see the values from `.env`. Nothing else in the
package touches `os.environ` — add a knob here and document it in
`.env.example`.
"""

from __future__ import annotations

import os
from pathlib import Path

# --- models and the loop ----------------------------------------------------

MODEL = os.environ.get("AGENT_MODEL") or "anthropic/claude-opus-4.8"
CHECKER_MODEL = os.environ.get("AGENT_CHECKER_MODEL") or MODEL
MAX_STEPS = int(os.environ.get("AGENT_MAX_STEPS") or 50)
CHECK_EVERY = int(os.environ.get("AGENT_CHECK_EVERY") or 10)
# Vision: send a page screenshot to the model each turn. Needs a multimodal
# AGENT_MODEL. On by default; set AGENT_VISION=0 to disable.
VISION = os.environ.get("AGENT_VISION", "1") != "0"

API_KEY = os.environ.get("OPENROUTER_API_KEY")
DEBUG_TOKENS = bool(os.environ.get("AGENT_DEBUG_TOKENS"))

# --- browser ---------------------------------------------------------------

HEADLESS = os.environ.get("HEADLESS") == "true"
# Cookies/localStorage carried across runs, so a site stays logged in.
STATE_FILE = Path(os.environ.get("AGENT_STATE_FILE", ".profile/state.json"))
# Persistent user facts the agent may read and write.
MEMORY_FILE = Path(os.environ.get("AGENT_MEMORY_FILE", ".profile/memory.json"))

# --- safety guardrails (deterministic, enforced in code) -------------------

# Navigation is refused outside these domains (comma-separated in
# AGENT_ALLOWED_DOMAINS). Empty = unrestricted.
ALLOWED_DOMAINS = [
    d.strip().lower() for d in os.environ.get("AGENT_ALLOWED_DOMAINS", "").split(",") if d.strip()
]

# Clicking/submitting an element whose label contains one of these phrases
# requires explicit human confirmation first — enforced in code, not left to the
# model's judgement (prompt injection can't bypass it).
_DEFAULT_CONFIRM = (
    "place order,place your order,pay now,proceed to pay,buy now,complete purchase,"
    "confirm order,confirm & pay,confirm and pay,submit order,delete,transfer"
)
CONFIRM_KEYWORDS = [
    k.strip().lower()
    for k in os.environ.get("AGENT_CONFIRM_KEYWORDS", _DEFAULT_CONFIRM).split(",")
    if k.strip()
]
