"""The database: engine and tables.

Present only when `DATABASE_URL` is set. With an empty URL the app boots the
filesystem stores and this module is never imported — local development and
CI stay fully offline.

Two design points:

* **Sync engine, not async.** Every database access in this app already runs
  through `asyncio.to_thread` (the routes are sync functions and the book
  runner pushes blocking calls to worker threads), so an async driver would
  add a second execution model for nothing. Transactions are short; the pool
  is small.
* **`pool_pre_ping`.** Neon's pooled endpoint drops idle connections; the
  pre-ping turns that invisible instead of throwing `OperationalError` at
  the next request.

Connect arguments differ by dialect: SQLite (tests, local experiments) runs
multi-threaded through `to_thread`, so it needs `check_same_thread=False`.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, LargeBinary, String, Text, create_engine
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.sql import func

__all__ = [
    "BookPageRow",
    "BookRow",
    "UserRow",
    "build_engine",
    "database_secrets",
    "persistence_mode",
]

logger = logging.getLogger(__name__)


class Base(DeclarativeBase):
    """One metadata for all three tables."""


class UserRow(Base):
    """A person who signed in with Google."""

    __tablename__ = "users"

    sub: Mapped[str] = mapped_column(String(128), primary_key=True)
    email: Mapped[str] = mapped_column(String(320), default="")
    name: Mapped[str] = mapped_column(String(255), default="")
    avatar: Mapped[str] = mapped_column(String(1024), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())


class BookRow(Base):
    """One picture book: what was asked for, its state, and its own artefacts.

    The portrait and the finished PDF live here as blobs — hand-clothes
    bookkeeping across a second storage vendor was rejected on purpose ("DB
    blobs" was the ruling). `status_payload` is the exact document the
    browser polls, so a database-mode poll returns what a filesystem-mode
    poll returns, byte for byte.
    """

    __tablename__ = "books"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str | None] = mapped_column(ForeignKey("users.sub"), nullable=True)
    status: Mapped[str] = mapped_column(String(16))
    generation_id: Mapped[str] = mapped_column(String(64))
    about: Mapped[dict[str, Any]] = mapped_column(JSON)
    prompts: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)
    status_payload: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    portrait: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    pdf: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=func.now())


class BookPageRow(Base):
    """One page's finished art. (book_id, page_number) is the key."""

    __tablename__ = "book_pages"

    book_id: Mapped[str] = mapped_column(ForeignKey("books.id"), primary_key=True)
    page_number: Mapped[int] = mapped_column(primary_key=True)
    image: Mapped[bytes] = mapped_column(LargeBinary)
    media_type: Mapped[str] = mapped_column(String(50))


def normalise_database_url(database_url: str) -> str:
    """Rewrite the URL into a dialect the installed driver can serve.

    Two rewrites, both about real pasted URLs:

    * `postgres://` (the scheme people still paste from Heroku-era docs)
      means the same thing as `postgresql://`.
    * A bare `postgresql://` asks for psycopg2, which is not installed; this
      app ships psycopg3, so the driver is named explicitly.
    """
    url = make_url(database_url)
    if url.drivername in ("postgres", "postgresql"):
        url = url.set(drivername="postgresql+psycopg")
    return url.render_as_string(hide_password=False)


def database_secrets(database_url: str) -> tuple[str, ...]:
    """The credential fragments of a connection URL that must never be logged.

    The whole URL is one secret because it is pasted whole and already contains
    the password. The password itself is armed separately: a URL can reach a log
    line split into parts (a repr, a per-field debug line), and the fragment is
    what identifies the credential in those cases.

    A malformed URL still yields the raw string — refusing to parse is no reason
    to leave a pasted credential unprotected.
    """
    if not database_url:
        return ()
    try:
        url = make_url(database_url)
    except Exception:
        return (database_url,)
    secrets = [database_url]
    if url.password:
        secrets.append(url.password)
    return tuple(secrets)


def build_engine(database_url: str) -> Engine:
    """Build the one engine for the process.

    Raises:
        ValueError: when the URL is empty or not a URL the installed driver
            can handle (the caller turns that into a loud startup failure,
            not a silent filesystem fallback).
    """
    if not database_url:
        raise ValueError("DATABASE_URL is not set; there is no database mode.")
    url = make_url(normalise_database_url(database_url))
    connect_args = {"check_same_thread": False} if url.drivername.startswith("sqlite") else {}
    return create_engine(
        url,
        pool_pre_ping=True,
        pool_size=2,
        max_overflow=5,
        connect_args=connect_args,
    )


def persistence_mode(engine: Engine) -> str:
    """The one word about the database allowed in a log line: the dialect.

    The URL itself is never logged — it embeds the password.
    """
    dialect = engine.dialect.name
    return {"postgresql": "postgres"}.get(dialect, dialect)
