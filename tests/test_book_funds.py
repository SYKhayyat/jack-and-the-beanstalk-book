"""One wallet, two books, one fee (B3).

The fee is taken when a book's PDF is written, so the start check has to count
the books already being made as money promised. Without that, two tabs holding
exactly $0.99 each pass the check, both render, and the ledger ends a fee short.

These tests use the same real routes, wallet and runner as `test_wallet_routes`,
with only Stripe and the image provider faked. The provider holds its calls
after the portrait, so the first book is still in flight when the second start
is attempted.
"""

from __future__ import annotations

import threading
import time
from typing import Any, cast

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.services.image_providers.base import GeneratedImage, ImageRequest
from tests.helpers import RecordingProvider, make_photo_bytes
from tests.test_wallet_routes import SUB, FakeStripe, wallet_app


@pytest.fixture
def stripe() -> FakeStripe:
    """A Stripe that records what it was asked, as `test_wallet_routes` uses."""
    return FakeStripe()


class _HoldingProvider(RecordingProvider):
    """Draws its free calls, then holds every picture until released."""

    name = "holding"

    def __init__(self, free_calls: int) -> None:
        super().__init__()
        self._free = free_calls
        self._lock = threading.Lock()
        self.release = threading.Event()

    def generate(self, request: ImageRequest) -> GeneratedImage:
        with self._lock:
            self._free -= 1
            held = self._free < 0
        if held:
            self.release.wait(timeout=30)
        return super().generate(request)


def _portrait(client: TestClient) -> dict[str, object]:
    response = client.post(
        "/api/cartoonify",
        files={"photo": ("me.png", make_photo_bytes(), "image/png")},
        data={"story": "jack_and_the_beanstalk", "photo_gender": "boy"},
    )
    assert response.status_code == 201, response.text
    return dict(response.json())


def _start(client: TestClient, generation_id: object, child_name: str) -> httpx.Response:
    return client.post(
        "/api/book/start",
        data={
            "story": "jack_and_the_beanstalk",
            "photo_gender": "boy",
            "generation_id": generation_id,
            "child_name": child_name,
        },
    )


def test_a_second_book_cannot_spend_the_fee_set_aside_for_the_first(
    settings: Settings, stripe: FakeStripe
) -> None:
    provider = _HoldingProvider(free_calls=1)  # the portrait, then hold
    with wallet_app(settings, provider, stripe) as (client, _):
        # The top-up route only offers $5/$10/$20, so fund the exact money the
        # test needs through the wallet itself: one picture for the portrait
        # and exactly one book's fee behind it.
        cast(Any, client.app).state.container.wallet.fund(
            SUB,
            amount_cents=99 + 15,
            session_id="cs_exact_price",
            payment_id="pi_exact_price",
            description="Test top-up",
        )
        portrait = _portrait(client)

        first = _start(client, portrait["generation_id"], "Shai")
        assert first.status_code == 201, first.text

        # A different child name is a different book, so this is not the
        # reuse path: it is a second $0.99 the wallet does not also have.
        second = _start(client, portrait["generation_id"], "Noa")

        assert second.status_code == 402, second.text
        detail = second.json()["detail"]
        assert "$0.99" in detail
        assert "set aside" in detail

        provider.release.set()
        book_id = first.json()["book_id"]
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            status = client.get(f"/api/book/{book_id}").json()["status"]
            if status in {"complete", "failed"}:
                break
            time.sleep(0.05)
        assert status == "complete", status

    # One book was made, so one fee was taken: the refused start cost nothing.
    assert "$0.00" in client.get("/wallet").text


def test_an_account_that_can_pay_for_two_books_may_start_two(
    settings: Settings, stripe: FakeStripe
) -> None:
    """The rule sets fees aside; it does not limit a family to one book at a time."""
    provider = _HoldingProvider(free_calls=1)
    with wallet_app(settings, provider, stripe) as (client, _):
        cast(Any, client.app).state.container.wallet.fund(
            SUB,
            amount_cents=1000,
            session_id="cs_two_books",
            payment_id="pi_two_books",
            description="Test top-up",
        )
        portrait = _portrait(client)

        first = _start(client, portrait["generation_id"], "Shai")
        second = _start(client, portrait["generation_id"], "Noa")

        assert first.status_code == 201, first.text
        assert second.status_code == 201, second.text

        provider.release.set()
        for response in (first, second):
            book_id = response.json()["book_id"]
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                status = client.get(f"/api/book/{book_id}").json()["status"]
                if status in {"complete", "failed"}:
                    break
                time.sleep(0.05)
            assert status == "complete", status

    # Two documents, two fees, and the portrait's one picture.
    assert "$7.87" in client.get("/wallet").text
