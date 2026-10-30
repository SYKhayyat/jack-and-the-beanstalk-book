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

from app.models.book import BOOK_PAGES
from app.services.prompts import cast_photoable, photoable_cast
from tests.helpers import RecordingProvider, make_photo_bytes, multipart

# The full book: page 1 and page 3 cast the mother, page 2 the old man, and the
# giant's pages follow. Read the count from the catalogue so it tracks Step 7.
PAGE_COUNT = len(BOOK_PAGES)

CHILD_PHOTO = make_photo_bytes(colour="#3f6f8f")
MOTHER_PHOTO = make_photo_bytes(colour="#aa3322")


def _make_portrait(client: TestClient, photo: bytes = CHILD_PHOTO) -> dict[str, object]:
    response = client.post(
        "/api/cartoonify",
        **multipart({"photo": ("child.png", photo, "image/png")}),
    )
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
    response = client.post("/api/book/start", data=data)
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
        response = client.get("/api/book/characters")
        assert response.status_code == 200
        keys = [entry["key"] for entry in response.json()["characters"]]
        assert keys == [key for key, _ in photoable_cast()]
        assert "milky_white" not in keys
        assert "mother" in keys and "old_man" in keys and "giant" in keys
        assert all(entry["photoable"] is True for entry in response.json()["characters"])

    def test_the_cow_is_not_photoable(self) -> None:
        assert cast_photoable("mother")
        assert cast_photoable("old_man")
        assert not cast_photoable("milky_white")

    def test_the_form_offers_a_slot_per_photoable_character_only(
        self, client: TestClient
    ) -> None:
        html = client.get("/").text
        for key, _ in photoable_cast():
            assert f'data-character="{key}"' in html
        # No upload path for the cow, anywhere in the form.
        assert 'data-character="milky_white"' not in html
        assert "character_photo_milky_white" not in html


class TestCharacterValidation:
    def test_milky_white_is_refused(self, client: TestClient) -> None:
        portrait = _make_portrait(client)
        response = client.post(
            "/api/book/start",
            data={
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
            data={"generation_id": portrait["generation_id"], "character": [pair, pair]},
        )
        assert response.status_code == 422
        assert response.json()["error"] == "invalid_settings"

    def test_a_malformed_pair_is_refused(self, client: TestClient) -> None:
        portrait = _make_portrait(client)
        response = client.post(
            "/api/book/start",
            data={
                "generation_id": portrait["generation_id"],
                "character": "mother",
            },
        )
        assert response.status_code == 422
        assert response.json()["error"] == "invalid_settings"

    def test_an_expired_character_draft_is_unknown(self, client: TestClient) -> None:
        portrait = _make_portrait(client)
        response = client.post(
            "/api/book/start",
            data={
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
        mother = _make_portrait(client, MOTHER_PHOTO)
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
        mother = _make_portrait(client, MOTHER_PHOTO)
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
        }
        assert len(sheets) == 1, "the cast sheet drifted between pages"
        sheet = sheets.pop()
        # It is the whole book's cast, once — not just the page's own.
        assert "The mother is a character" in sheet
        assert "The old man is a character" in sheet
        assert "Milky-White is the family's cow" in sheet
        assert "The giant is a character" in sheet

    def test_the_page_reference_map_states_the_supplied_order(
        self, client: TestClient
    ) -> None:
        child = _make_portrait(client, CHILD_PHOTO)
        mother = _make_portrait(client, MOTHER_PHOTO)
        start = _start_book(client, child, [f"mother:{mother['generation_id']}"])
        final = _wait_for_book(client, start["book_id"])
        assert final["status"] == "complete", final

        prompts = client.get(f"/book-media/{start['book_id']}/prompts.json").json()["prompts"]
        by_page = {int(entry["page"]): entry["prompt"] for entry in prompts}
        # The mother's pages name her two references after the child's two.
        assert "3) Mother's cartoon portrait" in by_page[1]
        assert "4) Mother's photograph" in by_page[1]
        # The trade page does not: only the child's two references are sent.
        assert "Mother's" not in by_page[2]
        assert "1) the child's cartoon portrait" in by_page[2]
        assert "2) the child's photograph" in by_page[2]
