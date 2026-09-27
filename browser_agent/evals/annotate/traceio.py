"""Reading and validating ea-1 Traces (see browser_agent/evals/analysis/TRACE_FORMAT.md).

Shared by the server (upload validation) and the CLI (finding refs and blobs on disk).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

TRACE_SCHEMA_VERSION = "ea-1"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
TRIAL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$")
FINAL_STATUSES = {"completed", "infra_error", "aborted"}


class TraceError(ValueError):
    pass


@dataclass(frozen=True)
class Ref:
    sha256: str
    bytes: int
    media_type: str


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def iter_refs(value: Any) -> Iterator[Ref]:
    """Every `{"sha256", "bytes", "media_type"}` object anywhere inside a JSON value."""
    if isinstance(value, dict):
        d: dict[str, Any] = value  # pyright: ignore[reportUnknownVariableType]
        sha = d.get("sha256")
        if isinstance(sha, str) and "bytes" in d and "media_type" in d:
            size = d["bytes"]
            if not SHA256_RE.match(sha) or not isinstance(size, int) or size < 0:
                raise TraceError(f"malformed ref: {json.dumps(d)[:200]}")
            yield Ref(sha, size, str(d["media_type"]))
        for v in d.values():
            yield from iter_refs(v)
    elif isinstance(value, list):
        for v in value:  # pyright: ignore[reportUnknownVariableType]
            yield from iter_refs(v)


@dataclass(frozen=True)
class ParsedTrace:
    header: dict[str, Any]
    events: list[dict[str, Any]]

    @property
    def trial_id(self) -> str:
        return str(self.header["trial_id"])

    def refs(self) -> dict[str, Ref]:
        out: dict[str, Ref] = {}
        for ref in iter_refs(self.header):
            out[ref.sha256] = ref
        for ev in self.events:
            for ref in iter_refs(ev):
                out[ref.sha256] = ref
        return out


def parse_events(events_bytes: bytes) -> list[dict[str, Any]]:
    try:
        text = events_bytes.decode("utf-8")
    except UnicodeDecodeError as e:
        raise TraceError("events.jsonl is not UTF-8") from e
    events: list[dict[str, Any]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError as e:
            raise TraceError(f"events.jsonl line {lineno}: {e}") from e
        if not isinstance(ev, dict):
            raise TraceError(f"events.jsonl line {lineno}: not an object")
        events.append(ev)  # pyright: ignore[reportUnknownArgumentType]
    return events


def parse_trace(trial_bytes: bytes, events_bytes: bytes) -> ParsedTrace:
    try:
        header = json.loads(trial_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise TraceError(f"trial.json: {e}") from e
    if not isinstance(header, dict):
        raise TraceError("trial.json is not an object")
    h: dict[str, Any] = header  # pyright: ignore[reportUnknownVariableType]
    if h.get("trace_schema_version") != TRACE_SCHEMA_VERSION:
        raise TraceError(
            f"trace_schema_version must be {TRACE_SCHEMA_VERSION!r}, "
            f"got {h.get('trace_schema_version')!r}"
        )
    for key in ("trial_id", "batch_id", "status"):
        if not isinstance(h.get(key), str) or not h[key]:
            raise TraceError(f"trial.json: {key} is required")
    if not TRIAL_ID_RE.match(h["trial_id"]):
        raise TraceError("trial.json: trial_id has unsupported characters")
    task = h.get("task")
    if not isinstance(task, dict) or not isinstance(task.get("id"), str):  # pyright: ignore[reportUnknownMemberType]
        raise TraceError("trial.json: task.id is required")
    if h["status"] not in FINAL_STATUSES:
        raise TraceError(
            f"trial.json: status {h['status']!r} is not final; upload only finished Trials"
        )
    events = parse_events(events_bytes)
    for i, ev in enumerate(events):
        if ev.get("seq") != i:
            raise TraceError(
                f"events.jsonl: seq must be dense from 0 (event {i} has {ev.get('seq')!r})"
            )
        if not isinstance(ev.get("type"), str):
            raise TraceError(f"events.jsonl: event {i} has no type")
    return ParsedTrace(header=h, events=events)


# ---------- on-disk layout (CLI side) ----------


def find_trial_dirs(root: Path) -> list[Path]:
    """`root` is a Trial directory, or anything above one (batch, trials/, error-analysis/)."""
    if (root / "trial.json").is_file():
        return [root]
    return sorted(
        p.parent for p in root.rglob("trial.json") if (p.parent / "events.jsonl").is_file()
    )


def find_blob_root(trial_dir: Path) -> Path | None:
    """The `.evals/blobs` directory above a Trial directory, if there is one."""
    for parent in [trial_dir, *trial_dir.parents]:
        cand = parent / "blobs"
        if (cand / "sha256").is_dir():
            return cand
        cand = parent / ".evals" / "blobs"
        if (cand / "sha256").is_dir():
            return cand
    return None


def blob_path(blob_root: Path, sha256: str) -> Path:
    return blob_root / "sha256" / sha256[:2] / sha256
