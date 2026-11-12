"""A child the studio remembers, so the next book starts without a photo (Idea 2).

The one behaviour that matters: the portrait the visitor already approved is
handed back as a draft, and a real book is made from it — no upload, no image
call, nothing charged. Then the boundaries: only the star, only its own story,
only its owner, and forgetting really deletes both pictures.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.services.user_store import User
from app.sessions import SESSION_COOKIE
from tests.helpers import JACK_KEY, RED_KEY, RecordingProvider, make_photo_bytes

OWNER = "owner-sub"
OTHER = "other-sub"


@contextmanager
def _site(
    tmp_path: Path, seeded_template: Path, **overrides: Any
) -> Iterator[tuple[TestClient, RecordingProvider]]:
    """The app with Google sign-in on, so a book (and a child) has an owner."""
    storage = tmp_path / "storage"
    storage.mkdir(parents=True, exist_ok=True)
    (storage / "app.sqlite3").write_bytes(seeded_template.read_bytes())
    settings = Settings(
        image_provider="mock",  # type: ignore[arg-type]
        app_env="test",
        storage_dir=storage,
        google_client_id="cid",
        google_client_secret="cs",
        session_secret="session-secret",
        max_generations_per_minute=1000,
        **overrides,
    )
    provider = RecordingProvider()
    with TestClient(create_app(settings, provider=provider)) as client:
        yield client, provider


def _sign_in(client: TestClient, sub: str) -> None:
    client.app.state.container.user_store.upsert(  # type: ignore[attr-defined]
        User(sub=sub, name=sub, email=f"{sub}@example.com")
    )
    token = client.app.state.sessions.issue_google(  # type: ignore[attr-defined]
        sub=sub, name=sub, email=f"{sub}@example.com", ttl_seconds=3600
    )
    client.cookies.set(SESSION_COOKIE, token)


def _portrait(client: TestClient, *, story: str = JACK_KEY, character: str = "") -> str:
    form: dict[str, Any] = {"story": story, "photo_gender": "girl"}
    if character:
        form["character"] = character
    response = client.post(
        "/api/cartoonify",
        files={"photo": ("me.png", make_photo_bytes(), "image/png")},
        data=form,
    )
    assert response.status_code == 201, response.text
    return str(response.json()["generation_id"])


def _remember(
    client: TestClient, generation_id: str, *, name: str = "Maya", gender: str = "girl"
) -> dict[str, Any]:
    response = client.post(
        "/api/child-profile",
        data={"generation_id": generation_id, "name": name, "photo_gender": gender},
    )
    assert response.status_code == 201, response.text
    return response.json()


def _wait_for_book(client: TestClient, book_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        payload = client.get(f"/api/book/{book_id}").json()
        if payload["status"] in {"complete", "failed"}:
            return payload
        time.sleep(0.05)
    raise AssertionError("the book never settled")


def test_a_remembered_child_starts_the_next_book_with_no_upload(
    tmp_path: Path, seeded_template: Path
) -> None:
    """The saved portrait is a draft again: a whole book, drawn from it."""
    with _site(tmp_path, seeded_template) as (client, provider):
        _sign_in(client, OWNER)
        saved = _remember(client, _portrait(client))

        assert saved["name"] == "Maya"
        assert saved["story_key"] == JACK_KEY
        assert saved["portrait_url"].startswith("/api/child-profile/portrait?v=")

        calls_before = len(provider.requests)
        reused = client.post("/api/child-profile/use", data={"story": JACK_KEY})
        assert reused.status_code == 201, reused.text
        # Handing the picture back draws nothing, so it cannot be billed.
        assert len(provider.requests) == calls_before

        started = client.post(
            "/api/book/start",
            data={"story": JACK_KEY, "generation_id": reused.json()["generation_id"]},
        )
        assert started.status_code == 201, started.text
        final = _wait_for_book(client, started.json()["book_id"])

        assert final["status"] == "complete", final
        # The pages were really drawn, from the portrait with no photo upload.
        assert len(provider.requests) > calls_before


def test_the_remembered_portrait_is_never_a_cast_members(
    tmp_path: Path, seeded_template: Path
) -> None:
    """Remembering the mother would put her face on every page of the next book."""
    with _site(tmp_path, seeded_template) as (client, _provider):
        _sign_in(client, OWNER)
        response = client.post(
            "/api/child-profile",
            data={"generation_id": _portrait(client, character="mother"), "name": "Maya"},
        )

        assert response.status_code == 422, response.text
        assert "only the star" in response.json()["detail"].lower()
        assert client.app.state.container.child_profiles.get(OWNER) is None  # type: ignore[attr-defined]


def test_forgetting_deletes_the_portrait_and_the_row(
    tmp_path: Path, seeded_template: Path
) -> None:
    """Forgetting is a delete, not a flag: the pictures leave with the row."""
    with _site(tmp_path, seeded_template) as (client, _provider):
        _sign_in(client, OWNER)
        _remember(client, _portrait(client))
        store = client.app.state.container.child_profiles  # type: ignore[attr-defined]

        portrait = client.get("/api/child-profile/portrait")
        assert portrait.status_code == 200
        assert portrait.headers["content-type"].startswith("image/")

        forgotten = client.delete("/api/child-profile")
        assert forgotten.json() == {"forgotten": True}
        assert store.get(OWNER) is None
        assert client.get("/api/child-profile/portrait").status_code == 404
        assert (
            client.post("/api/child-profile/use", data={"story": JACK_KEY}).status_code == 404
        )
        assert client.delete("/api/child-profile").json() == {"forgotten": False}


def test_another_account_cannot_see_or_use_the_remembered_child(
    tmp_path: Path, seeded_template: Path
) -> None:
    """A remembered child is the account's own, like every other picture."""
    with _site(tmp_path, seeded_template) as (client, _provider):
        _sign_in(client, OWNER)
        _remember(client, _portrait(client))

        _sign_in(client, OTHER)
        assert client.get("/api/child-profile/portrait").status_code == 404
        assert (
            client.post("/api/child-profile/use", data={"story": JACK_KEY}).status_code == 404
        )
        assert client.delete("/api/child-profile").json() == {"forgotten": False}

        # The owner's record is untouched by the other account's attempts.
        assert client.app.state.container.child_profiles.get(OWNER) is not None  # type: ignore[attr-defined]


def test_the_studio_offers_the_child_by_name_everywhere_and_by_portrait_once(
    tmp_path: Path, seeded_template: Path
) -> None:
    """The name and boy/girl answer travel; a portrait wears its story's costume."""
    with _site(tmp_path, seeded_template) as (client, _provider):
        _sign_in(client, OWNER)
        _remember(client, _portrait(client))

        jack = client.get(f"/stories/{JACK_KEY}/studio")
        assert jack.status_code == 200
        assert 'id="saved-child"' in jack.text
        assert '"name": "Maya"' in jack.text or '"name":"Maya"' in jack.text
        assert "Use this portrait" in jack.text

        red = client.get(f"/stories/{RED_KEY}/studio")
        assert red.status_code == 200
        # Same child, same answers — but no portrait card, because this story
        # would need its own costume drawn.
        assert 'id="saved-child"' not in red.text
        assert "Maya" in red.text


def test_a_portrait_drawn_for_another_story_is_refused(
    tmp_path: Path, seeded_template: Path
) -> None:
    with _site(tmp_path, seeded_template) as (client, _provider):
        _sign_in(client, OWNER)
        _remember(client, _portrait(client))

        response = client.post("/api/child-profile/use", data={"story": RED_KEY})

        assert response.status_code == 422, response.text
        assert "different book" in response.json()["detail"]


def test_the_saved_portrait_is_never_cached(tmp_path: Path, seeded_template: Path) -> None:
    """It is a picture of a child: private, and gone with the browser tab."""
    with _site(tmp_path, seeded_template) as (client, _provider):
        _sign_in(client, OWNER)
        _remember(client, _portrait(client))

        response = client.get("/api/child-profile/portrait")

        assert response.status_code == 200
        assert response.headers["cache-control"] == "private, no-store"
