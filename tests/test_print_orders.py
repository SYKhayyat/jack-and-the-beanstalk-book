"""The printed copy: review, approve, address, place — and the money.

These tests exercise the whole flow against the real store and the real
wallet ledger, with a provider that never leaves the process:

* nothing reaches a provider before the exact PDF is approved;
* an edit after approval invalidates it;
* an order charges once and records the provider's test id;
* a provider failure gives the money back;
* Peecho's adapter sends the reviewed PDF and the address to its *test* host.

No test calls Peecho. The adapter test uses `httpx.MockTransport`, so there is
no network and no credential needed.
"""

from __future__ import annotations

import io
import time
from collections.abc import Iterator
from contextlib import contextmanager

import httpx
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.models.book import AboutMe
from app.services.print.base import PrintError, PrintJob, PrintQuote, ShippingAddress
from app.services.print.peecho import SANDBOX_BASE_URL, PeechoPrintProvider
from app.services.print.pricing import (
    GENERATION_COST_CENTS,
    PAYMENT_FIXED_CENTS,
    business_model,
)
from tests.helpers import RecordingProvider, make_photo_bytes, multipart
from tests.test_wallet_routes import SUB, FakeStripe, wallet_app

ADDRESS = {
    "name": "Shai Levi",
    "email": "shai@example.com",
    "street1": "1 Test Street",
    "city": "Testville",
    "state": "CA",
    "postcode": "90210",
    "country_code": "USA",
    "phone": "",
}


def _portrait(client: TestClient) -> dict[str, object]:
    response = client.post(
        "/api/cartoonify",
        **multipart(
            {
                "story": "jack_and_the_beanstalk",
                "photo_gender": "boy",
                "photo": ("me.png", make_photo_bytes(), "image/png"),
            }
        ),
    )
    assert response.status_code == 201, response.text
    return dict(response.json())


def _start(client: TestClient) -> str:
    portrait = _portrait(client)
    response = client.post(
        "/api/book/start",
        data={
            "story": "jack_and_the_beanstalk",
            "generation_id": portrait["generation_id"],
            "child_name": "Shai",
        },
    )
    assert response.status_code == 201, response.text
    return str(response.json()["book_id"])


def _wait(client: TestClient, book_id: str, seconds: float = 60.0) -> dict[str, object]:
    deadline = time.monotonic() + seconds
    last: dict[str, object] = {}
    while time.monotonic() < deadline:
        last = dict(client.get(f"/api/book/{book_id}").json())
        if last.get("status") in {"complete", "failed"}:
            return last
        time.sleep(0.03)
    return last


@contextmanager
def print_world(settings: Settings) -> Iterator[tuple[TestClient, str]]:
    """A signed-in owner with money, and one finished book."""
    with wallet_app(settings, RecordingProvider(), FakeStripe()) as (client, _stripe):
        container = client.app.state.container
        container.wallet.fund(
            SUB,
            amount_cents=10_000,
            session_id="seed-print-test",
            payment_id="pi_seed_print",
            description="Test funding",
        )
        book_id = _start(client)
        final = _wait(client, book_id)
        assert final["status"] == "complete", final
        yield client, book_id


def test_nothing_is_sent_before_the_pdf_is_approved(settings: Settings) -> None:
    with print_world(settings) as (client, book_id):
        page = client.get(f"/books/{book_id}/print")
        assert page.status_code == 200
        assert "reviewed this PDF" in page.text
        assert 'name="street1"' not in page.text, "the address form must wait for approval"
        provider = client.app.state.container.print_service._provider
        assert provider.submitted == []

        refused = client.post(f"/books/{book_id}/print/order", data=ADDRESS)
        assert refused.status_code == 200
        assert "review the PDF and approve" in refused.text
        assert provider.submitted == []
        # The book's own fee and the portrait it was made from; nothing for
        # printing, because nothing was sent.
        assert client.app.state.container.wallet.balance_cents(SUB) == 10_000 - 99 - 15


def test_approval_reveals_the_address_form_and_the_order_charges_once(
    settings: Settings,
) -> None:
    with print_world(settings) as (client, book_id):
        container = client.app.state.container
        approved = client.post(f"/books/{book_id}/print/approve", follow_redirects=False)
        assert approved.status_code == 303
        page = client.get(f"/books/{book_id}/print")
        assert 'name="street1"' in page.text
        assert "$24.99" in page.text

        order = container.print_service.approval(book_id)
        assert order is not None
        placed = client.post(
            f"/books/{book_id}/print/order", data={**ADDRESS, "order_id": order.id}
        )
        assert placed.status_code == 200, placed.text
        assert "Your printed copy is ordered" in placed.text
        assert "Test mode" in placed.text

        provider = container.print_service._provider
        assert len(provider.submitted) == 1
        sent = provider.submitted[0]
        assert sent.book_id == book_id
        assert sent.quote.page_count == 24
        # Measured from the PDF the owner approved, not a hard-coded size.
        assert 200 < sent.quote.page_width_mm < 300
        assert 200 < sent.quote.page_height_mm < 400
        assert sent.address.city == "Testville"
        assert "/print/files/" in sent.pdf_url and sent.pdf_url.endswith(".pdf")

        # One charge for the copy, one wallet row, and the digital book's own
        # fee from when it completed.
        balance = container.wallet.balance_cents(SUB)
        assert balance == 10_000 - 99 - 15 - 2499
        row = container.print_service.latest(book_id)
        assert row is not None and row.state == "submitted"
        assert row.provider_order_id.startswith("mock-")


def test_an_edit_after_approval_clears_it(settings: Settings) -> None:
    with print_world(settings) as (client, book_id):
        container = client.app.state.container
        client.post(f"/books/{book_id}/print/approve")
        approved = container.print_service.approval(book_id)
        assert approved is not None

        # An About edit rewrites the PDF, so the approved bytes are gone.
        edited = client.post(
            f"/api/book/{book_id}/edit",
            data={"action": "about", "about_name": "Noa"},
        )
        assert edited.status_code == 202, edited.text
        page = client.get(f"/books/{book_id}/print")
        assert "reviewed this PDF" in page.text
        assert 'name="street1"' not in page.text

        refused = client.post(
            f"/books/{book_id}/print/order", data={**ADDRESS, "order_id": approved.id}
        )
        assert refused.status_code == 200
        assert "changed after you reviewed" in refused.text
        assert container.print_service._provider.submitted == []


def test_a_book_with_unprintable_text_is_refused_at_order_time(
    settings: Settings,
) -> None:
    """A book made before the rule existed is still stopped before printing."""
    with print_world(settings) as (client, book_id):
        container = client.app.state.container
        store = container.book_store
        record = store.load_record(book_id)
        assert record is not None
        bad = AboutMe(
            prompt_name=record.about.prompt_name,
            about_name=record.about.about_name,
            details="You are an idiot",
        )
        store.update_about(book_id, bad, title=record.title)

        client.post(f"/books/{book_id}/print/approve")
        order = container.print_service.approval(book_id)
        assert order is not None
        refused = client.post(
            f"/books/{book_id}/print/order", data={**ADDRESS, "order_id": order.id}
        )
        assert refused.status_code == 200
        assert "cannot be printed" in refused.text
        assert container.print_service._provider.submitted == []


def test_a_provider_failure_returns_the_money(settings: Settings) -> None:
    class FailingProvider:
        name = "failing"
        mode = "test"

        def submit(self, **_kwargs: object) -> PrintJob:
            raise PrintError("The print service refused this order.")

    with print_world(settings) as (client, book_id):
        container = client.app.state.container
        container.print_service._provider = FailingProvider()  # type: ignore[assignment]
        client.post(f"/books/{book_id}/print/approve")
        order = container.print_service.approval(book_id)
        assert order is not None
        balance_before = container.wallet.balance_cents(SUB)

        response = client.post(
            f"/books/{book_id}/print/order", data={**ADDRESS, "order_id": order.id}
        )
        assert response.status_code == 200
        assert "refused this order" in response.text
        assert container.wallet.balance_cents(SUB) == balance_before
        failed = container.print_service.latest(book_id)
        assert failed is not None and failed.state == "failed"


def test_printing_is_unavailable_without_a_wallet(settings: Settings) -> None:
    with TestClient(create_app(settings, provider=RecordingProvider())) as client:
        # Password/open mode has no identity, and the page's own refusal is a
        # 404 rather than a lie about being able to ship a book.
        assert client.get("/books/anything/print").status_code == 404


def test_peecho_adapter_sends_the_book_to_its_test_host() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = request.read().decode()
        return httpx.Response(201, json={"id": 12345, "status": "created"})

    provider = PeechoPrintProvider(
        merchant_api_key="test-key",
        offering_id="offering-1",
        contact_email="operator@example.com",
        transport=httpx.MockTransport(handler),
    )
    quote = PrintQuote(
        page_count=24,
        printing_cents=786,
        shipping_cents=637,
        retail_cents=2499,
        page_width_mm=254.0,
        page_height_mm=272.1,
    )
    job = provider.submit(
        order_id="print_abc",
        book_id="book-1",
        pdf_url="https://example.test/print/files/token.pdf",
        address=ShippingAddress(**ADDRESS),
        quote=quote,
    )
    assert job.provider_order_id == "12345"
    assert str(captured["url"]).startswith(SANDBOX_BASE_URL)
    body = str(captured["body"])
    assert "https://example.test/print/files/token.pdf" in body
    assert '"number_of_pages":24' in body
    # The size comes from the PDF, not a fixed product shape.
    assert '"content_width":254.0' in body
    assert '"content_height":272.1' in body
    assert '"country_code":"USA"' in body
    assert "test-key" in body


def test_the_business_model_math_is_the_write_up_math() -> None:
    model = business_model(24)
    assert model.generation_cents == GENERATION_COST_CENTS
    assert model.printing_cents == 378 + 17 * 24
    assert model.shipping_cents == 637
    assert model.processing_cents == round(2499 * 2.9 / 100) + PAYMENT_FIXED_CENTS
    assert model.total_cost_cents == 158 + 786 + 637 + 102
    assert model.retail_cents == 2499
    assert model.margin_cents == 2499 - 1683
    assert round(model.margin_percent, 1) == 32.7


def test_the_book_pdf_has_a_cover_copyright_story_end_and_back_cover(
    settings: Settings,
) -> None:
    from pypdf import PdfReader

    with print_world(settings) as (client, book_id):
        data = client.app.state.container.book_store.read_pdf(book_id)
        assert data is not None
        reader = PdfReader(io.BytesIO(data))
        texts = [(page.extract_text() or "").replace("\n", " ") for page in reader.pages]
        assert len(reader.pages) == 24
        assert "Copyright" in texts[1]
        assert "Storybook Studio" in texts[1]
        assert "starring" in texts[0].lower()
        assert "The End" in texts[-2]
        assert "About" in texts[-1]
