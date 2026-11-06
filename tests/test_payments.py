"""The Stripe seam, tested without Stripe.

`httpx.MockTransport` is the whole point: the provider's contract is its HTTP
requests and responses, and the suite stays offline. The key is fake but shaped
like a real one, so a leak test can prove it never appears in an error.
"""

from __future__ import annotations

import base64
from collections.abc import Callable

import httpx
import pytest

from app.services.payments import (
    PaymentError,
    StripePaymentProvider,
)

# Deliberately not shaped like a real key: a fake that matches a provider's
# pattern would trip GitHub's push protection and force an unblock on every
# push. It only has to be a unique string the redaction test can look for.
KEY = "not-a-real-stripe-credential-for-tests"


def provider(handler: Callable[[httpx.Request], httpx.Response]) -> StripePaymentProvider:
    return StripePaymentProvider(KEY, transport=httpx.MockTransport(handler))


def test_a_checkout_session_is_one_line_item_with_our_reference() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "id": "cs_1",
                "url": "https://checkout.stripe.com/c/pay/cs_1",
                "amount_total": 1000,
                "currency": "usd",
            },
        )

    checkout = provider(handler).create_checkout(
        amount_cents=1000,
        currency="usd",
        success_url="https://book.test/wallet/return?session_id={CHECKOUT_SESSION_ID}",
        cancel_url="https://book.test/wallet?message=cancelled",
        reference="sub-1",
    )

    assert checkout.id == "cs_1"
    assert checkout.url == "https://checkout.stripe.com/c/pay/cs_1"
    request = seen[0]
    assert request.method == "POST"
    assert request.url.path == "/v1/checkout/sessions"
    # Basic auth: the key is the username, never a query parameter or a body
    # field that could end up in a log.
    assert request.headers["authorization"].startswith("Basic ")
    body = request.content.decode()
    assert "client_reference_id=sub-1" in body
    assert "unit_amount%5D=1000" in body
    assert "amount_total" not in body


def test_a_paid_session_is_read_back_with_its_payment_intent() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={
                "id": "cs_1",
                "amount_total": 500,
                "currency": "usd",
                "payment_status": "paid",
                "payment_intent": "pi_1",
                "client_reference_id": "sub-1",
            },
        )

    checkout = provider(handler).read_checkout("cs_1")
    assert checkout.paid is True
    assert checkout.amount_cents == 500
    assert checkout.payment_intent == "pi_1"
    # The account the session was created for, echoed back by Stripe.
    assert checkout.reference == "sub-1"
    assert seen[0].method == "GET"
    assert seen[0].url.path == "/v1/checkout/sessions/cs_1"


def test_an_unpaid_session_is_reported_as_unpaid() -> None:
    paid = provider(
        lambda _: httpx.Response(
            200,
            json={
                "id": "cs_1",
                "amount_total": 500,
                "currency": "usd",
                "payment_status": "unpaid",
                "payment_intent": None,
            },
        )
    ).read_checkout("cs_1")
    assert paid.paid is False
    assert paid.payment_intent == ""


def test_a_refund_names_the_payment_and_the_amount() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"id": "re_1", "amount": 500, "status": "succeeded"})

    refund = provider(handler).refund(payment_intent="pi_1", amount_cents=500)
    assert refund.id == "re_1"
    assert refund.amount_cents == 500
    body = seen[0].content.decode()
    assert "payment_intent=pi_1" in body
    assert "amount=500" in body


def test_stripes_refusal_is_raised_without_the_key() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            402,
            json={"error": {"type": "card_error", "message": "Your card was declined."}},
        )

    with pytest.raises(PaymentError) as caught:
        provider(handler).refund(payment_intent="pi_1", amount_cents=500)
    assert "declined" in str(caught.value)
    assert KEY not in str(caught.value)


def test_a_transport_failure_is_raised_without_the_key() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError(f"boom while using {KEY}")

    with pytest.raises(PaymentError) as caught:
        provider(handler).read_checkout("cs_1")
    assert "Stripe payment failed" in str(caught.value)
    assert KEY not in str(caught.value)


def test_a_response_that_is_not_json_is_a_payment_error() -> None:
    gateway = provider(lambda _: httpx.Response(200, text="<html>gateway</html>"))
    with pytest.raises(PaymentError):
        gateway.read_checkout("cs_1")


def test_an_unexpected_response_shape_is_a_payment_error() -> None:
    with pytest.raises(PaymentError):
        provider(lambda _: httpx.Response(200, json=[])).read_checkout("cs_1")


def test_an_empty_key_is_refused() -> None:
    with pytest.raises(ValueError):
        StripePaymentProvider("")


def test_a_pasted_key_is_stripped_before_it_becomes_a_header() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(
            200,
            json={"id": "cs_1", "url": "https://checkout.test/cs_1", "amount_total": 500},
        )

    pasted = StripePaymentProvider(f"  {KEY}\n", transport=httpx.MockTransport(handler))
    pasted.create_checkout(
        amount_cents=500,
        currency="usd",
        success_url="https://book.test/wallet/return",
        cancel_url="https://book.test/wallet",
        reference="sub-1",
    )
    expected = base64.b64encode(f"{KEY}:".encode()).decode()
    assert seen[0].headers["authorization"] == f"Basic {expected}"


def test_a_live_key_is_refused_at_boot() -> None:
    """The app promises test mode; a live key must not quietly move money."""
    live_key = "sk_" + "live_example"
    with pytest.raises(ValueError, match="test mode"):
        StripePaymentProvider(live_key)


def test_a_refund_that_stripe_marks_failed_is_not_recorded_as_sent() -> None:
    refused = provider(
        lambda _: httpx.Response(200, json={"id": "re_1", "amount": 500, "status": "failed"})
    )
    with pytest.raises(PaymentError):
        refused.refund(payment_intent="pi_1", amount_cents=500)
