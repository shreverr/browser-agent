"""Alembic environment. The URL comes from `config.attributes["url"]` (set by db.migrate)
or from ANNOTATE_DATABASE_URL / DATABASE_URL when run through the alembic CLI."""

from __future__ import annotations

import os

from alembic import context
from sqlalchemy import create_engine, pool

from browser_agent.evals.annotate.config import normalize_database_url
from browser_agent.evals.annotate.models import Base

config = context.config
target_metadata = Base.metadata


def _url() -> str:
    url = (
        config.attributes.get("url")
        or os.environ.get("ANNOTATE_DATABASE_URL")
        or os.environ.get("DATABASE_URL")
    )
    if not url:
        raise RuntimeError("set ANNOTATE_DATABASE_URL")
    return normalize_database_url(url)


def run_offline() -> None:
    context.configure(url=_url(), target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def run_online() -> None:
    engine = create_engine(_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=connection.dialect.name == "sqlite",
        )
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_offline()
else:
    run_online()
