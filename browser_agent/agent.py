# OpenAI response/tool payloads are deliberately dynamic at this adapter boundary.
# pyright: reportUnknownArgumentType=false
# pyright: reportUnknownMemberType=false
# pyright: reportUnknownVariableType=false
# pyright: reportCallIssue=false
# pyright: reportArgumentType=false
"""Asynchronous model/tool loop over the public browser-session contract."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from openai import AsyncOpenAI

from . import prompts
from .browser import (
    ActionResult,
    BrowserAction,
    BrowserSession,
    Observation,
    OutcomeStatus,
    SemanticControl,
    TargetHandle,
)
from .config import (
    API_KEY,
    CHECK_EVERY,
    CHECKER_MODEL,
    CONFIRM_KEYWORDS,
    DEBUG_TOKENS,
    MAX_STEPS,
    MODEL,
    VISION,
)
from .memory import format_memory, load_memory, save_memory
from .tools import READ_TOOLS, REGISTRY, TOOLS

AskFn = Callable[[str], Awaitable[str]]
WriteFn = Callable[[str], None]


class ChatClient(Protocol):
    @property
    def chat(self) -> Any: ...


class AutomationOutcome(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class TaskResult:
    answer: str
    automation_outcome: AutomationOutcome
    human_interventions: tuple[str, ...] = ()


_VERIFICATION_HINT = re.compile(
    r"captcha|human verification|verify you are human|unusual traffic|cloudflare|turnstile",
    re.IGNORECASE,
)


def _control_text(control: SemanticControl) -> str:
    bits = [control.role, control.name]
    if control.description:
        bits.append(control.description)
    if control.value is not None:
        bits.append(f"value={control.value!r}")
    if control.states:
        bits.append(f"states={','.join(sorted(control.states))}")
    if control.frame_breadcrumb:
        bits.append(f"frame={' > '.join(control.frame_breadcrumb)}")
    if control.fallback_reason:
        bits.append(f"fallback={control.fallback_reason}")
    return " ".join(bit for bit in bits if bit)


def render_state(state: Observation) -> str:
    """Render one observation into bounded model-facing text."""
    lines = [f"URL: {state.url}", f"Title: {state.title}", "", "Page outline:"]
    for node in state.context:
        marker = "#" if node.kind == "heading" else "-"
        breadcrumb = (
            f" [frame: {' > '.join(node.frame_breadcrumb)}]" if node.frame_breadcrumb else ""
        )
        lines.append(f"{marker} {node.text}{breadcrumb}")
    for index, control in enumerate(state.controls):
        lines.append(f"[{index}] {_control_text(control)}")
    if not state.controls:
        lines.append("(no interactive elements)")
    for region in state.unsupported_regions:
        breadcrumb = " > ".join(region.frame_breadcrumb) or "active document"
        lines.append(f"- Unsupported region ({region.reason}) in {breadcrumb}")
    if state.truncated:
        omitted = ", ".join(f"{key}={value}" for key, value in state.omitted_counts.items())
        lines.append(f"- Observation truncated{': ' + omitted if omitted else ''}")
    lines.extend(f"- Warning: {warning}" for warning in state.warnings)
    return "\n".join(lines)


def looks_like_loop(actions: list[str]) -> bool:
    if len(actions) >= 3 and all(action == actions[-1] for action in actions[-3:]):
        return True
    if len(actions) >= 4:
        a, b, c, d = actions[-4:]
        return a == c and b == d and a != b
    return False


def looks_stalled(state_sigs: list[str]) -> bool:
    return len(state_sigs) >= 4 and len(set(state_sigs[-4:])) == 1


def repeat_guard_step(
    action_sig: str,
    sig: str,
    last_action_sig: str | None,
    last_state_sig: str | None,
    recent_sigs: list[str],
    stuck_repeats: int,
) -> tuple[bool, int]:
    stuck = action_sig == last_action_sig and (sig == last_state_sig or sig in recent_sigs)
    return stuck, (stuck_repeats + 1 if stuck else 0)


def page_signature(state: Observation) -> str:
    controls = "|".join(_control_text(control) for control in state.controls[:6])
    context = "|".join(node.text for node in state.context[:4])
    return f"{state.url}#{state.document_generation}#{len(state.controls)}#{controls}#{context}"


def _looks_like_vision_error(error: Exception) -> bool:
    text = str(error).lower()
    return any(word in text for word in ("image", "vision", "multimodal", "modalit", "image_url"))


def _is_anthropic(model: str) -> bool:
    lowered = (model or "").lower()
    return "anthropic/" in lowered or "claude" in lowered


def _as_int(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _has_image(message: Mapping[str, Any]) -> bool:
    content = message.get("content")
    return isinstance(content, list) and any(
        isinstance(part, dict) and part.get("type") == "image_url" for part in content
    )


def _jsonable(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def render_action_result(result: ActionResult) -> str:
    payload: dict[str, object] = {
        "status": result.status.value,
        "message": result.message,
        "retryable": result.retryable,
    }
    if result.error_code:
        payload["error_code"] = result.error_code
    if result.details:
        details = dict(result.details)
        if "data" in details and isinstance(details["data"], str):
            details["data"] = f"<base64 image: {len(details['data'])} characters>"
        payload["details"] = _jsonable(details)
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


async def supervise(
    client: ChatClient, task: str, recent_actions: list[str], state_text: str
) -> dict[str, Any]:
    try:
        response = await client.chat.completions.create(
            model=CHECKER_MODEL,
            max_tokens=300,
            messages=[
                {"role": "system", "content": prompts.SUPERVISOR_SYSTEM},
                {
                    "role": "user",
                    "content": prompts.SUPERVISOR_USER.format(
                        task=task, actions="\n".join(recent_actions), state=state_text
                    ),
                },
            ],
        )
        match = re.search(r"\{[\s\S]*\}", response.choices[0].message.content or "")
        if not match:
            return {"looping": False, "advice": ""}
        parsed = json.loads(match.group(0))
        return {"looping": bool(parsed.get("looping")), "advice": str(parsed.get("advice", ""))}
    except Exception:
        return {"looping": False, "advice": ""}


class Agent:
    """Persistent conversation and browser session; task outcome resets per run."""

    def __init__(
        self,
        browser: BrowserSession,
        *,
        client: ChatClient | None = None,
        write: WriteFn = print,
        start_url: str | None = None,
    ) -> None:
        self.browser = browser
        self._vision_on = VISION
        self._write = write
        self._started = False
        self._start_url = start_url
        self._invalidated = False
        self._task_interventions: list[str] = []
        system = prompts.SYSTEM + ("\n\n" + prompts.VISION_NOTE if VISION else "")
        system_content: Any = system
        if _is_anthropic(MODEL):
            system_content = [
                {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}
            ]
        self.messages: list[dict[str, Any]] = [{"role": "system", "content": system_content}]
        self.memory = load_memory()
        self.client = client or AsyncOpenAI(
            base_url="https://openrouter.ai/api/v1",
            api_key=API_KEY,
            default_headers={
                "HTTP-Referer": "https://github.com/local/browser-agent",
                "X-Title": "browser-agent",
            },
        )

    async def start(self) -> None:
        if self._started:
            return
        await self.browser.start()
        if self._start_url:
            try:
                result = await self.browser.execute(
                    BrowserAction("navigate", arguments={"url": self._start_url})
                )
                if result.status is not OutcomeStatus.SUCCEEDED:
                    raise RuntimeError(f"Could not open startup page: {result.message}")
            except BaseException:
                await self.browser.close()
                raise
        self._started = True
        if self._vision_on:
            self._write(
                f"👁️  Vision ON — sending a screenshot each turn. "
                f"AGENT_MODEL must be multimodal (current: {MODEL}). Set AGENT_VISION=0 to disable."
            )

    async def close(self) -> None:
        await self.browser.close()
        self._started = False

    async def _capture_screenshot(self) -> None:
        if not self._vision_on:
            return
        try:
            result = await self.browser.execute(BrowserAction("screenshot", read_only=True))
        except Exception:
            return
        data = result.details.get("data")
        if result.status is not OutcomeStatus.SUCCEEDED or not isinstance(data, str):
            return
        observation = result.observation
        viewport = observation.viewport if observation is not None else None
        label = prompts.SHOT_LABEL
        if viewport is not None:
            label += prompts.SHOT_COORDS.format(w=int(viewport.width), h=int(viewport.height))
        self.messages.append(
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": label},
                    {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{data}"}},
                ],
            }
        )

    def _disable_vision(self) -> None:
        self._vision_on = False
        for message in self.messages:
            if _has_image(message):
                message["content"] = prompts.SHOT_OMITTED

    async def _complete(self) -> Any:
        kwargs = {
            "model": MODEL,
            "max_tokens": 4000,
            "messages": self.messages,
            "tools": TOOLS,
            "tool_choice": "auto",
            "parallel_tool_calls": False,
        }
        try:
            return await self.client.chat.completions.create(**kwargs)
        except Exception as error:
            if self._vision_on and _looks_like_vision_error(error):
                self._write(
                    "⚠️  Model rejected image input — disabling vision and retrying text-only."
                )
                self._disable_vision()
                return await self.client.chat.completions.create(**kwargs)
            raise

    def _compact_history(self) -> None:
        messages = self.messages

        def text_indices(predicate: Callable[[str], bool]) -> list[int]:
            return [
                index
                for index, message in enumerate(messages)
                if isinstance(message.get("content"), str) and predicate(message["content"])
            ]

        for index in text_indices(lambda content: prompts.PAGE_MARK in content)[:-1]:
            content = messages[index]["content"]
            messages[index]["content"] = content.split(prompts.PAGE_MARK, 1)[0] + prompts.PAGE_NOTE

        def is_html(content: str) -> bool:
            return content.startswith(prompts.HTML_PREFIX) or prompts.SUP_MARK in content

        for index in text_indices(is_html)[:-1]:
            content = messages[index]["content"]
            messages[index]["content"] = (
                prompts.HTML_PREFIX + prompts.HTML_NOTE
                if content.startswith(prompts.HTML_PREFIX)
                else content.split(prompts.SUP_MARK, 1)[0] + prompts.HTML_NOTE
            )
        for index in [i for i, message in enumerate(messages) if _has_image(message)][:-1]:
            messages[index]["content"] = prompts.SHOT_OMITTED

    def _mark_intervention(self, reason: str) -> None:
        self._task_interventions.append(reason)

    async def _ask(self, ask: AskFn, question: str, reason: str) -> str:
        self._mark_intervention(reason)
        return (await ask(question)).strip()

    @staticmethod
    def _has_verification(observation: Observation) -> bool:
        content = " ".join(
            [observation.title, *(node.text for node in observation.context)]
            + [f"{control.name} {control.description}" for control in observation.controls]
        )
        return bool(_VERIFICATION_HINT.search(content))

    async def _pause_for_verification(self, ask: AskFn) -> Observation:
        self._write("🧩 Human verification detected. Waiting for browser intervention…")
        answer = await self._ask(ask, prompts.CAPTCHA_PAUSE, "verification")
        state = await self.browser.observe()
        self.messages.append(
            {
                "role": "user",
                "content": prompts.CAPTCHA_RESUMED.format(
                    skipped=" (skipped)" if re.search(r"skip", answer, re.I) else "",
                    state=render_state(state),
                ),
            }
        )
        return state

    async def _meta_tool_result(self, name: str, inp: dict[str, Any], ask: AskFn) -> str | None:
        if name == "ask_user":
            answer = await self._ask(
                ask, str(inp.get("question", "(agent has a question)")), "user_input"
            )
            return f"User answered: {answer}" if answer else "User gave no answer."
        if name == "remember":
            key = str(inp.get("key", "")).strip()
            value = str(inp.get("value", "")).strip()
            if key:
                self.memory[key] = value
                await asyncio.to_thread(save_memory, self.memory)
                self._write(f"💾 Remembered: {key} = {value}")
            return f"Saved: {key} = {value}"
        if name == "forget":
            key = str(inp.get("key", "")).strip()
            if key in self.memory:
                del self.memory[key]
                await asyncio.to_thread(save_memory, self.memory)
                self._write(f"🗑️  Forgot: {key}")
            return f"Deleted: {key}"
        return None

    @staticmethod
    def _confirm_target(name: str, inp: dict[str, Any], state: Observation) -> str | None:
        if not CONFIRM_KEYWORDS or name not in {"click", "fill_form"}:
            return None
        indices = [_as_int(inp.get("index"))]
        if name == "fill_form":
            indices = [
                _as_int(field.get("index"))
                for field in inp.get("fields") or []
                if isinstance(field, dict) and field.get("submit")
            ]
        labels = [
            _control_text(state.controls[index])
            for index in indices
            if index is not None and 0 <= index < len(state.controls)
        ]
        return next(
            (label for label in labels if any(word in label.lower() for word in CONFIRM_KEYWORDS)),
            None,
        )

    @staticmethod
    def _target(state: Observation, value: object) -> TargetHandle:
        index = _as_int(value)
        if index is None or index < 0 or index >= len(state.controls):
            raise ValueError(f"No control with index {value!r} in current observation")
        return state.controls[index].handle

    @classmethod
    def _browser_action(cls, name: str, inp: dict[str, Any], state: Observation) -> BrowserAction:
        entry = REGISTRY.get(name)
        if entry is None or entry["run"] is None:
            raise ValueError(f"Unknown tool: {name}")
        read_only = bool(entry["read_only"])
        backend_name = "back" if name == "go_back" else name
        arguments: dict[str, object] = dict(inp)
        target: TargetHandle | None = None
        if name in {"click", "type", "type_otp", "select_option"}:
            target = cls._target(state, inp.get("index"))
            arguments.pop("index", None)
        elif name == "fill_form":
            fields: list[dict[str, object]] = []
            raw_fields = inp.get("fields")
            if not isinstance(raw_fields, list):
                raise ValueError("fill_form requires a fields list")
            for field in raw_fields:
                if not isinstance(field, dict):
                    raise ValueError("fill_form fields must be objects")
                converted = dict(field)
                converted["target"] = cls._target(state, field.get("index"))
                converted.pop("index", None)
                fields.append(converted)
            arguments = {"fields": fields}
        elif name == "click_at":
            screenshot = state.screenshot
            if screenshot is None:
                raise ValueError("current observation has no screenshot metadata")
            arguments.update(
                observation_id=state.observation_id, screenshot_id=screenshot.screenshot_id
            )
        return BrowserAction(backend_name, arguments, target=target, read_only=read_only)

    async def _maybe_steer(self, task: str, actions: list[str], state_text: str) -> bool:
        verdict = await supervise(self.client, task, actions[-8:], state_text)
        if not (verdict["looping"] and verdict["advice"].strip()):
            return False
        advice = verdict["advice"].strip()
        self._write(f"🧭 supervisor: {advice}")
        html = ""
        try:
            result = await self.browser.execute(BrowserAction("get_html", read_only=True))
            html = str(result.details.get("html", ""))
        except Exception:
            pass
        self.messages.append(
            {
                "role": "user",
                "content": prompts.SUPERVISOR_INJECT.format(advice=advice, html=html),
            }
        )
        return True

    async def run(self, task: str, ask: AskFn) -> TaskResult:
        if not self._started or self._invalidated:
            raise RuntimeError("agent browser session is not running")
        self._task_interventions = []
        try:
            return await self._run_task(task, ask)
        except asyncio.CancelledError:
            self._invalidated = True
            try:
                async with asyncio.timeout(self.browser.config.timeouts.shutdown):
                    await asyncio.shield(
                        self.browser.invalidate(asyncio.CancelledError("task cancelled"))
                    )
            except BaseException:
                pass
            raise

    async def _run_task(self, task: str, ask: AskFn) -> TaskResult:
        state = await self.browser.observe()
        state_text = render_state(state)
        memory = format_memory(self.memory)
        self.messages.append(
            {
                "role": "user",
                "content": (f"What you know about this user:\n{memory}\n\n" if memory else "")
                + f"Task: {task}{prompts.PAGE_MARK}{state_text}",
            }
        )
        actions: list[str] = []
        state_sigs: list[str] = []
        last_steer = -99
        last_action_sig: str | None = None
        last_state_sig: str | None = None
        stuck_repeats = 0
        confirmed: set[str] = set()

        for step in range(1, MAX_STEPS + 1):
            if self._has_verification(state):
                state = await self._pause_for_verification(ask)
                state_text = render_state(state)
            await self._capture_screenshot()
            self._compact_history()
            response = await self._complete()
            self._debug_tokens(step, response)
            message = response.choices[0].message if response.choices else None
            if message is None:
                return self._result("(no response from model)")
            dumped = (
                message.model_dump(exclude_none=True)
                if hasattr(message, "model_dump")
                else {
                    "role": "assistant",
                    "content": getattr(message, "content", None),
                    "tool_calls": getattr(message, "tool_calls", None),
                }
            )
            self.messages.append(dumped)
            if message.content and message.content.strip():
                self._write(f"💭 {message.content.strip()}")
            calls = [
                call
                for call in (message.tool_calls or [])
                if getattr(call, "type", "function") == "function"
            ]
            if not calls:
                return self._result(
                    (message.content or "").strip() or "(agent stopped without a final answer)"
                )

            acted = False
            for call in calls:
                name = call.function.name
                try:
                    inp = json.loads(call.function.arguments) if call.function.arguments else {}
                except (TypeError, json.JSONDecodeError):
                    inp = {}
                self._write(f"🔧 {name}({json.dumps(inp)})  [step {step}/{MAX_STEPS}]")
                if name == "done":
                    self._reply(call, "done")
                    return self._result(str(inp.get("answer", "(no answer provided)")))
                meta = await self._meta_tool_result(name, inp, ask)
                if meta is not None:
                    self._reply(call, meta)
                    continue

                target_label = self._confirm_target(name, inp, state)
                confirmation_key = f"{name}:{target_label.casefold()}" if target_label else None
                if target_label and confirmation_key not in confirmed:
                    self._write(f"🛑 Consequential action detected: {target_label}")
                    answer = await self._ask(
                        ask,
                        prompts.CONFIRM_ASK.format(label=target_label),
                        "confirmation",
                    )
                    if not re.match(r"\s*(y|yes|ok|sure|proceed|confirm|go)\b", answer, re.I):
                        self._reply(call, prompts.CONFIRM_DECLINED.format(label=target_label))
                        acted = True
                        continue
                    confirmed.add(confirmation_key)

                action_sig = f"{name} {json.dumps(inp, sort_keys=True)}"
                refusing = action_sig == last_action_sig and stuck_repeats >= 2
                if refusing:
                    result_text = prompts.REPEAT_REFUSAL.format(action=action_sig)
                else:
                    try:
                        action = self._browser_action(name, inp, state)
                        result = await self.browser.execute(action)
                        result_text = render_action_result(result)
                        if result.observation is not None:
                            state = result.observation
                    except asyncio.CancelledError:
                        raise
                    except Exception as error:
                        result_text = json.dumps(
                            {
                                "status": OutcomeStatus.FAILED.value,
                                "message": f"Action failed: {error}",
                                "retryable": False,
                            },
                            sort_keys=True,
                        )

                if name in READ_TOOLS:
                    actions.append(name)
                    self._reply(call, result_text)
                else:
                    state_text = render_state(state)
                    signature = page_signature(state)
                    stuck, stuck_repeats = repeat_guard_step(
                        action_sig,
                        signature,
                        last_action_sig,
                        last_state_sig,
                        state_sigs[-5:],
                        stuck_repeats,
                    )
                    last_action_sig, last_state_sig = action_sig, signature
                    if stuck and not refusing:
                        result_text = prompts.STUCK_WARNING + result_text
                    actions.append(f"{action_sig} @ {state.url}")
                    state_sigs.append(signature)
                    self._reply(call, f"{result_text}{prompts.PAGE_MARK}{state_text}")
                acted = True

            if acted:
                due = step % CHECK_EVERY == 0
                suspect = looks_like_loop(actions) or looks_stalled(state_sigs)
                if (
                    (due or suspect)
                    and step - last_steer >= 2
                    and await self._maybe_steer(task, actions, state_text)
                ):
                    last_steer = step
        return self._result(f"Reached the step limit ({MAX_STEPS}) without finishing.")

    def _result(self, answer: str) -> TaskResult:
        interventions = tuple(self._task_interventions)
        outcome = AutomationOutcome.FAILED if interventions else AutomationOutcome.SUCCEEDED
        return TaskResult(answer, outcome, interventions)

    def _reply(self, call: Any, content: str) -> None:
        self.messages.append({"role": "tool", "tool_call_id": call.id, "content": content})

    def _debug_tokens(self, step: int, response: Any) -> None:
        if not DEBUG_TOKENS:
            return
        usage = getattr(response, "usage", None)
        if usage:
            self._write(
                f"⚙️  tokens step {step}: prompt={usage.prompt_tokens} "
                f"completion={usage.completion_tokens} total={usage.total_tokens}"
            )
