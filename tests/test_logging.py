"""Step 8: one thread through the operator's log.

A book renders in a background task, so "what happened to this visitor's book?"
cannot be answered by one request line. Three things are pinned here:

* Every request gets an id, on the response and on every line it causes.
* Every page logs one line with the book, the owner, the page, bytes, duration
  and outcome — success or failure.
* A book's start, completion and failure are each one line with the same shape.

These are operator-only lines. Nothing here is reachable by a visitor, so the
provider name is allowed — but no secret, no image bytes and no visitor free
text ever is.
"""

from __future__ import annotations

import logging
import re
import time

from fastapi.testclient import TestClient

from app.models.book import BOOK_PAGES
from app.services.image_providers.base import (
    GeneratedImage,
    ImageProvider,
    ImageRequest,
    ProviderError,
)
from tests.helpers import make_photo_bytes, multipart

_REQUEST_LINE = re.compile(r"request=([0-9a-f]{12})\b")


def _make_portrait(client: TestClient) -> dict[str, object]:
    response = client.post(
        "/api/cartoonify",
        **multipart({"photo": ("me.png", make_photo_bytes(), "image/png")}),
    )
    assert response.status_code == 201, response.text
    return dict(response.json())


def _start_book(client: TestClient, portrait: dict[str, object]) -> str:
    response = client.post(
        "/api/book/start",
        data={"generation_id": portrait["generation_id"], "child_name": "Shai"},
    )
    assert response.status_code == 201, response.text
    return str(response.json()["book_id"])


def _wait_for_book(client: TestClient, book_id: str) -> dict[str, object]:
    deadline = time.monotonic() + 10
    last: dict[str, object] = {}
    while time.monotonic() < deadline:
        response = client.get(f"/api/book/{book_id}")
        assert response.status_code == 200, response.text
        last = dict(response.json())
        if last["status"] in {"complete", "failed"}:
            return last
        time.sleep(0.05)
    return last


def _lines(caplog, logger: str, prefix: str) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == logger and record.getMessage().startswith(prefix)
    ]


class FailingPageProvider(ImageProvider):
    """Fails on one named page, like the book API's own failing provider."""

    name = "failing-for-log-test"

    def generate(self, request: ImageRequest) -> GeneratedImage:
        if "Draw page 2 of" in request.prompt:
            raise ProviderError("The provider gave up.")
        return GeneratedImage(data=make_photo_bytes(), media_type="image/png", model="demo-v1")

    def generate_candidates(
        self, request: ImageRequest, count: int = 1
    ) -> list[GeneratedImage]:
        return [self.generate(request)]

    def health_check(self) -> tuple[bool, str]:
        return True, "failing provider ready"


class TestRequestId:
    def test_every_response_carries_an_id_and_a_request_line(
        self, caplog, client: TestClient
    ) -> None:
        with caplog.at_level(logging.INFO, logger="app.main"):
            response = client.get("/")

        request_id = response.headers.get("X-Request-ID")
        assert request_id is not None
        assert re.fullmatch(r"[0-9a-f]{12}", request_id)
        line = next(line for line in _lines(caplog, "app.main", "request.done") if "/" in line)
        assert f"request={request_id}" in line
        assert "method=GET" in line
        assert "path=/" in line
        assert "status=200" in line
        assert "duration_ms=" in line

    def test_a_visitor_cannot_choose_the_id(self, caplog, client: TestClient) -> None:
        """An inbound header must not put a visitor's string in the operator log."""
        chosen = "chosen-by-the-visitor"
        with caplog.at_level(logging.INFO, logger="app.main"):
            response = client.get("/", headers={"X-Request-ID": chosen})

        assert response.headers["X-Request-ID"] != chosen
        assert chosen not in caplog.text


class TestBookLogTrail:
    def test_one_line_per_page_with_owner_bytes_duration_and_outcome(
        self, caplog, client: TestClient
    ) -> None:
        with caplog.at_level(logging.INFO):
            portrait = _make_portrait(client)
            book_id = _start_book(client, portrait)
            final = _wait_for_book(client, book_id)
        assert final["status"] == "complete", final

        pages = _lines(caplog, "app.services.book_runner", "book.page")
        assert len(pages) == len(BOOK_PAGES)
        for line in pages:
            assert "user=-" in line  # no sign-in in this deployment
            assert f"book={book_id}" in line
            assert re.search(r"page=\d+/\d+", line)
            assert re.search(r"bytes=\d+", line)
            assert re.search(r"ms=\d", line)
            assert "outcome=done" in line
            # The background task inherited the starting request's id.
            assert _REQUEST_LINE.search(line), line

    def test_completion_is_one_line_with_pages_and_outcome(
        self, caplog, client: TestClient
    ) -> None:
        with caplog.at_level(logging.INFO):
            portrait = _make_portrait(client)
            book_id = _start_book(client, portrait)
            _wait_for_book(client, book_id)

        done = _lines(caplog, "app.services.book_runner", "book.complete")
        assert len(done) == 1, done
        line = done[0]
        assert "user=-" in line
        assert f"book={book_id}" in line
        assert f"pages={len(BOOK_PAGES)}" in line
        assert re.search(r"pdf_bytes=\d+", line)
        assert re.search(r"ms=\d", line)
        assert "outcome=complete" in line

    def test_failure_logs_the_page_and_the_book_with_their_outcomes(
        self, caplog, client_factory
    ) -> None:
        with caplog.at_level(logging.INFO), client_factory(FailingPageProvider()) as client:
            portrait = _make_portrait(client)
            book_id = _start_book(client, portrait)
            final = _wait_for_book(client, book_id)
        assert final["status"] == "failed", final

        failed_pages = _lines(caplog, "app.services.book_runner", "book.page")
        failed = [line for line in failed_pages if "outcome=failed" in line]
        assert len(failed) == 1, failed_pages
        assert "page=2/" in failed[0]
        assert "error=ProviderError" in failed[0]
        assert re.search(r"ms=\d", failed[0])

        failure = _lines(caplog, "app.services.book_runner", "book.failure")
        assert len(failure) == 1, failure
        assert "user=-" in failure[0]
        assert f"book={book_id}" in failure[0]
        assert "outcome=failed" in failure[0]
        assert re.search(r"pages_done=\d+", failure[0])


class TestLogsCarryNoSecrets:
    def test_the_database_password_fragment_is_redacted_on_its_own(
        self, caplog, tmp_path
    ) -> None:
        """Step 8 changed the constraint: the URL was armed only whole.

        A URL can be logged in pieces (a repr, a per-field debug line), so the
        password portion is now armed separately. The whole URL stays armed.
        """
        from app.config import Settings
        from app.main import create_app

        url = "postgres://user:a-very-long-db-password@host/db"
        settings = Settings(
            image_provider="mock",  # type: ignore[arg-type]
            gemini_api_key="",
            app_env="test",
            storage_dir=tmp_path / "storage",
            database_url=url,
        )
        create_app(settings)

        with caplog.at_level(logging.WARNING, logger="app.main"):
            logging.getLogger("app.main").warning(
                "url=%s password=%s", url, "a-very-long-db-password"
            )

        assert "a-very-long-db-password" not in caplog.text
        assert url not in caplog.text
        assert caplog.text.count("***REDACTED***") >= 2

    def test_a_deployment_with_no_database_arms_no_extra_fragment(self) -> None:
        from app.db import database_secrets

        assert database_secrets("") == ()
        # A malformed URL still protects the whole pasted string.
        assert database_secrets("postgres://nope") == ("postgres://nope",)
