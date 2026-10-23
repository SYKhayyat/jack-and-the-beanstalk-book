"""Pipeline behaviour: two ordered passes, correct references, persisted output."""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from PIL import Image

from app.models import CartoonSettings, get_scene
from app.services.images import decode_upload
from app.services.pipeline import GenerationPipeline
from app.services.prompts import render_scene
from app.services.storage import GenerationStore
from tests.helpers import RecordingProvider, StubProvider, make_photo_bytes

PLANTING = get_scene("planting-seeds")
CLIMBING = get_scene("climbing-beanstalk")


@pytest.fixture
def decoded() -> tuple[bytes, str]:
    image = decode_upload(make_photo_bytes())
    return image.data, image.media_type


@pytest.fixture
def store(tmp_path: Path) -> GenerationStore:
    return GenerationStore(tmp_path / "storage")


class TestTwoPassStructure:
    def test_calls_the_provider_exactly_twice(
        self, provider: RecordingProvider, store: GenerationStore, decoded: tuple[bytes, str]
    ) -> None:
        pipeline = GenerationPipeline(provider, store)
        outcome = pipeline.run(
            photo_data=decoded[0],
            photo_media_type=decoded[1],
            scene=PLANTING,
            settings=CartoonSettings(),
            persist=False,
        )

        assert [request.label for request in provider.requests] == [
            "cartoonify",
            "scene-compose",
        ]
        assert outcome.result.cartoon_image
        assert outcome.result.scene_image

    def test_first_pass_uses_the_photo_only(
        self, provider: RecordingProvider, store: GenerationStore, decoded: tuple[bytes, str]
    ) -> None:
        GenerationPipeline(provider, store).run(
            photo_data=decoded[0],
            photo_media_type=decoded[1],
            scene=PLANTING,
            settings=CartoonSettings(),
            persist=False,
        )
        assert len(provider.requests[0].reference_images) == 1
        assert provider.requests[0].reference_images[0] == decoded[0]

    def test_second_pass_uses_both_the_cartoon_and_the_original(
        self, provider: RecordingProvider, store: GenerationStore, decoded: tuple[bytes, str]
    ) -> None:
        # This double anchor is the mechanism that keeps the face recognisable
        # once the scene action competes for the model's attention.
        GenerationPipeline(provider, store).run(
            photo_data=decoded[0],
            photo_media_type=decoded[1],
            scene=CLIMBING,
            settings=CartoonSettings(),
            persist=False,
        )
        second = provider.requests[1]
        assert len(second.reference_images) == 2
        assert second.reference_images[1] == decoded[0]

    def test_prompts_reach_the_provider(
        self, provider: RecordingProvider, store: GenerationStore, decoded: tuple[bytes, str]
    ) -> None:
        result = (
            GenerationPipeline(provider, store)
            .run(
                photo_data=decoded[0],
                photo_media_type=decoded[1],
                scene=PLANTING,
                settings=CartoonSettings(),
                persist=False,
            )
            .result
        )

        assert provider.requests[0].prompt == result.cartoon_prompt
        assert provider.requests[1].prompt == result.scene_prompt

    def test_aspect_ratio_is_forwarded(
        self, provider: RecordingProvider, store: GenerationStore, decoded: tuple[bytes, str]
    ) -> None:
        GenerationPipeline(provider, store).run(
            photo_data=decoded[0],
            photo_media_type=decoded[1],
            scene=PLANTING,
            settings=CartoonSettings(aspect_ratio="16:9"),
            persist=False,
        )
        assert all(request.aspect_ratio == "16:9" for request in provider.requests)


class TestResultContents:
    def test_caption_is_the_scene_specification_sent(
        self, provider: RecordingProvider, store: GenerationStore, decoded: tuple[bytes, str]
    ) -> None:
        result = (
            GenerationPipeline(provider, store)
            .run(
                photo_data=decoded[0],
                photo_media_type=decoded[1],
                scene=CLIMBING,
                settings=CartoonSettings(),
                persist=False,
            )
            .result
        )

        assert result.caption == render_scene(CLIMBING, CartoonSettings()).caption
        assert result.caption in provider.requests[1].prompt
        with Image.open(io.BytesIO(result.scene_image)) as scene_image:
            assert scene_image.format == "PNG"

    def test_records_provider_and_model(
        self, provider: RecordingProvider, store: GenerationStore, decoded: tuple[bytes, str]
    ) -> None:
        result = (
            GenerationPipeline(provider, store)
            .run(
                photo_data=decoded[0],
                photo_media_type=decoded[1],
                scene=PLANTING,
                settings=CartoonSettings(),
                persist=False,
            )
            .result
        )

        assert result.provider == "recording"
        assert result.model == "recording-v1"

    def test_generation_ids_are_unique(
        self, provider: RecordingProvider, store: GenerationStore, decoded: tuple[bytes, str]
    ) -> None:
        pipeline = GenerationPipeline(provider, store)
        first = pipeline.run(
            photo_data=decoded[0],
            photo_media_type=decoded[1],
            scene=PLANTING,
            settings=CartoonSettings(),
            persist=False,
        )
        second = pipeline.run(
            photo_data=decoded[0],
            photo_media_type=decoded[1],
            scene=PLANTING,
            settings=CartoonSettings(),
            persist=False,
        )
        assert first.result.generation_id != second.result.generation_id

    def test_suspiciously_small_output_is_warned_about(
        self, store: GenerationStore, decoded: tuple[bytes, str]
    ) -> None:
        result = (
            GenerationPipeline(StubProvider(b"x" * 10), store)
            .run(
                photo_data=decoded[0],
                photo_media_type=decoded[1],
                scene=PLANTING,
                settings=CartoonSettings(),
                persist=False,
            )
            .result
        )

        assert any("suspiciously small" in warning for warning in result.warnings)

    def test_empty_output_raises_rather_than_silently_passing(
        self, store: GenerationStore, decoded: tuple[bytes, str]
    ) -> None:
        with pytest.raises(ValueError, match="empty"):
            GenerationPipeline(StubProvider(b""), store).run(
                photo_data=decoded[0],
                photo_media_type=decoded[1],
                scene=PLANTING,
                settings=CartoonSettings(),
                persist=False,
            )


class TestPersistence:
    def test_saves_both_images_and_metadata(
        self, provider: RecordingProvider, store: GenerationStore, decoded: tuple[bytes, str]
    ) -> None:
        outcome = GenerationPipeline(provider, store).run(
            photo_data=decoded[0],
            photo_media_type=decoded[1],
            scene=PLANTING,
            settings=CartoonSettings(),
        )
        generation_id = outcome.result.generation_id

        assert store.read_image(generation_id, "cartoon") is not None
        assert store.read_image(generation_id, "scene") is not None

        meta = store.read_meta(generation_id)
        assert meta is not None
        assert meta["caption"] == render_scene(PLANTING, CartoonSettings()).caption
        assert meta["provider"] == "recording"
        assert meta["scene_key"] == "planting-seeds"

    def test_persist_false_writes_nothing(
        self, provider: RecordingProvider, store: GenerationStore, decoded: tuple[bytes, str]
    ) -> None:
        outcome = GenerationPipeline(provider, store).run(
            photo_data=decoded[0],
            photo_media_type=decoded[1],
            scene=PLANTING,
            settings=CartoonSettings(),
            persist=False,
        )
        assert store.read_meta(outcome.result.generation_id) is None

    def test_urls_point_at_the_served_paths(
        self, provider: RecordingProvider, store: GenerationStore, decoded: tuple[bytes, str]
    ) -> None:
        outcome = GenerationPipeline(provider, store).run(
            photo_data=decoded[0],
            photo_media_type=decoded[1],
            scene=PLANTING,
            settings=CartoonSettings(),
            persist=False,
        )
        urls = outcome.stored.to_urls()
        assert urls["scene"].startswith(f"/media/{outcome.result.generation_id}/")
