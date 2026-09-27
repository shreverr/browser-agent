"""JSON API under /api. Auth per route: Token (upload/proposals), Reader (human or token),
HumanWrite (signed-in, allowlisted human + CSRF header)."""

from __future__ import annotations

import asyncio
import json
import threading
from collections import OrderedDict
from collections.abc import AsyncIterator, Iterator
from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Query, Request
from fastapi.responses import PlainTextResponse, RedirectResponse, Response, StreamingResponse
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from . import service, traceio
from .auth import Human, HumanWrite, Reader, Token, settings_of
from .broadcast import Broadcaster
from .export import build_export, to_yaml
from .models import Blob
from .service import ServiceError, Unit, human
from .storage import BlobStore, blob_key, safe_media_type, trial_key

router = APIRouter(prefix="/api")
JSONBody = Annotated[dict[str, Any], Body()]


def get_db(request: Request) -> Iterator[Session]:
    db: Session = request.app.state.sessions()
    try:
        yield db
    finally:
        db.close()


DB = Annotated[Session, Depends(get_db)]


def store_of(request: Request) -> BlobStore:
    return request.app.state.store


def finish(request: Request, u: Unit) -> None:
    """Commit, then tell every open viewer what changed."""
    bc: Broadcaster = request.app.state.broadcaster
    for event in u.commit():
        bc.publish({**event, "actor": u.actor})


class EventsCache:
    """Parsed events.jsonl per Trial. Traces are immutable, so entries never go stale."""

    def __init__(self, size: int = 64) -> None:
        self._data: OrderedDict[str, list[dict[str, Any]]] = OrderedDict()
        self._size = size
        self._lock = threading.Lock()

    def get(self, key: str) -> list[dict[str, Any]] | None:
        with self._lock:
            if key in self._data:
                self._data.move_to_end(key)
                return self._data[key]
        return None

    def put(self, key: str, events: list[dict[str, Any]]) -> None:
        with self._lock:
            self._data[key] = events
            while len(self._data) > self._size:
                self._data.popitem(last=False)


# ---------- upload contract (bearer token) ----------


@router.post("/blobs/missing")
def blobs_missing(_: Token, db: DB, body: JSONBody) -> dict[str, list[str]]:
    shas = body.get("sha256")
    if not isinstance(shas, list):
        raise ServiceError(422, 'body must be {"sha256": [...]}')
    return {"missing": service.missing_blobs(db, shas)}  # pyright: ignore[reportUnknownArgumentType]


async def _read_body(request: Request, limit: int) -> bytes:
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        raise ServiceError(413, f"body larger than {limit} bytes")
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > limit:
            raise ServiceError(413, f"body larger than {limit} bytes")
        chunks.append(chunk)
    return b"".join(chunks)


@router.put("/blobs/{sha256}")
async def put_blob(sha256: str, request: Request, _: Token) -> dict[str, Any]:
    data = await _read_body(request, settings_of(request).max_upload_bytes)
    media_type = request.headers.get("content-type", "application/octet-stream")

    def work() -> bool:
        db: Session = request.app.state.sessions()
        try:
            u = Unit(db, service.UPLOADER)
            created = service.put_blob(u, store_of(request), sha256, data, media_type)
            u.commit()
            return created
        finally:
            db.close()

    created = await run_in_threadpool(work)
    return {"sha256": sha256, "bytes": len(data), "created": created}


@router.put("/trials/{trial_id}")
async def put_trial(trial_id: str, request: Request, _: Token) -> Response:
    limit = settings_of(request).max_upload_bytes
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > 2 * limit:
        raise ServiceError(413, "upload too large")
    form = await request.form(max_files=4, max_fields=4, max_part_size=limit)
    parts: dict[str, bytes] = {}
    for name in ("trial.json", "events.jsonl"):
        v = form.get(name)
        if v is None:
            raise ServiceError(422, f"multipart field {name!r} is required")
        parts[name] = v.encode() if isinstance(v, str) else await v.read()
        if len(parts[name]) > limit:
            raise ServiceError(413, f"{name} larger than {limit} bytes")
    await form.close()

    def work() -> str:
        db: Session = request.app.state.sessions()
        try:
            u = Unit(db, service.UPLOADER)
            result = service.put_trial(
                u, store_of(request), trial_id, parts["trial.json"], parts["events.jsonl"]
            )
            finish(request, u)
            return result
        finally:
            db.close()

    result = await run_in_threadpool(work)
    body = json.dumps({"trial_id": trial_id, "status": result})
    return Response(
        body, status_code=201 if result == "created" else 200, media_type="application/json"
    )


# ---------- reads (human session or token) ----------


def _opt_bool(v: str | None) -> bool | None:
    if v is None or v == "":
        return None
    return v.lower() in {"1", "true", "yes"}


@router.get("/trials")
def list_trials(
    p: Reader,
    db: DB,
    batch: str | None = None,
    status: str | None = None,
    cell: str | None = None,
    has_note: str | None = None,
    author: str | None = None,
    mode: str | None = None,
    coded: str | None = Query(None, pattern="^(coded|uncoded)$"),
    mine: str | None = Query(None, pattern="^(noted|unnoted)$"),
) -> dict[str, Any]:
    trials = service.list_trials(
        db,
        p.login,
        batch=batch or None,
        status=status or None,
        cell=cell or None,
        has_note=_opt_bool(has_note),
        author=author or None,
        mode=mode or None,
        coded=coded,
        mine=mine,
    )
    return {"trials": trials}


@router.get("/trials/{trial_id}")
def get_trial(trial_id: str, _: Reader, db: DB, request: Request) -> dict[str, Any]:
    t = service.get_trial(db, trial_id)
    cache: EventsCache = request.app.state.events_cache
    events = cache.get(t.id)
    if events is None:
        events = traceio.parse_events(store_of(request).get_bytes(trial_key(t.id, "events.jsonl")))
        cache.put(t.id, events)
    return {
        "trial_id": t.id,
        "header": t.header,
        "events": events,
        "n_events": t.n_events,
        "uploaded_at": service.iso(t.uploaded_at),
    }


@router.get("/trials/{trial_id}/notes")
def trial_notes(trial_id: str, _: Reader, db: DB) -> dict[str, Any]:
    service.get_trial(db, trial_id)
    return {"notes": service.list_notes(db, trial_id=trial_id)}


@router.get("/blobs/{sha256}", response_model=None)
def get_blob(sha256: str, _: Reader, db: DB, request: Request) -> Response:
    if not traceio.SHA256_RE.match(sha256):
        raise ServiceError(422, "not a sha256")
    b = db.get(Blob, sha256)
    if b is None:
        raise ServiceError(404, "no such blob")
    store = store_of(request)
    if settings_of(request).blob_redirect:
        return RedirectResponse(store.presign(blob_key(sha256), b.media_type), 302)
    return StreamingResponse(
        store.iter_bytes(blob_key(sha256)),
        media_type=safe_media_type(b.media_type),
        headers={
            "Content-Length": str(b.bytes),
            "Cache-Control": "private, max-age=31536000, immutable",
            "Content-Security-Policy": "sandbox; default-src 'none'",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/notes")
def notes(
    _: Reader,
    db: DB,
    trial_id: str | None = None,
    author: str | None = None,
    mode: str | None = None,
    uncoded: bool = False,
) -> dict[str, Any]:
    return {
        "notes": service.list_notes(
            db, trial_id=trial_id, author=author, mode=mode, uncoded=uncoded
        )
    }


@router.get("/modes")
def modes(_: Reader, db: DB, include_merged: bool = False) -> dict[str, Any]:
    return {"modes": service.list_modes(db, include_merged)}


@router.get("/assignments")
def assignments(
    _: Reader,
    db: DB,
    mode: str | None = None,
    state: str | None = Query(None, pattern="^(proposed|accepted|rejected)$"),
    note_id: int | None = None,
) -> dict[str, Any]:
    return {"assignments": service.list_assignments(db, mode=mode, state=state, note_id=note_id)}


@router.get("/taxonomy/export", response_class=PlainTextResponse)
def taxonomy_export(_: Reader, db: DB) -> PlainTextResponse:
    return PlainTextResponse(
        to_yaml(build_export(db)),
        media_type="application/yaml",
        headers={"Content-Disposition": 'attachment; filename="taxonomy.yaml"'},
    )


@router.get("/audit")
def audit(_: Reader, db: DB, limit: int = 200) -> dict[str, Any]:
    return {"audit": service.list_audit(db, limit)}


# ---------- writes (signed-in humans) ----------


@router.put("/trials/{trial_id}/note")
def put_note(
    trial_id: str, login: HumanWrite, db: DB, body: JSONBody, request: Request
) -> dict[str, Any]:
    u = Unit(db, human(login))
    note = service.upsert_note(u, login, trial_id, body)
    finish(request, u)
    return note


@router.post("/modes", status_code=201)
def create_mode(login: HumanWrite, db: DB, body: JSONBody, request: Request) -> dict[str, Any]:
    u = Unit(db, human(login))
    m = service.create_mode(u, login, body)
    finish(request, u)
    return m


@router.patch("/modes/{mode_id}")
def update_mode(
    mode_id: str, login: HumanWrite, db: DB, body: JSONBody, request: Request
) -> dict[str, Any]:
    u = Unit(db, human(login))
    m = service.update_mode(u, mode_id, body)
    finish(request, u)
    return m


@router.post("/modes/{mode_id}/merge")
def merge_mode(
    mode_id: str, login: HumanWrite, db: DB, body: JSONBody, request: Request
) -> dict[str, Any]:
    u = Unit(db, human(login))
    result = service.merge_mode(u, mode_id, body.get("into"))
    finish(request, u)
    return result


@router.post("/modes/{mode_id}/examples", status_code=201)
def pin_example(
    mode_id: str, login: HumanWrite, db: DB, body: JSONBody, request: Request
) -> dict[str, Any]:
    u = Unit(db, human(login))
    e = service.pin_example(u, login, mode_id, body)
    finish(request, u)
    return e


@router.delete("/examples/{example_id}")
def unpin_example(example_id: int, login: HumanWrite, db: DB, request: Request) -> dict[str, bool]:
    u = Unit(db, human(login))
    service.unpin_example(u, example_id)
    finish(request, u)
    return {"ok": True}


@router.post("/assignments", status_code=201)
def create_assignment(
    login: HumanWrite, db: DB, body: JSONBody, request: Request
) -> dict[str, Any]:
    u = Unit(db, human(login))
    a = service.assign(u, login, body.get("note_id"), body.get("mode_id"))
    finish(request, u)
    return a


@router.post("/assignments/{assignment_id}/{decision}")
def decide(
    assignment_id: int,
    decision: str,
    login: HumanWrite,
    db: DB,
    request: Request,
    body: Annotated[dict[str, Any] | None, Body()] = None,
) -> dict[str, Any]:
    if decision not in ("accept", "reject"):
        raise ServiceError(404, "decision must be accept or reject")
    u = Unit(db, human(login))
    rationale = (body or {}).get("rationale")
    a = service.decide(
        u, login, assignment_id, decision + "ed", rationale if isinstance(rationale, str) else None
    )
    finish(request, u)
    return a


@router.delete("/assignments/{assignment_id}")
def delete_assignment(
    assignment_id: int, login: HumanWrite, db: DB, request: Request
) -> dict[str, bool]:
    u = Unit(db, human(login))
    service.unassign(u, login, assignment_id)
    finish(request, u)
    return {"ok": True}


# ---------- Claude's proposals (bearer token) ----------


@router.post("/proposals", status_code=201)
def proposals(_: Token, db: DB, body: JSONBody, request: Request) -> dict[str, Any]:
    u = Unit(db, service.CLAUDE)
    result = service.propose(u, body.get("proposals"))
    finish(request, u)
    return result


# ---------- live updates ----------


@router.get("/events")
async def events(request: Request, login: Human) -> StreamingResponse:
    bc: Broadcaster = request.app.state.broadcaster
    q = bc.subscribe()

    async def stream() -> AsyncIterator[str]:
        try:
            yield "retry: 3000\n\n"
            yield f"event: hello\ndata: {json.dumps({'login': login})}\n\n"
            while not await request.is_disconnected():
                try:
                    ev = await asyncio.wait_for(q.get(), timeout=15)
                except TimeoutError:
                    yield ": ping\n\n"  # keeps Render's proxy from closing an idle stream
                    continue
                yield f"data: {json.dumps(ev)}\n\n"
        finally:
            bc.unsubscribe(q)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
