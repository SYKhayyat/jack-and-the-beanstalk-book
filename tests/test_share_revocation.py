"""Stopping a share link (A10): the owner can take back a link sent to the wrong person.

* "Stop sharing" retires every link already minted for the book, at once;
* a new link made afterwards works, and the old ones stay dead;
* a link minted before generations existed (no "g") still reads as generation 0;
* a deployment whose signing key dies with the process refuses to mint links,
  because each one would quietly stop working at the next restart.
"""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.services.share_links import _encode, issue, read
from tests.test_share_links import KEY, NOW, _mint, _start_book, _wait


def test_a_token_carries_its_generation_and_an_old_token_reads_as_zero() -> None:
    token = issue("book-abc", key=KEY, expires_at=NOW + 60, generation=3)
    found = read(token, key=KEY, now=NOW)
    assert found is not None and found.generation == 3

    legacy = issue("book-abc", key=KEY, expires_at=NOW + 60)
    found = read(legacy, key=KEY, now=NOW)
    assert found is not None and found.generation == 0


def test_stop_sharing_kills_every_link_and_a_new_one_works(client: TestClient) -> None:
    book_id = _start_book(client)
    assert _wait(client, book_id)["status"] == "complete"
    assert "Share a read-only link" in client.get(f"/books/{book_id}").text
    first = _mint(client, book_id)
    # Asking again while sharing is on shows the same link: one link a book.
    assert _mint(client, book_id) == first
    stranger = TestClient(client.app)
    assert stranger.get(f"/share/{first}").status_code == 200

    page = client.get(f"/books/{book_id}")
    assert "Stop sharing" in page.text

    stopped = client.post(f"/books/{book_id}/share/stop", follow_redirects=False)
    assert stopped.status_code == 303
    assert stopped.headers["location"] == f"/books/{book_id}?message=sharing_stopped"
    assert "stopped working" in client.get(stopped.headers["location"]).text

    for path in ("", "/cover", "/pages/1", "/pdf"):
        assert stranger.get(f"/share/{first}{path}").status_code == 404, path
    # R3: after a stop the page shows no link at all, only "Share again", and
    # a reload says the same, because the state is the server's.
    for _ in range(2):
        page = client.get(f"/books/{book_id}").text
        assert "/share/" not in page.replace(f"/books/{book_id}/share", "")
        assert 'id="share-link"' not in page and 'id="copy-share-link"' not in page
        assert 'id="sharing-off"' in page and "Share again" in page
        assert "Stop sharing" not in page

    fresh = _mint(client, book_id)
    assert fresh != first
    assert stranger.get(f"/share/{fresh}").status_code == 200
    assert stranger.get(f"/share/{first}").status_code == 404


def test_a_forged_generation_is_no_token() -> None:
    """The generation is inside the signature: editing it is a forgery."""
    token = issue("book-abc", key=KEY, expires_at=NOW + 60, generation=1)
    signature = token.partition(".")[2]
    bumped = _encode({"b": "book-abc", "e": NOW + 60, "g": 2})
    assert read(f"{bumped}.{signature}", key=KEY, now=NOW) is None


def test_a_deployment_with_a_throwaway_key_refuses_to_make_links(tmp_path: Path) -> None:
    settings = Settings(
        image_provider="mock",  # type: ignore[arg-type]
        gemini_api_key="",
        app_env="test",
        storage_dir=tmp_path / "storage",
        max_generations_per_minute=1000,
    )
    assert settings.session_key_is_ephemeral
    with TestClient(create_app(settings)) as client:
        book_id = _start_book(client)
        assert _wait(client, book_id)["status"] == "complete"
        refused = client.get(f"/books/{book_id}/share", follow_redirects=False)
        assert refused.status_code == 409
        assert "SESSION_SECRET" in refused.text
