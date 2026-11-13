"""The read-only face of a book: `/share/{token}` and the pictures behind it (W3).

A share link has to work for somebody with no account, no session and no
wallet, and it must give away nothing but the finished book. That is why these
routes are their own module rather than a flag on the reader's: every one of
them takes a *token* instead of a session, none of them is reachable with an
owner's cookie, and none of them touches an edit, a delete, a payment or the
uploaded photograph.

What a token holder gets is what a grandparent needs: the cover, every page
with its words, the About page, and the PDF. Nothing else — the sources ZIP,
the prompts and the original photo stay behind the owner's session, and a link
stops working when the book itself is swept (`SHARE` expires with retention).
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from typing import Any, cast

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from app.dependencies import ContainerDep
from app.routes.auth import CurrentSessionDep
from app.routes.book_access import book_edition, ensure_book, no_store, private_cached, user_id
from app.routes.web import page_chrome
from app.services.book_store import BookStatus
from app.services.media import media_type_for
from app.services.prompts import render_book_page
from app.services.share_links import ShareLink, issue, read
from app.services.typography import book_text

__all__ = ["router"]

logger = logging.getLogger(__name__)

router = APIRouter()

#: How long a link lives, at most. The book's own retention is the other cap:
#: a link that outlived its book would only ever answer 404.
SHARE_LIFETIME_DAYS = 30


def _key(container: ContainerDep) -> bytes:
    """The deployment's signing key, the same one the session cookie uses."""
    return container.settings.session_signing_key()


def _expiry(book_started: str, retention_days: int) -> int:
    """The latest a link may work: the moment the book is deleted, or 30 days."""
    soonest = datetime.now(UTC) + timedelta(days=SHARE_LIFETIME_DAYS)
    try:
        started = datetime.fromisoformat(book_started)
    except ValueError:
        return int(soonest.timestamp())
    if started.tzinfo is None:
        started = started.replace(tzinfo=UTC)
    swept = started + timedelta(days=retention_days)
    return int(min(soonest, swept).timestamp())


def _link(token: str, container: ContainerDep) -> ShareLink:
    """The book a token names, or a 404 that says nothing it should not."""
    found = read(token, key=_key(container))
    if found is None:
        raise HTTPException(
            status_code=404, detail="This link has expired, or it is not a link to a book."
        )
    return found


def _render(request: Request, name: str, context: dict[str, Any]) -> HTMLResponse:
    templates = request.app.state.templates
    return cast(
        HTMLResponse, templates.TemplateResponse(request=request, name=name, context=context)
    )


@router.get("/books/{book_id}/share")
async def make_share_link(
    book_id: str, container: ContainerDep, session: CurrentSessionDep
) -> RedirectResponse:
    """Mint a read-only link for a finished book and show it on the book page.

    The owner's page is the only place a link can be asked for: it is minted
    behind the same ownership check as the book itself, so nobody else can
    make one, and nothing is stored — the token is the whole grant.
    """
    await asyncio.to_thread(ensure_book, book_id, user_id(session), container)
    record = await asyncio.to_thread(container.book_store.load_record, book_id)
    status = await asyncio.to_thread(container.book_store.load_status, book_id) or {}
    if record is None:
        raise HTTPException(status_code=404, detail="No book with that id.")
    if status.get("status") != BookStatus.COMPLETE:
        raise HTTPException(
            status_code=409, detail="A book can be shared once it is finished."
        )
    token = issue(
        book_id,
        key=_key(container),
        expires_at=_expiry(record.created_at, container.settings.book_retention_days),
    )
    logger.info("share.minted user=%s book=%s", user_id(session) or "-", book_id)
    return RedirectResponse(url=f"/books/{book_id}?share={token}", status_code=303)


@router.get("/share/{token}", response_class=HTMLResponse)
async def shared_book(
    token: str, request: Request, container: ContainerDep, session: CurrentSessionDep
) -> HTMLResponse:
    """One finished book, read-only, for whoever holds the link."""
    link = _link(token, container)
    book_id = link.book_id
    record, status = await asyncio.gather(
        asyncio.to_thread(container.book_store.load_record, book_id),
        asyncio.to_thread(container.book_store.load_status, book_id),
    )
    status = status or {}
    if record is None:
        raise HTTPException(status_code=404, detail="This book is no longer here.")
    if status.get("status") != BookStatus.COMPLETE:
        raise HTTPException(status_code=404, detail="This book is not ready to be shared.")

    edition = book_edition(container, book_id)
    drawn: set[int] = set()
    stored = status.get("pages")
    if isinstance(stored, list):
        for entry in stored:
            if not isinstance(entry, dict):
                continue
            number = entry.get("number")
            if isinstance(number, int) and entry.get("status") == "done":
                drawn.add(number)
    pages = [
        {
            "number": page.number,
            "title": page.scene.title,
            "story": render_book_page(
                page, record.settings, prompt_name=record.about.prompt_name
            ).story,
            "art": f"/share/{token}/pages/{page.number}",
            "drawn": page.number in drawn,
        }
        for page in edition.pages
    ]
    heading, *sentences = (book_text(line) for line in record.about.rendered_about_lines())
    chrome = await asyncio.to_thread(page_chrome, container, session, request)
    response = _render(
        request,
        "share.html",
        {
            **chrome,
            "title": record.title,
            "subtitle": edition.subtitle_for(record.about.prompt_name),
            "cover_url": f"/share/{token}/cover",
            "pages": pages,
            "about_heading": heading,
            "about_sentences": sentences,
            "pdf_url": f"/share/{token}/pdf",
            "good_until": datetime.fromtimestamp(link.expires_at, UTC).isoformat(),
            "noindex": True,
        },
    )
    # The token *is* the grant, and it is in the URL. Without this, any image
    # or link the page loads sends the whole link in a `Referer` header to a
    # server that was never meant to have it.
    response.headers["Referrer-Policy"] = "no-referrer"
    return response


@router.get("/share/{token}/cover")
async def shared_cover(token: str, request: Request, container: ContainerDep) -> Response:
    """The cover picture, for a token holder."""
    data = await asyncio.to_thread(
        container.book_store.read_cover, _link(token, container).book_id
    )
    if data is None:
        raise HTTPException(status_code=404, detail="There is no cover to show.")
    return private_cached(request, data, media_type=media_type_for(data))


@router.get("/share/{token}/pages/{number}")
async def shared_page(
    token: str, number: int, request: Request, container: ContainerDep
) -> Response:
    """One page's art, for a token holder."""
    book_id = _link(token, container).book_id
    data = await asyncio.to_thread(container.book_store.read_page, book_id, number)
    if data is None:
        raise HTTPException(status_code=404, detail="There is no picture on that page.")
    return private_cached(request, data, media_type=media_type_for(data))


@router.get("/share/{token}/pdf")
async def shared_pdf(token: str, container: ContainerDep) -> Response:
    """The assembled book, as a download, for a token holder."""
    book_id = _link(token, container).book_id
    data = await asyncio.to_thread(container.book_store.read_pdf, book_id)
    if data is None:
        raise HTTPException(status_code=404, detail="The book is not ready yet.")
    return no_store(
        data,
        media_type="application/pdf",
        filename="picture-book.pdf",
        download=True,
    )
