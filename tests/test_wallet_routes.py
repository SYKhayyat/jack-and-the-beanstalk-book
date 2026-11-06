"""The wallet as a visitor meets it: a page, Stripe, and a book that costs 99¢.

Stripe is replaced by a fake provider at the app boundary, so these tests run
the real routes, the real wallet and the real book runner — only the network
call is gone. A session is minted with the app's own codec, which is exactly
what the Google callback does after it has verified somebody.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.services.image_providers.base import ImageProvider
from app.services.payments import (
    CheckoutSession,
    PaidCheckout,
    PaymentError,
    PaymentProvider,
    Refund,
)
from app.services.user_store import User
from app.services.wallet import FUNDING_AMOUNTS_CENTS
from app.sessions import SESSION_COOKIE, SessionKind
from tests.helpers import RecordingProvider, make_photo_bytes

SUB = "google-sub-1"


class FakeStripe(PaymentProvider):
    """A Stripe that remembers what it was asked to do."""

    name = "fake-stripe"

    def __init__(self) -> None:
        self.checkouts: list[dict[str, object]] = []
        self.reads: list[str] = []
        self.sessions: dict[str, PaidCheckout] = {}
        self.refunds: list[tuple[str, int]] = []
        self.fail_refund = False

    def create_checkout(
        self,
        *,
        amount_cents: int,
        currency: str,
        success_url: str,
        cancel_url: str,
        reference: str,
    ) -> CheckoutSession:
        checkout_id = f"cs_{len(self.checkouts) + 1}"
        self.checkouts.append(
            {
                "id": checkout_id,
                "amount_cents": amount_cents,
                "currency": currency,
                "success_url": success_url,
                "cancel_url": cancel_url,
                "reference": reference,
            }
        )
        return CheckoutSession(
            id=checkout_id,
            url=f"https://checkout.stripe.test/{checkout_id}",
            amount_cents=amount_cents,
            currency=currency,
        )

    def read_checkout(self, session_id: str) -> PaidCheckout:
        self.reads.append(session_id)
        if session_id not in self.sessions:
            raise PaymentError("Stripe payment failed: no such session")
        return self.sessions[session_id]

    def refund(self, *, payment_intent: str, amount_cents: int) -> Refund:
        self.refunds.append((payment_intent, amount_cents))
        if self.fail_refund:
            raise PaymentError("Stripe payment failed: card_declined")
        return Refund(
            id=f"re_{len(self.refunds)}", amount_cents=amount_cents, status="succeeded"
        )


@contextmanager
def wallet_app(
    settings: Settings, provider: ImageProvider, payments: PaymentProvider
) -> Iterator[tuple[TestClient, FakeStripe]]:
    """An app with Google mode on (so sessions carry an identity) and fake Stripe."""
    configured = settings.model_copy(
        update={
            "google_client_id": "client-id",
            "google_client_secret": "client-secret",
            "session_secret": "test-session-secret",
        }
    )
    assert isinstance(payments, FakeStripe)
    app = create_app(configured, provider=provider, payments=payments)
    with TestClient(app) as client:
        # The Google callback records the person before it signs them in; the
        # stand-in session has to leave the same row behind, because a book is
        # filed under a real `users` row.
        client.app.state.container.user_store.upsert(User(sub=SUB, name="Shai"))
        token = client.app.state.sessions.issue(
            SessionKind.GOOGLE, ttl_seconds=3600, sub=SUB, name="Shai"
        )
        client.cookies.set(SESSION_COOKIE, token)
        yield client, payments


@pytest.fixture
def stripe() -> FakeStripe:
    return FakeStripe()


@pytest.fixture
def browser(settings: Settings, provider: RecordingProvider, stripe: FakeStripe):
    with wallet_app(settings, provider, stripe) as (client, fake):
        yield client, fake


def paid_session(
    stripe: FakeStripe, cents: int = 1000, *, paid: bool = True, reference: str = SUB
) -> str:
    session_id = f"cs_{len(stripe.sessions) + 10}"
    stripe.sessions[session_id] = PaidCheckout(
        id=session_id,
        amount_cents=cents,
        currency="usd",
        payment_intent=f"pi_{session_id}",
        paid=paid,
        reference=reference,
    )
    return session_id


def test_the_wallet_page_starts_empty(browser) -> None:
    client, _ = browser
    response = client.get("/wallet")
    assert response.status_code == 200
    assert "$0.00" in response.text
    assert "$0.99" in response.text
    for cents in FUNDING_AMOUNTS_CENTS:
        assert f'value="{cents}"' in response.text


def test_a_top_up_sends_the_visitor_to_stripe(browser) -> None:
    client, stripe = browser
    response = client.post(
        "/wallet/topup", data={"amount_cents": 1000}, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"] == "https://checkout.stripe.test/cs_1"
    assert stripe.checkouts[0]["amount_cents"] == 1000
    assert stripe.checkouts[0]["reference"] == SUB
    # Stripe replaces this placeholder on the paid return trip.
    assert "{CHECKOUT_SESSION_ID}" in str(stripe.checkouts[0]["success_url"])


def test_an_amount_outside_the_closed_set_never_reaches_stripe(browser) -> None:
    client, stripe = browser
    response = client.post("/wallet/topup", data={"amount_cents": 1})
    assert response.status_code == 400
    assert stripe.checkouts == []


def test_a_paid_session_credits_the_wallet_once(browser) -> None:
    client, stripe = browser
    session_id = paid_session(stripe, 1000)

    first = client.get(f"/wallet/return?session_id={session_id}", follow_redirects=False)
    assert first.status_code == 303
    assert first.headers["location"].endswith("message=funded")
    assert client.get("/wallet").text.count("$10.00") >= 1

    # A refreshed success page is the normal case, not an error.
    client.get(f"/wallet/return?session_id={session_id}")
    page = client.get("/wallet").text
    assert page.count("$10.00") >= 1
    assert "Top-up — $10.00" in page


def test_an_unpaid_session_is_not_credited(browser) -> None:
    client, stripe = browser
    session_id = paid_session(stripe, 1000, paid=False)
    response = client.get(f"/wallet/return?session_id={session_id}", follow_redirects=False)
    assert response.headers["location"].endswith("message=payment_failed")
    assert "$0.00" in client.get("/wallet").text


def test_a_paid_session_for_some_other_amount_is_not_a_top_up(browser) -> None:
    client, stripe = browser
    session_id = paid_session(stripe, 1234)
    client.get(f"/wallet/return?session_id={session_id}")
    assert "$0.00" in client.get("/wallet").text


def test_someone_elses_paid_session_cannot_fund_this_wallet(browser) -> None:
    """The session carries our account id; a leaked id is not a gift."""
    client, stripe = browser
    session_id = paid_session(stripe, 1000, reference="google-sub-other")
    response = client.get(f"/wallet/return?session_id={session_id}", follow_redirects=False)
    assert response.headers["location"].endswith("message=payment_failed")
    assert "$0.00" in client.get("/wallet").text


def test_a_session_id_that_is_not_one_is_never_sent_to_stripe(browser) -> None:
    """The id is pasted into an API path; a fabricated one must not get that far."""
    client, stripe = browser
    for candidate in ("../refunds", "cs_1/../../v1/refunds", "not-a-session"):
        response = client.get(
            "/wallet/return", params={"session_id": candidate}, follow_redirects=False
        )
        assert response.headers["location"].endswith("message=payment_failed")
    assert stripe.reads == []


def test_an_unknown_session_id_is_a_failure_not_a_credit(browser) -> None:
    client, _ = browser
    response = client.get("/wallet/return?session_id=cs_made_up", follow_redirects=False)
    assert response.headers["location"].endswith("message=payment_failed")
    assert "$0.00" in client.get("/wallet").text


def test_a_refund_goes_back_to_the_card(browser) -> None:
    client, stripe = browser
    session_id = paid_session(stripe, 1000)
    client.get(f"/wallet/return?session_id={session_id}")

    response = client.post("/wallet/refund", follow_redirects=False)
    assert response.headers["location"].endswith("message=refunded")
    assert stripe.refunds == [(f"pi_{session_id}", 1000)]
    page = client.get("/wallet").text
    assert "$0.00" in page
    assert "processed" in page


def test_a_failed_refund_puts_the_money_back(browser) -> None:
    client, stripe = browser
    session_id = paid_session(stripe, 1000)
    client.get(f"/wallet/return?session_id={session_id}")
    stripe.fail_refund = True

    response = client.post("/wallet/refund", follow_redirects=False)
    assert response.headers["location"].endswith("message=refund_failed")
    page = client.get("/wallet").text
    assert "$10.00" in page
    assert "failed" in page


def test_with_nothing_in_the_wallet_a_book_is_refused_before_it_renders(browser) -> None:
    client, _ = browser
    portrait = client.post(
        "/api/cartoonify",
        files={"photo": ("me.png", make_photo_bytes(), "image/png")},
        data={"story": "jack_and_the_beanstalk"},
    )
    assert portrait.status_code == 201, portrait.text
    response = client.post(
        "/api/book/start",
        data={
            "story": "jack_and_the_beanstalk",
            "generation_id": portrait.json()["generation_id"],
        },
    )
    assert response.status_code == 402, response.text
    assert "$0.99" in response.json()["detail"]


def test_a_funded_book_is_charged_when_its_pdf_is_written(browser) -> None:
    client, stripe = browser
    client.get(f"/wallet/return?session_id={paid_session(stripe, 1000)}")
    portrait = client.post(
        "/api/cartoonify",
        files={"photo": ("me.png", make_photo_bytes(), "image/png")},
        data={"story": "jack_and_the_beanstalk"},
    )
    start = client.post(
        "/api/book/start",
        data={
            "story": "jack_and_the_beanstalk",
            "generation_id": portrait.json()["generation_id"],
        },
    )
    assert start.status_code == 201, start.text
    book_id = start.json()["book_id"]

    deadline = time.monotonic() + 10
    status = ""
    while time.monotonic() < deadline:
        status = client.get(f"/api/book/{book_id}").json()["status"]
        if status in {"complete", "failed"}:
            break
        time.sleep(0.05)
    assert status == "complete"

    page = client.get("/wallet").text
    assert "$9.01" in page
    assert "Book" in page
    assert "\u2212$0.99" in page


def test_the_wallet_has_no_page_without_an_identity(settings: Settings, provider) -> None:
    """The password fallback has no person, so it has no wallet."""
    app = create_app(settings, provider=provider)
    with TestClient(app) as client:
        assert client.get("/wallet", follow_redirects=False).status_code == 404
