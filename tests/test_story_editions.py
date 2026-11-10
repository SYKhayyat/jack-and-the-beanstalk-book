"""T7 and T6.5: a book is made from its story's data, pinned, and gated.

These are the end-to-end promises of the seed move:

* the stories are data — the app seeds them at boot, and a page prompt, the
  PDF and every screen read the book's own pinned edition;
* both stories can be made, each with its own cast, words, title and props;
* a book keeps the words and looks it started with, whatever is re-seeded or
  revised afterwards (T4.1, T7.5);
* a character who can never be photographed has no way to be given a face,
  in either story (T7.2), and a portrait made for one story is refused by the
  other;
* the operator's start gate holds a story back until its plates are approved,
  and the switch lives on the admin screen (T6.5).
"""

from __future__ import annotations

import dataclasses
import io
import shutil
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfReader
from sqlalchemy import insert, select

from app.books import SEEDS, StorySeed
from app.config import Settings
from app.db import BookRow, BookStoryPageRow
from app.main import create_app
from app.services.template_store import NOT_READY, TemplateStore
from app.services.user_store import User
from app.sessions import SESSION_COOKIE
from tests.helpers import JACK_KEY, RED_KEY, RecordingProvider, make_photo_bytes, multipart

RED_SEED: StorySeed = next(seed for seed in SEEDS if seed.key == RED_KEY)
OPERATOR = "operator-sub"


def _portrait(client: TestClient, story: str, character: str = "") -> str:
    form: dict[str, object] = {
        "story": story,
        "photo": ("p.png", make_photo_bytes(), "image/png"),
    }
    if character:
        form["character"] = character
    response = client.post("/api/cartoonify", **multipart(form))
    assert response.status_code == 201, response.text
    return str(response.json()["generation_id"])


def _start(client: TestClient, story: str, generation_id: str, **fields: object):
    return client.post(
        "/api/book/start",
        data={"story": story, "generation_id": generation_id, **fields},
    )


def _wait(client: TestClient, book_id: str) -> dict[str, object]:
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        status = client.get(f"/api/book/{book_id}").json()
        if status["status"] in {"complete", "failed"}:
            return dict(status)
        time.sleep(0.05)
    raise AssertionError("the book never settled")


def _prompts(client: TestClient, book_id: str) -> list[dict[str, str]]:
    return list(client.get(f"/book-media/{book_id}/prompts.json").json()["prompts"])


# --- both stories are makeable, each from its own data --------------------------------


class TestRedIsABook:
    def test_a_red_book_is_drawn_and_printed_from_reds_own_story(
        self, client: TestClient, provider: RecordingProvider
    ) -> None:
        star = _portrait(client, RED_KEY)
        grandmother = _portrait(client, RED_KEY, "grandmother")
        started = _start(
            client,
            RED_KEY,
            star,
            child_name="Noa",
            character=f"grandmother:{grandmother}",
        )
        assert started.status_code == 201, started.text
        book_id = started.json()["book_id"]
        status = _wait(client, book_id)
        assert status["status"] == "complete", status
        assert status["title"] == "Little Red Riding Hood"

        prompts = _prompts(client, book_id)
        assert len(prompts) == 21
        for entry in prompts:
            text = entry["prompt"]
            for jack_only in ("beanstalk", "Beanstalk", "Milky-White", "the giant"):
                assert jack_only not in text, (entry["key"], jack_only)
        page_one = next(entry["prompt"] for entry in prompts if entry["page"] == "1")
        assert 'the picture book "Little Red Riding Hood"' in page_one
        assert "bright red hooded cloak" in page_one  # the star's costume

        # The grandmother's face goes only to the pages that cast her (T7.3).
        cast_her = {page.number for page in RED_SEED.pages if "grandmother" in page.scene.cast}
        for entry in prompts:
            if entry["page"] == "cover":
                continue
            names_her = "Grandmother's cartoon portrait" in entry["prompt"]
            assert names_her == (int(entry["page"]) in cast_her), entry["page"]

        pdf = client.get(f"/book-media/{book_id}/pdf")
        text = " ".join(
            page.extract_text() or "" for page in PdfReader(io.BytesIO(pdf.content)).pages
        )
        # The tale keeps its name on the cover and names the star beneath it.
        assert "Little Red Riding Hood" in text
        assert "starring Noa" in text
        assert "starring Noa" in client.get(f"/books/{book_id}").text
        assert "Grandmother" in text

    def test_the_wolf_can_never_be_given_a_face(self, client: TestClient) -> None:
        """T7.2 for Red: no portrait, no start, no edit takes a wolf photo."""
        refused = client.post(
            "/api/cartoonify",
            **multipart(
                {
                    "story": RED_KEY,
                    "photo": ("w.png", make_photo_bytes(), "image/png"),
                    "character": "wolf",
                }
            ),
        )
        assert refused.status_code == 422
        star = _portrait(client, RED_KEY)
        assert _start(client, RED_KEY, star, character=f"wolf:{star}").status_code == 422
        characters = client.get(f"/api/stories/{RED_KEY}/characters").json()["characters"]
        assert [entry["key"] for entry in characters] == [
            "mother",
            "grandmother",
            "woodcutter",
        ]

    def test_a_portrait_made_for_one_story_is_refused_by_the_other(
        self, client: TestClient
    ) -> None:
        jack_star = _portrait(client, JACK_KEY)
        response = _start(client, RED_KEY, jack_star)
        assert response.status_code == 422
        assert "different book" in response.json()["detail"]

        red_star = _portrait(client, RED_KEY)
        jack_mother = _portrait(client, JACK_KEY, "mother")
        response = _start(client, RED_KEY, red_star, character=f"mother:{jack_mother}")
        assert response.status_code == 422

    def test_each_story_lists_its_own_pages(self, client: TestClient) -> None:
        red = client.get(f"/api/stories/{RED_KEY}/pages").json()
        jack = client.get(f"/api/stories/{JACK_KEY}/pages").json()
        assert len(red["pages"]) == len(jack["pages"]) == 20
        assert red["pages"][0]["key"] == "the-basket"
        assert jack["pages"][0]["key"] == "empty-cupboard"
        # Rendered with the story's own default name.
        assert "Red" in red["pages"][0]["story"]
        assert client.get("/api/stories/no_such_story/pages").status_code == 404


# --- a book keeps what it started on ---------------------------------------------------


class TestABookKeepsItsEdition:
    def test_a_reseeded_page_does_not_reach_a_book_already_made(
        self, client: TestClient
    ) -> None:
        container = client.app.state.container
        star = _portrait(client, RED_KEY)
        old_book = _start(client, RED_KEY, star).json()["book_id"]
        _wait(client, old_book)
        old_words = RED_SEED.pages[0].story

        pages = list(RED_SEED.pages)
        pages[0] = dataclasses.replace(pages[0], story=old_words + " The sun was up.")
        container.templates.seed([dataclasses.replace(RED_SEED, pages=tuple(pages))])
        container.templates.revise_character(RED_KEY, "wolf", look="A small, shy grey wolf.")

        # A fresh store: nothing cached, everything read back from the pins.
        fresh = TemplateStore(container.engine)
        kept = fresh.book_edition(old_book)
        assert kept.pages[0].story == old_words
        assert kept.cast["wolf"].look == RED_SEED.cast["wolf"].look

        new_book = _start(client, RED_KEY, _portrait(client, RED_KEY)).json()["book_id"]
        made_now = fresh.book_edition(new_book)
        assert made_now.pages[0].story == old_words + " The sun was up."
        assert made_now.cast["wolf"].look == "A small, shy grey wolf."

    def test_a_book_saved_before_books_pinned_their_story_is_pinned_at_boot(
        self, settings: Settings, provider: RecordingProvider
    ) -> None:
        app = create_app(settings, provider=provider)
        engine = app.state.container.engine
        with engine.begin() as conn:
            conn.execute(
                insert(BookRow).values(
                    id="legacy-book",
                    status="complete",
                    generation_id="gen-legacy",
                    about={"prompt_name": "Ada", "about_name": "Ada"},
                    title="Ada and the Beanstalk",
                    story_key=JACK_KEY,
                )
            )
        rebooted = create_app(settings, provider=provider)
        with rebooted.state.container.engine.connect() as conn:
            pinned = conn.execute(
                select(BookStoryPageRow.page_number).where(
                    BookStoryPageRow.book_id == "legacy-book"
                )
            ).all()
        assert len(pinned) == 21
        edition = rebooted.state.container.templates.book_edition("legacy-book")
        assert edition.key == JACK_KEY


# --- T6.5: the operator's start gate ---------------------------------------------------


@pytest.fixture
def operator_client(
    tmp_path: Path, seeded_template: Path, provider: RecordingProvider
) -> Iterator[TestClient]:
    storage = tmp_path / "storage"
    storage.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(seeded_template, storage / "app.sqlite3")
    settings = Settings(
        image_provider="mock",  # type: ignore[arg-type]
        app_env="test",
        storage_dir=storage,
        google_client_id="cid",
        google_client_secret="cs",
        session_secret="session-secret",
        operator_sub=OPERATOR,
        max_generations_per_minute=1000,
    )
    with TestClient(create_app(settings, provider=provider)) as client:
        yield client


def _sign_in(client: TestClient, sub: str) -> None:
    client.app.state.container.user_store.upsert(User(sub=sub, name=sub))
    token = client.app.state.sessions.issue_google(  # type: ignore[attr-defined]
        sub=sub, name=sub, ttl_seconds=3600
    )
    client.cookies.set(SESSION_COOKIE, token)


class TestTheStartGate:
    def test_the_operator_turns_the_gate_on_and_off_from_the_admin_screen(
        self, operator_client: TestClient
    ) -> None:
        _sign_in(operator_client, OPERATOR)
        index = operator_client.get("/admin/plates").text
        assert f'action="/admin/plates/{RED_KEY}/gate"' in index
        assert "New books of this story can start now." in index

        on = operator_client.post(
            f"/admin/plates/{RED_KEY}/gate", data={"required": "on"}, follow_redirects=False
        )
        assert on.status_code == 303
        templates = operator_client.app.state.container.templates
        assert templates.story_state(RED_KEY).requires_template is True
        assert templates.story_state(JACK_KEY).requires_template is False
        assert "New books are held back" in operator_client.get("/admin/plates").text

        operator_client.post(f"/admin/plates/{RED_KEY}/gate", data={}, follow_redirects=False)
        assert templates.story_state(RED_KEY).requires_template is False

    def test_only_the_operator_reaches_the_switch(self, operator_client: TestClient) -> None:
        _sign_in(operator_client, "somebody-else")
        response = operator_client.post(
            f"/admin/plates/{RED_KEY}/gate", data={"required": "on"}
        )
        assert response.status_code == 404
        templates = operator_client.app.state.container.templates
        assert templates.story_state(RED_KEY).requires_template is False

    def test_with_the_gate_on_nothing_billable_starts_for_that_story(
        self, client: TestClient, provider: RecordingProvider
    ) -> None:
        templates = client.app.state.container.templates
        star = _portrait(client, RED_KEY)
        calls = len(provider.requests)
        templates.set_requires_template(RED_KEY, True)

        start = _start(client, RED_KEY, star)
        assert start.status_code == 409
        assert start.json() == {"error": "story_not_ready", "detail": NOT_READY}
        portrait = client.post(
            "/api/cartoonify",
            **multipart(
                {"story": RED_KEY, "photo": ("p.png", make_photo_bytes(), "image/png")}
            ),
        )
        assert portrait.status_code == 409
        assert len(provider.requests) == calls, "a held-back story spent an image call"

        # Jack is not held back by Red's gate.
        assert _start(client, JACK_KEY, _portrait(client, JACK_KEY)).status_code == 201

    def test_a_book_already_started_is_untouched_by_the_gate(self, client: TestClient) -> None:
        templates = client.app.state.container.templates
        book_id = _start(client, RED_KEY, _portrait(client, RED_KEY)).json()["book_id"]
        _wait(client, book_id)
        templates.set_requires_template(RED_KEY, True)
        response = client.post(
            f"/api/book/{book_id}/edit", data={"action": "page", "page_number": "2"}
        )
        assert response.status_code == 202, response.text
        assert _wait(client, book_id)["status"] == "complete"
