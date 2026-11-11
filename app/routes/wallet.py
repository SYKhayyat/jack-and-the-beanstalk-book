"""The wallet page: fund it, read the ledger, ask for money back.

Four routes around one ledger:

    GET  /wallet                 -> balance, top-up buttons, history, refunds
    POST /wallet/topup           -> make a Stripe Checkout Session, redirect to it
    GET  /wallet/return          -> verify the paid session, then credit once
    POST /wallet/refund          -> record a refund request for the operator

Two rules shape the code:

* **Money is only credited from Stripe's own answer.** `session_id` comes back
  in the URL, and a visitor can type any value; the id is therefore read from
  Stripe with our secret key, and only a paid session in the closed set of
  top-up amounts is credited. The wallet's own `fund` is idempotent per
  session, so a refreshed success page cannot credit twice.
* **A failure is shown as a sentence this app wrote.** Stripe's error text is
  for the operator's log; the page gets one of a closed set of messages
  selected by a query parameter, so no wording from outside ever reaches it.

A top-up can start from any screen: the form carries a local `next` path, and
the Stripe success and cancel URLs bring the visitor back to exactly that page.
The path is only ever a path on this site; an absolute URL is discarded.
"""

from __future__ import annotations

import asyncio
import logging
import re
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit, urlunsplit

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
    KIND_FUNDING,
    KIND_PICTURE,
    KIND_PICTURE_REFUND,
    KIND_REFUND,
    KIND_REFUND_REVERSAL,
    REFUND_FAILED,
    REFUND_PROCESSED,
    REFUND_PROCESSING,
    REFUND_REQUESTED,
    WalletError,
    format_cents,
)

__all__ = ["cost_panel", "router"]

logger = logging.getLogger(__name__)

router = APIRouter(tags=["wallet"])

#: A Checkout Session id as Stripe writes it. The value arrives in a query
#: string, so it is validated before it is pasted into an outgoing API path.
_SESSION_ID = re.compile(r"cs_[A-Za-z0-9_]{1,200}")

#: The type chips above the ledger. A closed set, and the values are the
#: ledger's own kinds so a filter cannot invent one.
_KIND_FILTERS = (
    ("all", "All"),
    (KIND_FUNDING, "Top-ups"),
    (KIND_CHARGE, "Book fees"),
    (KIND_PICTURE, "Pictures"),
    (KIND_PICTURE_REFUND, "Returned pictures"),
    (KIND_REFUND, "Refunds"),
    (KIND_REFUND_REVERSAL, "Adjustments"),
)
_KIND_LABELS = dict(_KIND_FILTERS)

#: How many movements one page of the history shows. The page is narrowed and
#: paged by the store, so this is the read, not just the render (S5).
_HISTORY_PAGE_SIZE = 50

_REFUND_LABELS = {
    REFUND_REQUESTED: "Waiting for the operator",
    REFUND_PROCESSING: "Sending back to the card",
    REFUND_PROCESSED: "Sent back to the card",
    REFUND_FAILED: "Failed — the money is back in your wallet",
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
    request: Request,
    container: ContainerDep,
    session: CurrentSessionDep,
    kind: str = "",
    book: str = "",
    page: str = "",
) -> HTMLResponse:
    """The account's balance, the way to add to it, and every movement.

    The ledger is filtered and paged in one place: the URL carries `kind`,
    `book` and `page`, so a view can be refreshed, bookmarked or sent on, and
    a year-old account's wallet reads fifty rows instead of its whole ledger.
    """
    owner = _owner(session)
    templates: Jinja2Templates = request.app.state.templates
    wallet = container.wallet
    chrome = page_chrome(container, session, request)
    known_books = {entry.book_id: entry for entry in chrome["books"]}
    # The header has already read the balance for this request; reuse it
    # rather than paying for a second round trip on a database deployment.
    balance = int(chrome["wallet_balance_cents"])
    refundable = wallet.refundable_cents(owner) if container.payments else 0

    kind_filter = kind if kind in _KIND_LABELS else "all"
    book_ids = {_book_id(reference) for reference in wallet.history_books(owner)}
    book_ids.discard("")
    # A top-up or a refund is not tied to a book; ignore a book filter that a
    # hand-written URL pairs with one of those kinds.
    if kind_filter not in ("all", KIND_CHARGE, KIND_PICTURE, KIND_PICTURE_REFUND):
        book_filter = "all"
    else:
        book_filter = book if book in book_ids else "all"
    kinds = None if kind_filter == "all" else (kind_filter,)
    book_id = "" if book_filter == "all" else book_filter

    wanted = _page_number(page)
    offset = (wanted - 1) * _HISTORY_PAGE_SIZE
    entries, total = wallet.history_page(
        owner, kinds=kinds, book_id=book_id, limit=_HISTORY_PAGE_SIZE, offset=offset
    )
    pages = max(1, -(-total // _HISTORY_PAGE_SIZE))
    page_no = min(wanted, pages)
    if page_no != wanted:
        # A page past the end (an old link after rows were refunded) shows the
        # last page rather than an empty table.
        offset = (page_no - 1) * _HISTORY_PAGE_SIZE
        entries, total = wallet.history_page(
            owner, kinds=kinds, book_id=book_id, limit=_HISTORY_PAGE_SIZE, offset=offset
        )

    rows: list[dict[str, object]] = []
    for entry in entries:
        entry_book = _book_id(entry.reference)
        title = ""
        if entry_book:
            stored = known_books.get(entry_book)
            title = (stored.title if stored and stored.title else "") or _quoted_title(
                entry.description
            )
        rows.append(
            {
                "description": entry.description,
                "kind": entry.kind,
                "kind_label": _KIND_LABELS.get(entry.kind, entry.kind),
                "amount_label": format_cents(entry.amount_cents),
                "credit": entry.amount_cents > 0,
                "created_at": entry.created_at,
                "book_id": entry_book,
                "book_title": title,
                "book_href": f"/books/{entry_book}" if entry_book in known_books else "",
            }
        )

    book_options = [
        (
            candidate,
            (known_books[candidate].title or "Your book")
            if candidate in known_books
            else "Your book",
        )
        for candidate in sorted(book_ids)
    ]
    filters_active = kind_filter != "all" or book_filter != "all"

    return templates.TemplateResponse(
        request=request,
        name="wallet.html",
        context={
            **chrome,
            "balance_cents": balance,
            "balance_label": format_cents(balance),
            "price_label": format_cents(wallet.price_cents) if container.payments else "",
            "short": container.payments is not None and balance < wallet.price_cents,
            "refundable_cents": refundable,
            "refundable_label": format_cents(refundable),
            "amounts": [(cents, format_cents(cents)) for cents in FUNDING_AMOUNTS_CENTS],
            "history": rows,
            "history_total": total,
            "page": page_no,
            "pages": pages,
            "newer_href": _filter_href(kind_filter, book_filter, page_no - 1)
            if page_no > 1
            else "",
            "older_href": _filter_href(kind_filter, book_filter, page_no + 1)
            if page_no < pages
            else "",
            "kind_filter": kind_filter,
            "book_filter": book_filter,
            "kind_filters": [
                (value, label, _filter_href(value, book_filter))
                for value, label in _KIND_FILTERS
            ],
            "book_options": [
                (candidate, title, _filter_href(kind_filter, candidate))
                for candidate, title in book_options
            ],
            # A top-up or a refund is not tied to a book; only offer the book
            # filter where it can actually change the list.
            "book_filter_relevant": kind_filter
            in ("all", KIND_CHARGE, KIND_PICTURE, KIND_PICTURE_REFUND),
            "filters_active": filters_active,
            "clear_href": _filter_href("all", "all"),
            "refunds": [
                {
                    "amount_label": format_cents(request_refund.amount_cents),
                    "status": request_refund.status,
                    "status_label": _REFUND_LABELS.get(
                        request_refund.status, request_refund.status
                    ),
                    "pending": request_refund.status == REFUND_REQUESTED,
                    "created_at": request_refund.created_at,
                    "note": request_refund.note,
                }
                for request_refund in wallet.refund_requests(owner)
            ],
            "payments_enabled": container.payments is not None,
        },
    )


@router.post("/wallet/topup")
async def topup(
    request: Request,
    container: ContainerDep,
    session: CurrentSessionDep,
    amount_cents: int = Form(...),
    next: str = Form("/wallet"),
) -> RedirectResponse:
    """Send the visitor to Stripe's hosted page for one closed-set amount.

    `next` is the screen to come back to. It is validated here and again when
    the visitor returns, because a query string is visitor-controlled.
    """
    owner = _owner(session)
    target = _safe_next(next)
    if container.payments is None:
        return _back("payments_off", target)
    if amount_cents not in FUNDING_AMOUNTS_CENTS:
        # The amounts are a closed set, exactly like every other setting the
        # visitor can choose: no free-text amount reaches Stripe.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="That is not one of the top-up amounts this app offers.",
        )
    # The braces must survive: Stripe replaces {CHECKOUT_SESSION_ID} itself.
    success_url = (
        str(request.url_for("wallet_return"))
        + "?session_id={CHECKOUT_SESSION_ID}&"
        + urlencode({"next": target})
    )
    cancel_url = _absolute_page(request, _with_message(target, "cancelled"))
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
        return _back("payment_failed", target)
    logger.info("wallet.topup_started owner=%s cents=%d", owner, amount_cents)
    return RedirectResponse(checkout.url, status_code=status.HTTP_303_SEE_OTHER)


@router.get("/wallet/return", name="wallet_return")
async def wallet_return(
    container: ContainerDep,
    session: CurrentSessionDep,
    session_id: str = "",
    next: str = "",
) -> RedirectResponse:
    """Credit a paid Stripe session, once, after checking with Stripe.

    The amount must be one of the top-up amounts: a session created elsewhere
    — even a genuinely paid one — is not a top-up this app offers, and is
    logged rather than credited.
    """
    owner = _owner(session)
    target = _safe_next(next)
    if container.payments is None:
        return _back("payments_off", target)
    if not _SESSION_ID.fullmatch(session_id):
        return _back("payment_failed", target)
    try:
        checkout = await asyncio.to_thread(container.payments.read_checkout, session_id)
    except PaymentError:
        logger.warning("wallet.return_unreadable owner=%s", owner)
        return _back("payment_failed", target)
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
        return _back("payment_failed", target)
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
    return _back("funded", target)


@router.post("/wallet/refund")
async def refund(container: ContainerDep, session: CurrentSessionDep) -> RedirectResponse:
    """Record a request for everything refundable, for the operator to send.

    The request debits the wallet immediately, so the same money cannot be
    asked for twice while it waits. The operator processes refunds from
    /admin/status; a failed send puts the money back (see `Wallet.process_refund`).
    """
    owner = _owner(session)
    if container.payments is None:
        return _back("payments_off")
    try:
        request_refund = await asyncio.to_thread(container.wallet.request_refund, owner)
    except WalletError:
        return _back("nothing_to_refund")
    logger.info(
        "wallet.refund_requested owner=%s cents=%d id=%s",
        owner,
        request_refund.amount_cents,
        request_refund.id,
    )
    return _back("refund_requested")


def cost_panel(container: Container, price_cents: int) -> dict[str, object]:
    """What a book costs to make, against what it is sold for.

    Summed from the ledger of every model call this deployment has made; see
    `app/services/costs.py`. Operator-only: the admin status page is the only
    caller, because a visitor has no use for the app's margin.
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


# --- filters and return paths --------------------------------------------------


def _filter_href(kind: str, book: str, page: int = 1) -> str:
    """A wallet URL carrying the chosen filters and page; no empty defaults."""
    query: list[tuple[str, str]] = []
    if kind != "all":
        query.append(("kind", kind))
    if book != "all":
        query.append(("book", book))
    if page > 1:
        query.append(("page", str(page)))
    return "/wallet?" + urlencode(query) if query else "/wallet"


def _page_number(raw: str) -> int:
    """A page of the history: anything that is not a positive whole number is 1.

    A mangled or hand-typed `page` shows the first page rather than a 422 on a
    customer's screen.
    """
    return int(raw) if raw.isdigit() and int(raw) > 0 else 1


def _book_id(reference: str | None) -> str:
    """The book id behind a ledger entry, or an empty string.

    A book fee names it as `book:<id>`, a picture bought for a book as
    `picture:<id>:<job>:<n>`, so the history can link either back to its book.
    """
    if not reference:
        return ""
    if reference.startswith("book:"):
        return reference.removeprefix("book:")
    if reference.startswith("picture:"):
        return reference.removeprefix("picture:").split(":", 1)[0]
    return ""


def _quoted_title(description: str) -> str:
    """The title a book charge wrote into its description."""
    quoted = description.split('"')
    return quoted[1] if len(quoted) >= 3 and quoted[1] else ""


def _safe_next(raw: str) -> str:
    """A local path, or /wallet.

    The value is visitor-controlled, so it may never become an open redirect.
    Only a same-site absolute path is kept; a scheme, a host, a backslash or a
    control character sends the visitor to the wallet instead.
    """
    candidate = (raw or "").strip()
    if not candidate or len(candidate) > 500:
        return "/wallet"
    if "\\" in candidate or any(ord(char) < 32 or ord(char) == 127 for char in candidate):
        return "/wallet"
    parts = urlsplit(candidate)
    if parts.scheme or parts.netloc or not parts.path.startswith("/"):
        return "/wallet"
    # Reject `//host` in any spelling: a browser may decode `%2f` into a
    # path separator, which would turn a local-looking path into another site.
    decoded = unquote(parts.path)
    if decoded.startswith("//") or "\\" in decoded:
        return "/wallet"
    return candidate


def _with_message(path: str, message: str) -> str:
    """The same local path with one of the app's message keys in the query."""
    parts = urlsplit(_safe_next(path))
    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key != "message"
    ]
    query.append(("message", message))
    return urlunsplit(("", "", parts.path, urlencode(query), parts.fragment))


def _absolute_page(request: Request, path: str) -> str:
    """A local path as the absolute URL Stripe needs for a cancel return."""
    return str(request.base_url).rstrip("/") + _safe_next(path)


def _back(message: str, next_path: str = "/wallet") -> RedirectResponse:
    """Back to the page the visitor came from, with one of the app's sentences."""
    return RedirectResponse(
        _with_message(next_path, message), status_code=status.HTTP_303_SEE_OTHER
    )
