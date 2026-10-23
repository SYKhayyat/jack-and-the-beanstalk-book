"""The two-step flow: review the cartoon, then pay for the scene.

The point of splitting the pipeline is cost. Iterating on a likeness you do not
like should not cost a scene call each time, and moving an approved portrait
into the second scene should cost one call, not two. These tests pin both.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.services.pipeline import UnknownGeneration
from app.services.storage import GenerationStore
from tests.conftest import generate_form
from tests.helpers import RecordingProvider, make_photo_bytes, multipart, tiny_png_bytes


def cartoonify_form(**overrides: object) -> dict[str, object]:
    form = generate_form()
    form.pop("scene", None)
    form.update(overrides)
    return form


class TestCartoonifyEndpoint:
    def test_returns_the_portrait_and_nothing_else(self, client: TestClient) -> None:
        response = client.post("/api/cartoonify", **multipart(cartoonify_form()))
        assert response.status_code == 201

        payload = response.json()
        assert payload["stage"] == "cartoonified"
        assert payload["generation_id"]
        assert payload["images"]["cartoon_url"]
        # The scene has not happened yet, so nothing may claim otherwise.
        assert "scene_url" not in payload["images"]
        assert "caption" not in payload

    def test_the_portrait_is_served(self, client: TestClient) -> None:
        payload = client.post("/api/cartoonify", **multipart(cartoonify_form())).json()
        image = client.get(payload["images"]["cartoon_url"])
        assert image.status_code == 200
        assert image.headers["content-type"].startswith("image/")

    def test_costs_exactly_one_call(
        self, client: TestClient, provider: RecordingProvider
    ) -> None:
        client.post("/api/cartoonify", **multipart(cartoonify_form()))
        assert [r.label for r in provider.requests] == ["cartoonify"]

    def test_echoes_the_settings_and_the_prompt(self, client: TestClient) -> None:
        payload = client.post(
            "/api/cartoonify", **multipart(cartoonify_form(cartoon_style="manga"))
        ).json()

        assert payload["settings"]["cartoon_style"] == "manga"
        assert "Preserve the identity" in payload["prompts"]["cartoon"]

    def test_still_validates_the_settings(self, client: TestClient) -> None:
        response = client.post(
            "/api/cartoonify", **multipart(cartoonify_form(cartoon_intensity=99))
        )
        assert response.status_code == 422

    def test_still_rejects_a_non_image(self, client: TestClient) -> None:
        form = cartoonify_form(photo=("x.png", b"not an image at all", "image/png"))
        assert client.post("/api/cartoonify", **multipart(form)).status_code == 400


class TestComposeEndpoint:
    def _draft_id(self, client: TestClient) -> str:
        return client.post("/api/cartoonify", **multipart(cartoonify_form())).json()[
            "generation_id"
        ]

    def test_composes_the_approved_portrait(self, client: TestClient) -> None:
        draft_id = self._draft_id(client)
        response = client.post(
            "/api/compose", data={"generation_id": draft_id, "scene": "planting-seeds"}
        )
        assert response.status_code == 201

        payload = response.json()
        assert payload["caption"].startswith("Jack is planting the seeds.")
        assert payload["images"]["scene_url"]
        assert payload["images"]["cartoon_url"]
        assert payload["settings"]["cartoon_style"]

    def test_compose_costs_one_call_on_top_of_the_draft(
        self, client: TestClient, provider: RecordingProvider
    ) -> None:
        draft_id = self._draft_id(client)
        client.post(
            "/api/compose", data={"generation_id": draft_id, "scene": "planting-seeds"}
        )
        assert [r.label for r in provider.requests] == ["cartoonify", "scene-compose"]

    def test_scene_pass_receives_both_identity_anchors(
        self, client: TestClient, provider: RecordingProvider
    ) -> None:
        draft_id = self._draft_id(client)
        client.post(
            "/api/compose", data={"generation_id": draft_id, "scene": "climbing-beanstalk"}
        )

        compose_request = provider.requests[-1]
        assert len(compose_request.reference_images) == 2

    def test_both_scenes_from_one_portrait(
        self, client: TestClient, provider: RecordingProvider
    ) -> None:
        draft_id = self._draft_id(client)
        captions = []
        for scene in ("planting-seeds", "climbing-beanstalk"):
            response = client.post(
                "/api/compose", data={"generation_id": draft_id, "scene": scene}
            )
            assert response.status_code == 201
            captions.append(response.json()["caption"])

        assert captions[0] != captions[1]
        # One cartoonify plus two scene passes: three calls, not four.
        assert len(provider.requests) == 3

    def test_unknown_draft_is_404(self, client: TestClient) -> None:
        response = client.post(
            "/api/compose", data={"generation_id": "nope", "scene": "planting-seeds"}
        )
        assert response.status_code == 404
        assert response.json()["error"] == "unknown_draft"

    def test_unknown_scene_is_400(self, client: TestClient) -> None:
        draft_id = self._draft_id(client)
        response = client.post(
            "/api/compose", data={"generation_id": draft_id, "scene": "riding-a-giant"}
        )
        assert response.status_code == 400

    def test_draft_survives_a_round_trip_through_disk(
        self, client: TestClient, settings: Settings
    ) -> None:
        draft_id = self._draft_id(client)

        store = GenerationStore(settings.storage_dir)
        draft = store.load_draft(draft_id)

        assert draft is not None
        assert draft.cartoon_image
        assert draft.photo_data  # the photo must persist, for the second reference


class TestCostSaving:
    def test_iterating_on_the_cartoon_costs_one_call_each_time(
        self, client: TestClient, provider: RecordingProvider
    ) -> None:
        # Three attempts to get the likeness right, then one scene.
        for intensity in (4, 7, 10):
            client.post(
                "/api/cartoonify", **multipart(cartoonify_form(cartoon_intensity=intensity))
            )

        draft_id = provider.requests[-1]
        assert len(provider.requests) == 3

        client.post(
            "/api/compose", data={"generation_id": "unused", "scene": "planting-seeds"}
        )
        # The compose failed on the id, so still three calls. That is the point:
        # a bad draft id is caught by a 404 before anything is billed.
        assert len(provider.requests) == 3
        assert draft_id.label == "cartoonify"


class TestOneShotEndpointStillWorks:
    def test_generate_still_does_both_passes(
        self, client: TestClient, provider: RecordingProvider
    ) -> None:
        response = client.post("/api/generate", **multipart(generate_form()))
        assert response.status_code == 201

        payload = response.json()
        assert payload["caption"]
        assert payload["images"]["scene_url"]
        assert [r.label for r in provider.requests] == ["cartoonify", "scene-compose"]

    def test_one_shot_draft_can_be_used_for_the_other_scene(
        self, client: TestClient, provider: RecordingProvider
    ) -> None:
        payload = client.post("/api/generate", **multipart(generate_form())).json()
        response = client.post(
            "/api/compose",
            data={
                "generation_id": payload["generation_id"],
                "scene": "climbing-beanstalk",
            },
        )

        assert response.status_code == 201
        assert response.json()["scene_key"] == "climbing-beanstalk"
        assert [request.label for request in provider.requests] == [
            "cartoonify",
            "scene-compose",
            "scene-compose",
        ]

    def test_generate_rejects_an_unknown_scene(self, client: TestClient) -> None:
        response = client.post("/api/generate", **multipart(generate_form(scene="nope")))
        assert response.status_code == 400


class TestDraftStore:
    def test_load_draft_returns_none_for_traversal(self, tmp_path) -> None:
        store = GenerationStore(tmp_path)
        assert store.load_draft("../../etc/passwd") is None

    def test_load_draft_returns_none_when_photo_is_missing(self, tmp_path) -> None:
        store = GenerationStore(tmp_path)
        client = TestClient(create_app(Settings(_env_file=None, storage_dir=tmp_path)))
        draft_id = client.post("/api/cartoonify", **multipart(cartoonify_form())).json()[
            "generation_id"
        ]

        (tmp_path / draft_id / "photo.jpg").unlink()
        assert store.load_draft(draft_id) is None

    def test_pipeline_raises_unknown_generation(self, settings: Settings) -> None:
        from app.models import get_scene
        from app.services.pipeline import GenerationPipeline

        pipeline = GenerationPipeline(
            RecordingProvider(), GenerationStore(settings.storage_dir)
        )
        with pytest.raises(UnknownGeneration, match="expired"):
            pipeline.compose_draft(
                generation_id="nothing-here",
                scene=get_scene("planting-seeds"),  # type: ignore[arg-type]
            )


class TestDifferentPhotosStaySeparate:
    def test_two_uploads_get_two_independent_drafts(self, client: TestClient) -> None:
        first = client.post(
            "/api/cartoonify",
            **multipart(
                cartoonify_form(
                    photo=("a.png", make_photo_bytes(colour="#aa0000"), "image/png")
                )
            ),
        ).json()
        second = client.post(
            "/api/cartoonify",
            **multipart(
                cartoonify_form(
                    photo=("b.png", make_photo_bytes(colour="#0000aa"), "image/png")
                )
            ),
        ).json()

        assert first["generation_id"] != second["generation_id"]

        # Composing the first must still show the first person's portrait.
        image = client.get(first["images"]["cartoon_url"])
        assert image.status_code == 200


class TestCandidatePersistence:
    def test_all_candidates_round_trip_and_are_served(self, settings: Settings) -> None:
        from app.models import CartoonSettings
        from app.services.image_providers.base import GeneratedImage
        from app.services.pipeline import GenerationPipeline

        class MultiCandidateProvider(RecordingProvider):
            def generate_candidates(self, request, count):
                self.requests.append(request)
                return [
                    GeneratedImage(
                        data=tiny_png_bytes(colour=colour),
                        media_type="image/png",
                        model="multi-v1",
                    )
                    for colour in ("#aa0000", "#00aa00", "#0000aa")[:count]
                ]

        store = GenerationStore(settings.storage_dir)
        draft = GenerationPipeline(store=store, provider=MultiCandidateProvider()).cartoonify(
            photo_data=tiny_png_bytes(),
            photo_media_type="image/png",
            settings=CartoonSettings(candidates=3),
        )
        loaded = store.load_draft(draft.generation_id)

        assert loaded is not None
        assert len(loaded.candidates) == 3
        assert all(store.read_image(draft.generation_id, f"candidate-{i}") for i in (1, 2))
