"""The wallet page: fund it, read the ledger, send money back.

Four routes around one ledger:

    GET  /wallet                 -> balance, top-up buttons, history, refunds
    POST /wallet/topup           -> make a Stripe Checkout Session, redirect to it
    GET  /wallet/return          -> verify the paid session, then credit once
    POST /wallet/refund          -> request a refund and send it through Stripe

Two rules shape the code:

* **Money is only credited from Stripe's own answer.** `session_id` comes back
  in the URL, and a visitor can type any value; the id is therefore read from
  Stripe with our secret key, and only a paid session in the closed set of
  top-up amounts is credited. The wallet's own `fund` is idempotent per
  session, so a refreshed success page cannot credit twice.
* **A failure is shown as a sentence this app wrote.** Stripe's error text is
  for the operator's log; the page gets one of a closed set of messages
  selected by a query parameter, so no wording from outside ever reaches it.
"""

from __future__ import annotations

import asyncio
import logging
import re

from fastapi import APIRouter, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.container import Container
from app.dependencies import ContainerDep
from app.routes.auth import CurrentSessionDep
from app.routes.web import page_chrome
from app.services.costs import CATEGORY_LABELS, format_microusd
from app.services.payments import PaymentError
from app.services.wallet import (
    CURRENCY,
    FUNDING_AMOUNTS_CENTS,
    KIND_CHARGE,
    REFUND_PROCESSED,
    WalletError,
    format_cents,
)

__all__ = ["router"]

logger = logging.getLogger(__name__)

router = APIRouter(tags=["wallet"])

#: A Checkout Session id as Stripe writes it. The value arrives in a query
#: string, so it is validated before it is pasted into an outgoing API path.
_SESSION_ID = re.compile(r"cs_[A-Za-z0-9_]{1,200}")

#: What the page may say. A closed set, like the login page's: a message in a
#: URL can only select a sentence this app already wrote.
_MESSAGES = {
    "funded": "Your top-up arrived — the balance below is up to date.",
    "cancelled": "No payment was taken.",
    "refunded": "The refund was accepted and is on its way back to your card.",
    "refund_failed": (
        "Stripe could not complete that refund, so the money is back in your wallet."
    ),
    "nothing_to_refund": "There is nothing to refund right now.",
    "payment_failed": "That payment did not go through. No money was taken.",
    "payments_off": "Payments are not configured on this deployment.",
}


def _owner(session: CurrentSessionDep) -> str:
    """The signed-in account's id, or a redirect-worthy refusal.

    The auth guard guarantees a session here; what it cannot guarantee is an
    *identity*, because the password fallback carries none. A wallet belongs
    to a person, so there is nothing to show without one.
    """
    if session is None or not session.user_id:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="There is no wallet without a signed-in account.",
        )
    return session.user_id


@router.get("/wallet", response_class=HTMLResponse)
def wallet_page(
    request: Request, container: ContainerDep, session: CurrentSessionDep, message: str = ""
) -> HTMLResponse:
    """The account's balance, the way to add to it, and every movement."""
    owner = _owner(session)
    templates: Jinja2Templates = request.app.state.templates
    wallet = container.wallet
    # One read each: the page shows the balance twice (big number and the
    # refund button) and each read is a round trip in database mode.
    balance = wallet.balance_cents(owner)
    refundable = wallet.refundable_cents(owner) if container.payments else 0
    history = wallet.history(owner)
    charged = [
        entry.reference.removeprefix("book:")
        for entry in history
        if entry.kind == KIND_CHARGE and entry.reference
    ]
    made_for = container.costs.book_costs_microusd(charged)
    return templates.TemplateResponse(
        request=request,
        name="wallet.html",
        context={
            **page_chrome(container, session),
            "balance_cents": balance,
            "balance_label": format_cents(balance),
            "price_label": format_cents(wallet.price_cents),
            "refundable_cents": refundable,
            "refundable_label": format_cents(refundable),
            "amounts": [(cents, format_cents(cents)) for cents in FUNDING_AMOUNTS_CENTS],
            "history": [
                {
                    "description": entry.description,
                    "amount_label": format_cents(entry.amount_cents),
                    "credit": entry.amount_cents > 0,
                    "created_at": entry.created_at,
                    # What the book behind a charge cost this app to make.
                    "made_for": format_microusd(made_for[book_id])
                    if (book_id := (entry.reference or "").removeprefix("book:")) in made_for
                    and entry.kind == KIND_CHARGE
                    else "",
                }
                for entry in history
            ],
            "costs": cost_panel(container, wallet.price_cents),
            "refunds": [
                {
                    "amount_label": format_cents(request_refund.amount_cents),
                    "status": request_refund.status,
                    "created_at": request_refund.created_at,
                    "note": request_refund.note,
                }
                for request_refund in wallet.refund_requests(owner)
            ],
            "payments_enabled": container.payments is not None,
            "message": _MESSAGES.get(message, ""),
        },
    )


@router.post("/wallet/topup")
async def topup(
    request: Request,
    container: ContainerDep,
    session: CurrentSessionDep,
    amount_cents: int = Form(...),
) -> RedirectResponse:
    """Send the visitor to Stripe's hosted page for one closed-set amount."""
    owner = _owner(session)
    if container.payments is None:
        return _back("payments_off")
    if amount_cents not in FUNDING_AMOUNTS_CENTS:
        # The amounts are a closed set, exactly like every other setting the
        # visitor can choose: no free-text amount reaches Stripe.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="That is not one of the top-up amounts this app offers.",
        )
    success_url = str(request.url_for("wallet_return")) + "?session_id={CHECKOUT_SESSION_ID}"
    cancel_url = str(request.url_for("wallet_page")) + "?message=cancelled"
    try:
        checkout = await asyncio.to_thread(
            container.payments.create_checkout,
            amount_cents=amount_cents,
            currency=CURRENCY,
            success_url=success_url,
            cancel_url=cancel_url,
            reference=owner,
        )
    except PaymentError:
        # Stripe's own sentence goes to the operator's log, not the visitor.
        logger.warning("wallet.topup_failed owner=%s cents=%d", owner, amount_cents)
        return _back("payment_failed")
    logger.info("wallet.topup_started owner=%s cents=%d", owner, amount_cents)
    return RedirectResponse(checkout.url, status_code=status.HTTP_303_SEE_OTHER)


@router.get("/wallet/return", name="wallet_return")
async def wallet_return(
    container: ContainerDep,
    session: CurrentSessionDep,
    session_id: str = "",
) -> RedirectResponse:
    """Credit a paid Stripe session, once, after checking with Stripe.

    The amount must be one of the top-up amounts: a session created elsewhere
    — even a genuinely paid one — is not a top-up this app offers, and is
    logged rather than credited.
    """
    owner = _owner(session)
    if container.payments is None:
        return _back("payments_off")
    if not _SESSION_ID.fullmatch(session_id):
        return _back("payment_failed")
    try:
        checkout = await asyncio.to_thread(container.payments.read_checkout, session_id)
    except PaymentError:
        logger.warning("wallet.return_unreadable owner=%s", owner)
        return _back("payment_failed")
    if (
        not checkout.paid
        or checkout.currency != CURRENCY
        or checkout.amount_cents not in FUNDING_AMOUNTS_CENTS
        or not checkout.payment_intent
        # The session must be the one this account asked for: `reference` is
        # the `client_reference_id` we set when creating it, echoed by Stripe.
        # Without this, a paid session id that leaked would credit whoever
        # pasted it into their own return URL.
        or checkout.reference != owner
    ):
        logger.warning(
            "wallet.return_refused owner=%s paid=%s cents=%d currency=%s reference=%s",
            owner,
            checkout.paid,
            checkout.amount_cents,
            checkout.currency,
            "match" if checkout.reference == owner else "mismatch",
        )
        return _back("payment_failed")
    funded, created = await asyncio.to_thread(
        container.wallet.fund,
        owner,
        amount_cents=checkout.amount_cents,
        session_id=checkout.id,
        payment_id=checkout.payment_intent,
        description=f"Top-up — {format_cents(checkout.amount_cents)}",
    )
    logger.info(
        "wallet.return owner=%s cents=%d session=%s outcome=%s",
        owner,
        funded.amount_cents,
        checkout.id,
        "credited" if created else "already_credited",
    )
    return _back("funded")


@router.post("/wallet/refund")
async def refund(container: ContainerDep, session: CurrentSessionDep) -> RedirectResponse:
    """Take everything refundable out, and send it back through Stripe.

    The request debits the wallet immediately, so the same money cannot be
    requested twice; processing then sends it through Stripe. A failed send
    puts the money back and says so — a failed refund costs the visitor
    nothing.
    """
    owner = _owner(session)
    if container.payments is None:
        return _back("payments_off")
    try:
        request_refund = await asyncio.to_thread(container.wallet.request_refund, owner)
    except WalletError:
        return _back("nothing_to_refund")
    result = await asyncio.to_thread(
        container.wallet.process_refund, owner, request_refund.id, container.payments
    )
    return _back("refunded" if result.status == REFUND_PROCESSED else "refund_failed")


def cost_panel(container: Container, price_cents: int) -> dict[str, object]:
    """What a book costs to make, against what it is sold for.

    Summed from the ledger of every model call this deployment has made; see
    `app/services/costs.py`. The same for every visitor: it is the app's cost,
    not the account's.
    """
    summary = container.costs.summary()
    price_micro = price_cents * 10_000
    return {
        "books": summary.books_measured,
        "average": format_microusd(summary.average_book_microusd),
        "price": format_cents(price_cents),
        "margin": format_microusd(price_micro - summary.average_book_microusd),
        "share": round(100 * summary.average_book_microusd / price_micro)
        if price_micro
        else 0,
        "rows": [
            {
                "label": CATEGORY_LABELS[item.category],
                "calls": f"{item.calls_per_book:.1f}",
                "cost": format_microusd(item.cost_microusd),
            }
            for item in summary.per_book
        ],
        "total_calls": summary.total_calls,
        "total": format_microusd(summary.total_microusd),
        "overhead": format_microusd(summary.overhead_microusd),
        "failed_calls": summary.failed_calls,
        "unpriced_calls": summary.unpriced_calls,
    }


def _back(message: str) -> RedirectResponse:
    """Back to the wallet page, with one of the app's own sentences."""
    return RedirectResponse(
        f"/wallet?message={message}", status_code=status.HTTP_303_SEE_OTHER
    )
