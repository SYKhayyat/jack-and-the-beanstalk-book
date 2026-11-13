"""Order a printed copy: review, approve, address, place — in test mode.

The order of these steps is the feature. The owner opens the finished PDF,
presses "I've reviewed this PDF", and only then does the delivery form appear.
The approval records the PDF's hash; if the book is edited in the meantime the
hash no longer matches, the form disappears again and the page asks for a new
review. Nothing reaches the print provider before a matching approval exists.

The copy is charged through the wallet before the provider is called, and the
charge is reversed if the provider refuses. The provider itself is the mock or
Peecho's test environment; see `app/services/print/peecho.py` for why there is
no live host in this code.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from functools import partial
from typing import Any, cast

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.dependencies import ContainerDep
from app.routes.auth import CurrentSessionDep
from app.routes.book_access import book_edition, ensure_book, no_store
from app.routes.web import page_chrome
from app.services.book_store import BookStatus
from app.services.pdf import BOOK_MATTER_PAGES
from app.services.print.base import COUNTRIES, PrintError, ShippingAddress
from app.services.share_links import issue, read
from app.services.typography import book_text
from app.services.wallet import InsufficientFunds, format_cents

__all__ = ["router"]

logger = logging.getLogger(__name__)

router = APIRouter()

#: A provider fetches the PDF within seconds of accepting the job. One hour is
#: generous for a retry and short enough that the link is not an archive.
_FILE_LINK_SECONDS = 60 * 60


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


def _complete_book(
    book_id: str, owner: str, container: ContainerDep
) -> tuple[Any, dict[str, object], Any]:
    """The record, status and edition of an owner's finished book."""
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
    return record, status, book_edition(container, book_id)


def _page_count(edition: Any) -> int:
    """Every page Peecho will print: cover, copyright, story, The End, back cover."""
    return len(edition.pages) + BOOK_MATTER_PAGES


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
    record, _status, edition = _complete_book(book_id, owner, container)
    service = container.print_service
    pages = _page_count(edition)
    current_pdf = container.book_store.read_pdf(book_id) or b""
    if current_pdf:
        try:
            quote = service.quote_for_pdf(current_pdf)
        except PrintError:
            # A PDF nobody can read cannot be approved either; show the
            # edition's own price so the page still explains itself.
            quote = service.quote(pages)
    else:
        quote = service.quote(pages)
    current_sha = hashlib.sha256(current_pdf).hexdigest()
    approval = service.approval(book_id)
    latest = service.latest(book_id)
    approved = approval is not None and approval.pdf_sha256 == current_sha
    heading, *sentences = (book_text(line) for line in record.about.rendered_about_lines())
    return {
        **page_chrome(container, session, request),
        "current_id": book_id,
        "book": record,
        "title": record.title,
        "about_heading": heading,
        "about_sentences": sentences,
        "page_count": quote.page_count,
        "price_label": format_cents(quote.retail_cents),
        "quote": quote,
        "service_enabled": service.enabled,
        "provider_name": service.provider_name,
        "provider_mode": service.mode,
        "approved": approved,
        "approval": approval if approved else None,
        "latest": latest,
        "submitted": latest if latest is not None and latest.state == "submitted" else None,
        # What this order actually cost, not what a copy costs today: the
        # receipt must not change when the operator changes the price.
        "paid_label": format_cents(latest.retail_cents) if latest is not None else "",
        "pdf_url": f"/book-media/{book_id}/pdf",
        "pdf_sha": current_sha,
        "countries": COUNTRIES,
        "error": error,
        "form": form or _prefill(session, latest),
    }


def _prefill(session: CurrentSessionDep, latest: Any) -> dict[str, str]:
    """The delivery form's starting values: last order, then the account."""
    if latest is not None and latest.address:
        return {key: str(value) for key, value in latest.address.items()}
    return {
        "name": (session.name if session else "") or "",
        "email": (session.email if session else "") or "",
        "country_code": "US",
        "street1": "",
        "street2": "",
        "city": "",
        "state": "",
        "postcode": "",
    }


@router.get("/books/{book_id}/print", response_class=HTMLResponse)
def print_page(
    book_id: str, request: Request, container: ContainerDep, session: CurrentSessionDep
) -> HTMLResponse:
    """Review the PDF, then the delivery form; or the test order's receipt."""
    return _render(request, "print.html", _print_context(request, container, session, book_id))


@router.post("/books/{book_id}/print/approve")
async def approve_print(
    book_id: str,
    request: Request,
    container: ContainerDep,
    session: CurrentSessionDep,
) -> Response:
    """Record that the owner has read this exact PDF. Nothing is sent yet."""
    owner = _owner(session)
    _record, _status, edition = await asyncio.to_thread(
        _complete_book, book_id, owner, container
    )
    pdf = await asyncio.to_thread(container.book_store.read_pdf, book_id)
    if pdf is None:
        raise HTTPException(status_code=409, detail="The book has no PDF yet.")
    try:
        await asyncio.to_thread(
            lambda: container.print_service.approve(
                book_id=book_id,
                user_id=owner,
                pdf=pdf,
                page_count=_page_count(edition),
            )
        )
    except PrintError as exc:
        # Most likely a PDF that cannot be measured. Keep the owner on the
        # print page with the reason, instead of a bare 500.
        return _render(
            request,
            "print.html",
            await asyncio.to_thread(
                partial(_print_context, request, container, session, book_id, error=str(exc))
            ),
        )
    return RedirectResponse(url=f"/books/{book_id}/print?approved=1", status_code=303)


@router.post("/books/{book_id}/print/order")
async def place_print_order(
    book_id: str,
    request: Request,
    container: ContainerDep,
    session: CurrentSessionDep,
    order_id: str = Form(""),
    name: str = Form("", max_length=120),
    email: str = Form("", max_length=320),
    street1: str = Form("", max_length=200),
    street2: str = Form("", max_length=200),
    city: str = Form("", max_length=100),
    state: str = Form("", max_length=100),
    postcode: str = Form("", max_length=32),
    country_code: str = Form("", max_length=2),
) -> Response:
    """Charge for one copy and place the test print job.

    Every failure is shown on the page with the address still filled in, so a
    mistyped postal code does not cost the visitor the whole form.
    """
    owner = _owner(session)
    record, _status, _edition = await asyncio.to_thread(
        _complete_book, book_id, owner, container
    )
    service = container.print_service
    approval = await asyncio.to_thread(service.approval, book_id)
    form = {
        "name": name,
        "email": email,
        "street1": street1,
        "street2": street2,
        "city": city,
        "state": state,
        "postcode": postcode,
        "country_code": country_code,
    }
    if approval is None or approval.id != order_id:
        return _render(
            request,
            "print.html",
            await asyncio.to_thread(
                lambda: _print_context(
                    request,
                    container,
                    session,
                    book_id,
                    error="Please review the PDF and approve this book before ordering.",
                    form=form,
                )
            ),
        )
    pdf = await asyncio.to_thread(container.book_store.read_pdf, book_id)
    if pdf is None:
        raise HTTPException(status_code=409, detail="The book has no PDF yet.")
    address = ShippingAddress(
        name=name,
        street1=street1,
        street2=street2,
        city=city,
        state=state,
        postcode=postcode,
        country_code=country_code,
        email=email,
    )
    if not service.enabled:
        return _render(
            request,
            "print.html",
            await asyncio.to_thread(
                lambda: _print_context(
                    request,
                    container,
                    session,
                    book_id,
                    error=(
                        "Printed copies are not available on this deployment: printing "
                        "needs a configured print service and a wallet."
                    ),
                    form=form,
                )
            ),
        )
    try:
        await asyncio.to_thread(
            lambda: service.place(
                order=approval,
                pdf=pdf,
                pdf_url=_file_url(request, container, book_id),
                address=address,
                about=record.about,
            )
        )
    except (PrintError, InsufficientFunds) as exc:
        return _render(
            request,
            "print.html",
            await asyncio.to_thread(
                partial(
                    _print_context,
                    request,
                    container,
                    session,
                    book_id,
                    error=str(exc),
                    form=form,
                )
            ),
        )
    return RedirectResponse(url=f"/books/{book_id}/print?placed=1", status_code=303)


def _file_url(request: Request, container: ContainerDep, book_id: str) -> str:
    """The signed, short-lived address the print service fetches the PDF from.

    A provider needs a public URL, and a book PDF is normally behind the
    owner's session. The token grants exactly one file for one hour; the route
    it points at serves only the PDF, never a page, the photo or the sources.
    """
    token = issue(
        book_id,
        key=container.settings.session_signing_key(),
        expires_at=int(time.time()) + _FILE_LINK_SECONDS,
        scope="print-file",
    )
    base = container.settings.public_site_url.strip() or str(request.base_url).rstrip("/")
    return f"{base}/print/files/{token}.pdf"


@router.get("/print/files/{token}.pdf")
async def print_file(token: str, container: ContainerDep) -> Response:
    """The PDF a test print provider fetches, with no session and no other data."""
    link = read(token, key=container.settings.session_signing_key(), scope="print-file")
    if link is None:
        raise HTTPException(status_code=404, detail="This print file link has expired.")
    data = await asyncio.to_thread(container.book_store.read_pdf, link.book_id)
    if data is None:
        raise HTTPException(status_code=404, detail="This book has no PDF any more.")
    logger.info("print.file_served book=%s bytes=%d", link.book_id, len(data))
    return no_store(data, media_type="application/pdf", filename="picture-book.pdf")
