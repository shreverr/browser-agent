"""Claude proposes assignments with the token; humans accept or reject them."""

from __future__ import annotations

from collections.abc import Callable

from fastapi.testclient import TestClient

TID = "a.t1.a1"


def _setup(
    uploaded: Callable[..., str], as_user: Callable[[str], TestClient]
) -> tuple[TestClient, TestClient, int, str]:
    uploaded(TID)
    alice, bob = as_user("alice"), as_user("bob")
    note = alice.put(
        f"/api/trials/{TID}/note", json={"verdict": "fail", "text": "clicked an ad"}
    ).json()["id"]
    mode = alice.post("/api/modes", json={"name": "Clicked ad"}).json()["id"]
    return alice, bob, note, mode


def test_claude_proposes_and_a_human_accepts(
    uploaded: Callable[..., str], as_user: Callable[[str], TestClient], uploader: TestClient
) -> None:
    alice, bob, note, mode = _setup(uploaded, as_user)
    r = uploader.post(
        "/api/proposals",
        json={"proposals": [{"note_id": note, "mode_id": mode, "rationale": "mentions an ad"}]},
    )
    assert r.status_code == 201 and r.json()["created"] == 1
    (a,) = alice.get(f"/api/trials/{TID}/notes").json()["notes"][0]["assignments"]
    assert (a["state"], a["proposed_by"], a["rationale"]) == (
        "proposed",
        "claude",
        "mentions an ad",
    )
    assert alice.get("/api/modes").json()["modes"][0]["trial_count"] == 0  # proposals don't count
    assert alice.get("/api/modes").json()["modes"][0]["proposed_count"] == 1

    # the same pair again is skipped, never overwritten
    again = uploader.post(
        "/api/proposals",
        json={"proposals": [{"trial_id": TID, "author": "alice", "mode": "clicked AD"}]},
    )
    assert again.json()["created"] == 0 and again.json()["skipped"][0]["state"] == "proposed"

    r = bob.post(f"/api/assignments/{a['id']}/accept")  # any human may decide Claude's proposals
    assert (
        r.status_code == 200 and r.json()["state"] == "accepted" and r.json()["decided_by"] == "bob"
    )
    assert alice.get("/api/modes").json()["modes"][0]["trial_count"] == 1
    r = alice.post(
        f"/api/assignments/{a['id']}/reject", json={"rationale": "it was a sponsored result"}
    )
    assert r.json()["state"] == "rejected"
    assert alice.get("/api/modes").json()["modes"][0]["trial_count"] == 0
    assert alice.post(f"/api/assignments/{a['id']}/maybe").status_code == 404


def test_invalid_proposals_write_nothing(
    uploaded: Callable[..., str], as_user: Callable[[str], TestClient], uploader: TestClient
) -> None:
    alice, _, note, mode = _setup(uploaded, as_user)
    r = uploader.post(
        "/api/proposals",
        json={
            "proposals": [
                {"note_id": note, "mode_id": mode},
                {"note_id": note, "mode": "A brand new mode"},
            ]
        },
    )
    assert r.status_code == 422
    assert "cannot create modes" in r.json()["detail"]["errors"][0]["error"]
    assert alice.get("/api/assignments").json()["assignments"] == []
    assert uploader.post("/api/proposals", json={"proposals": []}).status_code == 422
    assert (
        alice.post(
            "/api/proposals", json={"proposals": [{"note_id": note, "mode_id": mode}]}
        ).status_code
        == 401
    )


def test_human_assignments(
    uploaded: Callable[..., str], as_user: Callable[[str], TestClient]
) -> None:
    alice, bob, note, mode = _setup(uploaded, as_user)
    # on my own note, an assignment is accepted straight away
    mine = alice.post("/api/assignments", json={"note_id": note, "mode_id": mode}).json()
    assert mine["state"] == "accepted" and mine["proposed_by"] == "human:alice"

    # on someone else's note it is a proposal only that note's author can decide
    other = bob.post("/api/modes", json={"name": "Gave up early"}).json()["id"]
    prop = bob.post("/api/assignments", json={"note_id": note, "mode_id": other}).json()
    assert prop["state"] == "proposed" and prop["proposed_by"] == "human:bob"
    assert bob.post(f"/api/assignments/{prop['id']}/accept").status_code == 403
    assert alice.post(f"/api/assignments/{prop['id']}/accept").json()["state"] == "accepted"

    # removal: my note or my proposal only
    carol = as_user("carol")
    assert carol.delete(f"/api/assignments/{mine['id']}").status_code == 403
    assert alice.delete(f"/api/assignments/{mine['id']}").status_code == 200
    states = [
        a["mode_name"] for a in alice.get("/api/assignments?state=accepted").json()["assignments"]
    ]
    assert states == ["Gave up early"]
