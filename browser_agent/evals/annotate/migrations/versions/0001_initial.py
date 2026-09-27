"""initial schema: blob, trial, note, mode, assignment, example, audit

Revision ID: 0001
Revises:
Create Date: 2026-09-27
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSON = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "blob",
        sa.Column("sha256", sa.String(64), primary_key=True),
        sa.Column("bytes", sa.BigInteger(), nullable=False),
        sa.Column("media_type", sa.String(255), nullable=False),
        sa.Column("created_at", TS, nullable=False),
    )
    op.create_table(
        "trial",
        sa.Column("id", sa.String(255), primary_key=True),
        sa.Column("batch_id", sa.String(255), nullable=False, index=True),
        sa.Column("task_id", sa.String(255), nullable=False, index=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("terminal_reason", sa.String(32), nullable=True),
        sa.Column("header", JSON, nullable=False),
        sa.Column("n_events", sa.Integer(), nullable=False),
        sa.Column("trial_sha256", sa.String(64), nullable=False),
        sa.Column("events_sha256", sa.String(64), nullable=False),
        sa.Column("uploaded_at", TS, nullable=False),
    )
    op.create_table(
        "mode",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("definition", sa.Text(), nullable=False),
        sa.Column("gradability", sa.String(16), nullable=False),
        sa.Column("merged_into", sa.Uuid(), sa.ForeignKey("mode.id"), nullable=True),
        sa.Column("created_by", sa.String(100), nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
        sa.CheckConstraint(
            "gradability IN ('code', 'judge', 'unknown')", name="ck_mode_gradability"
        ),
    )
    op.create_index(
        "uq_mode_active_name",
        "mode",
        ["name"],
        unique=True,
        postgresql_where=sa.text("merged_into IS NULL"),
        sqlite_where=sa.text("merged_into IS NULL"),
    )
    op.create_table(
        "note",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column(
            "trial_id", sa.String(255), sa.ForeignKey("trial.id"), nullable=False, index=True
        ),
        sa.Column("author", sa.String(100), nullable=False),
        sa.Column("first_bad_seq", sa.Integer(), nullable=True),
        sa.Column("verdict", sa.String(16), nullable=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
        sa.UniqueConstraint("trial_id", "author", name="uq_note_trial_author"),
        sa.CheckConstraint(
            "verdict IS NULL OR verdict IN ('fail', 'pass_but_bad', 'ok')", name="ck_note_verdict"
        ),
    )
    op.create_table(
        "assignment",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("note_id", sa.Integer(), sa.ForeignKey("note.id"), nullable=False, index=True),
        sa.Column("mode_id", sa.Uuid(), sa.ForeignKey("mode.id"), nullable=False, index=True),
        sa.Column("proposed_by", sa.String(120), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("decided_by", sa.String(100), nullable=True),
        sa.Column("rationale", sa.Text(), nullable=True),
        sa.Column("created_at", TS, nullable=False),
        sa.Column("updated_at", TS, nullable=False),
        sa.Column("decided_at", TS, nullable=True),
        sa.UniqueConstraint("note_id", "mode_id", name="uq_assignment_note_mode"),
        sa.CheckConstraint(
            "state IN ('proposed', 'accepted', 'rejected')", name="ck_assignment_state"
        ),
    )
    op.create_table(
        "example",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("mode_id", sa.Uuid(), sa.ForeignKey("mode.id"), nullable=False, index=True),
        sa.Column("trial_id", sa.String(255), sa.ForeignKey("trial.id"), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=True),
        sa.Column("caption", sa.Text(), nullable=False),
        sa.Column("pinned_by", sa.String(100), nullable=False),
        sa.Column("created_at", TS, nullable=False),
    )
    op.create_table(
        "audit",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("at", TS, nullable=False, index=True),
        sa.Column("actor", sa.String(120), nullable=False),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("entity", sa.String(32), nullable=False),
        sa.Column("entity_id", sa.String(255), nullable=False),
        sa.Column("before", JSON, nullable=True),
        sa.Column("after", JSON, nullable=True),
    )


def downgrade() -> None:
    for table in ("audit", "example", "assignment", "note"):
        op.drop_table(table)
    op.drop_index("uq_mode_active_name", table_name="mode")
    for table in ("mode", "trial", "blob"):
        op.drop_table(table)
