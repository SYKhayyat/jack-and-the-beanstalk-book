"""Every picture beyond a book's own fee is bought as it is drawn (B4/C3).

A book is one fee of 99¢, and that fee includes the cover and every story page
it draws for itself. Everything drawn *on request* after that — a portrait, a
redrawn page or cover, a replaced face's pages — costs 15¢ a picture, taken
from the wallet before the picture is drawn. When the wallet cannot cover the
next one the drawing stops, the pictures already made are kept, and the book
page says why in a notice the visitor can put away.

These tests run the real routes, wallet and runner against the fake Stripe the
wallet tests already use; only the network call is gone.
"""

from __future__ import annotations

from typing import Any, cast

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.pricing import PICTURE_PRICE_CENTS
from app.services.wallet import KIND_CHARGE, KIND_PICTURE, KIND_PICTURE_REFUND
from tests.helpers import RecordingProvider, make_photo_bytes, multipart
from tests.test_book_api import wait_for_book
from tests.test_wallet_routes import SUB, FakeStripe, wallet_app


@pytest.fixture
def stripe() -> FakeStripe:
    return FakeStripe()


def _wallet(client: TestClient) -> Any:
    return cast(Any, client.app).state.container.wallet


def _fund(client: TestClient, cents: int = 500) -> None:
    _wallet(client).fund(
        SUB,
        amount_cents=cents,
        session_id=f"cs_pictures_{cents}",
        payment_id=f"pi_pictures_{cents}",
        description="Test top-up",
    )


def _portrait(client: TestClient, **overrides: object) -> dict[str, object]:
    form: dict[str, object] = {
        "story": "jack_and_the_beanstalk",
        "photo_gender": "boy",
        "photo": ("me.png", make_photo_bytes(), "image/png"),
    }
    form.update(overrides)
    response = client.post("/api/cartoonify", **multipart(form))
    assert response.status_code == 201, response.text
    return dict(response.json())


def _start(client: TestClient, generation_id: object) -> str:
    response = client.post(
        "/api/book/start",
        data={
            "story": "jack_and_the_beanstalk",
            "generation_id": generation_id,
            "child_name": "Noa",
        },
    )
    assert response.status_code == 201, response.text
    return str(response.json()["book_id"])


def _run(client: TestClient, generation_id: object) -> str:
    """A whole book, rendered and finished: the fixture most of these need."""
    book_id = _start(client, generation_id)
    status = wait_for_book(client, book_id, seconds=120)
    assert status["status"] == "complete", status.get("error")
    return book_id


def _charged(client: TestClient, kind: str) -> int:
    """What the ledger says was spent (+credited) under one kind, in cents."""
    entries = [entry for entry in _wallet(client).history(SUB) if entry.kind == kind]
    return sum(int(entry.amount_cents) for entry in entries)


def test_a_portrait_costs_one_picture(settings: Settings, stripe: FakeStripe) -> None:
    provider = RecordingProvider()
    with wallet_app(settings, provider, stripe) as (client, _):
        _fund(client)
        _portrait(client)

        assert _charged(client, KIND_PICTURE) == -PICTURE_PRICE_CENTS
        assert _wallet(client).balance_cents(SUB) == 500 - PICTURE_PRICE_CENTS
        # The price is on the screen before the button is pressed, not only in
        # the error after it.
        studio = client.get("/stories/jack_and_the_beanstalk/studio").text
        assert "Each portrait is $0.15 a picture" in studio


def test_a_portrait_the_wallet_cannot_pay_for_is_not_drawn(
    settings: Settings, stripe: FakeStripe
) -> None:
    provider = RecordingProvider()
    with wallet_app(settings, provider, stripe) as (client, _):
        response = client.post(
            "/api/cartoonify",
            files={"photo": ("me.png", make_photo_bytes(), "image/png")},
            data={"story": "jack_and_the_beanstalk", "photo_gender": "boy"},
        )

        assert response.status_code == 402, response.text
        detail = response.json()["detail"]
        assert "$0.15" in detail
        assert "$0.00" in detail
        # Nothing was drawn, so nothing was billed: the zero-balance account
        # cannot turn a public URL into an open tab at the model.
        assert provider.requests == []
        assert _wallet(client).balance_cents(SUB) == 0


def test_a_portrait_only_charges_for_the_pictures_it_drew(
    settings: Settings, stripe: FakeStripe
) -> None:
    """Three candidates are paid for up front; one returned pays for one."""
    provider = RecordingProvider()  # returns one picture whatever is asked for
    with wallet_app(settings, provider, stripe) as (client, _):
        _fund(client)
        _portrait(client, candidates=3)

        assert _charged(client, KIND_PICTURE) == -(3 * PICTURE_PRICE_CENTS)
        assert _charged(client, KIND_PICTURE_REFUND) == 2 * PICTURE_PRICE_CENTS
        assert _wallet(client).balance_cents(SUB) == 500 - PICTURE_PRICE_CENTS


def test_a_new_book_draws_its_own_pictures_within_its_fee(
    settings: Settings, stripe: FakeStripe
) -> None:
    provider = RecordingProvider()
    with wallet_app(settings, provider, stripe) as (client, _):
        _fund(client)
        portrait = _portrait(client)
        _run(client, portrait["generation_id"])

        # The book's 21 pictures cost nothing beyond its fee: only the portrait
        # was ever charged as a picture.
        assert _charged(client, KIND_PICTURE) == -PICTURE_PRICE_CENTS
        assert _charged(client, KIND_CHARGE) == -99
        assert _wallet(client).balance_cents(SUB) == 500 - PICTURE_PRICE_CENTS - 99


def test_a_redrawn_cover_costs_a_picture(settings: Settings, stripe: FakeStripe) -> None:
    provider = RecordingProvider()
    with wallet_app(settings, provider, stripe) as (client, _):
        _fund(client)
        portrait = _portrait(client)
        book_id = _run(client, portrait["generation_id"])

        response = client.post(f"/api/book/{book_id}/edit", data={"action": "cover"})
        assert response.status_code == 202, response.text
        status = wait_for_book(client, book_id, seconds=120)
        assert status["status"] == "complete", status.get("error")

        assert _charged(client, KIND_PICTURE) == -(2 * PICTURE_PRICE_CENTS)


def test_a_redraw_with_no_money_left_is_refused_before_it_starts(
    settings: Settings, stripe: FakeStripe
) -> None:
    provider = RecordingProvider()
    with wallet_app(settings, provider, stripe) as (client, _):
        _fund(client)
        portrait = _portrait(client)
        book_id = _run(client, portrait["generation_id"])
        drawn = len(provider.requests)
        _wallet(client).request_refund(SUB)  # everything refundable leaves

        response = client.post(f"/api/book/{book_id}/edit", data={"action": "cover"})

        assert response.status_code == 402, response.text
        assert "$0.15" in response.json()["detail"]
        assert len(provider.requests) == drawn  # nothing was drawn


def test_running_out_mid_redraw_stops_and_keeps_what_was_drawn(
    settings: Settings, stripe: FakeStripe
) -> None:
    """Two pictures' money, a whole-book face swap: two are drawn, then it stops."""
    provider = RecordingProvider()
    with wallet_app(settings, provider, stripe) as (client, _):
        _fund(client)
        portrait = _portrait(client)
        book_id = _run(client, portrait["generation_id"])
        # Leave exactly two pictures' worth: the swap asks for 21.
        balance = _wallet(client).balance_cents(SUB)
        _wallet(client).request_refund(SUB, amount_cents=balance - 2 * PICTURE_PRICE_CENTS)
        before = len(provider.requests)

        response = client.post(
            f"/api/book/{book_id}/edit",
            data={
                "action": "character",
                "character_key": "star",
                "generation_id": portrait["generation_id"],
            },
        )
        assert response.status_code == 202, response.text
        status = wait_for_book(client, book_id, seconds=120)

        assert status["status"] == "failed", status
        assert status["error_code"] == "out_of_funds"
        assert "$0.15" in str(status["error"])
        # Only the pictures the wallet could pay for were drawn, and the pages
        # already finished are still on the plan.
        assert len(provider.requests) - before <= 2
        assert _wallet(client).balance_cents(SUB) >= 0
        done = [page for page in status["pages"] if page["status"] == "done"]
        assert done, status

        # The page says why, in a notice the visitor can put away, and names
        # the price of finishing; the fee is on the redraw buttons too.
        page_html = client.get(f"/books/{book_id}").text
        assert 'class="funds-notice"' in page_html
        assert "funds-dismiss" in page_html
        assert 'class="inline-message error hidden" id="failed-message"' in page_html
        assert "Finish this book — $0.15 a picture" in page_html
        assert "Redraw this page — $0.15" in page_html
