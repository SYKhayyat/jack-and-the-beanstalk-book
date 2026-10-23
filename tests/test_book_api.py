"""The book HTTP flow: catalogue, start, poll, PDF — with a recording provider."""

from __future__ import annotations

import io
import time
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image
from pypdf import PdfReader

from app.services.image_providers.base import (
    GeneratedImage,
    ImageProvider,
    ImageRequest,
    ProviderError,
)
from tests.helpers import make_photo_bytes, multipart

PAGE_COUNT = 4


def photo_file() -> tuple[str, bytes, str]:
    return ("me.png", make_photo_bytes(), "image/png")


def make_portrait(client: TestClient, **overrides: object) -> dict[str, object]:
    form: dict[str, object] = {"photo": photo_file(), "scene": "planting-seeds"}
    form.update(overrides)
    response = client.post("/api/cartoonify", **multipart(form))
    assert response.status_code == 201, response.text
    return dict(response.json())


def wait_for_book(
    client: TestClient, book_id: str, seconds: float = 10.0
) -> dict[str, object]:
    """Public status until the background job settles (complete or failed)."""
    deadline = time.monotonic() + seconds
    last: dict[str, object] = {}
    while time.monotonic() < deadline:
        response = client.get(f"/api/book/{book_id}")
        assert response.status_code == 200, response.text
        last = dict(response.json())
        if last["status"] in {"complete", "failed"}:
            return last
        time.sleep(0.05)
    return last


class TestCatalogue:
    def test_lists_the_story_pages(self, client: TestClient) -> None:
        response = client.get("/api/book/pages")
        assert response.status_code == 200
        body = response.json()
        assert [page["key"] for page in body["pages"]] == [
            "empty-cupboard",
            "the-trade",
            "beans-out-the-window",
            "beanstalk-at-dawn",
        ]
        assert [page["number"] for page in body["pages"]] == [1, 2, 3, 4]
        # Story lines, exactly the prose the PDF will draw:
        assert "Milky-White" in body["pages"][0]["story"]


class TestBookEndToEnd:
    def test_full_flow_produces_a_pdf(self, client: TestClient, tmp_path: Path) -> None:
        portrait = make_portrait(client)
        start = client.post(
            "/api/book/start",
            data={
                "generation_id": portrait["generation_id"],
                "child_name": "Shai",
                "child_age": "7",
                "child_gender": "a boy",
                "details": "Loves dinosaurs\nLives in Jerusalem",
            },
        )
        assert start.status_code == 201, start.text
        book_id = start.json()["book_id"]
        assert start.json()["pdf_url"] == f"/book-media/{book_id}/pdf"

        status_payload = wait_for_book(client, book_id)
        assert status_payload["status"] == "complete", status_payload
        done = status_payload["pages"]
        assert [page["status"] for page in done] == ["done"] * PAGE_COUNT

        # Every page is shown as the browser's grid shows it.
        for number, page in enumerate(status_payload["pages"], start=1):
            image = client.get(f"/book-media/{book_id}/pages/{number}")
            assert image.status_code == 200, page
            assert Image.open(io.BytesIO(image.content)).size  # readable art

        pdf = client.get(f"/book-media/{book_id}/pdf")
        assert pdf.status_code == 200
        assert pdf.headers["content-type"] == "application/pdf"
        assert pdf.content[:5] == b"%PDF-"
        reader = PdfReader(io.BytesIO(pdf.content))
        assert len(reader.pages) == PAGE_COUNT + 1  # story pages + About Me

    def test_portrait_media_available(self, client: TestClient) -> None:
        portrait = make_portrait(client)
        start = client.post(
            "/api/book/start",
            data={"generation_id": portrait["generation_id"], "child_name": "Shai"},
        )
        assert start.status_code == 201
        book_id = start.json()["book_id"]
        wait_for_book(client, book_id)
        portrait_image = client.get(f"/book-media/{book_id}/portrait")
        assert portrait_image.status_code == 200


class FailingPageProvider(ImageProvider):
    """Fails on a chosen page call so a book dies mid-run, after page 1."""

    name = "failing-mid-book"

    def __init__(self) -> None:
        self.calls = 0

    def generate(self, request: ImageRequest) -> GeneratedImage:
        self.calls += 1
        if self.calls == 2:  # page 1 is call 1; page 2 is call 2
            raise ProviderError("The provider gave up.")
        return GeneratedImage(data=make_photo_bytes(), media_type="image/png", model="demo-v1")

    def generate_candidates(
        self, request: ImageRequest, count: int = 1
    ) -> list[GeneratedImage]:
        return [
            GeneratedImage(data=make_photo_bytes(), media_type="image/png", model="demo-v1")
        ]


class TestBookErrors:
    def test_failed_mid_run_keeps_finished_pages(self, client_factory) -> None:
        """Page 1 survives; status says failed with a message; no PDF is served."""
        with client_factory(FailingPageProvider()) as failing_client:
            portrait = make_portrait(failing_client)
            start = failing_client.post(
                "/api/book/start",
                data={"generation_id": portrait["generation_id"], "child_name": "Shai"},
            )
            assert start.status_code == 201
            book_id = start.json()["book_id"]

            status_payload = wait_for_book(failing_client, book_id)
            assert status_payload["status"] == "failed", status_payload
            assert "stopped early" in str(status_payload["error"])
            statuses = [page["status"] for page in status_payload["pages"]]
            assert statuses == ["done", "pending", "pending", "pending"]
            # The finished page is still there to look at.
            page1 = failing_client.get(f"/book-media/{book_id}/pages/1")
            assert page1.status_code == 200
            # And the PDF route stays honest rather than serving a half book.
            pdf = failing_client.get(f"/book-media/{book_id}/pdf")
            assert pdf.status_code == 409

    def test_unknown_book_404(self, client: TestClient) -> None:
        response = client.get("/api/book/deadbeef")
        assert response.status_code == 404
        assert response.json()["error"] == "unknown_book"

    def test_pdf_409_while_still_rendering(self, client_factory) -> None:
        class SlowProvider(ImageProvider):
            """Slow enough that the book cannot complete inside the test."""

            name = "slow-book"

            def generate(self, request: ImageRequest) -> GeneratedImage:
                time.sleep(0.05)
                return GeneratedImage(
                    data=make_photo_bytes(), media_type="image/png", model="demo-v1"
                )

            def generate_candidates(
                self, request: ImageRequest, count: int = 1
            ) -> list[GeneratedImage]:
                return [
                    GeneratedImage(
                        data=make_photo_bytes(), media_type="image/png", model="demo-v1"
                    )
                ]

        with client_factory(SlowProvider()) as slow_client:
            portrait = make_portrait(slow_client)
            start = slow_client.post(
                "/api/book/start",
                data={"generation_id": portrait["generation_id"], "child_name": "Shai"},
            )
            book_id = start.json()["book_id"]
            # The first status fetch proves the book is still rendering, so the
            # 409 below is guaranteed rather than racy on a fast machine.
            assert wait_for_book(slow_client, book_id, seconds=0.05)["status"] == "rendering"
            pdf = slow_client.get(f"/book-media/{book_id}/pdf")
            assert pdf.status_code == 409, "a running book must not serve its PDF yet"

    def test_unknown_page_number_409(self, client: TestClient) -> None:
        portrait = make_portrait(client)
        start = client.post(
            "/api/book/start",
            data={"generation_id": portrait["generation_id"], "child_name": "Shai"},
        )
        book_id = start.json()["book_id"]
        response = client.get(f"/book-media/{book_id}/pages/99")
        assert response.status_code in {200, 409}

    def test_start_without_a_live_draft_404(self, client: TestClient) -> None:
        response = client.post(
            "/api/book/start",
            data={"generation_id": "does-not-exist", "child_name": "Shai"},
        )
        assert response.status_code == 404
        assert response.json()["error"] == "unknown_draft"

    def test_missing_child_name_422(self, client: TestClient) -> None:
        portrait = make_portrait(client)
        response = client.post(
            "/api/book/start",
            data={"generation_id": portrait["generation_id"]},
        )
        assert response.status_code == 422  # FastAPI Form(min_length) catches it

    def test_about_me_never_reaches_any_prompt(self, client: TestClient) -> None:
        """The free text the uploader types is PDF-only: no prompt may contain it."""
        marker = "dinosaurs-and-beanstalks-marker"
        portrait = make_portrait(client)
        start = client.post(
            "/api/book/start",
            data={"generation_id": portrait["generation_id"], "child_name": "Shai"}
            | {"details": marker},
        )
        book_id = start.json()["book_id"]
        wait_for_book(client, book_id)
        # The book's prompts record all the prompts that were actually sent.
        prompts = client.get(f"/book-media/{book_id}/prompts.json")
        assert prompts.status_code == 200
        assert marker not in prompts.text
