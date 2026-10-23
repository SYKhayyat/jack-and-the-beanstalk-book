"""HTTP contract tests: the routes a browser (or Render's health check) uses."""

from __future__ import annotations

import json

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.models import CartoonStyle
from app.services.image_providers.base import ProviderError, ProviderUnavailable
from tests.conftest import generate_form
from tests.helpers import (
    StubProvider,
    make_noisy_photo_bytes,
    make_photo_bytes,
    multipart,
)


class TestIndexPage:
    def test_renders(self, client: TestClient) -> None:
        response = client.get("/")
        assert response.status_code == 200
        assert "Jack and the Beanstalk" in response.text

    def test_offers_both_required_scenes(self, client: TestClient) -> None:
        body = client.get("/").text
        assert "Jack is planting the seeds" in body
        assert "Jack is climbing the beanstalk" in body

    def test_offers_every_setting_control(self, client: TestClient) -> None:
        body = client.get("/").text
        for field in (
            'name="cartoon_intensity"',
            'name="cartoon_style"',
            'name="likeness_strength"',
            'name="character_gender"',
            'name="photo_gender"',
            'name="skin_tone"',
            'name="apparent_age"',
            'name="hair_style"',
            'name="hair_color"',
            'name="outfit"',
            'name="expression"',
            'name="keep_glasses"',
            'name="keep_facial_hair"',
            'name="scene"',
            'name="photo"',
        ):
            assert field in body, f"missing control: {field}"

    def test_shows_which_provider_is_active(self, client: TestClient) -> None:
        assert "recording" in client.get("/").text


class TestCatalogueEndpoints:
    def test_scenes_lists_both_scenes(self, client: TestClient) -> None:
        payload = client.get("/api/scenes").json()
        keys = [scene["key"] for scene in payload["scenes"]]
        assert keys == ["planting-seeds", "climbing-beanstalk"]

    def test_scene_caption_matches_the_catalog(self, client: TestClient) -> None:
        payload = client.get("/api/scenes").json()
        for scene in payload["scenes"]:
            assert scene["caption"].endswith(".")
            assert scene["title"]

    def test_settings_options_expose_every_enum(self, client: TestClient) -> None:
        payload = client.get("/api/settings-options").json()
        assert len(payload["cartoon_style"]) == len(CartoonStyle)
        assert payload["limits"]["cartoon_intensity"] == {"min": 1, "max": 10}
        assert payload["defaults"]["cartoon_intensity"] == 8


class TestGenerateEndpoint:
    def test_returns_201_and_a_caption(self, client: TestClient) -> None:
        response = client.post("/api/generate", **multipart(generate_form()))
        assert response.status_code == 201

        payload = response.json()
        assert payload["caption"].startswith("Jack is planting the seeds.")
        assert payload["scene_key"] == "planting-seeds"

    def test_returns_urls_that_serve_real_images(self, client: TestClient) -> None:
        payload = client.post("/api/generate", **multipart(generate_form())).json()

        for key in ("cartoon_url", "scene_url"):
            image = client.get(payload["images"][key])
            assert image.status_code == 200
            assert image.headers["content-type"].startswith("image/")

    def test_both_scenes_work_for_the_same_person(self, client: TestClient) -> None:
        photo = make_photo_bytes()
        captions = []

        for scene in ("planting-seeds", "climbing-beanstalk"):
            form = generate_form(photo=("me.png", photo, "image/png"), scene=scene)
            response = client.post("/api/generate", **multipart(form))
            assert response.status_code == 201
            captions.append(response.json()["caption"])

        assert captions[0] != captions[1]
        assert "climbing the beanstalk" in captions[1]

    def test_echoes_the_settings_used(self, client: TestClient) -> None:
        form = generate_form(
            cartoon_intensity=3, cartoon_style="manga", skin_tone="deep_brown"
        )
        payload = client.post("/api/generate", **multipart(form)).json()

        assert payload["settings"]["cartoon_intensity"] == 3
        assert payload["settings"]["cartoon_style"] == "manga"
        assert payload["settings"]["skin_tone"] == "deep_brown"

    def test_settings_change_the_prompt(self, client: TestClient) -> None:
        form = generate_form(cartoon_style="manga", character_gender="girl")
        prompts = client.post("/api/generate", **multipart(form)).json()["prompts"]

        assert "screentone" in prompts["cartoon"].lower()
        assert "girl" in prompts["scene"]

    def test_girl_settings_produce_jane_and_her_pronouns(self, client: TestClient) -> None:
        form = generate_form(character_gender="girl")
        payload = client.post("/api/generate", **multipart(form)).json()

        assert payload["scene_title"].startswith("Jane")
        assert payload["caption"].startswith("Jane is planting the seeds.")
        assert "Jane" in payload["prompts"]["scene"]

    def test_returns_both_prompts(self, client: TestClient) -> None:
        response = client.post("/api/generate", **multipart(generate_form()))
        prompts = response.json()["prompts"]

        assert "Preserve the identity" in prompts["cartoon"]
        assert "Preserve the identity" in prompts["scene"]

    def test_meta_endpoint_returns_the_prompts_and_caption(self, client: TestClient) -> None:
        payload = client.post("/api/generate", **multipart(generate_form())).json()
        meta = client.get(payload["meta_url"]).json()

        assert meta["caption"] == payload["caption"]
        assert meta["model"] == "recording-v1"
        assert meta["scene_prompt"]

    def test_response_never_contains_the_api_key(
        self, client: TestClient, settings: Settings
    ) -> None:
        body = client.post("/api/generate", **multipart(generate_form())).text
        assert "gemini_api_key" not in body.lower()


class TestGenerateValidation:
    def test_unknown_scene_is_rejected(self, client: TestClient) -> None:
        response = client.post(
            "/api/generate", **multipart(generate_form(scene="dancing-giant"))
        )
        assert response.status_code == 400
        assert response.json()["error"] == "unknown_scene"

    def test_missing_photo_is_rejected(self, client: TestClient) -> None:
        response = client.post("/api/generate", data={"scene": "planting-seeds"})
        assert response.status_code == 422

    def test_out_of_range_intensity_is_rejected(self, client: TestClient) -> None:
        response = client.post(
            "/api/generate", **multipart(generate_form(cartoon_intensity=99))
        )
        assert response.status_code == 422

    def test_unknown_style_is_rejected(self, client: TestClient) -> None:
        response = client.post(
            "/api/generate", **multipart(generate_form(cartoon_style="oil_painting"))
        )
        assert response.status_code == 422
        assert response.json()["error"] == "invalid_settings"

    def test_non_image_upload_is_rejected(self, client: TestClient) -> None:
        form = generate_form(photo=("payload.png", b"#!/bin/sh\necho hi", "image/png"))
        response = client.post("/api/generate", **multipart(form))

        assert response.status_code == 400
        assert response.json()["error"] == "invalid_image"

    def test_generation_rate_limit_stops_repeat_billable_requests(
        self, settings: Settings
    ) -> None:
        # The allowance counts billable images, not requests: one /api/generate
        # is two image calls (portrait + scene), so the tightest window that
        # admits one such request is 2.
        tight = settings.model_copy(update={"max_generations_per_minute": 2})
        client = TestClient(create_app(tight, provider=StubProvider(b"x")))

        assert client.post("/api/generate", **multipart(generate_form())).status_code == 201
        response = client.post("/api/generate", **multipart(generate_form()))

        assert response.status_code == 429
        assert response.json()["error"] == "generation_rate_limited"
        assert response.headers["retry-after"] == "60"

    def test_empty_upload_is_rejected(self, client: TestClient) -> None:
        form = generate_form(photo=("empty.png", b"", "image/png"))
        response = client.post("/api/generate", **multipart(form))
        assert response.status_code == 400

    def test_oversized_upload_is_rejected(self, settings: Settings) -> None:
        tight = settings.model_copy(update={"max_upload_mb": 1})
        client = TestClient(create_app(tight, provider=StubProvider(b"\x89PNG" + b"x" * 64)))

        oversized = make_noisy_photo_bytes(size=(1600, 1600))
        form = generate_form(photo=("big.jpg", oversized, "image/jpeg"))
        response = client.post("/api/generate", **multipart(form))

        assert response.status_code == 400
        assert "larger than" in response.json()["detail"]


class TestProviderFailureMapping:
    def test_provider_error_becomes_502(self, settings: Settings) -> None:
        class Failing(StubProvider):
            def generate(self, request):
                raise ProviderError("upstream refused")

        client = TestClient(create_app(settings, provider=Failing(b"x")))
        response = client.post("/api/generate", **multipart(generate_form()))

        assert response.status_code == 502
        assert response.json()["error"] == "provider_error"

    def test_provider_unavailable_becomes_503(self, settings: Settings) -> None:
        class Down(StubProvider):
            def generate(self, request):
                raise ProviderUnavailable("GEMINI_API_KEY is not set")

        client = TestClient(create_app(settings, provider=Down(b"x")))
        response = client.post("/api/generate", **multipart(generate_form()))

        assert response.status_code == 503
        assert "mock" in response.json()["hint"]


class TestMediaRoutes:
    def test_unknown_image_returns_404(self, client: TestClient) -> None:
        assert client.get("/media/does-not-exist/scene").status_code == 404

    def test_unknown_stem_returns_404(self, client: TestClient) -> None:
        payload = client.post("/api/generate", **multipart(generate_form())).json()
        assert client.get(f"/media/{payload['generation_id']}/sidecar").status_code == 404

    def test_path_traversal_is_refused(self, client: TestClient) -> None:
        for attempt in ("../../etc/passwd", "..%2F..%2Fetc%2Fpasswd", "a/b"):
            assert client.get(f"/media/{attempt}/scene").status_code == 404

    def test_missing_meta_returns_404(self, client: TestClient) -> None:
        assert client.get("/media/nope/meta.json").status_code == 404


class TestHealth:
    def test_healthz_reports_the_active_provider(self, client: TestClient) -> None:
        payload = client.get("/healthz").json()
        assert payload["status"] == "ok"
        assert payload["provider"]["name"] == "recording"

    def test_healthz_never_leaks_the_key(self, client: TestClient, settings: Settings) -> None:
        body = client.get("/healthz").text
        assert "gemini_api_key" not in body
        assert "GEMINI_API_KEY" not in body

    def test_openapi_schema_is_valid_json(self, client: TestClient) -> None:
        assert json.loads(client.get("/openapi.json").text)["info"]["title"]
