"""The OpenAI provider: request shaping and response parsing, no network.

Real SDK, fake transport. Cheap to run and it means the cheapest provider in the
app is also the best-tested one.
"""

from __future__ import annotations

import base64
import io
from dataclasses import dataclass, field
from typing import Any

import pytest
from PIL import Image

from app.config import Settings
from app.services.image_providers.base import (
    ImageRequest,
    ProviderError,
    ProviderUnavailable,
    TokenUsage,
)
from app.services.image_providers.openai_provider import (
    PORTRAIT_MAX_EDGE,
    QUALITIES,
    REFERENCE_MAX_EDGE,
    OpenAIImageProvider,
    size_for_aspect_ratio,
)
from tests.helpers import make_photo_bytes, tiny_png_bytes

PNG = tiny_png_bytes()
B64 = base64.b64encode(PNG).decode()


@dataclass
class FakeItem:
    b64_json: str | None = None
    url: str | None = None


@dataclass
class FakeResponse:
    data: list[FakeItem] = field(default_factory=list)
    usage: Any = None


@dataclass
class FakeDetails:
    text_tokens: int | None = None
    image_tokens: int | None = None


@dataclass
class FakeUsage:
    input_tokens: int | None = None
    output_tokens: int | None = None
    input_tokens_details: FakeDetails | None = None


class FakeImages:
    def __init__(self, response: FakeResponse | None = None, raises: Exception | None = None):
        self._response = response
        self._raises = raises
        self.calls: list[dict[str, Any]] = []

    def edit(self, **kwargs: Any) -> FakeResponse:
        self.calls.append(kwargs)
        if self._raises is not None:
            raise self._raises
        assert self._response is not None
        return self._response


class FakeClient:
    def __init__(self, images: FakeImages) -> None:
        self.images = images


def req(**overrides: Any) -> ImageRequest:
    """A request with the one reference image the provider requires."""
    defaults: dict[str, Any] = {
        "prompt": "draw jack",
        "reference_images": (PNG,),
        "reference_media_types": ("image/png",),
    }
    defaults.update(overrides)
    return ImageRequest(**defaults)  # type: ignore[arg-type]


def build(images: FakeImages) -> OpenAIImageProvider:
    provider = OpenAIImageProvider(api_key="test-key-not-real")
    provider._client = FakeClient(images)
    return provider


class TestConstruction:
    def test_missing_key_is_refused_before_any_request(self) -> None:
        with pytest.raises(ProviderUnavailable, match="OPENAI_API_KEY"):
            OpenAIImageProvider(api_key="")

    def test_unknown_quality_is_refused_at_startup(self) -> None:
        # Better than discovering it on the first billable call.
        with pytest.raises(ProviderUnavailable, match="Unknown OPENAI_QUALITY"):
            OpenAIImageProvider(api_key="k", quality="ultra")

    @pytest.mark.parametrize("quality", list(QUALITIES))
    def test_every_documented_quality_is_accepted(self, quality: str) -> None:
        assert OpenAIImageProvider(api_key="k", quality=quality).health_check()[0]

    def test_defaults_are_the_cheapest_configuration(self) -> None:
        settings = Settings(_env_file=None)
        assert settings.openai_model == "gpt-image-2.5-flare"
        assert settings.openai_quality == "low"

    def test_health_check_reports_the_configuration(self) -> None:
        ok, reason = OpenAIImageProvider(api_key="k", model="m", quality="high").health_check()
        assert ok
        assert "m" in reason and "high" in reason


class TestSizeMapping:
    @pytest.mark.parametrize(
        ("aspect", "expected"),
        [("1:1", "1024x1024"), ("4:3", "1152x864"), ("3:4", "864x1152"), ("16:9", "1344x768")],
    )
    def test_known_ratios_map_to_a_size(self, aspect: str, expected: str) -> None:
        assert size_for_aspect_ratio(aspect) == expected

    @pytest.mark.parametrize("aspect", ["1:1", "4:3", "3:4", "16:9"])
    def test_every_size_meets_the_documented_constraints(self, aspect: str) -> None:
        width, _, height = size_for_aspect_ratio(aspect).partition("x")
        w, h = int(width), int(height)
        assert w % 16 == 0 and h % 16 == 0, "edges must be multiples of 16"
        assert 1 / 3 <= w / h <= 3, "aspect ratio must be within 1:3..3:1"
        assert 655_360 <= w * h <= 8_294_400, "total pixels out of bounds"

    def test_unknown_ratio_is_rejected(self) -> None:
        with pytest.raises(ProviderError, match="Unsupported aspect ratio"):
            size_for_aspect_ratio("21:9")

    def test_error_lists_the_ratios_that_work(self) -> None:
        with pytest.raises(ProviderError, match="4:3"):
            size_for_aspect_ratio("banana")


class TestRequestShaping:
    def test_sends_the_prompt_model_quality_and_size(self) -> None:
        images = FakeImages(FakeResponse([FakeItem(b64_json=B64)]))
        provider = build(images)

        provider.generate(req(prompt="draw jack", aspect_ratio="4:3"))

        call = images.calls[0]
        assert call["prompt"] == "draw jack"
        assert call["model"] == "gpt-image-2.5-flare"
        assert call["quality"] == "low"
        assert call["size"] == "1152x864"
        assert call["output_format"] == "png"

    def test_omits_input_fidelity_because_it_is_unsupported_on_gpt_image_2(self) -> None:
        # OpenAI documents input_fidelity for gpt-image-1/1.5 and says to omit
        # it for gpt-image-2 and later.
        images = FakeImages(FakeResponse([FakeItem(b64_json=B64)]))
        build(images).generate(req(prompt="p"))

        assert "input_fidelity" not in images.calls[0]

    def test_single_reference_is_sent_bare(self) -> None:
        images = FakeImages(FakeResponse([FakeItem(b64_json=B64)]))
        build(images).generate(
            ImageRequest(
                prompt="p", reference_images=(PNG,), reference_media_types=("image/png",)
            )
        )

        assert not isinstance(images.calls[0]["image"], list)

    def test_two_references_are_sent_as_a_list(self) -> None:
        images = FakeImages(FakeResponse([FakeItem(b64_json=B64)]))
        build(images).generate(
            ImageRequest(
                prompt="p",
                reference_images=(PNG, PNG),
                reference_media_types=("image/png", "image/jpeg"),
            )
        )

        sent = images.calls[0]["image"]
        assert isinstance(sent, list) and len(sent) == 2

    def test_references_are_named_for_multipart(self) -> None:
        images = FakeImages(FakeResponse([FakeItem(b64_json=B64)]))
        build(images).generate(
            ImageRequest(
                prompt="p", reference_images=(PNG,), reference_media_types=("image/jpeg",)
            )
        )

        upload = images.calls[0]["image"]
        assert upload.name == "reference-0.jpg"
        assert upload.read() == PNG

    def test_no_reference_images_is_a_provider_error(self) -> None:
        with pytest.raises(ProviderError, match="at least one reference image"):
            build(FakeImages()).generate(ImageRequest(prompt="p"))

    def test_unknown_ratio_fails_before_any_request_is_made(self) -> None:
        images = FakeImages(FakeResponse([FakeItem(b64_json=B64)]))
        with pytest.raises(ProviderError):
            build(images).generate(req(prompt="p", aspect_ratio="5:1"))
        assert images.calls == []


class TestResponseHandling:
    def test_decodes_base64_png(self) -> None:
        images = FakeImages(FakeResponse([FakeItem(b64_json=B64)]))
        result = build(images).generate(req(prompt="p"))
        assert result.data == PNG
        assert result.media_type == "image/png"
        assert result.model == "gpt-image-2.5-flare"

    def test_takes_the_first_image_when_several_come_back(self) -> None:
        other = base64.b64encode(b"\x89PNG" + b"other").decode()
        images = FakeImages(FakeResponse([FakeItem(b64_json=B64), FakeItem(b64_json=other)]))
        assert build(images).generate(req(prompt="p")).data == PNG

    def test_skips_items_with_no_data(self) -> None:
        images = FakeImages(FakeResponse([FakeItem(), FakeItem(b64_json=B64)]))
        assert build(images).generate(req(prompt="p")).data == PNG

    def test_url_only_response_is_reported_clearly(self) -> None:
        images = FakeImages(FakeResponse([FakeItem(url="https://example.com/x.png")]))
        with pytest.raises(ProviderError, match="base64"):
            build(images).generate(req(prompt="p"))

    def test_empty_response_becomes_a_provider_error(self) -> None:
        images = FakeImages(FakeResponse([]))
        with pytest.raises(ProviderError, match="no image"):
            build(images).generate(req(prompt="p"))

    def test_undecodable_payload_is_a_provider_error(self) -> None:
        images = FakeImages(FakeResponse([FakeItem(b64_json="not base64!!")]))
        with pytest.raises(ProviderError, match="undecodable"):
            build(images).generate(req(prompt="p"))

    def test_sdk_failure_becomes_a_provider_error(self) -> None:
        images = FakeImages(raises=RuntimeError("content policy violation"))
        with pytest.raises(ProviderError, match="content policy violation"):
            build(images).generate(req(prompt="p"))


class TestCostDiscipline:
    def test_one_generate_makes_exactly_one_request(self) -> None:
        images = FakeImages(FakeResponse([FakeItem(b64_json=B64)]))
        build(images).generate(req(prompt="p"))
        assert len(images.calls) == 1

    def test_a_failure_is_not_retried(self) -> None:
        images = FakeImages(raises=RuntimeError("transient"))
        with pytest.raises(ProviderError):
            build(images).generate(req(prompt="p"))
        assert len(images.calls) == 1

    def test_the_client_disables_sdk_retries(self) -> None:
        # The SDK retries twice by default on connection errors. On a billable
        # endpoint that is the same trap as an explicit retry loop.
        provider = OpenAIImageProvider(api_key="k")
        assert getattr(provider._client, "max_retries", None) == 0


class TestSecretHandling:
    def test_key_is_stripped_of_surrounding_whitespace(self) -> None:
        # A key pasted into an env var with a trailing newline must still work,
        # rather than failing later with an illegal Authorization header.
        provider = OpenAIImageProvider(api_key="  sk-test\n")
        assert provider._client.api_key == "sk-test"

    def test_a_whitespace_only_key_is_refused(self) -> None:
        with pytest.raises(ProviderUnavailable, match="OPENAI_API_KEY"):
            OpenAIImageProvider(api_key="   \n")

    def test_a_failing_request_never_echoes_the_key(self) -> None:
        secret = "sk-supersecretvalue"
        images = FakeImages(raises=RuntimeError(f"Illegal header value b'Bearer {secret}\n'"))
        provider = OpenAIImageProvider(api_key=secret)
        provider._client = FakeClient(images)

        with pytest.raises(ProviderError) as caught:
            provider.generate(req(prompt="p"))

        assert secret not in str(caught.value)
        assert "REDACTED" in str(caught.value)

    def test_the_original_error_is_not_chained(self) -> None:
        # `from None` is what keeps the raw transport message (which can carry
        # the key) out of the logged traceback.
        images = FakeImages(raises=RuntimeError("boom"))
        with pytest.raises(ProviderError) as caught:
            build(images).generate(req(prompt="p"))

        assert caught.value.__cause__ is None
        assert caught.value.__suppress_context__ is True


class TestTokenUsage:
    """Each image carries its call's reported token bill, for the cost ledger."""

    def _usage(self, usage: Any) -> TokenUsage | None:
        response = FakeResponse(data=[FakeItem(b64_json=B64)], usage=usage)
        return build(FakeImages(response)).generate(req()).usage

    def test_text_and_image_input_are_split_as_reported(self) -> None:
        usage = FakeUsage(
            input_tokens=3100,
            output_tokens=196,
            input_tokens_details=FakeDetails(text_tokens=100, image_tokens=3000),
        )
        assert self._usage(usage) == TokenUsage(
            text_input_tokens=100, image_input_tokens=3000, output_tokens=196
        )

    def test_an_unsplit_input_is_counted_at_the_dearer_image_rate(self) -> None:
        usage = FakeUsage(input_tokens=3100, output_tokens=196)
        assert self._usage(usage) == TokenUsage(
            text_input_tokens=0, image_input_tokens=3100, output_tokens=196
        )

    def test_no_usage_is_none_so_the_call_is_unpriced_not_free(self) -> None:
        assert self._usage(None) is None
        assert self._usage(FakeUsage(output_tokens=196)) is None

    def test_every_candidate_carries_the_one_call_usage(self) -> None:
        usage = FakeUsage(
            input_tokens=10, output_tokens=600, input_tokens_details=FakeDetails(4, 6)
        )
        response = FakeResponse(data=[FakeItem(b64_json=B64)] * 3, usage=usage)
        images = build(FakeImages(response)).generate_candidates(req(), 3)
        assert {image.usage for image in images} == {TokenUsage(4, 6, 600)}


class TestReferenceSize:
    """References go up at most REFERENCE_MAX_EDGE on the long edge: fewer input tokens."""

    def _sent(self, data: bytes, media_type: str = "image/png") -> Any:
        images = FakeImages(FakeResponse([FakeItem(b64_json=B64)]))
        build(images).generate(
            ImageRequest(
                prompt="p", reference_images=(data,), reference_media_types=(media_type,)
            )
        )
        return images.calls[0]["image"]

    def test_a_large_reference_is_scaled_to_the_cap_keeping_its_shape(self) -> None:
        upload = self._sent(make_photo_bytes((1536, 1024)), "image/jpeg")
        assert upload.name == "reference-0.png"
        with Image.open(upload) as sent:
            assert sent.size == (REFERENCE_MAX_EDGE, round(REFERENCE_MAX_EDGE * 2 / 3))

    def test_a_small_reference_is_sent_untouched(self) -> None:
        small = make_photo_bytes((800, 600))
        assert self._sent(small).read() == small

    def test_transparency_survives_the_resize(self) -> None:
        buffer = io.BytesIO()
        Image.new("RGBA", (2048, 2048), (0, 0, 0, 0)).save(buffer, format="PNG")
        with Image.open(self._sent(buffer.getvalue())) as sent:
            assert sent.mode == "RGBA"

    def test_portraits_after_the_first_image_go_at_half_the_size(self) -> None:
        """The plate keeps its 1024; a portrait is read only for a face and a look."""
        images = FakeImages(FakeResponse([FakeItem(b64_json=B64)]))
        big = make_photo_bytes((1024, 1024))
        build(images).generate(
            ImageRequest(
                prompt="p",
                reference_images=(big, big, big),
                reference_media_types=("image/png",) * 3,
            )
        )
        plate, *portraits = images.calls[0]["image"]
        with Image.open(plate) as sent:
            assert max(sent.size) == REFERENCE_MAX_EDGE
        for portrait in portraits:
            with Image.open(portrait) as sent:
                assert max(sent.size) == PORTRAIT_MAX_EDGE
