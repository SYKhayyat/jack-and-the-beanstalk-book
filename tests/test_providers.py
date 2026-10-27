"""Provider construction and the mock provider's contract.

These tests are what let the app change image backends safely: they pin the
failure messages a misconfigured deployment will actually see.
"""

from __future__ import annotations

import pytest

from app.config import Settings
from app.services.image_providers import MockImageProvider, build_provider
from app.services.image_providers.base import (
    ImageProvider,
    ImageRequest,
    ProviderUnavailable,
)
from app.services.image_providers.registry import KNOWN_PROVIDERS


def make_settings(**overrides: object) -> Settings:
    return Settings(_env_file=None, storage_dir=overrides.pop("storage_dir"), **overrides)  # type: ignore[arg-type]


class TestRegistry:
    def test_mock_needs_no_key(self, tmp_path) -> None:
        provider = build_provider(make_settings(image_provider="mock", storage_dir=tmp_path))
        assert isinstance(provider, MockImageProvider)

    def test_gemini_without_a_key_is_unavailable(self, tmp_path) -> None:
        with pytest.raises(ProviderUnavailable, match="GEMINI_API_KEY"):
            build_provider(
                make_settings(image_provider="gemini", gemini_api_key="", storage_dir=tmp_path)
            )

    def test_whitespace_key_counts_as_missing(self, tmp_path) -> None:
        with pytest.raises(ProviderUnavailable):
            build_provider(
                make_settings(
                    image_provider="gemini", gemini_api_key="   ", storage_dir=tmp_path
                )
            )

    def test_unknown_provider_is_reported_clearly(self) -> None:
        with pytest.raises(ProviderUnavailable, match="Unknown image provider"):
            build_provider(Settings.model_construct(image_provider="dalle3"))

    def test_every_registered_provider_implements_the_contract(self) -> None:
        for name, provider_cls in KNOWN_PROVIDERS.items():
            assert issubclass(provider_cls, ImageProvider)
            assert provider_cls.name == name.value
            assert callable(provider_cls.generate)


class TestTheBackendChoiceIsTheOperators:
    """The provider and the model are picked in the environment, and nowhere else."""

    def test_the_active_model_follows_the_chosen_provider(self) -> None:
        openai = Settings(_env_file=None, image_provider="openai", openai_model="o")
        gemini = Settings(_env_file=None, image_provider="gemini", gemini_model="g")
        mock = Settings(_env_file=None, image_provider="mock")

        assert openai.active_model == "o"
        assert gemini.active_model == "g"
        assert mock.active_model == "-"

    def test_the_user_settings_cannot_carry_a_backend(self) -> None:
        # The visitor's settings object forbids unknown fields, so a request
        # cannot smuggle in a provider or a model.
        from pydantic import ValidationError

        from app.models import CartoonSettings

        with pytest.raises(ValidationError):
            CartoonSettings.model_validate({"provider": "mock", "model": "gpt-image-2"})


class TestMockProvider:
    def test_returns_a_decodable_image(self) -> None:
        result = MockImageProvider().generate(ImageRequest(prompt="hello", label="test"))
        assert result.data.startswith(b"\x89PNG")
        assert result.media_type == "image/png"
        assert result.extension == "png"

    def test_is_deterministic(self) -> None:
        request = ImageRequest(prompt="the same prompt", label="cartoonify")
        first = MockImageProvider().generate(request)
        second = MockImageProvider().generate(request)
        assert first.data == second.data

    def test_different_prompts_produce_different_images(self) -> None:
        one = MockImageProvider().generate(ImageRequest(prompt="planting seeds", label="a"))
        two = MockImageProvider().generate(
            ImageRequest(prompt="climbing the beanstalk", label="a")
        )
        assert one.data != two.data

    def test_labels_are_distinct_in_the_output(self) -> None:
        portrait = MockImageProvider().generate(
            ImageRequest(prompt="same", label="cartoonify")
        )
        scene = MockImageProvider().generate(
            ImageRequest(prompt="same", label="scene-compose")
        )
        assert portrait.data != scene.data

    def test_health_check_is_ok(self) -> None:
        ok, reason = MockImageProvider().health_check()
        assert ok and reason == "ok"


class TestPipelineThroughMockProvider:
    """End-to-end over the real pipeline with only the network faked out."""

    def test_full_run_produces_a_persisted_portrait(self, settings: Settings) -> None:
        from app.models import CartoonSettings
        from app.services.images import decode_upload
        from app.services.pipeline import GenerationPipeline
        from app.services.storage import GenerationStore
        from tests.helpers import make_photo_bytes

        pipeline = GenerationPipeline(
            MockImageProvider(), GenerationStore(settings.storage_dir)
        )
        decoded = decode_upload(make_photo_bytes())

        draft = pipeline.cartoonify(
            photo_data=decoded.data,
            photo_media_type=decoded.media_type,
            settings=CartoonSettings(),
        )

        assert draft.cartoon_image.startswith(b"\x89PNG")
        stored_draft = GenerationStore(settings.storage_dir).load_draft(draft.generation_id)
        assert stored_draft is not None
        assert stored_draft.cartoon_image == draft.cartoon_image
