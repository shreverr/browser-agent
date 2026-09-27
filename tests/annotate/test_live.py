"""Live updates: writes publish change events to every SSE subscriber."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from typing import Any

from fastapi import FastAPI
from fastapi.testclient import TestClient

from browser_agent.evals.annotate.broadcast import Broadcaster


def test_broadcaster_fans_out_across_threads() -> None:
    async def main() -> list[dict[str, Any]]:
        bc = Broadcaster()
        q1, q2 = bc.subscribe(), bc.subscribe()
        t = threading.Thread(target=bc.publish, args=({"kind": "note"},))
        t.start()
        t.join()
        got = [await asyncio.wait_for(q1.get(), 1), await asyncio.wait_for(q2.get(), 1)]
        bc.unsubscribe(q1)
        assert bc.subscriber_count == 1
        return got

    assert asyncio.run(main()) == [{"kind": "note"}, {"kind": "note"}]


def test_a_stalled_subscriber_is_told_to_resync() -> None:
    async def main() -> list[dict[str, Any]]:
        bc = Broadcaster(max_queue=2)
        q = bc.subscribe()
        for i in range(3):
            bc.publish({"kind": "note", "i": i})
        await asyncio.sleep(0.01)
        return [q.get_nowait() for _ in range(q.qsize())]

    assert asyncio.run(main()) == [{"kind": "resync"}]


def test_writes_publish_events_after_commit(
    app: FastAPI, uploaded: Callable[..., str], as_user: Callable[[str], TestClient]
) -> None:
    seen: list[dict[str, Any]] = []
    bc: Broadcaster = app.state.broadcaster
    bc.publish = seen.append  # type: ignore[method-assign]
    tid = uploaded("a.t1.a1")
    alice = as_user("alice")
    alice.put(f"/api/trials/{tid}/note", json={"text": "hi"})
    mode = alice.post("/api/modes", json={"name": "A"}).json()["id"]
    alice.post("/api/modes", json={"name": "A"})  # 409: nothing committed, nothing published
    kinds = [(e["kind"], e["actor"]) for e in seen]
    assert kinds == [("trial", "uploader"), ("note", "human:alice"), ("mode", "human:alice")]
    assert seen[-1]["mode_id"] == mode
