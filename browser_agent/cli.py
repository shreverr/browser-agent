"""Async CLI entry point for interactive and one-shot browser tasks."""

from __future__ import annotations

import asyncio
import base64
import os
import sys
from collections.abc import Callable, Sequence
from typing import TextIO

from .agent import Agent, AutomationOutcome
from .browser import BrowserConfig, NodriverSession
from .config import (
    ALLOWED_DOMAINS,
    API_KEY,
    BROWSER_EXECUTABLE,
    HEADLESS,
    START_URL,
    STATE_ENCRYPTION_KEY,
    STATE_ROOT,
)
from .state import (
    CapturePolicy,
    EpisodeMetadata,
    LocalArtifactStore,
    LocalBrowserStateAdapter,
)

BANNER = """
┌────────────────────────────────────────────┐
│   🌐  browser-agent                        │
│   an AI that drives your browser           │
└────────────────────────────────────────────┘
Type a task and press Enter. Follow-ups share one browser session.

  /help     show commands
  /exit     quit (or Ctrl+C)
"""

AgentFactory = Callable[[], Agent]


def preflight() -> None:
    if not API_KEY:
        raise RuntimeError(
            "OPENROUTER_API_KEY is not set. Add it to .env (see .env.example) or your shell."
        )


def _encryption_key() -> bytes:
    if STATE_ENCRYPTION_KEY:
        try:
            key = base64.b64decode(STATE_ENCRYPTION_KEY, validate=True)
        except ValueError as error:
            raise RuntimeError("AGENT_STATE_ENCRYPTION_KEY must be valid base64") from error
        if len(key) != 32:
            raise RuntimeError("AGENT_STATE_ENCRYPTION_KEY must decode to exactly 32 bytes")
        return key
    key_path = STATE_ROOT / "checkpoint.key"
    key_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        key = key_path.read_bytes()
    except FileNotFoundError:
        key = os.urandom(32)
        descriptor = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(key)
    if len(key) != 32:
        raise RuntimeError(f"checkpoint key at {key_path} must contain exactly 32 bytes")
    return key


def build_agent() -> Agent:
    artifacts = LocalArtifactStore(STATE_ROOT / "artifacts", encryption_key=_encryption_key())
    state = LocalBrowserStateAdapter(STATE_ROOT / "browser", artifacts)
    session = NodriverSession(
        BrowserConfig(
            headless=HEADLESS,
            executable_path=BROWSER_EXECUTABLE,
            allowed_domains=tuple(ALLOWED_DOMAINS),
        ),
        state,
        CapturePolicy(
            version="capture-v1",
            redaction_policy_version="redaction-v1",
            restricted_storage=True,
        ),
        EpisodeMetadata(code_revision="working-tree", platform=sys.platform, task_id="cli"),
    )
    return Agent(session, start_url=START_URL)


async def ask(question: str, *, input_fn: Callable[[str], str] = input) -> str:
    return await asyncio.to_thread(input_fn, f"\n🤔 {question}\n   ❯ ")


async def one_shot(
    task: str,
    *,
    agent_factory: AgentFactory = build_agent,
    input_fn: Callable[[str], str] = input,
    stdout: TextIO = sys.stdout,
) -> int:
    agent = agent_factory()
    try:
        await agent.start()
        print(f"\n🎯 Task: {task}\n", file=stdout)
        result = await agent.run(task, lambda question: ask(question, input_fn=input_fn))
        print(f"\n✅ Result:\n{result.answer}\n", file=stdout)
        if result.automation_outcome is AutomationOutcome.FAILED:
            print("Automation outcome: failed (human intervention required)", file=stdout)
        return 0
    finally:
        await agent.close()


async def interactive(
    *,
    agent_factory: AgentFactory = build_agent,
    input_fn: Callable[[str], str] = input,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
) -> int:
    agent = agent_factory()
    await agent.start()
    print(BANNER, file=stdout)
    try:
        while True:
            try:
                task = (await asyncio.to_thread(input_fn, "❯ ")).strip()
            except EOFError:
                break
            if not task:
                continue
            if task in {"/exit", "/quit"}:
                break
            if task == "/help":
                print("Type a browser task. /exit quits; follow-ups share session.", file=stdout)
                continue
            try:
                result = await agent.run(task, lambda question: ask(question, input_fn=input_fn))
                print(f"\n✅ {result.answer}\n", file=stdout)
                if result.automation_outcome is AutomationOutcome.FAILED:
                    print("Automation outcome: failed (human intervention required)\n", file=stdout)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                print(f"\n❌ {error}\n", file=stderr)
        return 0
    finally:
        await agent.close()


async def async_main(
    argv: Sequence[str] | None = None,
    *,
    agent_factory: AgentFactory = build_agent,
    input_fn: Callable[[str], str] = input,
    stdout: TextIO = sys.stdout,
    stderr: TextIO = sys.stderr,
) -> int:
    try:
        preflight()
        args = list(sys.argv[1:] if argv is None else argv)
        task = " ".join(args).strip()
        if task:
            return await one_shot(
                task,
                agent_factory=agent_factory,
                input_fn=input_fn,
                stdout=stdout,
            )
        return await interactive(
            agent_factory=agent_factory,
            input_fn=input_fn,
            stdout=stdout,
            stderr=stderr,
        )
    except asyncio.CancelledError:
        return 130
    except Exception as error:
        print(f"\n❌ Error: {error}\n", file=stderr)
        return 1


def main() -> None:
    try:
        status = asyncio.run(async_main())
    except KeyboardInterrupt:
        status = 130
    raise SystemExit(status)


if __name__ == "__main__":
    main()
