# ruff: noqa: RUF001 -- the typography cases are curly quotes on purpose.
"""The finished book read on screen: a book, and a light one (UI.org bundle B).

* G4/U8: every story page is one surface (art, then its words on the page, a
  folio) and the cover carries its title, as the shelf and the PDF do;
* U7: a finished book opens on its cover;
* P1/P2: pictures reach the screen as sized WebP under versioned URLs that a
  browser may keep for a year, while a stale version is revalidated;
* typographic punctuation in the reader and the PDF alike.
"""

from __future__ import annotations

import io
import re

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app.services.pictures import (
    THUMB_WIDTH,
    VARIANT_WIDTHS,
    encode_variant,
    page_slot,
    picture_set,
    slot_page,
)
from app.services.typography import book_text
from tests.test_frontend_structure import STATIC, _book, _wait


@pytest.fixture
def finished(client: TestClient) -> tuple[TestClient, str]:
    book_id = _book(client)
    assert _wait(client, book_id)["status"] == "complete"
    return client, book_id


# --- the URL scheme ---------------------------------------------------------------


def test_a_picture_set_names_every_size_under_one_version() -> None:
    pictures = picture_set("/book-media/b/pages/3", "ab" * 32)
    assert pictures.src == "/book-media/b/pages/3?w=1600&v=abababababababab"
    assert pictures.srcset.count("&v=abababababababab") == 2
    assert "800w" in pictures.srcset and "1600w" in pictures.srcset
    assert pictures.thumb == f"/book-media/b/pages/3?w={THUMB_WIDTH}&v=abababababababab"


def test_a_picture_without_a_hash_falls_back_to_its_plain_url() -> None:
    pictures = picture_set("/book-media/b/cover", None)
    assert (pictures.src, pictures.srcset, pictures.thumb) == (
        "/book-media/b/cover",
        "",
        "/book-media/b/cover",
    )


def test_slots_round_trip() -> None:
    assert page_slot(0) == "cover"
    assert slot_page(page_slot(7)) == 7
    for slot in ("cover", "portrait", "page-0", "page-x", "pages-2"):
        assert slot_page(slot) is None


def test_a_variant_is_webp_and_never_enlarged() -> None:
    buffer = io.BytesIO()
    Image.new("RGB", (1152, 864), (200, 120, 40)).save(buffer, format="PNG")
    small = encode_variant(buffer.getvalue(), 800)
    large = encode_variant(buffer.getvalue(), 1600)
    with Image.open(io.BytesIO(small)) as image:
        assert (image.format, image.size) == ("WEBP", (800, 600))
    with Image.open(io.BytesIO(large)) as image:
        assert image.size == (1152, 864)
    with pytest.raises(ValueError, match="px wide"):
        encode_variant(buffer.getvalue(), 333)


# --- serving ------------------------------------------------------------------------


def test_a_current_version_is_kept_for_a_year_and_is_small(
    finished: tuple[TestClient, str],
) -> None:
    client, book_id = finished
    page = next(
        entry
        for entry in client.get(f"/api/book/{book_id}").json()["pages"]
        if entry["number"] == 1
    )
    full = client.get(page["url"])
    sized = client.get(page["src"])
    assert sized.status_code == 200
    assert sized.headers["content-type"] == "image/webp"
    assert sized.headers["cache-control"] == "private, max-age=31536000, immutable"
    assert len(sized.content) < len(full.content)
    # A second view is the stored copy, byte for byte.
    assert client.get(page["src"]).content == sized.content
    # And a revalidation is a 304.
    again = client.get(page["src"], headers={"If-None-Match": sized.headers["etag"]})
    assert again.status_code == 304


def test_a_stale_version_gets_todays_picture_and_must_revalidate(
    finished: tuple[TestClient, str],
) -> None:
    client, book_id = finished
    response = client.get(f"/book-media/{book_id}/cover?w=800&v=0000000000000000")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "private, max-age=0, must-revalidate"


def test_only_the_named_widths_exist(finished: tuple[TestClient, str]) -> None:
    client, book_id = finished
    assert client.get(f"/book-media/{book_id}/pages/1?w=1000").status_code == 422
    for width in VARIANT_WIDTHS:
        assert client.get(f"/book-media/{book_id}/portrait?w={width}").status_code == 200


def test_a_variant_is_dropped_with_its_book(finished: tuple[TestClient, str]) -> None:
    client, book_id = finished
    assert client.get(f"/book-media/{book_id}/pages/1?w=800").status_code == 200
    assert client.delete(f"/api/book/{book_id}").status_code in (200, 204)
    assert client.get(f"/book-media/{book_id}/pages/1?w=800").status_code == 404


# --- the page -------------------------------------------------------------------------


def test_the_reader_sets_every_page_as_a_book(finished: tuple[TestClient, str]) -> None:
    client, book_id = finished
    html = client.get(f"/books/{book_id}").text
    # The cover carries its title, as the shelf and the PDF do (U8).
    cover = re.search(r'id="spread-0".*?</article>', html, flags=re.DOTALL)
    assert cover is not None
    assert 'class="book-cover-title"' in cover.group(0)
    # Story pages: sized art, the words on the page, a folio; page 1 a drop cap.
    assert html.count('class="leaf-text"') == 20
    assert html.count('class="folio"') == 20
    assert 'class="spread-story story-text drop-cap"' in html
    assert "srcset=" in html and "?w=800&amp;v=" in html
    # The page picker stays, folded, once the book is finished (U9).
    assert re.search(r'<details class="page-picker" id="page-picker">', html)
    assert f"/book-media/{book_id}/pages/1?w={THUMB_WIDTH}&amp;v=" in html
    # Nothing straight where a printer would curl it.
    for story in re.findall(r'<p class="spread-story[^"]*">(.*?)</p>', html):
        assert "'" not in story and "&#39;" not in story


def test_a_finished_book_opens_on_its_cover() -> None:
    source = (STATIC / "book-live.js").read_text()
    assert 'if (status === "complete") return 0;' in source
    assert 'if (status === "complete") return lastIndex;' not in source


def test_the_face_edit_says_whose_he_or_she_the_portrait_follows() -> None:
    source = (STATIC / "book-live.js").read_text()
    assert 'portraitForm.append("photo_gender", bookGender)' in source


# --- typography ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("typed", "printed"),
    [
        ("'Go straight there,' she said.", "‘Go straight there,’ she said."),
        ('"Fee-fi-fo-fum!"', "“Fee-fi-fo-fum!”"),
        ("Jack's beans", "Jack’s beans"),
        ("up -- and up...", "up—and up…"),
        ('צה"ל', 'צה"ל'),
    ],
)
def test_book_text_sets_punctuation_like_a_printer(typed: str, printed: str) -> None:
    assert book_text(typed) == printed
