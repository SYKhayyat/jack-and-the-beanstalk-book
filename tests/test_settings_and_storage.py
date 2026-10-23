"""Settings validation and storage-path safety."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from app.config import ProviderName, Settings
from app.models import CartoonSettings
from app.models.settings import CartoonStyle, LikenessStrength
from app.services.storage import Draft, GenerationStore
from tests.helpers import tiny_png_bytes


class TestCartoonSettings:
    def test_defaults_are_sensible(self) -> None:
        defaults = CartoonSettings()
        assert defaults.cartoon_intensity == 8
        assert defaults.cartoon_style is CartoonStyle.WESTERN_CARTOON
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
    """A minimal valid draft: one cartoon, one photo, default settings."""
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


def get_settings_cache_clear() -> None:
    """Clear the settings singleton so an env change is observed."""
    from app.config import get_settings

    get_settings.cache_clear()
