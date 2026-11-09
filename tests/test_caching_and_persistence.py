"""T13: pictures are not downloaded twice. T14: a book outlives the process.

Both are about the same thing from two sides — the bytes already on disk. T13
stops the browser paying for them again; T14 proves they are still there after a
restart, still closed to anyone else, and that reading the PDF does not rebuild
it (which would mean paying for the pictures a second time).
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.services.image_providers.base import GeneratedImage
from tests.helpers import RecordingProvider, make_photo_bytes
from tests.test_book_library_and_edits import _google_client, _sign_in, _wait
from tests.test_ui_backend import _comic, _start


class AChangingPainter(RecordingProvider):
    """Paints a different picture every call, as a real model would.

    The plain recording provider returns one fixed image forever, which makes a
    redraw byte-identical to the original — correct, but it cannot show that a
    *different* picture gets a different tag.
    """

    name = "changing"

    def __init__(self) -> None:
        super().__init__()
        self._painted = 0

    def generate(self, request):  # type: ignore[no-untyped-def]
        self._painted += 1
        self.requests.append(request)
        colour = f"#{self._painted:02x}{(self._painted * 7) % 256:02x}40"
        return GeneratedImage(
            data=make_photo_bytes(colour=colour), media_type="image/png", model="changing"
        )


def _settings_for(tmp_path: Path) -> Settings:
    """The same settings the shared client uses, so both paths build one database."""
    return Settings(
        image_provider="mock",  # type: ignore[arg-type]
        app_env="test",
        storage_dir=tmp_path / "storage",
        google_client_id="cid",
        google_client_secret="cs",
        session_secret="session-secret",
    )


def _completed(client: TestClient, **fields: object) -> str:
    comic = _comic(client, **fields)
    book_id = _start(client, str(comic["generation_id"]))
    assert _wait(client, book_id)["status"] == "complete"
    return book_id


def _redraw(client: TestClient, book_id: str, page_number: int) -> None:
    response = client.post(
        f"/api/book/{book_id}/edit",
        data={"action": "page", "page_number": str(page_number)},
    )
    assert response.status_code == 202, response.text
    assert _wait(client, book_id)["status"] == "complete"


class TestRevalidatingAPicture:
    def test_a_page_carries_a_content_tag(self, tmp_path: Path) -> None:
        with _google_client(tmp_path) as client:
            _sign_in(client, "owner-1")
            book_id = _completed(client)
            url = client.get(f"/api/book/{book_id}").json()["pages"][1]["url"]

            first = client.get(url)
            assert first.status_code == 200
            body = first.content
            assert first.headers["etag"] == f'"{hashlib.sha256(body).hexdigest()}"'
            # Private to this reader, and revalidated rather than reused blind.
            assert first.headers["cache-control"] == "private, max-age=0, must-revalidate"
            assert first.headers["x-content-type-options"] == "nosniff"

    def test_a_picture_the_client_already_has_costs_no_bytes(self, tmp_path: Path) -> None:
        with _google_client(tmp_path) as client:
            _sign_in(client, "owner-1")
            book_id = _completed(client)
            url = client.get(f"/api/book/{book_id}").json()["pages"][1]["url"]

            tag = client.get(url).headers["etag"]
            again = client.get(url, headers={"If-None-Match": tag})
            assert again.status_code == 304
            assert again.content == b""
            assert again.headers["etag"] == tag

    def test_a_redrawn_page_is_not_served_from_the_old_copy(self, tmp_path: Path) -> None:
        """The tag is the hash, so new bytes mean a new tag and a fresh 200."""
        provider = AChangingPainter()
        app = create_app(_settings_for(tmp_path), provider=provider)
        with TestClient(app) as client:
            _sign_in(client, "owner-1")
            book_id = _completed(client)
            url = client.get(f"/api/book/{book_id}").json()["pages"][1]["url"]
            before = client.get(url).headers["etag"]

            _redraw(client, book_id, 1)

            after = client.get(url, headers={"If-None-Match": before})
            assert after.status_code == 200, "a redrawn page must never be a 304"
            assert after.headers["etag"] != before
            assert after.headers["etag"] == f'"{hashlib.sha256(after.content).hexdigest()}"'

    def test_a_wildcard_tag_is_honoured(self, tmp_path: Path) -> None:
        with _google_client(tmp_path) as client:
            _sign_in(client, "owner-1")
            book_id = _completed(client)
            url = client.get(f"/api/book/{book_id}").json()["pages"][2]["url"]
            assert client.get(url, headers={"If-None-Match": "*"}).status_code == 304

    def test_a_list_of_tags_is_understood(self, tmp_path: Path) -> None:
        """Browsers send every copy they hold: one match is enough."""
        with _google_client(tmp_path) as client:
            _sign_in(client, "owner-1")
            book_id = _completed(client)
            url = client.get(f"/api/book/{book_id}").json()["pages"][3]["url"]
            tag = client.get(url).headers["etag"]
            listed = f'"some-older-copy", W/{tag}, "another"'
            assert client.get(url, headers={"If-None-Match": listed}).status_code == 304

    def test_the_cover_and_the_portrait_are_tagged_too(self, tmp_path: Path) -> None:
        with _google_client(tmp_path) as client:
            _sign_in(client, "owner-1")
            book_id = _completed(client)
            for suffix in ("cover", "portrait"):
                url = f"/book-media/{book_id}/{suffix}"
                first = client.get(url)
                assert first.status_code == 200, suffix
                assert first.headers.get("etag"), suffix
                again = client.get(url, headers={"If-None-Match": first.headers["etag"]})
                assert again.status_code == 304, suffix

    def test_the_childs_photograph_is_never_cached_by_the_device(self, tmp_path: Path) -> None:
        """An illustration may be revalidated; the real photograph never stays."""
        with _google_client(tmp_path) as client:
            _sign_in(client, "owner-1")
            book_id = _completed(client)
            photo = client.get(f"/book-media/{book_id}/photo")
            assert photo.status_code == 200
            assert photo.headers["cache-control"] == "private, no-store"
            assert "etag" not in photo.headers

    def test_downloading_the_portrait_is_always_a_file(self, tmp_path: Path) -> None:
        """`?download=1` is a request for the file, never for a 304."""
        with _google_client(tmp_path) as client:
            _sign_in(client, "owner-1")
            book_id = _completed(client)
            url = f"/book-media/{book_id}/portrait"
            tag = client.get(url).headers["etag"]
            download = client.get(f"{url}?download=1", headers={"If-None-Match": tag})
            assert download.status_code == 200
            assert "attachment" in download.headers["content-disposition"]


class TestABookOutlivesTheProcess:
    def test_every_picture_and_the_pdf_survive_a_restart(self, tmp_path: Path) -> None:
        """The second process reads the same book from the database, not memory."""
        settings = Settings(
            image_provider="mock",  # type: ignore[arg-type]
            app_env="test",
            storage_dir=tmp_path / "storage",
            google_client_id="cid",
            google_client_secret="cs",
            session_secret="session-secret",
        )
        with TestClient(create_app(settings)) as first:
            _sign_in(first, "owner-1")
            book_id = _completed(first)
            urls = [page["url"] for page in first.get(f"/api/book/{book_id}").json()["pages"]]
            before = {url: first.get(url).content for url in urls}

        # A new process: new app, new container, same database file.
        with TestClient(create_app(settings)) as second:
            _sign_in(second, "owner-1")
            payload = second.get(f"/api/book/{book_id}").json()
            assert payload["status"] == "complete"
            assert second.get(f"/book-media/{book_id}/pdf").status_code == 200
            for url in urls:
                assert second.get(url).content == before[url]

    def test_another_person_cannot_reach_the_book(self, tmp_path: Path) -> None:
        """Restart or not, a book belongs to one account: everyone else gets 404."""
        with _google_client(tmp_path) as client:
            _sign_in(client, "owner-1")
            book_id = _completed(client)
            url = client.get(f"/api/book/{book_id}").json()["pages"][1]["url"]

            _sign_in(client, "someone-else")
            assert client.get(f"/api/book/{book_id}").status_code == 404
            assert client.get(url).status_code == 404
            assert client.get(f"/book-media/{book_id}/pdf").status_code == 404
            assert client.get(f"/book-media/{book_id}/cover").status_code == 404

    def test_reading_the_pdf_does_not_draw_anything_again(self, tmp_path: Path) -> None:
        """A download is a read. If it re-rendered, every download would cost money."""
        provider = RecordingProvider()
        settings = Settings(
            image_provider="mock",  # type: ignore[arg-type]
            app_env="test",
            storage_dir=tmp_path / "storage",
            google_client_id="cid",
            google_client_secret="cs",
            session_secret="session-secret",
        )
        app = create_app(settings, provider=provider)
        with TestClient(app) as client:
            _sign_in(client, "owner-1")
            book_id = _completed(client)
            calls = len(provider.requests)

            for _ in range(3):
                assert client.get(f"/book-media/{book_id}/pdf").status_code == 200
            assert len(provider.requests) == calls, "the PDF was built again on a download"

    def test_a_second_reader_of_the_same_book_sees_the_same_pages(
        self, tmp_path: Path
    ) -> None:
        """Two tabs, one set of bytes: the second must not trigger a redraw."""
        provider = RecordingProvider()
        settings = Settings(
            image_provider="mock",  # type: ignore[arg-type]
            app_env="test",
            storage_dir=tmp_path / "storage",
            google_client_id="cid",
            google_client_secret="cs",
            session_secret="session-secret",
        )
        app = create_app(settings, provider=provider)
        with TestClient(app) as client:
            _sign_in(client, "owner-1")
            book_id = _completed(client)
            calls = len(provider.requests)
            tags = [
                client.get(page["url"]).headers["etag"]
                for page in client.get(f"/api/book/{book_id}").json()["pages"]
            ]
            # Revalidating all 21 pictures and reading the PDF draws nothing.
            for index, tag in enumerate(tags):
                number = index  # pages[0] is the cover at this level
                if number == 0:
                    continue
                client.get(
                    f"/book-media/{book_id}/pages/{number}", headers={"If-None-Match": tag}
                )
            client.get(f"/book-media/{book_id}/pdf")
            assert len(provider.requests) == calls
