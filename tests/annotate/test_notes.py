"""One note per annotator per Trial; nobody clobbers anybody."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from browser_agent.evals.annotate.models import Audit, Note

TID = "ev-charger-barstow.t1.a1"


def test_each_author_has_their_own_note(
    app: FastAPI, uploaded: Callable[..., str], as_user: Callable[[str], TestClient]
) -> None:
    uploaded(TID)
    alice, bob = as_user("alice"), as_user("bob")
    a1 = alice.put(
        f"/api/trials/{TID}/note",
        json={"verdict": "fail", "first_bad_seq": 1, "text": "clicked the ad"},
    )
    assert a1.status_code == 200, a1.text
    b1 = bob.put(f"/api/trials/{TID}/note", json={"verdict": "pass_but_bad", "text": "slow"})
    a2 = alice.put(
        f"/api/trials/{TID}/note",
        json={"verdict": "fail", "first_bad_seq": 2, "text": "clicked the ad twice"},
    )
    assert a2.json()["id"] == a1.json()["id"] != b1.json()["id"]

    notes = {n["author"]: n for n in alice.get(f"/api/trials/{TID}/notes").json()["notes"]}
    assert notes["alice"]["text"] == "clicked the ad twice" and notes["alice"]["first_bad_seq"] == 2
    assert notes["bob"]["text"] == "slow" and notes["bob"]["verdict"] == "pass_but_bad"

    with app.state.sessions() as db:
        assert db.scalar(select(func.count()).select_from(Note)) == 2
        assert (
            db.scalar(select(func.count()).select_from(Audit).where(Audit.action == "note.upsert"))
            == 3
        )
        db.add(Note(trial_id=TID, author="alice", text="dup"))
        with pytest.raises(IntegrityError):
            db.flush()


def test_note_validation(
    uploaded: Callable[..., str], as_user: Callable[[str], TestClient]
) -> None:
    uploaded(TID, steps=2)  # 2 observations + 2 model calls + trial_end = 5 events
    alice = as_user("alice")
    assert alice.put(f"/api/trials/{TID}/note", json={"first_bad_seq": 5}).status_code == 422
    assert alice.put(f"/api/trials/{TID}/note", json={"first_bad_seq": 4}).status_code == 200
    assert alice.put(f"/api/trials/{TID}/note", json={"verdict": "meh"}).status_code == 422
    assert alice.put("/api/trials/nope.t1.a1/note", json={"text": "x"}).status_code == 404


def test_trial_list_shows_my_note_and_others(
    uploaded: Callable[..., str], as_user: Callable[[str], TestClient]
) -> None:
    uploaded(TID)
    uploaded("amazon-kettle.t1.a1")
    alice, bob = as_user("alice"), as_user("bob")
    bob.put(f"/api/trials/{TID}/note", json={"text": "wrong filter"})

    rows = {t["trial_id"]: t for t in alice.get("/api/trials").json()["trials"]}
    assert rows[TID]["my_note"] is None and rows[TID]["other_note_count"] == 1
    assert "task_type:research" in rows[TID]["cell"]
    assert rows[TID]["steps"] == 2 and rows[TID]["answer"] == "Found 3 chargers"

    alice.put(f"/api/trials/{TID}/note", json={"verdict": "fail", "text": "agree"})
    rows = {t["trial_id"]: t for t in alice.get("/api/trials").json()["trials"]}
    assert rows[TID]["my_note"]["verdict"] == "fail" and rows[TID]["note_count"] == 2

    def ids(q: str) -> list[str]:
        return [t["trial_id"] for t in alice.get(f"/api/trials?{q}").json()["trials"]]

    assert ids("mine=unnoted") == ["amazon-kettle.t1.a1"]
    assert ids("author=bob") == [TID]
    assert ids("has_note=false") == ["amazon-kettle.t1.a1"]
    assert ids("coded=uncoded") == ["amazon-kettle.t1.a1", TID]
    assert ids("status=done") == ["amazon-kettle.t1.a1", TID]
    assert ids("cell=map") == ["amazon-kettle.t1.a1", TID]
    assert ids("batch=nope") == []
