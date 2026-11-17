"""The printed copy: choose, approve, address, quote, place — and the money.

These tests exercise the whole flow against the real store and the real
wallet ledger, with a provider that never leaves the process:

* nothing reaches a provider before the owner approves a version and an edition;
* approval builds the two print files (interior and cover) at the edition's size;
* the buyer pays the edition's price plus the provider's shipping quote (A6);
* an edit after approval invalidates it;
* a refusal gives the money back; a lost answer leaves the order `unknown`,
  charged, and looked up before anything is sent again (A4);
* the printer's file links serve only the approved bytes (A3);
* Lulu's adapter talks to its sandbox host only (Q1).

No test calls Lulu: the adapter tests use `httpx.MockTransport`.
"""

from __future__ import annotations

import io
import json
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager

import httpx
import pytest
from fastapi.testclient import TestClient
from pypdf import PdfReader

from app.config import Settings
from app.main import create_app
from app.models.book import AboutMe, BookFormat
from app.services.print.base import (
    PrintError,
    PrintJob,
    PrintOrder,
    PrintUncertain,
    ShippingAddress,
)
from app.services.print.lulu import SANDBOX_BASE_URL, LuluPrintProvider
from app.services.print.mock import MockPrintProvider
from app.services.print.pricing import GENERATION_COST_CENTS, business_model
from app.services.print.products import PRODUCTS, Product, product_for
from app.services.share_links import issue
from tests.helpers import RecordingProvider, make_photo_bytes, multipart
from tests.test_wallet_routes import SUB, FakeStripe, wallet_app

ADDRESS = {
    "name": "Shai Levi",
    "email": "shai@example.com",
    "phone": "+1 555 010 0199",
    "street1": "1 Test Street",
    "city": "Testville",
    "state": "CA",
    "postcode": "90210",
    "country_code": "US",
}
#: The mock provider's US shipping line.
US_SHIPPING = 499
HARDCOVER = 2999
PAPERBACK = 1799
#: The digital book's own fee and the portrait it was made from.
BOOK_FEES = 99 + 15


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


def approve(
    client: TestClient, book_id: str, trim: str = "square", binding: str = "hardcover"
) -> PrintOrder:
    # The size is the book's own, set in the viewer; printing only binds it.
    client.app.state.container.book_store.set_format(book_id, BookFormat(trim))
    response = client.post(
        f"/books/{book_id}/print/approve",
        data={"binding": binding},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    assert response.headers["location"] == f"/books/{book_id}/print"
    return _approval(client, book_id)


def _approval(client: TestClient, book_id: str, seconds: float = 30.0) -> PrintOrder:
    """The approval the background build stores, once it has."""
    service = client.app.state.container.print_service
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        order = service.approval(book_id)
        if order is not None:
            return order
        time.sleep(0.02)
    raise AssertionError("the print files were never made")


def _settled(client: TestClient, book_id: str, seconds: float = 30.0) -> None:
    """Wait until no print build is running for this book."""
    preparer = client.app.state.container.print_preparer
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if book_id not in preparer._tasks:
            return
        time.sleep(0.02)
    raise AssertionError("the print build never finished")


def quote(client: TestClient, book_id: str, order: PrintOrder) -> PrintOrder:
    response = client.post(
        f"/books/{book_id}/print/quote",
        data={**ADDRESS, "order_id": order.id},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    quoted = client.app.state.container.print_service.get(order.id)
    assert quoted is not None and quoted.quoted_at is not None
    return quoted


def place(client: TestClient, book_id: str, order: PrintOrder) -> httpx.Response:
    return client.post(f"/books/{book_id}/print/order", data={"order_id": order.id})


@contextmanager
def print_world(settings: Settings, funds: int = 10_000) -> Iterator[tuple[TestClient, str]]:
    """A signed-in owner with money, and one finished book."""
    with wallet_app(settings, RecordingProvider(), FakeStripe()) as (client, _stripe):
        container = client.app.state.container
        container.wallet.fund(
            SUB,
            amount_cents=funds,
            session_id="seed-print-test",
            payment_id="pi_seed_print",
            description="Test funding",
        )
        book_id = _start(client)
        final = _wait(client, book_id)
        assert final["status"] == "complete", final
        yield client, book_id


def _provider(client: TestClient) -> MockPrintProvider:
    provider = client.app.state.container.print_service._provider
    assert isinstance(provider, MockPrintProvider)
    return provider


def test_nothing_is_sent_before_the_book_is_approved(settings: Settings) -> None:
    with print_world(settings) as (client, book_id):
        page = client.get(f"/books/{book_id}/print")
        assert page.status_code == 200
        assert 'id="approve-print"' in page.text
        assert "$29.99" in page.text and "$17.99" in page.text
        assert 'name="street1"' not in page.text, "the address form must wait for approval"

        refused = client.post(f"/books/{book_id}/print/order", data={"order_id": "nope"})
        assert refused.status_code == 200
        assert "choose an edition" in refused.text
        assert _provider(client).submitted == []
        assert client.app.state.container.wallet.balance_cents(SUB) == 10_000 - BOOK_FEES


@pytest.mark.parametrize(
    ("trim", "binding"),
    [
        ("square", "hardcover"),
        ("square", "paperback"),
        ("letter", "hardcover"),
        ("letter", "paperback"),
        ("wide", "hardcover"),
        ("wide", "paperback"),
    ],
)
def test_approval_builds_print_files_at_the_editions_size(
    settings: Settings, trim: str, binding: str
) -> None:
    product = product_for(trim, binding)
    with print_world(settings) as (client, book_id):
        order = approve(client, book_id, trim, binding)
        service = client.app.state.container.print_service
        assert order.product_key == product.key
        assert order.retail_cents == product.retail_cents
        assert order.page_count == product.printed_pages(order.page_count)

        interior = service.read_file(order.id, "interior", order.interior_sha256)
        cover = service.read_file(order.id, "cover", order.cover_sha256)
        assert interior is not None and cover is not None
        pages = PdfReader(io.BytesIO(interior)).pages
        assert len(pages) == order.page_count
        width, height = product.page_size
        for page in pages:
            assert float(page.mediabox.width) == pytest.approx(width, abs=0.5)
            assert float(page.mediabox.height) == pytest.approx(height, abs=0.5)
        spread = PdfReader(io.BytesIO(cover)).pages
        size = product.estimated_cover(order.page_count)
        assert len(spread) == 1
        assert float(spread[0].mediabox.width) == pytest.approx(size.width, abs=0.5)
        assert float(spread[0].mediabox.height) == pytest.approx(size.height, abs=0.5)


def test_the_buyer_pays_the_edition_plus_quoted_shipping_once(settings: Settings) -> None:
    with print_world(settings) as (client, book_id):
        container = client.app.state.container
        order = approve(client, book_id)
        page = client.get(f"/books/{book_id}/print")
        assert 'name="street1"' in page.text
        assert 'id="place-print-order"' not in page.text, "no order before a quote"

        refused = place(client, book_id, order)
        assert "delivery address" in refused.text
        assert _provider(client).submitted == []

        quoted = quote(client, book_id, order)
        assert quoted.shipping_cents == US_SHIPPING
        assert quoted.total_cents == HARDCOVER + US_SHIPPING
        page = client.get(f"/books/{book_id}/print")
        assert "$4.99" in page.text and "$34.98" in page.text
        assert 'id="place-print-order"' in page.text

        placed = place(client, book_id, quoted)
        assert placed.status_code == 200, placed.text
        assert "Your printed copy is ordered" in placed.text
        assert "$34.98" in placed.text

        sent = _provider(client).submitted
        assert len(sent) == 1
        assert sent[0].order_id == order.id
        assert sent[0].product.key == "square-hardcover"
        assert sent[0].pages == order.page_count
        assert sent[0].address.city == "Testville"
        assert "/print/files/" in sent[0].interior_url
        assert "/print/files/" in sent[0].cover_url
        assert sent[0].interior_url != sent[0].cover_url

        assert container.wallet.balance_cents(SUB) == 10_000 - BOOK_FEES - 3498
        row = container.print_service.latest(book_id)
        assert row is not None and row.state == "submitted"
        assert row.provider_order_id.startswith("mock-")

        # A second press of the same button neither charges nor orders again.
        place(client, book_id, quoted)
        assert len(_provider(client).submitted) == 1
        assert container.wallet.balance_cents(SUB) == 10_000 - BOOK_FEES - 3498


def test_an_invalid_address_is_not_quoted(settings: Settings) -> None:
    with print_world(settings) as (client, book_id):
        order = approve(client, book_id)
        response = client.post(
            f"/books/{book_id}/print/quote",
            data={**ADDRESS, "phone": "", "order_id": order.id},
        )
        assert response.status_code == 200
        assert "phone number" in response.text
        stored = client.app.state.container.print_service.get(order.id)
        assert stored is not None and stored.quoted_at is None


def test_an_unknown_edition_is_refused(settings: Settings) -> None:
    with print_world(settings) as (client, book_id):
        response = client.post(f"/books/{book_id}/print/approve", data={"binding": "spiral"})
        assert response.status_code == 200
        assert "choose one of the editions" in response.text
        assert client.app.state.container.print_service.approval(book_id) is None


def test_approving_returns_at_once_and_the_page_shows_the_files_being_made(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The print files build in the background; the request does not wait."""
    with print_world(settings) as (client, book_id):
        runner = client.app.state.container.book_runner
        real = runner.print_interior
        release = threading.Event()
        builds: list[str] = []

        def slow_interior(book: str, product: Product) -> bytes | None:
            builds.append(book)
            assert release.wait(30)
            return real(book, product)

        monkeypatch.setattr(runner, "print_interior", slow_interior)
        first = client.post(
            f"/books/{book_id}/print/approve",
            data={"binding": "hardcover"},
            follow_redirects=False,
        )
        assert first.status_code == 303
        page = client.get(f"/books/{book_id}/print")
        assert 'id="print-preparing"' in page.text
        assert "print-preparing.js" in page.text
        assert 'id="approve-print"' not in page.text

        # A second press while it builds joins the build, not a second one.
        again = client.post(
            f"/books/{book_id}/print/approve",
            data={"binding": "hardcover"},
            follow_redirects=False,
        )
        assert again.status_code == 303
        release.set()
        order = _approval(client, book_id)
        _settled(client, book_id)
        assert builds == [book_id]

        page = client.get(f"/books/{book_id}/print")
        assert 'id="print-preparing"' not in page.text
        assert 'name="street1"' in page.text
        assert order.page_count > 0


def test_a_print_build_that_fails_shows_why_and_offers_the_button_again(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    with print_world(settings) as (client, book_id):
        runner = client.app.state.container.book_runner
        monkeypatch.setattr(runner, "print_cover", lambda *_args: None)
        response = client.post(
            f"/books/{book_id}/print/approve",
            data={"binding": "hardcover"},
            follow_redirects=False,
        )
        assert response.status_code == 303
        _settled(client, book_id)
        page = client.get(f"/books/{book_id}/print")
        assert "no cover picture to print" in page.text
        assert 'id="approve-print"' in page.text
        assert 'id="print-preparing"' not in page.text
        assert client.app.state.container.print_service.approval(book_id) is None


def test_an_edit_after_approval_clears_it(settings: Settings) -> None:
    with print_world(settings) as (client, book_id):
        order = quote(client, book_id, approve(client, book_id))

        # An About edit rewrites the PDF, so the approved version is gone.
        edited = client.post(
            f"/api/book/{book_id}/edit",
            data={"action": "about", "about_name": "Noa"},
        )
        assert edited.status_code == 202, edited.text
        page = client.get(f"/books/{book_id}/print")
        assert 'id="approve-print"' in page.text
        assert 'name="street1"' not in page.text

        refused = place(client, book_id, order)
        assert "changed after you reviewed" in refused.text
        assert _provider(client).submitted == []


def test_a_book_with_unprintable_text_is_refused_at_order_time(
    settings: Settings,
) -> None:
    """A book made before the rule existed is still stopped before printing."""
    with print_world(settings) as (client, book_id):
        store = client.app.state.container.book_store
        record = store.load_record(book_id)
        assert record is not None
        bad = AboutMe(
            prompt_name=record.about.prompt_name,
            about_name=record.about.about_name,
            details="You are an idiot",
        )
        store.update_about(book_id, bad, title=record.title)

        order = quote(client, book_id, approve(client, book_id))
        refused = place(client, book_id, order)
        assert "cannot be printed" in refused.text
        assert _provider(client).submitted == []


class RefusingProvider(MockPrintProvider):
    def submit(self, **_kwargs: object) -> PrintJob:  # type: ignore[override]
        raise PrintError("The print service refused this request. Nothing was printed.")


class LosingProvider(MockPrintProvider):
    """Takes the job, then the answer is lost on the way back."""

    def __init__(self) -> None:
        super().__init__()
        self.lose = True

    def submit(self, **kwargs: object) -> PrintJob:  # type: ignore[override]
        job = super().submit(**kwargs)  # type: ignore[arg-type]
        if self.lose:
            raise PrintUncertain("The print service did not answer in time.")
        return job


class TimingOutProvider(MockPrintProvider):
    """Never takes the job, and never answers."""

    def submit(self, **_kwargs: object) -> PrintJob:  # type: ignore[override]
        raise PrintUncertain("The print service did not answer in time.")


class BrokenProvider(MockPrintProvider):
    def submit(self, **_kwargs: object) -> PrintJob:  # type: ignore[override]
        raise RuntimeError("a bug the adapter did not wrap")


def test_a_provider_refusal_returns_the_money(settings: Settings) -> None:
    with print_world(settings) as (client, book_id):
        container = client.app.state.container
        container.print_service._provider = RefusingProvider()
        order = quote(client, book_id, approve(client, book_id))
        before = container.wallet.balance_cents(SUB)

        response = place(client, book_id, order)
        assert "refused this request" in response.text
        assert container.wallet.balance_cents(SUB) == before
        failed = container.print_service.latest(book_id)
        assert failed is not None and failed.state == "failed"


def test_a_lost_answer_keeps_the_charge_and_is_looked_up_before_a_retry(
    settings: Settings,
) -> None:
    with print_world(settings) as (client, book_id):
        container = client.app.state.container
        provider = LosingProvider()
        container.print_service._provider = provider
        order = quote(client, book_id, approve(client, book_id))
        before = container.wallet.balance_cents(SUB)

        response = place(client, book_id, order)
        assert "did not confirm whether it took this order" in response.text
        row = container.print_service.latest(book_id)
        assert row is not None and row.state == "unknown"
        assert container.wallet.balance_cents(SUB) == before - 3498, "no automatic refund"
        page = client.get(f"/books/{book_id}/print")
        assert 'id="retry-print-order"' in page.text
        assert 'id="cancel-print-order"' in page.text

        # Try again: the printer has the job, so it is found, not sent twice.
        provider.lose = False
        retried = place(client, book_id, row)
        assert "Your printed copy is ordered" in retried.text
        assert len(provider.submitted) == 1
        assert container.wallet.balance_cents(SUB) == before - 3498


def test_an_unexpected_error_is_unknown_not_refunded(settings: Settings) -> None:
    with print_world(settings) as (client, book_id):
        container = client.app.state.container
        container.print_service._provider = BrokenProvider()
        order = quote(client, book_id, approve(client, book_id))
        before = container.wallet.balance_cents(SUB)
        place(client, book_id, order)
        row = container.print_service.latest(book_id)
        assert row is not None and row.state == "unknown"
        assert container.wallet.balance_cents(SUB) == before - 3498


def test_cancelling_an_unknown_order_refunds_only_when_the_printer_has_none(
    settings: Settings,
) -> None:
    with print_world(settings) as (client, book_id):
        container = client.app.state.container
        container.print_service._provider = TimingOutProvider()
        order = quote(client, book_id, approve(client, book_id))
        before = container.wallet.balance_cents(SUB)
        place(client, book_id, order)

        cancelled = client.post(f"/books/{book_id}/print/cancel", data={"order_id": order.id})
        assert cancelled.status_code == 200
        row = container.print_service.latest(book_id)
        assert row is not None and row.state == "failed"
        assert container.wallet.balance_cents(SUB) == before

        # A second request cannot refund twice.
        client.post(f"/books/{book_id}/print/cancel", data={"order_id": order.id})
        assert container.wallet.balance_cents(SUB) == before


def test_cancelling_an_order_the_printer_has_keeps_it(settings: Settings) -> None:
    with print_world(settings) as (client, book_id):
        container = client.app.state.container
        container.print_service._provider = LosingProvider()
        order = quote(client, book_id, approve(client, book_id))
        before = container.wallet.balance_cents(SUB)
        place(client, book_id, order)

        client.post(f"/books/{book_id}/print/cancel", data={"order_id": order.id})
        row = container.print_service.latest(book_id)
        assert row is not None and row.state == "submitted"
        assert container.wallet.balance_cents(SUB) == before - 3498


def test_an_approval_already_being_placed_cannot_be_placed_twice(
    settings: Settings,
) -> None:
    """A double submit must not charge twice or order a second copy."""
    with print_world(settings) as (client, book_id):
        container = client.app.state.container
        service = container.print_service
        order = quote(client, book_id, approve(client, book_id))
        # Another request has taken the approval and is mid-order.
        assert service._store.claim(order.id) is not None
        before = container.wallet.balance_cents(SUB)

        page = client.get(f"/books/{book_id}/print")
        assert 'id="print-placing"' in page.text
        response = place(client, book_id, order)
        assert "already being placed" in response.text
        assert _provider(client).submitted == []
        assert container.wallet.balance_cents(SUB) == before


def test_a_short_wallet_leaves_the_approval_to_try_again(settings: Settings) -> None:
    with print_world(settings, funds=1_000) as (client, book_id):
        container = client.app.state.container
        service = container.print_service
        order = quote(client, book_id, approve(client, book_id))
        before = container.wallet.balance_cents(SUB)

        response = place(client, book_id, order)
        assert "balance is" in response.text
        assert _provider(client).submitted == []
        assert container.wallet.balance_cents(SUB) == before
        # The claim was handed back, so the page still offers the order.
        again = service.approval(book_id)
        assert again is not None and again.state == "approved"


def test_print_file_links_serve_only_the_approved_bytes(settings: Settings) -> None:
    """A3: the link names the hash; a share link opens nothing here."""
    with print_world(settings) as (client, book_id):
        container = client.app.state.container
        order = approve(client, book_id)
        key = container.settings.session_signing_key()
        expires = int(time.time()) + 600

        def token(payload: str, scope: str = "print-file") -> str:
            return issue(payload, key=key, expires_at=expires, scope=scope)

        good = client.get(
            f"/print/files/{token(f'{order.id}/interior/{order.interior_sha256}')}.pdf"
        )
        assert good.status_code == 200
        assert good.headers["content-type"].startswith("application/pdf")
        assert good.content[:4] == b"%PDF"
        cover = client.get(
            f"/print/files/{token(f'{order.id}/cover/{order.cover_sha256}')}.pdf"
        )
        assert cover.status_code == 200 and cover.content != good.content

        stale = token(f"{order.id}/interior/{'0' * 64}")
        assert client.get(f"/print/files/{stale}.pdf").status_code == 404
        wrong_kind = token(f"{order.id}/pdf/{order.interior_sha256}")
        assert client.get(f"/print/files/{wrong_kind}.pdf").status_code == 404
        share = token(book_id, scope="share")
        assert client.get(f"/print/files/{share}.pdf").status_code == 404
        print_token = token(f"{order.id}/interior/{order.interior_sha256}")
        assert client.get(f"/share/{print_token}").status_code == 404


def test_the_receipt_shows_what_was_paid(settings: Settings) -> None:
    with print_world(settings) as (client, book_id):
        order = quote(client, book_id, approve(client, book_id, "letter", "paperback"))
        place(client, book_id, order)
        page = client.get(f"/books/{book_id}/print")
        assert "Your printed copy is ordered" in page.text
        assert "$22.98" in page.text  # $17.99 + $4.99


def test_printing_is_unavailable_without_a_wallet(settings: Settings) -> None:
    with TestClient(create_app(settings, provider=RecordingProvider())) as client:
        # Password/open mode has no identity, and the page's own refusal is a
        # 404 rather than a lie about being able to ship a book.
        assert client.get("/books/anything/print").status_code == 404


# -- products and the business model ---------------------------------------------


def test_short_books_are_padded_to_the_bindings_minimum() -> None:
    hard = PRODUCTS["square-hardcover"]
    soft = PRODUCTS["square-paperback"]
    assert hard.printed_pages(5) == 24
    assert soft.printed_pages(5) == 32, "Lulu validates perfect bound from 32"
    assert hard.printed_pages(25) == 26, "a leaf has two sides"
    with pytest.raises(ValueError):
        hard.printed_pages(801)


def test_lulu_package_ids() -> None:
    assert PRODUCTS["square-hardcover"].pod_package_id == "0850X0850.FC.PRE.CW.080CW444.GXX"
    assert PRODUCTS["letter-paperback"].pod_package_id == "0850X1100.FC.PRE.PB.080CW444.GXX"


def test_the_business_model_math_is_the_write_up_math() -> None:
    hard = business_model(PRODUCTS["square-hardcover"], 24)
    assert hard.generation_cents == GENERATION_COST_CENTS
    assert hard.printing_cents == 1614
    assert hard.processing_cents == 117
    assert hard.total_cost_cents == 1889
    assert hard.margin_cents == 1110
    assert round(hard.margin_percent, 1) == 37.0

    soft = business_model(PRODUCTS["letter-paperback"], 32)
    assert soft.printing_cents == 903
    assert soft.processing_cents == 82
    assert soft.total_cost_cents == 1143
    assert soft.margin_cents == 656
    assert round(soft.margin_percent, 1) == 36.5

    # Shipping is passed on at cost: only Stripe's cut on it moves the margin.
    shipped = business_model(PRODUCTS["square-hardcover"], 24, shipping_cents=500)
    assert shipped.charged_cents == 3499
    assert shipped.margin_cents == 1110 - (shipped.processing_cents - 117)


def test_the_book_pdf_has_a_cover_copyright_story_end_and_back_cover(
    settings: Settings,
) -> None:
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


# -- Lulu's adapter, against a fake sandbox -----------------------------------------


class FakeLulu:
    def __init__(self) -> None:
        self.calls: list[httpx.Request] = []
        self.token_calls = 0
        self.jobs: list[dict[str, object]] = []
        self.fail_submit: Exception | int | None = None

    def __call__(self, request: httpx.Request) -> httpx.Response:
        assert str(request.url).startswith(SANDBOX_BASE_URL)
        self.calls.append(request)
        path = request.url.path
        if path.endswith("/openid-connect/token"):
            self.token_calls += 1
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        assert request.headers["authorization"] == "Bearer tok"
        body = json.loads(request.content) if request.content else {}
        if path == "/cover-dimensions/":
            assert body["interior_page_count"] == 24
            return httpx.Response(
                200, json={"width": "1311.0", "height": "720.0", "unit": "pt"}
            )
        if path == "/print-job-cost-calculations/":
            return httpx.Response(
                201,
                json={
                    "currency": "USD",
                    "total_cost_incl_tax": "23.20",
                    "line_item_costs": [{"total_cost_incl_tax": "16.14"}],
                },
            )
        if path == "/print-jobs/" and request.method == "POST":
            if isinstance(self.fail_submit, Exception):
                raise self.fail_submit
            if isinstance(self.fail_submit, int):
                return httpx.Response(self.fail_submit, text="no")
            job = {"id": 42, "external_id": body["external_id"], "status": {"name": "CREATED"}}
            self.jobs.append({**job, "body": body})
            return httpx.Response(201, json=job)
        if path == "/print-jobs/" and request.method == "GET":
            wanted = request.url.params["search"]
            return httpx.Response(
                200,
                json={"results": [j for j in self.jobs if j["external_id"] == wanted]},
            )
        return httpx.Response(404)


def _lulu(fake: FakeLulu) -> LuluPrintProvider:
    return LuluPrintProvider(
        client_key="key",
        client_secret="secret",
        contact_email="operator@example.com",
        transport=httpx.MockTransport(fake),
    )


def _submit(provider: LuluPrintProvider, product: Product) -> PrintJob:
    return provider.submit(
        order_id="print_abc",
        product=product,
        pages=24,
        interior_url="https://example.test/print/files/i.pdf",
        cover_url="https://example.test/print/files/c.pdf",
        address=ShippingAddress(**ADDRESS),
        title="Shai and the Beanstalk",
    )


def test_lulu_sizes_quotes_and_submits_with_one_cached_token() -> None:
    fake = FakeLulu()
    provider = _lulu(fake)
    product = PRODUCTS["square-hardcover"]

    size = provider.cover_size(product, 24)
    assert size.width == 1311.0 and size.height == 720.0

    cost = provider.quote(product, 24, ShippingAddress(**ADDRESS))
    assert cost.printing_cents == 1614
    assert cost.shipping_cents == 2320 - 1614, "everything beyond printing is shipping"

    job = _submit(provider, product)
    assert job.provider_order_id == "42" and job.state == "CREATED"
    sent = fake.jobs[0]["body"]
    assert isinstance(sent, dict)
    assert sent["external_id"] == "print_abc"
    item = sent["line_items"][0]
    assert item["printable_normalization"]["pod_package_id"] == product.pod_package_id
    assert item["printable_normalization"]["interior"]["source_url"].endswith("/i.pdf")
    assert item["printable_normalization"]["cover"]["source_url"].endswith("/c.pdf")
    assert sent["shipping_address"]["country_code"] == "US"
    assert sent["shipping_address"]["phone_number"] == ADDRESS["phone"]

    found = provider.find("print_abc")
    assert found is not None and found.provider_order_id == "42"
    assert provider.find("print_other") is None
    assert fake.token_calls == 1


def test_lulu_reports_a_lost_answer_as_uncertain() -> None:
    product = PRODUCTS["square-paperback"]
    for failure in (httpx.ReadTimeout("slow"), 502):
        fake = FakeLulu()
        fake.fail_submit = failure
        with pytest.raises(PrintUncertain):
            _submit(_lulu(fake), product)


def test_lulu_reports_a_refusal_or_no_connection_as_a_plain_error() -> None:
    product = PRODUCTS["square-paperback"]
    for failure in (httpx.ConnectError("down"), 400):
        fake = FakeLulu()
        fake.fail_submit = failure
        with pytest.raises(PrintError) as caught:
            _submit(_lulu(fake), product)
        assert not isinstance(caught.value, PrintUncertain)


def test_the_lulu_provider_refuses_to_start_without_its_keys() -> None:
    with pytest.raises(PrintError, match="LULU_CLIENT_KEY"):
        LuluPrintProvider(client_key="", client_secret="s", contact_email="a@b.co")
    with pytest.raises(PrintError, match="LULU_SHIPPING_LEVEL"):
        LuluPrintProvider(
            client_key="k", client_secret="s", contact_email="a@b.co", shipping_level="FAST"
        )
    with pytest.raises(PrintError, match="LULU_CONTACT_EMAIL"):
        LuluPrintProvider(client_key="k", client_secret="s", contact_email=" ")
