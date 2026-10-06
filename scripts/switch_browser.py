#!/usr/bin/env python3
"""Switch local browser configuration while keeping separate browser state stores."""

import argparse
import json
import re
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("browser", choices=("fortress", "cloakbrowser"))
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    settings_path = root / ".profile/browser-switch/settings.json"
    if not settings_path.is_file():
        parser.error("Local browser setup is missing: .profile/browser-switch/settings.json")
    settings = json.loads(settings_path.read_text())[args.browser]
    executable = Path(settings["AGENT_BROWSER_EXECUTABLE"])
    if not executable.is_file():
        parser.error(f"Browser launcher is missing: {executable}")
    if args.browser == "cloakbrowser":
        binary_file = settings_path.parent / "cloakbrowser-binary.txt"
        if not binary_file.is_file() or not Path(binary_file.read_text().strip()).is_file():
            parser.error("CloakBrowser binary download is incomplete")
    env_path = root / ".env"
    content = env_path.read_text() if env_path.exists() else ""
    for key, value in settings.items():
        line = f'{key}="{value}"'
        pattern = rf"^{key}=.*$"
        if re.search(pattern, content, re.MULTILINE):
            content = re.sub(pattern, lambda _, line=line: line, content, flags=re.MULTILINE)
        else:
            content = content.rstrip("\n") + "\n" + line + "\n"
    env_path.write_text(content)
    env_path.chmod(0o600)
    print(f"Selected {args.browser}: {executable}")
    print(f"Browser state: {settings['AGENT_STATE_ROOT']}")
    print("Restart the agent to use this browser.")


if __name__ == "__main__":
    main()
