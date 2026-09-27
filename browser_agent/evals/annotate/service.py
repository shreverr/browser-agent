"""Domain operations. Every function works inside one `Unit` (one DB transaction).

Functions never commit: the API layer commits the Unit and only then publishes its change
events, so a failure anywhere (a merge half-way through, an R2 write) rolls back cleanly.
Rules enforced here:
- one note per annotator per Trial;
- only humans create, rename, redefine or merge modes;
- `claude` may only create `proposed` assignments;
- Trials are immutable once uploaded.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import traceio
from .models import (
    ASSIGNMENT_STATES,
    GRADABILITY,
    VERDICTS,
    Assignment,
    Audit,
    Blob,
    Example,
    Mode,
    Note,
    Trial,
    now,
)
from .storage import BlobStore, blob_key, trial_key

CLAUDE = "claude"
UPLOADER = "uploader"
STATE_RANK = {"rejected": 0, "proposed": 1, "accepted": 2}


class ServiceError(Exception):
    def __init__(self, status: int, detail: Any) -> None:
        super().__init__(str(detail))
        self.status = status
        self.detail = detail


def human(login: str) -> str:
    return f"human:{login}"


@dataclass
class Unit:
    db: Session
    actor: str
    events: list[dict[str, Any]] = field(default_factory=lambda: list[dict[str, Any]]())

    def audit(
        self,
        action: str,
        entity: str,
        entity_id: object,
        before: dict[str, Any] | None,
        after: dict[str, Any] | None,
    ) -> None:
        self.db.add(
            Audit(
                actor=self.actor,
                action=action,
                entity=entity,
                entity_id=str(entity_id),
                before=before,
                after=after,
            )
        )

    def emit(self, kind: str, **data: Any) -> None:
        self.events.append({"kind": kind, **data})

    def commit(self) -> list[dict[str, Any]]:
        self.db.commit()
        return self.events


def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=UTC)).isoformat()  # SQLite drops the zone


# ---------- modes: resolution through merged_into ----------


def load_modes(db: Session) -> dict[uuid.UUID, Mode]:
    return {m.id: m for m in db.scalars(select(Mode))}


def resolve(modes: dict[uuid.UUID, Mode], mode_id: uuid.UUID) -> Mode:
    m = modes[mode_id]
    seen: set[uuid.UUID] = set()
    while m.merged_into is not None and m.id not in seen:
        seen.add(m.id)
        m = modes[m.merged_into]
    return m


def parse_uuid(value: object, what: str = "mode id") -> uuid.UUID:
    try:
        return uuid.UUID(str(value))
    except ValueError as e:
        raise ServiceError(422, f"bad {what}: {value!r}") from e


def active_mode(db: Session, mode_id: object) -> Mode:
    m = db.get(Mode, parse_uuid(mode_id))
    if m is None:
        raise ServiceError(404, "no such mode")
    if m.merged_into is not None:
        raise ServiceError(409, f"mode was merged into {m.merged_into}")
    return m


def mode_dict(m: Mode) -> dict[str, Any]:
    return {
        "id": str(m.id),
        "name": m.name,
        "definition": m.definition,
        "gradability": m.gradability,
        "merged_into": str(m.merged_into) if m.merged_into else None,
        "created_by": m.created_by,
        "created_at": iso(m.created_at),
        "updated_at": iso(m.updated_at),
    }


# ---------- blobs + Trials (upload contract) ----------


def missing_blobs(db: Session, shas: list[str]) -> list[str]:
    bad = [s for s in shas if not isinstance(s, str) or not traceio.SHA256_RE.match(s)]  # pyright: ignore[reportUnnecessaryIsInstance]
    if bad:
        raise ServiceError(422, {"error": "not sha256 hex", "values": bad[:20]})
    wanted = list(dict.fromkeys(shas))
    have: set[str] = set()
    for i in range(0, len(wanted), 500):
        chunk = wanted[i : i + 500]
        have.update(db.scalars(select(Blob.sha256).where(Blob.sha256.in_(chunk))))
    return [s for s in wanted if s not in have]


def put_blob(u: Unit, store: BlobStore, sha: str, data: bytes, media_type: str) -> bool:
    if not traceio.SHA256_RE.match(sha):
        raise ServiceError(422, "blob name must be lowercase sha256 hex")
    actual = traceio.sha256_hex(data)
    if actual != sha:
        raise ServiceError(400, {"error": "hash mismatch", "expected": sha, "actual": actual})
    if u.db.get(Blob, sha) is not None:
        return False
    store.put(blob_key(sha), data, media_type)
    u.db.add(Blob(sha256=sha, bytes=len(data), media_type=media_type or "application/octet-stream"))
    try:
        u.db.flush()
    except IntegrityError:  # a concurrent upload of the same bytes won; same content, fine
        u.db.rollback()
        return False
    return True


def put_trial(
    u: Unit, store: BlobStore, trial_id: str, trial_bytes: bytes, events_bytes: bytes
) -> str:
    try:
        trace = traceio.parse_trace(trial_bytes, events_bytes)
    except traceio.TraceError as e:
        raise ServiceError(422, str(e)) from e
    if trace.trial_id != trial_id:
        raise ServiceError(422, f"trial.json trial_id {trace.trial_id!r} != URL {trial_id!r}")
    t_sha, e_sha = traceio.sha256_hex(trial_bytes), traceio.sha256_hex(events_bytes)

    def compare(existing: Trial) -> str:
        if existing.trial_sha256 == t_sha and existing.events_sha256 == e_sha:
            return "exists"
        raise ServiceError(409, f"a different Trace is already stored as {trial_id}")

    existing = u.db.get(Trial, trial_id)
    if existing is not None:
        return compare(existing)

    refs = trace.refs()
    known = {
        b.sha256: b
        for i in range(0, len(refs), 500)
        for b in u.db.scalars(select(Blob).where(Blob.sha256.in_(list(refs)[i : i + 500])))
    }
    missing = sorted(s for s in refs if s not in known)
    if missing:
        raise ServiceError(422, {"error": "refs not uploaded", "missing": missing})
    wrong = sorted(s for s, r in refs.items() if known[s].bytes != r.bytes)
    if wrong:
        raise ServiceError(
            422, {"error": "ref byte counts disagree with stored blobs", "refs": wrong}
        )

    h = trace.header
    task: dict[str, Any] = h["task"]
    row = Trial(
        id=trial_id,
        batch_id=h["batch_id"],
        task_id=task["id"],
        status=h["status"],
        terminal_reason=h.get("terminal_reason"),
        header=h,
        n_events=len(trace.events),
        trial_sha256=t_sha,
        events_sha256=e_sha,
    )
    u.db.add(row)
    try:
        u.db.flush()
    except IntegrityError:
        u.db.rollback()
        again = u.db.get(Trial, trial_id)
        if again is None:
            raise
        return compare(again)
    # The row is flushed but uncommitted: a concurrent upload of the same id blocks on it.
    store.put(trial_key(trial_id, "trial.json"), trial_bytes, "application/json")
    store.put(trial_key(trial_id, "events.jsonl"), events_bytes, "application/json")
    u.audit(
        "trial.upload", "trial", trial_id, None, {"trial_sha256": t_sha, "events_sha256": e_sha}
    )
    u.emit("trial", trial_id=trial_id)
    return "created"


def get_trial(db: Session, trial_id: str) -> Trial:
    t = db.get(Trial, trial_id)
    if t is None:
        raise ServiceError(404, "no such Trial")
    return t


# ---------- Trial list ----------


def _cell_tags(header: dict[str, Any]) -> list[str]:
    cell = (header.get("task") or {}).get("cell") or {}
    tags: list[str] = []
    if isinstance(cell, dict):
        for k, v in cell.items():  # pyright: ignore[reportUnknownVariableType]
            for x in v if isinstance(v, list) else [v]:  # pyright: ignore[reportUnknownVariableType]
                tags.append(f"{k}:{x}")
    return tags


def note_has_content(n: Note) -> bool:
    return bool(n.text.strip() or n.verdict or n.first_bad_seq is not None)


def list_trials(
    db: Session,
    me: str | None,
    *,
    batch: str | None = None,
    status: str | None = None,
    cell: str | None = None,
    has_note: bool | None = None,
    author: str | None = None,
    mode: str | None = None,
    coded: str | None = None,
    mine: str | None = None,
) -> list[dict[str, Any]]:
    modes = load_modes(db)
    trials = list(db.scalars(select(Trial).order_by(Trial.batch_id, Trial.id)))
    notes_by_trial: dict[str, list[Note]] = defaultdict(list)
    for n in db.scalars(select(Note)):
        notes_by_trial[n.trial_id].append(n)
    asg_by_trial: dict[str, list[tuple[Assignment, Mode]]] = defaultdict(list)
    for a, trial_id in db.execute(
        select(Assignment, Note.trial_id).join(Note, Note.id == Assignment.note_id)
    ):
        asg_by_trial[trial_id].append((a, resolve(modes, a.mode_id)))
    mode_filter = resolve(modes, parse_uuid(mode)).id if mode else None

    out: list[dict[str, Any]] = []
    for t in trials:
        h = t.header
        notes = [n for n in notes_by_trial[t.id] if note_has_content(n)]
        mine_note = next((n for n in notes_by_trial[t.id] if n.author == me), None)
        mode_counts: dict[uuid.UUID, dict[str, Any]] = {}
        for a, m in asg_by_trial[t.id]:
            if a.state == "rejected":
                continue
            e = mode_counts.setdefault(
                m.id, {"id": str(m.id), "name": m.name, "accepted": 0, "proposed": 0}
            )
            e[a.state] += 1
        is_coded = any(e["accepted"] for e in mode_counts.values())
        tags = _cell_tags(h)
        if batch and t.batch_id != batch:
            continue
        if status and status not in (t.status, t.terminal_reason):
            continue
        if cell and cell not in tags and not any(tag.split(":", 1)[1] == cell for tag in tags):
            continue
        if has_note is not None and bool(notes) != has_note:
            continue
        if author and not any(n.author == author.lower() for n in notes):
            continue
        if mode_filter and not mode_counts.get(mode_filter, {}).get("accepted"):
            continue
        if coded == "uncoded" and is_coded or coded == "coded" and not is_coded:
            continue
        has_mine = mine_note is not None and note_has_content(mine_note)
        if mine == "noted" and not has_mine or mine == "unnoted" and has_mine:
            continue
        totals = h.get("totals") or {}
        answer = h.get("answer") or ""
        out.append(
            {
                "trial_id": t.id,
                "batch_id": t.batch_id,
                "task_id": t.task_id,
                "cell": tags,
                "status": t.status,
                "terminal_reason": t.terminal_reason,
                "automation_outcome": h.get("automation_outcome"),
                "unscripted_fallback": bool(h.get("unscripted_fallback")),
                "infra_class": (h.get("infra") or {}).get("class"),
                "steps": totals.get("steps"),
                "cost_usd": totals.get("cost_usd"),
                "answer": answer[:200] + ("…" if len(answer) > 200 else ""),
                "uploaded_at": iso(t.uploaded_at),
                "my_note": (
                    {
                        "id": mine_note.id,
                        "verdict": mine_note.verdict,
                        "first_bad_seq": mine_note.first_bad_seq,
                        "has_text": bool(mine_note.text.strip()),
                    }
                    if mine_note is not None and note_has_content(mine_note)
                    else None
                ),
                "note_count": len(notes),
                "authors": sorted({n.author for n in notes}),
                "other_note_count": sum(1 for n in notes if n.author != me),
                "coded": is_coded,
                "modes": sorted(mode_counts.values(), key=lambda e: e["name"].lower()),
            }
        )
    return out


# ---------- notes ----------


def assignment_dict(a: Assignment, modes: dict[uuid.UUID, Mode]) -> dict[str, Any]:
    m = resolve(modes, a.mode_id)
    return {
        "id": a.id,
        "note_id": a.note_id,
        "mode_id": str(m.id),
        "mode_name": m.name,
        "state": a.state,
        "proposed_by": a.proposed_by,
        "decided_by": a.decided_by,
        "rationale": a.rationale,
        "created_at": iso(a.created_at),
        "decided_at": iso(a.decided_at),
    }


def note_dict(n: Note, assignments: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    d: dict[str, Any] = {
        "id": n.id,
        "trial_id": n.trial_id,
        "author": n.author,
        "verdict": n.verdict,
        "first_bad_seq": n.first_bad_seq,
        "text": n.text,
        "created_at": iso(n.created_at),
        "updated_at": iso(n.updated_at),
    }
    if assignments is not None:
        d["assignments"] = assignments
    return d


def _note_audit(n: Note) -> dict[str, Any]:
    return {"verdict": n.verdict, "first_bad_seq": n.first_bad_seq, "text": n.text}


def list_notes(
    db: Session,
    *,
    trial_id: str | None = None,
    author: str | None = None,
    mode: str | None = None,
    uncoded: bool = False,
) -> list[dict[str, Any]]:
    modes = load_modes(db)
    q = select(Note).order_by(Note.trial_id, Note.created_at, Note.id)
    if trial_id:
        q = q.where(Note.trial_id == trial_id)
    if author:
        q = q.where(Note.author == author.lower())
    notes = list(db.scalars(q))
    by_note: dict[int, list[Assignment]] = defaultdict(list)
    ids = [n.id for n in notes]
    for i in range(0, len(ids), 500):
        for a in db.scalars(
            select(Assignment)
            .where(Assignment.note_id.in_(ids[i : i + 500]))
            .order_by(Assignment.id)
        ):
            by_note[a.note_id].append(a)
    mode_filter = resolve(modes, parse_uuid(mode)).id if mode else None
    out: list[dict[str, Any]] = []
    for n in notes:
        asg = by_note[n.id]
        accepted = {resolve(modes, a.mode_id).id for a in asg if a.state == "accepted"}
        if mode_filter and mode_filter not in accepted:
            continue
        if uncoded and (accepted or not note_has_content(n)):
            continue
        out.append(note_dict(n, [assignment_dict(a, modes) for a in asg]))
    return out


def upsert_note(u: Unit, login: str, trial_id: str, body: dict[str, Any]) -> dict[str, Any]:
    t = get_trial(u.db, trial_id)
    verdict = body.get("verdict")
    if verdict is not None and verdict not in VERDICTS:
        raise ServiceError(422, f"verdict must be one of {', '.join(VERDICTS)}")
    seq = body.get("first_bad_seq")
    if seq is not None and (
        not isinstance(seq, int) or isinstance(seq, bool) or not 0 <= seq < t.n_events
    ):
        raise ServiceError(422, f"first_bad_seq must be an event seq in 0..{t.n_events - 1}")
    text = body.get("text") or ""
    if not isinstance(text, str) or len(text) > 20_000:
        raise ServiceError(422, "text must be a string of at most 20000 characters")

    n = u.db.scalars(select(Note).where(Note.trial_id == trial_id, Note.author == login)).first()
    before = _note_audit(n) if n else None
    if n is None:
        n = Note(trial_id=trial_id, author=login, text="")
        u.db.add(n)
    n.verdict, n.first_bad_seq, n.text, n.updated_at = verdict, seq, text, now()
    try:
        u.db.flush()
    except IntegrityError as e:  # two tabs created my note at once; the client retries
        raise ServiceError(409, "note was created concurrently; retry") from e
    after = _note_audit(n)
    if before != after:
        u.audit("note.upsert", "note", n.id, before, after)
        u.emit("note", trial_id=trial_id, note_id=n.id, author=login)
    return note_dict(n)


# ---------- modes ----------


def _check_name(db: Session, name: object, exclude: uuid.UUID | None = None) -> str:
    if not isinstance(name, str) or not name.strip():
        raise ServiceError(422, "mode name is required")
    clean = " ".join(name.split())
    if len(clean) > 200:
        raise ServiceError(422, "mode name is at most 200 characters")
    for m in db.scalars(select(Mode).where(Mode.merged_into.is_(None))):
        if m.id != exclude and m.name.lower() == clean.lower():
            raise ServiceError(409, f"an active mode is already named {m.name!r}")
    return clean


def _check_gradability(g: object) -> str:
    if g not in GRADABILITY:
        raise ServiceError(422, f"gradability must be one of {', '.join(GRADABILITY)}")
    return str(g)


def create_mode(u: Unit, login: str, body: dict[str, Any]) -> dict[str, Any]:
    m = Mode(
        name=_check_name(u.db, body.get("name")),
        definition=str(body.get("definition") or ""),
        gradability=_check_gradability(body.get("gradability") or "unknown"),
        created_by=login,
    )
    u.db.add(m)
    u.db.flush()
    u.audit("mode.create", "mode", m.id, None, mode_dict(m))
    u.emit("mode", mode_id=str(m.id))
    return mode_dict(m)


def update_mode(u: Unit, mode_id: str, body: dict[str, Any]) -> dict[str, Any]:
    m = active_mode(u.db, mode_id)
    before = mode_dict(m)
    if "name" in body:
        m.name = _check_name(u.db, body["name"], exclude=m.id)
    if "definition" in body:
        if not isinstance(body["definition"], str):
            raise ServiceError(422, "definition must be a string")
        m.definition = body["definition"]
    if "gradability" in body:
        m.gradability = _check_gradability(body["gradability"])
    after = mode_dict(m)
    if before != after:
        m.updated_at = now()
        action = "mode.rename" if before["name"] != after["name"] else "mode.update"
        u.audit(action, "mode", m.id, before, mode_dict(m))
        u.emit("mode", mode_id=str(m.id))
    return mode_dict(m)


def merge_mode(u: Unit, source_id: str, target_id: object) -> dict[str, Any]:
    """Fold `source` into `target` in one transaction. Ids survive; counts follow."""
    src = active_mode(u.db, source_id)
    dst = active_mode(u.db, target_id)
    if src.id == dst.id:
        raise ServiceError(422, "cannot merge a mode into itself")
    before = {"source": mode_dict(src), "target": mode_dict(dst)}

    moved = deduped = 0
    target_rows = {
        a.note_id: a for a in u.db.scalars(select(Assignment).where(Assignment.mode_id == dst.id))
    }
    for a in list(u.db.scalars(select(Assignment).where(Assignment.mode_id == src.id))):
        t = target_rows.get(a.note_id)
        if t is None:
            a.mode_id, a.updated_at = dst.id, now()
            target_rows[a.note_id] = a
            moved += 1
            continue
        if STATE_RANK[a.state] > STATE_RANK[t.state]:
            t.state, t.decided_by, t.decided_at = a.state, a.decided_by, a.decided_at
            t.rationale = t.rationale or a.rationale
            t.updated_at = now()
        u.db.delete(a)
        deduped += 1
    u.db.flush()

    seen = {
        (e.trial_id, e.seq) for e in u.db.scalars(select(Example).where(Example.mode_id == dst.id))
    }
    for e in list(u.db.scalars(select(Example).where(Example.mode_id == src.id))):
        if (e.trial_id, e.seq) in seen:
            u.db.delete(e)
        else:
            e.mode_id = dst.id
            seen.add((e.trial_id, e.seq))
    for m in u.db.scalars(select(Mode).where(Mode.merged_into == src.id)):
        m.merged_into = dst.id  # keep chains one hop long
    src.merged_into, src.updated_at = dst.id, now()
    dst.updated_at = now()
    u.db.flush()
    after = {"source": mode_dict(src), "target": mode_dict(dst), "moved": moved, "deduped": deduped}
    u.audit("mode.merge", "mode", src.id, before, after)
    u.emit("mode", mode_id=str(src.id), merged_into=str(dst.id))
    return {"source": mode_dict(src), "target": mode_dict(dst), "moved": moved, "deduped": deduped}


def list_modes(db: Session, include_merged: bool = False) -> list[dict[str, Any]]:
    modes = load_modes(db)
    trials: dict[uuid.UUID, set[str]] = defaultdict(set)
    notes: dict[uuid.UUID, set[int]] = defaultdict(set)
    proposed: dict[uuid.UUID, int] = defaultdict(int)
    for a, trial_id in db.execute(
        select(Assignment, Note.trial_id).join(Note, Note.id == Assignment.note_id)
    ):
        mid = resolve(modes, a.mode_id).id
        if a.state == "accepted":
            trials[mid].add(trial_id)
            notes[mid].add(a.note_id)
        elif a.state == "proposed":
            proposed[mid] += 1
    examples: dict[uuid.UUID, list[dict[str, Any]]] = defaultdict(list)
    for e in db.scalars(select(Example).order_by(Example.created_at, Example.id)):
        examples[resolve(modes, e.mode_id).id].append(example_dict(e))
    merged_from: dict[uuid.UUID, list[dict[str, str]]] = defaultdict(list)
    for m in modes.values():
        if m.merged_into is not None:
            merged_from[resolve(modes, m.id).id].append({"id": str(m.id), "name": m.name})
    out: list[dict[str, Any]] = []
    for m in modes.values():
        if m.merged_into is not None and not include_merged:
            continue
        d = mode_dict(m)
        d.update(
            trial_count=len(trials[m.id]),
            note_count=len(notes[m.id]),
            proposed_count=proposed[m.id],
            examples=examples[m.id],
            merged_from=merged_from[m.id],
        )
        out.append(d)
    out.sort(key=lambda d: (d["merged_into"] is not None, -d["trial_count"], d["name"].lower()))
    return out


# ---------- examples ----------


def example_dict(e: Example) -> dict[str, Any]:
    return {
        "id": e.id,
        "mode_id": str(e.mode_id),
        "trial_id": e.trial_id,
        "seq": e.seq,
        "caption": e.caption,
        "pinned_by": e.pinned_by,
        "created_at": iso(e.created_at),
    }


def pin_example(u: Unit, login: str, mode_id: str, body: dict[str, Any]) -> dict[str, Any]:
    m = active_mode(u.db, mode_id)
    t = get_trial(u.db, str(body.get("trial_id") or ""))
    seq = body.get("seq")
    if seq is not None and (
        not isinstance(seq, int) or isinstance(seq, bool) or not 0 <= seq < t.n_events
    ):
        raise ServiceError(422, f"seq must be in 0..{t.n_events - 1}")
    caption = str(body.get("caption") or "")[:2000]
    existing = u.db.scalars(
        select(Example).where(
            Example.mode_id == m.id,
            Example.trial_id == t.id,
            Example.seq.is_(None) if seq is None else Example.seq == seq,
        )
    ).first()
    if existing is not None:
        return example_dict(existing)
    e = Example(mode_id=m.id, trial_id=t.id, seq=seq, caption=caption, pinned_by=login)
    u.db.add(e)
    u.db.flush()
    u.audit("example.pin", "example", e.id, None, example_dict(e))
    u.emit("example", mode_id=str(m.id), trial_id=t.id)
    return example_dict(e)


def unpin_example(u: Unit, example_id: int) -> None:
    e = u.db.get(Example, example_id)
    if e is None:
        raise ServiceError(404, "no such example")
    u.audit("example.unpin", "example", e.id, example_dict(e), None)
    u.emit("example", mode_id=str(e.mode_id), trial_id=e.trial_id)
    u.db.delete(e)


# ---------- assignments ----------


def _asg_audit(a: Assignment) -> dict[str, Any]:
    return {
        "note_id": a.note_id,
        "mode_id": str(a.mode_id),
        "state": a.state,
        "proposed_by": a.proposed_by,
        "decided_by": a.decided_by,
        "rationale": a.rationale,
    }


def list_assignments(
    db: Session, *, mode: str | None = None, state: str | None = None, note_id: int | None = None
) -> list[dict[str, Any]]:
    modes = load_modes(db)
    q = select(Assignment).order_by(Assignment.id)
    if state:
        q = q.where(Assignment.state == state)
    if note_id is not None:
        q = q.where(Assignment.note_id == note_id)
    mode_filter = resolve(modes, parse_uuid(mode)).id if mode else None
    return [
        assignment_dict(a, modes)
        for a in db.scalars(q)
        if mode_filter is None or resolve(modes, a.mode_id).id == mode_filter
    ]


def assign(u: Unit, login: str, note_id: object, mode_id: object) -> dict[str, Any]:
    """Assign a note to a mode. On my own note it is accepted; on someone else's, proposed."""
    if not isinstance(note_id, int):
        raise ServiceError(422, "note_id must be an integer")
    n = u.db.get(Note, note_id)
    if n is None:
        raise ServiceError(404, "no such note")
    m = active_mode(u.db, mode_id)
    own = n.author == login
    a = u.db.scalars(
        select(Assignment).where(Assignment.note_id == n.id, Assignment.mode_id == m.id)
    ).first()
    if a is not None:
        if own and a.state != "accepted":
            before = _asg_audit(a)
            a.state, a.decided_by, a.decided_at, a.updated_at = "accepted", login, now(), now()
            u.audit("assignment.accept", "assignment", a.id, before, _asg_audit(a))
            u.emit("assignment", trial_id=n.trial_id, note_id=n.id)
        return assignment_dict(a, load_modes(u.db))
    a = Assignment(
        note_id=n.id,
        mode_id=m.id,
        proposed_by=human(login),
        state="accepted" if own else "proposed",
        decided_by=login if own else None,
        decided_at=now() if own else None,
    )
    u.db.add(a)
    u.db.flush()
    u.audit("assignment.create", "assignment", a.id, None, _asg_audit(a))
    u.emit("assignment", trial_id=n.trial_id, note_id=n.id)
    return assignment_dict(a, load_modes(u.db))


def decide(
    u: Unit, login: str, assignment_id: int, state: str, rationale: str | None = None
) -> dict[str, Any]:
    """Accept or reject. Claude's proposals: any human. A human's proposal: the note's author."""
    if state not in ("accepted", "rejected"):
        raise ServiceError(422, "state must be accepted or rejected")
    a = u.db.get(Assignment, assignment_id)
    if a is None:
        raise ServiceError(404, "no such assignment")
    n = u.db.get(Note, a.note_id)
    assert n is not None
    if a.proposed_by != CLAUDE and n.author != login:
        raise ServiceError(403, "only the note's author decides a human's proposal on it")
    before = _asg_audit(a)
    a.state, a.decided_by, a.decided_at, a.updated_at = state, login, now(), now()
    if rationale:
        a.rationale = rationale
    u.audit(f"assignment.{state[:-2]}", "assignment", a.id, before, _asg_audit(a))
    u.emit("assignment", trial_id=n.trial_id, note_id=n.id)
    return assignment_dict(a, load_modes(u.db))


def unassign(u: Unit, login: str, assignment_id: int) -> None:
    a = u.db.get(Assignment, assignment_id)
    if a is None:
        raise ServiceError(404, "no such assignment")
    n = u.db.get(Note, a.note_id)
    assert n is not None
    if n.author != login and a.proposed_by != human(login):
        raise ServiceError(
            403, "you can only remove assignments on your own note or your own proposals"
        )
    u.audit("assignment.delete", "assignment", a.id, _asg_audit(a), None)
    u.emit("assignment", trial_id=n.trial_id, note_id=n.id)
    u.db.delete(a)


def propose(u: Unit, items: object) -> dict[str, Any]:
    """Bulk proposals from Claude. All-or-nothing on validation; existing pairs are skipped."""
    if not isinstance(items, list) or not items:
        raise ServiceError(422, "proposals must be a non-empty list")
    if len(items) > 5000:  # pyright: ignore[reportUnknownArgumentType]
        raise ServiceError(422, "at most 5000 proposals per call")
    modes = load_modes(u.db)
    active_by_name = {m.name.lower(): m for m in modes.values() if m.merged_into is None}
    errors: list[dict[str, Any]] = []
    plan: list[tuple[Note, Mode, str | None]] = []
    for i, raw in enumerate(items):  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
        if not isinstance(raw, dict):
            errors.append({"index": i, "error": "not an object"})
            continue
        item: dict[str, Any] = raw  # pyright: ignore[reportUnknownVariableType]
        note: Note | None = None
        if isinstance(item.get("note_id"), int):
            note = u.db.get(Note, item["note_id"])
        elif item.get("trial_id") and item.get("author"):
            note = u.db.scalars(
                select(Note).where(
                    Note.trial_id == item["trial_id"], Note.author == str(item["author"]).lower()
                )
            ).first()
        mode: Mode | None = None
        if item.get("mode_id"):
            try:
                mid = uuid.UUID(str(item["mode_id"]))
                mode = resolve(modes, mid) if mid in modes else None
            except ValueError:
                mode = None
        elif isinstance(item.get("mode"), str):
            mode = active_by_name.get(item["mode"].strip().lower())
        rationale = item.get("rationale")
        if note is None:
            errors.append(
                {"index": i, "error": "no such note (give note_id, or trial_id + author)"}
            )
        if mode is None:
            errors.append(
                {
                    "index": i,
                    "error": "no such active mode (give mode_id or an existing mode name); Claude cannot create modes",
                }
            )
        if rationale is not None and not isinstance(rationale, str):
            errors.append({"index": i, "error": "rationale must be a string"})
        if note is not None and mode is not None:
            plan.append((note, mode, rationale))
    if errors:
        raise ServiceError(
            422, {"error": "invalid proposals; nothing was written", "errors": errors}
        )

    created: list[int] = []
    skipped: list[dict[str, Any]] = []
    seen: set[tuple[int, uuid.UUID]] = set()
    trials: set[str] = set()
    for note, mode, rationale in plan:
        key = (note.id, mode.id)
        existing = u.db.scalars(
            select(Assignment).where(Assignment.note_id == note.id, Assignment.mode_id == mode.id)
        ).first()
        if existing is not None or key in seen:
            skipped.append(
                {
                    "note_id": note.id,
                    "mode_id": str(mode.id),
                    "state": existing.state if existing else "duplicate",
                }
            )
            continue
        seen.add(key)
        a = Assignment(
            note_id=note.id,
            mode_id=mode.id,
            proposed_by=CLAUDE,
            state="proposed",
            rationale=rationale,
        )
        u.db.add(a)
        u.db.flush()
        u.audit("assignment.propose", "assignment", a.id, None, _asg_audit(a))
        created.append(a.id)
        trials.add(note.trial_id)
    if created:
        u.emit("assignment", bulk=True, trial_ids=sorted(trials))
    return {"created": len(created), "assignment_ids": created, "skipped": skipped}


# ---------- audit ----------


def list_audit(db: Session, limit: int = 200) -> list[dict[str, Any]]:
    rows = db.scalars(select(Audit).order_by(Audit.id.desc()).limit(max(1, min(limit, 1000))))
    return [
        {
            "id": r.id,
            "at": iso(r.at),
            "actor": r.actor,
            "action": r.action,
            "entity": r.entity,
            "entity_id": r.entity_id,
            "before": r.before,
            "after": r.after,
        }
        for r in rows
    ]


__all__ = ["ASSIGNMENT_STATES", "ServiceError", "Unit"]
