"""Shared fixtures.

Three things matter here:

1. `settings` never reads the developer's real `.env`, so a key on your machine
   cannot leak into a test run or change what the tests exercise.
2. `client` always runs against a stub provider, so the suite is offline,
   free, and deterministic.
3. Every test gets its own SQLite file, built once per session and copied. That
   is the same database the app runs on with no `DATABASE_URL`, so the tests
   exercise the real store, the real schema and the boot-time migration —
   without a Postgres server and without paying for a migration chain per test.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine

from app.books import SEEDS
from app.config import Settings
from app.container import Container, build_container
from app.db import build_engine, prepare_database
from app.event_loop_guard import set_strict
from app.main import create_app
from app.services.image_providers.base import ImageProvider
from app.services.template_store import TemplateStore
from tests.helpers import RecordingProvider, isolated_settings, make_photo_bytes

# `Settings` reads `.env` by default. A developer's real `.env` (Google
# credentials, a database URL) must never leak into a test run or change what
# the tests exercise, so the suite always builds settings from scratch.
Settings.model_config["env_file"] = None

# A database query on the event loop fails the test that made it (AUDIT §10 A5).
set_strict(True)


@pytest.fixture(scope="session")
def schema_template(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A database migrated to head once, copied into each test's storage dir.

    The app migrates at boot; running the whole chain from empty for every one
    of hundreds of tests would dominate the suite. A copy still goes through
    the boot path (a no-op upgrade against a database already at head), so the
    migration is still exercised, and `tests/test_migrations.py` owns the
    from-empty up/down proof.
    """
    path = tmp_path_factory.mktemp("schema") / "app.sqlite3"
    engine = build_engine(f"sqlite:///{path}")
    try:
        prepare_database(engine)
    finally:
        engine.dispose()
    return path


@pytest.fixture(scope="session")
def seeded_template(tmp_path_factory: pytest.TempPathFactory, schema_template: Path) -> Path:
    """The same database with the story seeds loaded, as the app's boot leaves it.

    Booting seeds the stories; starting from a seeded copy makes that boot step
    the no-op it is on every restart after the first.
    """
    path = tmp_path_factory.mktemp("seeded") / "app.sqlite3"
    shutil.copyfile(schema_template, path)
    engine = build_engine(f"sqlite:///{path}")
    try:
        TemplateStore(engine).seed(SEEDS)
    finally:
        engine.dispose()
    return path


@pytest.fixture
def engine(tmp_path: Path, schema_template: Path) -> Iterator[Engine]:
    """A throwaway SQLite database at head, for store-level tests."""
    path = tmp_path / "app.sqlite3"
    shutil.copyfile(schema_template, path)
    built = build_engine(f"sqlite:///{path}")
    try:
        yield built
    finally:
        built.dispose()


@pytest.fixture
def settings(tmp_path: Path, seeded_template: Path) -> Settings:
    return isolated_settings(tmp_path, seeded_template)


@pytest.fixture
def provider() -> RecordingProvider:
    return RecordingProvider()


@pytest.fixture
def container(settings: Settings, provider: RecordingProvider) -> Container:
    return build_container(settings, provider=provider)


@pytest.fixture
def client(settings: Settings, provider: RecordingProvider) -> Iterator[TestClient]:
    """A test client wired to the recording provider."""
    app = create_app(settings, provider=provider)
    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture
def client_factory(settings: Settings):
    """Build a test client around an arbitrary provider (failure paths etc.)."""

    @contextmanager
    def build(provider: ImageProvider) -> Iterator[TestClient]:
        app = create_app(settings, provider=provider)
        with TestClient(app) as test_client:
            yield test_client

    return build


@pytest.fixture
def photo() -> bytes:
    return make_photo_bytes()


def generate_form(**overrides: object) -> dict[str, object]:
    """Build a valid /api/cartoonify form payload (a Jack portrait), with overrides."""
    form: dict[str, object] = {
        "photo": ("me.png", make_photo_bytes(), "image/png"),
        "story": "jack_and_the_beanstalk",
        "photo_gender": "boy",
    }
    form.update(overrides)
    return form
