from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from browser_agent.browser import Observation, SemanticControl, TargetHandle
from browser_agent.evals.analysis.capture import BlobStore
from browser_agent.evals.analysis.run import base_header, run_trial
from tests.test_async_agent import FakeClient, FakeSession, observation, response, tool_call


def order_page() -> Observation:
    base = observation(9)
    return Observation(
        base.observation_id,
        base.active_target_id,
        base.url,
        base.title,
        base.document_generation,
        base.frame_generations,
        base.viewport,
        controls=(
            SemanticControl(TargetHandle(base.observation_id, "c1"), "button", "Place order"),
        ),
    )


def events(trial_dir: Path) -> list[dict[str, Any]]:
    lines = (trial_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines]


@pytest.mark.asyncio
async def test_trial_records_trace_with_simulated_user(tmp_path: Path) -> None:
    session = FakeSession()
    client = FakeClient(
        [
            response(tool_call("click", '{"index": 0}', "c-1")),
            response(tool_call("ask_user", '{"question": "Which dates?"}', "c-2")),
            response(tool_call("ask_user", '{"question": "Budget?"}', "c-3")),
            response(tool_call("remember", '{"key": "k", "value": "v"}', "c-4")),
            response(tool_call("done", '{"answer": "found it"}', "c-5")),
        ]
    )
    task = {
        "id": "demo",
        "text": "Find a campsite",
        "memory": {"name": "Asha Rao"},
        "replies": [{"id": "dates", "match": "date", "reply": "20-22 October"}],
    }
    blobs = BlobStore(tmp_path / "blobs")
    trial_dir = tmp_path / "trials" / "demo.t1.a1"
    header = base_header(task, "demo.t1.a1", "b", 1, blobs)

    header = await run_trial(task, trial_dir, header, blobs, {}, 30, session=session, client=client)

    assert header["status"] == "completed"
    assert header["terminal_reason"] == "done"
    assert header["answer"] == "found it"
    assert header["automation_outcome"] == "failed"
    assert header["unscripted_fallback"] is True
    assert header["memory_writes"] == [{"op": "remember", "key": "k", "value": "v"}]
    assert header["totals"]["steps"] == 5
    assert json.loads((trial_dir / "trial.json").read_text())["status"] == "completed"

    trace = events(trial_dir)
    assert [event["seq"] for event in trace] == list(range(len(trace)))
    types = [event["type"] for event in trace]
    assert types.count("model_call") == 5
    assert types[-1] == "trial_end"
    exchanges = [event for event in trace if event["type"] == "user_exchange"]
    assert [(e["reply"], e["source"]) for e in exchanges] == [
        ("20-22 October", "scripted:dates"),
        ("", "unscripted_fallback"),
    ]
    click = next(e for e in trace if e["type"] == "action_result" and e["name"] == "click")
    assert click["status"] == "succeeded"
    assert click["structured_ref"] is not None
    assert "Current page:" in click["rendered"]
    first_call = next(e for e in trace if e["type"] == "model_call")
    for ref in first_call["message_refs"]:
        digest = ref["sha256"]
        assert (tmp_path / "blobs" / "sha256" / digest[:2] / digest).exists()
    first_message = json.loads(
        (
            tmp_path
            / "blobs"
            / "sha256"
            / first_call["message_refs"][1]["sha256"][:2]
            / first_call["message_refs"][1]["sha256"]
        ).read_text()
    )
    assert "Asha Rao" in first_message["content"]


@pytest.mark.asyncio
async def test_confirmation_is_always_declined(tmp_path: Path) -> None:
    session = FakeSession()
    session.current = order_page()
    client = FakeClient(
        [
            response(tool_call("click", '{"index": 0}', "c-1")),
            response(tool_call("done", '{"answer": "stopped"}', "c-2")),
        ]
    )
    task = {"id": "order", "text": "Buy it"}
    blobs = BlobStore(tmp_path / "blobs")
    trial_dir = tmp_path / "trials" / "order.t1.a1"

    await run_trial(
        task,
        trial_dir,
        base_header(task, "order.t1.a1", "b", 1, blobs),
        blobs,
        {},
        30,
        session=session,
        client=client,
    )

    assert session.actions == []
    trace = events(trial_dir)
    exchange = next(e for e in trace if e["type"] == "user_exchange")
    assert (exchange["kind"], exchange["reply"], exchange["source"]) == (
        "confirmation",
        "no",
        "policy",
    )
    declined = next(e for e in trace if e["type"] == "action_result" and e["name"] == "click")
    assert declined["status"] == "handled"
    assert "DECLINED" in declined["rendered"]


@pytest.mark.asyncio
async def test_verification_challenge_ends_trial_as_captcha_infra(tmp_path: Path) -> None:
    session = FakeSession()
    session.current = observation(1, title="Verify you are human")
    task = {"id": "wall", "text": "Search"}
    blobs = BlobStore(tmp_path / "blobs")
    trial_dir = tmp_path / "trials" / "wall.t1.a1"

    header = await run_trial(
        task,
        trial_dir,
        base_header(task, "wall.t1.a1", "b", 1, blobs),
        blobs,
        {},
        30,
        session=session,
        client=FakeClient([]),
    )

    assert header["status"] == "infra_error"
    assert header["infra"]["class"] == "captcha"
    assert events(trial_dir)[-1]["type"] == "trial_end"


def test_relative_dates_resolve_at_trial_time() -> None:
    import datetime as dt

    from browser_agent.evals.analysis.dates import resolve_task

    today = dt.date(2026, 9, 27)  # a Sunday
    task = resolve_task(
        {
            "id": "d",
            "text": "Arrive {second_friday_next_month}, leave {today+2d}; {next_saturday}; {today+14m}",
            "replies": [{"id": "r", "match": "when", "reply": "{last_sunday_next_month}"}],
        },
        today,
    )
    assert "Friday 9 October 2026" in task["text"]
    assert "Tuesday 29 September 2026" in task["text"]
    assert "Saturday 3 October 2026" in task["text"]
    assert "27 November 2027" in task["text"]
    assert task["replies"][0]["reply"] == "Sunday 25 October 2026"
    assert task["text_template"].startswith("Arrive {second_friday_next_month}")


def test_analysis_gate_uses_word_boundaries_and_gates_submit_typing() -> None:
    from browser_agent.evals.analysis.capture import RecordingAgent

    base = observation(3)
    labels = ["Add to Cart", "Postcode", "Search", "Reserve"]
    page = Observation(
        base.observation_id,
        base.active_target_id,
        base.url,
        base.title,
        base.document_generation,
        base.frame_generations,
        base.viewport,
        controls=tuple(
            SemanticControl(TargetHandle(base.observation_id, f"c{i}"), "button", label)
            for i, label in enumerate(labels)
        ),
    )
    gate = RecordingAgent._confirm_target
    assert gate("click", {"index": 0}, page) is not None
    assert gate("click", {"index": 1}, page) is None
    assert gate("type", {"index": 3, "text": "x", "submit": True}, page) is not None
    assert gate("type", {"index": 3, "text": "x"}, page) is None
    assert gate("click", {"index": 2}, page) is None
