"""Move inline pictures and PDFs out of the database into R2, and sweep orphans.

    python -m app.scripts.migrate_blobs            # move every inline blob (idempotent)
    python -m app.scripts.migrate_blobs --sweep    # list R2 objects no row points at
    python -m app.scripts.migrate_blobs --sweep --delete

Uses the database the app would (`DATABASE_URL`, else the local SQLite file)
and the R2 settings from the environment. Each blob is uploaded, read back and
compared byte for byte before its row is rewritten to a pointer, so a failed or
interrupted run loses nothing; re-running picks up where it stopped.

After a Postgres run the freed space is not returned to the host until
`VACUUM FULL` is run on the big tables (Neon: from its SQL editor).
"""

from __future__ import annotations

import argparse
from typing import Any

from app.scripts._venv import ensure_venv

ensure_venv()

from sqlalchemy import text  # noqa: E402
from sqlalchemy.engine import Engine  # noqa: E402

from app.blobs import (  # noqa: E402
    BlobRef,
    BlobStore,
    R2BlobStore,
    decode_pointer,
    is_pointer,
    same_content,
    set_blob_store,
)
from app.config import get_settings  # noqa: E402
from app.db import Base, build_engine, prepare_database  # noqa: E402


def blob_columns() -> list[tuple[str, list[str], str]]:
    """(table, primary-key columns, blob column) for every BlobRef column."""
    found = []
    for table in Base.metadata.sorted_tables:
        keys = [c.name for c in table.primary_key.columns]
        for column in table.columns:
            if isinstance(column.type, BlobRef):
                found.append((table.name, keys, column.name))
    return found


def migrate(engine: Engine, store: BlobStore) -> tuple[int, int]:
    """Returns (rows moved, bytes that left the database)."""
    moved = freed = 0
    for table, keys, column in blob_columns():
        pk = ", ".join(keys)
        where = " AND ".join(f"{k} = :{k}" for k in keys)
        with engine.connect() as conn:
            ids = conn.execute(
                text(f"SELECT {pk} FROM {table} WHERE {column} IS NOT NULL")
            ).fetchall()
        for row in ids:
            params = dict(zip(keys, row, strict=True))
            with engine.begin() as conn:
                value: Any = conn.execute(
                    text(f"SELECT {column} FROM {table} WHERE {where}"),
                    params,
                ).scalar_one()
                if value is None or is_pointer(bytes(value)):
                    continue
                data = bytes(value)
                pointer = store.put(data)
                if not same_content(data, store.get(pointer)):
                    raise RuntimeError(f"{table}.{column} {params}: read-back differs")
                conn.execute(
                    text(f"UPDATE {table} SET {column} = :p WHERE {where}"),
                    {**params, "p": pointer},
                )
                moved += 1
                freed += len(data)
        print(f"{table}.{column}: done")
    return moved, freed


def referenced_keys(engine: Engine) -> set[str]:
    keys_in_use: set[str] = set()
    for table, _keys, column in blob_columns():
        with engine.connect() as conn:
            for row in conn.execute(
                text(f"SELECT {column} FROM {table} WHERE {column} IS NOT NULL")
            ):
                value = bytes(row[0])
                if is_pointer(value):
                    keys_in_use.add(decode_pointer(value))
    return keys_in_use


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--sweep", action="store_true", help="find unreferenced objects")
    parser.add_argument("--delete", action="store_true", help="with --sweep: delete them")
    args = parser.parse_args()

    settings = get_settings()
    if not settings.r2_enabled:
        print("R2 is not configured: set the four R2_* variables.")
        return 2
    store = R2BlobStore.connect(
        account_id=settings.r2_account_id,
        bucket=settings.r2_bucket,
        access_key_id=settings.r2_access_key_id,
        secret_access_key=settings.r2_secret_access_key,
    )
    set_blob_store(store)
    engine = prepare_database(build_engine(settings.effective_database_url))
    try:
        if args.sweep:
            used = referenced_keys(engine)
            orphans = [k for k in iter(store.keys()) if k not in used]
            print(f"{len(used)} referenced, {len(orphans)} orphaned")
            if args.delete:
                for key in orphans:
                    store.delete(key)
                print("deleted")
            return 0
        moved, freed = migrate(engine, store)
        print(f"moved {moved} blobs, {freed / 1e6:.1f} MB out of the database")
    finally:
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
