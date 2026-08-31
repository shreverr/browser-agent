"""browser-agent: an LLM-driven browser agent running on Camoufox (Firefox).

Loading `.env` here (before any submodule is imported) guarantees the
module-level reads in `config.py` see the configured values.
"""

from dotenv import load_dotenv

load_dotenv()
