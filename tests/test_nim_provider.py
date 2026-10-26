"""The NVIDIA NIM provider: request shaping and response parsing, no network.

Real HTTP client, fake transport. The transport records what went out, so the
tests can pin the exact JSON NIM is sent — the data-URI form of the reference
images, the size derived from the aspect ratio, and the condensed prompt.
"""

from __future__ import annotations

import base64
import io
import json
from typing import Any

import httpx
import pytest
from PIL import Image

from app.config import Settings
from app.services.image_providers.base import (
    ImageRequest,
    ProviderError,
    ProviderUnavailable,
)
from app.services.image_providers.nim_provider import (
    NimImageProvider,
    condense_prompt,
    size_for_aspect_ratio,
)
from tests.helpers import tiny_png_bytes

PNG = tiny_png_bytes()
B64 = base64.b64encode(PNG).decode()
JPEG = b"\xff\xd8\xff\xe0" + b"not really a jpeg" + b"\xff\xd9"


class Recorder:
    """A fake transport that records requests and replays canned answers."""

    def __init__(
        self,
        response: httpx.Response | None = None,
        raises: Exception | None = None,
    ) -> None:
        self._response = response
        self._raises = raises
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self._raises is not None:
            raise self._raises
        assert self._response is not None
        return self._response

    @property
    def count(self) -> int:
        return len(self.requests)

    def body(self, index: int = 0) -> dict[str, Any]:
        return json.loads(self.requests[index].content)


def image_response(encoded: str = B64) -> httpx.Response:
    return httpx.Response(
        200,
        json={"created": 1, "data": [{"b64_json": encoded}], "output_format": "png"},
    )


def _webp_bytes() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), "#2f7d4f").save(buffer, format="WEBP")
    return buffer.getvalue()


def build(recorder: Recorder, **kwargs: Any) -> NimImageProvider:
    provider = NimImageProvider(api_key="test-key-not-real", **kwargs)
    provider._client = httpx.Client(transport=httpx.MockTransport(recorder))
    return provider


def req(**overrides: Any) -> ImageRequest:
    defaults: dict[str, Any] = {
        "prompt": "draw jack",
        "reference_images": (PNG,),
        "reference_media_types": ("image/png",),
    }
    defaults.update(overrides)
    return ImageRequest(**defaults)  # type: ignore[arg-type]


class TestConstruction:
    def test_missing_key_is_refused_before_any_request(self) -> None:
        with pytest.raises(ProviderUnavailable, match="NIM_API_KEY"):
            NimImageProvider(api_key="")

    def test_whitespace_key_counts_as_missing(self) -> None:
        with pytest.raises(ProviderUnavailable, match="NIM_API_KEY"):
            NimImageProvider(api_key="  \n")

    def test_key_is_stripped_of_surrounding_whitespace(self) -> None:
        provider = NimImageProvider(api_key="  nvapi-test\n")
        assert provider._client.headers["authorization"] == "Bearer nvapi-test"

    def test_the_default_model_is_the_free_editing_one(self) -> None:
        settings = Settings(_env_file=None)
        assert settings.nim_model == "flux.2-klein-4b"

    def test_health_check_reports_the_model(self) -> None:
        ok, reason = NimImageProvider(api_key="k", model="m").health_check()
        assert ok
        assert "m" in reason


class TestSizeMapping:
    @pytest.mark.parametrize(
        ("aspect", "expected"),
        [("1:1", "1024x1024"), ("4:3", "1216x912"), ("3:4", "912x1216"), ("16:9", "1344x768")],
    )
    def test_known_ratios_map_to_a_size(self, aspect: str, expected: str) -> None:
        assert size_for_aspect_ratio(aspect) == expected

    @pytest.mark.parametrize("aspect", ["1:1", "4:3", "3:4", "16:9"])
    def test_every_size_is_inside_the_documented_edge_range(self, aspect: str) -> None:
        width, _, height = size_for_aspect_ratio(aspect).partition("x")
        w, h = int(width), int(height)
        assert 512 <= w <= 1344 and 512 <= h <= 1344
        assert w % 16 == 0 and h % 16 == 0, "edges must be multiples of 16"

    def test_unknown_ratio_is_rejected(self) -> None:
        with pytest.raises(ProviderError, match="Unsupported aspect ratio"):
            size_for_aspect_ratio("21:9")

    def test_error_lists_the_ratios_that_work(self) -> None:
        with pytest.raises(ProviderError, match="4:3"):
            size_for_aspect_ratio("banana")


class TestPromptCondensing:
    def test_a_prompt_that_fits_is_sent_unchanged(self) -> None:
        assert condense_prompt("short prompt", 800, 200) == "short prompt"

    def test_a_long_prompt_is_shortened_to_the_limit(self) -> None:
        condensed = condense_prompt("x" * 3000, 800, 200)
        assert len(condensed) <= 800

    def test_the_head_and_the_tail_survive(self) -> None:
        condensed = condense_prompt("HEAD" + "y" * 3000 + "TAIL", 800, 200)
        assert condensed.startswith("HEAD")
        assert condensed.endswith("TAIL")

    def test_a_limit_too_small_for_both_ends_keeps_the_head(self) -> None:
        condensed = condense_prompt("HEAD" + "y" * 100 + "TAIL", 10, 200)
        assert condensed == "HEAD" + "y" * 6


class TestRequestShaping:
    def test_sends_the_model_size_and_the_single_image_limit(self) -> None:
        recorder = Recorder(image_response())
        build(recorder).generate(req(prompt="draw jack", aspect_ratio="4:3"))

        body = recorder.body()
        assert body["model"] == "flux.2-klein-4b"
        assert body["size"] == "1216x912"
        assert body["n"] == 1
        assert body["response_format"] == "b64_json"
        assert body["prompt"] == "draw jack"

    def test_the_call_goes_to_the_openai_compatible_editing_endpoint(self) -> None:
        recorder = Recorder(image_response())
        build(recorder).generate(req())

        assert str(recorder.requests[0].url) == (
            "https://integrate.api.nvidia.com/v1/images/edits"
        )

    def test_the_key_travels_in_the_authorization_header(self) -> None:
        recorder = Recorder(image_response())
        provider = build(recorder)
        provider._api_key = "nvapi-secret"
        provider._client = httpx.Client(
            transport=httpx.MockTransport(recorder),
            headers={"Authorization": "Bearer nvapi-secret"},
        )
        provider.generate(req())

        assert recorder.requests[0].headers["authorization"] == "Bearer nvapi-secret"

    def test_reference_images_go_out_as_data_uris(self) -> None:
        recorder = Recorder(image_response())
        build(recorder).generate(req())

        images = recorder.body()["image"]
        assert isinstance(images, list) and len(images) == 1
        assert images[0].startswith("data:image/png;base64,")
        assert base64.b64decode(images[0].split(",", 1)[1]) == PNG

    def test_two_references_are_sent_as_two_data_uris(self) -> None:
        recorder = Recorder(image_response())
        build(recorder).generate(
            ImageRequest(
                prompt="p",
                reference_images=(PNG, PNG),
                reference_media_types=("image/png", "image/jpeg"),
            )
        )

        images = recorder.body()["image"]
        assert len(images) == 2
        assert images[1].startswith("data:image/jpeg;base64,")

    def test_a_long_prompt_is_condensed_before_it_is_sent(self) -> None:
        recorder = Recorder(image_response())
        build(recorder).generate(req(prompt="z" * 3000))

        assert len(recorder.body()["prompt"]) <= 800

    def test_a_forced_size_overrides_the_aspect_ratio(self) -> None:
        recorder = Recorder(image_response())
        build(recorder, size="1328x1328").generate(req(aspect_ratio="16:9"))

        assert recorder.body()["size"] == "1328x1328"

    def test_no_reference_images_is_a_provider_error(self) -> None:
        recorder = Recorder(image_response())
        with pytest.raises(ProviderError, match="at least one reference image"):
            build(recorder).generate(ImageRequest(prompt="p"))
        assert recorder.count == 0

    def test_an_unknown_ratio_fails_before_any_request_is_made(self) -> None:
        recorder = Recorder(image_response())
        with pytest.raises(ProviderError):
            build(recorder).generate(req(aspect_ratio="5:1"))
        assert recorder.count == 0


class TestReferenceFormats:
    def test_a_jpg_media_type_is_sent_as_jpeg(self) -> None:
        recorder = Recorder(image_response())
        build(recorder).generate(
            ImageRequest(
                prompt="p", reference_images=(PNG,), reference_media_types=("image/jpg",)
            )
        )

        assert recorder.body()["image"][0].startswith("data:image/jpeg;base64,")

    def test_a_format_nim_does_not_take_is_converted_to_png(self) -> None:
        recorder = Recorder(image_response())
        webp = _webp_bytes()
        build(recorder).generate(
            ImageRequest(
                prompt="p", reference_images=(webp,), reference_media_types=("image/webp",)
            )
        )

        sent = recorder.body()["image"][0]
        assert sent.startswith("data:image/png;base64,")
        assert base64.b64decode(sent.split(",", 1)[1]).startswith(b"\x89PNG")

    def test_an_unreadable_reference_is_a_provider_error(self) -> None:
        recorder = Recorder(image_response())
        with pytest.raises(ProviderError, match="could not be converted"):
            build(recorder).generate(
                ImageRequest(
                    prompt="p",
                    reference_images=(b"not an image",),
                    reference_media_types=("image/webp",),
                )
            )
        assert recorder.count == 0


class TestResponseHandling:
    def test_decodes_the_base64_image(self) -> None:
        recorder = Recorder(image_response())
        result = build(recorder).generate(req())
        assert result.data == PNG
        assert result.media_type == "image/png"
        assert result.model == "flux.2-klein-4b"

    def test_the_bytes_decide_the_media_type(self) -> None:
        recorder = Recorder(image_response(base64.b64encode(JPEG).decode()))
        result = build(recorder).generate(req())
        assert result.media_type == "image/jpeg"

    def test_an_empty_data_list_is_reported_clearly(self) -> None:
        recorder = Recorder(httpx.Response(200, json={"created": 1, "data": []}))
        with pytest.raises(ProviderError, match="no image"):
            build(recorder).generate(req())

    def test_a_response_without_base64_is_reported_clearly(self) -> None:
        recorder = Recorder(httpx.Response(200, json={"created": 1, "data": [{}]}))
        with pytest.raises(ProviderError, match="no image data"):
            build(recorder).generate(req())

    def test_a_non_json_response_is_reported_clearly(self) -> None:
        recorder = Recorder(httpx.Response(200, text="<html>oops</html>"))
        with pytest.raises(ProviderError, match="not JSON"):
            build(recorder).generate(req())

    def test_undecodable_payload_is_a_provider_error(self) -> None:
        recorder = Recorder(image_response("not base64!!"))
        with pytest.raises(ProviderError, match="undecodable"):
            build(recorder).generate(req())

    def test_an_empty_image_is_a_provider_error(self) -> None:
        recorder = Recorder(image_response(" "))
        with pytest.raises(ProviderError, match="empty image"):
            build(recorder).generate(req())

    def test_the_reported_format_is_used_when_the_bytes_do_not_say(self) -> None:
        recorder = Recorder(
            httpx.Response(
                200,
                json={
                    "created": 1,
                    "data": [{"b64_json": base64.b64encode(b"odd bytes").decode()}],
                    "output_format": "jpeg",
                },
            )
        )
        assert build(recorder).generate(req()).media_type == "image/jpeg"

    def test_png_is_the_last_resort_for_an_unknown_format(self) -> None:
        recorder = Recorder(
            httpx.Response(
                200,
                json={
                    "created": 1,
                    "data": [{"b64_json": base64.b64encode(b"odd bytes").decode()}],
                },
            )
        )
        assert build(recorder).generate(req()).media_type == "image/png"

    def test_a_response_of_an_unexpected_shape_is_reported_clearly(self) -> None:
        recorder = Recorder(httpx.Response(200, json=["not", "an", "object"]))
        with pytest.raises(ProviderError, match="unexpected response shape"):
            build(recorder).generate(req())


class TestFailures:
    def test_a_rate_limit_becomes_a_provider_error(self) -> None:
        recorder = Recorder(httpx.Response(429, json={"detail": "too many requests"}))
        with pytest.raises(ProviderError, match="429"):
            build(recorder).generate(req())

    def test_the_error_detail_is_carried_into_the_message(self) -> None:
        recorder = Recorder(httpx.Response(422, json={"detail": [{"msg": "prompt too long"}]}))
        with pytest.raises(ProviderError, match="prompt too long"):
            build(recorder).generate(req())

    def test_a_transport_failure_becomes_a_provider_error(self) -> None:
        recorder = Recorder(raises=httpx.ConnectError("boom"))
        with pytest.raises(ProviderError, match="boom"):
            build(recorder).generate(req())

    def test_a_failing_request_never_echoes_the_key(self) -> None:
        secret = "nvapi-supersecretvalue"
        recorder = Recorder(
            httpx.Response(401, json={"detail": f"bad key {secret}"}),
        )
        provider = NimImageProvider(api_key=secret)
        provider._client = httpx.Client(transport=httpx.MockTransport(recorder))

        with pytest.raises(ProviderError) as caught:
            provider.generate(req())

        assert secret not in str(caught.value)
        assert "REDACTED" in str(caught.value)

    def test_the_original_error_is_not_chained(self) -> None:
        recorder = Recorder(raises=httpx.ConnectError("boom"))
        with pytest.raises(ProviderError) as caught:
            build(recorder).generate(req())

        assert caught.value.__cause__ is None
        assert caught.value.__suppress_context__ is True

    def test_a_plain_text_failure_is_still_reported(self) -> None:
        recorder = Recorder(httpx.Response(503, text="service unavailable"))
        with pytest.raises(ProviderError, match="service unavailable"):
            build(recorder).generate(req())

    def test_a_failure_without_a_known_shape_falls_back_to_its_text(self) -> None:
        recorder = Recorder(httpx.Response(500, json={"unexpected": "shape"}))
        with pytest.raises(ProviderError, match="500"):
            build(recorder).generate(req())


class TestCostDiscipline:
    def test_one_generate_makes_exactly_one_request(self) -> None:
        recorder = Recorder(image_response())
        build(recorder).generate(req())
        assert recorder.count == 1

    def test_a_failure_is_not_retried(self) -> None:
        recorder = Recorder(raises=httpx.ConnectError("transient"))
        with pytest.raises(ProviderError):
            build(recorder).generate(req())
        assert recorder.count == 1

    def test_asking_for_three_candidates_costs_one_call(self) -> None:
        # NIM's editing endpoint allows n=1 only, so the alternatives are not
        # worth three calls.
        recorder = Recorder(image_response())
        images = build(recorder).generate_candidates(req(), 3)

        assert len(images) == 1
        assert recorder.count == 1

    def test_asking_for_no_candidates_is_a_programming_error(self) -> None:
        with pytest.raises(ValueError, match="at least 1"):
            build(Recorder(image_response())).generate_candidates(req(), 0)
