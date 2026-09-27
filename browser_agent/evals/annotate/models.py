"""Postgres data model for annotation. Portable to SQLite for tests (JSONB -> JSON variant).

Trials are immutable once uploaded. Every mutation of notes, modes, assignments and
examples is mirrored into the append-only `audit` table by the service layer.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

JSONType = JSON().with_variant(JSONB(), "postgresql")

VERDICTS = ("fail", "pass_but_bad", "ok")
GRADABILITY = ("code", "judge", "unknown")
ASSIGNMENT_STATES = ("proposed", "accepted", "rejected")


def now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSONType}


class Blob(Base):
    """Index of what is in the object store, so uploads can check refs without HEAD calls."""

    __tablename__ = "blob"
    sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    bytes: Mapped[int] = mapped_column(BigInteger)
    media_type: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Trial(Base):
    __tablename__ = "trial"
    id: Mapped[str] = mapped_column(String(255), primary_key=True)  # trial_id
    batch_id: Mapped[str] = mapped_column(String(255), index=True)
    task_id: Mapped[str] = mapped_column(String(255), index=True)
    status: Mapped[str] = mapped_column(String(32))
    terminal_reason: Mapped[str | None] = mapped_column(String(32))
    header: Mapped[dict[str, Any]] = mapped_column(JSONType)  # the full trial.json
    n_events: Mapped[int] = mapped_column(Integer)
    trial_sha256: Mapped[str] = mapped_column(String(64))
    events_sha256: Mapped[str] = mapped_column(String(64))
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Note(Base):
    __tablename__ = "note"
    __table_args__ = (
        UniqueConstraint("trial_id", "author", name="uq_note_trial_author"),
        CheckConstraint(
            "verdict IS NULL OR verdict IN ('fail', 'pass_but_bad', 'ok')", name="ck_note_verdict"
        ),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trial_id: Mapped[str] = mapped_column(ForeignKey("trial.id"), index=True)
    author: Mapped[str] = mapped_column(String(100))
    first_bad_seq: Mapped[int | None] = mapped_column(Integer)
    verdict: Mapped[str | None] = mapped_column(String(16))
    text: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Mode(Base):
    __tablename__ = "mode"
    __table_args__ = (
        CheckConstraint("gradability IN ('code', 'judge', 'unknown')", name="ck_mode_gradability"),
        Index(
            "uq_mode_active_name",
            "name",
            unique=True,
            postgresql_where=text("merged_into IS NULL"),
            sqlite_where=text("merged_into IS NULL"),
        ),
    )
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(200))
    definition: Mapped[str] = mapped_column(Text, default="")
    gradability: Mapped[str] = mapped_column(String(16), default="unknown")
    merged_into: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("mode.id"))
    created_by: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Assignment(Base):
    __tablename__ = "assignment"
    __table_args__ = (
        UniqueConstraint("note_id", "mode_id", name="uq_assignment_note_mode"),
        CheckConstraint(
            "state IN ('proposed', 'accepted', 'rejected')", name="ck_assignment_state"
        ),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    note_id: Mapped[int] = mapped_column(ForeignKey("note.id"), index=True)
    mode_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("mode.id"), index=True)
    proposed_by: Mapped[str] = mapped_column(String(120))  # "human:<login>" | "claude"
    state: Mapped[str] = mapped_column(String(16))
    decided_by: Mapped[str | None] = mapped_column(String(100))
    rationale: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Example(Base):
    __tablename__ = "example"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    mode_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("mode.id"), index=True)
    trial_id: Mapped[str] = mapped_column(ForeignKey("trial.id"))
    seq: Mapped[int | None] = mapped_column(Integer)
    caption: Mapped[str] = mapped_column(Text, default="")
    pinned_by: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)


class Audit(Base):
    __tablename__ = "audit"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, index=True)
    actor: Mapped[str] = mapped_column(String(120))  # "human:<login>" | "claude" | "uploader"
    action: Mapped[str] = mapped_column(String(64))
    entity: Mapped[str] = mapped_column(String(32))
    entity_id: Mapped[str] = mapped_column(String(255))
    before: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    after: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
