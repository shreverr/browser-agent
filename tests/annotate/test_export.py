"""taxonomy.yaml: counts follow merges; uncoded notes; inter-rater agreement."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import yaml
from fastapi.testclient import TestClient

from browser_agent.evals.annotate import cli


def test_export_counts_merged_modes_examples_uncoded_and_agreement(
    uploaded: Callable[..., str],
    as_user: Callable[[str], TestClient],
    uploader: TestClient,
    tmp_path: Path,
) -> None:
    t1, t2, t3, t4 = (uploaded(f"{x}.t1.a1") for x in "abcd")
    alice, bob = as_user("alice"), as_user("bob")

    def note(c: TestClient, tid: str, verdict: str, seq: int | None = None) -> int:
        return c.put(
            f"/api/trials/{tid}/note",
            json={"verdict": verdict, "first_bad_seq": seq, "text": f"{tid} note"},
        ).json()["id"]

    def mode(name: str, **kw: str) -> str:
        return alice.post("/api/modes", json={"name": name, **kw}).json()["id"]

    def assign(c: TestClient, n: int, m: str) -> None:
        assert c.post("/api/assignments", json={"note_id": n, "mode_id": m}).status_code == 201

    ad, sponsored, gave_up = (
        mode("Clicked ad", gradability="code"),
        mode("Clicked sponsored"),
        mode("Gave up", definition="stopped early"),
    )
    a1, a2, a3 = note(alice, t1, "fail", 1), note(alice, t2, "fail", 2), note(alice, t3, "ok")
    b1, b2 = note(bob, t1, "fail", 1), note(bob, t2, "pass_but_bad", 3)
    note(bob, t4, "fail")  # uncoded
    assign(alice, a1, ad)
    assign(alice, a2, sponsored)
    assign(alice, a2, gave_up)
    assign(bob, b1, sponsored)
    assign(bob, b2, gave_up)
    alice.post(
        f"/api/modes/{sponsored}/examples", json={"trial_id": t2, "seq": 2, "caption": "top result"}
    )
    # a proposal is not a member until accepted
    uploader.post("/api/proposals", json={"proposals": [{"note_id": a3, "mode_id": gave_up}]})
    alice.post(f"/api/modes/{ad}/merge", json={"into": sponsored})

    out = tmp_path / "taxonomy.yaml"
    cli.export(uploader, out)  # the CLI reads with the token
    data = yaml.safe_load(out.read_text())
    assert data["trace_schema_version"] == "ea-1"
    assert data["trials_uploaded"] == 4 and data["trials_noted"] == 4

    modes = {m["name"]: m for m in data["modes"]}
    assert set(modes) == {"Clicked sponsored", "Gave up"}  # the merged mode is folded in
    s = modes["Clicked sponsored"]
    assert s["count"] == 2 and s["trials"] == [t1, t2]  # t1 by both authors counts once
    assert s["merged_from"] == ["Clicked ad"]
    assert s["examples"] == [{"trial_id": t2, "seq": 2, "caption": "top result"}]
    assert modes["Gave up"] == {
        "id": gave_up,
        "name": "Gave up",
        "definition": "stopped early",
        "gradability": "unknown",
        "count": 1,
        "examples": [],
        "trials": [t2],
    }
    assert [data["modes"][0]["name"], data["modes"][1]["name"]] == ["Clicked sponsored", "Gave up"]

    uncoded = {(u["trial_id"], u["author"]) for u in data["uncoded"]}
    assert uncoded == {(t3, "alice"), (t4, "bob")}

    (pair,) = data["agreement"]
    assert pair["authors"] == ["alice", "bob"]
    assert pair["shared_trials"] == 2 and pair["coded_by_both"] == 2
    # t1: {sponsored} vs {sponsored} (after merge) -> same; t2: {sponsored, gave_up} vs {gave_up}
    assert pair["same_modes"] == 1 and pair["any_shared_mode"] == 2
    assert pair["mean_jaccard"] == 0.75
    assert pair["verdict_agreement"] == 0.5
    assert pair["first_bad_seq_agreement"] == 0.5

    r = alice.get("/api/taxonomy/export")
    assert r.headers["content-type"].startswith("application/yaml")
