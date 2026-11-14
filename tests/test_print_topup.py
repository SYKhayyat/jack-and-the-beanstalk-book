"""Paying for a printed copy from a wallet that is short (A7).

A copy costs more than the largest preset top-up, so the print page offers
"add the difference" instead: one trip to Stripe for an amount the server
works out, then straight back to the order form.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

import pytest

from app.config import Settings
from app.services.payments import PaidCheckout
from app.services.wallet import MAX_TOPUP_CENTS, is_offered_topup, topup_for_shortfall
from tests.helpers import RecordingProvider
from tests.test_print_orders import ADDRESS, _start, _wait
from tests.test_wallet_routes import SUB, FakeStripe, wallet_app


def test_the_difference_is_whole_dollars_and_never_under_five() -> None:
    assert topup_for_shortfall(0) == 0
    assert topup_for_shortfall(-300) == 0
    assert topup_for_shortfall(1) == 500
    assert topup_for_shortfall(2113) == 2200
    assert topup_for_shortfall(2400) == 2400
    with pytest.raises(ValueError):
        topup_for_shortfall(MAX_TOPUP_CENTS + 1)


def test_only_an_amount_the_app_could_ask_for_is_credited() -> None:
    assert is_offered_topup(500) and is_offered_topup(2200)
    assert not is_offered_topup(2213), "not whole dollars"
    assert not is_offered_topup(400), "under the smallest preset"
    assert not is_offered_topup(MAX_TOPUP_CENTS + 100)


def test_a_short_wallet_tops_up_the_difference_once_and_then_orders(
    settings: Settings,
) -> None:
    stripe = FakeStripe()
    with wallet_app(settings, RecordingProvider(), stripe) as (client, _):
        container = client.app.state.container
        container.wallet.fund(
            SUB,
            amount_cents=500,
            session_id="seed-short",
            payment_id="pi_seed_short",
            description="Test funding",
        )
        book_id = _start(client)
        assert _wait(client, book_id)["status"] == "complete"
        client.post(f"/books/{book_id}/print/approve")
        balance = container.wallet.balance_cents(SUB)
        need = topup_for_shortfall(2499 - balance)

        page = client.get(f"/books/{book_id}/print")
        assert 'id="print-topup-button"' in page.text
        assert f"Add ${need // 100}.00 to my wallet" in page.text

        # The amount is the server's: whatever the form says is ignored.
        sent = client.post(
            f"/books/{book_id}/print/topup",
            data={"amount_cents": "1"},
            follow_redirects=False,
        )
        assert sent.status_code == 303
        assert sent.headers["location"].startswith("https://checkout.stripe.test/")
        checkout = stripe.checkouts[-1]
        assert checkout["amount_cents"] == need
        assert checkout["reference"] == SUB

        # Stripe sends the visitor back; the wallet is credited, and the
        # visitor lands on the print page, not the wallet.
        session_id = str(checkout["id"])
        stripe.sessions[session_id] = PaidCheckout(
            id=session_id,
            amount_cents=need,
            currency="usd",
            payment_intent=f"pi_{session_id}",
            paid=True,
            reference=SUB,
        )
        success = urlsplit(str(checkout["success_url"]))
        query = parse_qs(success.query)
        back = client.get(
            f"{success.path}?session_id={session_id}&next={query['next'][0]}",
            follow_redirects=False,
        )
        assert back.status_code == 303
        assert back.headers["location"].startswith(f"/books/{book_id}/print?")
        assert container.wallet.balance_cents(SUB) == balance + need

        page = client.get(back.headers["location"])
        assert 'id="print-topup-button"' not in page.text

        order = container.print_service.approval(book_id)
        assert order is not None
        placed = client.post(
            f"/books/{book_id}/print/order", data={**ADDRESS, "order_id": order.id}
        )
        assert "Your printed copy is ordered" in placed.text
        assert container.wallet.balance_cents(SUB) == balance + need - 2499


def test_a_wallet_that_can_pay_is_not_offered_a_top_up(settings: Settings) -> None:
    stripe = FakeStripe()
    with wallet_app(settings, RecordingProvider(), stripe) as (client, _):
        client.app.state.container.wallet.fund(
            SUB,
            amount_cents=10_000,
            session_id="seed-rich",
            payment_id="pi_seed_rich",
            description="Test funding",
        )
        book_id = _start(client)
        assert _wait(client, book_id)["status"] == "complete"
        client.post(f"/books/{book_id}/print/approve")
        assert 'id="print-topup-button"' not in client.get(f"/books/{book_id}/print").text
        nothing = client.post(f"/books/{book_id}/print/topup", follow_redirects=False)
        assert nothing.status_code == 303
        assert nothing.headers["location"] == f"/books/{book_id}/print"
        assert stripe.checkouts == []
