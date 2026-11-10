"""The studio's history: every portrait shown, and every take picked.

A portrait is a draft on disk that expires in a day, so without this the
operator could never answer "how many tries did that face take, and what did
the parent say was wrong?". One row per model call (`draw`) and one per pick
(`choose`), with the look settings, the ticked chips and the backend. No
pictures: those stay in the expiring draft.

Operator data only. Nothing here is shown to a visitor.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy import insert as sa_insert
from sqlalchemy.engine import Engine

from app.db import PortraitAttemptRow
from app.models.feedback import RejectionFeedback
from app.services.storage import Draft

__all__ = ["PortraitAttempt", "PortraitLog"]

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PortraitAttempt:
    """One row, as the operator screen shows it."""

    id: str
    user_id: str | None
    story_key: str
    cast_key: str
    kind: str
    settings: dict[str, Any]
    rejection: tuple[str, ...]
    provider: str
    model: str
    takes: int
    chosen_index: int | None
    source_id: str | None
    created_at: datetime


class PortraitLog:
    """Writes and reads `portrait_attempts`."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def record_draw(
        self, draft: Draft, *, user_id: str | None, rejection: RejectionFeedback | None
    ) -> None:
        """Log one portrait call. Never raises: history must not cost a portrait."""
        self._write(
            {
                "id": draft.generation_id,
                "user_id": user_id,
                "story_key": draft.story_key,
                "cast_key": draft.cast_key,
                "kind": "draw",
                "settings": draft.settings.model_dump(mode="json"),
                "rejection": [reason.value for reason in rejection.reasons]
                if rejection is not None
                else [],
                "provider": draft.provider[:32],
                "model": draft.model[:128],
                "takes": len(draft.candidates),
                "chosen_index": None,
                "source_id": None,
            }
        )

    def record_choice(
        self, chosen: Draft, *, source_id: str, index: int, user_id: str | None
    ) -> None:
        """Log the visitor keeping take `index` of draft `source_id` as `chosen`."""
        self._write(
            {
                "id": chosen.generation_id,
                "user_id": user_id,
                "story_key": chosen.story_key,
                "cast_key": chosen.cast_key,
                "kind": "choose",
                "settings": chosen.settings.model_dump(mode="json"),
                "rejection": [],
                "provider": chosen.provider[:32],
                "model": chosen.model[:128],
                "takes": 1,
                "chosen_index": index,
                "source_id": source_id,
            }
        )

    def lineage(self, generation_id: str) -> list[PortraitAttempt]:
        """The draft and, for a picked take, the draw it was picked from."""
        found: list[PortraitAttempt] = []
        current: str | None = generation_id
        with self._engine.connect() as conn:
            while current and len(found) < 4:
                row = conn.execute(
                    select(PortraitAttemptRow).where(PortraitAttemptRow.id == current)
                ).one_or_none()
                if row is None:
                    break
                found.append(_attempt(row))
                current = row.source_id
        return found

    def for_user(
        self, user_id: str, *, story_key: str, before: datetime, limit: int = 30
    ) -> list[PortraitAttempt]:
        """A person's portrait history for one story, newest first, up to `before`."""
        with self._engine.connect() as conn:
            rows = conn.execute(
                select(PortraitAttemptRow)
                .where(
                    PortraitAttemptRow.user_id == user_id,
                    PortraitAttemptRow.story_key == story_key,
                    PortraitAttemptRow.created_at <= before,
                )
                .order_by(PortraitAttemptRow.created_at.desc())
                .limit(limit)
            ).all()
        return [_attempt(row) for row in rows]

    def recent(self, limit: int = 50) -> list[PortraitAttempt]:
        with self._engine.connect() as conn:
            rows = conn.execute(
                select(PortraitAttemptRow)
                .order_by(PortraitAttemptRow.created_at.desc())
                .limit(limit)
            ).all()
        return [_attempt(row) for row in rows]

    def expire(self, *, older_than: timedelta) -> int:
        """Drop history older than a book is kept. Returns how many rows went."""
        cutoff = _now() - older_than
        with self._engine.begin() as conn:
            result = conn.execute(
                delete(PortraitAttemptRow).where(PortraitAttemptRow.created_at < cutoff)
            )
        return int(result.rowcount or 0)

    def _write(self, row: dict[str, Any]) -> None:
        try:
            with self._engine.begin() as conn:
                conn.execute(sa_insert(PortraitAttemptRow).values(**row, created_at=_now()))
        except Exception:
            logger.exception("portrait_log.record_failed id=%s", row.get("id"))


def _attempt(row: Any) -> PortraitAttempt:
    return PortraitAttempt(
        id=row.id,
        user_id=row.user_id,
        story_key=row.story_key,
        cast_key=row.cast_key,
        kind=row.kind,
        settings=dict(row.settings or {}),
        rejection=tuple(str(item) for item in (row.rejection or [])),
        provider=row.provider,
        model=row.model,
        takes=int(row.takes),
        chosen_index=row.chosen_index,
        source_id=row.source_id,
        created_at=row.created_at,
    )


def _now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)
