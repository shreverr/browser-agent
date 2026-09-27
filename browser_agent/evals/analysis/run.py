# Throwaway batch runner for error analysis (shreverr/browser-agent#32).
"""Run error-analysis tasks one by one, each on a fresh profile, and write ea-1 Traces.

    uv run --group analysis python -m browser_agent.evals.analysis.run TASKS.yaml \
        [--batch-id ID] [--only ID ...] [--trials K] [--timeout SECONDS]

Simulated user: `ask_user` questions are matched against the task's `replies` rules in
order (case-insensitive regex). A question no rule matches gets no answer and flags the
Trial `unscripted_fallback`. Consequential-action confirmations are always declined.
A verification challenge ends the Trial as `infra_error` / `captcha`.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import datetime as dt
import json
import platform
import re
import subprocess
import sys
import traceback
import urllib.request
from pathlib import Path
from typing import Any

import yaml

from ... import agent as agent_module
from ... import prompts
from ...browser import BrowserAction, BrowserAdapterError, BrowserConfig, NodriverSession
from ...config import CHECK_EVERY, CHECKER_MODEL, HEADLESS, MAX_STEPS, MODEL, VISION
from ...state import CapturePolicy, EpisodeMetadata, LocalArtifactStore, LocalBrowserStateAdapter
from ...tools import TOOLS
from .capture import SCHEMA_VERSION, BlobStore, CaptchaEncountered, RecordingAgent, TraceWriter

ROOT = Path(".evals")
INFRA_RETRIES = 2

# Never touch the real user memory file during analysis runs.
agent_module.save_memory = lambda _memory: None  # type: ignore[assignment]


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def code_revision() -> str:
    try:
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=True
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        return sha + ("+dirty" if dirty else "")
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def context_lengths() -> dict[str, int]:
    try:
        with urllib.request.urlopen("https://openrouter.ai/api/v1/models", timeout=15) as response:
            data = json.load(response)["data"]
        return {
            model["id"]: int(model["context_length"])
            for model in data
            if model.get("context_length")
        }
    except Exception:
        return {}


class SimulatedUser:
    def __init__(self, rules: list[dict[str, str]], agent: RecordingAgent) -> None:
        self.rules = rules
        self.agent = agent

    async def __call__(self, question: str) -> str:
        if question == prompts.CAPTCHA_PAUSE:
            raise CaptchaEncountered("verification challenge detected")
        if question.startswith(prompts.CONFIRM_ASK.split("{", 1)[0]):
            self.agent.exchange_source = "policy"
            return "no"
        for rule in self.rules:
            if re.search(rule["match"], question, re.IGNORECASE):
                self.agent.exchange_source = f"scripted:{rule['id']}"
                return rule["reply"]
        self.agent.exchange_source = "unscripted_fallback"
        return ""


def build_session(state_root: Path, allowed_domains: list[str]) -> Any:
    artifacts = LocalArtifactStore(state_root / "artifacts", encryption_key=bytes(32))
    state = LocalBrowserStateAdapter(state_root / "browser", artifacts)
    return NodriverSession(
        BrowserConfig(headless=HEADLESS, allowed_domains=tuple(allowed_domains)),
        state,
        CapturePolicy(
            version="capture-v1",
            redaction_policy_version="redaction-v1",
            restricted_storage=True,
        ),
        EpisodeMetadata(
            code_revision="error-analysis", platform=sys.platform, task_id="error-analysis"
        ),
    )


def terminal_reason(agent: RecordingAgent, answer: str) -> str:
    if agent.done_called:
        return "done"
    if answer.startswith("Reached the step limit"):
        return "max_steps"
    if answer == "(no response from model)":
        return "no_response"
    return "no_tool_call"


def infra_class(error: BaseException) -> str:
    if isinstance(error, CaptchaEncountered):
        return "captcha"
    module = type(error).__module__
    if module.startswith("openai"):
        return "provider"
    if isinstance(error, BrowserAdapterError):
        return "chrome"
    return "other"


async def run_trial(
    task: dict[str, Any],
    trial_dir: Path,
    header: dict[str, Any],
    blobs: BlobStore,
    lengths: dict[str, int],
    timeout: float,
    *,
    session: Any = None,
    client: Any = None,
) -> dict[str, Any]:
    trace = TraceWriter(trial_dir, blobs, header)
    if session is None:
        session = build_session(trial_dir / "state", list(task.get("allowed_domains") or []))
    agent = RecordingAgent(session, client=client, write=lambda _text: None)
    agent.memory = {str(k): str(v) for k, v in (task.get("memory") or {}).items()}
    agent.attach(trace, lengths)
    ask = SimulatedUser(list(task.get("replies") or []), agent)
    answer = ""
    try:
        await agent.start()
        header["environment"]["chrome_version"] = getattr(
            getattr(session, "metadata", None), "browser_version", None
        )
        if task.get("start_url"):
            await agent.browser.execute(BrowserAction("navigate", {"url": task["start_url"]}))
        async with asyncio.timeout(timeout):
            result = await agent.run(task["text"], ask)
        answer = result.answer
        header.update(
            status="completed",
            terminal_reason=terminal_reason(agent, answer),
            answer=answer,
            automation_outcome=result.automation_outcome.value,
        )
    except TimeoutError:
        header.update(status="completed", terminal_reason="timeout", automation_outcome="failed")
    except (KeyboardInterrupt, asyncio.CancelledError):
        header.update(status="aborted")
        raise
    except Exception as error:
        header.update(
            status="infra_error",
            infra={
                "class": infra_class(error),
                "message": f"{type(error).__name__}: {error}",
                "traceback": traceback.format_exc(),
            },
        )
    finally:
        with contextlib.suppress(Exception):
            await agent.close()
        header.update(
            ended_at=_now(),
            unscripted_fallback=agent.unscripted,
            memory_writes=agent.memory_writes,
            totals={
                "steps": trace.step,
                "model_calls": agent.calls,
                "prompt_tokens": agent.tokens["prompt"],
                "completion_tokens": agent.tokens["completion"],
                "cost_usd": round(agent.cost, 6),
                "wall_s": round(trace.elapsed(), 1),
                "peak_context_pct": agent.peak_context_pct,
            },
        )
        trace.emit(
            "trial_end",
            status=header["status"],
            terminal_reason=header.get("terminal_reason"),
            answer=header.get("answer"),
        )
        trace.write_header()
        trace.close()
    return header


def base_header(
    task: dict[str, Any], trial_id: str, batch_id: str, attempt: int, blobs: BlobStore
) -> dict[str, Any]:
    system = prompts.SYSTEM + ("\n\n" + prompts.VISION_NOTE if VISION else "")
    return {
        "trace_schema_version": SCHEMA_VERSION,
        "trial_id": trial_id,
        "batch_id": batch_id,
        "attempt": attempt,
        "kind": "task",
        "task": {
            "id": task["id"],
            "text": task["text"],
            "start_url": task.get("start_url"),
            "sites": task.get("sites") or [],
            "cell": task.get("cell") or {},
            "notes": task.get("notes"),
        },
        "variant": {
            "code_revision": code_revision(),
            "model": MODEL,
            "checker_model": CHECKER_MODEL,
            "max_steps": MAX_STEPS,
            "check_every": CHECK_EVERY,
            "vision": VISION,
            "system_prompt_ref": blobs.text(system),
            "tools_ref": blobs.json(TOOLS),
        },
        "initial_memory": task.get("memory") or {},
        "simulated_user": task.get("replies") or [],
        "environment": {"os": platform.platform(), "chrome_version": None, "headless": HEADLESS},
        "started_at": _now(),
        "ended_at": None,
        "status": "running",
        "terminal_reason": None,
        "answer": None,
        "automation_outcome": None,
        "unscripted_fallback": False,
        "memory_writes": [],
        "infra": None,
        "totals": None,
    }


def finished(trial_dir: Path) -> dict[str, Any] | None:
    try:
        header = json.loads((trial_dir / "trial.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return header if header.get("status") not in {None, "running", "aborted"} else None


async def main_async(args: argparse.Namespace) -> int:
    tasks = yaml.safe_load(Path(args.tasks).read_text(encoding="utf-8"))["tasks"]
    if args.only:
        tasks = [task for task in tasks if task["id"] in set(args.only)]
    batch_dir = ROOT / "error-analysis" / args.batch_id
    blobs = BlobStore(ROOT / "blobs")
    lengths = context_lengths()
    for task in tasks:
        for k in range(1, args.trials + 1):
            for attempt in range(1, INFRA_RETRIES + 2):
                trial_id = f"{task['id']}.t{k}.a{attempt}"
                trial_dir = batch_dir / "trials" / trial_id
                header = finished(trial_dir)
                if header is None:
                    if trial_dir.exists():
                        for path in sorted(trial_dir.rglob("*"), reverse=True):
                            path.unlink() if path.is_file() else path.rmdir()
                    print(f"▶ {trial_id}", flush=True)
                    header = await run_trial(
                        task,
                        trial_dir,
                        base_header(task, trial_id, args.batch_id, attempt, blobs),
                        blobs,
                        lengths,
                        args.timeout,
                    )
                totals = header.get("totals") or {}
                print(
                    f"  {trial_id}: {header['status']}"
                    f" {header.get('terminal_reason') or (header.get('infra') or {}).get('class')}"
                    f" steps={totals.get('steps')} cost=${totals.get('cost_usd')}",
                    flush=True,
                )
                infra = header.get("infra") or {}
                if header["status"] != "infra_error" or infra.get("class") == "captcha":
                    break
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tasks")
    parser.add_argument("--batch-id", default=dt.date.today().isoformat())
    parser.add_argument("--only", nargs="*")
    parser.add_argument("--trials", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=600)
    return asyncio.run(main_async(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
