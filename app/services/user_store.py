"""Who has signed in, and the store that remembers them.

A user here is minimal on purpose: Google's permanent `sub` for the person, the
email and name Google reports, a picture URL, and when they first appeared. The
`sub` is the primary key because it never changes — an email can be edited on
the Google account, and a name is whatever someone likes this week — so it is
the id books are filed under and the id a second visit is matched on.

One implementation, behind one small protocol: `DatabaseUserStore`, the
`users` table (`app/db.py`). It is the same table whether the database is
Postgres or the local SQLite file, so a person survives across processes in
every environment, and a book's owner is a real row rather than a filename.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import insert as sa_insert
from sqlalchemy import select
from sqlalchemy import update as sa_update
from sqlalchemy.engine import Engine, Row
from sqlalchemy.exc import IntegrityError

from app.db import UserRow

__all__ = ["DatabaseUserStore", "User", "UserStore"]

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class User:
    """A person who has signed in with Google."""

    sub: str
    email: str = ""
    name: str = ""
    picture: str = ""
    created_at: str = ""


class UserStore:
    """The contract: remember a person, and look them up again."""

    def upsert(self, user: User) -> User:
        """Insert or update a person, returning the stored record.

        Called on every sign-in: the first time it creates them, and afterwards
        it refreshes whatever Google reports now (a new name, a new picture)
        without disturbing the id the books are filed under.
        """
        raise NotImplementedError

    def get(self, sub: str) -> User | None:
        """The person with that id, or None if they are unknown."""
        raise NotImplementedError


class DatabaseUserStore(UserStore):
    """The `users` table, behind the same two-method contract."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def upsert(self, user: User) -> User:
        """Insert or update a person, returning the stored record.

        First sign-in creates the row; later ones refresh what Google reports
        now. The first-seen timestamp is sticky.

        Two callbacks for the same account can overlap (a double-clicked
        button, a retried request). Both would read "no row" and both would
        insert, so the loser gets an IntegrityError; it retries, finds the row
        its twin created, and takes the update branch. The visitor never sees
        the race.
        """
        _check_sub(user.sub)
        try:
            return self._upsert_once(user)
        except IntegrityError:
            return self._upsert_once(user)

    def _upsert_once(self, user: User) -> User:
        with self._engine.begin() as conn:
            row = conn.execute(select(UserRow).where(UserRow.sub == user.sub)).one_or_none()
            if row is None:
                conn.execute(
                    sa_insert(UserRow).values(
                        sub=user.sub,
                        email=user.email,
                        name=user.name,
                        avatar=user.picture,
                        created_at=_utcnow(),
                    )
                )
            else:
                conn.execute(
                    sa_update(UserRow)
                    .where(UserRow.sub == user.sub)
                    .values(email=user.email, name=user.name, avatar=user.picture)
                )
            stored = conn.execute(select(UserRow).where(UserRow.sub == user.sub)).one()
        return _user_from_row(stored)

    def get(self, sub: str) -> User | None:
        with self._engine.connect() as conn:
            row = conn.execute(select(UserRow).where(UserRow.sub == sub)).one_or_none()
        if row is None:
            return None
        return _user_from_row(row)


def _user_from_row(row: UserRow | Row[UserRow]) -> User:
    """The interface's record from a table row."""
    return User(
        sub=row.sub,
        email=row.email,
        name=row.name,
        picture=row.avatar,
        created_at=row.created_at.isoformat() if row.created_at else "",
    )


def _check_sub(sub: str) -> None:
    """Refuse an id the `users` table cannot store, before it reaches SQL.

    The sub is Google's own opaque id and becomes a primary key, not a
    filename. Empty or over-long ids are a broken client, not a person, and
    Postgres would raise on the length after the transaction had begun.
    """
    if not sub or len(sub) > 128:
        raise ValueError(f"unusable user id {sub!r}")


def _utcnow() -> datetime:
    """Naive UTC, the shape every dialect stores in a plain DateTime column."""
    return datetime.now(UTC).replace(tzinfo=None)
