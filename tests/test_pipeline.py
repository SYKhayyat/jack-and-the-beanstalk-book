"""Pipeline behaviour: the portrait pass, references, persisted drafts."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.models import CartoonSettings
from app.services.images import decode_upload
from app.services.pipeline import GenerationPipeline
from app.services.storage import GenerationStore
from tests.helpers import RecordingProvider, make_photo_bytes


@pytest.fixture
def decoded() -> tuple[bytes, str]:
    image = decode_upload(make_photo_bytes())
    return image.data, image.media_type


@pytest.fixture
def store(tmp_path: Path) -> GenerationStore:
    return GenerationStore(tmp_path / "storage")


class TestPortraitPass:
    def test_calls_the_provider_once(
        self, provider: RecordingProvider, store: GenerationStore, decoded: tuple[bytes, str]
    ) -> None:
        GenerationPipeline(provider, store).cartoonify(
            photo_data=decoded[0], photo_media_type=decoded[1], settings=CartoonSettings()
        )
        assert [request.label for request in provider.requests] == ["cartoonify"]

    def test_portrait_prompt_reaches_the_provider(
        self, provider: RecordingProvider, store: GenerationStore, decoded: tuple[bytes, str]
    ) -> None:
        draft = GenerationPipeline(provider, store).cartoonify(
            photo_data=decoded[0], photo_media_type=decoded[1], settings=CartoonSettings()
        )
        assert provider.requests[0].prompt == draft.cartoon_prompt
        assert draft.cartoon_prompt  # not an empty prompt

    def test_recording_provider_keeps_the_single_call_contract(
        self, provider: RecordingProvider, store: GenerationStore, decoded: tuple[bytes, str]
    ) -> None:
        """Providers that cannot batch return one image, not `count` calls."""
        draft = GenerationPipeline(provider, store).cartoonify(
            photo_data=decoded[0],
            photo_media_type=decoded[1],
            settings=CartoonSettings(candidates=3),
        )
        assert len(draft.candidates) == 1  # single call, correct over pretending

    def test_generation_ids_are_unique(
        self, provider: RecordingProvider, store: GenerationStore, decoded: tuple[bytes, str]
    ) -> None:
        pipeline = GenerationPipeline(provider, store)
        first = pipeline.cartoonify(
            photo_data=decoded[0], photo_media_type=decoded[1], settings=CartoonSettings()
        )
        second = pipeline.cartoonify(
            photo_data=decoded[0], photo_media_type=decoded[1], settings=CartoonSettings()
        )
        assert first.generation_id != second.generation_id


class TestPersistence:
    def test_persisted_draft_round_trips_through_the_store(
        self, provider: RecordingProvider, store: GenerationStore, decoded: tuple[bytes, str]
    ) -> None:
        settings = CartoonSettings()
        draft = GenerationPipeline(provider, store).cartoonify(
            photo_data=decoded[0], photo_media_type=decoded[1], settings=settings
        )
        loaded = store.load_draft(draft.generation_id)
        assert loaded is not None
        assert loaded.cartoon_image == draft.cartoon_image
        assert loaded.settings.apparent_age == settings.apparent_age
        assert loaded.photo_data == decoded[0]

    def test_persist_false_writes_nothing(
        self, provider: RecordingProvider, store: GenerationStore, decoded: tuple[bytes, str]
    ) -> None:
        draft = GenerationPipeline(provider, store).cartoonify(
            photo_data=decoded[0],
            photo_media_type=decoded[1],
            settings=CartoonSettings(),
            persist=False,
        )
        assert store.load_draft(draft.generation_id) is None


class TestUnknownGeneration:
    def test_unknown_draft_has_a_friendly_message(self) -> None:
        from app.services.pipeline import UnknownGeneration

        assert "expired" in UnknownGeneration("does-not-exist").args[0].lower()
