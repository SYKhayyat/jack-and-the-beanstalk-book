"""The library and the edit surface: cover, redraw, replace a face, About, delete."""

from __future__ import annotations

import io
import re
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfReader

from app.config import Settings
from app.main import create_app
from app.services.image_providers.base import (
    GeneratedImage,
    ImageProvider,
    ImageRequest,
    ProviderError,
)
from app.services.user_store import User
from app.sessions import SESSION_COOKIE
from tests.helpers import RecordingProvider, jack, make_photo_bytes, multipart, scene_png_bytes

#: Jack's tale exactly as a new book is made from it (its seed's edition).
JACK = jack()
BOOK_PAGES = JACK.pages


STUDIO = "/stories/jack_and_the_beanstalk/studio"
ABOUT = "/stories/jack_and_the_beanstalk/about"

PAGE_COUNT = len(BOOK_PAGES)


def _portrait(client: TestClient, character: str = "") -> dict[str, object]:
    form: dict[str, object] = {
        "story": "jack_and_the_beanstalk",
        "photo": ("me.png", make_photo_bytes(), "image/png"),
    }
    if character:
        form["character"] = character
    response = client.post("/api/cartoonify", **multipart(form))
    assert response.status_code == 201, response.text
    return dict(response.json())


def _start(client: TestClient, **fields: object) -> str:
    portrait = _portrait(client)
    data = {"generation_id": portrait["generation_id"], "child_name": "Shai"}
    data.update(fields)
    response = client.post("/api/book/start", data={"story": "jack_and_the_beanstalk", **data})
    assert response.status_code == 201, response.text
    return str(response.json()["book_id"])


def _wait(client: TestClient, book_id: str, seconds: float = 60.0) -> dict[str, object]:
    deadline = time.monotonic() + seconds
    last: dict[str, object] = {}
    while time.monotonic() < deadline:
        response = client.get(f"/api/book/{book_id}")
        assert response.status_code == 200, response.text
        last = dict(response.json())
        if last["status"] in {"complete", "failed"}:
            return last
        time.sleep(0.03)
    return last


def _pdf_text(data: bytes) -> str:
    return "\n".join((page.extract_text() or "") for page in PdfReader(io.BytesIO(data)).pages)


@pytest.fixture(scope="module")
def completed_library(tmp_path_factory) -> Iterator[tuple[TestClient, str]]:  # type: ignore[no-untyped-def]
    """One finished, owned book shared by the read-only library tests.

    Each of those tests used to draw a whole 21-picture book to read it back,
    which is six seconds apiece. None of them changes the book.
    """
    tmp_path = tmp_path_factory.mktemp("library")
    with _google_client(tmp_path) as client:
        _sign_in(client, "owner-1")
        book_id = _start(client)
        _wait(client, book_id)
        yield client, book_id


def _page_calls(provider: RecordingProvider) -> list[ImageRequest]:
    return [request for request in provider.requests if request.label == "book-page"]


class ChangingProvider(ImageProvider):
    """A provider whose every answer differs, so page bytes prove a redraw."""

    name = "changing"

    def __init__(self) -> None:
        self.calls = 0
        self.requests: list[ImageRequest] = []

    def generate(self, request: ImageRequest) -> GeneratedImage:
        self.calls += 1
        self.requests.append(request)
        return GeneratedImage(
            data=scene_png_bytes(seed=self.calls),
            media_type="image/png",
            model="changing-v1",
        )


class TestCover:
    def test_a_cover_is_made_served_and_first_in_the_pdf(
        self, client: TestClient, provider: RecordingProvider
    ) -> None:
        book_id = _start(client)
        final = _wait(client, book_id)
        assert final["status"] == "complete", final
        assert final["cover_url"] == f"/book-media/{book_id}/cover"
        assert final["title"] == "Shai and the Beanstalk"

        cover = client.get(f"/book-media/{book_id}/cover")
        assert cover.status_code == 200
        assert cover.content[:4] in {b"\x89PNG", b"\xff\xd8\xff\xe0", b"\xff\xd8\xff\xe1"}

        cover_calls = [r for r in provider.requests if r.label == "book-cover"]
        assert len(cover_calls) == 1
        assert "COVER ILLUSTRATION" in cover_calls[0].prompt
        assert "beanstalk" in cover_calls[0].prompt

        pdf = client.get(f"/book-media/{book_id}/pdf")
        assert pdf.status_code == 200
        reader = PdfReader(io.BytesIO(pdf.content))
        assert len(reader.pages) == PAGE_COUNT + 2  # cover + story + About
        first_page = reader.pages[0].extract_text() or ""
        assert "Shai and the Beanstalk" in first_page


class TestEditing:
    def test_one_page_can_be_redrawn(self, client_factory) -> None:
        provider = ChangingProvider()
        with client_factory(provider) as client:
            book_id = _start(client)
            _wait(client, book_id)
            before = len(_page_calls(provider))
            page_one_before = client.get(f"/book-media/{book_id}/pages/1").content

            response = client.post(
                f"/api/book/{book_id}/edit",
                data={"action": "page", "page_number": "2", "reason": "wrong_moment"},
            )
            assert response.status_code == 202, response.text
            final = _wait(client, book_id)
            assert final["status"] == "complete", final

            new_calls = _page_calls(provider)[before:]
            assert len(new_calls) == 1, "an edit redrew more than the one page"
            assert "Draw page 2 of" in new_calls[0].prompt
            # The ticked chip is the reviewer's own fault name, said the same
            # way a reviewer's finding is.
            assert "the picture did not show the moment this page is about" in (
                new_calls[0].prompt
            )
            # The untouched page kept its bytes; the edited one changed.
            assert client.get(f"/book-media/{book_id}/pages/1").content == page_one_before

    def test_a_redraw_reason_outside_the_page_vocabulary_is_refused(
        self, client_factory
    ) -> None:
        """Portrait chips ("vary the light") would fight a plate: not accepted here."""
        provider = ChangingProvider()
        with client_factory(provider) as client:
            book_id = _start(client)
            _wait(client, book_id)
            before = len(_page_calls(provider))
            response = client.post(
                f"/api/book/{book_id}/edit",
                data={
                    "action": "page",
                    "page_number": "2",
                    "reason": "background_distracting",
                },
            )
            assert response.status_code == 422, response.text
            assert len(_page_calls(provider)) == before

    def test_a_book_made_before_photos_were_stored_can_still_be_redrawn(
        self, client_factory, monkeypatch
    ) -> None:
        """Books from before the photograph was kept are still editable.

        Their portrait is the likeness the pages were drawn from, so it stands
        in as the second anchor instead of the book becoming uneditable.
        """
        provider = ChangingProvider()
        with client_factory(provider) as client:
            book_id = _start(client)
            _wait(client, book_id)
            store = client.app.state.container.book_store
            monkeypatch.setattr(store, "read_source_photo", lambda _book_id: None)

            before = len(_page_calls(provider))
            response = client.post(
                f"/api/book/{book_id}/edit",
                data={"action": "page", "page_number": "2", "reason": "wrong_moment"},
            )
            assert response.status_code == 202, response.text
            final = _wait(client, book_id)
            assert final["status"] == "complete", final
            assert len(_page_calls(provider)[before:]) == 1

    def test_replacing_a_character_redraws_only_the_pages_that_cast_them(
        self, client_factory
    ) -> None:
        provider = ChangingProvider()
        with client_factory(provider) as client:
            mother = _portrait(client, character="mother")
            child = _portrait(client)
            start = client.post(
                "/api/book/start",
                data={
                    "story": "jack_and_the_beanstalk",
                    "generation_id": child["generation_id"],
                    "child_name": "Shai",
                    "character": f"mother:{mother['generation_id']}",
                },
            )
            assert start.status_code == 201, start.text
            book_id = str(start.json()["book_id"])
            _wait(client, book_id)

            replacement = _portrait(client, character="mother")
            before = len(_page_calls(provider))
            response = client.post(
                f"/api/book/{book_id}/edit",
                data={
                    "action": "character",
                    "character_key": "mother",
                    "generation_id": replacement["generation_id"],
                },
            )
            assert response.status_code == 202, response.text
            final = _wait(client, book_id)
            assert final["status"] == "complete", final

            new_calls = _page_calls(provider)[before:]
            expected = {page.number for page in BOOK_PAGES if "mother" in page.cast}
            redrawn = {
                int(request.prompt.split("Draw page ")[1].split(" ", 1)[0])
                for request in new_calls
            }
            assert redrawn == expected
            # Every redrawn page received the new mother: four references.
            assert all(len(request.reference_images) == 4 for request in new_calls)

    def test_a_character_with_no_page_is_refused_before_its_face_is_stored(
        self, client_factory, monkeypatch
    ) -> None:
        """A rejected character edit must not leave a portrait behind.

        The catalogue is the source of "which pages cast whom". With no page
        casting the mother, the request is a 422 — and the face must not have
        been written first.
        """
        provider = ChangingProvider()
        with client_factory(provider) as client:
            book_id = _start(client)
            _wait(client, book_id)
            replacement = _portrait(client, character="mother")
            monkeypatch.setattr(
                "app.models.story.StoryEdition.pages_casting", lambda _self, _key: set()
            )

            response = client.post(
                f"/api/book/{book_id}/edit",
                data={
                    "action": "character",
                    "character_key": "mother",
                    "generation_id": replacement["generation_id"],
                },
            )
            assert response.status_code == 422, response.text
            assert "does not appear" in response.json()["detail"]
            store = client.app.state.container.book_store
            assert "mother" not in store.load_characters(book_id)

    def test_the_about_page_can_change_without_redrawing(self, client_factory) -> None:
        provider = ChangingProvider()
        with client_factory(provider) as client:
            book_id = _start(client)
            _wait(client, book_id)
            calls_before = len(provider.requests)

            response = client.post(
                f"/api/book/{book_id}/edit",
                data={
                    "action": "about",
                    "about_name": "Shai",
                    "about_age": "7",
                    "about_title": "a stargazer",
                    "details": "Loves the moon.",
                },
            )
            assert response.status_code == 202, response.text
            assert len(provider.requests) == calls_before, "the About edit drew a picture"

            pdf = client.get(f"/book-media/{book_id}/pdf")
            assert pdf.status_code == 200
            text = _pdf_text(pdf.content)
            assert "a stargazer" in text
            assert "Loves the moon." in text

    def test_delete_removes_the_book(self, client: TestClient) -> None:
        book_id = _start(client)
        _wait(client, book_id)
        assert client.delete(f"/api/book/{book_id}").status_code == 204
        assert client.get(f"/api/book/{book_id}").status_code == 404

    def test_deleting_a_book_stops_its_running_render(self, settings: Settings) -> None:
        """A deleted book must not keep drawing pages, or write itself back.

        The render used to survive the delete: it kept making billable page
        calls and then failed against a directory that no longer existed.
        """
        provider = BlockingPageProvider()
        # One slot, so exactly one page is in flight when the delete arrives.
        tight = settings.model_copy(update={"max_concurrent_generations": 1})
        with TestClient(create_app(tight, provider=provider)) as client:
            provider.block = True
            book_id = _start(client)
            assert provider.entered.wait(10), "the book never started drawing"
            calls_at_delete = len(provider.requests)

            assert client.delete(f"/api/book/{book_id}").status_code == 204
            provider.release.set()
            time.sleep(0.4)

            assert len(provider.requests) == calls_at_delete, (
                "the cancelled book kept making page calls"
            )
            assert client.get(f"/api/book/{book_id}").status_code == 404
            assert not (tight.storage_dir / "books" / book_id).exists()

    def test_a_refused_edit_does_not_touch_the_running_job(self, client_factory) -> None:
        """A 409 must not rewrite the poll document of the job it refused.

        The second request used to publish its own "rendering" status before
        discovering the book was busy, which turned the page that was being
        drawn right now back into `pending`.
        """
        provider = BlockingPageProvider()
        with client_factory(provider) as client:
            book_id = _start(client)
            assert _wait(client, book_id)["status"] == "complete"

            provider.block = True
            first = client.post(
                f"/api/book/{book_id}/edit",
                data={"action": "page", "page_number": "2"},
            )
            assert first.status_code == 202, first.text
            assert provider.entered.wait(10), "the first edit never reached the provider"
            assert _page_status(client, book_id, 2) == "making"

            second = client.post(
                f"/api/book/{book_id}/edit",
                data={"action": "page", "page_number": "3"},
            )
            assert second.status_code == 409, second.text
            assert _page_status(client, book_id, 2) == "making", (
                "the refused edit clobbered the running job's status"
            )

            provider.release.set()
            assert _wait(client, book_id)["status"] == "complete"


def _page_status(client: TestClient, book_id: str, number: int) -> str:
    payload = client.get(f"/api/book/{book_id}").json()
    entry = next(page for page in payload["pages"] if page["number"] == number)
    return str(entry["status"])


class BlockingPageProvider(ImageProvider):
    """Holds a page call open so a test can observe a book mid-edit."""

    name = "blocking"

    def __init__(self) -> None:
        self.block = False
        self.entered = threading.Event()
        self.release = threading.Event()
        self.requests: list[ImageRequest] = []

    def generate(self, request: ImageRequest) -> GeneratedImage:
        self.requests.append(request)
        if self.block and request.label == "book-page":
            self.entered.set()
            if not self.release.wait(15):
                raise ProviderError("The blocking test provider was never released.")
        return GeneratedImage(
            data=scene_png_bytes(), media_type="image/png", model="blocking-v1"
        )


def _google_settings(tmp_path: Path) -> Settings:
    return Settings(
        image_provider="mock",  # type: ignore[arg-type]
        gemini_api_key="",
        app_env="test",
        storage_dir=tmp_path / "storage",
        google_client_id="cid",
        google_client_secret="cs",
        session_secret="session-secret",
    )


def _google_client(tmp_path: Path) -> TestClient:
    return TestClient(create_app(_google_settings(tmp_path)))


def _sign_in(client: TestClient, sub: str) -> None:
    """Mint the same session the Google callback would, without the network.

    The callback also records the person, and books are filed under a real
    `users` row (the foreign key says so), so the stand-in does the same write.
    """
    client.app.state.container.user_store.upsert(User(sub=sub, name=sub))
    token = client.app.state.sessions.issue_google(  # type: ignore[attr-defined]
        sub=sub, name=sub, ttl_seconds=3600
    )
    client.cookies.set(SESSION_COOKIE, token)


class TestLibrary:
    def test_a_signed_in_person_sees_only_their_books(
        self, completed_library: tuple[TestClient, str]
    ) -> None:
        client, book_id = completed_library
        _sign_in(client, "owner-1")

        listing = client.get("/api/books").json()["books"]
        assert [entry["book_id"] for entry in listing] == [book_id]
        assert listing[0]["title"] == "Shai and the Beanstalk"

        detail = client.get(f"/books/{book_id}")
        assert detail.status_code == 200
        assert "Shai and the Beanstalk" in detail.text

        _sign_in(client, "owner-2")
        assert client.get("/api/books").json()["books"] == []
        assert client.get(f"/books/{book_id}").status_code == 404

    def test_every_screen_offers_my_books_for_a_signed_in_person(
        self, completed_library: tuple[TestClient, str]
    ) -> None:
        client, book_id = completed_library
        _sign_in(client, "owner-1")
        for path in ("/", STUDIO, ABOUT, f"/books/{book_id}"):
            page = client.get(path)
            assert 'id="books-drawer"' in page.text, path
            assert f'href="/books/{book_id}"' in page.text, path

    def test_every_book_in_the_drawer_has_its_own_delete_button(
        self, completed_library: tuple[TestClient, str]
    ) -> None:
        """UAT: putting a book away meant opening it first. The cross is in the row."""
        client, book_id = completed_library
        _sign_in(client, "owner-1")
        for path in ("/", f"/books/{book_id}"):
            page = client.get(path)
            assert 'class="drawer-row"' in page.text
            assert 'class="drawer-delete"' in page.text
            assert f'data-delete-book="{book_id}"' in page.text
        # The count the reader sees: twenty-one drawn pictures (cover + 20).
        assert "21 pictures" in page.text

    def test_the_downloads_are_private_to_the_book_owner(
        self, completed_library: tuple[TestClient, str]
    ) -> None:
        """Another signed-in person must not fetch someone's photo or cartoon."""
        client, book_id = completed_library
        _sign_in(client, "owner-1")
        assert client.get(f"/book-media/{book_id}/photo?download=1").status_code == 200

        _sign_in(client, "owner-2")
        for path in ("photo", "portrait"):
            response = client.get(f"/book-media/{book_id}/{path}?download=1")
            assert response.status_code == 404, path

    def test_the_book_can_be_read_in_the_page_without_the_pdf(
        self, completed_library: tuple[TestClient, str]
    ) -> None:
        """UAT: the PDF would not open, so the book itself is shown in the page."""
        client, book_id = completed_library
        _sign_in(client, "owner-1")
        page = client.get(f"/books/{book_id}")
        assert page.status_code == 200
        assert 'id="book-slot"' in page.text
        assert 'id="turn-back"' in page.text
        assert 'id="turn-next"' in page.text
        # The story line travels with the picture, not only in the PDF.
        assert "spread-story" in page.text
        # Cover + twenty story pages + the About page.
        assert len(re.findall(r'class="spread[ "]', page.text)) == 22

    def test_password_mode_has_no_library(self, client: TestClient) -> None:
        book_id = _start(client)
        _wait(client, book_id)
        assert client.get("/api/books").json() == {"books": []}
        assert 'id="books-drawer"' not in client.get("/").text


class FlakyPageProvider(ImageProvider):
    """Refuses one named page until `healed`, like a moderation block that a
    reworded prompt (or a second roll) gets past."""

    name = "flaky-page"

    def __init__(self, fail_page: int) -> None:
        self.fail_page = fail_page
        self.healed = False
        self.requests: list[ImageRequest] = []

    def generate(self, request: ImageRequest) -> GeneratedImage:
        self.requests.append(request)
        if not self.healed and f"Draw page {self.fail_page} of" in request.prompt:
            raise ProviderError("Your request was rejected by the safety system.")
        return GeneratedImage(data=scene_png_bytes(), media_type="image/png", model="flaky-v1")


class TestFinishAFailedBook:
    def test_finish_button_draws_only_the_missing_pages(self, client_factory) -> None:
        provider = FlakyPageProvider(fail_page=2)
        with client_factory(provider) as client:
            book_id = _start(client)
            failed = _wait(client, book_id)
            assert failed["status"] == "failed"
            finished = {
                int(page["number"])
                for page in failed["pages"]  # type: ignore[attr-defined]
                if page["status"] == "done"
            }
            assert 2 not in finished
            assert re.search(r'class="failed-row"', client.get(f"/books/{book_id}").text), (
                "a failed book offers Finish this book"
            )

            provider.healed = True
            before = len(_page_calls(provider))
            response = client.post(f"/api/book/{book_id}/edit", data={"action": "retry"})
            assert response.status_code == 202, response.text
            assert _wait(client, book_id)["status"] == "complete"

            redrawn = _page_calls(provider)[before:]
            assert redrawn, "the missing page was never drawn"
            assert any("Draw page 2 of" in r.prompt for r in redrawn)
            for number in finished:
                assert not any(f"Draw page {number} of" in r.prompt for r in redrawn)
            assert 'class="failed-row hidden"' in client.get(f"/books/{book_id}").text
