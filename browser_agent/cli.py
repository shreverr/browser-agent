"""Entry point. Ported from src/cli.ts.

Interactive REPL (no args) or one-shot mode (task passed as arguments). The
`ask()` bridge (via input()) serves both the main prompt and the agent's
mid-task `ask_user` / CAPTCHA pauses.
"""

from __future__ import annotations

import os
import sys

from .agent import Agent

BANNER = """
┌────────────────────────────────────────────┐
│   🌐  browser-agent                          │
│   an AI that drives your browser             │
└────────────────────────────────────────────┘
Type a task and press Enter. The agent runs it in the browser.
It narrates what it's doing, and will ask you questions when something
is unclear or a real choice/irreversible action comes up.
Follow-ups continue in the same session.

  /help     show commands
  /exit     quit (or Ctrl+C)

Defaults to Indian context (₹ INR, amazon.in, google.co.in) unless you say otherwise.
"""


def preflight() -> None:
    if not os.environ.get("OPENROUTER_API_KEY"):
        print(
            "OPENROUTER_API_KEY is not set. Add it to .env (see .env.example) or your shell.",
            file=sys.stderr,
        )
        sys.exit(1)


def ask(question: str) -> str:
    """Async question function used for both the main prompt and the agent's
    mid-task `ask_user` calls."""
    try:
        return input(f"\n🤔 {question}\n   ❯ ")
    except EOFError:
        return ""


def one_shot(task: str) -> None:
    headless = os.environ.get("HEADLESS") == "true"
    agent = Agent()
    agent.start(headless)
    print(f"\n🎯 Task: {task}\n")
    try:
        answer = agent.run(task, ask)
        print(f"\n✅ Result:\n{answer}\n")
    finally:
        agent.close()


def interactive() -> None:
    # Interactive mode shows the browser by default so you can watch it work.
    headless = os.environ.get("HEADLESS") == "true"
    agent = Agent()
    agent.start(headless)

    print(BANNER)

    try:
        while True:
            try:
                task = input("❯ ").strip()
            except EOFError:
                break  # Ctrl+D
            if not task:
                continue
            if task in ("/exit", "/quit"):
                break
            if task == "/help":
                print("\n  Just type what you want done, e.g.:")
                print("    recommend a guitar under 30k")
                print("    open flipkart and find the cheapest 1TB SSD")
                print("  The agent will ask you if anything is unclear.")
                print("  /exit to quit. Follow-ups stay in the same browser session.\n")
                continue

            try:
                answer = agent.run(task, ask)
                print(f"\n✅ {answer}\n")
            except Exception as err:
                print(f"\n❌ {err}\n", file=sys.stderr)
    except KeyboardInterrupt:
        pass  # Ctrl+C — fall through to a clean shutdown
    finally:
        agent.close()


def main() -> None:
    preflight()
    task = " ".join(sys.argv[1:]).strip()
    if task:
        one_shot(task)  # backwards-compatible single-task mode
        return
    interactive()


if __name__ == "__main__":
    try:
        main()
    except Exception as err:  # noqa: BLE001
        print(f"\n❌ Error: {err}\n", file=sys.stderr)
        sys.exit(1)
