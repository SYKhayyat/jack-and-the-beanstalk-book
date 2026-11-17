"""Order a printed copy: review, choose, address, quote, place — in a sandbox.

The order of these steps is the feature:

1. The owner opens the finished PDF, picks a binding (the size is the book's
   own, chosen in the viewer) and
   presses "I've reviewed this book". The approval records the web PDF's
   hash and builds the print interior and cover in the background, storing
   both (A1, A3); the page shows them being made until they are.
   If the book is edited later the hash no longer matches and the page asks
   for a new review.
2. The delivery address is priced by the print provider itself, so the buyer
   sees the edition's price plus shipping at cost before paying (A6).
3. Placing the order charges the wallet for that total and sends the job.
   A clear refusal is refunded at once; a lost answer leaves the order
   `unknown` until it is looked up (A4).

The printer fetches the two files from signed one-hour links, which serve the
stored bytes only while their hash is the approved one. The provider is the
mock or Lulu's sandbox; see `app/services/print/lulu.py` for why there is no
live host in this code.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from typing import Any, cast

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.dependencies import ContainerDep
from app.models.book import BookFormat
from app.routes.auth import CurrentSessionDep
from app.routes.book_access import ensure_book, no_store
from app.routes.wallet import start_topup
from app.routes.web import page_chrome
from app.services.book_store import BookStatus
from app.services.print.base import (
    COUNTRIES,
    PrintError,
    PrintOrder,
    ShippingAddress,
)
from app.services.print.products import PRODUCTS, product_by_key, product_for
from app.services.share_links import issue, read
from app.services.typography import book_text
from app.services.wallet import InsufficientFunds, format_cents, topup_for_shortfall

__all__ = ["router"]

logger = logging.getLogger(__name__)

router = APIRouter()

#: A provider fetches the files within minutes of accepting the job. One hour
#: is generous for a retry and short enough that a link is not an archive.
_FILE_LINK_SECONDS = 60 * 60
_FILE_KINDS = ("interior", "cover")


def _owner(session: CurrentSessionDep) -> str:
    """A print order belongs to a person: there is no anonymous shipping."""
    if session is None or not session.user_id:
        raise HTTPException(
            status_code=404,
            detail=(
                "Printed copies need a signed-in account, because there is a "
                "book and an address to own."
            ),
        )
    return session.user_id


def _render(request: Request, name: str, context: dict[str, Any]) -> HTMLResponse:
    templates = request.app.state.templates
    return cast(
        HTMLResponse, templates.TemplateResponse(request=request, name=name, context=context)
    )


def _complete_book(book_id: str, owner: str, container: ContainerDep) -> Any:
    """The record of an owner's finished book."""
    ensure_book(book_id, owner, container)
    record = container.book_store.load_record(book_id)
    status = container.book_store.load_status(book_id) or {}
    if record is None:
        raise HTTPException(status_code=404, detail="No book with that id.")
    if status.get("status") != BookStatus.COMPLETE:
        raise HTTPException(
            status_code=409,
            detail="A book can be printed once it is finished.",
        )
    return record


def _editions(book_format: BookFormat) -> list[dict[str, Any]]:
    """The editions of a book in its size: the viewer picks the size (B-size)."""
    return [
        {
            "key": product.key,
            "trim": str(product.trim),
            "binding": str(product.binding),
            "label": product.label,
            "price_label": format_cents(product.retail_cents),
        }
        for product in PRODUCTS.values()
        if product.trim is book_format
    ]


def _print_context(
    request: Request,
    container: ContainerDep,
    session: CurrentSessionDep,
    book_id: str,
    *,
    error: str = "",
    form: dict[str, str] | None = None,
) -> dict[str, Any]:
    owner = _owner(session)
    record = _complete_book(book_id, owner, container)
    service = container.print_service
    current_pdf = container.book_store.read_pdf(book_id) or b""
    current_sha = hashlib.sha256(current_pdf).hexdigest()
    approval = service.approval(book_id)
    latest = service.latest(book_id)
    approved = approval is not None and approval.pdf_sha256 == current_sha
    preparing = None if approved else container.print_preparer.state(book_id, current_sha)
    if preparing is not None and preparing.error and not error:
        # A build that failed in the background: the reason, then the button.
        error = preparing.error
    order = approval if approved else None
    quoted = order is not None and order.quoted_at is not None
    total = order.total_cents if order is not None and quoted else 0
    shortfall = _shortfall(container, owner, total) if quoted else 0
    product = product_by_key(order.product_key) if order is not None else None
    heading, *sentences = (book_text(line) for line in record.about.rendered_about_lines())
    return {
        **page_chrome(container, session, request),
        "current_id": book_id,
        "book": record,
        "title": record.title,
        "about_heading": heading,
        "about_sentences": sentences,
        "editions": _editions(record.book_format),
        "format_label": record.book_format.label,
        "edition_label": product.label if product is not None else "",
        "page_count": order.page_count if order is not None else 0,
        "price_label": format_cents(order.retail_cents) if order is not None else "",
        "shipping_label": format_cents(order.shipping_cents) if quoted and order else "",
        "total_label": format_cents(total) if quoted else "",
        "quoted": quoted,
        # A copy costs more than the largest preset top-up, so the page offers
        # the difference in one trip to Stripe instead (A7).
        "can_top_up": container.payments is not None and shortfall > 0,
        "balance_label": format_cents(container.wallet.balance_cents(owner)),
        "topup_label": format_cents(shortfall),
        "service_enabled": service.enabled,
        "provider_name": service.provider_name,
        "provider_mode": service.mode,
        "approved": approved,
        "preparing": preparing is not None and preparing.running,
        "approval": order,
        "latest": latest,
        "submitted": latest if latest is not None and latest.state == "submitted" else None,
        "unknown": latest if latest is not None and latest.state == "unknown" else None,
        # A request that charged and then died mid-call leaves `placing`; the
        # same button resumes it once the claim has gone stale.
        "placing": latest if latest is not None and latest.state == "placing" else None,
        # What this order actually cost, not what a copy costs today: the
        # receipt must not change when the operator changes the price.
        "paid_label": format_cents(latest.total_cents) if latest is not None else "",
        "pdf_url": f"/book-media/{book_id}/pdf",
        "pdf_sha": current_sha,
        "countries": COUNTRIES,
        "error": error,
        "form": form or _prefill(session, order, latest),
    }


def _shortfall(container: ContainerDep, owner: str, price_cents: int) -> int:
    """The top-up that lets the wallet pay `price_cents`; 0 when none is offered.

    0 when the wallet already can, and also when the gap is beyond one top-up:
    the order is then refused for funds, as it would be without this offer.
    """
    try:
        return topup_for_shortfall(price_cents - container.wallet.balance_cents(owner))
    except ValueError:
        logger.warning("print.topup_over_limit owner=%s price=%d", owner, price_cents)
        return 0


def _prefill(
    session: CurrentSessionDep, order: PrintOrder | None, latest: PrintOrder | None
) -> dict[str, str]:
    """The delivery form's starting values: this quote, last order, the account."""
    for source in (order, latest):
        if source is not None and source.address:
            return {key: str(value) for key, value in source.address.items()}
    return {
        "name": (session.name if session else "") or "",
        "email": (session.email if session else "") or "",
        "phone": "",
        "country_code": "US",
        "street1": "",
        "street2": "",
        "city": "",
        "state": "",
        "postcode": "",
    }


async def _page(
    request: Request,
    container: ContainerDep,
    session: CurrentSessionDep,
    book_id: str,
    *,
    error: str = "",
    form: dict[str, str] | None = None,
) -> HTMLResponse:
    context = await asyncio.to_thread(
        lambda: _print_context(request, container, session, book_id, error=error, form=form)
    )
    return _render(request, "print.html", context)


@router.get("/books/{book_id}/print", response_class=HTMLResponse)
def print_page(
    book_id: str, request: Request, container: ContainerDep, session: CurrentSessionDep
) -> HTMLResponse:
    """Review and choose, then the address and its price; or the order's receipt."""
    return _render(request, "print.html", _print_context(request, container, session, book_id))


@router.post("/books/{book_id}/print/approve")
async def approve_print(
    book_id: str,
    request: Request,
    container: ContainerDep,
    session: CurrentSessionDep,
    binding: str = Form("hardcover", max_length=16),
) -> Response:
    """Record that the owner has read this book; its print files build in the background.

    The size is the book's own, the one it is read in: printing never asks for
    another, so the copy is the book that was reviewed.
    """
    owner = _owner(session)
    record = await asyncio.to_thread(_complete_book, book_id, owner, container)
    try:
        product = product_for(record.book_format, binding)
    except ValueError:
        return await _page(
            request, container, session, book_id, error="Please choose one of the editions."
        )
    pdf = await asyncio.to_thread(container.book_store.read_pdf, book_id)
    if pdf is None:
        raise HTTPException(status_code=409, detail="The book has no PDF yet.")
    runner = container.book_runner
    service = container.print_service
    # The interior and cover take most of a minute on the free plan, so they
    # are built in the background: the page shows them being made and
    # refreshes until the approval is stored, or shows why it could not be.
    # A second press while one build runs joins it instead of starting another.
    container.print_preparer.start(
        book_id,
        pdf=pdf,
        product_key=product.key,
        build=lambda: service.approve(
            book_id=book_id,
            user_id=owner,
            pdf=pdf,
            product=product,
            build_interior=lambda chosen: runner.print_interior(book_id, chosen),
            build_cover=lambda chosen, size: runner.print_cover(book_id, chosen, size),
        ),
    )
    return RedirectResponse(url=f"/books/{book_id}/print", status_code=303)


def _address_form(
    name: str,
    email: str,
    phone: str,
    street1: str,
    street2: str,
    city: str,
    state: str,
    postcode: str,
    country_code: str,
) -> dict[str, str]:
    return {
        "name": name,
        "email": email,
        "phone": phone,
        "street1": street1,
        "street2": street2,
        "city": city,
        "state": state,
        "postcode": postcode,
        "country_code": country_code,
    }


@router.post("/books/{book_id}/print/quote")
async def quote_print(
    book_id: str,
    request: Request,
    container: ContainerDep,
    session: CurrentSessionDep,
    order_id: str = Form(""),
    name: str = Form("", max_length=35),
    email: str = Form("", max_length=320),
    phone: str = Form("", max_length=20),
    street1: str = Form("", max_length=200),
    street2: str = Form("", max_length=200),
    city: str = Form("", max_length=30),
    state: str = Form("", max_length=100),
    postcode: str = Form("", max_length=32),
    country_code: str = Form("", max_length=2),
) -> Response:
    """Price this copy to this address with the print provider (A6)."""
    owner = _owner(session)
    await asyncio.to_thread(_complete_book, book_id, owner, container)
    form = _address_form(
        name, email, phone, street1, street2, city, state, postcode, country_code
    )
    service = container.print_service
    approval = await asyncio.to_thread(service.approval, book_id)
    if approval is None or approval.id != order_id:
        return await _page(
            request,
            container,
            session,
            book_id,
            error="Please review the book and choose an edition before ordering.",
            form=form,
        )
    address = ShippingAddress.from_dict(form)
    try:
        await asyncio.to_thread(service.quote, approval, address)
    except PrintError as exc:
        return await _page(request, container, session, book_id, error=str(exc), form=form)
    return RedirectResponse(url=f"/books/{book_id}/print?quoted=1", status_code=303)


@router.post("/books/{book_id}/print/topup")
async def top_up_for_print(
    book_id: str, request: Request, container: ContainerDep, session: CurrentSessionDep
) -> Response:
    """Add exactly what this copy still needs to the wallet, then come back.

    The amount is worked out here from the quoted total and the balance,
    never read from the form, so the visitor cannot choose what reaches Stripe.
    """
    owner = _owner(session)
    await asyncio.to_thread(_complete_book, book_id, owner, container)
    approval = await asyncio.to_thread(container.print_service.approval, book_id)
    target = f"/books/{book_id}/print"
    if approval is None or approval.quoted_at is None:
        return RedirectResponse(url=target, status_code=303)
    amount = await asyncio.to_thread(_shortfall, container, owner, approval.total_cents)
    if amount <= 0:
        return RedirectResponse(url=target, status_code=303)
    return await start_topup(request, container, owner, amount, target)


@router.post("/books/{book_id}/print/order")
async def place_print_order(
    book_id: str,
    request: Request,
    container: ContainerDep,
    session: CurrentSessionDep,
    order_id: str = Form(""),
) -> Response:
    """Charge the quoted total and place the sandbox print job.

    Also retries an order whose outcome is `unknown`: it is looked up at the
    printer by its id before anything is sent again.
    """
    owner = _owner(session)
    record = await asyncio.to_thread(_complete_book, book_id, owner, container)
    service = container.print_service
    if not service.enabled:
        return await _page(
            request,
            container,
            session,
            book_id,
            error=(
                "Printed copies are not available on this deployment: printing "
                "needs a configured print service and a wallet."
            ),
        )
    order = await asyncio.to_thread(service.get, order_id) if order_id else None
    if order is None or order.book_id != book_id or order.user_id != owner:
        return await _page(
            request,
            container,
            session,
            book_id,
            error="Please review the book and choose an edition before ordering.",
        )
    pdf = await asyncio.to_thread(container.book_store.read_pdf, book_id) or b""
    try:
        await asyncio.to_thread(
            lambda: service.place(
                order=order,
                pdf=pdf,
                file_urls=lambda placed: _file_urls(request, container, placed),
                title=record.title,
                about=record.about,
            )
        )
    except (PrintError, InsufficientFunds) as exc:
        return await _page(request, container, session, book_id, error=str(exc))
    return RedirectResponse(url=f"/books/{book_id}/print?placed=1", status_code=303)


@router.post("/books/{book_id}/print/cancel")
async def cancel_print_order(
    book_id: str,
    request: Request,
    container: ContainerDep,
    session: CurrentSessionDep,
    order_id: str = Form(""),
) -> Response:
    """The buyer's refund request for an order whose outcome is unknown (A4)."""
    owner = _owner(session)
    await asyncio.to_thread(_complete_book, book_id, owner, container)
    service = container.print_service
    order = await asyncio.to_thread(service.get, order_id) if order_id else None
    if order is None or order.book_id != book_id or order.user_id != owner:
        return await _page(request, container, session, book_id, error="No such print order.")
    try:
        await asyncio.to_thread(service.cancel_unknown, order)
    except PrintError as exc:
        return await _page(request, container, session, book_id, error=str(exc))
    return RedirectResponse(url=f"/books/{book_id}/print?checked=1", status_code=303)


def _file_urls(
    request: Request, container: ContainerDep, order: PrintOrder
) -> tuple[str, str]:
    """The signed, short-lived addresses the printer fetches the two files from.

    A provider needs public URLs, and an order's files are normally behind the
    owner's session. Each token names one order, one file and the hash it was
    approved with, for one hour; the route serves nothing else (A3).
    """
    key = container.settings.session_signing_key()
    expires = int(time.time()) + _FILE_LINK_SECONDS
    base = container.settings.public_site_url.strip() or str(request.base_url).rstrip("/")

    def link(kind: str, sha256: str) -> str:
        token = issue(
            f"{order.id}/{kind}/{sha256}", key=key, expires_at=expires, scope="print-file"
        )
        return f"{base}/print/files/{token}.pdf"

    return link("interior", order.interior_sha256), link("cover", order.cover_sha256)


@router.get("/print/files/{token}.pdf")
async def print_file(token: str, container: ContainerDep) -> Response:
    """One approved print file, exactly as approved, with no session and no other data."""
    link = read(token, key=container.settings.session_signing_key(), scope="print-file")
    if link is None:
        raise HTTPException(status_code=404, detail="This print file link has expired.")
    order_id, _, rest = link.book_id.partition("/")
    kind, _, sha256 = rest.partition("/")
    if kind not in _FILE_KINDS or not sha256:
        raise HTTPException(status_code=404, detail="This print file link has expired.")
    data = await asyncio.to_thread(container.print_service.read_file, order_id, kind, sha256)
    if data is None:
        raise HTTPException(status_code=404, detail="This print file is no longer available.")
    logger.info("print.file_served order=%s kind=%s bytes=%d", order_id, kind, len(data))
    return no_store(data, media_type="application/pdf", filename=f"{kind}.pdf")
