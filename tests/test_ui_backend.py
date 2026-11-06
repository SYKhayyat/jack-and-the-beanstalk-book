"""The small backend pieces the new screens need.

* `POST /api/cartoonify/{id}/choose` — a chosen take reaches the book;
* `aspect_ratio` on `book/start` — the page shape is chosen on the About screen;
* `about_photo` — the About page frames the cartoon, the photo, or both;
* `GET /book-media/{id}/sources.zip` — the photos and comics, to keep;
* `/login` — the signed-out shelf, with the banner and the covers.
"""

from __future__ import annotations

import io
import zipfile
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfReader

from app.config import Settings
from app.main import create_app
from app.models import AboutMe, AboutPhotoChoice
from app.models.book import BOOK_PAGES
from app.services.image_providers.base import GeneratedImage, ImageRequest
from app.services.pdf import PageArt, build_book_pdf
from tests.helpers import RecordingProvider, make_photo_bytes, multipart
from tests.test_book_library_and_edits import _google_client, _sign_in, _wait


def _comic(client: TestClient, **fields: object) -> dict[str, object]:
    form: dict[str, object] = {"photo": ("me.png", make_photo_bytes(), "image/png"), **fields}
    response = client.post("/api/cartoonify", **multipart(form))
    assert response.status_code == 201, response.text
    return dict(response.json())


def _start(client: TestClient, generation_id: str, **fields: object) -> str:
    response = client.post(
        "/api/book/start", data={"generation_id": generation_id, "child_name": "Mia", **fields}
    )
    assert response.status_code == 201, response.text
    return str(response.json()["book_id"])


class MultiTakeProvider(RecordingProvider):
    """Answers a request for N takes with N different pictures, in one call."""

    def generate_candidates(self, request: ImageRequest, count: int) -> list[GeneratedImage]:
        self.requests.append(request)
        colours = ("#aa3344", "#33aa44", "#3344aa")
        return [
            GeneratedImage(
                data=make_photo_bytes(colour=colours[i]), media_type="image/png", model="m"
            )
            for i in range(count)
        ]


@pytest.fixture
def takes_client(client_factory) -> Iterator[TestClient]:
    with client_factory(MultiTakeProvider()) as test_client:
        yield test_client


class TestChooseATake:
    def test_the_chosen_take_becomes_a_draft_of_its_own(
        self, takes_client: TestClient
    ) -> None:
        client = takes_client
        comic = _comic(client, candidates="2")
        original = str(comic["generation_id"])
        container = client.app.state.container  # type: ignore[attr-defined]
        second_take = container.store.load_draft(original).candidates[1]

        response = client.post(f"/api/cartoonify/{original}/choose", data={"index": "1"})
        assert response.status_code == 201, response.text
        payload = response.json()
        chosen = payload["generation_id"]
        assert chosen != original
        assert payload["billed_image_count"] == 0, "choosing draws nothing"

        draft = container.store.load_draft(chosen)
        assert draft.candidates == (second_take,)
        assert draft.cast_key == ""
        # The original stays, so the visitor can change their mind.
        assert container.store.load_draft(original) is not None

    def test_choosing_keeps_the_character_slot(self, takes_client: TestClient) -> None:
        comic = _comic(takes_client, candidates="2", character="mother")
        response = takes_client.post(
            f"/api/cartoonify/{comic['generation_id']}/choose", data={"index": "1"}
        )
        assert response.status_code == 201
        assert response.json()["cast_key"] == "mother"

    def test_an_out_of_range_take_is_a_422(self, client: TestClient) -> None:
        comic = _comic(client)
        response = client.post(
            f"/api/cartoonify/{comic['generation_id']}/choose", data={"index": "4"}
        )
        assert response.status_code == 422

    def test_an_expired_draft_is_a_404(self, client: TestClient) -> None:
        response = client.post("/api/cartoonify/gone/choose", data={"index": "0"})
        assert response.status_code == 404

    def test_choosing_draws_nothing(self, client_factory) -> None:
        provider = MultiTakeProvider()
        with client_factory(provider) as client:
            comic = _comic(client, candidates="2")
            calls = len(provider.requests)
            client.post(
                f"/api/cartoonify/{comic['generation_id']}/choose", data={"index": "1"}
            )
            assert len(provider.requests) == calls


class TestPageShape:
    def test_the_about_screen_shape_overrides_the_portrait(self, client: TestClient) -> None:
        comic = _comic(client)  # the portrait is made at the default 4:3
        book_id = _start(client, str(comic["generation_id"]), aspect_ratio="1:1")
        record = client.app.state.container.book_store.load_record(book_id)  # type: ignore[attr-defined]
        assert str(record.settings.aspect_ratio) == "1:1"

    def test_a_blank_shape_keeps_the_portrait_shape(self, client: TestClient) -> None:
        comic = _comic(client, aspect_ratio="3:4")
        book_id = _start(client, str(comic["generation_id"]))
        record = client.app.state.container.book_store.load_record(book_id)  # type: ignore[attr-defined]
        assert str(record.settings.aspect_ratio) == "3:4"

    def test_an_unknown_shape_is_a_422(self, client: TestClient) -> None:
        comic = _comic(client)
        response = client.post(
            "/api/book/start",
            data={"generation_id": comic["generation_id"], "aspect_ratio": "5:7"},
        )
        assert response.status_code == 422

    def test_a_different_shape_is_a_different_book(self, client: TestClient) -> None:
        """The ten-minute reuse must not hand back a book of the wrong shape."""
        comic = _comic(client)
        first = _start(client, str(comic["generation_id"]), aspect_ratio="1:1")
        second = _start(client, str(comic["generation_id"]), aspect_ratio="16:9")
        assert first != second


class TestAboutPhoto:
    def test_records_written_before_the_choice_still_load(self) -> None:
        about = AboutMe.model_validate({"prompt_name": "Mia", "about_name": "Mia"})
        assert about.about_photo == AboutPhotoChoice.CARTOON

    def test_both_frames_two_pictures_on_the_last_page(self) -> None:
        pages = [PageArt(image=make_photo_bytes(), page=page) for page in BOOK_PAGES[:2]]
        portrait = make_photo_bytes(colour="#aa3344")
        photo = make_photo_bytes(colour="#33aa44")
        counts = {}
        for choice in AboutPhotoChoice:
            about = AboutMe(prompt_name="Mia", about_name="Mia", about_photo=choice)
            pdf = build_book_pdf(about, pages, None, portrait=portrait, photo=photo)
            counts[choice.value] = len(PdfReader(io.BytesIO(pdf)).pages[-1].images)
        assert counts == {"cartoon": 1, "photo": 1, "both": 2}

    def test_a_missing_photo_falls_back_to_the_cartoon(self) -> None:
        pages = [PageArt(image=make_photo_bytes(), page=BOOK_PAGES[0])]
        about = AboutMe(
            prompt_name="Mia", about_name="Mia", about_photo=AboutPhotoChoice.PHOTO
        )
        pdf = build_book_pdf(about, pages, None, portrait=make_photo_bytes(), photo=None)
        assert len(PdfReader(io.BytesIO(pdf)).pages[-1].images) == 1

    def test_the_finished_book_uses_the_choice(self, client: TestClient) -> None:
        comic = _comic(client)
        book_id = _start(client, str(comic["generation_id"]), about_photo="both")
        assert _wait(client, book_id)["status"] == "complete"
        pdf = client.get(f"/book-media/{book_id}/pdf").content
        assert len(PdfReader(io.BytesIO(pdf)).pages[-1].images) == 2

    def test_an_unknown_choice_is_a_422(self, client: TestClient) -> None:
        comic = _comic(client)
        response = client.post(
            "/api/book/start",
            data={"generation_id": comic["generation_id"], "about_photo": "selfie"},
        )
        assert response.status_code == 422

    def test_the_about_edit_can_change_the_choice(self, client: TestClient) -> None:
        comic = _comic(client)
        book_id = _start(client, str(comic["generation_id"]))
        _wait(client, book_id)
        response = client.post(
            f"/api/book/{book_id}/edit", data={"action": "about", "about_photo": "both"}
        )
        assert response.status_code == 202, response.text
        record = client.app.state.container.book_store.load_record(book_id)  # type: ignore[attr-defined]
        assert record.about.about_photo == AboutPhotoChoice.BOTH


class TestSourcesZip:
    def test_the_archive_holds_every_photo_and_picture(self, tmp_path: Path) -> None:
        with _google_client(tmp_path) as client:
            _sign_in(client, "owner-1")
            star = _comic(client)
            mother = _comic(client, character="mother")
            book_id = _start(
                client,
                str(star["generation_id"]),
                character=f"mother:{mother['generation_id']}",
            )
            assert _wait(client, book_id)["status"] == "complete"

            response = client.get(f"/book-media/{book_id}/sources.zip")
            assert response.status_code == 200
            assert response.headers["cache-control"] == "private, no-store"
            assert "attachment" in response.headers["content-disposition"]
            names = set(zipfile.ZipFile(io.BytesIO(response.content)).namelist())

            stems = {name.rsplit(".", 1)[0] for name in names}
            expected = {
                "README",
                "star/photo",
                "star/comic",
                "mother/photo",
                "mother/comic",
                "cover",
                "about",
                "prompts",
                *(f"pages/page-{page.number:02d}" for page in BOOK_PAGES),
            }
            assert expected <= stems, expected - stems
            assert not any(name.startswith("giant/") for name in names)

            _sign_in(client, "owner-2")
            assert client.get(f"/book-media/{book_id}/sources.zip").status_code == 404


def _locked(tmp_path: Path, **overrides: object) -> TestClient:
    settings = Settings(
        image_provider="mock",  # type: ignore[arg-type]
        gemini_api_key="",
        app_env="test",
        storage_dir=tmp_path / "storage",
        app_password="let-me-in",
        session_secret="test-session-secret",
        **overrides,  # type: ignore[arg-type]
    )
    return TestClient(create_app(settings, provider=RecordingProvider()))


class TestSignedOutShelf:
    def test_login_is_the_shelf_with_the_banner(self, tmp_path: Path) -> None:
        with _locked(tmp_path) as client:
            page = client.get("/login")
            assert page.status_code == 200
            assert 'class="banner"' in page.text
            assert 'id="login-form"' in page.text
            assert "/static/covers/jack_and_the_beanstalk.svg" in page.text
            assert "Little Red Riding Hood" in page.text
            # The covers are pictures only; the banner is the way in.
            assert "/stories/" not in page.text
            assert 'id="books-drawer"' not in page.text

    def test_the_shelf_itself_still_needs_a_session(self, tmp_path: Path) -> None:
        with _locked(tmp_path) as client:
            response = client.get("/", follow_redirects=False)
            assert response.status_code == 303
            assert response.headers["location"].startswith("/login")
            for path in (
                "/stories/jack_and_the_beanstalk/studio",
                "/stories/jack_and_the_beanstalk/about",
            ):
                assert client.get(path, follow_redirects=False).status_code == 303

    def test_the_fonts_and_covers_load_before_sign_in(self, tmp_path: Path) -> None:
        with _locked(tmp_path) as client:
            for path in (
                "/static/style.css",
                "/static/fonts/Literata-Bold.ttf",
                "/static/covers/little_red_riding_hood.svg",
            ):
                assert client.get(path).status_code == 200, path
