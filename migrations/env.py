"""Alembic environment: the URL comes from DATABASE_URL, never from here.

This file is committed; a connection URL embeds a password, so it is read
from the environment at run time:

    DATABASE_URL=postgres://... make migrate

The target metadata is the app's own models (`app/db.py`), so autogenerate
stays in sync with the code rather than with a second description of the
schema.
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import create_engine, pool

from app.db import Base, normalise_database_url

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _database_url() -> str:
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        raise RuntimeError(
            "DATABASE_URL is not set. Export it (e.g. "
            "`export DATABASE_URL=postgres://...`) and run the command again."
        )
    return url


def run_migrations_offline() -> None:
    """Emit SQL to stdout without a database connection."""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Connect to DATABASE_URL and apply the pending migrations."""
    connectable = create_engine(
        normalise_database_url(_database_url()), poolclass=pool.NullPool
    )
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
