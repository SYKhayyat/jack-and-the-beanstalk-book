"""Step 6: the character bible — photoable cast, per-page references, cow pinned.

The judge's ruling: the cast is book-defined and a user may attach a photo to
any of the people in it. Three things are load-bearing and test-pinned here:

* **Only the photoable cast exists as an upload path.** Milky-White is a cow:
  she is cast in prose and can never be given a face — not in the catalogue,
  not in the form, not in the start endpoint.
* **A page receives only its own cast's references.** The per-call reference
  set is the child's double anchor plus this page's photographed cast, never
  the whole book.
* **The cast sheet is book-level.** The text half of the character bible is
  byte-identical on every page, so a look cannot drift page by page.
"""

from __future__ import annotations

import time

from fastapi.testclient import TestClient

from app.prompt_data import prompt_text
from tests.helpers import RecordingProvider, jack, make_photo_bytes, multipart

#: Jack's tale exactly as a new book is made from it (its seed's edition).
JACK = jack()
BOOK_PAGES = JACK.pages


# The full book: page 1 and page 3 cast the mother, page 2 the old man, and the
# giant's pages follow. Read the count from the catalogue so it tracks Step 7.
PAGE_COUNT = len(BOOK_PAGES)

CHILD_PHOTO = make_photo_bytes(colour="#3f6f8f")
MOTHER_PHOTO = make_photo_bytes(colour="#aa3322")


def _make_portrait(
    client: TestClient, photo: bytes = CHILD_PHOTO, character: str | None = None
) -> dict[str, object]:
    form: dict[str, object] = {
        "story": "jack_and_the_beanstalk",
        "photo": ("child.png", photo, "image/png"),
    }
    if character:
        form["character"] = character
    response = client.post("/api/cartoonify", **multipart(form))
    assert response.status_code == 201, response.text
    return dict(response.json())


def _start_book(
    client: TestClient, portrait: dict[str, object], characters: list[str] | None = None
) -> dict[str, object]:
    data: dict[str, object] = {
        "generation_id": portrait["generation_id"],
        "child_name": "Shai",
    }
    if characters:
        data["character"] = characters
    response = client.post("/api/book/start", data={"story": "jack_and_the_beanstalk", **data})
    assert response.status_code == 201, response.text
    return dict(response.json())


def _wait_for_book(
    client: TestClient, book_id: str, seconds: float = 10.0
) -> dict[str, object]:
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


def _page_requests(provider: RecordingProvider) -> list:
    """The book-page calls the runner actually made, in page number order."""
    requests = [r for r in provider.requests if r.label == "book-page"]
    return sorted(requests, key=lambda request: _page_number(request.prompt))


def _page_number(prompt: str) -> int:
    marker = "Draw page "
    start = prompt.index(marker) + len(marker)
    return int(prompt[start:].split(" ", 1)[0])


class TestCharacterCatalogue:
    def test_catalogue_lists_only_the_photoable_cast(self, client: TestClient) -> None:
        response = client.get("/api/stories/jack_and_the_beanstalk/characters")
        assert response.status_code == 200
        keys = [entry["key"] for entry in response.json()["characters"]]
        assert keys == [
            key for key, _ in [(member.key, member.label) for member in JACK.photoable_cast]
        ]
        assert "milky_white" not in keys
        assert "mother" in keys and "old_man" in keys and "giant" in keys
        assert all(entry["photoable"] is True for entry in response.json()["characters"])

    def test_the_cow_is_not_photoable(self) -> None:
        assert JACK.can_photograph("mother")
        assert JACK.can_photograph("old_man")
        assert not JACK.can_photograph("milky_white")

    def test_the_form_offers_a_slot_per_photoable_character_only(
        self, client: TestClient
    ) -> None:
        html = client.get("/stories/jack_and_the_beanstalk/studio").text
        for key, _ in [(member.key, member.label) for member in JACK.photoable_cast]:
            assert f'data-section="{key}"' in html
        # No upload path for the cow, anywhere in the studio.
        assert 'data-section="milky_white"' not in html
        assert "milky_white" not in html


class TestCharacterValidation:
    def test_milky_white_is_refused(self, client: TestClient) -> None:
        portrait = _make_portrait(client)
        response = client.post(
            "/api/book/start",
            data={
                "story": "jack_and_the_beanstalk",
                "generation_id": portrait["generation_id"],
                "character": f"milky_white:{portrait['generation_id']}",
            },
        )
        assert response.status_code == 422
        assert response.json()["error"] == "invalid_settings"

    def test_an_unknown_character_is_refused(self, client: TestClient) -> None:
        portrait = _make_portrait(client)
        response = client.post(
            "/api/book/start",
            data={
                "story": "jack_and_the_beanstalk",
                "generation_id": portrait["generation_id"],
                "character": f"dragon:{portrait['generation_id']}",
            },
        )
        assert response.status_code == 422
        assert response.json()["error"] == "invalid_settings"

    def test_one_character_may_not_be_photographed_twice(self, client: TestClient) -> None:
        portrait = _make_portrait(client)
        pair = f"mother:{portrait['generation_id']}"
        response = client.post(
            "/api/book/start",
            data={
                "story": "jack_and_the_beanstalk",
                "generation_id": portrait["generation_id"],
                "character": [pair, pair],
            },
        )
        assert response.status_code == 422
        assert response.json()["error"] == "invalid_settings"

    def test_a_malformed_pair_is_refused(self, client: TestClient) -> None:
        portrait = _make_portrait(client)
        response = client.post(
            "/api/book/start",
            data={
                "story": "jack_and_the_beanstalk",
                "generation_id": portrait["generation_id"],
                "character": "mother",
            },
        )
        assert response.status_code == 422
        assert response.json()["error"] == "invalid_settings"

    def test_the_cow_can_never_be_cartoonified(self, client: TestClient) -> None:
        response = client.post(
            "/api/cartoonify",
            **multipart(
                {
                    "story": "jack_and_the_beanstalk",
                    "photo": ("cow.png", MOTHER_PHOTO, "image/png"),
                    "character": "milky_white",
                }
            ),
        )
        assert response.status_code == 422
        assert response.json()["error"] == "invalid_settings"

    def test_an_unknown_character_slot_cannot_be_cartoonified(
        self, client: TestClient
    ) -> None:
        response = client.post(
            "/api/cartoonify",
            **multipart(
                {
                    "story": "jack_and_the_beanstalk",
                    "photo": ("x.png", MOTHER_PHOTO, "image/png"),
                    "character": "dragon",
                }
            ),
        )
        assert response.status_code == 422
        assert response.json()["error"] == "invalid_settings"

    def test_a_star_portrait_cannot_be_filed_as_a_character(self, client: TestClient) -> None:
        star = _make_portrait(client)
        response = client.post(
            "/api/book/start",
            data={
                "story": "jack_and_the_beanstalk",
                "generation_id": star["generation_id"],
                "character": f"mother:{star['generation_id']}",
            },
        )
        assert response.status_code == 422
        assert response.json()["error"] == "invalid_settings"

    def test_a_character_portrait_cannot_be_the_books_star(self, client: TestClient) -> None:
        mother = _make_portrait(client, MOTHER_PHOTO, character="mother")
        response = client.post(
            "/api/book/start",
            data={"story": "jack_and_the_beanstalk", "generation_id": mother["generation_id"]},
        )
        assert response.status_code == 422
        assert response.json()["error"] == "invalid_settings"

    def test_a_portrait_cannot_be_moved_to_a_different_slot(self, client: TestClient) -> None:
        child = _make_portrait(client, CHILD_PHOTO)
        mother = _make_portrait(client, MOTHER_PHOTO, character="mother")
        response = client.post(
            "/api/book/start",
            data={
                "story": "jack_and_the_beanstalk",
                "generation_id": child["generation_id"],
                "character": f"old_man:{mother['generation_id']}",
            },
        )
        assert response.status_code == 422
        assert response.json()["error"] == "invalid_settings"

    def test_an_expired_character_draft_is_unknown(self, client: TestClient) -> None:
        portrait = _make_portrait(client)
        response = client.post(
            "/api/book/start",
            data={
                "story": "jack_and_the_beanstalk",
                "generation_id": portrait["generation_id"],
                "character": "mother:does-not-exist",
            },
        )
        assert response.status_code == 404
        assert response.json()["error"] == "unknown_draft"


class TestPerPageReferences:
    def test_no_character_photos_means_the_child_double_anchor_only(
        self, client: TestClient, provider: RecordingProvider
    ) -> None:
        portrait = _make_portrait(client)
        start = _start_book(client, portrait)
        final = _wait_for_book(client, start["book_id"])
        assert final["status"] == "complete", final

        requests = _page_requests(provider)
        assert len(requests) == PAGE_COUNT
        for request in requests:
            # Exactly the approved portrait and the original photograph.
            assert len(request.reference_images) == 2, request.prompt[:200]
        # The same two anchor bytes on every page: no page is left without
        # them, and no page gains a third reference out of nowhere.
        assert len({request.reference_images for request in requests}) == 1

    def test_a_photographed_character_is_sent_only_on_their_pages(
        self, client: TestClient, provider: RecordingProvider
    ) -> None:
        child = _make_portrait(client, CHILD_PHOTO)
        mother = _make_portrait(client, MOTHER_PHOTO, character="mother")
        start = _start_book(client, child, [f"mother:{mother['generation_id']}"])
        final = _wait_for_book(client, start["book_id"])
        assert final["status"] == "complete", final

        by_page = {_page_number(r.prompt): r for r in _page_requests(provider)}
        assert sorted(by_page) == list(range(1, PAGE_COUNT + 1))

        # Both mother pages send four references (child portrait, child photo,
        # mother portrait, mother photo) and the very same four, in order.
        assert len(by_page[1].reference_images) == 4
        assert len(by_page[3].reference_images) == 4
        assert by_page[1].reference_images == by_page[3].reference_images

        # The trade page casts only the old man: the child's two anchors and
        # nothing else — the mother's photograph is never sent there.
        trade = by_page[2].reference_images
        assert len(trade) == 2
        assert by_page[1].reference_images[:2] == trade
        mother_photo = by_page[1].reference_images[3]
        assert mother_photo not in trade

    def test_the_cast_sheet_is_byte_identical_across_the_book(
        self, client: TestClient
    ) -> None:
        child = _make_portrait(client, CHILD_PHOTO)
        mother = _make_portrait(client, MOTHER_PHOTO, character="mother")
        start = _start_book(client, child, [f"mother:{mother['generation_id']}"])
        final = _wait_for_book(client, start["book_id"])
        assert final["status"] == "complete", final

        prompts = client.get(f"/book-media/{start['book_id']}/prompts.json").json()["prompts"]
        # The cast sheet is one paragraph of the prompt, so it is taken as that
        # paragraph rather than as the span between it and the reference map:
        # the two are no longer neighbours, because everything page-specific
        # now comes after the book-level block (see the prefix-cache test).
        sheets = {
            next(
                section
                for section in entry["prompt"].split("\n\n")
                if section.startswith("THE CAST (")
            )
            for entry in prompts
            if entry["key"] != "cover"
        }
        assert len(sheets) == 1, "the cast sheet drifted between pages"
        sheet = sheets.pop()
        # It is the whole book's cast, once — not just the page's own.
        # The mother has a photo in this book, so her photo-led look is used.
        assert "The mother (she has reference images, named below)" in sheet
        assert "The old man (no reference photo" in sheet
        # The cow is a prop of the places, not a cast member: the world sheet
        # carries her look, the cast sheet does not.
        assert "Milky-White" not in sheet
        assert "The giant (no reference photo" in sheet

    def test_the_page_reference_map_states_the_supplied_order(
        self, client: TestClient
    ) -> None:
        child = _make_portrait(client, CHILD_PHOTO)
        mother = _make_portrait(client, MOTHER_PHOTO, character="mother")
        start = _start_book(client, child, [f"mother:{mother['generation_id']}"])
        final = _wait_for_book(client, start["book_id"])
        assert final["status"] == "complete", final

        prompts = client.get(f"/book-media/{start['book_id']}/prompts.json").json()["prompts"]
        by_page = {
            int(entry["page"]): entry["prompt"]
            for entry in prompts
            if entry["page"] != "cover"
        }
        # The mother's pages name her two references after the child's two.
        assert "3) Mother's cartoon portrait" in by_page[1]
        assert "4) Mother's photograph" in by_page[1]
        # The trade page does not: only the child's two references are sent.
        assert "Mother's" not in by_page[2]
        assert "1) the child's cartoon portrait" in by_page[2]
        assert "2) the child's photograph" in by_page[2]


class TestCastPortraitPrompt:
    def test_a_character_portrait_is_not_prompted_as_the_child(
        self, client: TestClient, provider: RecordingProvider
    ) -> None:
        """The mother's cartoon was coming back prompted as "Jack, a boy".

        The cast pass must carry her own look and the book's fixed style, and
        none of the child's name, age, pronouns or outfit.
        """
        _make_portrait(client, MOTHER_PHOTO, character="mother")
        prompt = provider.requests[-1].prompt
        assert "THE CHARACTER: the woman in the photograph, as the mother" in prompt
        assert "TASK: Turn the person in the supplied photograph" in prompt
        # The child's clothing/identity block belongs only to the star.
        assert "The character's name is" not in prompt
        assert "Jack" not in prompt
        assert "Jane" not in prompt
        assert "storybook tunic" not in prompt.lower()
        assert (
            "TASK: Turn the person in the supplied photograph into a single character portrait"
            not in prompt
        )
        # The book's fixed style, word for word, on the character portrait too.
        style_sentence = prompt_text()["styles"][
            prompt_text()["stories"]["jack_and_the_beanstalk"]["style"]
        ]
        assert "ART STYLE: " + style_sentence in prompt
        # Issue #34: the slot's glasses / facial-hair switches reach the prompt
        # (the browser sent them; the prompt used to drop them).
        accessories = prompt_text()["accessories"]
        assert accessories["keep_glasses"] in prompt or accessories["remove_glasses"] in prompt
        assert (
            accessories["keep_facial_hair"] in prompt
            or accessories["remove_facial_hair"] in prompt
        )
        # The photo is the authority on head covering and hair, not the text.
        assert "grey-brown hair" not in prompt
        assert prompt.endswith(prompt_text()["negative"]["standing"])

    def test_the_star_portrait_still_names_the_child(
        self, client: TestClient, provider: RecordingProvider
    ) -> None:
        _make_portrait(client, CHILD_PHOTO)
        prompt = provider.requests[-1].prompt
        assert "The character's name is Jack" in prompt
        assert "THE CHARACTER: the mother" not in prompt
