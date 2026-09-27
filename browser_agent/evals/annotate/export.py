"""The taxonomy rollup: `taxonomy.yaml`, the durable snapshot committed to the repo.

Counts are distinct Trials with an accepted assignment, following merged_into, so a merge
never changes the total. Inter-rater agreement is reported per pair of authors over the
Trials both of them noted.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from itertools import combinations
from typing import Any

import yaml
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import Assignment, Example, Note, Trial, now
from .service import load_modes, note_has_content, resolve
from .traceio import TRACE_SCHEMA_VERSION


def build_export(db: Session) -> dict[str, Any]:
    modes = load_modes(db)
    notes = [
        n
        for n in db.scalars(select(Note).order_by(Note.trial_id, Note.author))
        if note_has_content(n)
    ]
    note_by_id = {n.id: n for n in notes}
    accepted: dict[int, set[uuid.UUID]] = defaultdict(set)  # note id -> resolved mode ids
    for a in db.scalars(select(Assignment).where(Assignment.state == "accepted")):
        accepted[a.note_id].add(resolve(modes, a.mode_id).id)

    members: dict[uuid.UUID, set[str]] = defaultdict(set)
    for note_id, mids in accepted.items():
        n = note_by_id.get(note_id)
        if n is None:
            continue
        for mid in mids:
            members[mid].add(n.trial_id)

    examples: dict[uuid.UUID, list[dict[str, Any]]] = defaultdict(list)
    for e in db.scalars(select(Example).order_by(Example.created_at, Example.id)):
        ex: dict[str, Any] = {"trial_id": e.trial_id}
        if e.seq is not None:
            ex["seq"] = e.seq
        if e.caption:
            ex["caption"] = e.caption
        examples[resolve(modes, e.mode_id).id].append(ex)

    merged_from: dict[uuid.UUID, list[str]] = defaultdict(list)
    for m in modes.values():
        if m.merged_into is not None:
            merged_from[resolve(modes, m.id).id].append(m.name)

    active = [m for m in modes.values() if m.merged_into is None]
    active.sort(key=lambda m: (-len(members[m.id]), m.name.lower()))
    mode_rows: list[dict[str, Any]] = []
    for m in active:
        row: dict[str, Any] = {
            "id": str(m.id),
            "name": m.name,
            "definition": m.definition,
            "gradability": m.gradability,
            "count": len(members[m.id]),
            "examples": examples[m.id],
            "trials": sorted(members[m.id]),
        }
        if merged_from[m.id]:
            row["merged_from"] = sorted(merged_from[m.id])
        mode_rows.append(row)

    uncoded = [
        {
            "trial_id": n.trial_id,
            "author": n.author,
            "verdict": n.verdict,
            "first_bad_seq": n.first_bad_seq,
            "text": n.text,
        }
        for n in notes
        if not accepted.get(n.id)
    ]

    return {
        "trace_schema_version": TRACE_SCHEMA_VERSION,
        "generated_at": now().isoformat(timespec="seconds"),
        "trials_uploaded": db.scalar(select(func.count()).select_from(Trial)) or 0,
        "trials_noted": len({n.trial_id for n in notes}),
        "modes": mode_rows,
        "uncoded": uncoded,
        "agreement": agreement(notes, accepted),
    }


def agreement(notes: list[Note], accepted: dict[int, set[uuid.UUID]]) -> list[dict[str, Any]]:
    """Per pair of authors: how often their notes on the same Trial landed in the same modes."""
    by_trial: dict[str, dict[str, Note]] = defaultdict(dict)
    for n in notes:
        by_trial[n.trial_id][n.author] = n
    pairs: dict[tuple[str, str], list[tuple[Note, Note]]] = defaultdict(list)
    for authored in by_trial.values():
        for a, b in combinations(sorted(authored), 2):
            pairs[(a, b)].append((authored[a], authored[b]))

    out: list[dict[str, Any]] = []
    for (a, b), shared in sorted(pairs.items()):
        coded = [(x, y) for x, y in shared if accepted.get(x.id) and accepted.get(y.id)]
        jaccards = [
            len(accepted[x.id] & accepted[y.id]) / len(accepted[x.id] | accepted[y.id])
            for x, y in coded
        ]
        verdicts = [(x.verdict, y.verdict) for x, y in shared if x.verdict and y.verdict]
        seqs = [
            (x.first_bad_seq, y.first_bad_seq)
            for x, y in shared
            if x.first_bad_seq is not None and y.first_bad_seq is not None
        ]
        out.append(
            {
                "authors": [a, b],
                "shared_trials": len(shared),
                "coded_by_both": len(coded),
                "same_modes": sum(1 for x, y in coded if accepted[x.id] == accepted[y.id]),
                "any_shared_mode": sum(1 for x, y in coded if accepted[x.id] & accepted[y.id]),
                "mean_jaccard": round(sum(jaccards) / len(jaccards), 3) if jaccards else None,
                "verdict_agreement": (
                    round(sum(1 for x, y in verdicts if x == y) / len(verdicts), 3)
                    if verdicts
                    else None
                ),
                "first_bad_seq_agreement": (
                    round(sum(1 for x, y in seqs if x == y) / len(seqs), 3) if seqs else None
                ),
            }
        )
    return out


def to_yaml(data: dict[str, Any]) -> str:
    header = (
        "# Error-analysis failure taxonomy, exported from the annotation server (#32).\n"
        "# count = distinct Trials with an accepted assignment (merged modes folded in).\n"
    )
    return header + yaml.safe_dump(data, sort_keys=False, allow_unicode=True, width=100)
