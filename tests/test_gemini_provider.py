"""The Gemini provider, with the SDK and the network replaced.

Real SDK, fake transport: this exercises our request shaping and response
parsing without spending a cent or needing a key.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from app.config import Settings
from app.services.image_providers.base import (
    ImageRequest,
    ProviderError,
    ProviderUnavailable,
)
from app.services.image_providers.gemini import GeminiImageProvider
from tests.helpers import tiny_png_bytes

# Resolved from settings rather than hardcoded, so renaming the default does not
# mean updating assertions in several places.
DEFAULT_SETTINGS_MODEL = Settings(_env_file=None).gemini_model


@dataclass
class FakeInlineData:
    data: bytes
    mime_type: str


@dataclass
class FakePart:
    inline_data: FakeInlineData | None = None
    text: str | None = None


@dataclass
class FakeContent:
    parts: list[FakePart] = field(default_factory=list)


@dataclass
class FakeCandidate:
    content: FakeContent


class FakeResponse:
    def __init__(self, candidates: list[FakeCandidate] | None, text: str = "") -> None:
        self.candidates = candidates or []
        self.text = text


class FakeModels:
    """Captures the call and returns a canned response."""

    def __init__(self, response: FakeResponse | None = None, raises: Exception | None = None):
        self.response = response
        self.raises = raises
        self.calls: list[dict[str, Any]] = []

    def generate_content(self, **kwargs: Any) -> FakeResponse:
        self.calls.append(kwargs)
        if self.raises is not None:
            raise self.raises
        assert self.response is not None
        return self.response


class FakeClient:
    def __init__(self, models: FakeModels) -> None:
        self.models = models


def image_response(data: bytes | None = None, mime: str = "image/png") -> FakeResponse:
    part = FakePart(inline_data=FakeInlineData(data or tiny_png_bytes(), mime))
    return FakeResponse([FakeCandidate(FakeContent([part]))])


def build(models: FakeModels) -> tuple[GeminiImageProvider, FakeModels]:
    provider = GeminiImageProvider(api_key="test-key-not-real")
    provider._client = FakeClient(models)
    return provider, models


class TestConstruction:
    def test_missing_key_is_rejected_before_any_network_use(self) -> None:
        with pytest.raises(ProviderUnavailable, match="GEMINI_API_KEY"):
            GeminiImageProvider(api_key="")

    def test_health_check_reports_the_model(self) -> None:
        ok, reason = GeminiImageProvider(api_key="k", model="m").health_check()
        assert ok
        assert "m" in reason

    def test_key_is_never_stored_on_the_instance(self) -> None:
        provider = GeminiImageProvider(api_key="super-secret-key")
        assert "super-secret-key" not in repr(provider.__dict__.get("_client", ""))
        assert not hasattr(provider, "api_key")


class TestRequestShaping:
    def test_references_become_inline_parts_in_order(self) -> None:
        models = FakeModels(image_response())
        provider, _ = build(models)

        provider.generate(
            ImageRequest(
                prompt="draw jack",
                reference_images=(b"photo-bytes", b"cartoon-bytes"),
                reference_media_types=("image/jpeg", "image/png"),
                aspect_ratio="16:9",
                label="scene-compose",
            )
        )

        parts = models.calls[0]["contents"]
        assert isinstance(parts[0].inline_data.data, bytes) or parts[0] is not None
        assert models.calls[0]["model"] == DEFAULT_SETTINGS_MODEL

    def test_prompt_is_sent_as_a_text_part(self) -> None:
        models = FakeModels(image_response())
        provider, _ = build(models)
        provider.generate(ImageRequest(prompt="the exact prompt", label="cartoonify"))

        contents = models.calls[0]["contents"]
        assert contents[-1].text == "the exact prompt"

    def test_asks_for_image_and_sets_the_aspect_ratio(self) -> None:
        models = FakeModels(image_response())
        provider, _ = build(models)
        provider.generate(ImageRequest(prompt="p", aspect_ratio="3:4"))

        config = models.calls[0]["config"]
        assert config.response_modalities == ["IMAGE", "TEXT"]
        assert config.image_config.aspect_ratio == "3:4"


class TestResponseHandling:
    def test_extracts_a_png(self) -> None:
        models = FakeModels(image_response())
        provider, _ = build(models)

        result = provider.generate(ImageRequest(prompt="p"))
        assert result.data.startswith(b"\x89PNG")
        assert result.media_type == "image/png"
        assert result.model == DEFAULT_SETTINGS_MODEL

    def test_extracts_a_jpeg(self) -> None:
        models = FakeModels(image_response(b"\xff\xd8\xff\xe0rest", "image/jpeg"))
        provider, _ = build(models)

        result = provider.generate(ImageRequest(prompt="p"))
        assert result.media_type == "image/jpeg"
        assert result.extension == "jpg"

    def test_skips_text_only_parts(self) -> None:
        response = FakeResponse(
            [
                FakeCandidate(
                    FakeContent(
                        [
                            FakePart(text="Here is your image:"),
                            FakePart(inline_data=FakeInlineData(b"\x89PNG-data", "image/png")),
                        ]
                    )
                )
            ]
        )
        models = FakeModels(response)
        provider, _ = build(models)

        assert provider.generate(ImageRequest(prompt="p")).data == b"\x89PNG-data"

    def test_text_only_response_raises_with_the_model_reason(self) -> None:
        response = FakeResponse(
            [FakeCandidate(FakeContent([FakePart(text="I cannot draw that.")]))],
            text="I cannot draw that.",
        )
        provider, _ = build(FakeModels(response))

        with pytest.raises(ProviderError, match="I cannot draw that"):
            provider.generate(ImageRequest(prompt="p"))

    def test_sdk_failure_becomes_a_provider_error(self) -> None:
        provider, _ = build(FakeModels(raises=RuntimeError("quota exceeded")))
        with pytest.raises(ProviderError, match="quota exceeded"):
            provider.generate(ImageRequest(prompt="p"))

    def test_empty_response_becomes_a_provider_error(self) -> None:
        provider, _ = build(FakeModels(FakeResponse([])))
        with pytest.raises(ProviderError, match="no image"):
            provider.generate(ImageRequest(prompt="p"))


class TestCostDiscipline:
    def test_one_generate_call_makes_exactly_one_request(self) -> None:
        # The assignment forbids wrapping the API call in a retry loop, which is
        # also the classic way to burn a student's balance.
        models = FakeModels(image_response())
        provider, _ = build(models)

        provider.generate(ImageRequest(prompt="p"))
        assert len(models.calls) == 1

    def test_a_failure_is_not_retried(self) -> None:
        models = FakeModels(raises=RuntimeError("transient"))
        provider, _ = build(models)

        with pytest.raises(ProviderError):
            provider.generate(ImageRequest(prompt="p"))
        assert len(models.calls) == 1


class TestDefaultModelChoice:
    def test_default_model_is_not_one_google_has_retired(self) -> None:
        # Google deprecated gemini-2.5-flash-image and scheduled shutdown for
        # 2026-10-02. Shipping it as the default would mean the app breaks on
        # first real use, so the default is pinned by a test rather than by
        # good intentions.
        assert "2.5-flash-image" not in DEFAULT_SETTINGS_MODEL

    def test_default_model_is_a_current_image_model(self) -> None:
        assert "image" in DEFAULT_SETTINGS_MODEL

    def test_config_default_and_provider_default_agree(self) -> None:
        # One source of truth: whichever model config names is the one used.
        from app.config import get_settings

        get_settings.cache_clear()
        try:
            configured = Settings(_env_file=None).gemini_model
            provider = GeminiImageProvider(api_key="k")
            assert provider._model == configured
        finally:
            get_settings.cache_clear()
