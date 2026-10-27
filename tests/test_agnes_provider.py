"""The Agnes provider: request shaping and response parsing, no network.

Real HTTP client, fake transport. The transport records what went out, so the
tests can pin the JSON Agnes receives — the tier-based size, the reference
images as Data URIs inside `extra_body`, and the b64_json output format.
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
from app.services.image_providers.agnes_provider import (
    AgnesImageProvider,
    size_for_aspect_ratio,
)
from app.services.image_providers.base import ImageRequest, ProviderError, ProviderUnavailable
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
        json={"created": 1780000000, "data": [{"url": None, "b64_json": encoded}]},
    )


def build(recorder: Recorder, **kwargs: Any) -> AgnesImageProvider:
    provider = AgnesImageProvider(api_key="test-key-not-real", **kwargs)
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
        with pytest.raises(ProviderUnavailable, match="AGNES_API_KEY"):
            AgnesImageProvider(api_key="")

    def test_whitespace_key_counts_as_missing(self) -> None:
        with pytest.raises(ProviderUnavailable, match="AGNES_API_KEY"):
            AgnesImageProvider(api_key="  \n")

    def test_key_is_stripped_of_surrounding_whitespace(self) -> None:
        provider = AgnesImageProvider(api_key="  agnes-test\n")
        assert provider._client.headers["authorization"] == "Bearer agnes-test"

    def test_an_unknown_size_tier_is_refused_at_startup(self) -> None:
        # Better than discovering it on the first real generation.
        with pytest.raises(ProviderUnavailable, match="Unknown AGNES_SIZE"):
            AgnesImageProvider(api_key="k", size="8K")

    def test_the_default_model_is_the_reference_image_one(self) -> None:
        assert Settings(_env_file=None).agnes_model == "agnes-image-2.5-flash"

    def test_the_default_size_is_the_one_the_free_tier_sustains(self) -> None:
        # 1K: about 10 requests a minute on the free tier; 2K is 5, 3K is one.
        assert Settings(_env_file=None).agnes_size == "1K"

    def test_health_check_reports_the_configuration(self) -> None:
        ok, reason = AgnesImageProvider(api_key="k", model="m", size="2K").health_check()
        assert ok
        assert "m" in reason and "2K" in reason


class TestSizeMapping:
    @pytest.mark.parametrize("aspect", ["1:1", "4:3", "3:4", "16:9"])
    def test_every_ratio_the_app_offers_maps_to_a_tier(self, aspect: str) -> None:
        size, ratio = size_for_aspect_ratio(aspect)
        assert size == "1K"
        assert ratio == aspect

    def test_unknown_ratio_is_rejected(self) -> None:
        with pytest.raises(ProviderError, match="Unsupported aspect ratio"):
            size_for_aspect_ratio("9:20")

    def test_error_lists_the_ratios_that_work(self) -> None:
        with pytest.raises(ProviderError, match="4:3"):
            size_for_aspect_ratio("banana")


class TestRequestShaping:
    def test_sends_the_model_prompt_size_and_ratio(self) -> None:
        recorder = Recorder(image_response())
        build(recorder).generate(req(prompt="draw jack", aspect_ratio="4:3"))

        body = recorder.body()
        assert body["model"] == "agnes-image-2.5-flash"
        assert body["prompt"] == "draw jack"
        assert body["size"] == "1K"
        assert body["ratio"] == "4:3"

    def test_references_and_output_format_live_in_extra_body(self) -> None:
        # The docs call a top-level response_format the common mistake, and
        # every worked example puts both under extra_body.
        recorder = Recorder(image_response())
        build(recorder).generate(req())

        body = recorder.body()
        extra = body["extra_body"]
        assert extra["response_format"] == "b64_json"
        assert isinstance(extra["image"], list) and len(extra["image"]) == 1
        assert "response_format" not in body
        assert "image" not in body

    def test_the_call_goes_to_the_images_generations_endpoint(self) -> None:
        recorder = Recorder(image_response())
        build(recorder).generate(req())

        assert str(recorder.requests[0].url) == (
            "https://apihub.agnes-ai.com/v1/images/generations"
        )

    def test_references_go_out_as_data_uris(self) -> None:
        recorder = Recorder(image_response())
        build(recorder).generate(req())

        sent = recorder.body()["extra_body"]["image"][0]
        assert sent.startswith("data:image/png;base64,")
        assert base64.b64decode(sent.split(",", 1)[1]) == PNG

    def test_two_references_are_sent_as_two_data_uris(self) -> None:
        # The book pass sends the portrait and the original photo.
        recorder = Recorder(image_response())
        build(recorder).generate(
            ImageRequest(
                prompt="p",
                reference_images=(PNG, PNG),
                reference_media_types=("image/png", "image/jpeg"),
            )
        )

        images = recorder.body()["extra_body"]["image"]
        assert len(images) == 2
        assert images[1].startswith("data:image/jpeg;base64,")

    def test_a_jpg_media_type_is_sent_as_jpeg(self) -> None:
        recorder = Recorder(image_response())
        build(recorder).generate(
            ImageRequest(
                prompt="p", reference_images=(PNG,), reference_media_types=("image/jpg",)
            )
        )

        assert recorder.body()["extra_body"]["image"][0].startswith("data:image/jpeg;base64,")

    def test_a_forced_size_tier_is_used(self) -> None:
        recorder = Recorder(image_response())
        build(recorder, size="2K").generate(req(aspect_ratio="16:9"))

        assert recorder.body()["size"] == "2K"

    def test_no_reference_images_is_a_provider_error(self) -> None:
        recorder = Recorder(image_response())
        with pytest.raises(ProviderError, match="at least one reference image"):
            build(recorder).generate(ImageRequest(prompt="p"))
        assert recorder.count == 0

    def test_a_webp_reference_is_converted_to_png(self) -> None:
        recorder = Recorder(image_response())
        buffer = io.BytesIO()
        Image.new("RGB", (8, 8), "#2f7d4f").save(buffer, format="WEBP")
        build(recorder).generate(
            ImageRequest(
                prompt="p",
                reference_images=(buffer.getvalue(),),
                reference_media_types=("image/webp",),
            )
        )

        sent = recorder.body()["extra_body"]["image"][0]
        assert sent.startswith("data:image/png;base64,")


class TestResponseHandling:
    def test_decodes_the_base64_image(self) -> None:
        recorder = Recorder(image_response())
        result = build(recorder).generate(req())
        assert result.data == PNG
        assert result.media_type == "image/png"
        assert result.model == "agnes-image-2.5-flash"

    def test_the_bytes_decide_the_media_type(self) -> None:
        recorder = Recorder(image_response(base64.b64encode(JPEG).decode()))
        result = build(recorder).generate(req())
        assert result.media_type == "image/jpeg"

    def test_an_empty_data_list_is_reported_clearly(self) -> None:
        recorder = Recorder(httpx.Response(200, json={"created": 1, "data": []}))
        with pytest.raises(ProviderError, match="no image"):
            build(recorder).generate(req())

    def test_a_url_only_response_is_reported_clearly(self) -> None:
        recorder = Recorder(
            httpx.Response(
                200, json={"created": 1, "data": [{"url": "https://example.com/x.png"}]}
            )
        )
        with pytest.raises(ProviderError, match="URL instead of image data"):
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

    def test_a_response_of_an_unexpected_shape_is_reported_clearly(self) -> None:
        recorder = Recorder(httpx.Response(200, json=["not", "an", "object"]))
        with pytest.raises(ProviderError, match="unexpected response shape"):
            build(recorder).generate(req())

    def test_a_data_item_with_nothing_in_it_is_reported_clearly(self) -> None:
        recorder = Recorder(httpx.Response(200, json={"created": 1, "data": [{}]}))
        with pytest.raises(ProviderError, match="no image data"):
            build(recorder).generate(req())

    def test_unrecognised_bytes_fall_back_to_png(self) -> None:
        # The extension the app gives the file must be one it can serve.
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

    def test_webp_output_is_named_as_webp(self) -> None:
        webp = b"RIFF" + b"\x00\x00\x00\x00" + b"WEBP" + b"payload"
        recorder = Recorder(image_response(base64.b64encode(webp).decode()))
        assert build(recorder).generate(req()).media_type == "image/webp"


class TestFailures:
    def test_a_rate_limit_becomes_a_provider_error(self) -> None:
        recorder = Recorder(httpx.Response(429, json={"error": {"message": "rate limited"}}))
        with pytest.raises(ProviderError, match="429"):
            build(recorder).generate(req())

    def test_the_error_message_is_carried_into_the_exception(self) -> None:
        recorder = Recorder(
            httpx.Response(400, json={"error": {"message": "image parameter missing"}})
        )
        with pytest.raises(ProviderError, match="image parameter missing"):
            build(recorder).generate(req())

    def test_a_transport_failure_becomes_a_provider_error(self) -> None:
        recorder = Recorder(raises=httpx.ConnectError("boom"))
        with pytest.raises(ProviderError, match="boom"):
            build(recorder).generate(req())

    def test_a_failing_request_never_echoes_the_key(self) -> None:
        secret = "agnes-supersecretvalue"
        recorder = Recorder(
            httpx.Response(401, json={"error": {"message": f"bad key {secret}"}}),
        )
        provider = AgnesImageProvider(api_key=secret)
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

    def test_a_list_valued_error_is_joined_into_one_line(self) -> None:
        recorder = Recorder(httpx.Response(422, json={"detail": [{"msg": "prompt required"}]}))
        with pytest.raises(ProviderError, match="prompt required"):
            build(recorder).generate(req())

    def test_a_plain_text_failure_is_still_reported(self) -> None:
        recorder = Recorder(httpx.Response(503, text="service unavailable"))
        with pytest.raises(ProviderError, match="service unavailable"):
            build(recorder).generate(req())

    def test_a_failure_without_a_known_shape_falls_back_to_its_text(self) -> None:
        recorder = Recorder(httpx.Response(500, json={"unexpected": "shape"}))
        with pytest.raises(ProviderError, match="500"):
            build(recorder).generate(req())

    def test_a_long_error_message_is_truncated_not_dumped(self) -> None:
        # A second retry of the same message adds nothing; the operator needs
        # the shape of the problem, not 3000 characters of vendor prose.
        recorder = Recorder(httpx.Response(400, text="x" * 2000))
        with pytest.raises(ProviderError) as caught:
            build(recorder).generate(req())

        assert len(str(caught.value)) < 400
        assert "..." in str(caught.value)


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
        # One call returns one picture, and every call is a request against the
        # free tier's per-minute ceiling.
        recorder = Recorder(image_response())
        images = build(recorder).generate_candidates(req(), 3)

        assert len(images) == 1
        assert recorder.count == 1

    def test_asking_for_no_candidates_is_a_programming_error(self) -> None:
        with pytest.raises(ValueError, match="at least 1"):
            build(Recorder(image_response())).generate_candidates(req(), 0)

    def test_the_client_waits_long_enough_for_a_slow_image(self) -> None:
        # The docs recommend 60s-360s; the app-wide ceiling is 600s.
        provider = AgnesImageProvider(api_key="k", timeout_seconds=600)
        assert provider._client.timeout.read == 360
