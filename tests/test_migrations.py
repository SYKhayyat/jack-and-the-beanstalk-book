"""The schema, built the way the app builds it.

Nothing calls `create_all` in the app: the one Alembic chain runs at boot
(`app.db.prepare_database`) and `make migrate` runs the same chain ahead of
time. The other database tests start from a copy of this migrated file, so
nothing would notice if the schema and the models drifted apart — every insert
would still succeed against a schema the app was not written for, and only a
real deployment would find out.

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

from app.config import Settings
from app.db import Base, build_engine, local_database_url, prepare_database
from app.models.book import AboutMe
from app.services.book_store import BookStatus
from app.services.db_book_store import DatabaseBookStore
from app.services.user_store import DatabaseUserStore, User

ROOT = Path(__file__).resolve().parent.parent
VERSION_TABLE = "alembic_version"
TABLES = sorted(name for name in Base.metadata.tables if name != VERSION_TABLE)


def migrate(database_url: str) -> None:
    """Run `alembic upgrade head` against `database_url`, as `make migrate` does."""
    alembic(database_url, "upgrade", "head")


def alembic(database_url: str, *arguments: str) -> None:
    """Run one Alembic command against `database_url` in a subprocess."""
    result = subprocess.run(
        [sys.executable, "-m", "alembic", *arguments],
        cwd=ROOT,
        env={**os.environ, "DATABASE_URL": database_url},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout


def describe(metadata: MetaData, *, affinity: bool = False) -> dict[str, dict[str, object]]:
    """Every table's columns, primary key and foreign keys, ignoring Alembic's own.

    `affinity` compares types by their generic family (BLOB and BYTEA are both
    binary), which is what a schema built on another engine can be held to.
    """
    described: dict[str, dict[str, object]] = {}
    for name, table in metadata.tables.items():
        if name == VERSION_TABLE:
            continue
        described[name] = {
            "columns": {
                column.name: (
                    column.type._type_affinity.__name__ if affinity else str(column.type),
                    column.nullable,
                    column.primary_key,
                )
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


def reflect(database_url: str) -> MetaData:
    engine = create_engine(database_url)
    try:
        metadata = MetaData()
        metadata.reflect(engine)
    finally:
        engine.dispose()
    return metadata


#: Postgres is opt-in: tests never need a server, but a developer (or CI with a
#: service container) can point this at a throwaway database to run the same
#: up/down cycle on the production engine. The database is wiped to `base`.
POSTGRES_URL = os.environ.get("TEST_POSTGRES_URL", "")


@pytest.fixture(
    params=[
        "sqlite",
        pytest.param(
            "postgres",
            marks=pytest.mark.skipif(
                not POSTGRES_URL, reason="set TEST_POSTGRES_URL to run against Postgres"
            ),
        ),
    ]
)
def empty_database(request: pytest.FixtureRequest, tmp_path: Path) -> str:
    """A database at `base`: a fresh SQLite file, or the opt-in Postgres one."""
    if request.param == "sqlite":
        return f"sqlite:///{tmp_path / 'cycle.sqlite'}"
    alembic(POSTGRES_URL, "downgrade", "base")
    return POSTGRES_URL


class TestTheWholeChain:
    """T3.2: the chain runs up and down on both engines, and re-running is a no-op."""

    def test_up_down_up_lands_on_the_models_each_time(self, empty_database: str) -> None:
        models = describe(Base.metadata, affinity=True)

        alembic(empty_database, "upgrade", "head")
        assert describe(reflect(empty_database), affinity=True) == models

        alembic(empty_database, "downgrade", "base")
        assert describe(reflect(empty_database), affinity=True) == {}

        alembic(empty_database, "upgrade", "head")
        assert describe(reflect(empty_database), affinity=True) == models

    def test_upgrading_twice_changes_nothing(self, empty_database: str) -> None:
        """`make migrate` is idempotent: a second run is not a second schema."""
        alembic(empty_database, "upgrade", "head")
        first = describe(reflect(empty_database), affinity=True)

        alembic(empty_database, "upgrade", "head")
        assert describe(reflect(empty_database), affinity=True) == first


def test_booting_with_no_url_creates_and_migrates_the_local_file(tmp_path: Path) -> None:
    """T3.1: the default configuration is a database, not a special case.

    An empty `DATABASE_URL` means one SQLite file under `STORAGE_DIR`; booting
    the app builds it and runs the same chain a deployment runs, and booting a
    second time is a cheap no-op rather than a second attempt at the schema.
    """
    settings = Settings(
        image_provider="mock",  # type: ignore[arg-type]
        gemini_api_key="",
        app_env="test",
        storage_dir=tmp_path / "storage",
    )
    assert settings.database_url == ""
    assert settings.effective_database_url == local_database_url(tmp_path / "storage")

    engine = prepare_database(build_engine(settings.effective_database_url))
    try:
        assert settings.local_database_path.is_file()
        assert describe(reflect(settings.effective_database_url), affinity=True) == describe(
            Base.metadata, affinity=True
        )
        prepare_database(engine)
    finally:
        engine.dispose()


class TestMigration0005:
    """T4.6: the story-template migration runs up and down on both engines."""

    def test_up_down_up_lands_on_the_models_each_time(self, empty_database: str) -> None:
        models = describe(Base.metadata, affinity=True)
        alembic(empty_database, "upgrade", "0004")
        before = describe(reflect(empty_database), affinity=True)

        alembic(empty_database, "upgrade", "head")
        assert describe(reflect(empty_database), affinity=True) == models

        alembic(empty_database, "downgrade", "0004")
        assert describe(reflect(empty_database), affinity=True) == before

        alembic(empty_database, "upgrade", "head")
        assert describe(reflect(empty_database), affinity=True) == models

        alembic(empty_database, "downgrade", "base")

    def test_a_book_made_before_it_becomes_an_unpinned_jack_book(
        self, empty_database: str
    ) -> None:
        """Existing rows survive: the new NOT NULL columns carry server defaults."""
        alembic(empty_database, "upgrade", "0004")
        engine = create_engine(empty_database)
        try:
            with engine.begin() as connection:
                connection.exec_driver_sql(
                    "INSERT INTO books (id, status, generation_id, about, created_at, "
                    "updated_at) VALUES ('old', 'complete', 'g', '{}', "
                    "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                )
                connection.exec_driver_sql(
                    "INSERT INTO book_pages (book_id, page_number, image, media_type) "
                    "VALUES ('old', 1, %s, 'image/png')"
                    % ("X'00'" if empty_database.startswith("sqlite") else "'\\x00'")
                )
            alembic(empty_database, "upgrade", "head")
            with engine.connect() as connection:
                book = connection.exec_driver_sql(
                    "SELECT story_key, template_version_id, cover_review FROM books"
                ).one()
                page = connection.exec_driver_sql(
                    "SELECT attempt_count, review, best_effort FROM book_pages"
                ).one()
            # And the store that serves it still reads the old rows (T3.4).
            store = DatabaseBookStore(engine)
            assert store.load_status("old") is not None
            assert store.read_page("old", 1) == b"\x00"
        finally:
            engine.dispose()
        assert tuple(book) == ("jack_and_the_beanstalk", None, None)
        assert page.attempt_count == 0
        assert page.review is None
        assert not page.best_effort
        alembic(empty_database, "downgrade", "base")
