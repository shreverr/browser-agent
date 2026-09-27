"""The upload contract from TRACE_FORMAT.md: blobs/missing, PUT blob, PUT trial."""

from __future__ import annotations

import dataclasses
import hashlib
import json
from collections.abc import Callable
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from browser_agent.evals.annotate import cli

from .conftest import FakeS3Client, TraceDir


def _files(d: Path) -> dict[str, tuple[str, bytes, str]]:
    return {
        "trial.json": ("trial.json", (d / "trial.json").read_bytes(), "application/json"),
        "events.jsonl": ("events.jsonl", (d / "events.jsonl").read_bytes(), "application/x-ndjson"),
    }


def test_upload_requires_the_bearer_token(anon: TestClient) -> None:
    assert anon.post("/api/blobs/missing", json={"sha256": []}).status_code == 401
    bad = anon.post(
        "/api/blobs/missing", json={"sha256": []}, headers={"Authorization": "Bearer nope"}
    )
    assert bad.status_code == 401


def test_blob_hash_is_verified(uploader: TestClient) -> None:
    data = b"hello"
    wrong = hashlib.sha256(b"other").hexdigest()
    r = uploader.put(f"/api/blobs/{wrong}", content=data, headers={"Content-Type": "text/plain"})
    assert r.status_code == 400
    assert r.json()["detail"]["error"] == "hash mismatch"

    sha = hashlib.sha256(data).hexdigest()
    assert uploader.post("/api/blobs/missing", json={"sha256": [sha]}).json() == {"missing": [sha]}
    r = uploader.put(f"/api/blobs/{sha}", content=data, headers={"Content-Type": "text/plain"})
    assert r.status_code == 200 and r.json()["created"] is True
    again = uploader.put(f"/api/blobs/{sha}", content=data, headers={"Content-Type": "text/plain"})
    assert again.json()["created"] is False
    assert uploader.post("/api/blobs/missing", json={"sha256": [sha]}).json() == {"missing": []}
    assert uploader.put("/api/blobs/NOTHEX", content=data).status_code == 422


def test_trial_with_missing_refs_is_rejected(uploader: TestClient, traces: TraceDir) -> None:
    d = traces.trial("ev-charger-barstow.t1.a1")
    r = uploader.put("/api/trials/ev-charger-barstow.t1.a1", files=_files(d))
    assert r.status_code == 422
    assert r.json()["detail"]["error"] == "refs not uploaded"
    assert len(r.json()["detail"]["missing"]) > 0


def test_cli_upload_is_idempotent_and_dedupes_blobs(
    uploader: TestClient, traces: TraceDir, s3: FakeS3Client
) -> None:
    a = traces.trial("ev-charger-barstow.t1.a1")
    b = traces.trial("ev-charger-barstow.t2.a1")  # shares every blob with the first Trial
    first = cli.upload(uploader, [a.parent], log=lambda *_: None)
    assert sorted(first.created) == ["ev-charger-barstow.t1.a1", "ev-charger-barstow.t2.a1"]
    assert first.blobs_uploaded == first.blobs_referenced > 0
    puts = s3.puts

    second = cli.upload(uploader, [a, b], log=lambda *_: None)
    assert second.created == [] and sorted(second.existing) == sorted(first.created)
    assert second.blobs_uploaded == 0
    assert s3.puts == puts  # nothing rewritten

    r = uploader.put("/api/trials/ev-charger-barstow.t1.a1", files=_files(a))
    assert r.status_code == 200 and r.json()["status"] == "exists"


def test_different_bytes_under_the_same_id_conflict(
    uploader: TestClient, traces: TraceDir, s3: FakeS3Client
) -> None:
    d = traces.trial("ev-charger-barstow.t1.a1")
    cli.upload(uploader, [d], log=lambda *_: None)
    stored = s3.objects[("traces", "trials/ev-charger-barstow.t1.a1/trial.json")][0]

    traces.trial("ev-charger-barstow.t1.a1", answer="a different answer")
    rep = cli.upload(uploader, [d], log=lambda *_: None)
    assert rep.conflicts == ["ev-charger-barstow.t1.a1"]
    r = uploader.put("/api/trials/ev-charger-barstow.t1.a1", files=_files(d))
    assert r.status_code == 409
    assert s3.objects[("traces", "trials/ev-charger-barstow.t1.a1/trial.json")][0] == stored


def test_trial_id_must_match_and_status_must_be_final(
    uploader: TestClient, traces: TraceDir
) -> None:
    d = traces.trial("ev-charger-barstow.t1.a1")
    cli.upload(uploader, [d], log=lambda *_: None)
    r = uploader.put("/api/trials/other.t1.a1", files=_files(d))
    assert r.status_code == 422 and "trial_id" in r.json()["detail"]

    running = traces.trial("still-going.t1.a1", status="running")
    rep = cli.upload(uploader, [running], log=lambda *_: None)
    assert rep.created == [] and rep.skipped
    r = uploader.put("/api/trials/still-going.t1.a1", files=_files(running))
    assert r.status_code == 422 and "not final" in r.json()["detail"]


def test_bad_seq_is_rejected(uploader: TestClient, traces: TraceDir) -> None:
    d = traces.trial("ev-charger-barstow.t1.a1")
    cli.upload(uploader, [d], log=lambda *_: None)
    lines = (d / "events.jsonl").read_bytes().splitlines()
    ev = json.loads(lines[1])
    ev["seq"] = 7
    lines[1] = json.dumps(ev).encode()
    files = _files(d)
    files["events.jsonl"] = ("events.jsonl", b"\n".join(lines) + b"\n", "application/x-ndjson")
    r = uploader.put("/api/trials/ev-charger-barstow.t1.a1", files=files)
    assert r.status_code == 422 and "seq must be dense" in r.json()["detail"]


def test_reads_return_header_events_and_blobs(
    uploaded: Callable[..., str], uploader: TestClient, as_user: Callable[[str], TestClient]
) -> None:
    tid = uploaded("ev-charger-barstow.t1.a1")
    alice = as_user("alice")
    r = alice.get(f"/api/trials/{tid}")
    assert r.status_code == 200
    body = r.json()
    assert body["header"]["trial_id"] == tid
    assert [e["seq"] for e in body["events"]] == list(range(body["n_events"]))

    ref = body["events"][0]["rendered_ref"]
    blob = alice.get(f"/api/blobs/{ref['sha256']}")
    assert blob.status_code == 200
    assert blob.content == b"PlugShare map step 1"
    assert blob.headers["content-type"].startswith("text/plain")
    assert blob.headers["x-content-type-options"] == "nosniff"
    assert "sandbox" in blob.headers["content-security-policy"]
    # the token can read too (Claude reads notes to propose groupings)
    assert uploader.get(f"/api/blobs/{ref['sha256']}").status_code == 200
    assert alice.get("/api/blobs/" + "0" * 64).status_code == 404


def test_html_blobs_are_never_served_as_html(
    uploader: TestClient, as_user: Callable[[str], TestClient]
) -> None:
    data = b"<script>alert(1)</script>"
    sha = hashlib.sha256(data).hexdigest()
    uploader.put(f"/api/blobs/{sha}", content=data, headers={"Content-Type": "text/html"})
    alice = as_user("alice")
    r = alice.get(f"/api/blobs/{sha}")
    assert r.headers["content-type"] == "application/octet-stream"


def test_blob_redirect_mode(
    app: FastAPI, uploader: TestClient, as_user: Callable[[str], TestClient]
) -> None:
    data = b"x"
    sha = hashlib.sha256(data).hexdigest()
    uploader.put(f"/api/blobs/{sha}", content=data, headers={"Content-Type": "text/plain"})
    app.state.settings = dataclasses.replace(app.state.settings, blob_redirect=True)
    alice = as_user("alice")
    r = alice.get(f"/api/blobs/{sha}", follow_redirects=False)
    assert r.status_code == 302 and r.headers["location"].startswith("https://r2.test/")
