"""The operator's stop buttons (Idea 10).

Two switches, because there are two different things to stop:

* `new_books` — refuse work that would spend money from now on: a portrait, a
  new book, an edit. The visitor gets one plain sentence, and the wallet is
  never touched.
* `drawing` — let the jobs in flight stop before their next picture. A book is
  left resumable, not failed: the pictures already paid for are kept, and
  turning the switch back on resumes them.

They live in the database, not the environment, because a kill switch is only
worth having if it works in the second it is needed. A missing row is off, so
an empty database is a working one and no seeding is required.

`is_on` is read by the render loop between pages, so the table is read through
a short cache: the answer may be a couple of seconds old, which is the same
order as the page timeout and far below a deploy.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from time import monotonic

from sqlalchemy import insert as sa_insert
from sqlalchemy import select
from sqlalchemy import update as sa_update
from sqlalchemy.engine import Engine

from app.db import RuntimeSwitchRow

__all__ = [
    "SWITCHES",
    "SWITCH_DRAWING",
    "SWITCH_LABELS",
    "SWITCH_NEW_BOOKS",
    "Switch",
    "Switches",
]

logger = logging.getLogger(__name__)

#: Work that would spend money from now on: a portrait, a book, an edit.
SWITCH_NEW_BOOKS = "new_books"
#: The jobs already running: stop them before their next picture.
SWITCH_DRAWING = "drawing"

#: Every switch, in the order the operator's page lists them.
SWITCHES: tuple[str, ...] = (SWITCH_NEW_BOOKS, SWITCH_DRAWING)

SWITCH_LABELS: dict[str, str] = {
    SWITCH_NEW_BOOKS: "Stop new books and portraits",
    SWITCH_DRAWING: "Stop drawing the books in progress",
}

#: How long an answer may be reused. The render loop asks between pages; two
#: seconds is nothing next to a picture, and it keeps the check off the database
#: on every page of every book.
_CACHE_SECONDS = 2.0


@dataclass(frozen=True, slots=True)
class Switch:
    """One switch, as the operator page shows it."""

    name: str
    on: bool = False
    note: str = ""
    changed_by: str = ""
    changed_at: str = ""

    @property
    def label(self) -> str:
        return SWITCH_LABELS.get(self.name, self.name)


class Switches:
    """Reads and writes `runtime_switches`, with a very short read cache."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self._cache: tuple[float, dict[str, bool]] | None = None

    def all(self) -> dict[str, Switch]:
        """Every switch's current state, defaults included."""
        with self._engine.connect() as conn:
            rows = conn.execute(select(RuntimeSwitchRow)).all()
        stored = {row.name: row for row in rows}
        found: dict[str, Switch] = {}
        for name in SWITCHES:
            row = stored.get(name)
            if row is None:
                found[name] = Switch(name=name)
                continue
            changed_at: datetime | None = row.changed_at
            found[name] = Switch(
                name=name,
                on=bool(row.on),
                note=row.note or "",
                changed_by=row.changed_by or "",
                changed_at=changed_at.isoformat() if changed_at else "",
            )
        return found

    def is_on(self, name: str, *, fresh: bool = False) -> bool:
        """Whether `name` is pulled. Missing rows and unknown names are off.

        `fresh` skips the cache: the operator's own request must see what it
        just wrote, not the answer from a second ago.
        """
        cached = self._cache
        if not fresh and cached is not None and monotonic() - cached[0] < _CACHE_SECONDS:
            return cached[1].get(name, False)
        states = self.all()
        self._cache = (monotonic(), {key: item.on for key, item in states.items()})
        return states.get(name, Switch(name=name)).on

    def set(self, name: str, on: bool, *, operator: str, note: str = "") -> Switch:
        """Pull or release a switch, and say who did it.

        Raises:
            ValueError: for a name that is not a switch, so a typo in a form
                cannot create a row nothing reads.
        """
        if name not in SWITCHES:
            raise ValueError(
                f"{name!r} is not a switch; the switches are {', '.join(SWITCHES)}"
            )
        now = datetime.now(UTC).replace(tzinfo=None)
        with self._engine.begin() as conn:
            changed = conn.execute(
                sa_update(RuntimeSwitchRow)
                .where(RuntimeSwitchRow.name == name)
                .values(on=on, note=note, changed_by=operator, changed_at=now)
            )
            if not changed.rowcount:
                conn.execute(
                    sa_insert(RuntimeSwitchRow).values(
                        name=name,
                        on=on,
                        note=note,
                        changed_by=operator,
                        changed_at=now,
                    )
                )
        self._cache = None
        logger.warning(
            "operator.switch name=%s on=%s operator=%s note=%s",
            name,
            on,
            operator,
            note or "-",
        )
        return self.all()[name]
