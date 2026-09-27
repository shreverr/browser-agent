"""Modes: only humans create, rename, merge. Renames and merges keep ids and counts."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select

from browser_agent.evals.annotate import service
from browser_agent.evals.annotate.models import Assignment, Example, Mode


def _note(c: TestClient, tid: str, text: str = "x") -> int:
    r = c.put(f"/api/trials/{tid}/note", json={"verdict": "fail", "text": text})
    assert r.status_code == 200, r.text
    return r.json()["id"]


def _mode(c: TestClient, name: str, **kw: Any) -> str:
    r = c.post("/api/modes", json={"name": name, **kw})
    assert r.status_code == 201, r.text
    return r.json()["id"]


def _assign(c: TestClient, note_id: int, mode_id: str) -> dict[str, Any]:
    r = c.post("/api/assignments", json={"note_id": note_id, "mode_id": mode_id})
    assert r.status_code == 201, r.text
    return r.json()


def _counts(c: TestClient) -> dict[str, int]:
    return {m["name"]: m["trial_count"] for m in c.get("/api/modes").json()["modes"]}


def test_create_and_rename_keep_ids_and_counts(
    uploaded: Callable[..., str], as_user: Callable[[str], TestClient]
) -> None:
    t1 = uploaded("a.t1.a1")
    alice = as_user("alice")
    mid = _mode(alice, "Wrong filter", definition="applied the wrong filter", gradability="code")
    assert alice.post("/api/modes", json={"name": "wrong  FILTER"}).status_code == 409
    assert alice.post("/api/modes", json={"name": "x", "gradability": "vibes"}).status_code == 422
    _assign(alice, _note(alice, t1), mid)
    assert _counts(alice) == {"Wrong filter": 1}

    r = alice.patch(f"/api/modes/{mid}", json={"name": "Filter misapplied", "gradability": "judge"})
    assert r.status_code == 200 and r.json()["id"] == mid
    modes = alice.get("/api/modes").json()["modes"]
    assert [(m["id"], m["name"], m["trial_count"], m["gradability"]) for m in modes] == [
        (mid, "Filter misapplied", 1, "judge")
    ]
    audit = alice.get("/api/audit").json()["audit"]
    assert audit[0]["action"] == "mode.rename" and audit[0]["actor"] == "human:alice"
    assert audit[0]["before"]["name"] == "Wrong filter"


def test_merge_moves_assignments_dedupes_and_preserves_counts(
    uploaded: Callable[..., str], as_user: Callable[[str], TestClient]
) -> None:
    t1, t2, t3 = uploaded("a.t1.a1"), uploaded("b.t1.a1"), uploaded("c.t1.a1")
    alice, bob = as_user("alice"), as_user("bob")
    a = _mode(alice, "Clicked ad")
    b = _mode(alice, "Clicked sponsored result")
    n1, n2 = _note(alice, t1), _note(alice, t2)
    n2b, n3 = _note(bob, t2), _note(bob, t3)
    _assign(alice, n1, a)
    _assign(alice, n2, a)
    _assign(alice, n2, b)  # n2 is in both: must dedupe on merge
    _assign(bob, n2b, b)
    _assign(bob, n3, b)
    alice.post(f"/api/modes/{a}/examples", json={"trial_id": t1, "seq": 1, "caption": "ad"})
    assert _counts(alice) == {"Clicked ad": 2, "Clicked sponsored result": 2}

    r = alice.post(f"/api/modes/{a}/merge", json={"into": b})
    assert r.status_code == 200, r.text
    assert r.json()["moved"] == 1 and r.json()["deduped"] == 1
    modes = alice.get("/api/modes").json()["modes"]
    assert [(m["id"], m["trial_count"]) for m in modes] == [(b, 3)]
    assert modes[0]["merged_from"] == [{"id": a, "name": "Clicked ad"}]
    assert [e["trial_id"] for e in modes[0]["examples"]] == [t1]
    merged = {m["id"]: m for m in alice.get("/api/modes?include_merged=true").json()["modes"]}
    assert merged[a]["merged_into"] == b

    # merged modes are frozen, their name is free again, and the id still resolves
    assert alice.patch(f"/api/modes/{a}", json={"name": "zzz"}).status_code == 409
    assert alice.post(f"/api/modes/{b}/merge", json={"into": a}).status_code == 409
    assert alice.post(f"/api/modes/{b}/merge", json={"into": b}).status_code == 422
    _mode(alice, "Clicked ad")
    rows = {t["trial_id"]: t for t in alice.get(f"/api/trials?mode={a}").json()["trials"]}
    assert set(rows) == {t1, t2, t3}


def test_merge_chains_are_flattened(
    uploaded: Callable[..., str], as_user: Callable[[str], TestClient]
) -> None:
    t1 = uploaded("a.t1.a1")
    alice = as_user("alice")
    a, b, c = _mode(alice, "A"), _mode(alice, "B"), _mode(alice, "C")
    _assign(alice, _note(alice, t1), c)
    alice.post(f"/api/modes/{c}/merge", json={"into": a})
    alice.post(f"/api/modes/{a}/merge", json={"into": b})
    merged = {m["name"]: m for m in alice.get("/api/modes?include_merged=true").json()["modes"]}
    assert merged["C"]["merged_into"] == b and merged["A"]["merged_into"] == b
    assert merged["B"]["trial_count"] == 1


def test_merge_is_transactional(
    app: FastAPI,
    uploaded: Callable[..., str],
    as_user: Callable[[str], TestClient],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    t1, t2 = uploaded("a.t1.a1"), uploaded("b.t1.a1")
    alice = as_user("alice")
    a, b = _mode(alice, "A"), _mode(alice, "B")
    _assign(alice, _note(alice, t1), a)
    _assign(alice, _note(alice, t2), a)
    alice.post(f"/api/modes/{a}/examples", json={"trial_id": t1})

    real_audit = service.Unit.audit

    def exploding_audit(self: service.Unit, action: str, *args: Any, **kw: Any) -> None:
        if action == "mode.merge":  # after every assignment and example was re-pointed
            raise RuntimeError("boom")
        real_audit(self, action, *args, **kw)

    monkeypatch.setattr(service.Unit, "audit", exploding_audit)
    client = TestClient(
        app, raise_server_exceptions=False, headers=dict(alice.headers), cookies=alice.cookies
    )
    assert client.post(f"/api/modes/{a}/merge", json={"into": b}).status_code == 500
    monkeypatch.setattr(service.Unit, "audit", real_audit)

    with app.state.sessions() as db:
        assert {x.mode_id for x in db.scalars(select(Assignment))} == {service.parse_uuid(a)}
        assert {x.mode_id for x in db.scalars(select(Example))} == {service.parse_uuid(a)}
        assert all(m.merged_into is None for m in db.scalars(select(Mode)))
    assert _counts(alice) == {"A": 2, "B": 0}


def test_examples(uploaded: Callable[..., str], as_user: Callable[[str], TestClient]) -> None:
    t1 = uploaded("a.t1.a1")
    alice = as_user("alice")
    m = _mode(alice, "A")
    e = alice.post(f"/api/modes/{m}/examples", json={"trial_id": t1, "seq": 2, "caption": "here"})
    assert e.status_code == 201
    again = alice.post(f"/api/modes/{m}/examples", json={"trial_id": t1, "seq": 2})
    assert again.json()["id"] == e.json()["id"]
    assert (
        alice.post(f"/api/modes/{m}/examples", json={"trial_id": t1, "seq": 99}).status_code == 422
    )
    assert alice.delete(f"/api/examples/{e.json()['id']}").status_code == 200
    assert alice.get("/api/modes").json()["modes"][0]["examples"] == []
