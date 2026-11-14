"""Delete the stored objects no row points at any more.

Blobs are content-addressed and shared (`app/blobs.py`), so deleting a row
cannot delete its object: another row may hold the same picture. Without this
sweep, every expired book, forgotten child and deleted account would leave its
photographs in the bucket forever, which is the opposite of what the privacy
page promises (A8).

Run by the hourly retention sweep (`app.main.expire_once`) and by
`python -m app.scripts.migrate_blobs --sweep`.

Two guards keep it from deleting a picture that is about to be used:

* **Age.** An object written in the last `grace` is never deleted: a store
  uploads a picture before it opens the transaction that writes the pointer,
  so a fresh object may simply not be committed yet. A content-addressed
  re-upload rewrites the object, so its age restarts.
* **Two looks.** The referenced keys are read before the bucket is listed and
  again just before deleting, so a pointer copied from a row that was then
  deleted (a book duplicated a template's plate) is caught by the second look.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import LargeBinary, Table, select, type_coerce
from sqlalchemy.engine import Engine

from app.blobs import BlobRef, BlobStore, decode_pointer, is_pointer
from app.db import Base

__all__ = ["DEFAULT_GRACE", "blob_columns", "referenced_keys", "sweep_orphans"]

logger = logging.getLogger(__name__)

#: How old an unreferenced object must be before it is deleted. An upload and
#: its transaction take seconds; an hour is room for a slow one.
DEFAULT_GRACE = timedelta(hours=1)


def blob_columns() -> list[tuple[Table, str]]:
    """(table, column name) for every `BlobRef` column in the schema."""
    return [
        (table, column.name)
        for table in Base.metadata.sorted_tables
        for column in table.columns
        if isinstance(column.type, BlobRef)
    ]


def referenced_keys(engine: Engine) -> set[str]:
    """Every object key a row points at."""
    keys: set[str] = set()
    with engine.connect() as conn:
        for table, name in blob_columns():
            # The raw column, not the BlobRef: a pointer must not be fetched.
            column = table.c[name]
            raw = select(type_coerce(column, LargeBinary())).where(column.is_not(None))
            for (value,) in conn.execute(raw):
                data = bytes(value)
                if is_pointer(data):
                    keys.add(decode_pointer(data))
    return keys


def sweep_orphans(
    engine: Engine, store: BlobStore, *, grace: timedelta = DEFAULT_GRACE
) -> int:
    """Delete objects older than `grace` that no row points at; returns how many."""
    used = referenced_keys(engine)
    cutoff = datetime.now(UTC) - grace
    candidates = [
        key for key, written in store.listing() if key not in used and written < cutoff
    ]
    if not candidates:
        return 0
    used = referenced_keys(engine)
    deleted = 0
    for key in candidates:
        if key in used:
            continue
        store.delete(key)
        deleted += 1
    if deleted:
        logger.info("blobs.swept deleted=%d", deleted)
    return deleted
