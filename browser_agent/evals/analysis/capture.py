# Throwaway capture patch for error analysis (shreverr/browser-agent#32).
# pyright: reportUnknownArgumentType=false
# pyright: reportUnknownMemberType=false
# pyright: reportUnknownVariableType=false
# pyright: reportAttributeAccessIssue=false
"""Record one Trial as an ea-1 Trace (see TRACE_FORMAT.md) around the unchanged Agent.

The Agent keeps its module-global configuration. Recording happens at three seams:
a proxy chat client (model calls), a proxy browser session (observations and
structured action results), and a subclass overriding the Agent's private hooks
(compaction, user exchanges, replies, interventions, memory writes).
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import enum
import hashlib
import json
import re
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from ... import prompts
from ...agent import Agent, _control_text, render_state
from ...browser import ActionResult, BrowserAction, Observation
from ...config import CONFIRM_KEYWORDS

SCHEMA_VERSION = "ea-1"
INLINE_LIMIT = 64 * 1024
# Research #36 T19: the default gate misses the labels these live sites use.
EXTRA_CONFIRM_WORDS = (
    "book now", "reserve", "add to cart", "proceed to buy", "checkout",
    "continue to payment", "send", "submit", "post",
)  # fmt: skip
CONFIRM_WORDS = tuple(dict.fromkeys([*CONFIRM_KEYWORDS, *EXTRA_CONFIRM_WORDS]))


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def plain(value: object) -> object:
    """Convert browser dataclasses, enums and mapping proxies into JSON-able data."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: plain(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, enum.Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [plain(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if hasattr(value, "model_dump"):
        return plain(value.model_dump(exclude_none=True))
    return str(value)


def canonical(value: object) -> bytes:
    return json.dumps(
        plain(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()


class BlobStore:
    def __init__(self, root: Path) -> None:
        self.root = root

    def put(self, data: bytes, media_type: str) -> dict[str, object]:
        digest = hashlib.sha256(data).hexdigest()
        path = self.root / "sha256" / digest[:2] / digest
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(data)
            tmp.replace(path)
        return {"sha256": digest, "bytes": len(data), "media_type": media_type}

    def text(self, text: str) -> dict[str, object]:
        return self.put(text.encode(), "text/plain")

    def json(self, value: object) -> dict[str, object]:
        return self.put(canonical(value), "application/json")


class TraceWriter:
    """Streams events.jsonl and owns the trial.json header."""

    def __init__(self, trial_dir: Path, blobs: BlobStore, header: dict[str, Any]) -> None:
        self.dir = trial_dir
        self.blobs = blobs
        self.header = header
        self.step = 0
        self._seq = 0
        self._t0 = time.monotonic()
        trial_dir.mkdir(parents=True, exist_ok=True)
        self._events = (trial_dir / "events.jsonl").open("a", encoding="utf-8")
        self.write_header()

    def elapsed(self) -> float:
        return time.monotonic() - self._t0

    def emit(self, type_: str, **fields: object) -> None:
        event = {
            "seq": self._seq,
            "t_wall": _now(),
            "t_mono": round(self.elapsed(), 3),
            "step": self.step,
            "type": type_,
            **{key: plain(value) for key, value in fields.items()},
        }
        self._seq += 1
        self._events.write(json.dumps(event, ensure_ascii=False) + "\n")
        self._events.flush()

    def write_header(self) -> None:
        tmp = self.dir / "trial.json.tmp"
        tmp.write_text(json.dumps(self.header, indent=2, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.dir / "trial.json")

    def close(self) -> None:
        self._events.close()


class RecordingBrowser:
    """Proxy BrowserSession that records every observation and structured result."""

    def __init__(self, inner: Any, trace: TraceWriter) -> None:
        self._inner = inner
        self._trace = trace
        self.last_result: ActionResult | None = None
        self.last_observation_id: str | None = None
        self.suppress = False

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def record_observation(self, observation: Observation) -> None:
        if observation.observation_id == self.last_observation_id:
            return
        self.last_observation_id = observation.observation_id
        self._trace.emit(
            "observation",
            observation_id=observation.observation_id,
            url=observation.url,
            title=observation.title,
            rendered_ref=self._trace.blobs.text(render_state(observation)),
            structured_ref=self._trace.blobs.json(observation),
            n_controls=len(observation.controls),
            truncated=observation.truncated,
        )

    async def observe(self) -> Observation:
        observation = await self._inner.observe()
        self.record_observation(observation)
        return observation

    async def execute(self, action: BrowserAction) -> ActionResult:
        result = await self._inner.execute(action)
        if result.observation is not None:
            self.record_observation(result.observation)
        if result.error_code == "domain_blocked":
            self._trace.emit(
                "blocked_navigation", url=action.arguments.get("url"), message=result.message
            )
        if not self.suppress:
            self.last_result = result
        return result


class RecordingCompletions:
    def __init__(self, inner: Any, recorder: RecordingAgent) -> None:
        self._inner = inner
        self._recorder = recorder

    async def create(self, **kwargs: Any) -> Any:
        return await self._recorder.record_model_call(self._inner, kwargs)


class RecordingClient:
    def __init__(self, inner: Any, recorder: RecordingAgent) -> None:
        self._inner = inner
        completions = RecordingCompletions(inner.chat.completions, recorder)
        self.chat = type("Chat", (), {"completions": completions})()


class CaptchaEncountered(Exception):
    """A verification challenge ends an error-analysis Trial as infra."""


class RecordingAgent(Agent):
    """Agent whose private hooks also write the Trace. Behaviour is unchanged."""

    trace: TraceWriter
    context_lengths: dict[str, int]
    exchange_source: str

    def attach(self, trace: TraceWriter, context_lengths: dict[str, int]) -> None:
        self.trace = trace
        self.context_lengths = context_lengths
        self.exchange_source = "policy"
        self.memory_writes: list[dict[str, str]] = []
        self.done_called = False
        self.unscripted = False
        self.peak_context_pct = 0.0
        self.cost = 0.0
        self.tokens = {"prompt": 0, "completion": 0}
        self.calls = {"agent": 0, "supervisor": 0}
        self.recording_browser = RecordingBrowser(self.browser, trace)
        self.browser = self.recording_browser  # type: ignore[assignment]
        self.client = RecordingClient(self.client, self)

    # --- model calls ---------------------------------------------------------

    async def record_model_call(self, completions: Any, kwargs: dict[str, Any]) -> Any:
        role = "agent" if kwargs.get("tools") else "supervisor"
        if role == "agent":
            self.trace.step += 1
        messages = kwargs.get("messages") or []
        message_refs = [self.trace.blobs.json(message) for message in messages]
        tools = kwargs.get("tools")
        request = {key: value for key, value in kwargs.items() if key != "extra_body"}
        extra_body = dict(kwargs.get("extra_body") or {})
        extra_body.setdefault("usage", {"include": True})
        started = time.monotonic()
        error: str | None = None
        response: Any = None
        try:
            response = await completions.create(**kwargs, extra_body=extra_body)
            return response
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            self.calls[role] += 1
            self._emit_model_call(
                role, kwargs, message_refs, tools, request, response, error, started
            )

    def _emit_model_call(
        self,
        role: str,
        kwargs: dict[str, Any],
        message_refs: list[dict[str, object]],
        tools: Any,
        request: dict[str, Any],
        response: Any,
        error: str | None,
        started: float,
    ) -> None:
        usage = plain(getattr(response, "usage", None)) or {}
        assert isinstance(usage, dict)
        prompt_tokens = int(usage.get("prompt_tokens") or 0)
        completion_tokens = int(usage.get("completion_tokens") or 0)
        details = usage.get("completion_tokens_details") or {}
        prompt_details = usage.get("prompt_tokens_details") or {}
        cost = usage.get("cost")
        served = getattr(response, "model", None)
        extra = getattr(response, "model_extra", None) or {}
        context_length = self.context_lengths.get(str(kwargs.get("model")))
        context_pct = (
            round(100 * prompt_tokens / context_length, 2)
            if context_length and prompt_tokens
            else None
        )
        choice = response.choices[0] if response is not None and response.choices else None
        message = choice.message if choice is not None else None
        raw_calls = [
            {"id": call.id, "name": call.function.name, "arguments": call.function.arguments}
            for call in (getattr(message, "tool_calls", None) or [])
            if getattr(call, "type", "function") == "function"
        ]
        self.tokens["prompt"] += prompt_tokens
        self.tokens["completion"] += completion_tokens
        if isinstance(cost, (int, float)):
            self.cost += float(cost)
        if context_pct is not None and role == "agent":
            self.peak_context_pct = max(self.peak_context_pct, context_pct)
        self.trace.emit(
            "model_call",
            role=role,
            message_refs=message_refs,
            tools_ref=self.trace.blobs.json(tools) if tools else None,
            request_sha256=hashlib.sha256(canonical(request)).hexdigest(),
            requested_model=kwargs.get("model"),
            served_model=served,
            provider=extra.get("provider"),
            usage={
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "reasoning_tokens": (details or {}).get("reasoning_tokens"),
                "cached_tokens": (prompt_details or {}).get("cached_tokens"),
            },
            cost_usd=cost,
            context_length=context_length,
            context_pct=context_pct,
            latency_ms=round(1000 * (time.monotonic() - started)),
            finish_reason=getattr(choice, "finish_reason", None),
            retries=None,
            content=getattr(message, "content", None),
            raw_tool_calls=raw_calls,
            error=error,
        )
        if role != "agent":
            return
        for call in raw_calls:
            try:
                arguments = json.loads(call["arguments"]) if call["arguments"] else {}
            except (TypeError, json.JSONDecodeError):
                arguments = {"_raw": call["arguments"]}
            if call["name"] == "done":
                self.done_called = True
            self.trace.emit("tool_call", call_id=call["id"], name=call["name"], arguments=arguments)

    # --- agent hooks ---------------------------------------------------------

    @staticmethod
    def _confirm_target(name: str, inp: dict[str, Any], state: Observation) -> str | None:
        """Analysis-only gate: word-boundary keywords, and `type(submit=true)` gated too."""
        if name == "type" and inp.get("submit") or name == "click":
            indices = [inp.get("index")]
        elif name == "fill_form":
            indices = [
                field.get("index")
                for field in inp.get("fields") or []
                if isinstance(field, dict) and field.get("submit")
            ]
        else:
            return None
        for value in indices:
            try:
                index = int(value)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                continue
            if not 0 <= index < len(state.controls):
                continue
            label = _control_text(state.controls[index])
            if any(re.search(rf"\b{re.escape(word)}\b", label, re.I) for word in CONFIRM_WORDS):
                return label
        return None

    def _compact_history(self) -> None:
        before = [message.get("content") for message in self.messages]
        super()._compact_history()
        replaced = [
            {
                "index": index,
                "before_ref": self.trace.blobs.json(old),
                "after_ref": self.trace.blobs.json(message.get("content")),
            }
            for index, (old, message) in enumerate(zip(before, self.messages, strict=True))
            if old != message.get("content")
        ]
        if replaced:
            self.trace.emit("compaction", replaced=replaced)

    def _disable_vision(self) -> None:
        super()._disable_vision()
        self.trace.emit("vision_fallback", error="model rejected image input")

    async def _ask(self, ask: Any, question: str, reason: str) -> str:
        kind = {"user_input": "ask_user"}.get(reason, reason)
        self.exchange_source = "policy"
        reply = await super()._ask(ask, question, reason)
        if self.exchange_source == "unscripted_fallback":
            self.unscripted = True
        self.trace.emit(
            "user_exchange",
            kind=kind,
            question=question,
            reply=reply,
            source=self.exchange_source,
            counts_as_intervention=True,
        )
        return reply

    async def _meta_tool_result(self, name: str, inp: dict[str, Any], ask: Any) -> str | None:
        result = await super()._meta_tool_result(name, inp, ask)
        if name in {"remember", "forget"}:
            write = {
                "op": name,
                "key": str(inp.get("key", "")).strip(),
                "value": str(inp.get("value", "")).strip(),
            }
            self.memory_writes.append(write)
            self.trace.emit("memory_write", **write)
        return result

    async def _maybe_steer(self, task: str, actions: list[str], state_text: str) -> bool:
        before = len(self.messages)
        self.recording_browser.suppress = True
        try:
            steered = await super()._maybe_steer(task, actions, state_text)
        finally:
            self.recording_browser.suppress = False
        if steered:
            injected = self.messages[before]["content"] if len(self.messages) > before else ""
            self.trace.emit(
                "intervention",
                source="supervisor",
                trigger="cadence_or_detection",
                injected_text_ref=self.trace.blobs.text(str(injected)),
                action_refused=False,
            )
        return steered

    def _reply(self, call: Any, content: str) -> None:
        super()._reply(call, content)
        name = call.function.name
        result = self.recording_browser.last_result
        self.recording_browser.last_result = None
        refused = content.startswith(prompts.REPEAT_REFUSAL.split("`", 1)[0])
        if refused or content.startswith(prompts.STUCK_WARNING):
            self.trace.emit(
                "intervention",
                source="repeat_guard" if refused else "stuck_warning",
                trigger="repeat",
                injected_text_ref=None,
                action_refused=refused,
            )
        fields: dict[str, object] = {"call_id": call.id, "name": name}
        if result is None or refused:
            fields.update(status="handled", error_code=None, message=None, structured_ref=None)
            fields["observation_id"] = None
        else:
            structured = dataclasses.replace(result, observation=None)
            fields.update(
                status=result.status.value,
                error_code=result.error_code,
                message=result.message,
                structured_ref=self.trace.blobs.json(structured),
                observation_id=result.observation.observation_id if result.observation else None,
            )
        if len(content.encode()) <= INLINE_LIMIT:
            fields["rendered"] = content
        else:
            fields["rendered"] = None
            fields["rendered_ref"] = self.trace.blobs.text(content)
        self.trace.emit("action_result", **fields)
