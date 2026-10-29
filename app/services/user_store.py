"""Who has signed in, and the store that remembers them.

A user here is minimal on purpose: Google's permanent `sub` for the person, the
email and name Google reports, a picture URL, and when they first appeared. The
`sub` is the primary key because it never changes — an email can be edited on
the Google account, and a name is whatever someone likes this week — so it is
the id books are filed under and the id a second visit is matched on.

Two implementations, behind one small protocol:

* `FileUserStore` — one JSON file per person under a directory. Used for local
  development, tests and any deployment with no database.
* the database-backed store (see `db.py`), used when `DATABASE_URL` is set.

The composition root picks one; nothing above this module knows which is
running.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

__all__ = ["FileUserStore", "User", "UserStore"]

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


class FileUserStore(UserStore):
    """One JSON file per person under a root directory."""

    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def upsert(self, user: User) -> User:
        _guard(user.sub)
        existing = self.get(user.sub)
        # The first sign-in keeps its timestamp; later ones refresh the rest.
        stored = User(
            sub=user.sub,
            email=user.email,
            name=user.name,
            picture=user.picture,
            created_at=existing.created_at if existing else _now(),
        )
        path = self._path(user.sub)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(asdict(stored), indent=2), encoding="utf-8")
        tmp.replace(path)
        return stored

    def get(self, sub: str) -> User | None:
        if not _safe(sub):
            return None
        path = self._path(sub)
        if not path.is_file():
            return None
        try:
            payload = cast("dict[str, object]", json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            return None
        return User(
            sub=str(payload.get("sub", sub)),
            email=str(payload.get("email", "")),
            name=str(payload.get("name", "")),
            picture=str(payload.get("picture", "")),
            created_at=str(payload.get("created_at", "")),
        )

    def _path(self, sub: str) -> Path:
        return self.root / f"{_safe_name(sub)}.json"


def _safe_name(sub: str) -> str:
    """A sub is Google's own id, which is alphanumeric; refuse anything else.

    It becomes a filename, so a sub that could escape the directory is a
    malicious request rather than a bad account, and it must not turn into a
    path segment.
    """
    return "".join(char for char in sub if char.isalnum() or char in "-_.")[:128]


def _guard(sub: str) -> None:
    if not _safe(sub):
        raise ValueError(f"Unsafe user id {sub!r}.")


def _safe(sub: str) -> bool:
    return bool(sub) and not any(part in sub for part in ("/", "\\", "..", "\x00"))


def _now() -> str:
    return datetime.now(UTC).isoformat()
