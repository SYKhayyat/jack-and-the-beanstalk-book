"""The child a person asked the studio to remember.

One child per account: the name they typed, the boy/girl answer the story
needs, the portrait they approved, and the photograph it was drawn from. The
portrait is drawn in its story's costume, so the story's key is stored with it
and only that story may hand it back — a Jack portrait would put Jack's tunic
on Red.

The point is a second book that starts without an upload: the studio asks for
this record, writes it into a fresh draft (see `app/routes/child_profile.py`),
and the rest of the making-a-book flow — which knows only drafts — never learns
this exists. No image is drawn on reuse, so nothing is billed.

Privacy is the reason this is opt-in and small. It holds only the star (never a
cast member), it is written only when the visitor ticks "remember", it is
owner-only, and forgetting it deletes the row and with it both blobs.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Row, delete, select, update
from sqlalchemy import insert as sa_insert
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError

from app.blobs import stash
from app.db import ChildProfileRow

__all__ = ["ChildProfile", "ChildProfileStore", "DatabaseChildProfileStore"]

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class ChildProfile:
    """The remembered child: who they are, and the pictures that drew them."""

    user_id: str
    name: str = ""
    photo_gender: str = ""
    story_key: str = ""
    settings: dict[str, Any] | None = None
    portrait: bytes = b""
    portrait_media_type: str = "image/png"
    portrait_sha256: str = ""
    photo: bytes = b""
    photo_media_type: str = "image/png"
    updated_at: str = ""


class ChildProfileStore:
    """The contract: remember one child per account, read it, forget it."""

    def save(self, profile: ChildProfile) -> ChildProfile:
        """Store (or replace) this account's remembered child."""
        raise NotImplementedError

    def get(self, user_id: str) -> ChildProfile | None:
        """The child this account asked to remember, or None."""
        raise NotImplementedError

    def forget(self, user_id: str) -> bool:
        """Delete the record. Returns whether there was one."""
        raise NotImplementedError


class DatabaseChildProfileStore(ChildProfileStore):
    """The `child_profiles` table, one row per account."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def save(self, profile: ChildProfile) -> ChildProfile:
        """Upsert by primary key, so remembering a second child replaces the first.

        The two pictures are stashed in the blob store before the transaction
        opens: binding them would otherwise encode and upload inside it, holding
        the connection (and on SQLite the write lock) for that whole time.

        A double-ticked checkbox can deliver two writes at once; both are
        upserts of the same account's row, so the loser simply writes last and
        the record is still one child.
        """
        portrait = stash(profile.portrait)
        photo = stash(profile.photo)
        now = _now()
        values = {
            "name": profile.name,
            "photo_gender": profile.photo_gender,
            "story_key": profile.story_key,
            "settings": profile.settings or {},
            "portrait": portrait,
            "portrait_media_type": profile.portrait_media_type,
            "portrait_sha256": profile.portrait_sha256,
            "photo": photo,
            "photo_media_type": profile.photo_media_type,
            "updated_at": now,
        }
        try:
            with self._engine.begin() as conn:
                changed = conn.execute(
                    update(ChildProfileRow)
                    .where(ChildProfileRow.user_id == profile.user_id)
                    .values(**values)
                )
                if not changed.rowcount:
                    conn.execute(
                        sa_insert(ChildProfileRow).values(
                            user_id=profile.user_id,
                            created_at=now,
                            **values,
                        )
                    )
        except IntegrityError:  # pragma: no cover - two first writes at once
            with self._engine.begin() as conn:
                conn.execute(
                    update(ChildProfileRow)
                    .where(ChildProfileRow.user_id == profile.user_id)
                    .values(**values)
                )
        logger.info("child_profile.saved user=%s story=%s", profile.user_id, profile.story_key)
        return self.get(profile.user_id) or profile

    def get(self, user_id: str) -> ChildProfile | None:
        with self._engine.connect() as conn:
            row = conn.execute(
                select(ChildProfileRow).where(ChildProfileRow.user_id == user_id)
            ).one_or_none()
        return _profile_from_row(row) if row is not None else None

    def forget(self, user_id: str) -> bool:
        with self._engine.begin() as conn:
            removed = conn.execute(
                delete(ChildProfileRow).where(ChildProfileRow.user_id == user_id)
            )
        forgot = bool(removed.rowcount)
        if forgot:
            logger.info("child_profile.forgotten user=%s", user_id)
        return forgot


def _profile_from_row(row: ChildProfileRow | Row[ChildProfileRow]) -> ChildProfile:
    updated_at: datetime | None = row.updated_at
    return ChildProfile(
        user_id=row.user_id,
        name=row.name,
        photo_gender=row.photo_gender,
        story_key=row.story_key,
        settings=dict(row.settings or {}),
        portrait=bytes(row.portrait or b""),
        portrait_media_type=row.portrait_media_type,
        portrait_sha256=row.portrait_sha256,
        photo=bytes(row.photo or b""),
        photo_media_type=row.photo_media_type,
        updated_at=updated_at.isoformat() if updated_at else "",
    )


def _now() -> datetime:
    """Naive UTC, the shape every dialect stores in a plain DateTime column."""
    return datetime.now(UTC).replace(tzinfo=None)
