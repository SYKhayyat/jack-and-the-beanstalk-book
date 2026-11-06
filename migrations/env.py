"""Alembic environment: the URL comes from DATABASE_URL, never from here.

This file is committed; a connection URL embeds a password, so it is read
from the environment at run time:

    DATABASE_URL=postgres://... make migrate

With `DATABASE_URL` unset, the chain applies to the local SQLite file the app
itself would use, so `make migrate` and a plain `make dev` agree.

The app can also hand its own engine over in `config.attributes["engine"]`
(`app.db.prepare_database`), which is how a process migrates at boot without
publishing its URL in the environment. In that mode the logging configuration
is left alone: the app has already installed its own handlers and secrets
redaction, and alembic.ini's logging must not replace them mid-request.

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

#: The engine the app handed over, if it is migrating its own database.
shared_engine = config.attributes.get("engine")

if config.config_file_name is not None and shared_engine is None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _database_url() -> str:
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        # The same default the app boots with: one SQLite file under the
        # storage directory. Imported here rather than at module import so a
        # `DATABASE_URL` deployment never reads the settings file.
        from app.config import get_settings

        return get_settings().effective_database_url
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
    """Apply the pending migrations on the app's engine, or a fresh connection."""
    connectable = shared_engine or create_engine(
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
