"""The read-only gift link (W3): minted for the owner, readable by anyone, expiring.

A grandparent has no account, so the token *is* the grant. What these tests
hold the feature to:

* the token is signed — a changed character is a 404, not a different book;
* it expires, and it cannot be minted before the book is finished;
* what it opens is the finished book and nothing else: no sources ZIP, no
  original photo, no edit.
"""

from __future__ import annotations

import re
import time

from fastapi.testclient import TestClient

from app.services.share_links import issue, read

KEY = b"test-share-key"
NOW = 1_800_000_000


# --- the token itself ---------------------------------------------------------


def test_a_token_names_one_book_and_its_expiry() -> None:
    token = issue("book-abc", key=KEY, expires_at=NOW + 60)
    found = read(token, key=KEY, now=NOW)
    assert found is not None
    assert (found.book_id, found.expires_at) == ("book-abc", NOW + 60)


def test_a_changed_token_is_no_token() -> None:
    """One character in the payload or the signature is a forged link."""
    token = issue("book-abc", key=KEY, expires_at=NOW + 60)
    payload, _, signature = token.partition(".")
    for forged in (
        payload[:-1] + ("a" if payload[-1] != "a" else "b") + "." + signature,
        payload + "." + signature[:-1] + ("a" if signature[-1] != "a" else "b"),
        payload,  # no signature at all
        "not-a-token",
        "",
    ):
        assert read(forged, key=KEY, now=NOW) is None, forged


def test_an_expired_token_or_another_deployment_s_key_is_no_token() -> None:
    token = issue("book-abc", key=KEY, expires_at=NOW + 60)
    assert read(token, key=KEY, now=NOW + 61) is None
    assert read(token, key=b"another-deployment", now=NOW) is None


# --- the routes ---------------------------------------------------------------


def _start_book(client: TestClient) -> str:
    portrait = client.post(
        "/api/cartoonify",
        files={"photo": ("me.png", _photo(), "image/png")},
        data={"story": "jack_and_the_beanstalk", "photo_gender": "boy"},
    )
    assert portrait.status_code == 201, portrait.text
    started = client.post(
        "/api/book/start",
        data={
            "story": "jack_and_the_beanstalk",
            "generation_id": portrait.json()["generation_id"],
            "child_name": "Shai",
        },
    )
    assert started.status_code == 201, started.text
    return str(started.json()["book_id"])


def _photo() -> bytes:
    from tests.helpers import make_photo_bytes

    return make_photo_bytes()


def _wait(client: TestClient, book_id: str, seconds: float = 120.0) -> dict[str, object]:
    deadline = time.monotonic() + seconds
    last: dict[str, object] = {}
    while time.monotonic() < deadline:
        last = dict(client.get(f"/api/book/{book_id}").json())
        if last["status"] in {"complete", "failed"}:
            return last
        time.sleep(0.05)
    return last


def test_a_link_cannot_be_made_before_the_book_is_finished(client: TestClient) -> None:
    book_id = _start_book(client)
    response = client.get(f"/books/{book_id}/share", follow_redirects=False)
    assert response.status_code == 409, response.text


def test_the_owner_gets_a_link_and_it_reads_the_book(client: TestClient) -> None:
    book_id = _start_book(client)
    assert _wait(client, book_id)["status"] == "complete"

    minted = client.get(f"/books/{book_id}/share", follow_redirects=False)
    assert minted.status_code == 303, minted.text
    location = minted.headers["location"]
    assert location.startswith(f"/books/{book_id}?share="), location
    token = location.partition("?share=")[2]

    # The book page now shows the link to copy, with its expiry.
    page = client.get(location)
    assert page.status_code == 200
    assert 'id="share-link"' in page.text
    assert f"/share/{token}" in page.text

    # And the link itself works with no session at all.
    shared = TestClient(client.app).get(f"/share/{token}")
    assert shared.status_code == 200, shared.text
    assert "Shai" in shared.text
    assert "noindex" in shared.text.lower()
    assert shared.text.count("shared-spread") == 22, "the cover, twenty pages and About"
    # The token lives in the URL; nothing the page fetches may leak it onward.
    assert shared.headers["referrer-policy"] == "no-referrer"

    cover = client.get(f"/share/{token}/cover")
    assert cover.status_code == 200
    assert cover.headers["content-type"].startswith("image/")
    assert cover.headers["cache-control"] == "private, max-age=0, must-revalidate"

    first_page = client.get(f"/share/{token}/pages/1")
    assert first_page.status_code == 200
    assert first_page.headers["content-type"].startswith("image/")

    pdf = client.get(f"/share/{token}/pdf")
    assert pdf.status_code == 200
    assert pdf.headers["content-type"] == "application/pdf"


def test_a_link_opens_nothing_but_the_book(client: TestClient) -> None:
    """No sources ZIP, no prompts, no photograph, no edit — only the book."""
    book_id = _start_book(client)
    assert _wait(client, book_id)["status"] == "complete"
    token = (
        client.get(f"/books/{book_id}/share", follow_redirects=False)
        .headers["location"]
        .partition("?share=")[2]
    )

    for path in (
        f"/share/{token}/sources.zip",
        f"/share/{token}/prompts.json",
        f"/share/{token}/portrait",
        f"/share/{token}/photo",
    ):
        assert client.get(path).status_code == 404, path

    # Nothing under /share accepts a write, whatever the token says. The
    # module's whole surface is GET — that is the property, not the lucky
    # status of one route — and the owner's own write routes are not behind
    # a token at all, so a link holder has no way to reach one.
    writes = [
        route.path
        for route in client.app.routes
        if getattr(route, "path", "").startswith("/share")
        and getattr(route, "methods", None)
        and "GET" not in route.methods
    ]
    assert writes == []


def test_an_expired_link_is_a_404(client: TestClient) -> None:
    book_id = _start_book(client)
    assert _wait(client, book_id)["status"] == "complete"
    expired = issue(
        book_id,
        key=client.app.state.container.settings.session_signing_key(),
        expires_at=1,
    )
    response = client.get(f"/share/{expired}")
    assert response.status_code == 404
    assert not re.search(r"Shai", response.text), "an expired link must not leak the book"
