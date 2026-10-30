"""Settings validation and storage-path safety."""

from __future__ import annotations

import io
import json
import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from time import time

import pytest
from PIL import Image
from pydantic import ValidationError

from app.config import ProviderName, Settings
from app.container import build_container
from app.main import expire_once
from app.models import CartoonSettings
from app.models.book import AboutMe
from app.models.settings import CartoonStyle, LikenessStrength
from app.services.book_store import BookStatus, FileBookStore
from app.services.storage import Draft, GenerationStore
from tests.helpers import RecordingProvider, tiny_png_bytes

ABOUT = AboutMe.model_validate({"prompt_name": "Jack", "about_name": "Jack"})
HOUR = 3600.0
DAY = 24 * HOUR


class TestCartoonSettings:
    def test_defaults_are_sensible(self) -> None:
        defaults = CartoonSettings()
        assert defaults.cartoon_intensity == 8
        assert defaults.cartoon_style is CartoonStyle.PIXAR_LIKE
        assert defaults.likeness_strength is LikenessStrength.STRICT

    @pytest.mark.parametrize("bad", [0, 11, -3])
    def test_intensity_bounds_are_enforced(self, bad: int) -> None:
        with pytest.raises(ValidationError):
            CartoonSettings(cartoon_intensity=bad)

    @pytest.mark.parametrize("intensity", [1, 5, 10])
    def test_intensity_boundaries_are_allowed(self, intensity: int) -> None:
        assert CartoonSettings(cartoon_intensity=intensity).cartoon_intensity == intensity

    def test_unknown_field_is_rejected(self) -> None:
        # extra="forbid" stops a typo in a form field silently doing nothing.
        with pytest.raises(ValidationError):
            CartoonSettings(carton_style="manga")  # type: ignore[call-arg]

    def test_unknown_enum_value_is_rejected(self) -> None:
        with pytest.raises(ValidationError):
            CartoonSettings(cartoon_style="impressionism")  # type: ignore[arg-type]

    def test_free_text_fields_are_length_capped(self) -> None:
        with pytest.raises(ValidationError):
            CartoonSettings(outfit="x" * 500)

    def test_identity_directive_names_the_identifying_features(self) -> None:
        directive = CartoonSettings().identity_directive()
        for feature in ("bone structure", "eye colour", "hairline", "freckles"):
            assert feature in directive


class TestSettingsConfig:
    def test_reads_the_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("GEMINI_API_KEY", "key-from-env")
        monkeypatch.setenv("GEMINI_MODEL", "some-other-model")
        get_settings_cache_clear()

        settings = Settings(_env_file=None)
        assert settings.gemini_api_key == "key-from-env"
        assert settings.gemini_model == "some-other-model"

    def test_provider_defaults_to_mock(self) -> None:
        assert Settings(_env_file=None).image_provider is ProviderName.MOCK

    def test_storage_dir_is_created(self, tmp_path: Path) -> None:
        target = tmp_path / "nested" / "storage"
        settings = Settings(_env_file=None, storage_dir=target)
        assert settings.storage_dir.is_dir()

    def test_max_upload_bytes_derives_from_megabytes(self) -> None:
        assert Settings(_env_file=None, max_upload_mb=10).max_upload_bytes == 10 * 1024 * 1024

    def test_production_flag(self) -> None:
        assert Settings(_env_file=None, app_env="production").is_production
        assert not Settings(_env_file=None, app_env="development").is_production


def make_draft(generation_id: str = "abc123") -> Draft:
    """A minimal valid draft: one cartoon, one photo, default settings.

    No `candidate_media_types`: this is deliberately the legacy shape.
    """
    return Draft(
        generation_id=generation_id,
        settings=CartoonSettings(),
        cartoon_prompt="cartoon prompt",
        candidates=(tiny_png_bytes(),),
        photo_data=tiny_png_bytes(),
        photo_media_type="image/png",
        provider="recording",
        model="recording-v1",
    )


def _jpeg_bytes() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (16, 12), "#334455").save(buffer, format="JPEG")
    return buffer.getvalue()


class TestPortraitMediaType:
    def test_a_jpeg_portrait_round_trips_its_type(self, tmp_path: Path) -> None:
        store = GenerationStore(tmp_path)
        draft = replace(
            make_draft(),
            candidates=(_jpeg_bytes(),),
            candidate_media_types=("image/jpeg",),
        )
        store.save_draft(draft)

        loaded = store.load_draft("abc123")
        assert loaded is not None
        assert loaded.cartoon_media_type == "image/jpeg"

    def test_a_legacy_draft_sniffs_its_type(self, tmp_path: Path) -> None:
        store = GenerationStore(tmp_path)
        store.save_draft(make_draft())  # no media types recorded

        loaded = store.load_draft("abc123")
        assert loaded is not None
        assert loaded.cartoon_media_type == "image/png"


class TestGenerationStore:
    def test_round_trips_a_draft(self, tmp_path: Path) -> None:
        store = GenerationStore(tmp_path)
        store.save_draft(make_draft())

        assert store.read_image("abc123", "cartoon") is not None
        assert store.read_image("abc123", "photo") is not None

    def test_drafts_are_isolated_from_each_other(self, tmp_path: Path) -> None:
        store = GenerationStore(tmp_path)
        store.save_draft(make_draft("first"))
        store.save_draft(make_draft("second"))

        assert store.read_image("first", "cartoon") is not None
        assert store.read_image("second", "cartoon") is not None

    def test_missing_draft_returns_none(self, tmp_path: Path) -> None:
        store = GenerationStore(tmp_path)
        assert store.read_image("nope", "cartoon") is None

    def test_unknown_stem_returns_none(self, tmp_path: Path) -> None:
        store = GenerationStore(tmp_path)
        store.save_draft(make_draft())
        assert store.read_image("abc123", "passwd") is None

    @pytest.mark.parametrize(
        "generation_id",
        ["../escape", "..", "a/b", "a\\b", ".hidden", "", "a\x00b"],
    )
    def test_traversal_attempts_are_refused(self, tmp_path: Path, generation_id: str) -> None:
        store = GenerationStore(tmp_path)
        store.save_draft(make_draft())
        assert store.read_image(generation_id, "cartoon") is None

    def test_delete_removes_the_draft(self, tmp_path: Path) -> None:
        store = GenerationStore(tmp_path)
        stored = store.save_draft(make_draft())
        store.delete("abc123")

        assert not stored.directory.exists()
        assert store.read_image("abc123", "cartoon") is None


def _age_files(directory: Path, seconds: float) -> None:
    """Backdate every file in `directory`, as if it had been written then."""
    stamp = time() - seconds
    for child in directory.iterdir():
        if child.is_file():
            os.utime(child, (stamp, stamp))


def _age_book(store: FileBookStore, book_id: str, seconds: float) -> None:
    """Rewrite a book's recorded start time, so retention sees it as old."""
    path = store.path(book_id, "book.json")
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["created_at"] = (datetime.now(UTC) - timedelta(seconds=seconds)).isoformat()
    path.write_text(json.dumps(payload), encoding="utf-8")


class TestDraftRetention:
    """#10: a child's upload must not be kept for the life of the deployment."""

    def test_an_old_draft_is_deleted_and_a_fresh_one_kept(self, tmp_path: Path) -> None:
        store = GenerationStore(tmp_path)
        store.save_draft(make_draft("old"))
        store.save_draft(make_draft("fresh"))
        _age_files(tmp_path / "old", 2 * HOUR)

        assert store.expire(older_than_seconds=HOUR) == 1

        assert not (tmp_path / "old").exists()
        assert store.load_draft("old") is None
        assert store.load_draft("fresh") is not None

    def test_the_newest_file_dates_a_draft(self, tmp_path: Path) -> None:
        """Writing a draft touches files inside it, so age must follow them."""
        store = GenerationStore(tmp_path)
        store.save_draft(make_draft("touched"))
        # Old portrait, freshly re-saved draft document: the draft is live.
        _age_files(tmp_path / "touched", 2 * HOUR)
        (tmp_path / "touched" / "draft.json").touch()

        assert store.expire(older_than_seconds=HOUR) == 0
        assert store.load_draft("touched") is not None

    def test_a_zero_window_deletes_nothing(self, tmp_path: Path) -> None:
        store = GenerationStore(tmp_path)
        store.save_draft(make_draft("kept"))
        assert store.expire(older_than_seconds=0) == 0
        assert store.load_draft("kept") is not None


class TestBookRetention:
    """#10: finished books are megabytes of a child's photographs."""

    def _book(self, store: FileBookStore, book_id: str, status: str) -> None:
        store.save_record(book_id=book_id, user_id=None, about=ABOUT, generation_id="gen-1")
        store.save_status(book_id, status, [])

    def test_an_old_finished_book_is_deleted(self, tmp_path: Path) -> None:
        store = FileBookStore(tmp_path / "books")
        self._book(store, "old", BookStatus.COMPLETE)
        store.save_page("old", 1, b"PNG-PAGE")
        self._book(store, "fresh", BookStatus.COMPLETE)
        _age_book(store, "old", 8 * DAY)

        assert store.expire(older_than_seconds=7 * DAY) == 1

        # The whole directory goes: pages, portrait and PDF with it.
        assert store.load_status("old") is None
        assert store.read_page("old", 1) is None
        assert not (tmp_path / "books" / "old").exists()
        assert store.load_status("fresh") is not None

    def test_an_old_failed_book_is_deleted(self, tmp_path: Path) -> None:
        store = FileBookStore(tmp_path / "books")
        self._book(store, "failed", BookStatus.FAILED)
        _age_book(store, "failed", 8 * DAY)

        assert store.expire(older_than_seconds=7 * DAY) == 1
        assert store.load_status("failed") is None

    @pytest.mark.parametrize("status", [BookStatus.PENDING, BookStatus.RENDERING])
    def test_a_live_book_is_never_expired(self, tmp_path: Path, status: str) -> None:
        """Age is not enough: a book that is still being made is not rubbish."""
        store = FileBookStore(tmp_path / "books")
        self._book(store, "live", status)
        _age_book(store, "live", 8 * DAY)

        assert store.expire(older_than_seconds=7 * DAY) == 0
        assert store.load_status("live") is not None

    def test_a_zero_window_deletes_nothing(self, tmp_path: Path) -> None:
        store = FileBookStore(tmp_path / "books")
        self._book(store, "kept", BookStatus.COMPLETE)
        assert store.expire(older_than_seconds=0) == 0
        assert store.load_status("kept") is not None


class TestTheSweep:
    """The lifespan sweep applies one policy to both stores."""

    def test_startup_deletes_everything_past_retention(self, settings: Settings) -> None:
        container = build_container(settings, provider=RecordingProvider())
        draft = container.store.save_draft(make_draft("old-draft"))
        _age_files(draft.directory, 25 * HOUR)
        container.book_store.save_record(
            book_id="old-book", user_id=None, about=ABOUT, generation_id="old-draft"
        )
        container.book_store.save_status("old-book", BookStatus.COMPLETE, [])
        _age_book(container.book_store, "old-book", 8 * DAY)

        assert expire_once(container) == (1, 1)

        assert container.store.load_draft("old-draft") is None
        assert container.book_store.load_status("old-book") is None

    def test_a_young_deployment_loses_nothing(self, settings: Settings) -> None:
        container = build_container(settings, provider=RecordingProvider())
        container.store.save_draft(make_draft("fresh-draft"))
        container.book_store.save_record(
            book_id="fresh-book", user_id=None, about=ABOUT, generation_id="fresh-draft"
        )
        container.book_store.save_status("fresh-book", BookStatus.COMPLETE, [])

        assert expire_once(container) == (0, 0)
        assert container.store.load_draft("fresh-draft") is not None
        assert container.book_store.load_status("fresh-book") is not None


def get_settings_cache_clear() -> None:
    """Clear the settings singleton so an env change is observed."""
    from app.config import get_settings

    get_settings.cache_clear()
