"""The production schema, built the way production builds it.

A deployed process never calls `create_all`: `make migrate` runs Alembic and the
app boots on whatever that produced. The other database tests build their
tables from the models instead, so nothing would notice if the two drifted
apart — every insert would still succeed against a schema the app was not
written for, and only a real deployment would find out.

These tests run the operator's own command in a subprocess. A subprocess is
deliberate: `migrations/env.py` calls `fileConfig`, which reconfigures global
logging, and this suite's log assertions must not depend on which test ran
first. It also means the thing under test is the command in `Makefile`, not a
Python approximation of it.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import MetaData, create_engine

from app.db import Base, build_engine
from app.models.book import AboutMe
from app.services.book_store import BookStatus
from app.services.db_book_store import DatabaseBookStore
from app.services.user_store import DatabaseUserStore, User

ROOT = Path(__file__).resolve().parent.parent
VERSION_TABLE = "alembic_version"
TABLES = sorted(name for name in Base.metadata.tables if name != VERSION_TABLE)


def migrate(database_url: str) -> None:
    """Run `alembic upgrade head` against `database_url`, as `make migrate` does."""
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=ROOT,
        env={**os.environ, "DATABASE_URL": database_url},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def describe(metadata: MetaData) -> dict[str, dict[str, object]]:
    """Every table's columns, primary key and foreign keys, ignoring Alembic's own."""
    described: dict[str, dict[str, object]] = {}
    for name, table in metadata.tables.items():
        if name == VERSION_TABLE:
            continue
        described[name] = {
            "columns": {
                column.name: (str(column.type), column.nullable, column.primary_key)
                for column in table.columns
            },
            "primary_key": [column.name for column in table.primary_key],
            "foreign_keys": sorted(
                f"{column.name}->{foreign_key.column.table.name}.{foreign_key.column.name}"
                for column in table.columns
                for foreign_key in column.foreign_keys
            ),
        }
    return described


@pytest.fixture(scope="module")
def migrated_url(tmp_path_factory: pytest.TempPathFactory) -> str:
    """A fresh database, migrated once for this module."""
    path = tmp_path_factory.mktemp("migrated") / "schema.sqlite"
    url = f"sqlite:///{path}"
    migrate(url)
    return url


@pytest.fixture(scope="module")
def migrated(migrated_url: str) -> MetaData:
    """What the migration actually built, read back from the database."""
    engine = create_engine(migrated_url)
    try:
        metadata = MetaData()
        metadata.reflect(engine)
    finally:
        engine.dispose()
    return metadata


class TestTheMigratedSchema:
    def test_the_migration_applies_to_an_empty_database(self, migrated_url: str) -> None:
        # The fixture itself would have failed; this pins the intent for a reader.
        assert Path(migrated_url.removeprefix("sqlite:///")).exists()

    def test_it_creates_exactly_the_models_tables(self, migrated: MetaData) -> None:
        assert sorted(describe(migrated)) == TABLES

    @pytest.mark.parametrize("table", TABLES)
    def test_each_table_matches_the_models_column_for_column(
        self, migrated: MetaData, table: str
    ) -> None:
        assert describe(migrated)[table] == describe(Base.metadata)[table]

    def test_a_signed_in_person_and_their_book_round_trip_through_it(
        self, migrated_url: str
    ) -> None:
        """Shape is not enough: the schema has to accept the writes the ORM makes.

        The user's text fields and both timestamps are NOT NULL and supplied by
        the ORM itself (a client-side `now()`, never a database default), so an
        insert is the only thing that proves the migration and the models agree.
        """
        engine = build_engine(migrated_url)
        try:
            DatabaseUserStore(engine).upsert(
                User(sub="sub-1", email="shai@example.com", name="Shai")
            )
            store = DatabaseBookStore(engine)
            store.save_record(
                book_id="book-1",
                user_id="sub-1",
                about=AboutMe.model_validate({"prompt_name": "Shai", "about_name": "Shai"}),
                generation_id="gen-1",
            )
            store.save_page("book-1", 1, b"page art")
            store.save_status("book-1", BookStatus.RENDERING, [])

            assert store.owner_of("book-1") == "sub-1"
            assert store.read_page("book-1", 1) == b"page art"
            assert store.load_status("book-1") is not None
        finally:
            engine.dispose()

    def test_the_database_is_at_the_only_head(self, migrated_url: str) -> None:
        """One unbroken history, recorded in the database Alembic just built."""
        heads = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini"))).get_heads()
        assert len(heads) == 1, f"migration history is branched: {heads}"

        engine = create_engine(migrated_url)
        try:
            with engine.connect() as connection:
                recorded = connection.exec_driver_sql(
                    "SELECT version_num FROM alembic_version"
                ).scalar()
        finally:
            engine.dispose()
        assert recorded == heads[0]
