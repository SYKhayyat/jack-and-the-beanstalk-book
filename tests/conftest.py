"""Shared fixtures.

Two things matter here:

1. `settings` never reads the developer's real `.env`, so a key on your machine
   cannot leak into a test run or change what the tests exercise.
2. `client` always runs against a stub provider, so the suite is offline,
   free, and deterministic.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.container import Container, build_container
from app.main import create_app
from app.services.image_providers.base import ImageProvider
from tests.helpers import RecordingProvider, make_photo_bytes


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    """Isolated settings pointed at a throwaway storage directory."""
    return Settings(
        image_provider="mock",  # type: ignore[arg-type]
        gemini_api_key="",
        app_env="test",
        storage_dir=tmp_path / "storage",
    )


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
    """Build a valid /api/generate form payload, with optional overrides."""
    form: dict[str, object] = {
        "photo": ("me.png", make_photo_bytes(), "image/png"),
        "scene": "planting-seeds",
    }
    form.update(overrides)
    return form
